# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""End-user credential delegation for data access tools.

By default an agent queries data as its own service identity, which makes it a
confused deputy: a store clerk and a regional auditor asking the same question receive
identical results, because BigQuery only ever sees the agent. This module resolves the
credentials a tool should use per invocation, preferring the end user's OAuth access
token when the host application has supplied one.

The token is read from `tool_context.state`, matching the contract ADK's own BigQuery
integration uses for `external_access_token_key`. The hosting application is
responsible for running the OAuth flow and writing the token into session state:

    session.state["user_access_token"] = "<end user OAuth 2.0 access token>"

Combined with BigQuery row-level security (see `sql/04_row_level_security.sql`), this
pushes authorisation down into the data layer, where the model cannot route around it.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

import google.auth
import google.oauth2.credentials

from app import config

logger = logging.getLogger(__name__)

BIGQUERY_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_adc_credentials = None


class DelegationError(PermissionError):
    """Raised when end-user credentials are required but unavailable."""


@dataclass(frozen=True)
class ResolvedCredentials:
    """Credentials plus a description of the principal they represent."""

    credentials: Any
    principal: str
    is_end_user: bool

    @property
    def audit_note(self) -> str:
        """A short string suitable for logs and tool output provenance."""
        return f"query executed as {self.principal}"


def _get_adc():
    """Returns cached Application Default Credentials (the agent's own identity)."""
    global _adc_credentials
    if _adc_credentials is None:
        _adc_credentials, _ = google.auth.default(scopes=BIGQUERY_SCOPES)
    return _adc_credentials


def _read_token(tool_context: Optional[Any]) -> Optional[str]:
    """Extracts the end-user access token from session state, if present."""
    if tool_context is None:
        return None
    state = getattr(tool_context, "state", None)
    if state is None:
        return None
    try:
        token = state.get(config.END_USER_TOKEN_STATE_KEY)
    except Exception:  # pragma: no cover - defensive against exotic state objects
        return None
    # MagicMock and similar test doubles return truthy objects for any attribute
    # access; only accept a real string token.
    return token if isinstance(token, str) and token else None


def resolve_credentials(tool_context: Optional[Any]) -> ResolvedCredentials:
    """Resolves the credentials a data tool should use for this invocation.

    Prefers the end user's delegated OAuth token. Falls back to the agent's own service
    identity unless `REQUIRE_END_USER_AUTH` is set, in which case the call fails closed
    rather than silently over-granting.

    Args:
        tool_context: The ADK-injected tool context carrying session state.

    Returns:
        The credentials to use, along with a description of the acting principal.

    Raises:
        DelegationError: If delegation is mandatory but no end-user token is present.
    """
    token = _read_token(tool_context)
    if token:
        logger.info("Using delegated end-user credentials for data access")
        return ResolvedCredentials(
            credentials=google.oauth2.credentials.Credentials(token=token),
            principal="the signed-in end user (delegated OAuth)",
            is_end_user=True,
        )

    if config.REQUIRE_END_USER_AUTH:
        raise DelegationError(
            "End-user authentication is required but no access token was found in "
            f"session state under '{config.END_USER_TOKEN_STATE_KEY}'. The hosting "
            "application must complete the OAuth flow before invoking data tools."
        )

    logger.info("No end-user token present; falling back to the agent service identity")
    return ResolvedCredentials(
        credentials=_get_adc(),
        principal="the agent service identity",
        is_end_user=False,
    )
