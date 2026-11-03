import asyncio
import dataclasses
import importlib
import inspect
import json
import pkgutil
import re
import string
import threading
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from functools import wraps
from logging import getLogger
from typing import TYPE_CHECKING, Any, Generic, Literal, NoReturn, ParamSpec, TypeVar
from zoneinfo import ZoneInfo

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2
from google.protobuf import duration_pb2, timestamp_pb2

from cloudtask import exceptions
from cloudtask.context import TaskContext, task_context_scope
from cloudtask.encoder import CloudTaskJSONEncoder
from cloudtask.testing import PushedTask, PushedTasks

if TYPE_CHECKING:
    from cloudtask.auth import WorkerAuthenticator

# Type aliases for better readability in decorators
P = ParamSpec("P")
R = TypeVar("R")

logger = getLogger("cloudtask")

IfExists = Literal["ignore", "raise"]
EagerMode = Literal["immediate", "remote", "record"]
TaskName = str | Callable[..., str]

# Cloud Tasks rejects tasks over 1 MB; checked on the JSON body, conservatively, before calling Google.
MAX_PAYLOAD_BYTES = 1_000_000

# Dispatch deadline accepted for HTTP tasks, in seconds.
MIN_TIMEOUT, MAX_TIMEOUT = 15, 30 * 60

# Background deliveries of `remote` mode, referenced until they finish.
_background_tasks: set[asyncio.Task] = set()

# Cloud Tasks task IDs: letters, digits, hyphens and underscores, at most 500 characters.
_TASK_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,500}")

# Headers Cloud Tasks sets or overrides itself (https://cloud.google.com/tasks/docs/reference/rest/v2/HttpRequest).
_RESERVED_HEADERS = frozenset({"host", "content-length", "user-agent"})
_RESERVED_HEADER_PREFIXES = ("x-google-", "x-appengine-", "x-cloudtasks-")


def validate_task_name(name: str) -> None:
    """
    Check a task name against Cloud Tasks rules (``[A-Za-z0-9_-]{1,500}``).

    Raises:
        ValueError: The name is empty, too long or has other characters (dots, colons, spaces...).
    """
    if not isinstance(name, str) or not _TASK_NAME_RE.fullmatch(name):
        length = len(name) if isinstance(name, str) else type(name).__name__
        raise ValueError(
            "Invalid task name: use only letters, digits, '-' and '_', 1 to 500 characters "
            f"(got {length})."
        )


def validate_timeout(timeout: int) -> None:
    """
    Raises:
        ValueError: The dispatch deadline is outside what Cloud Tasks accepts for HTTP tasks (15 s to 30 min).
    """
    if not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise ValueError(
            f"Task timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds (got {timeout})."
        )


def _loop_is_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _import_httpx():
    try:
        import httpx
    except ImportError:
        raise ImportError(
            "The 'httpx' library is required for eager='remote'. "
            "Install it with: uv add 'py-cloud-task[remote]'"
        ) from None
    return httpx


def _merge_headers(*layers: Mapping[str, str] | None) -> dict[str, str]:
    """Merge header layers case-insensitively; later layers win. ``_`` in names becomes ``-``."""
    merged: dict[str, tuple[str, str]] = {}
    for layer in layers:
        for name, value in (layer or {}).items():
            name = name.replace("_", "-")
            merged[name.lower()] = (name, str(value))
    return dict(merged.values())


def _check_reserved_headers(headers: Mapping[str, str], oidc: bool) -> None:
    for name in headers:
        lowered = name.lower()
        if (
            lowered in _RESERVED_HEADERS
            or lowered.startswith(_RESERVED_HEADER_PREFIXES)
            or (oidc and lowered == "authorization")
        ):
            raise ValueError(
                f"Header '{name}' is reserved by Cloud Tasks and can't be set."
            )


def _template_fields(template: str) -> set[str]:
    return {
        field
        for _, field, _, _ in string.Formatter().parse(template)
        if field is not None
    }


def _render_task_name(
    spec: TaskName, signature: inspect.Signature, args: tuple, kwargs: dict
) -> str:
    """Render a decorator name (template or callable) with the call's arguments, then validate it."""
    bound = signature.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    arguments = dict(bound.arguments)
    if callable(spec):
        rendered = spec(**arguments)
    else:
        try:
            rendered = spec.format(**arguments)
        except (KeyError, IndexError) as e:
            raise ValueError(
                f"Task name template {spec!r} references an unknown argument: {e}"
            ) from None
    validate_task_name(rendered)
    return rendered


