# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Travel concierge — Dystopic benchmark port.

The upstream sample resolves Google Cloud credentials at import time and runs
on Vertex/Gemini. The port routes all model calls through LiteLLM so the model
is an environment choice (a harness-variant knob on the platform side), with
no cloud project needed to import the package.
"""

import os

MODEL_NAME = os.getenv("CONCIERGE_MODEL", "openai/gpt-4.1")

from google.adk.models.lite_llm import LiteLlm  # noqa: E402

MODEL = LiteLlm(model=MODEL_NAME)

from . import agent  # noqa: E402,F401
