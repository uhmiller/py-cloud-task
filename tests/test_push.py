"""Push options: context headers, duplicate names, per-call names and push-side validation."""

import asyncio
import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from google.api_core.exceptions import AlreadyExists, PermissionDenied

from cloudtask import TaskAbort, exceptions
from tests.conftest import SECRET

QUEUE_PATH = "projects/proj/locations/europe-west1/queues/default"


def mock_google(client):
    response = SimpleNamespace(name=f"{QUEUE_PATH}/tasks/generated")
    client._async_client = mock.Mock(create_task=mock.AsyncMock(return_value=response))
    client._sync_client = mock.Mock(create_task=mock.Mock(return_value=response))
    return client


def sent_task(create_task: mock.Mock) -> dict:
    return create_task.call_args.kwargs["request"]["task"]


def sent_headers(create_task: mock.Mock) -> dict:
    return sent_task(create_task)["http_request"]["headers"]


# --- context headers -------------------------------------------------------------------------------------------


@pytest.fixture
def ctx_client(make_client):
    provider = {
        "X-Request-ID": "req-1",
        "traceparent": "00-abc-def-01",
        "X-Layer": "context",
    }
    return mock_google(make_client(context_headers=lambda: dict(provider)))


async def test_context_headers_are_sent(ctx_client):
    @ctx_client.task()
    async def send():
        pass

    await send().push()
    headers = sent_headers(ctx_client._async_client.create_task)
    assert headers["X-Request-ID"] == "req-1"
    assert headers["traceparent"] == "00-abc-def-01"

    send().sync_push()
    assert sent_headers(ctx_client._sync_client.create_task)["X-Request-ID"] == "req-1"


async def test_header_precedence(ctx_client):
    @ctx_client.task(headers={"X-Layer": "decorator", "X-Deco": "1"})
    async def send():
        pass

    task = send()
    assert task.headers["X-Layer"] == "decorator"

    task.headers = {"x-layer": "instance", "X-Inst": "1"}
    await task.push(headers={"X-Push": "1"})
    headers = sent_headers(ctx_client._async_client.create_task)
    assert headers["x-layer"] == "instance"
    assert "X-Layer" not in headers  # case-insensitive merge, no duplicates
    assert {headers["X-Deco"], headers["X-Inst"], headers["X-Push"]} == {"1"}

    await task.push(headers={"X-LAYER": "push"})
    assert sent_headers(ctx_client._async_client.create_task)["X-LAYER"] == "push"


async def test_instance_headers_dont_leak_into_other_calls(client):
    mock_google(client)

    @client.task(headers={"X-Deco": "1"})
    async def send():
        pass

    first = send()
    first.headers = {"X-Only-First": "1"}
    assert "X-Only-First" not in send().headers


async def test_secret_and_content_type_cannot_be_overridden(client):
    mock_google(client)

    @client.task()
    async def send():
        pass

    await send().push(headers={"x-pyct-secret": "forged", "content-type": "text/plain"})
    headers = sent_headers(client._async_client.create_task)
    lowered = {k.lower(): v for k, v in headers.items()}
    assert len(lowered) == len(headers)
    assert lowered["x-pyct-secret"] == SECRET
    assert lowered["content-type"] == "application/json"


@pytest.mark.parametrize(
    "header",
    [
        "Host",
        "content-length",
        "User-Agent",
        "X-Google-Foo",
        "X-AppEngine-Bar",
        "X-CloudTasks-TaskName",
        "Authorization",
    ],
)
async def test_reserved_headers_are_refused(client, header):
    mock_google(client)

    @client.task()
    async def send():
        pass

    with pytest.raises(ValueError, match="reserved"):
        await send().push(headers={header: "x"})
    client._async_client.create_task.assert_not_called()


async def test_authorization_is_allowed_without_oidc(make_client):
    client = mock_google(make_client(sae=""))

    @client.task()
    async def send():
        pass

    await send().push(headers={"Authorization": "Bearer x"})
    assert sent_headers(client._async_client.create_task)["Authorization"] == "Bearer x"


