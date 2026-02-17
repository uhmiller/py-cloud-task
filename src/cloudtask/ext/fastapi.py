import inspect
from contextlib import AsyncExitStack
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from pydantic import BaseModel

from cloudtask import CloudTaskClient, exceptions
from cloudtask.executor import CloudTaskExecutor


class TaskRequestBody(BaseModel):
    """Schema for the Google Cloud Task payload."""

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

        # dependencies resolution
        async with AsyncExitStack() as stack:
            solved_result = await solve_dependencies(
                async_exit_stack=stack,
                request=self.request,
                dependency_overrides_provider=self.request.app.dependency_overrides,
                dependant=dependant,
                body=None,
                embed_body_fields=False,
            )

            # merge task kwargs and resolved dependencies
            # priority for dependencies
            task_kwargs = kwargs.copy()
            task_kwargs.update(solved_result.values)

            if solved_result.errors:
                raise exceptions.TaskExecutionError(
                    f"Dependency resolution failed: {solved_result.errors}"
                )

            return await super()._execute_impl(func, args, task_kwargs)


class CloudTaskRouter(APIRouter):
    """
    FastAPI Router specialized for handling Google Cloud Tasks.
    Usage:
        app.include_router(CloudTaskRouter(client), prefix="/tasks")
    """

    def __init__(self, client: CloudTaskClient, **kwargs):
        super().__init__(**kwargs)
        self.client = client
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

        except exceptions.InvalidTaskSecretError:
            raise HTTPException(status_code=403, detail="Invalid Task Secret")

        except exceptions.TaskNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))

        except exceptions.TaskExecutionError:
            raise HTTPException(
                status_code=500,
                detail="Task execution failed. Check worker logs for details.",
            )
