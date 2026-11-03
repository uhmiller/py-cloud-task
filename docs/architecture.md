# Architecture

How `py-cloud-task` works inside. For usage, see the [README](../README.md).

## Flow

```
producer (API request, task, scheduler)              Google Cloud Tasks               worker (same or other service)
───────────────────────────────────────              ──────────────────               ──────────────────────────────
@client.task  → registered in client.registry        queue (rate limits,              POST <url> (+ secret header,
async def send_email(user_id): ...                   retries with backoff,             OIDC token, X-CloudTasks-*)
                                                     dedup by task name)                     │
task = send_email("123")   # Task (nothing sent)                                             ▼
await task.push(at=...) ── create_task(HTTP task) ──► dispatch when due ──────────────► CloudTaskRouter (ext.fastapi)
                                                     non-2xx → retry                   CloudTaskExecutor
                                                                                       ├ authenticate (secret)
                                                                                       ├ validate payload
                                                                                       ├ registry lookup of body["path"]
                                                                                       ├ (opt.) coerce arguments
                                                                                       ├ set TaskContext
                                                                                       ├ FastAPI: solve Depends(...)
                                                                                       └ run → response = retry decision
```

## Modules

| Module | Role | Depends on |
|---|---|---|
| `core.py` | `CloudTaskClient` (configuration, `@task`, registry), `Task` (payload, headers, names, push, eager modes) | `google-cloud-tasks`, stdlib |
| `executor.py` | `CloudTaskExecutor`: authenticate, validate, resolve, run (sync and async) | stdlib (`asgiref` optional) |
| `auth.py` | `WorkerAuthenticator`: fail closed, constant-time secret check | stdlib |
| `context.py` | `TaskContext`, `current_task()` (a `ContextVar`) | stdlib |
| `validation.py` | `ArgumentValidator` for `validate_args` | pydantic (lazy import) |
| `testing.py` | `PushedTask`, `PushedTasks`, `recorder()` | stdlib |
| `encoder.py` | JSON encoder (datetime, Decimal, UUID, sets, Pydantic models) | stdlib |
| `exceptions.py` | `CloudTaskException` hierarchy | — |
| `ext/fastapi.py` | `CloudTaskRouter`, `FastAPICloudTaskExecutor`, `task_context` dependency | FastAPI (`fastapi` extra) |

`core`, `executor`, `auth`, `context` and `testing` must stay framework-agnostic.

## Producer side (`core.py`)

### `CloudTaskClient`

Holds the configuration (project, location, queue, worker `url`, `sae`, `secret`, `timezone`, `force_to_queue`,
`eager`, `add_url_func_name_suffix`, `context_headers`, `validate_args`, `retry_unknown_tasks`, …). The Google
clients are created lazily on the first real push.

`client.task(...)` is a decorator. At decoration it validates the static options (timeout bounds, a static name),
builds the `ArgumentValidator` when `validate_args` applies (attached to the factory as
`__cloudtask_validator__`) and registers the factory in `client.registry` under `module.qualname`. Calling the
factory renders the task name (template or callable, bound with `inspect.signature(...).bind_partial`) and returns
a `Task`.

Precedence for every option: client → decorator → task instance → `push()` argument.

### `Task`

- **Payload**: `{"func": <name>, "path": "<module>.<qualname>", "args": [...], "kwargs": {...}}`, encoded by
  `_encode_body()`. An unencodable argument raises `TaskPushError` naming the argument (never its value); a body
  over `MAX_PAYLOAD_BYTES` (1 MB) raises `TaskPushError`.
- **Headers** (`_build_headers`): merged case-insensitively, lowest → highest: `client.context_headers()` (a
  failing provider is logged and skipped) → decorator → `task.headers` → `push(headers=)`. Reserved Cloud Tasks
  headers raise `ValueError`. The secret header and `Content-Type` are applied last.
- **Name**: validated on assignment (`[A-Za-z0-9_-]{1,500}`); the task path uses `force_to_queue` like the parent.
  `AlreadyExists` from Google → logged, returns the existing path (`if_exists="raise"` → `TaskAlreadyExists`).
- **Timeout** → `dispatch_deadline` (15 s – 30 min). **`at`** → `schedule_time`; naive datetimes get the client
  timezone with a warning.
- `execute()` runs the function in-process (used by the worker and by `immediate` mode).

