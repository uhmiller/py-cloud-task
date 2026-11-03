from collections.abc import Callable
from typing import Any

import pytest

from cloudtask import CloudTaskClient

WORKER_URL = "https://api.example.com/tasks"
SAE = "tasks@project.iam.gserviceaccount.com"
SECRET = "s3cret-value"


@pytest.fixture
def make_client() -> Callable[..., CloudTaskClient]:
    """Build a client with test defaults; keyword arguments override them."""

    def factory(**overrides: Any) -> CloudTaskClient:
        options: dict[str, Any] = {
            "queue": "default",
            "project": "proj",
            "location": "europe-west1",
            "url": WORKER_URL,
            "sae": SAE,
            "secret": SECRET,
        }
        options.update(overrides)
        return CloudTaskClient(**options)

    return factory


@pytest.fixture
def client(make_client) -> CloudTaskClient:
    return make_client()
