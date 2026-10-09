"""Official OpenAI Responses API adapter for strict, non-stored extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from app.modules.concierge.domain.intent import Intent
from app.modules.concierge.ports.models import (
    ModelCallFailure,
    ModelExtraction,
    ModelRanking,
    ModelUsage,
)

_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "application"
    / "prompts"
    / "intent-extraction.v3.md"
)
_RANKING_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "application"
    / "prompts"
    / "recommendation-ranking.v1.md"
)
_FIXED_MESSAGE_OVERHEAD_TOKENS = 256


def strict_intent_schema() -> dict[str, Any]:
    def nullable_text(maximum: int) -> dict[str, Any]:
        return {
            "anyOf": [
                {"type": "string", "maxLength": maximum},
                {"type": "null"},
            ]
        }

    def nullable_integer(minimum: int, maximum: int) -> dict[str, Any]:
        return {
            "anyOf": [
                {"type": "integer", "minimum": minimum, "maximum": maximum},
                {"type": "null"},
            ]
        }
    properties = {
        "platform": nullable_text(48),
        "genre": nullable_text(64),
        "style": nullable_text(96),
        "players": nullable_integer(1, 12),
        "price_min_brl_cents": nullable_integer(0, 100_000_000),
        "price_max_brl_cents": nullable_integer(0, 100_000_000),
        "constraints": {
            "type": "array",
            "items": {"type": "string", "maxLength": 120},
            "maxItems": 5,
        },
        "mode": {
            "anyOf": [
                {"type": "string", "enum": ["purchase", "rental"]},
                {"type": "null"},
            ]
        },
        "mode_cleared": {"type": "boolean"},
        "cleared_fields": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "platform", "genre", "style", "players", "price_range",
                    "constraints",
                ],
            },
            "maxItems": 6,
        },
        "clarification_field": {
            "type": "string",
            "enum": [
                "platform",
                "genre",
                "style",
                "players",
                "price_range",
                "none",
            ],
        },
        "rejections": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "game_id": {"type": "string", "maxLength": 36},
                    "reason": {
                        "type": "string",
                        "enum": [
                            "price", "platform", "genre", "style", "condition",
                            "availability", "players", "other",
                        ],
                    },
                },
                "required": ["game_id", "reason"],
                "additionalProperties": False,
            },
        },
        "rejection_ambiguous": {"type": "boolean"},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def strict_recommendation_schema() -> dict[str, Any]:
    properties = {
        "recommendations": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "game_id": {"type": "string", "maxLength": 36},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 96},
                        "maxItems": 8,
                    },
                },
                "required": ["game_id", "evidence"],
                "additionalProperties": False,
            },
        }
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


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
        self.ranking_prompt = _RANKING_PROMPT_PATH.read_text()

    def extract_intent(
        self,
        message_text: str,
        previous_intent: Intent | None,
        *,
        correlation_id: UUID,
        previous_options: list[dict[str, str]] | None = None,
    ) -> ModelExtraction:
        try:
            response = self.client.responses.create(
                **self._request_payload(message_text, previous_intent, previous_options),
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
        self,
        message_text: str,
        previous_intent: Intent | None,
        *,
        previous_options: list[dict[str, str]] | None = None,
    ) -> int:
        serialized_request = json.dumps(
            self._request_payload(message_text, previous_intent, previous_options),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # The UTF-8 byte count bounds byte-level tokenization; the fixed margin
        # covers role/message framing and provider-side input delimiters.
        return len(serialized_request.encode("utf-8")) + _FIXED_MESSAGE_OVERHEAD_TOKENS

    def _request_payload(
        self,
        message_text: str,
        previous_intent: Intent | None,
        previous_options: list[dict[str, str]] | None = None,
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
                        "Dados anteriores não confiáveis em JSON: "
                        + json.dumps(
                            {
                                "intent": json.loads(previous_json),
                                "options_from_previous_recommendation": previous_options or [],
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                },
                {"role": "user", "content": message_text},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "concierge_intent_v3",
                    "strict": True,
                    "schema": strict_intent_schema(),
                }
            },
        }

    def ranking_input_token_upper_bound(
        self, intent: Intent, candidates: list[dict[str, object]]
    ) -> int:
        serialized_request = json.dumps(
            self._ranking_request_payload(intent, candidates),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return len(serialized_request.encode("utf-8")) + _FIXED_MESSAGE_OVERHEAD_TOKENS

    def rank_recommendations(
        self,
        intent: Intent,
        candidates: list[dict[str, object]],
        *,
        correlation_id: UUID,
    ) -> ModelRanking:
        try:
            response = self.client.responses.create(
                **self._ranking_request_payload(intent, candidates),
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
        return ModelRanking(
            output_json,
            ModelUsage(usage.input_tokens, usage.output_tokens),
        )

    def _ranking_request_payload(
        self, intent: Intent, candidates: list[dict[str, object]]
    ) -> dict[str, Any]:
        return {
            "model": self.model_snapshot,
            "store": False,
            "max_output_tokens": min(self.max_output_tokens, 600),
            "input": [
                {"role": "system", "content": self.ranking_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"intent": intent.to_dict(), "candidates": candidates},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "concierge_recommendation_ranking_v1",
                    "strict": True,
                    "schema": strict_recommendation_schema(),
                }
            },
        }
