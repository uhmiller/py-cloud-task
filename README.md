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
    url="https://public.app.com/tasks",  # The public URL of your worker
    sae="my-service-account@my-gcp-project.iam.gserviceaccount.com",  # Service Account email for OIDC auth
    secret="super-secret-token",  # Optional: Header secret for extra security
    force_to_queue=None,  # set the name of queue to ingnore all conf and push only to this queue name
    eager=Fale,  # set True to execute task immediately without push to Google Cloud Tasks
)
```

### 2. Define a Task

Use the `@client.task` decorator. You can define tasks anywhere in your code.

```python
@client.task(queue='other', name='uniquename')
async def send_welcome_email(user_id: str, email: str):
    print(f"Sending email to {email}...")
    # ... logic to send email ...
    return "sent"

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

Google Cloud Tasks ensures that tasks with the same name are executed only once. You can set a custom name to prevent
duplicate execution.

```python
# Instantiate the task wrapper first
task = send_welcome_email("123", "user@example.com")

# Set a deterministic name (e.g., specific to the user and action)
task.name = "welcome-email-user-123"

# Push to cloud
await task.push()

```

### Local Development (Eager Mode)

When developing locally, you often don't want to send tasks to Google Cloud. Use `eager=True` to execute tasks
immediately in the current process.

```python
# In your local config
client = CloudTaskClient(..., eager=True)

# This will run the function immediately (awaitable) without calling Google
await send_welcome_email("123", "user@example.com").push()

```

### Local Integration Testing (Remote Simulation)

If you want to test the full HTTP flow (serialization -> HTTP request -> worker execution) without Google Cloud
infrastructure, use `.remote()`. This requires `httpx`.

```python
# Simulate a request to your local running worker
# This bypasses Google but tests your Router and Dependencies
await send_welcome_email("123", "user@example.com").remote(url="http://localhost:8000/tasks/run")

```

---

## FastAPI Integration

**py-cloud-task** has first-class support for FastAPI. It leverages FastAPI's native **Dependency Injection** system.

### 1. Setup the Router

```python
from fastapi import FastAPI
from cloudtask.fastapi import CloudTaskRouter

from app.core.tasks import ct as ct_client

app = FastAPI()

# Register the route that receives tasks from Google
app.include_router(CloudTaskRouter(ct_client), prefix="/tasks")

```

### 2. Use `Depends` in Tasks

You can inject database sessions, services, or any other dependency directly into your tasks, just like in API
endpoints.

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tasks import ct
from app.core.db import get_db


@ct.task()
async def task_process_order(
        order_id: int,
        db: AsyncSession = Depends(get_db)  # <--- Magic happens here
):
    # The 'db' session is created, injected, and closed automatically!
    order = await db.get(Order, order_id)
    order.status = "processed"
    await db.commit()

```

**Note:** When triggering the task, you **only** pass the data arguments. The dependencies are resolved by the worker.

```python
# Correct usage (Dependency is ignored during push)
await process_order(order_id=500).push()

```

---

## Security

To ensure that only Google Cloud Tasks can call your worker endpoint, the library supports two mechanisms:

1. **OIDC Token (Recommended):** The library automatically attaches an OIDC token identifying the Service Account. Your
   Cloud Run/Functions service should validates this token (Google handles this automatically for Cloud Run if you don't
   allow unauthenticated invocations).
2. **Secret Header:** You can configure a shared secret.

```python
client = CloudTaskClient(..., secret="my-secret-key")

```

The router will automatically validate the `X-PYCT-SECRET` header and reject unauthorized requests (403 Forbidden).

---

## Contributing

Contributions are welcome! If you find a bug or want to add a feature (e.g., Flask or Django adapters), please open an
issue or submit a PR.

<p align="center">
<span style="color: #666;">Built with ❤️ by the engineering team at <a href="https://ziett.co">Ziett</a></span>
</p>
