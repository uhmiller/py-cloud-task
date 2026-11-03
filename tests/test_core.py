"""Push side: the Cloud Tasks request built by Task.push / sync_push (Google is always mocked)."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

import pytest

from cloudtask import exceptions
from cloudtask.core import task_path_of
from tests.conftest import SAE, SECRET, WORKER_URL

QUEUE_PATH = "projects/proj/locations/europe-west1/queues/default"


@pytest.fixture
def google(client):
    """Mocked async and sync Google clients, returning a fixed task name."""
    response = SimpleNamespace(name=f"{QUEUE_PATH}/tasks/123")
    client._async_client = mock.Mock(create_task=mock.AsyncMock(return_value=response))
    client._sync_client = mock.Mock(create_task=mock.Mock(return_value=response))
    return client


def sent_request(create_task: mock.Mock) -> dict:
    create_task.assert_called_once()
    return create_task.call_args.kwargs["request"]


async def test_push_builds_http_task(google):
    @google.task()
    async def send(user_id, *, flag=False):
        pass

    name = await send("u1", flag=True).push(at=datetime(2030, 1, 1, tzinfo=UTC))

    assert name == f"{QUEUE_PATH}/tasks/123"
    request = sent_request(google._async_client.create_task)
    assert request["parent"] == QUEUE_PATH
    http = request["task"]["http_request"]
    assert http["url"] == WORKER_URL
    assert http["headers"]["X-PYCT-SECRET"] == SECRET
    assert http["headers"]["Content-Type"] == "application/json"
    assert http["oidc_token"] == {"service_account_email": SAE}
    assert json.loads(http["body"]) == {
        "func": "send",
        "path": task_path_of(send),
        "args": ["u1"],
        "kwargs": {"flag": True},
    }
    assert request["task"]["schedule_time"].ToDatetime(tzinfo=UTC) == datetime(
        2030, 1, 1, tzinfo=UTC
    )


def test_sync_push_builds_http_task(google):
    @google.task(name="dedup-1", timeout=60)
    def send(user_id):
        pass

    assert send("u1").sync_push() == f"{QUEUE_PATH}/tasks/123"
    task = sent_request(google._sync_client.create_task)["task"]
    assert task["name"] == f"{QUEUE_PATH}/tasks/dedup-1"
    assert task["dispatch_deadline"].seconds == 60


async def test_push_error_is_wrapped(google):
    google._async_client.create_task.side_effect = RuntimeError("boom")

    @google.task()
    async def send():
        pass

    with pytest.raises(exceptions.TaskPushError):
        await send().push()
