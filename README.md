<h1 align="center">py-cloud-task</h1>

<p align="center">
    <strong>A Framework Agnostic Client for Google Cloud Tasks.</strong>
    <br>
    Move from "Pull" (Workers) to "Push" (Serverless) architecture effortlessly.
</p>

<p align="center">
    <a href="https://github.com/ziett/py-cloud-task/actions" target="_blank">
        <img src="https://img.shields.io/github/actions/workflow/status/ziett/py-cloud-task/tests.yml?branch=main&label=tests&style=flat-square" alt="Tests">
    </a>
    <a href="https://pypi.org/project/py-cloud-task/" target="_blank">
        <img src="https://img.shields.io/pypi/pyversions/py-cloud-task.svg?color=%2334D058&style=flat-square" alt="Supported Python versions">
    </a>
</p>

---

**py-cloud-task** is a lightweight, async-first library that abstracts the complexity of **Google Cloud Tasks**. It
provides a developer experience similar to Celery or TaskIQ but is designed specifically for **Serverless**
environments (Cloud Run, App Engine, Cloud Functions, FastAPI).

It handles serialization, authentication (OIDC), scheduling, and—crucially—**FastAPI Dependency Injection**
automatically.

### Why use this instead of Celery/Redis?

| Feature          | Celery / Redis (Pull)                        | py-cloud-task (Push)                              |
|:-----------------|:---------------------------------------------|:--------------------------------------------------|
| **Architecture** | Workers poll Redis 24/7 ("Are there tasks?") | Google calls your API via HTTP ("Here is a task") |
| **Cost**         | You pay for idle workers & Redis instances   | **Pay-per-use** (Scale to Zero supported)         |
| **Infra**        | Requires Redis/RabbitMQ management           | **Zero Ops** (Managed by Google)                  |
| **Retries**      | Managed by worker code                       | **Native** (Exponential backoff managed by GCP)   |
| **DX**           | Heavy setup                                  | **Decorator-based** (Just like FastAPI)           |

---

## Installation

Currently, the package is available via GitHub. You can install it using `uv` or `pip`.

### Using uv (Recommended)

```bash
# Instalação Core
uv add "py-cloud-task @ git+https://github.com/uhmiller/py-cloud-task.git"

# Com suporte a FastAPI
uv add "py-cloud-task[fastapi] @ git+https://github.com/uhmiller/py-cloud-task.git"

# Para simulação local (testes)
uv add "py-cloud-task[test] @ git+https://github.com/uhmiller/py-cloud-task.git"
```

---

## Quick Start

### 1. Configure the Client

The `CloudTaskClient` is the entry point. It holds the configuration for your Google Cloud project and queue.

```python
from cloudtask import CloudTaskClient

client = CloudTaskClient(
    project="my-gcp-project",
    location="europe-west1",
    queue="default",
    url="https://api.myapp.com/tasks/run",  # The public URL of your worker
    sae="my-service-account@my-gcp-project.iam.gserviceaccount.com",  # Service Account email for OIDC auth
    secret="super-secret-token",  # Required by the worker (see Security)
    force_to_queue=None,  # Optional: Force all tasks to a specific queue (useful for Staging)
    eager=None,  # None = Production (Sends to Google Cloud)
    add_url_func_name_suffix=False # url = /tasks => /tasks/{task_func_name}
)

```

### 2. Define a Task

Use the `@client.task` decorator. You can define tasks anywhere in your code.

```python
@client.task(queue='high-priority', name='welcome-email-{user_id}')
async def send_welcome_email(user_id: str, email: str):
    print(f"Sending email to {email}...")
    # ... logic to send email ...

```

### 3. Trigger the Task

You can trigger tasks asynchronously. This will serialize the arguments and send them to Google Cloud Tasks.

```python
# Simple trigger
await send_welcome_email(user_id="123", email="user@example.com").push()

```

---

## Advanced Usage

### Scheduling (Delayed Execution)

Schedule a task to run in the future using the `at` parameter.

```python
from datetime import datetime, timedelta

# Run 1 hour from now
eta = datetime.now() + timedelta(hours=1)

await send_welcome_email("123", "user@example.com").push(at=eta)

```

### Task Deduplication (Named Tasks)

Cloud Tasks accepts only one task per name, so a name per entity gives deduplication. Names may use letters,
digits, `-` and `_` (up to 500 characters); anything else raises `ValueError` when the task is created.

```python
# A template, formatted with the call's arguments (positional or keyword)
@client.task(name="expire-order-{order_id}")
async def expire_order(order_id: str): ...

# Or a callable that receives the arguments as keyword arguments
@client.task(name=lambda order_id, **_: f"expire-order-{order_id}")
async def expire_order(order_id: str): ...

# Or per instance / per push
task = expire_order("o1")
task.name = "expire-order-o1"
await task.push(name="expire-order-o1-retry")
```

A static name without placeholders is shared by every call (only the first push is accepted), so the decorator
logs a warning for it.

When the name is taken, `push()` logs it and returns the existing task path: the task is already scheduled.
Pass `if_exists="raise"` to get `TaskAlreadyExists` (a `TaskPushError`) instead. Cloud Tasks keeps a name reserved
for about an hour after its task ran or was deleted.

### Context Headers (request id, tracing)

Headers captured at push time link the task to the request that created it. `context_headers` is called on every
push (Cloud Tasks and `remote` mode); the library doesn't depend on any tracing package:

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

