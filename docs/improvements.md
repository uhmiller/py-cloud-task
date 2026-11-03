# Improvement plan

Review of `py-cloud-task` 0.2.1 (`development`, commit `2afd7f2`), done while adopting it in the Única Peça API
(November 2026). Items are ordered by priority. Each one has: the problem (with the code it refers to), the
proposal, and the tests that prove it. Line numbers refer to 0.2.1.

| # | Item | Priority | Breaking? |
|---|---|---|---|
| 1 | Worker runs any importable function; fails open without a secret ✅ done (0.3.0, unreleased) | **Critical** | Yes (only for misconfigured setups) |
| 2 | Context headers: propagate request id / trace to the task ✅ done (0.3.0, unreleased) | High | No |
| 3 | Duplicate task name is an error instead of "already scheduled" ✅ done (0.3.0, unreleased) | High | Minor |
| 4 | Per-call task names (template) instead of a static decorator `name` ✅ done (0.3.0, unreleased) | High | No |
| 5 | No way to stop retries (permanent failures) ✅ done (0.3.0, unreleased) | High | No |
| 6 | Task result is serialized into the response (can turn success into a retry) ✅ done (0.3.0, unreleased) | High | Minor |
| 7 | Task arguments are not validated or coerced | Medium | No (opt-in) |
| 8 | Task metadata (retry count, task name) not available to the task | Medium | No |
| 9 | Testing support: record pushes instead of running them | Medium | No |
| 10 | Eager-mode bugs (`sync_push` with async tasks; `remote` loses tasks) | Medium | No |
| 11 | Push-side validation (payload size, deadline bounds, naive datetimes) | Low | No |
| 12 | Logging, typing and housekeeping | Low | No |
| 13 | Test suite and CI ✅ done (0.3.0, unreleased) | **Required** for all of the above | — |
| 14 | FastAPI: dependency overrides crash the task (bug) ✅ done (0.3.0, unreleased) | **High** | No |

---

## 1. Security: only run registered tasks, fail closed

### Problem

`CloudTaskExecutor._prepare_execution` (executor.py:36-54) takes `body["path"]` from the request, and
`resolve_function` (executor.py:113-126) imports **any** module and gets **any** attribute. `_execute_impl`
(executor.py:75-88) then calls it: `task: Task = func(*args, **kwargs)`. The check that it is a task only happens
afterwards, when `.execute()` fails on a non-`Task` result. By then the function has already run.

Reproduced on 0.2.1 with a harmless function, no secret configured:

```python
client = CloudTaskClient(queue="q", project="p", location="l", url="http://x", sae="", secret="")
await CloudTaskExecutor(client).run({"path": "platform.python_version", "args": []}, {})
# platform.python_version() runs, then: AttributeError: 'str' object has no attribute 'execute'
```

With `{"path": "os.system", "args": ["<command>"]}` the command runs on the server. The guard is the secret, but:

- the secret is optional (`secret: str = ""`, core.py:373) and only checked when non-empty
  (`if self.client.secret:`, executor.py:39). An environment where the secret variable is missing exposes
  the endpoint (in the Ziett API, `GCT_SECRET` defaults to `""` and nothing enforces it);
- the OIDC token Cloud Tasks attaches is never verified by the library. It only helps when Cloud Run itself
  requires authentication, which is not the case when the API and the worker share a public service;
- the secret is compared with `!=` (executor.py:68): not constant time.

### Proposal

1. **Registry.** `CloudTaskClient.task()` registers each decorated function in `client.registry:
   dict[str, Callable]`, keyed by its dotted path (`f"{func.__module__}.{func.__qualname__}"`). The executor looks
   the path up in the registry and never imports from request data. Unknown path → `TaskNotFound`.
   - Because tasks register on import, the app must import its task modules at startup (it already does when
     the router or the producers import them). Add `client.autodiscover("myapp.tasks")` as a helper that
     imports a package and its submodules.
   - Two functions with the same path (re-import, reload) → keep the last one and log a debug line.
