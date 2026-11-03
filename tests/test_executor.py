"""Worker-side resolution: only registered tasks run; payload checks."""

import logging
import os
import platform
import sys
import textwrap

import pytest

from cloudtask import exceptions
from cloudtask.core import task_path_of
from cloudtask.executor import CloudTaskExecutor
from tests.conftest import SECRET

HEADERS = {"X-PYCT-SECRET": SECRET}


@pytest.mark.parametrize(
    "path", ["os.system", "platform.python_version", "builtins.eval", "nodots"]
)
async def test_unregistered_path_is_not_imported_or_called(client, monkeypatch, path):
    calls = []
    monkeypatch.setattr(os, "system", lambda *a: calls.append(a))
    monkeypatch.setattr(platform, "python_version", lambda *a: calls.append(a))
    executor = CloudTaskExecutor(client)

    with pytest.raises(exceptions.TaskNotFound):
        await executor.run({"path": path, "args": ["echo pwned"]}, HEADERS)
    with pytest.raises(exceptions.TaskNotFound):
        executor.sync_run({"path": path, "args": ["echo pwned"]}, HEADERS)

    assert calls == []


async def test_unknown_module_is_not_imported(client):
    sys.modules.pop("json.tool", None)
    with pytest.raises(exceptions.TaskNotFound):
        await CloudTaskExecutor(client).run({"path": "json.tool.main"}, HEADERS)
    assert "json.tool" not in sys.modules


async def test_registered_async_task_runs(client):
    received = []

    @client.task()
    async def add(a, b, *, scale=1):
        received.append((a, b, scale))
        return (a + b) * scale

    body = {"path": task_path_of(add), "args": [1, 2], "kwargs": {"scale": 10}}
    assert await CloudTaskExecutor(client).run(body, HEADERS) == 30
    assert received == [(1, 2, 10)]


def test_registered_sync_task_runs_with_sync_run(client):
    @client.task()
    def mul(a, b):
        return a * b

    body = {"path": task_path_of(mul), "args": [3, 4]}
    assert CloudTaskExecutor(client).sync_run(body, HEADERS) == 12


async def test_task_failure_is_wrapped(client):
    @client.task()
    async def boom():
        raise RuntimeError("nope")

    with pytest.raises(exceptions.TaskExecutionError) as info:
        await CloudTaskExecutor(client).run({"path": task_path_of(boom)}, HEADERS)
    assert isinstance(info.value.__cause__, RuntimeError)


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "x",
        {"args": []},
        {"path": ""},
        {"path": 1},
        {"path": "a.b", "args": {}},
        {"path": "a.b", "kwargs": []},
    ],
)
async def test_malformed_body_is_invalid_payload(client, body):
    with pytest.raises(exceptions.InvalidTaskPayload):
        await CloudTaskExecutor(client).run(body, HEADERS)


async def test_arguments_that_dont_fit_the_signature_are_invalid_payload(client):
    called = []

    @client.task()
    def one(a):
        called.append(a)

    executor = CloudTaskExecutor(client)
    for body in (
        {"path": task_path_of(one), "args": [1, 2]},
        {"path": task_path_of(one), "kwargs": {"b": 1}},
    ):
        with pytest.raises(exceptions.InvalidTaskPayload):
            await executor.run(body, HEADERS)
    assert called == []


async def test_abort_and_retry_are_not_wrapped(client):
    @client.task()
    async def aborts():
        raise exceptions.TaskAbort("already paid")

    @client.task()
    def retries():
        raise exceptions.TaskRetry()

    executor = CloudTaskExecutor(client)
    with pytest.raises(exceptions.TaskAbort, match="already paid"):
        await executor.run({"path": task_path_of(aborts)}, HEADERS)
    with pytest.raises(exceptions.TaskRetry):
        executor.sync_run({"path": task_path_of(retries)}, HEADERS)


async def test_authentication_happens_before_resolution(client):
    with pytest.raises(exceptions.InvalidTaskSecretError):
        await CloudTaskExecutor(client).run({"path": "os.system"}, {})


def test_task_path_uses_qualname(client):
    @client.task()
    def inner():
        pass

    path = task_path_of(inner)
    assert path == f"{__name__}.test_task_path_uses_qualname.<locals>.inner"
    assert inner().path == path
    assert inner().data["path"] == path
    assert client.registry[path] is inner


def test_registering_the_same_path_keeps_the_last(client, caplog):
    def first():
        return 1

    def second():
        return 2

    second.__qualname__ = first.__qualname__
    caplog.set_level(logging.DEBUG, logger="cloudtask")
    client.task()(first)
    latest = client.task()(second)

    assert client.registry[task_path_of(first)] is latest
    assert "registered again" in caplog.text


def test_get_task_unknown_raises(client):
    with pytest.raises(exceptions.TaskNotFound):
        client.get_task("nope.nothing")


def test_autodiscover_imports_package_submodules(client, tmp_path, monkeypatch):
    package = tmp_path / "discover_pkg"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "sub" / "__init__.py").write_text("")
    task_source = textwrap.dedent(
        """
        from tests.test_executor import CURRENT_CLIENT

        @CURRENT_CLIENT.task()
        def {name}():
            return "{name}"
        """
    )
    (package / "a.py").write_text(task_source.format(name="task_a"))
    (package / "sub" / "b.py").write_text(task_source.format(name="task_b"))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(globals(), "CURRENT_CLIENT", client)

    client.autodiscover("discover_pkg")

    assert {"discover_pkg.a.task_a", "discover_pkg.sub.b.task_b"} <= set(
        client.registry
    )
    for name in list(sys.modules):
        if name.startswith("discover_pkg"):
            del sys.modules[name]
