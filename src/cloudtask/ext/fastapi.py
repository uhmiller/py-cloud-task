import inspect
import logging
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from pydantic import BaseModel

from cloudtask import CloudTaskClient, exceptions
from cloudtask.executor import CloudTaskExecutor

logger = logging.getLogger("cloudtask.fastapi")


class TaskRequestBody(BaseModel):
    """Schema for the Google Cloud Task payload."""

    func: str | None = None
    path: str
    args: list[Any] = []
    kwargs: dict[str, Any] = {}


class FastAPICloudTaskExecutor(CloudTaskExecutor):
    def __init__(self, client: CloudTaskClient, request: Request):
        super().__init__(client)
        self.request = request

    async def _execute_impl(self, func: Callable, args: list, kwargs: dict) -> Any:

        # analyze the function to get dependencies
        dependant: Dependant = get_dependant(
            path=self.request.url.path,
            call=func,
        )

        if not dependant.dependencies:
            return await super()._execute_impl(func, args, kwargs)

        signature = inspect.signature(func)

        try:
            bound = signature.bind_partial(*args, **kwargs)
            provided_params = set(bound.arguments.keys())
        except TypeError:
            provided_params = set()

        dependant.query_params = [
            p for p in dependant.query_params if p.name not in provided_params
        ]
        dependant.header_params = [
            p for p in dependant.header_params if p.name not in provided_params
        ]
        dependant.cookie_params = [
            p for p in dependant.cookie_params if p.name not in provided_params
        ]
        dependant.body_params = [
            p for p in dependant.body_params if p.name not in provided_params
        ]

        # Dependencies are solved on a stack owned by this execution, so `yield` dependencies (a DB session)
        # are closed right after the task, and see the task's own exception (rollback) when it fails.
        try:
            async with AsyncExitStack() as stack:
                with self._task_exit_stack(stack):
                    solved_result = await solve_dependencies(
                        async_exit_stack=stack,
                        request=self.request,
                        dependency_overrides_provider=self.request.app,
                        dependant=dependant,
                        body=None,
                        embed_body_fields=False,
                    )

                if solved_result.errors:
                    logger.error(
                        "Dependency resolution failed for task %s", func.__name__
                    )
                    raise exceptions.TaskExecutionError(
                        f"Dependency resolution failed for '{func.__name__}'."
                    )

                # merge task kwargs and resolved dependencies (dependencies win)
                task_kwargs = {**kwargs, **solved_result.values}
                return await self._call(func, args, task_kwargs)

        except exceptions.TaskExecutionError:
            raise
        except Exception as e:
            logger.exception("Async task failed: %s", func.__name__)
            raise exceptions.TaskExecutionError(str(e)) from e

    @contextmanager
    def _task_exit_stack(self, stack: AsyncExitStack) -> Iterator[None]:
        """Make FastAPI register `yield` dependencies on ``stack`` instead of the request's stacks."""
        scope = self.request.scope
        keys = ("fastapi_inner_astack", "fastapi_function_astack")
        previous = {key: scope.get(key) for key in keys}
        scope.update(dict.fromkeys(keys, stack))
        try:
            yield
        finally:
            scope.update(previous)


class CloudTaskRouter(APIRouter):
    """
    FastAPI Router specialized for handling Google Cloud Tasks.
    Usage:
        app.include_router(CloudTaskRouter(client), prefix="/tasks")
    """

    def __init__(self, client: CloudTaskClient, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        # Fail closed at startup: refuses to build a worker without authentication.
        client.authenticator
        self.post("", response_model=dict[str, Any])(self._run_task)

    async def _run_task(self, request: Request, body: TaskRequestBody):
        """
        Internal handler that acts as a bridge between FastAPI and CloudTaskExecutor.
        """

        headers = dict(request.headers)
        payload = body.model_dump()
        executor = FastAPICloudTaskExecutor(client=self.client, request=request)

        try:
            result = await executor.run(body=payload, headers=headers)
            return {"status": "success", "result": result}

        except exceptions.TaskAuthenticationError:
            raise HTTPException(status_code=403, detail="Invalid task credentials")

        except exceptions.TaskNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))

        except exceptions.TaskExecutionError:
            raise HTTPException(
                status_code=500,
                detail="Task execution failed. Check worker logs for details.",
            )
