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

"""Shared pytest configuration.

`adk web` loads `.env` automatically, but pytest does not. This module reproduces that
behaviour for integration tests, then fills in inert placeholders so unit tests remain
hermetic and never depend on ambient GCP configuration or credentials.
"""

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Loads .env into the environment without overriding anything already set."""
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# Hermetic fallbacks so unit tests run on a machine with no GCP configuration at all.
# `setdefault` means a real .env (loaded above) always wins for integration tests.
os.environ.setdefault("PROJECT_ID", "test-project")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("DATA_AGENT_ID", "test-data-agent")
os.environ.setdefault(
    "DATA_AGENT_RESOURCE",
    "projects/test-project/locations/global/dataAgents/test-data-agent",
)
os.environ.setdefault("BIGTABLE_MCP_SERVICE_URL", "https://mcp-toolbox-bigtable.invalid")
