# py-cloud-task — guide for AI assistants

`py-cloud-task` (import name `cloudtask`) is a small, async-first client for **Google Cloud Tasks** in the
push model: a decorated function becomes a task; `.push()` sends an HTTP task to Cloud Tasks; Cloud Tasks
calls the worker endpoint back; the worker resolves the function and runs it. It is shared by the Ohrus
projects (Ziett API, Única Peça API), so **every change is a library change**: keep the public API stable,
document breaking changes and bump the version.

- Usage: [README.md](README.md). How it works inside: [docs/architecture.md](docs/architecture.md).
- Changes per version: [CHANGELOG.md](CHANGELOG.md). Add an entry for every user-visible change.

## Working rules

- **Never commit or push.** The maintainer reviews the uncommitted diff. Work on a branch from `development`
  (`fix/…`, `feat/…`, `chore/…`).
- **Never read `dist/`, `.env` or `.temp/`.** `dist/` is build output and may hold local credentials.
- Code, comments, docstrings, logs and docs in **English**. Talk to the maintainer in Portuguese.
- **Security first.** The worker endpoint is reachable from the internet: it must fail closed (no secret → refuse), only run registered tasks, and compare secrets in constant time.
- **No personal data in logs.** Never log task arguments, payloads or header values (they carry phone numbers,
  emails, tokens). Log the task path, the task name and the queue.
- **Framework-agnostic core.** `cloudtask.core`, `executor`, `auth`, `context` and `testing` depend only on
  `google-cloud-tasks` and the standard library (pydantic is imported lazily, only for `validate_args`). Framework code lives in `cloudtask.ext.<framework>` behind an optional extra.
- **Every behaviour change has tests** (`tests/`, pytest). Google's API is never called in tests: mock
  `CloudTasksAsyncClient` / `CloudTasksClient`.

## Layout

```
src/cloudtask/
  __init__.py        Public API: CloudTaskClient, Task, TaskContext, current_task, exceptions
  core.py            CloudTaskClient (config, @task decorator, registry) and Task (payload, headers, names,
                     push, eager modes: immediate / remote / record)
  auth.py            Worker authentication: WorkerAuthenticator (fail closed, shared secret in constant time)
  executor.py        CloudTaskExecutor: authenticates, validates, resolves from the registry, runs (sync and async)
  context.py         TaskContext + current_task() (ContextVar set around each run)
  validation.py      ArgumentValidator for validate_args (pydantic, lazy)
  testing.py         PushedTask(s) and recorder() for consumers' tests
  encoder.py         JSON encoder for payloads (datetime, Decimal, UUID, sets, Pydantic models)
  exceptions.py      CloudTaskException hierarchy
  ext/fastapi.py     CloudTaskRouter, FastAPICloudTaskExecutor (dependency injection), task_context dependency
docs/architecture.md Internals
tests/               pytest suite, one module per area (pytest-asyncio auto mode; Google always mocked)
```

## Commands

| Task | Command |
|---|---|
| Install (all extras + dev) | `uv sync --all-extras --dev` |
| Lint / format | `uv run ruff check .` / `uv run ruff format` |
| Tests | `uv run pytest` |
| Build | `uv build` |
| Release | tag `vX.Y.Z` on `main` → GitHub Actions publishes to PyPI |

Consumers install it locally while developing (e.g. Única Peça uses
`[tool.uv.sources] py-cloud-task = { path = "../../../py-cloud-task", editable = true }`), so changes here are
picked up there without publishing. Run the consumer's test suite before releasing.

## Conventions

- Python ≥ 3.14, full type hints (`py.typed` is shipped). Public functions and classes have docstrings.
- Keep the decorator API: `@client.task(...)` returns a factory; calling it returns a `Task`; `await task.push()`.
- Push-side errors raise `TaskPushError` (with the original exception chained); worker-side errors map to HTTP
  status codes in the framework extension. Remember: **Cloud Tasks retries every non-2xx response**, so a status
  code is also a retry decision.
- Configuration lives in `CloudTaskClient`; per-task options in the decorator; per-call options in `push()`.
  Precedence (lowest → highest): client → decorator → task instance → `push()` argument.
- Use `logging.getLogger("cloudtask…")` with lazy `%s` formatting, not f-strings.
