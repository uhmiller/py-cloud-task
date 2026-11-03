"""Opt-in argument validation and coercion."""

import enum
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import BaseModel

from cloudtask import exceptions
from cloudtask.core import task_path_of
from cloudtask.executor import CloudTaskExecutor
from cloudtask.ext.fastapi import CloudTaskRouter
from tests.conftest import SECRET

HEADERS = {"X-PYCT-SECRET": SECRET}


class Color(enum.StrEnum):
    RED = "red"


class Item(BaseModel):
    sku: str
    qty: int


async def test_arguments_are_coerced_to_annotations(make_client):
    client = make_client(validate_args=True)
    seen = []

    @client.task()
    async def work(
        when: datetime, price: Decimal, color: Color, item: Item, *, n: int = 0
    ):
        seen.append((when, price, color, item, n))

    body = {
        "path": task_path_of(work),
        "args": ["2030-01-01T00:00:00Z", "12.50", "red", {"sku": "a", "qty": "2"}],
        "kwargs": {"n": "3"},
    }
    await CloudTaskExecutor(client).run(body, HEADERS)
    assert seen == [
        (
            datetime(2030, 1, 1, tzinfo=UTC),
            Decimal("12.50"),
            Color.RED,
            Item(sku="a", qty=2),
            3,
        )
    ]


async def test_round_trip_from_push_payload(make_client):
    """What the encoder produces on push is what the validator accepts on the worker."""
    client = make_client(validate_args=True, eager="record")
    seen = []

    @client.task()
    def work(when: datetime, price: Decimal, item: Item):
        seen.append((when, price, item))

    original = (
        datetime(2030, 1, 1, 12, 30, tzinfo=UTC),
        Decimal("9.99"),
        Item(sku="b", qty=1),
    )
    task = work(*original)
    task.sync_push()
    body = json.loads(task._encode_body())
    CloudTaskExecutor(client).sync_run(body, HEADERS)
    assert seen == [original]


async def test_invalid_argument_is_rejected_without_its_value(make_client):
    client = make_client(validate_args=True)
    ran = []

    @client.task()
    async def work(qty: int):
        ran.append(qty)

    with pytest.raises(exceptions.InvalidTaskPayload) as info:
        await CloudTaskExecutor(client).run(
            {"path": task_path_of(work), "args": ["+351-secret-phone"]}, HEADERS
        )
    assert "qty" in str(info.value)
    assert "secret-phone" not in str(info.value)
    assert ran == []


async def test_validation_is_off_by_default_and_per_task(make_client):
    client = make_client()
    seen = []

    @client.task()
    def raw(qty: int):
        seen.append(qty)

    @client.task(validate_args=True)
    def checked(qty: int):
        seen.append(qty)

    executor = CloudTaskExecutor(client)
    executor.sync_run({"path": task_path_of(raw), "args": ["5"]}, HEADERS)
    executor.sync_run({"path": task_path_of(checked), "args": ["5"]}, HEADERS)
    assert seen == ["5", 5]


async def test_dependencies_are_not_validated(make_client):
    client = make_client(validate_args=True)
    seen = []

    class Session:
        pass

    def get_db():
        return Session()

    @client.task()
    async def work(order_id: int, db: Annotated[Session, Depends(get_db)]):
        seen.append((order_id, type(db).__name__))

    app = FastAPI()
    app.include_router(CloudTaskRouter(client), prefix="/tasks")
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await api.post(
        "/tasks", json={"path": task_path_of(work), "args": ["7"]}, headers=HEADERS
    )
    assert response.json() == {"status": "success"}
    assert seen == [(7, "Session")]

    response = await api.post(
        "/tasks", json={"path": task_path_of(work), "args": ["x"]}, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_validate_args_without_pydantic_fails_at_startup(make_client, monkeypatch):
    monkeypatch.setitem(sys.modules, "pydantic", None)
    with pytest.raises(exceptions.ImproperlyConfigured, match="validation"):
        make_client(validate_args=True)
