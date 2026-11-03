# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [0.3.0] - Unreleased

### Breaking

- **The worker only runs registered tasks.** `CloudTaskExecutor` resolves `body["path"]` through
  `client.registry` instead of importing it. Task modules must be imported in the worker process at startup
  (`client.autodiscover("app.tasks")` helps). Unknown paths raise `TaskNotFound`.
- **The worker fails closed.** `CloudTaskRouter` and `CloudTaskExecutor` raise `ImproperlyConfigured` when the
  client has no `secret`. For local development only, pass `insecure_allow_unauthenticated=True`.
- Task paths use `__qualname__` (`module.qualname`). Same value as before for module-level functions.
- The worker answers 200 `{"status": "success"}` without the task's return value.
- Unknown tasks and invalid payloads answer 200 `{"status": "rejected"}` instead of 404 / 500 / 422, so Cloud Tasks
  stops retrying them. `retry_unknown_tasks=True` restores 404 for unknown tasks.
- A duplicate task name (`AlreadyExists`) is no longer an error: `push()` returns the existing task path. Use
  `if_exists="raise"` for `TaskAlreadyExists` (a `TaskPushError` subclass).
- `CloudTaskExecutor.resolve_function` is an instance method that looks up the registry.
- Timeouts outside 15–1800 seconds raise `ValueError` (Cloud Tasks rejected them at push time).
- Removed the `Framework :: Django` classifier (there is no Django support yet).

### Added

- `client.registry`, `client.get_task(path)`, `client.autodiscover(*packages)`.
- `context_headers` client hook and `push(headers=)` / `sync_push(headers=)`, with a fixed precedence; reserved
  Cloud Tasks headers raise `ValueError`.
- Per-call task names: `@client.task(name="order-{order_id}")` or a callable; `push(name=)`; names are validated.
  A static decorator name logs a warning.
- `TaskAbort` (200, no retry) and `TaskRetry` (503) for tasks; `InvalidTaskPayload` for malformed requests.
- `TaskContext` (retry count, task name, queue, ETA, headers) for the running task: `Depends(task_context)` in
  FastAPI, `cloudtask.current_task()` elsewhere.
- `validate_args=True` (client or decorator): convert task arguments to their annotated types with pydantic;
  invalid values answer 200 `rejected`. New `validation` extra.
- `eager="record"`, `client.pushed` and `cloudtask.testing.recorder()` to assert pushes in tests.
- Push-side checks: unencodable arguments name the argument, payloads over 1 MB raise `TaskPushError`, naive `at`
  datetimes log a warning.
- With `add_url_func_name_suffix=True`, the FastAPI worker rejects a URL that doesn't match the payload's task.
- `remote` extra (httpx for `eager="remote"`); `test` is kept as an alias.
- Exceptions are exported from `cloudtask`, including the new `ImproperlyConfigured`, `TaskAuthenticationError`
  (base of `InvalidTaskSecretError`), `TaskAlreadyExists`, `InvalidTaskPayload`, `TaskAbort` and `TaskRetry`.
- Test suite (`tests/`) and a CI workflow (lint, format check, tests); the publish workflow runs the tests.

### Fixed

- The task secret is compared in constant time (`hmac.compare_digest`).
- `task.headers = {...}` no longer mutates the decorator headers (they leaked into every later call).
- The task name now uses `force_to_queue` like the parent queue (Cloud Tasks rejected the mismatch).
- The worker checks the secret before validating the payload (FastAPI used to answer 422 first).
- `sync_push()` of an async task in `immediate` mode never ran it (coroutine not awaited).
- `remote` mode could lose tasks: the background delivery wasn't referenced and could be garbage-collected.
- FastAPI: the dependency analysis of a task is cached instead of rebuilt on every run.
- FastAPI: `app.dependency_overrides` crashed tasks with dependencies (`AttributeError` → 500 → retry).
- FastAPI: `yield` dependencies of a task are closed right after the task and receive the task's exception;
  a failure in their teardown (e.g. a commit) now returns 500 instead of escaping after the response.
