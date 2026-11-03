# Architecture

## Flow

```
producer (API request, task, scheduler)                Google Cloud Tasks                 worker (same or other service)
───────────────────────────────────────                ──────────────────                 ──────────────────────────────
@client.task                                           queue (rate limits,                POST <url> (+ OIDC token, secret header)
async def send_email(user_id): ...                     retries with backoff,                     │
                                                       dedup by task name)                       ▼
task = send_email("123")   # Task object (nothing sent)                                   CloudTaskRouter (ext.fastapi)
await task.push(at=...)  ──── create_task(HTTP request) ──►  dispatch at schedule time ──►  CloudTaskExecutor
                                                       non-2xx → retry                     ├ validate secret
                                                                                           ├ resolve body["path"] → function
                                                                                           ├ FastAPI: solve Depends(...)
                                                                                           └ run → 2xx (done) / 5xx (retry)
```

## Pieces

### `CloudTaskClient` (`core.py`)

Holds the configuration: GCP `project`, `location`, default `queue`, worker `url`, service account email `sae`
(for the OIDC token Cloud Tasks attaches), optional shared `secret` (sent as `X-PYCT-SECRET`), `timezone` for
naive datetimes, `force_to_queue` (send everything to one queue, e.g. staging), `eager` mode, and
`add_url_func_name_suffix` (worker URL becomes `<url>/<function name>`, handy for logs and metrics).

`client.task(queue=, name=, url=, timeout=, headers=)` is a decorator. The decorated function, when called,
does **not** run: it returns a `Task` holding the function and its arguments.

The Google clients (`CloudTasksAsyncClient`, `CloudTasksClient`) are created lazily on first push.

### `Task` (`core.py`)

- `push(at=None)` (async) / `sync_push(schedule_time=None)`: build the Cloud Tasks request and create it.
  Returns the task resource name.
- Payload (JSON body): `{"func": <name>, "path": "<module>.<function>", "args": [...], "kwargs": {...}}`,
  encoded with `CloudTaskJSONEncoder`.
- HTTP request: `POST url`, headers (custom + secret + `Content-Type`), OIDC token when `sae` is set.
- `name` → full task path (`projects/…/queues/…/tasks/<name>`): Cloud Tasks rejects a second task with the same
  name (deduplication, for about an hour after the first one is deleted or executed).
- `timeout` → `dispatch_deadline` (how long Cloud Tasks waits for the worker; 15 s to 30 min for HTTP tasks).
- `at` → `schedule_time`; naive datetimes get the client timezone.
- `execute()` runs the function in the current process (used by the worker and by eager mode).

### Eager modes (development and tests)

| `eager` | `push()` does |
|---|---|
| `None` | Creates the task in Google Cloud Tasks (production). |
| `"immediate"` | Runs the function in-process, awaiting it (unit tests, simple local dev). |
| `"remote"` | Fire-and-forget HTTP POST of the payload to the worker URL (simulates Cloud Tasks against a second local process). |

### `CloudTaskExecutor` (`executor.py`)

Framework-agnostic worker logic: authenticate the request (shared secret, see below), read `path`, `args`,
`kwargs` from the body, look the task up in the client registry, call it to get the `Task`, then `execute()` it
(awaiting coroutines; `sync_run` uses `asgiref` for async tasks in sync frameworks).

### FastAPI extension (`ext/fastapi.py`)

- `CloudTaskRouter(client)` registers `POST ""` (mount it under a prefix such as `/tasks/{name}`).
- `FastAPICloudTaskExecutor` resolves FastAPI dependencies declared in the task signature
  (`db: AsyncSession = Depends(get_db)`), skipping parameters that the payload already provides, and runs the task
  inside an `AsyncExitStack` owned by the execution, so `yield` dependencies (the DB session) are closed right
  after the task and receive the task's exception (rollback). Overrides (`app.dependency_overrides`) apply.
- Error mapping: authentication failure → 403, unknown task → 404, task failure → 500. Every non-2xx response makes
  Cloud Tasks retry.

## Security model

- **Registry.** `@client.task` registers the function in `client.registry` (key: `module.qualname`). The executor
  resolves `body["path"]` only through the registry (`client.get_task`); it never imports from request data.
  Task modules must be imported at startup (`client.autodiscover("app.tasks")` helps).
- **Fail closed.** `CloudTaskExecutor` and `CloudTaskRouter` build `client.authenticator`
  (`cloudtask.auth.WorkerAuthenticator`), which raises `ImproperlyConfigured` without a `secret`.
  `insecure_allow_unauthenticated=True` is the local-development escape hatch.
- **Secret.** `X-PYCT-SECRET` (configurable), compared with `hmac.compare_digest`. Failure →
  `InvalidTaskSecretError` (a `TaskAuthenticationError`) → 403.
- **OIDC.** Cloud Tasks attaches an OIDC token for `sae`, but the library does not verify it (decision: shared
  secret only). It only helps when Cloud Run itself requires authentication.

## Consumers

| Project | Setup |
|---|---|
| Ziett API | `ziett/core/tasks.py` (client), `ziett/tasks/*` (tasks), router at `/o/tasks/{name}`, events push tasks |
| Única Peça API | `unica/core/tasks.py` (client), `unica/tasks/*` (tasks), router at `/o/tasks/{name}`, events push tasks |

Both mount the router in the same service as the API and use FastAPI dependency injection for the DB session.
