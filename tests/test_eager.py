"""Eager modes, record mode and testing helpers."""

import asyncio
import gc
import logging
from datetime import UTC, datetime
from unittest import mock

import httpx
import pytest

from cloudtask import TaskAbort, current_task, exceptions
from cloudtask.core import _background_tasks, task_path_of
from cloudtask.testing import PushedTasks, recorder


# --- immediate ---------------------------------------------------------------------------------------------------


def test_sync_push_runs_async_task_without_a_loop(make_client):
    client = make_client(eager="immediate")
    ran = []

    @client.task()
    async def work(x):
        await asyncio.sleep(0)
        ran.append(x)

    with mock.patch("warnings.warn") as warn:
        assert work(1).sync_push() is None
    assert ran == [1]
    warn.assert_not_called()


async def test_sync_push_of_async_task_inside_a_loop_is_a_clear_error(make_client):
    client = make_client(eager="immediate")

    @client.task()
    async def work():
        pass

    with pytest.raises(RuntimeError, match="await task.push"):
        work().sync_push()


async def test_immediate_sets_task_context(make_client):
    client = make_client(
        eager="immediate", context_headers=lambda: {"X-Request-ID": "r1"}
    )
    seen = []

    @client.task(name="job-{x}", queue="high")
    async def work(x):
        seen.append(current_task())

    await work(1).push()
    context = seen[0]
    assert context.path == task_path_of(work)
    assert context.retry_count == 0
    assert context.queue == "high"
    assert context.task_name == "job-1"
    assert context.headers["x-request-id"] == "r1"
    assert "x-pyct-secret" not in context.headers
    assert current_task() is None


def test_sync_immediate_swallows_abort(make_client, caplog):
    client = make_client(eager="immediate")

    @client.task()
    def work():
        raise TaskAbort("nothing to do")

    caplog.set_level(logging.WARNING, logger="cloudtask")
    work().sync_push()
    assert "nothing to do" in caplog.text


# --- remote ------------------------------------------------------------------------------------------------------


async def test_remote_keeps_a_reference_until_delivered(make_client, monkeypatch):
    delivered = asyncio.Event()

    async def handler(request):
        delivered.set()
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda: real(transport=transport))
    client = make_client(eager="remote")

    @client.task()
    async def work():
        pass

    await work().push()
    assert len(_background_tasks) == 1
    gc.collect()
    await asyncio.wait_for(delivered.wait(), 2)
    for _ in range(10):
        if not _background_tasks:
            break
        await asyncio.sleep(0)
    assert not _background_tasks


# --- record ------------------------------------------------------------------------------------------------------


@pytest.fixture
def rec_client(make_client):
    client = make_client(eager="record", context_headers=lambda: {"X-Request-ID": "r1"})
    client._async_client = mock.Mock()
    return client


async def test_record_stores_push_and_does_not_run(rec_client):
    ran = []

    @rec_client.task(name="expire-{order_id}", queue="high")
    async def expire(order_id, *, soft=False):
        ran.append(order_id)

    at = datetime(2030, 1, 1, tzinfo=UTC)
    path = await expire("o1", soft=True).push(at=at, headers={"X-Push": "1"})
    expire("o2").sync_push()

    assert ran == []
    rec_client._async_client.create_task.assert_not_called()
    first, second = rec_client.pushed
    assert first.path == task_path_of(expire)
    assert first.func_name == "expire"
    assert (first.args, first.kwargs) == (("o1",), {"soft": True})
    assert (first.queue, first.name, first.schedule_time) == ("high", "expire-o1", at)
    assert first.headers["X-Request-ID"] == "r1" and first.headers["X-Push"] == "1"
    assert (
        path
        == first.task_path
        == ("projects/proj/locations/europe-west1/queues/high/tasks/expire-o1")
    )
    assert second.name == "expire-o2"
    assert rec_client.pushed.of(expire) == [first, second]
    assert rec_client.pushed.paths() == [first.path, first.path]
    assert rec_client.pushed.last() is second


async def test_record_emulates_duplicate_names(rec_client):
    @rec_client.task(name="once-{x}")
    async def once(x):
        pass

    first = await once(1).push()
    assert await once(1).push() == first
    assert len(rec_client.pushed) == 1
    with pytest.raises(exceptions.TaskAlreadyExists):
        await once(1).push(if_exists="raise")


async def test_record_validates_like_production(rec_client):
    @rec_client.task()
    async def work(value):
        pass

    with pytest.raises(exceptions.TaskPushError, match="'value'"):
        await work(object()).push()
    with pytest.raises(ValueError, match="reserved"):
        await work(1).push(headers={"Host": "x"})
    assert rec_client.pushed == []


async def test_recorder_switches_and_restores(make_client):
    client = make_client()

    @client.task()
    async def work():
        pass

    before = client.pushed
    with recorder(client) as pushed:
        assert client.eager == "record"
        await work().push()
        assert isinstance(pushed, PushedTasks) and len(pushed) == 1
    assert client.eager is None
    assert client.pushed is before


def test_last_without_pushes_fails_clearly():
    with pytest.raises(AssertionError, match="No task was pushed"):
        PushedTasks().last()
