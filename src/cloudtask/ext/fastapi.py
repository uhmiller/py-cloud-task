import inspect
import logging
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from pydantic import BaseModel

from cloudtask import CloudTaskClient, exceptions
from cloudtask.core import task_path_of
from cloudtask.executor import (
    CONTROL_FLOW_EXCEPTIONS,
    CloudTaskExecutor,
    _log_control_flow,
)

logger = logging.getLogger("cloudtask.fastapi")


class TaskRequestBody(BaseModel):
    """Schema for the Google Cloud Task payload (OpenAPI docs; the executor validates the body itself)."""

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
                    # Missing or invalid task arguments: retrying can't fix them.
                    fields = sorted(
                        {
                            ".".join(map(str, err.get("loc", ())))
                            for err in solved_result.errors
                        }
                    )
                    raise exceptions.InvalidTaskPayload(
                        f"Invalid payload: task arguments failed validation ({', '.join(fields)})."
                    )

                # merge task kwargs and resolved dependencies (dependencies win)
                task_kwargs = {**kwargs, **solved_result.values}
                return await self._call(func, args, task_kwargs)

        except exceptions.TaskExecutionError:
            raise
        except CONTROL_FLOW_EXCEPTIONS as e:
            _log_control_flow(task_path_of(func), e)
            raise
        except Exception as e:
            logger.exception("Async task failed: %s", task_path_of(func))
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

    Responses (Cloud Tasks retries every non-2xx response):

    - 200 ``{"status": "success"}``: the task ran.
    - 200 ``{"status": "aborted", "reason": ...}``: the task raised ``TaskAbort``; no retry.
    - 200 ``{"status": "rejected", "error": ...}``: invalid payload or unknown task; retrying can't fix it.
      With ``client.retry_unknown_tasks=True`` an unknown task answers 404 instead.
    - 403: authentication failed. 503: the task raised ``TaskRetry``. 500: the task failed.
    """

    def __init__(self, client: CloudTaskClient, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        # Fail closed at startup: refuses to build a worker without authentication.
        client.authenticator
        self.post(
            "",
            response_model=dict[str, Any],
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": TaskRequestBody.model_json_schema()
                        }
                    },
                }
            },
        )(self._run_task)

    async def _run_task(self, request: Request):
        """
        Internal handler that acts as a bridge between FastAPI and CloudTaskExecutor.

        The body is parsed here, not by FastAPI, so authentication runs before any payload validation.
        """
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        executor = FastAPICloudTaskExecutor(client=self.client, request=request)

        try:
            # The result is not returned: it may not be serializable (which would turn a success into a retry)
            # and may carry personal data.
            await executor.run(body=payload, headers=request.headers)
            return {"status": "success"}

        except exceptions.TaskAuthenticationError:
            raise HTTPException(status_code=403, detail="Invalid task credentials")

        except exceptions.TaskAbort as e:
            return {"status": "aborted", "reason": e.reason}

        except exceptions.TaskRetry:
            return JSONResponse(status_code=503, content={"status": "retry"})

        except exceptions.InvalidTaskPayload as e:
            return {"status": "rejected", "error": str(e)}

        except exceptions.TaskNotFound as e:
            if self.client.retry_unknown_tasks:
                raise HTTPException(status_code=404, detail=str(e))
            return {"status": "rejected", "error": str(e)}

        except exceptions.TaskExecutionError:
            raise HTTPException(
                status_code=500,
                detail="Task execution failed. Check worker logs for details.",
            )
