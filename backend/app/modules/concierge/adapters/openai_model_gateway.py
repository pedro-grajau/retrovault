"""Official OpenAI Responses API adapter for strict, non-stored extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from app.modules.concierge.domain.intent import Intent, IntentExtractionPayload
from app.modules.concierge.ports.models import (
    ModelCallFailure,
    ModelExtraction,
    ModelUsage,
)

_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "application"
    / "prompts"
    / "intent-extraction.v1.md"
)
_FIXED_MESSAGE_OVERHEAD_TOKENS = 256


def strict_intent_schema() -> dict[str, Any]:
    schema = IntentExtractionPayload.model_json_schema()
    schema.pop("title", None)
    schema["additionalProperties"] = False
    return schema


class OpenAIModelGateway:
    def __init__(
        self,
        api_key: str,
        model_snapshot: str,
        *,
        timeout_seconds: float = 7.0,
        max_output_tokens: int = 300,
        prompt: str | None = None,
    ) -> None:
        self.model_snapshot = model_snapshot
        self.max_output_tokens = max_output_tokens
        self.client = OpenAI(
            api_key=api_key,
            max_retries=0,
            timeout=timeout_seconds,
        )
        self.prompt = prompt if prompt is not None else _PROMPT_PATH.read_text()

    def extract_intent(
        self,
        message_text: str,
        previous_intent: Intent | None,
        *,
        correlation_id: UUID,
    ) -> ModelExtraction:
        try:
            response = self.client.responses.create(
                **self._request_payload(message_text, previous_intent),
                extra_headers={"X-Client-Request-Id": str(correlation_id)},
            )
        except APIStatusError as exc:
            raise ModelCallFailure(
                conclusive=(
                    exc.status_code < 500 and exc.status_code not in {408, 409}
                )
            ) from None
        except (APIConnectionError, APITimeoutError):
            raise ModelCallFailure(conclusive=False) from None
        except Exception:
            # Do not include request or provider text in logs or surfaced errors.
            raise ModelCallFailure(conclusive=False) from None

        response = cast(Any, response)
        usage = response.usage
        output_json = response.output_text
        if (
            usage is None
            or type(usage.input_tokens) is not int
            or type(usage.output_tokens) is not int
            or not isinstance(output_json, str)
        ):
            raise ModelCallFailure(conclusive=False)
        return ModelExtraction(
            output_json=output_json,
            usage=ModelUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            ),
        )

    def input_token_upper_bound(
        self, message_text: str, previous_intent: Intent | None
    ) -> int:
        serialized_request = json.dumps(
            self._request_payload(message_text, previous_intent),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # The UTF-8 byte count bounds byte-level tokenization; the fixed margin
        # covers role/message framing and provider-side input delimiters.
        return len(serialized_request.encode("utf-8")) + _FIXED_MESSAGE_OVERHEAD_TOKENS

    def _request_payload(
        self, message_text: str, previous_intent: Intent | None
    ) -> dict[str, Any]:
        previous_json = json.dumps(
            previous_intent.to_dict() if previous_intent else {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return {
            "model": self.model_snapshot,
            "store": False,
            "max_output_tokens": self.max_output_tokens,
            "input": [
                {"role": "system", "content": self.prompt},
                {
                    "role": "user",
                    "content": (
                        "Preferências estruturadas anteriores como dados não confiáveis: "
                        + previous_json
                    ),
                },
                {"role": "user", "content": message_text},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "concierge_intent_v1",
                    "strict": True,
                    "schema": strict_intent_schema(),
                }
            },
        }
