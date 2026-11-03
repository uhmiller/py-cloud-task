"""Worker authentication: fail closed, constant-time shared secret."""

import logging
from unittest import mock

import pytest

from cloudtask import auth, exceptions
from cloudtask.core import task_path_of
from cloudtask.executor import CloudTaskExecutor
from tests.conftest import SECRET


def test_worker_without_authentication_is_refused(make_client):
    client = make_client(secret="")
    with pytest.raises(exceptions.ImproperlyConfigured, match="no authentication"):
        CloudTaskExecutor(client)


def test_producer_without_authentication_still_builds_tasks(make_client):
    client = make_client(secret="")

    @client.task()
    def noop():
        pass

    assert noop().path.endswith("noop")


async def test_insecure_escape_hatch_runs_and_warns(make_client, caplog):
    caplog.set_level(logging.WARNING, logger="cloudtask")
    client = make_client(secret="", insecure_allow_unauthenticated=True)

    @client.task()
    def ping():
        return "pong"

    assert (
        await CloudTaskExecutor(client).run({"path": task_path_of(ping)}, {}) == "pong"
    )
    assert "UNAUTHENTICATED" in caplog.text


# --- secret ----------------------------------------------------------------------------------------------------


@pytest.fixture
def ping_body(client):
    @client.task()
    def ping():
        return "pong"

    return {"path": task_path_of(ping)}


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-PYCT-SECRET": "wrong"}, {"X-PYCT-SECRET": ""}, {"X-OTHER": SECRET}],
)
async def test_wrong_or_missing_secret_is_rejected(client, ping_body, headers):
    with pytest.raises(exceptions.InvalidTaskSecretError):
        await CloudTaskExecutor(client).run(ping_body, headers)
    with pytest.raises(exceptions.InvalidTaskSecretError):
        CloudTaskExecutor(client).sync_run(ping_body, headers)


@pytest.mark.parametrize(
    "header_name", ["X-PYCT-SECRET", "x-pyct-secret", "HTTP_X_PYCT_SECRET"]
)
async def test_right_secret_runs(client, ping_body, header_name):
    assert (
        await CloudTaskExecutor(client).run(ping_body, {header_name: SECRET}) == "pong"
    )


async def test_secret_is_compared_in_constant_time(client, ping_body):
    with mock.patch.object(
        auth.hmac, "compare_digest", wraps=auth.hmac.compare_digest
    ) as spy:
        await CloudTaskExecutor(client).run(ping_body, {"X-PYCT-SECRET": SECRET})
    spy.assert_called_once_with(SECRET.encode(), SECRET.encode())