### Eager modes

| `eager` | `push()` |
|---|---|
| `None` | Creates the task in Cloud Tasks |
| `"immediate"` | Runs the task in-process inside a `TaskContext` (retry 0). `sync_push` runs async tasks with `asyncio.run`, or raises when a loop is running. `TaskAbort` is logged, not raised |
| `"remote"` | POSTs the payload to the worker URL with httpx in the background (a task referenced in `_background_tasks` until done, or a daemon thread for `sync_push`). No OIDC token: the local worker authenticates with the secret |
| `"record"` | Validates like production and appends a `PushedTask` to `client.pushed`; emulates `AlreadyExists` for repeated names |

## Worker side

### `CloudTaskExecutor` (`executor.py`)

`run(body, headers)` / `sync_run(...)`:

1. `client.authenticator.authenticate(headers)`. The authenticator is built on first access and raises
   `ImproperlyConfigured` without a secret (unless `insecure_allow_unauthenticated`); the executor builds it in
   `__init__`, so a misconfigured worker fails at startup.
2. Payload checks → `InvalidTaskPayload`: body is an object, `path` a non-empty string, `args` a list, `kwargs` an
   object, arguments bind (partially) to the signature.
3. `client.get_task(path)` → `TaskNotFound`. Nothing is ever imported from request data.
4. Optional `ArgumentValidator.validate` → `InvalidTaskPayload` (parameter names and messages, no values).
5. Sets the `TaskContext` (from `X-CloudTasks-*` headers; the secret, `Authorization` and cookies are hidden) for
   the duration of the run, then calls the factory and executes the `Task`.

`TaskAbort`, `TaskRetry` and `InvalidTaskPayload` propagate unchanged; any other exception becomes
`TaskExecutionError` (chained).

### FastAPI extension (`ext/fastapi.py`)

- `CloudTaskRouter(client)` registers `POST ""`, touches `client.authenticator` (fail closed at startup) and parses
  the JSON body itself, so authentication runs before payload validation (FastAPI would answer 422 first).
- `FastAPICloudTaskExecutor`:
  - with `add_url_func_name_suffix`, rejects a request whose last URL segment isn't the task's function name;
  - caches `get_dependant(...)` per function and copies it per run, dropping the parameters the payload provides;
  - solves dependencies with `app` as `dependency_overrides_provider`, on an `AsyncExitStack` owned by the run
    (it temporarily replaces `fastapi_inner_astack` / `fastapi_function_astack` in the request scope), so `yield`
    dependencies close right after the task and receive its exception;
  - dependency errors (missing task arguments) → `InvalidTaskPayload`.
- `task_context()` is the dependency returning the current `TaskContext`.

Response mapping (Cloud Tasks retries every non-2xx):

| Outcome | Response |
|---|---|
| success | 200 `{"status": "success"}` (the result is never serialized) |
| `TaskAbort` | 200 `{"status": "aborted", "reason": ...}` |
| `InvalidTaskPayload`, `TaskNotFound` | 200 `{"status": "rejected", "error": ...}`; `TaskNotFound` → 404 with `retry_unknown_tasks` |
| `TaskAuthenticationError` | 403 |
| `TaskRetry` | 503 |
| `TaskExecutionError` | 500 |

## Security model

- **Registry**: only functions decorated with `@client.task` (and imported) can run.
- **Fail closed**: no secret → the worker refuses to start.
- **Secret**: `X-PYCT-SECRET` (configurable), `hmac.compare_digest`, header names case-insensitive (WSGI
  `HTTP_*` names accepted).
- **OIDC**: Cloud Tasks attaches a token for `sae`; the library does not verify it (decision: the shared secret is
  the only library-level authentication). It matters only when Cloud Run itself requires authentication.
- **Logs**: task paths, names and queues only; never arguments, payloads or header values.

## Consumers

| Project | Setup |
|---|---|
| Ziett API | `ziett/core/tasks.py` (client), `ziett/tasks/*` (tasks), router at `/o/tasks/{name}`, events push tasks |
| Única Peça API | `unica/core/tasks.py` (client), `unica/tasks/*` (tasks), router at `/o/tasks/{name}`, events push tasks |

Both mount the router in the same service as the API, use `add_url_func_name_suffix=True` and FastAPI dependency
injection for the DB session. Both import every task module through their `tasks` package, which keeps the
registry complete.
