"""Task metadata for the running task."""

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import Depends, FastAPI

from cloudtask import TaskContext, current_task
from cloudtask.core import task_path_of
from cloudtask.executor import CloudTaskExecutor
from cloudtask.ext.fastapi import CloudTaskRouter, task_context
from tests.conftest import SECRET

CLOUD_TASKS_HEADERS = {
    "X-CloudTasks-QueueName": "high",
    "X-CloudTasks-TaskName": "expire-o1",
    "X-CloudTasks-TaskRetryCount": "2",
    "X-CloudTasks-TaskExecutionCount": "1",
    "X-CloudTasks-TaskETA": "1893456000.5",
    "X-Request-ID": "req-1",
    "Authorization": "Bearer token",
    "X-PYCT-SECRET": SECRET,
}


def test_from_headers_parses_cloud_tasks_headers():
    context = TaskContext.from_headers(
        "app.t", CLOUD_TASKS_HEADERS, hidden=frozenset({"x-pyct-secret"})
    )
    assert context.queue == "high"
    assert context.task_name == "expire-o1"
    assert (context.retry_count, context.execution_count) == (2, 1)
    assert context.is_retry
    assert context.eta == datetime(2030, 1, 1, 0, 0, 0, 500000, tzinfo=UTC)
    assert context.headers["x-request-id"] == "req-1"
    assert "authorization" not in context.headers
    assert "x-pyct-secret" not in context.headers


def test_from_headers_tolerates_missing_or_bad_values():
    context = TaskContext.from_headers(
        "app.t", {"X-CloudTasks-TaskRetryCount": "x", "X-CloudTasks-TaskETA": "soon"}
    )
    assert (context.retry_count, context.eta, context.queue) == (0, None, None)
    assert not context.is_retry


async def test_executor_sets_and_resets_current_task(client):
    seen = []

    @client.task()
    async def work():
        seen.append(current_task())

    @client.task()
    def sync_work():
        seen.append(current_task())

    executor = CloudTaskExecutor(client)
    await executor.run({"path": task_path_of(work)}, CLOUD_TASKS_HEADERS)
    executor.sync_run({"path": task_path_of(sync_work)}, CLOUD_TASKS_HEADERS)

    assert [c.path for c in seen] == [task_path_of(work), task_path_of(sync_work)]
    assert seen[0].retry_count == 2
    assert "x-pyct-secret" not in seen[0].headers
    assert current_task() is None


async def test_current_task_is_reset_when_the_task_fails(client):
    @client.task()
    async def boom():
        raise RuntimeError("x")

    with pytest.raises(Exception):
        await CloudTaskExecutor(client).run(
            {"path": task_path_of(boom)}, CLOUD_TASKS_HEADERS
        )
    assert current_task() is None


async def test_fastapi_task_context_dependency(client):
    seen = []

    @client.task()
    async def work(order_id: str, ctx: TaskContext = Depends(task_context)):
        seen.append((order_id, ctx.retry_count, ctx.task_name))

    app = FastAPI()
    app.include_router(CloudTaskRouter(client), prefix="/tasks")
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await api.post(
        "/tasks",
        json={"path": task_path_of(work), "args": ["o1"]},
        headers=CLOUD_TASKS_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert seen == [("o1", 2, "expire-o1")]


def test_task_context_outside_a_task_fails():
    with pytest.raises(RuntimeError, match="only available"):
        task_context()
