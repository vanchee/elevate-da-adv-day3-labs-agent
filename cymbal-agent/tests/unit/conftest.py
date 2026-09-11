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

"""Unit-test-only fixtures.

Tools now resolve credentials per invocation, which reaches Application Default
Credentials when no end-user token is present. Unit tests must stay hermetic, so ADC
lookup is stubbed here. Integration tests deliberately do NOT inherit this -- they need
the real ambient identity.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def stub_adc():
    """Replaces Application Default Credentials with an inert sentinel."""
    import app.auth

    app.auth._adc_credentials = None
    with patch("app.auth.google.auth.default", return_value=(object(), "test-project")):
        yield
    app.auth._adc_credentials = None