2. **Fail closed.** The executor refuses to run (raise `ImproperlyConfigured` at router creation, or 403 on every
   request) unless **at least one** authentication method is configured:
   - `secret` (non-empty), compared with `hmac.compare_digest`; and/or
   - ~~`verify_oidc=True`: verify the OIDC token~~ — **dropped** (maintainer decision, November 2026): the
     shared secret is the only worker authentication; the library does not verify Google OIDC tokens.
   - Escape hatch for local development only: `insecure_allow_unauthenticated=True`, which logs a warning on
     startup and must never be set by default.
3. Normalise header names once (they are case-insensitive): keep the existing normalisation, drop the
   `http-` prefix handling only if Django support is not planned.

### Tests

- Unknown dotted path (`os.system`, `platform.python_version`) → `TaskNotFound`, function not called (assert
  with a module-level spy registered under a different name).
- Registered task runs.
- No secret and no OIDC → router creation (or request) refused.
- Wrong secret → 403; right secret → runs; comparison uses `compare_digest` (mock it).

---

## 2. Context headers (request id, trace) — new feature

### Why

When an API request pushes a task, the task's logs, spans and errors should be linked to that request. Both
consumers already read `X-Request-ID` in a middleware (Única Peça accepts a valid incoming one and tags Logfire
and Sentry with it), and OpenTelemetry (Logfire) and Sentry continue a trace from `traceparent` /
`sentry-trace` + `baggage` headers automatically in their FastAPI integrations. So the task only needs **headers
captured at push time**.

Today headers can be set per decorator (`headers=`) or per instance (`task.headers = {...}`, core.py:320), but
there is no hook to add request-scoped headers to every push, and `push()` takes no headers.

### Proposal

1. Client hook, called on every push (async, sync and `remote`):

   ```python
   def context_headers() -> dict[str, str]:
       carrier: dict[str, str] = {}
       opentelemetry.propagate.inject(carrier)      # traceparent, tracestate (and baggage)
       if request_id := get_request_id():
           carrier["X-Request-ID"] = request_id
       return carrier

   client = CloudTaskClient(..., context_headers=context_headers)
   ```

   The library stays agnostic: it calls the provider and merges the result; it doesn't import OpenTelemetry or
   Sentry.
2. Per-call headers: `await task.push(at=..., headers={"X-Foo": "bar"})`.
3. Precedence (lowest → highest): `context_headers()` → decorator `headers` → `task.headers` → `push(headers=)`.
   Then the library sets `Content-Type` and the secret header last; they can't be overridden.
4. Reject headers Cloud Tasks reserves or overrides (`Host`, `Content-Length`, `User-Agent`, `X-Google-*`,
   `X-AppEngine-*`, `X-CloudTasks-*`, `Authorization` when OIDC is used) with a clear `ValueError` at push time.
5. A failing provider must not block the push: catch, log once (`warning`, no header values), push without them.
6. Worker side: nothing to do in the library. Document that the app's middleware and tracing integrations pick
   the headers up. Optionally expose them through `TaskContext` (§8).

### Tests

- Provider headers present in the built request (async, sync, remote).
- Precedence order; secret and `Content-Type` can't be overridden.
- Reserved header → `ValueError`.
- Provider raising → push still happens, warning logged.

---

## 3. Duplicate task name = "already scheduled", not an error

### Problem

Setting `task.name` is how callers get deduplication. But when Cloud Tasks answers `ALREADY_EXISTS`, `push()`
goes to `_handle_error` (core.py:290-294) and raises `TaskPushError`, so every caller must catch and inspect a
generic error to tell "duplicate" from "failure". Note also that Cloud Tasks keeps a name reserved for about an
hour after the task is deleted or executed, so a repeated name may be rejected even when no task is pending.

### Proposal

- Catch `google.api_core.exceptions.AlreadyExists` and treat it as success: log at `info`
  (`"Task %s already exists; not pushed again"`) and return the task path.
- Optional `push(..., if_exists="ignore" | "raise")`, default `"ignore"`; `"raise"` raises a dedicated
  `TaskAlreadyExists(TaskPushError)` (subclass, so existing `except TaskPushError` still works).
