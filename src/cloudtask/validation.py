"""Worker-side validation and coercion of task arguments (opt-in ``validate_args=True``, needs pydantic).

Arguments travel as JSON, so a ``datetime`` arrives as ``str``, a ``Decimal`` as ``str``, a Pydantic model as
``dict``. The validator converts each argument present in the payload to its annotated type. FastAPI dependencies
are not in the payload, so they are never validated here.
"""

import inspect
import typing
from collections.abc import Callable
from typing import Any

from cloudtask import exceptions


def ensure_pydantic() -> None:
    """
    Raises:
        ImproperlyConfigured: pydantic is not installed.
    """
    try:
        import pydantic  # noqa: F401
    except ImportError:
        raise exceptions.ImproperlyConfigured(
            "validate_args=True needs pydantic: install 'py-cloud-task[validation]'."
        ) from None


class ArgumentValidator:
    """Validates and coerces a task's payload arguments to the function's annotated types."""

    def __init__(self, func: Callable[..., Any]):
        self.func = func
        self._signature: inspect.Signature | None = None
        self._hints: dict[str, Any] = {}
        self._adapters: dict[str, Any] = {}

    def _prepare(self) -> inspect.Signature:
        # Resolved on first use: annotations may reference names defined after the decorator ran.
        if self._signature is None:
            self._hints = typing.get_type_hints(self.func, include_extras=True)
            self._signature = inspect.signature(self.func)
        return self._signature

    def _adapter(self, name: str) -> Any:
        if name not in self._adapters:
            from pydantic import ConfigDict, TypeAdapter
            from pydantic.errors import PydanticSchemaGenerationError

            annotation = self._hints[name]
            try:
                adapter = TypeAdapter(annotation)
            except PydanticSchemaGenerationError:
                adapter = TypeAdapter(
                    annotation, config=ConfigDict(arbitrary_types_allowed=True)
                )
            self._adapters[name] = adapter
        return self._adapters[name]

    def validate(self, args: list, kwargs: dict) -> tuple[list, dict]:
        """
        Return the arguments converted to their annotated types.

        Raises:
            InvalidTaskPayload: An argument doesn't match its annotation. The message names the parameters and
                the problems, never the values.
        """
        from pydantic import ValidationError

        signature = self._prepare()
        try:
            bound = signature.bind_partial(*args, **kwargs)
        except TypeError as e:
            raise exceptions.InvalidTaskPayload(f"Invalid payload: {e}") from None

        problems: list[str] = []
        for name, value in list(bound.arguments.items()):
            if name not in self._hints:
                continue
            adapter = self._adapter(name)
            kind = signature.parameters[name].kind
            try:
                if kind is inspect.Parameter.VAR_POSITIONAL:
                    bound.arguments[name] = tuple(
                        adapter.validate_python(v) for v in value
                    )
                elif kind is inspect.Parameter.VAR_KEYWORD:
                    bound.arguments[name] = {
                        k: adapter.validate_python(v) for k, v in value.items()
                    }
                else:
                    bound.arguments[name] = adapter.validate_python(value)
            except ValidationError as e:
                for error in e.errors(include_input=False, include_url=False):
                    location = ".".join(
                        str(part) for part in (name, *error.get("loc", ()))
                    )
                    problems.append(f"{location}: {error['msg']}")

        if problems:
            raise exceptions.InvalidTaskPayload(
                "Invalid payload: task arguments failed validation ("
                + "; ".join(problems)
                + ")."
            )
        return list(bound.args), dict(bound.kwargs)