async def test_failing_provider_does_not_block_the_push(make_client, caplog):
    def provider():
        raise RuntimeError("secret-value-123")

    client = mock_google(make_client(context_headers=provider))

    @client.task()
    async def send():
        pass

    caplog.set_level(logging.WARNING, logger="cloudtask")
    await send().push()
    client._async_client.create_task.assert_called_once()
    assert "context_headers provider failed" in caplog.text
    assert "secret-value-123" not in caplog.text


async def test_remote_mode_sends_context_headers(make_client, monkeypatch):
    received: list[httpx.Request] = []
    done = threading.Event()

    def handler(request):
        received.append(request)
        done.set()
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    real_async, real_sync = httpx.AsyncClient, httpx.Client
    monkeypatch.setattr(httpx, "AsyncClient", lambda: real_async(transport=transport))
    monkeypatch.setattr(httpx, "Client", lambda: real_sync(transport=transport))
    client = make_client(eager="remote", context_headers=lambda: {"X-Request-ID": "r1"})

    @client.task()
    async def send():
        pass

    await send().push(headers={"X-Push": "1"})
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.01)
    assert received[0].headers["x-request-id"] == "r1"
    assert received[0].headers["x-push"] == "1"
    assert received[0].headers["x-pyct-secret"] == SECRET

    done.clear()
    send().sync_push()
    assert done.wait(2)
    assert received[1].headers["x-request-id"] == "r1"


# --- duplicate task names --------------------------------------------------------------------------------------


@pytest.fixture
def dup_client(client):
    mock_google(client)
    client._async_client.create_task.side_effect = AlreadyExists("exists")
    client._sync_client.create_task.side_effect = AlreadyExists("exists")
    return client


async def test_already_exists_is_success(dup_client, caplog):
    @dup_client.task()
    async def send():
        pass

    caplog.set_level(logging.INFO, logger="cloudtask")
    assert await send().push(name="order-1") == f"{QUEUE_PATH}/tasks/order-1"
    assert send().sync_push(name="order-1") == f"{QUEUE_PATH}/tasks/order-1"
    assert "already exists" in caplog.text


async def test_already_exists_can_raise(dup_client):
    @dup_client.task()
    async def send():
        pass

    with pytest.raises(exceptions.TaskAlreadyExists) as info:
        await send().push(name="order-1", if_exists="raise")
    assert isinstance(info.value, exceptions.TaskPushError)
    assert isinstance(info.value.__cause__, AlreadyExists)
    with pytest.raises(exceptions.TaskAlreadyExists):
        send().sync_push(name="order-1", if_exists="raise")


async def test_other_google_errors_raise_push_error_with_cause(client):
    mock_google(client)
    client._async_client.create_task.side_effect = PermissionDenied("no")

    @client.task()
    async def send():
        pass

    with pytest.raises(exceptions.TaskPushError) as info:
        await send().push()
    assert not isinstance(info.value, exceptions.TaskAlreadyExists)
    assert isinstance(info.value.__cause__, PermissionDenied)


# --- task names per call ---------------------------------------------------------------------------------------


async def test_name_template_uses_call_arguments(client):
    mock_google(client)

    @client.task(name="expire-order-{order_id}-{kind}")
    async def expire(order_id: str, kind: str = "soft"):
        pass

    await expire("o1").push()
    assert sent_task(client._async_client.create_task)["name"] == (
        f"{QUEUE_PATH}/tasks/expire-order-o1-soft"
    )
    await expire(order_id="o2", kind="hard").push()
    assert sent_task(client._async_client.create_task)["name"].endswith(
        "expire-order-o2-hard"
    )


async def test_name_callable_receives_arguments(client):
    mock_google(client)

    @client.task(name=lambda order_id, **_: f"open-drop-{order_id}")
    async def open_drop(order_id: str, db=None):
        pass

    assert open_drop("d9").name == "open-drop-d9"


