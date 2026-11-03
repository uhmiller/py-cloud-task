<h1 align="center">py-cloud-task</h1>

<p align="center">
    <strong>Background tasks on Google Cloud Tasks, with a decorator.</strong>
    <br>
    Async-first, framework-agnostic, with first-class FastAPI support.
</p>

<p align="center">
    <a href="https://github.com/uhmiller/py-cloud-task/actions/workflows/ci.yml" target="_blank">
        <img src="https://img.shields.io/github/actions/workflow/status/uhmiller/py-cloud-task/ci.yml?branch=main&label=tests&style=flat-square" alt="Tests">
    </a>
    <a href="https://pypi.org/project/py-cloud-task/" target="_blank">
        <img src="https://img.shields.io/pypi/pyversions/py-cloud-task.svg?color=%2334D058&style=flat-square" alt="Supported Python versions">
    </a>
</p>

---

**py-cloud-task** turns a Python function into a [Google Cloud Tasks](https://cloud.google.com/tasks/docs) task.
You decorate a function, call it to build a task, and `push()` it. Cloud Tasks stores it, then calls your service
over HTTP when it's due, retrying with backoff until your service answers 2xx.

There are no workers to run and no broker to operate: the "worker" is an HTTP endpoint in your own app, which suits
Cloud Run and other serverless platforms that scale to zero.

| | Celery / RQ (pull) | py-cloud-task (push) |
|:--|:--|:--|
| **Who runs tasks** | Worker processes polling a broker 24/7 | Your web service, called by Google over HTTP |
| **Infrastructure** | Redis / RabbitMQ + workers | A Cloud Tasks queue (managed) |
| **Retries, rate limits** | Worker configuration | Queue configuration (managed by Google) |
| **Cost when idle** | Workers and broker keep running | None (scale to zero) |

## Contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [Quick start (FastAPI)](#quick-start-fastapi)
- [Guide](#guide): [defining tasks](#defining-tasks) · [pushing](#pushing-tasks) · [names and deduplication](#task-names-and-deduplication) · [retries](#controlling-retries) · [task context](#task-context) · [argument validation](#argument-validation) · [context headers](#context-headers-request-id-tracing)
- [The worker endpoint](#the-worker-endpoint)
- [Security](#security)
- [Local development and testing](#local-development-and-testing)
- [Configuration reference](#configuration-reference)
- [Limits](#limits)
- [Contributing](#contributing)

## How it works

```
your code                                   Google Cloud Tasks                 your service (worker endpoint)
─────────                                   ──────────────────                 ──────────────────────────────
@client.task()
async def send_email(user_id): ...

await send_email("u1").push()  ── create ──► queue: stores the task,  ── POST ──► CloudTaskRouter
                                             dispatches it when due,              ├ checks the secret header
                                             retries until 2xx                    ├ looks up "send_email" in the registry
                                                                                  ├ solves FastAPI Depends(...)
                                                                                  └ runs send_email("u1")
```

1. `@client.task()` registers the function. Calling `send_email("u1")` does **not** run it: it returns a `Task`.
2. `await task.push()` sends Cloud Tasks an HTTP task whose body is JSON:
   `{"path": "app.tasks.send_email", "args": ["u1"], "kwargs": {}}`.
3. Cloud Tasks POSTs that body to your worker URL. The worker checks the shared secret, finds the task in the
   registry, and runs it. Any non-2xx answer makes Cloud Tasks retry.

The producer (the code that pushes) and the worker are usually the same application, so one `CloudTaskClient`
serves both.

## Installation

Requires Python 3.14+.

```bash
uv add "py-cloud-task[fastapi]"      # or: pip install "py-cloud-task[fastapi]"

# From GitHub
uv add "py-cloud-task[fastapi] @ git+https://github.com/uhmiller/py-cloud-task.git"
```

| Extra | Installs | Needed for |
|---|---|---|
| `fastapi` | FastAPI | The worker router (`cloudtask.ext.fastapi`) and dependency injection in tasks |
| `remote` | httpx | `eager="remote"` (local worker simulation). `test` is the former name of this extra |
| `validation` | pydantic | `validate_args=True` (already installed with `fastapi`) |

### Google Cloud setup

You need, once per environment:

1. **A queue** (retries and rate limits are configured on it):
   `gcloud tasks queues create default --location=europe-west1`
2. **Permissions for the producer**: the service account your app runs as needs `roles/cloudtasks.enqueuer`.
3. **A service account for the OIDC token (optional)**: `sae` is the account Cloud Tasks signs requests as. Set it
   when Cloud Run requires authentication (the account needs `roles/run.invoker`); the producer also needs
   `roles/iam.serviceAccountUser` on it.
4. **A shared secret** (32+ random characters) available to the app, e.g. from Secret Manager. The worker refuses
   to start without it.

## Quick start (FastAPI)

**1. Create the client** (`app/core/tasks.py`). It holds the configuration shared by producer and worker.

```python
import os

from cloudtask import CloudTaskClient

client = CloudTaskClient(
    project="my-gcp-project",
    location="europe-west1",
    queue="default",
    url="https://api.example.com/tasks",  # public URL of the worker endpoint
    sae="tasks@my-gcp-project.iam.gserviceaccount.com",  # or "" for no OIDC token
    secret=os.environ["TASKS_SECRET"],
)
```

**2. Define tasks** (`app/tasks/orders.py`). Tasks can use FastAPI dependencies, like endpoints.

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.tasks import client
from app.models import Order


@client.task(name="expire-order-{order_id}")
async def expire_order(order_id: str, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if order and order.is_pending:
        order.status = "expired"
        await db.commit()
```

**3. Mount the worker endpoint** (`app/main.py`).

```python
from fastapi import FastAPI

from app.core.tasks import client
from cloudtask.ext.fastapi import CloudTaskRouter

client.autodiscover("app.tasks")  # import every task module, so their tasks are registered

app = FastAPI()
app.include_router(CloudTaskRouter(client), prefix="/tasks")
```

**4. Push tasks** from anywhere in your code. Pass only the data arguments; dependencies are solved on the worker.

```python
from datetime import timedelta

from app.tasks.orders import expire_order


@app.post("/orders")
async def create_order(...):
    order = await orders.create(...)  # commit first: the task may run before this request ends
    await expire_order(order.id).push(at=order.created_at + timedelta(minutes=30))
    return order
```

## Guide

### Defining tasks

```python
@client.task(queue="high", timeout=120, headers={"X-Source": "billing"})
async def charge(invoice_id: str): ...


@client.task()
def rebuild_index(): ...  # sync functions work too
```

- Arguments travel as JSON. Supported out of the box: JSON types, `datetime`, `date`, `time`, `Decimal`, `UUID`,
  sets and Pydantic models. On the worker they arrive as their JSON form (a `datetime` as `str`, a model as
  `dict`) unless you enable [argument validation](#argument-validation).
- **Pass ids, not objects.** State may change between the push and the run, so read it again inside the task. It
  also keeps payloads small and personal data out of Cloud Tasks.
- Make tasks **idempotent**: Cloud Tasks delivers at least once, and a retry may follow a run that partly
  succeeded.

### Pushing tasks

```python
task = charge("inv_1")             # nothing is sent yet
await task.push()                   # async (FastAPI, asyncio)
task.sync_push()                    # blocking, for synchronous code
await task.push(at=run_at)          # schedule; pass an aware datetime
await task.push(headers={"X-Tenant": "acme"}, name="charge-inv_1")
```

`push()` returns the task resource name (`projects/…/queues/…/tasks/…`). It raises `TaskPushError` (with the
original error chained) when Cloud Tasks rejects the task or an argument can't be encoded, and `ValueError` for an
invalid name, timeout or header. `delay()` / `sync_delay()` are aliases of `push()` / `sync_push()`.

A naive `at` datetime gets the client `timezone` (UTC by default) and logs a warning.

### Task names and deduplication

Cloud Tasks accepts one task per name, so naming a task after its entity prevents duplicates:

```python
@client.task(name="expire-order-{order_id}")                      # template, filled with the call's arguments
async def expire_order(order_id: str): ...

@client.task(name=lambda order_id, **_: f"expire-order-{order_id}")  # or a callable
async def expire_order(order_id: str): ...

task = expire_order("o1")
task.name = "expire-order-o1-v2"                                   # per instance
await task.push(name="expire-order-o1-v3")                         # or per push
```

- Names may use letters, digits, `-` and `_` (up to 500 characters). Anything else raises `ValueError` when the
  task is created.
- When the name is taken, `push()` logs it and returns the existing task's path: the work is already scheduled.
  Pass `if_exists="raise"` to get `TaskAlreadyExists` (a `TaskPushError`) instead.
- Cloud Tasks keeps a name reserved for about an hour after the task ran or was deleted.
- A static decorator name (no `{placeholders}`) is shared by every call, so only the first push is ever accepted;
  the decorator logs a warning for it.

### Controlling retries

Cloud Tasks retries every response that isn't 2xx, with the backoff configured on the queue. Exceptions in a task
answer 500, so they are retried. When retrying can't help, raise `TaskAbort`:

```python
from cloudtask import TaskAbort, TaskRetry


@client.task(name="expire-order-{order_id}")
async def expire_order(order_id: str, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if order is None or order.paid:
        raise TaskAbort("order missing or already paid")  # 200: no retry
    if not await payments.is_settled(order):
        raise TaskRetry()  # 503: retry, without an error log
    ...
```

The `TaskAbort` reason is logged and returned in the response, so keep personal data out of it. Both are
exceptions, so `yield` dependencies see them (a database session rolls back).

### Task context

Tasks can read metadata about the current attempt, taken from the headers Cloud Tasks sends:

```python
from cloudtask import TaskContext, current_task
from cloudtask.ext.fastapi import task_context


@client.task()
async def send_receipt(order_id: str, ctx: TaskContext = Depends(task_context)):
    if ctx.is_retry:
        logger.info("Retry %s of %s", ctx.retry_count, ctx.task_name)


@client.task()
def sync_job():
    ctx = current_task()  # outside FastAPI dependency injection
```

| Field | Meaning |
|---|---|
| `path` | Dotted path of the task |
| `queue`, `task_name` | Queue and task name |
| `retry_count`, `is_retry` | Previous attempts (0 on the first) |
| `execution_count` | Attempts that got a response from the worker |
| `eta` | Scheduled time |
| `headers` | Incoming headers (lower-case), without the secret, `Authorization` or cookies |

### Argument validation

With `validate_args=True`, the worker converts each argument to its annotated type with Pydantic, so type hints
hold inside the task:

```python
client = CloudTaskClient(..., validate_args=True)  # every task; or @client.task(validate_args=True)


@client.task()
async def bill(amount: Decimal, due: datetime, plan: Plan):  # Plan: an Enum or a Pydantic model
    ...
```

A value that doesn't fit its annotation answers 200 `rejected` (retrying can't fix it); the error names the
parameter, never the value. FastAPI dependencies are not validated.

### Context headers (request id, tracing)

To link a task to the request that pushed it, give the client a function that returns headers to send with every
push. Your worker's middleware and tracing integrations (OpenTelemetry, Sentry) then pick them up like any request:

```python
from opentelemetry import propagate


def context_headers() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)  # traceparent, tracestate, baggage
    if request_id := get_request_id():
        carrier["X-Request-ID"] = request_id
    return carrier


client = CloudTaskClient(..., context_headers=context_headers)
```

- Precedence (lowest → highest): `context_headers()` → decorator `headers` → `task.headers` →
  `push(headers=)`. Names are case-insensitive. The secret header and `Content-Type` are always set by the library.
- Headers Cloud Tasks reserves (`Host`, `Content-Length`, `User-Agent`, `X-Google-*`, `X-AppEngine-*`,
  `X-CloudTasks-*`, and `Authorization` when `sae` is set) raise `ValueError`.
- If the function raises, the push goes on without its headers and a warning is logged.

## The worker endpoint

`CloudTaskRouter(client)` adds `POST ""` to the prefix you mount it on. Its answers are also retry decisions:

| Situation | Response | Cloud Tasks |
|---|---|---|
| Task ran | 200 `{"status": "success"}` (the return value is not sent) | done |
| Task raised `TaskAbort` | 200 `{"status": "aborted", "reason": ...}` | done |
| Invalid payload or unknown task | 200 `{"status": "rejected", "error": ...}` | done |
| Wrong or missing secret | 403 | retries |
| Task raised `TaskRetry` | 503 | retries |
| Task failed | 500 | retries |

- **Only registered tasks run.** Tasks register when their module is imported, so the worker must import them at
  startup: `client.autodiscover("app.tasks")` imports a package and all its submodules.
- **Deploys.** During a blue/green deploy a task may reach an instance without its code. Set
  `retry_unknown_tasks=True` to answer 404 (retry) instead of `rejected` for unknown tasks.
- **Task name in the URL.** With `add_url_func_name_suffix=True` tasks are sent to `<url>/<function name>`
  (readable in logs and metrics). Mount the router on a prefix with a path parameter
  (`prefix="/tasks/{name}"`); a URL that doesn't match the payload's task is rejected.
- **Dependencies.** `Depends(...)` in a task works as in endpoints, including `app.dependency_overrides`.
  `yield` dependencies close right after the task and see its exception.

Other frameworks can use the framework-agnostic `CloudTaskExecutor(client)`: `await executor.run(body, headers)`
(or `sync_run`), mapping its exceptions to responses as in the table above.

## Security

The worker endpoint is reachable from the internet, so it is strict by default:

- **The shared secret is required.** The producer sends it in the `X-PYCT-SECRET` header (`secret_header_name`
  changes it) and the worker compares it in constant time. Creating the router without a `secret` raises
  `ImproperlyConfigured`, so a missing environment variable fails at startup instead of opening the endpoint.
  Code that only pushes tasks doesn't trigger the check.
- **Only registered tasks run.** The worker never imports anything named in the request.
- **The library never logs task arguments, payloads or header values**, only task paths, names and queues
  (tracebacks of failing tasks still carry your own exception messages).
- The OIDC token Cloud Tasks attaches when `sae` is set is **not** verified by the library. It protects the
  endpoint only if Cloud Run itself requires authentication; the secret is required either way.

For local development only, `insecure_allow_unauthenticated=True` lets the worker run without a secret (it logs a
warning). Never set it in a deployed environment.

## Local development and testing

`eager` changes what `push()` does, so nothing reaches Google outside production:

| `eager` | `push()` | Use it for |
|---|---|---|
| `None` | Creates the task in Cloud Tasks | Production, staging |
| `"immediate"` | Runs the task in the current process and waits for it | Local development without a worker |
| `"remote"` | POSTs the payload to the worker URL in the background (needs the `remote` extra) | Running API and worker locally, end to end |
| `"record"` | Records the push in `client.pushed` without running it | Unit tests |

In `immediate` mode FastAPI dependencies are not solved (there is no request): use it for tasks without
`Depends`, or test those through the router.

### Testing that a task was pushed

`cloudtask.testing.recorder` switches a client to `record` mode for a block. Pushes are validated as in production
(names, headers, encoding, size) but neither sent nor run:

```python
import pytest

from app.core.tasks import client
from app.tasks.orders import expire_order
from cloudtask.testing import recorder


@pytest.fixture
def pushed():
    with recorder(client) as pushed:
        yield pushed


async def test_checkout_schedules_expiry(api, pushed):
    await api.post("/orders", json={...})

    task = pushed.last()
    assert task.path == "app.tasks.orders.expire_order"
    assert task.args == ("o1",)
    assert task.name == "expire-order-o1"
    assert len(pushed.of(expire_order)) == 1
```

Each `PushedTask` has `path`, `func_name`, `args`, `kwargs`, `queue`, `name`, `schedule_time`, `headers` and
`task_path`. A second push with the same name behaves like Cloud Tasks (returns the existing path, or raises with
`if_exists="raise"`).

### Testing the task itself

Call the task through the router with your test client, so dependencies and overrides apply:

```python
async def test_expire_order(api):
    response = await api.post(
        "/tasks",
        json={"path": "app.tasks.orders.expire_order", "args": ["o1"]},
        headers={"X-PYCT-SECRET": settings.TASKS_SECRET},
    )
    assert response.json() == {"status": "success"}
```

## Configuration reference

### `CloudTaskClient(...)`

| Option | Default | Description |
|---|---|---|
| `project`, `location`, `queue` | required | GCP project, region and default queue |
| `url` | required | Worker endpoint URL |
| `sae` | required | Service account for the OIDC token Cloud Tasks attaches (`""` for none) |
| `secret` | `""` | Shared secret sent in the secret header; **required by the worker** |
| `secret_header_name` | `"X-PYCT-SECRET"` | Header carrying the secret |
| `timeout` | `None` | Default dispatch deadline in seconds (15 to 1800; Cloud Tasks default: 10 min) |
| `timezone` | `"UTC"` | Timezone applied to naive `at` datetimes |
| `force_to_queue` | `None` | Send every task to this queue (e.g. one queue for staging) |
| `eager` | `None` | `"immediate"`, `"remote"` or `"record"` (see above) |
| `add_url_func_name_suffix` | `False` | Send tasks to `<url>/<function name>` |
| `context_headers` | `None` | Function returning headers to send with every push |
| `validate_args` | `False` | Convert arguments to their annotated types on the worker |
| `retry_unknown_tasks` | `False` | Answer 404 (retry) instead of `rejected` for unknown tasks |
| `insecure_allow_unauthenticated` | `False` | Local development only: run the worker without a secret |

### `@client.task(...)`

| Option | Description |
|---|---|
| `queue` | Queue for this task |
| `name` | Task name per call: template (`"order-{order_id}"`) or callable |
| `url` | Worker URL for this task |
| `timeout` | Dispatch deadline in seconds (15 to 1800) |
| `headers` | Headers sent with this task |
| `validate_args` | Override the client's `validate_args` |

Precedence for every setting: client → decorator → task instance → `push()` argument.

### Exceptions (`from cloudtask import ...`)

| Exception | Raised when |
|---|---|
| `TaskPushError` | Cloud Tasks rejected the task, an argument isn't JSON-serializable, or the payload is too large |
| `TaskAlreadyExists` | `push(if_exists="raise")` and the name is taken (subclass of `TaskPushError`) |
| `TaskAbort` / `TaskRetry` | Raised by your task to stop or retry |
| `ImproperlyConfigured` | Worker without a secret, or `validate_args` without pydantic |
| `TaskNotFound`, `InvalidTaskPayload`, `InvalidTaskSecretError`, `TaskExecutionError` | Worker side, mapped to the responses above |

All inherit from `CloudTaskException`.

## Limits

| Limit | Value |
|---|---|
| Payload | 1 MB (checked before pushing) |
| Dispatch deadline (`timeout`) | 15 s to 30 min |
| Task name | `[A-Za-z0-9_-]`, up to 500 characters; reserved ~1 hour after the task ran |
| Delivery | At least once: tasks must be idempotent |

## Contributing

```bash
uv sync --all-extras --dev   # install
uv run ruff check .          # lint
uv run ruff format           # format
uv run pytest                # tests (Google's API is always mocked)
```

Bug reports and pull requests are welcome. See [CHANGELOG.md](CHANGELOG.md) for changes between versions and
[docs/architecture.md](docs/architecture.md) for how the library works inside.

---

<p align="center">
<span style="color: #666;">Built by the engineering team at <a href="https://ziett.co">Ziett</a></span>
</p>