- Same behaviour in `sync_push`.

### Tests

Mock the Google client to raise `AlreadyExists` → no exception, path returned; with `if_exists="raise"` →
`TaskAlreadyExists`. Other `GoogleAPICallError` → `TaskPushError` with the cause chained.

---

## 4. Task names per call

### Problem

`@client.task(name="…")` (core.py:426, 455) gives **every** call the same name, so the second push of that task
ever is rejected. Deduplication almost always needs a name per entity (`expire-order-<id>`,
`open-drop-<id>`), which today means `task = f(...); task.name = "..."; await task.push()`.

### Proposal

- Let the decorator take a template or a callable:

  ```python
  @client.task(name="expire-order-{order_id}")
  async def expire_order(order_id: str, db=Depends(get_db)): ...

  @client.task(name=lambda order_id, **_: f"expire-order-{order_id}")
  ```

  Bound with `inspect.signature(func).bind_partial(*args, **kwargs)` so positional calls work too.
- Validate the result against Cloud Tasks rules (`[A-Za-z0-9_-]{1,500}`) and fail fast with `ValueError`
  (UUIDs are fine; dots and colons are not).
- Keep `task.name = ...` and add `push(name=...)` for one-off overrides.
- A static string without placeholders keeps working but logs a warning once ("every call will share this
  name").

### Tests

Template and callable produce the expected task path; invalid characters → `ValueError`; static name warns.

---

## 5. Permanent failures: stop the retries

### Problem

Cloud Tasks retries **every** non-2xx response. The router maps any `TaskExecutionError` to 500
(ext/fastapi.py:119-123), so a task that can never succeed (the order was already paid, the entity was deleted,
the input is invalid) is retried until `max_attempts`, filling logs and error trackers. The same applies to
`TaskNotFound` (404): after a deploy that removes a task, its pending tasks are retried until they run out.

### Proposal

- New exceptions in `cloudtask.exceptions`:
  - `TaskAbort(message)`: the task decides not to continue. Router returns **200** with
    `{"status": "aborted", "reason": ...}` and logs at `warning`. Cloud Tasks stops.
  - (optional) `TaskRetry(after: timedelta | None)`: explicit retry, returns 503 (and `Retry-After` is ignored by
    Cloud Tasks, so document that backoff is the queue's).
- Unknown task (`TaskNotFound`) and invalid payload: document the choice. Recommended default: **200** with
  `status: "rejected"` and an `error` log, because retrying can't fix them; configurable with
  `retry_unknown_tasks=True` for blue/green deploys where the new code may not be live yet.
- Invalid secret / OIDC: keep 403 (an attacker getting retries is irrelevant; a misconfiguration should be loud).

### Tests

Task raising `TaskAbort` → 200 and no retry status; generic exception → 500; unknown task → 200 rejected
(or 404 with the flag).

---

## 6. Don't put the task result in the response

### Problem

`CloudTaskRouter._run_task` returns `{"status": "success", "result": result}` with
`response_model=dict[str, Any]` (ext/fastapi.py:98, 111). If the task returns something that can't be
serialized (an ORM object, a dataclass, a `set`), serialization fails **after the task ran** → 500 → Cloud Tasks
retries → the task runs again. Results may also carry personal data into Cloud Tasks / load balancer logs. (In
the Ziett API, `task_dispatch_message` returns the service result.) To confirm with a test.

### Proposal

Respond `{"status": "success"}` only (or `204`). If a result is ever needed, log it at `debug` in the executor.
Breaking only for anyone reading the response body, which Cloud Tasks doesn't.

### Tests

A task returning an object that isn't JSON-serializable → 200.

---

## 7. Validate and coerce task arguments

### Problem

Arguments travel as JSON and are passed to the function as they arrive (executor.py:28-29, ext/fastapi.py:82-85):
a `datetime` arrives as `str`, a `Decimal` as `str`, an enum as its value, a Pydantic model as `dict`. Type hints
in task signatures are therefore misleading, and errors appear deep inside the task.

### Proposal

- Opt-in per client or per task: `validate_args=True` wraps the function with `pydantic.validate_call`
  (excluding parameters that are FastAPI dependencies) so arguments are validated and coerced to the annotated
  types on the worker. Validation error → permanent failure (§5), not a retry.
- Push side: serialize at `push()` (already happens) and raise `TaskPushError` with the offending argument name
  when encoding fails.
- Document the recommended practice anyway: pass ids and small primitives, read the rest from the database
  inside the task (state may have changed since the push).

### Tests

`datetime`, `Decimal`, enum and model arguments arrive with the annotated types; invalid argument → aborted
(200) with a validation message.

---

## 8. Task metadata for the task

### Why

Tasks need to know whether this is a retry (idempotency, logging), and logs should carry the task name. Cloud
Tasks sends `X-CloudTasks-QueueName`, `X-CloudTasks-TaskName`, `X-CloudTasks-TaskRetryCount`,
`X-CloudTasks-TaskExecutionCount`, `X-CloudTasks-TaskETA` (and `X-CloudTasks-TaskPreviousResponse`,
`X-CloudTasks-TaskRetryReason`).

### Proposal

```python
@dataclass(frozen=True)
class TaskContext:
    path: str
    queue: str | None
    task_name: str | None
    retry_count: int
    execution_count: int
    eta: datetime | None
    headers: Mapping[str, str]   # incoming headers (e.g. X-Request-ID)
```

- FastAPI: `ctx: TaskContext = Depends(task_context)` in the task signature.
- Agnostic: a `contextvars.ContextVar` set by the executor around execution (`cloudtask.current_task()`).
- In eager `immediate` mode, a context with `retry_count=0` and the pushed headers.

### Tests

Headers → `TaskContext` fields; dependency available in FastAPI tasks; contextvar reset after execution.

---

## 9. Testing support

### Problem

Consumers want unit tests that assert "this task was pushed with these arguments" without running it, and
separate tests that call the task directly. Today the options are `eager="immediate"` (runs everything inline,
mixing concerns) or monkeypatching `Task.push`.

### Proposal

- `eager="record"`: `push()` appends to `client.pushed: list[PushedTask]` (path, args, kwargs, name, queue,
  schedule time, headers) and returns a fake name. Add `client.pushed.clear()`.
- `cloudtask.testing` module with a pytest fixture factory:

  ```python
  from cloudtask.testing import recorder
  pushed = recorder(client)   # context manager / fixture; restores the previous mode on exit
  assert pushed.last().path == "app.tasks.expire_order"
  ```

### Tests

Recording mode records and doesn't call Google or the function; restores the previous mode.

---

## 10. Eager-mode bugs

1. **`sync_push` with an async task in `immediate` mode doesn't run it** (core.py:131-140): it logs a warning and
   calls `self.execute()`, which returns a coroutine that is never awaited (`RuntimeWarning: coroutine … was never
   awaited`). Fix: run it with `asyncio.run` when no loop is running, or raise a clear error when one is.
2. **`remote` mode may lose tasks** (core.py:187-200): `asyncio.create_task(_send())` without keeping a reference;
   the task can be garbage-collected before it runs. Keep a module-level `set` of pending tasks with
   `add_done_callback(set.discard)` (the Ziett event dispatcher already does this).
3. `remote` posts without the task-name URL suffix logic being shared with production (it uses `_get_url()`, ok)
   but without OIDC: document that the local worker must allow unauthenticated requests or use the secret.

### Tests

Both cases with a spy task; `remote` with a mock transport.

---

## 11. Push-side validation

- **Payload size.** Cloud Tasks rejects tasks over 1 MB (HTTP tasks). Check `len(json_body)` before calling
  Google and raise `TaskPushError("payload too large: N bytes")`.
- **Dispatch deadline.** HTTP tasks accept 15 s – 30 min; validate `timeout` when the task is created.
- **Naive datetimes.** Silently applying the client timezone (core.py:275-279) hides bugs. Prefer raising
  `ValueError` for naive `at` (or at least `warning`), and document "pass aware datetimes".
- **Schedule in the past.** Allowed by Cloud Tasks (runs now); fine, but log at debug.

---

## 12. Logging, typing and housekeeping

- Use lazy logging (`logger.info("Task created: %s", name)`) instead of f-strings; never log args, payloads or
  header values. Current messages are fine content-wise.
- Translate the Portuguese comments (core.py:199, 239; workflow) to English.
- Remove the `delay` / `sync_delay` aliases, or document them as aliases (two names for one thing).
- `ext/fastapi.py`: cache `get_dependant(...)` per function (it is rebuilt on every execution).
- `CloudTaskRouter` is mounted under prefixes like `/tasks/{name}` while the function comes from the body; when
  `add_url_func_name_suffix` is on, check that the path segment matches `body["func"]` (useful in logs, cheap
  sanity check).
- `pyproject.toml`: the `Framework :: Django` classifier claims support that doesn't exist yet; README badges point
  to `ziett/py-cloud-task`, the repository is `uhmiller/py-cloud-task`; the `test` extra installs `httpx` only for
  `remote` mode — rename to `remote` or document it.
- README: document security (registry, fail closed), context headers, task names, `TaskAbort`, testing.
- `CHANGELOG.md` with the breaking changes (§1, §6) and a version bump to 0.3.0.

---

## 13. Test suite and CI

There are no tests in the repository and the publish workflow has the test step commented out.

- `tests/` with pytest + `pytest-asyncio`: one module per area (`test_core.py`, `test_executor.py`,
  `test_fastapi.py`, `test_eager.py`, `test_headers.py`). Mock the Google clients; use `httpx.ASGITransport` for the
  FastAPI router.
- CI workflow on pull requests: `uv sync --all-extras --dev`, `ruff check`, `ruff format --check`, `pytest`.
  Re-enable tests in the publish workflow.

---

## 14. FastAPI: dependency overrides crash the task (bug)

### Problem

`FastAPICloudTaskExecutor._execute_impl` (ext/fastapi.py:66-73) calls

```python
solve_dependencies(..., dependency_overrides_provider=self.request.app.dependency_overrides, ...)
```

FastAPI expects the **provider** (the app, an object with a `dependency_overrides` attribute), not the dict:
`fastapi/dependencies/utils.py` does `dependency_overrides_provider.dependency_overrides`. With no overrides the
dict is empty (falsy) and the branch is skipped, so production works; as soon as any override exists (every
test suite that overrides `get_db`), the task fails with
`AttributeError: 'dict' object has no attribute 'dependency_overrides'` → 500 → retry.

Found in Única Peça: the worker test with the usual `get_db` override fails; it is marked
`xfail(strict=True)` there until this is fixed.

Also check, while here: recent FastAPI versions no longer use the `async_exit_stack` argument of
`solve_dependencies` (a comment in FastAPI says it is kept only for compatibility). Dependencies with `yield`
(the DB session) are closed through the request scope's exit stacks instead. Confirm that the session opened for
a task is closed after the task (and not only at the end of the HTTP request), and that an exception in the task
rolls it back.

### Proposal

```python
dependency_overrides_provider=self.request.app,
```

Plus the checks above.

### Tests

FastAPI app with `app.dependency_overrides[get_db] = fake_db`; a task with `db = Depends(get_db)` receives the fake;
a `yield` dependency's teardown runs after the task, also when the task raises.

---

## Consumer notes (Única Peça API)

- Installed from the local checkout while these changes are made:
  `[tool.uv.sources] py-cloud-task = { path = "../../../py-cloud-task", editable = true }`. Switch back to the
  published version (or a git tag) before building the Docker image.
- Until §1 is released, Única Peça requires the task secret in staging and production (settings validation) and
  uses a development secret locally, so the endpoint is never open.
- Until §2 is released, task requests carry no request id; the Única Peça middleware already accepts
  `X-Request-ID`, so adding the client hook is the only change needed there.
- Única Peça sends sign-in codes synchronously on purpose (the code would otherwise sit in plain text in the task
  payload). Not a library concern, but worth remembering when tempted to make everything a task.
