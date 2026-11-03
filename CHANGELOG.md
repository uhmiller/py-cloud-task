# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [Unreleased] (0.3.0)

### Breaking

- **The worker only runs registered tasks.** `CloudTaskExecutor` resolves `body["path"]` through
  `client.registry` instead of importing it. Task modules must be imported in the worker process at startup
  (`client.autodiscover("app.tasks")` helps). Unknown paths raise `TaskNotFound`.
- **The worker fails closed.** `CloudTaskRouter` and `CloudTaskExecutor` raise `ImproperlyConfigured` when the
  client has no `secret`. For local development only, pass `insecure_allow_unauthenticated=True`.
- Task paths use `__qualname__` (`module.qualname`). Same value as before for module-level functions.
- `CloudTaskExecutor.resolve_function` is an instance method that looks up the registry.

### Added

- `client.registry`, `client.get_task(path)`, `client.autodiscover(*packages)`.
- Exceptions: `ImproperlyConfigured`, `TaskAuthenticationError` (base of `InvalidTaskSecretError`).
- Test suite (`tests/`) and a CI workflow (lint, format check, tests); the publish workflow runs the tests.

### Fixed

- The task secret is compared in constant time (`hmac.compare_digest`).
- FastAPI: `app.dependency_overrides` crashed tasks with dependencies (`AttributeError` → 500 → retry).
- FastAPI: `yield` dependencies of a task are closed right after the task and receive the task's exception;
  a failure in their teardown (e.g. a commit) now returns 500 instead of escaping after the response.