Precedence (lowest → highest): `context_headers()` → decorator `headers` → `task.headers` → `push(headers=)`.
The secret header and `Content-Type` are set last and can't be overridden. Headers Cloud Tasks reserves (`Host`,
`Content-Length`, `User-Agent`, `X-Google-*`, `X-AppEngine-*`, `X-CloudTasks-*`, and `Authorization` when `sae` is
set) raise `ValueError`. If the provider raises, the push goes on without its headers (a warning is logged).

On the worker, your middleware and tracing integrations read these headers as for any request.

### Stopping Retries (`TaskAbort`, `TaskRetry`)

Cloud Tasks retries every non-2xx response. When a task can never succeed, raise `TaskAbort`: the worker answers
200 and Cloud Tasks stops.

```python
from cloudtask import TaskAbort


@client.task(name="expire-order-{order_id}")
async def expire_order(order_id: str, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if order is None or order.paid:
        raise TaskAbort("order missing or already paid")  # logged and returned; no personal data
    ...
```

`TaskAbort` is an exception, so `yield` dependencies see it (a DB session rolls back). `TaskRetry` asks for a
retry (503) without an error log; the delay is the queue's backoff.

### Local Development (Eager Modes)

When developing locally, you often don't want to send tasks to Google Cloud. The `eager` parameter supports three modes
to help you develop and test safely.

#### Mode 1: Immediate Execution (`eager="immediate"`)

Runs the function directly in the current process. Fastest option for Unit Tests.

```python
client = CloudTaskClient(..., eager="immediate")
await send_welcome_email("123", "user@example.com").push()
# Result: Function runs instantly. No HTTP. No Serialization.

```

#### Mode 2: Remote Simulation (`eager="remote"`)

Simulates a full HTTP request to your local worker using `httpx`. This is perfect for **Integration Tests** because it
validates serialization, headers, and dependency injection without needing Google infrastructure.

```python
client = CloudTaskClient(..., eager="remote")
await send_welcome_email("123", "user@example.com").push()
# Result: Sends POST http://localhost:8000/tasks/run.

```

#### Mode 3: Production (`eager=None`)

The default behavior. Serializes the task and sends it to Google Cloud Tasks.

---

## FastAPI Integration

**py-cloud-task** has first-class support for FastAPI. It leverages FastAPI's native **Dependency Injection** system.

### 1. Setup the Router

```python
from fastapi import FastAPI
from cloudtask.ext.fastapi import CloudTaskRouter
from app.core.tasks import client

app = FastAPI()

# Register the route that receives tasks from Google
app.include_router(CloudTaskRouter(client), prefix="/tasks")

```

Worker responses (remember that Cloud Tasks retries every non-2xx response):

| Situation | Response |
|---|---|
| Task ran | 200 `{"status": "success"}` (the return value is not sent back) |
| Task raised `TaskAbort` | 200 `{"status": "aborted", "reason": ...}` |
| Invalid payload or unknown task | 200 `{"status": "rejected", "error": ...}` (404 with `retry_unknown_tasks=True`) |
| Wrong or missing secret | 403 |
| Task raised `TaskRetry` | 503 |
| Task failed | 500 |

`retry_unknown_tasks=True` keeps unknown tasks retrying, useful when a deploy may route a task to code that
doesn't have it yet.

### 2. Use `Depends` in Tasks

You can inject database sessions, services, or any other dependency directly into your tasks, just like in API
endpoints.

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db


@client.task()
async def process_order(
        order_id: int,
        db: AsyncSession = Depends(get_db)  # <--- Magic happens here
):
    # The 'db' session is created, injected, and closed automatically!
    order = await db.get(Order, order_id)
    order.status = "processed"
    await db.commit()

```

**Note:** When triggering the task, you **only** pass the data arguments. The dependencies are resolved automatically by
the worker.

```python
# Correct usage (Dependency is ignored during push)
await process_order(order_id=500).push()

```

---

## Security

The worker endpoint is reachable from the internet, so the worker side is strict by default.

### Only registered tasks run

`@client.task` registers the function in `client.registry` under its dotted path (`module.qualname`). The worker
looks the requested path up in the registry and **never imports anything from request data**; an unknown path is
rejected with `TaskNotFound`.

Tasks register when their module is imported, so the worker process must import its task modules at startup. If
nothing else imports them, use:

```python
client.autodiscover("app.tasks")  # imports app.tasks and all its submodules
```

### The worker fails closed

Creating a `CloudTaskRouter` (or a `CloudTaskExecutor`) raises `ImproperlyConfigured` unless a shared secret is
configured. The producer sends it in the `X-PYCT-SECRET` header (name configurable with `secret_header_name`) and
the worker compares it in constant time:

```python
client = CloudTaskClient(..., secret="my-secret-key")
```

Producers that only push tasks don't need it to build tasks; the check runs when the worker is created.

The OIDC token Cloud Tasks attaches when `sae` is set is not verified by the library. It only protects the
endpoint when Cloud Run itself requires authentication (IAM invoker); the secret is still required.

For local development only, `insecure_allow_unauthenticated=True` lets the worker run without a secret (it logs a
warning). Never set it in a deployed environment.

Authentication failures return **403 Forbidden**.

---

## Contributing

Contributions are welcome! If you find a bug or want to add a feature (e.g., Flask or Django adapters), please open an
issue or submit a PR.

---

<p align="center">
<span style="color: #666;">Built with ❤️ by the engineering team at <a href="https://ziett.co">Ziett</a></span>
</p>

<p align="center">
<a href="https://ziett.com">
<img src="https://ziett.co/icon.png" alt="Ziett Logo" width="60" height="60"/>
</a>
</p>
