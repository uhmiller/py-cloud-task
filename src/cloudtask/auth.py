"""Authentication of incoming task requests (worker side).

The worker endpoint is reachable from the internet, so it fails closed: a client used as a worker must configure a
shared secret, or explicitly opt out for local development.
"""

import hmac
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from cloudtask import exceptions

if TYPE_CHECKING:
    from cloudtask.core import CloudTaskClient

logger = logging.getLogger("cloudtask.auth")


def normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Lower-case header names and accept WSGI-style names (``HTTP_X_FOO`` → ``x-foo``)."""
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        name = key.lower().replace("_", "-").removeprefix("http-")
        normalized[name] = value
    return normalized


class WorkerAuthenticator:
    """Authenticates incoming task requests with the shared secret header, compared in constant time."""

    def __init__(self, client: CloudTaskClient):
        """
        Raises:
            ImproperlyConfigured: No secret is configured and unauthenticated access was not explicitly allowed.
        """
        self.secret: bytes | None = client.secret.encode() if client.secret else None
        self.secret_header = client.secret_header_name.lower()

        if self.secret is None:
            if not client.insecure_allow_unauthenticated:
                raise exceptions.ImproperlyConfigured(
                    "The task worker has no authentication: set 'secret' on the CloudTaskClient. For local "
                    "development only, 'insecure_allow_unauthenticated=True' disables this check."
                )
            logger.warning(
                "Task worker accepts UNAUTHENTICATED requests (insecure_allow_unauthenticated=True). "
                "Never use this setting in a deployed environment."
            )

    def authenticate(self, headers: Mapping[str, str]) -> None:
        """
        Check the secret header of a request.

        Raises:
            InvalidTaskSecretError: The header is missing or does not match.
        """
        if self.secret is None:
            return
        received = normalize_headers(headers).get(self.secret_header, "")
        if not hmac.compare_digest(received.encode(), self.secret):
            logger.warning("Rejected task request: invalid or missing secret header.")
            raise exceptions.InvalidTaskSecretError("Invalid or missing task secret.")