class Task(Generic[P, R]):
    """
    Represents a task wrapper that captures the function and its arguments,
    ready to be executed locally or pushed to Google Cloud Tasks.
    """

    def __init__(
        self,
        client: CloudTaskClient,
        func: Callable[P, R],
        func_args: tuple,
        func_kwargs: dict,
        queue: str,
        url: str,
        name: str | None = None,
        timeout: int | None = None,
        headers: Mapping[str, str] | None = None,
        add_url_func_name_suffix: bool = False,
    ):
        """
        Initializes the Task instance.

        Args:
            client (CloudTaskClient): The client instance responsible for configuration.
            func (Callable): The original function to be executed.
            func_args (tuple): Positional arguments for the function.
            func_kwargs (dict): Keyword arguments for the function.
            queue (str): The target Cloud Tasks queue name.
            url (str): The target URL (worker endpoint) that will process the task.
            name (str | None, optional): Explicit task name (deduplication ID). Defaults to None.
            timeout (int | None, optional): Dispatch deadline in seconds. Defaults to None.
            headers (Mapping | None, optional): Headers from the decorator. Defaults to None.
            add_url_func_name_suffix (bool, optional): Whether to add the function name as a suffix to the URL. Defaults to False.
        """
        self.queue = queue
        self.url = url
        self.name = name
        self.timeout = timeout
        self.add_url_func_name_suffix = add_url_func_name_suffix

        self._client = client
        self._decorator_headers = dict(headers or {})
        self._instance_headers: dict[str, str] = {}

        self._func = func
        self._func_path = task_path_of(func)
        self._func_args = func_args
        self._func_kwargs = func_kwargs

        self._is_coroutine = inspect.iscoroutinefunction(func)

    def __call__(self) -> R | Coroutine[Any, Any, R]:
        """Allows the task instance to be called directly like the original function."""
        return self.execute()

    def execute(self) -> R | Coroutine[Any, Any, R]:
        """
        Executes the task immediately in the current process.

        This is used for:
        1. Local development (Eager mode).
        2. The actual execution logic inside the Worker/Consumer.

        Returns:
            R: The return value of the original function.
        """
        logger.debug("Executing task %s in the current process.", self._func_path)
        return self._func(*self._func_args, **self._func_kwargs)

    async def push(
        self,
        at: datetime | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        name: str | None = None,
        if_exists: IfExists = "ignore",
    ) -> str | None:
        """
        Push the task to Cloud Tasks (async, non-blocking). Use it in FastAPI and other async code.

        Args:
            at: When the task should run. Pass an aware datetime; a naive one gets the client timezone and logs a
                warning.
            headers: Extra headers for this push (highest precedence).
            name: Task name for this push only (deduplication), overriding the decorator/instance name.
            if_exists: What to do when a task with this name already exists: ``"ignore"`` (return its path) or
                ``"raise"`` (``TaskAlreadyExists``).

        Returns:
            The task resource name; the recorded path in ``record`` mode; None in ``immediate`` and ``remote``.

        Raises:
            ValueError: Invalid task name, timeout or reserved header.
            TaskAlreadyExists: The name is taken and ``if_exists="raise"``.
            TaskPushError: An argument isn't JSON-serializable, the payload is too large, or Cloud Tasks rejected
                the task.
        """
        mode = self._client.eager
        if mode == "immediate":
            await self._run_immediate(headers)
            return None
        if mode == "remote":
            await self._remote(headers=self._build_headers(headers))
            return None
        if mode == "record":
            return self._record(at, headers, name, if_exists)

        task_name = self._resolve_push_name(name)
        payload = self._build_task_request_payload(at, headers=headers, name=task_name)

        # Send via Async Client (gRPC aio)
        try:
            response = await self._client.service.create_task(request=payload)
        except AlreadyExists as e:
            return self._handle_already_exists(e, task_name, if_exists)
        except Exception as e:
            self._handle_error(e)
        logger.info("Task created: %s", response.name)
        return response.name

    delay = push
    """Alias of :meth:`push`."""

    def sync_push(
        self,
        schedule_time: datetime | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        name: str | None = None,
        if_exists: IfExists = "ignore",
    ) -> str | None:
        """
        Push the task to Cloud Tasks, blocking. Use it only in synchronous code (e.g. Django views).

        Same options, return value and errors as :meth:`push` (``schedule_time`` is ``at``).
        """
        mode = self._client.eager
        if mode == "immediate":
            self._run_immediate_sync(headers)
            return None
        if mode == "remote":
            self._remote_sync(headers=self._build_headers(headers))
            return None
        if mode == "record":
            return self._record(schedule_time, headers, name, if_exists)

        task_name = self._resolve_push_name(name)
        payload = self._build_task_request_payload(
            schedule_time, headers=headers, name=task_name
        )

        try:
            response = self._client.sync_service.create_task(request=payload)
        except AlreadyExists as e:
            return self._handle_already_exists(e, task_name, if_exists)
        except Exception as e:
            self._handle_error(e)
        logger.info("Task created: %s", response.name)
        return response.name

    sync_delay = sync_push
    """Alias of :meth:`sync_push`."""

    # --- eager modes -------------------------------------------------------------------------------------------

    def _eager_context(self, headers: Mapping[str, str] | None) -> TaskContext:
        """The task context of an in-process run: first attempt, with the headers a push would send."""
        context = TaskContext.from_headers(
            self._func_path,
            self._build_headers(headers),
            hidden=frozenset({self._client.secret_header_name.lower()}),
        )
        return dataclasses.replace(
            context,
            queue=self._client.force_to_queue or self.queue,
            task_name=self.name,
        )

    async def _run_immediate(self, headers: Mapping[str, str] | None) -> None:
        logger.info("[Eager] Running task %s in the current process.", self._func_path)
        with task_context_scope(self._eager_context(headers)):
            try:
                result = self.execute()
                if inspect.isawaitable(result):
                    await result
            except exceptions.TaskAbort as e:
                logger.warning("Task %s aborted: %s", self._func_path, e.reason)

    def _run_immediate_sync(self, headers: Mapping[str, str] | None) -> None:
        logger.info("[Eager] Running task %s in the current process.", self._func_path)
        if self._is_coroutine and _loop_is_running():
            raise RuntimeError(
                f"Task {self._func_path} is async and an event loop is running: "
                "use 'await task.push()' instead of 'task.sync_push()'."
            )
        with task_context_scope(self._eager_context(headers)):
            try:
                result = self.execute()
                if inspect.iscoroutine(result):
                    asyncio.run(result)
            except exceptions.TaskAbort as e:
                logger.warning("Task %s aborted: %s", self._func_path, e.reason)

    def _record(
        self,
        schedule_time: datetime | None,
        headers: Mapping[str, str] | None,
        name: str | None,
        if_exists: IfExists,
    ) -> str:
        """``record`` mode: validate the push like production and append it to ``client.pushed``."""
        task_name = self._resolve_push_name(name)
        final_headers = self._build_headers(headers)
        self._encode_body()
        self._validate_timeout()
        schedule_time = self._normalize_schedule_time(schedule_time)
        queue = self._client.force_to_queue or self.queue
        pushed = self._client.pushed

        if task_name and any(p.queue == queue and p.name == task_name for p in pushed):
            return self._handle_already_exists(
                AlreadyExists("recorded"), task_name, if_exists
            )

        path = self._task_path(task_name or f"recorded-{len(pushed) + 1}")
        pushed.append(
            PushedTask(
                path=self._func_path,
                args=self._func_args,
                kwargs=dict(self._func_kwargs),
                queue=queue,
                name=task_name,
                schedule_time=schedule_time,
                headers=final_headers,
                task_path=path,
            )
        )
        logger.debug("Task recorded: %s", path)
        return path

    def _get_url(self) -> str:
        """Returns the target URL for the task, either from the decorator or the client."""
        if self.add_url_func_name_suffix:
            return f"{self.url.rstrip('/')}/{self._func.__name__}"
        return self.url

    async def _remote(
        self, url: str | None = None, headers: dict[str, str] | None = None
    ) -> None:
        """
        Simulates Google Cloud Task execution by firing an HTTP POST request
        to the worker URL in the background without waiting for the execution response.

        Args:
            url (str | None): Override the task URL (useful if testing locally on a different port).
            headers (dict | None): Final request headers (defaults to :attr:`headers`).

        Raises:
            ImportError: If 'httpx' is not installed.
        """
        httpx = _import_httpx()
        target_url = url or self._get_url()
        payload = self._encode_body()
        headers = headers if headers is not None else self.headers

        logger.info("Remote task dispatched: %s -> %s", self._func_path, target_url)

        async def _send():
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        target_url,
                        content=payload,
                        headers=headers,
                        timeout=self.timeout,
                    )
            except Exception as e:
                logger.error(
                    "Failed to deliver remote task %s to the worker: %s",
                    self._func_path,
                    type(e).__name__,
                )

        # Schedule on the event loop and return immediately. Keep a reference until it finishes: the loop only
        # keeps weak references, so an unreferenced task may be garbage-collected before it runs.
        background = asyncio.create_task(_send())
        _background_tasks.add(background)
        background.add_done_callback(_background_tasks.discard)

    def _remote_sync(
        self, url: str | None = None, headers: dict[str, str] | None = None
    ) -> None:
        """
        Simulates Google Cloud Task execution by firing an HTTP POST request
        to the worker URL in a background thread without waiting for the execution response.

        Args:
            url (str | None): Override the task URL (useful if testing locally on a different port).
            headers (dict | None): Final request headers (defaults to :attr:`headers`).

        Raises:
            ImportError: If 'httpx' is not installed.
        """
        httpx = _import_httpx()
        target_url = url or self._get_url()
        payload = self._encode_body()
        headers = headers if headers is not None else self.headers

        logger.info("Remote task dispatched: %s -> %s", self._func_path, target_url)

        def _send():
            try:
                with httpx.Client() as client:
                    client.post(
                        target_url,
                        content=payload,
                        headers=headers,
                        timeout=self.timeout,
                    )
            except Exception as e:
                logger.error(
                    "Failed to deliver remote task %s to the worker: %s",
                    self._func_path,
                    type(e).__name__,
                )

        # Run in a background thread and return immediately
        thread = threading.Thread(target=_send, daemon=True)
        thread.start()

    # --- request building --------------------------------------------------------------------------------------

    def _build_headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """
        Final request headers. Precedence (lowest → highest): client ``context_headers()`` → decorator
        ``headers`` → ``task.headers`` → ``extra`` (``push(headers=)``). The secret and ``Content-Type`` are set
        last and can't be overridden.

        Raises:
            ValueError: A header is reserved by Cloud Tasks.
        """
        merged = _merge_headers(
            self._client._get_context_headers(),
            self._decorator_headers,
            self._instance_headers,
            extra,
        )
        _check_reserved_headers(merged, oidc=bool(self._client.sae))

        library: dict[str, str] = {}
        if self._client.secret:
            library[self._client.secret_header_name] = self._client.secret
        library["Content-Type"] = "application/json"
        return _merge_headers(merged, library)

    def _resolve_push_name(self, name: str | None) -> str | None:
        if name is None:
            return self.name
        validate_task_name(name)
        return name

    def _encode_body(self) -> bytes:
        """
        Encode the payload as JSON.

        Raises:
            TaskPushError: An argument can't be encoded (the message names it, never its value), or the payload
                is over :data:`MAX_PAYLOAD_BYTES`.
        """
        try:
            body = json.dumps(self.data, cls=CloudTaskJSONEncoder).encode("utf-8")
        except (TypeError, ValueError) as e:
            argument = self._unencodable_argument()
            raise exceptions.TaskPushError(
                f"Task {self._func_path}: argument '{argument}' is not JSON-serializable "
                f"({type(e).__name__}). Pass ids and primitives."
            ) from e
        if len(body) > MAX_PAYLOAD_BYTES:
            raise exceptions.TaskPushError(
                f"Task {self._func_path}: payload too large ({len(body)} bytes, limit {MAX_PAYLOAD_BYTES}). "
                "Pass ids and read the data inside the task."
            )
        return body

    def _unencodable_argument(self) -> str:
        """Name (or position) of the first argument the JSON encoder rejects."""
        try:
            bound = inspect.signature(self._func).bind_partial(
                *self._func_args, **self._func_kwargs
            )
            values = list(bound.arguments.items())
        except TypeError:
            values = [(f"#{i}", v) for i, v in enumerate(self._func_args)]
            values += list(self._func_kwargs.items())
        for argument, value in values:
            try:
                json.dumps(value, cls=CloudTaskJSONEncoder)
            except TypeError, ValueError:
                return argument
        return "?"

    def _validate_timeout(self) -> None:
        if self.timeout:
            validate_timeout(self.timeout)

    def _normalize_schedule_time(
        self, schedule_time: datetime | None
    ) -> datetime | None:
        if schedule_time is None:
            return None
        if schedule_time.tzinfo is None:
            logger.warning(
                "Task %s scheduled with a naive datetime; assuming %s. Pass an aware datetime.",
                self._func_path,
                self._client.timezone,
            )
            schedule_time = schedule_time.replace(tzinfo=self._client.timezone)
        if schedule_time < datetime.now(UTC):
            logger.debug("Task %s scheduled in the past; it runs now.", self._func_path)
        return schedule_time

    def _build_task_request_payload(
        self,
        schedule_time: datetime | None,
        headers: Mapping[str, str] | None = None,
        name: str | None = None,
    ) -> dict:
        """Constructs the dictionary payload expected by Google Cloud Tasks API."""
        http_request = {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": self._get_url(),
            "headers": self._build_headers(headers),
            "body": self._encode_body(),
        }

        # OIDC token for Cloud Run IAM (the library itself authenticates with the secret header)
        if self._client.sae:
            http_request["oidc_token"] = {"service_account_email": self._client.sae}

        task: dict = {"http_request": http_request}

        # Explicit Task Naming (Deduplication)
        if name is None:
            name = self.name
        if name:
            task["name"] = self._task_path(name)

        # Dispatch Deadline (Timeout)
        if self.timeout:
            self._validate_timeout()
            duration = duration_pb2.Duration()
            duration.FromSeconds(self.timeout)
            task["dispatch_deadline"] = duration

        # Scheduling
        schedule_time = self._normalize_schedule_time(schedule_time)
        if schedule_time:
            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(schedule_time)
            task["schedule_time"] = timestamp

        return {
            "parent": self.queue_path,
            "task": task,
        }

    def _handle_already_exists(
        self, error: Exception, name: str | None, if_exists: IfExists
    ) -> str | None:
        path = self._task_path(name) if name else None
        if if_exists == "raise":
            raise exceptions.TaskAlreadyExists(
                f"Task {path} already exists."
            ) from error
        logger.info("Task %s already exists; not pushed again", path)
        return path

    def _handle_error(self, e: Exception) -> NoReturn:
        logger.error("Failed to push task %s", self._func_path, exc_info=True)
        raise exceptions.TaskPushError(str(e)) from e

    @property
    def name(self) -> str | None:
        """Task name (deduplication ID). Validated against Cloud Tasks naming rules on assignment."""
        return self._name

    @name.setter
    def name(self, value: str | None) -> None:
        if value is not None:
            validate_task_name(value)
        self._name = value

    @property
    def data(self) -> dict[str, Any]:
        """Returns the payload data structure to be sent to the worker."""
        return {
            "func": self._func.__name__,
            "path": self.path,
            "args": self._func_args,
            "kwargs": self._func_kwargs,
        }

    @property
    def headers(self) -> dict[str, str]:
        """The request headers (context, decorator and instance headers, plus the secret and Content-Type)."""
        return self._build_headers()

    @headers.setter
    def headers(self, headers: Mapping[str, str]):
        """Add instance headers (merged over the decorator headers)."""
        self._instance_headers.update(headers)

    @property
    def is_coroutine(self) -> bool:
        return self._is_coroutine

    @property
    def path(self) -> str:
        """Returns the fully qualified dot-path of the function."""
        return self._func_path

    def _task_path(self, name: str) -> str:
        return self._client.service_utils.task_path(
            self._client.project,
            self._client.location,
            self._client.force_to_queue or self.queue,
            name,
        )

    @property
    def task_path(self) -> str | None:
        """Returns the full GCP Resource path for the task name."""
        if self._client.eager or not self.name:
            return None
        return self._task_path(self.name)

    @property
    def queue_path(self) -> str | None:
        """Returns the full GCP Resource path for the queue."""
        if self._client.eager:
            return None

        return self._client.service_utils.queue_path(
            self._client.project,
            self._client.location,
            self._client.force_to_queue or self.queue,
        )


