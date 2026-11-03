"""Testing helpers: record pushes instead of sending them to Cloud Tasks or running them.

    from cloudtask.testing import recorder

    def test_checkout_schedules_expiry(client):
        with recorder(ct) as pushed:
            checkout(...)
        assert pushed.last().path == "app.tasks.expire_order"
        assert pushed.last().kwargs == {"order_id": "o1"}

As a pytest fixture:

    @pytest.fixture
    def pushed():
        with recorder(ct) as pushed:
            yield pushed
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cloudtask.core import CloudTaskClient


@dataclass(frozen=True)
class PushedTask:
    """A task pushed while the client was in ``eager="record"`` mode."""

    path: str
    args: tuple
    kwargs: dict[str, Any]
    queue: str
    name: str | None = None
    schedule_time: datetime | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    task_path: str = ""

    @property
    def func_name(self) -> str:
        """The function name (last part of :attr:`path`)."""
        return self.path.rsplit(".", 1)[-1]


class PushedTasks(list[PushedTask]):
    """The tasks recorded by a client (``client.pushed``). A list, plus a few lookup helpers."""

    def last(self) -> PushedTask:
        """The most recent push. Raises ``AssertionError`` when nothing was pushed."""
        assert self, "No task was pushed."
        return self[-1]

    def of(self, task: str | Callable[..., Any]) -> list[PushedTask]:
        """Pushes of one task, given its dotted path or the decorated function."""
        if callable(task):
            from cloudtask.core import task_path_of

            task = task_path_of(task)
        return [pushed for pushed in self if pushed.path == task]

    def paths(self) -> list[str]:
        """The dotted paths of every push, in order."""
        return [pushed.path for pushed in self]


@contextmanager
def recorder(client: CloudTaskClient) -> Iterator[PushedTasks]:
    """
    Switch ``client`` to ``eager="record"`` for the block and yield the recorded pushes.

    Pushes are validated as in production (task names, reserved headers, JSON encoding, payload size) but neither
    sent to Google nor executed. The previous mode and recordings are restored on exit.
    """
    previous_mode, previous_pushed = client.eager, client.pushed
    client.eager = "record"
    client.pushed = PushedTasks()
    try:
        yield client.pushed
    finally:
        client.eager, client.pushed = previous_mode, previous_pushed
