"""Offline, stateless scenario scripts shared by tests and the seeded demo."""

from dataclasses import dataclass
from types import MappingProxyType

from traceforge.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    Usage,
)


@dataclass(frozen=True)
class Failure:
    kind: str


def call(name: str, order_id: object, number: int = 1) -> ModelResponse:
    return ModelResponse.model_validate(
        {
            "tool_call": {
                "call_id": f"fake-call-{number}",
                "name": name,
                "arguments": {"order_id": order_id},
            },
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }
    )


FINAL = ModelResponse(
    text="Your fictional support request is complete.",
    usage=Usage(input_tokens=12, output_tokens=6, total_tokens=18),
)
SCENARIOS: MappingProxyType[str, tuple[ModelResponse | Failure, ...]] = MappingProxyType(
    {
        "text_only": (FINAL,),
        "valid_tool": (call("get_order", "ORDER-1001"), FINAL),
        "unknown_tool": (call("delete_everything", "ORDER-1001"),),
        "invalid_arguments": (call("get_order", 123),),
        "timeout": (Failure("timeout"),),
        "provider_error": (Failure("provider_error"),),
        "tool_failure": (call("get_order", "ORDER-MISSING"),),
        "refund_flow": (
            call("get_order", "ORDER-1001"),
            call("check_refund_eligibility", "ORDER-1001", 2),
            call("create_refund_request", "ORDER-1001", 3),
            FINAL,
        ),
    }
)


class FakeModelProvider:
    def complete(self, request: ModelRequest) -> ModelResponse:
        scenario = request.input.get("scenario", "text_only")
        if not isinstance(scenario, str) or scenario not in SCENARIOS:
            raise ProviderError("Unknown fake scenario")
        script = SCENARIOS[scenario]
        step = sum(message.role == "tool" for message in request.messages)
        if step >= len(script):
            raise ProviderError("Fake scenario exhausted")
        action = script[step]
        if isinstance(action, Failure):
            if action.kind == "timeout":
                raise TimeoutError("Simulated timeout")
            raise ProviderError("Simulated provider failure")
        return action.model_copy(deep=True)