async def test_push_name_overrides_without_mutating(client):
    mock_google(client)

    @client.task(name="t-{x}")
    async def send(x):
        pass

    task = send(1)
    await task.push(name="custom-1")
    assert sent_task(client._async_client.create_task)["name"].endswith("/custom-1")
    assert task.name == "t-1"


def test_force_to_queue_applies_to_the_task_name(make_client):
    client = mock_google(make_client(force_to_queue="staging"))

    @client.task(queue="other")
    def send():
        pass

    send().sync_push(name="n1")
    request = client._sync_client.create_task.call_args.kwargs["request"]
    assert request["parent"].endswith("/queues/staging")
    assert request["task"]["name"].startswith(request["parent"] + "/tasks/")


@pytest.mark.parametrize("bad", ["has.dot", "has:colon", "a b", "", "x" * 501])
async def test_invalid_names_fail_fast(client, bad):
    @client.task(name="order-{x}")
    async def send(x):
        pass

    if bad:  # "order-" + "" is a valid name
        with pytest.raises(ValueError, match="Invalid task name"):
            send(bad)
    task = send("ok")
    with pytest.raises(ValueError, match="Invalid task name"):
        task.name = bad
    with pytest.raises(ValueError, match="Invalid task name"):
        await task.push(name=bad)


def test_unknown_template_field_fails(client):
    @client.task(name="order-{missing}")
    def send(x):
        pass

    with pytest.raises(ValueError, match="unknown argument"):
        send(1)


def test_static_name_warns_once_at_decoration(client, caplog):
    caplog.set_level(logging.WARNING, logger="cloudtask")

    @client.task(name="always-the-same")
    def send():
        pass

    send()
    send()
    assert caplog.text.count("static name") == 1
    assert send().name == "always-the-same"


def test_invalid_static_name_fails_at_decoration(client):
    with pytest.raises(ValueError):

        @client.task(name="bad.name")
        def send():
            pass


# --- eager immediate + TaskAbort -------------------------------------------------------------------------------


async def test_immediate_mode_swallows_abort(make_client, caplog):
    client = make_client(eager="immediate")

    @client.task()
    async def aborts():
        raise TaskAbort("nothing to do")

    caplog.set_level(logging.WARNING, logger="cloudtask")
    assert await aborts().push() is None
    assert "nothing to do" in caplog.text


# --- push-side validation -------------------------------------------------------------------------------------


async def test_unencodable_argument_is_named(client):
    mock_google(client)

    @client.task()
    async def send(user_id, payload):
        pass

    with pytest.raises(exceptions.TaskPushError, match="argument 'payload'") as info:
        await send("u1", payload=object()).push()
    assert isinstance(info.value.__cause__, TypeError)
    client._async_client.create_task.assert_not_called()


async def test_payload_too_large(client):
    mock_google(client)

    @client.task()
    async def send(blob):
        pass

    with pytest.raises(exceptions.TaskPushError, match="payload too large"):
        await send("x" * 1_000_001).push()
    client._async_client.create_task.assert_not_called()


@pytest.mark.parametrize("timeout", [1, 14, 1801])
def test_timeout_out_of_bounds(make_client, timeout):
    with pytest.raises(ValueError, match="between 15 and 1800"):
        make_client(timeout=timeout)

    client = make_client()
    with pytest.raises(ValueError, match="between 15 and 1800"):

        @client.task(timeout=timeout)
        def send():
            pass

    @client.task()
    def other():
        pass

    task = other()
    task.timeout = timeout
    mock_google(client)
    with pytest.raises(ValueError, match="between 15 and 1800"):
        task.sync_push()


async def test_naive_datetime_warns_and_uses_client_timezone(make_client, caplog):
    client = mock_google(make_client(timezone="Africa/Luanda"))

    @client.task()
    async def send():
        pass

    caplog.set_level(logging.WARNING, logger="cloudtask")
    await send().push(at=datetime(2030, 1, 1, 9, 0))
    assert "naive datetime" in caplog.text
    stamp = sent_task(client._async_client.create_task)["schedule_time"]
    assert stamp.seconds == int(datetime(2030, 1, 1, 8, 0, tzinfo=UTC).timestamp())
