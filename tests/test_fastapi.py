"""FastAPI router: status codes, auth at startup, dependency injection (docs/improvements.md §1, §14)."""

from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI, Request

from cloudtask import exceptions
from cloudtask.core import task_path_of
from cloudtask.ext.fastapi import CloudTaskRouter
from tests.conftest import SECRET

HEADERS = {"X-PYCT-SECRET": SECRET}


def make_app(client) -> FastAPI:
    app = FastAPI()
    app.include_router(CloudTaskRouter(client), prefix="/tasks")
    return app


@pytest.fixture
def http(client):
    app = make_app(client)
    transport = httpx.ASGITransport(app=app)
    return app, httpx.AsyncClient(transport=transport, base_url="http://test")


def test_router_refuses_worker_without_authentication(make_client):
    with pytest.raises(exceptions.ImproperlyConfigured):
        CloudTaskRouter(make_client(secret=""))


async def test_success(client, http):
    app, api = http

    @client.task()
    async def ping(value):
        return value

    response = await api.post(
        "/tasks", json={"path": task_path_of(ping), "args": ["x"]}, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"


async def test_wrong_secret_is_403(client, http):
    _, api = http
    response = await api.post("/tasks", json={"path": "os.system", "args": ["id"]})
    assert response.status_code == 403


async def test_unregistered_path_is_404(client, http):
    _, api = http
    response = await api.post(
        "/tasks", json={"path": "os.system", "args": ["id"]}, headers=HEADERS
    )
    assert response.status_code == 404


async def test_task_failure_is_500(client, http):
    _, api = http

    @client.task()
    async def boom():
        raise RuntimeError("nope")

    response = await api.post(
        "/tasks", json={"path": task_path_of(boom)}, headers=HEADERS
    )
    assert response.status_code == 500


# --- dependency injection (§14) --------------------------------------------------------------------------------


class Session:
    def __init__(self, name: str, events: list):
        self.name = name
        self.events = events


def real_db():
    raise AssertionError("the override should be used")


async def test_dependency_overrides_are_used(client, http):
    app, api = http
    events: list = []
    app.dependency_overrides[real_db] = lambda: Session("fake", events)

    @client.task()
    async def uses_db(order_id: str, db: Session = Depends(real_db)):
        events.append((order_id, db.name))

    response = await api.post(
        "/tasks", json={"path": task_path_of(uses_db), "args": ["o1"]}, headers=HEADERS
    )
    assert response.status_code == 200
    assert events == [("o1", "fake")]


async def test_yield_dependency_closes_after_task(client, http):
    _, api = http
    events: list = []

    async def get_db():
        events.append("open")
        try:
            yield Session("db", events)
            events.append("commit")
        except Exception as e:
            events.append(f"rollback:{type(e).__name__}")
            raise
        finally:
            events.append("close")

    @client.task()
    async def works(db: Annotated[Session, Depends(get_db)]):
        events.append("task")

    @client.task()
    async def fails(db: Annotated[Session, Depends(get_db)]):
        events.append("task")
        raise LookupError("missing")

    response = await api.post(
        "/tasks", json={"path": task_path_of(works)}, headers=HEADERS
    )
    assert response.status_code == 200
    assert events == ["open", "task", "commit", "close"]

    events.clear()
    response = await api.post(
        "/tasks", json={"path": task_path_of(fails)}, headers=HEADERS
    )
    assert response.status_code == 500
    # the dependency sees the task's own exception, before the response is built
    assert events == ["open", "task", "rollback:LookupError", "close"]


async def test_sync_yield_dependency_and_request_injection(client, http):
    _, api = http
    events: list = []

    def get_resource(request: Request):
        events.append(("open", request.url.path))
        yield "resource"
        events.append("close")

    @client.task()
    def sync_task(n: int, res: str = Depends(get_resource)):
        events.append(("task", n, res))

    response = await api.post(
        "/tasks",
        json={"path": task_path_of(sync_task), "kwargs": {"n": 2}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert events == [("open", "/tasks"), ("task", 2, "resource"), "close"]


async def test_dependency_teardown_failure_is_500(client, http):
    _, api = http

    async def get_db():
        yield Session("db", [])
        raise RuntimeError("commit failed")

    @client.task()
    async def works(db: Session = Depends(get_db)):
        pass

    response = await api.post(
        "/tasks", json={"path": task_path_of(works)}, headers=HEADERS
    )
    assert response.status_code == 500