def task_path_of(func: Callable) -> str:
    """Return the dotted path that identifies a task function (``module.qualname``)."""
    return f"{func.__module__}.{func.__qualname__}"


class CloudTaskClient:
    """
    Main client for configuring and creating Cloud Tasks.
    Acts as a factory for decorators.
    """

    def __init__(
        self,
        queue: str,
        project: str,
        location: str,
        url: str,
        sae: str,
        timeout: int | None = None,
        secret: str = "",
        secret_header_name: str = "X-PYCT-SECRET",
        timezone: str | ZoneInfo | None = None,
        force_to_queue: str | None = None,
        eager: EagerMode | None = None,
        add_url_func_name_suffix: bool = False,
        insecure_allow_unauthenticated: bool = False,
        context_headers: Callable[[], Mapping[str, str]] | None = None,
        retry_unknown_tasks: bool = False,
        validate_args: bool = False,
    ):
        """
        Initializes the CloudTaskClient.

        Args:
            queue (str): Default Cloud Tasks queue name.
            project (str): GCP Project ID.
            location (str): GCP Region (e.g., 'europe-west1').
            url (str): Default target URL (worker endpoint).
            sae (str): Service Account Email for OIDC authentication.
            eager (str | None, optional): Development and test modes instead of pushing to GCP:
                ``"immediate"`` runs the task in-process, ``"remote"`` POSTs it to the worker URL in the background,
                ``"record"`` appends it to ``client.pushed``. Defaults to None (production).
            timeout (int | None, optional): Default dispatch deadline for tasks in seconds (15 to 1800).
                Defaults to None (Cloud Tasks default, 10 minutes).
            secret (str, optional): A shared secret/token to send in headers for security. Defaults to "".
            secret_header_name (str, optional): The header name for the secret. Defaults to "X-PYCT-SECRET".
            timezone (str | ZoneInfo | None, optional): Default timezone for scheduling. Defaults to UTC.
            force_to_queue (str | NOne, optional): Usefull for dev and staging env, to for use the same queue
            add_url_func_name_suffix (bool, optional): Whether to add the function name as a prefix to the URL. Defaults to False.
            insecure_allow_unauthenticated (bool, optional): Local development only. Let the worker run tasks
                without a secret. Defaults to False.
            context_headers (Callable | None, optional): Called on every push; returns headers to propagate to
                the task (request id, ``traceparent``...). Lowest precedence. If it raises, the push goes on
                without them. Defaults to None.
            retry_unknown_tasks (bool, optional): Worker side: answer 404 (Cloud Tasks retries) instead of
                200 "rejected" for tasks that aren't registered, e.g. during blue/green deploys. Defaults to False.
            validate_args (bool, optional): Worker side: validate and convert task arguments to their annotated
                types with pydantic (``datetime``, ``Decimal``, enums, models). Overridable per task.
                Defaults to False.
        """
        self.queue = queue
        self.location = location
        self.url = url
        self.project = project
        self.sae = sae
        self.eager = eager
        self.timeout = timeout
        self.secret = secret
        self.secret_header_name = secret_header_name
        self.force_to_queue = force_to_queue
        self.add_url_func_name_suffix = add_url_func_name_suffix
        self.insecure_allow_unauthenticated = insecure_allow_unauthenticated
        self.context_headers = context_headers
        self.retry_unknown_tasks = retry_unknown_tasks
        self.validate_args = validate_args
        if timeout:
            validate_timeout(timeout)
        if validate_args:
            from cloudtask.validation import ensure_pydantic

            ensure_pydantic()

        # Pushes recorded in eager="record" mode (see cloudtask.testing).
        self.pushed = PushedTasks()

        # Tasks the worker is allowed to run, keyed by their dotted path.
        self.registry: dict[str, Callable[..., Task]] = {}
        self._authenticator: WorkerAuthenticator | None = None

        # Timezone Configuration
        if isinstance(timezone, str):
            self.timezone = ZoneInfo(timezone)
        else:
            self.timezone = timezone or ZoneInfo("UTC")

        self._sync_client = None
        self._async_client = None

        logger.debug(
            "CloudTaskClient initialized. Eager: %s, Timezone: %s", eager, self.timezone
        )

    def task(
        self,
        queue: str | None = None,
        name: TaskName | None = None,
        url: str | None = None,
        timeout: int | None = None,
        headers: dict[str, str] | None = None,
        validate_args: bool | None = None,
    ) -> Callable[[Callable[P, R]], Callable[P, Task[P, R]]]:
        """
        Decorator to convert a function into a Cloud Task.

        Args:
            queue (str | None, optional): Override default queue.
            name (str | Callable | None, optional): Task name (deduplication ID) for each call. A template
                formatted with the call's arguments (``"expire-order-{order_id}"``) or a callable receiving them
                as keyword arguments. A static name is shared by every call (only the first push succeeds).
            url (str | None, optional): Override default target URL.
            timeout (int | None, optional): Override default timeout (15 to 1800 seconds).
            headers (dict | None, optional): Add specific headers.
            validate_args (bool | None, optional): Override the client's ``validate_args`` for this task.

        Returns:
            Callable: The decorated function which returns a Task object when called.
        """

        def decorator(func: Callable[P, R]) -> Callable[P, Task[P, R]]:
            path = task_path_of(func)
            signature = inspect.signature(func)
            if timeout:
                validate_timeout(timeout)
            validate = self.validate_args if validate_args is None else validate_args
            validator = None
            if validate:
                from cloudtask.validation import ArgumentValidator, ensure_pydantic

                ensure_pydantic()
                validator = ArgumentValidator(func)
            static_name = isinstance(name, str) and not _template_fields(name)
            if static_name:
                validate_task_name(name)
                logger.warning(
                    "Task %s has a static name: every call shares it, so only the first push is accepted. "
                    "Use a template such as 'name-{arg}' or a callable.",
                    path,
                )

            @wraps(func)
            def inner(*args: P.args, **kwargs: P.kwargs) -> Task[P, R]:
                task_name = (
                    name
                    if static_name or name is None
                    else _render_task_name(name, signature, args, kwargs)
                )
                return Task(
                    func=func,
                    func_args=args,
                    func_kwargs=kwargs,
                    queue=queue or self.queue,
                    url=url or self.url,
                    timeout=timeout or self.timeout,
                    name=task_name,
                    headers=headers,
                    client=self,
                    add_url_func_name_suffix=self.add_url_func_name_suffix,
                )

            inner.__cloudtask_validator__ = validator  # type: ignore[attr-defined]
            self._register(path, inner)
            return inner

        return decorator

    def _get_context_headers(self) -> Mapping[str, str]:
        """Call the ``context_headers`` provider; a failing provider never blocks the push."""
        if self.context_headers is None:
            return {}
        try:
            return dict(self.context_headers())
        except Exception as e:
            logger.warning(
                "context_headers provider failed (%s); pushing without context headers.",
                type(e).__name__,
            )
            return {}

    def _register(self, path: str, factory: Callable[..., Task]) -> None:
        if path in self.registry:
            logger.debug("Task %s registered again; keeping the last definition.", path)
        self.registry[path] = factory

    def get_task(self, path: str) -> Callable[..., Task]:
        """
        Return the registered task factory for a dotted path.

        Raises:
            TaskNotFound: No task is registered under that path.
        """
        try:
            return self.registry[path]
        except KeyError:
            raise exceptions.TaskNotFound(f"Task '{path}' is not registered.") from None

    def autodiscover(self, *packages: str) -> None:
        """
        Import the given modules or packages (and all their submodules) so their tasks register.

        The worker only runs registered tasks, and tasks register when their module is imported. Call this at
        startup, e.g. ``client.autodiscover("myapp.tasks")``.
        """
        for name in packages:
            module = importlib.import_module(name)
            if hasattr(module, "__path__"):
                for info in pkgutil.walk_packages(
                    module.__path__, prefix=f"{module.__name__}."
                ):
                    importlib.import_module(info.name)

    @property
    def authenticator(self) -> WorkerAuthenticator:
        """
        Worker-side request authenticator, built (and the configuration checked) on first access.

        Raises:
            ImproperlyConfigured: The worker would accept unauthenticated requests.
        """
        if self._authenticator is None:
            from cloudtask.auth import WorkerAuthenticator

            self._authenticator = WorkerAuthenticator(self)
        return self._authenticator

    @property
    def sync_service(self):
        """Lazy load synchronous client (google.cloud.tasks_v2.CloudTasksClient)"""
        if self.eager:
            return None

        if not self._sync_client:
            self._sync_client = tasks_v2.CloudTasksClient()
        return self._sync_client

    @property
    def service(self):
        """Lazy load ASYNC client (google.cloud.tasks_v2.CloudTasksAsyncClient)"""
        if self.eager:
            return None
        if not self._async_client:
            self._async_client = tasks_v2.CloudTasksAsyncClient()
        return self._async_client

    @property
    def service_utils(self):
        """Helper to access paths (can use either client class, they share helpers)"""
        return tasks_v2.CloudTasksClient
