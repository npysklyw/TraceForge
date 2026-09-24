"""Bounded model/tool loop. It knows neither fake scenarios nor database sessions."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from pydantic import JsonValue

from traceforge.application.redaction import TraceSanitizer
from traceforge.providers.base import Message, ModelProvider, ModelRequest, ModelResponse, Usage
from traceforge.tools.registry import InvalidToolArguments, ToolRegistry, UnknownTool


@dataclass(frozen=True)
class ExecutionEventData:
    kind: str
    payload: dict[str, JsonValue]
    started_at: datetime
    finished_at: datetime
    latency_ms: float
    usage: Usage


@dataclass(frozen=True)
class ToolObservation:
    sequence: int
    call_id: str
    name: str
    arguments: dict[str, JsonValue]
    arguments_validated: bool
    output: JsonValue
    started_at: datetime
    finished_at: datetime
    latency_ms: float
    error_type: str | None
    error: str | None


@dataclass(frozen=True)
class Outcome:
    output: str | None
    error_type: str | None
    error: str | None
    usage: Usage
    started_at: datetime
    finished_at: datetime
    latency_ms: float


ERROR_MESSAGES = {
    "provider_timeout": "The model provider timed out.",
    "provider_error": "The model provider could not complete the request.",
    "unknown_tool": "The requested tool is not registered.",
    "invalid_tool_arguments": "Tool arguments do not match the registered schema.",
    "tool_error": "The tool could not complete the request.",
    "step_limit": "The model exceeded the permitted number of steps.",
}


class AgentRunner:
    def __init__(self, provider: ModelProvider, registry: ToolRegistry) -> None:
        self.provider = provider
        self.registry = registry

    def run(
        self,
        request: ModelRequest,
        *,
        max_steps: int,
        sanitizer: TraceSanitizer,
        on_event: Callable[[ExecutionEventData], None],
        on_tool: Callable[[ToolObservation], None],
    ) -> Outcome:
        if not 1 <= max_steps <= 12:
            raise ValueError("max_steps must be between 1 and 12")
        started_at, start = datetime.now(UTC), perf_counter()
        messages: list[Message] = []
        usages: list[Usage] = []
        tool_count = 0

        def emit(
            kind: str,
            payload: dict[str, JsonValue],
            began: datetime | None = None,
            elapsed: float = 0,
            usage: Usage | None = None,
            ended: datetime | None = None,
        ) -> None:
            now = datetime.now(UTC)
            on_event(
                ExecutionEventData(
                    kind,
                    sanitizer.object(payload),
                    began or now,
                    ended or now,
                    elapsed,
                    usage or Usage(),
                )
            )

        def finish(output: str | None = None, error_type: str | None = None) -> Outcome:
            error = ERROR_MESSAGES[error_type] if error_type else None
            if error_type:
                emit("error", {"type": error_type, "message": error})
            emit("case_completed", {"status": "error" if error_type else "completed"})
            # Missing usage stays unknown. Never fabricate counts from text length.
            totals: dict[str, int | None] = {}
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                values = [getattr(usage, field) for usage in usages]
                totals[field] = (
                    sum(values) if values and all(v is not None for v in values) else None
                )
            return Outcome(
                sanitizer.text(output) if output is not None else None,
                error_type,
                error,
                Usage.model_validate(totals),
                started_at,
                datetime.now(UTC),
                (perf_counter() - start) * 1000,
            )

        emit("case_started", {"input": request.input})
        for step in range(max_steps):
            emit(
                "model_request",
                {
                    "model": request.model,
                    "step": step,
                    "input": request.input,
                    "messages": [message.model_dump(mode="json") for message in messages],
                    "tools": [tool.name for tool in self.registry.definitions],
                },
            )
            began, timer = datetime.now(UTC), perf_counter()
            try:
                raw_response = self.provider.complete(
                    request.model_copy(
                        update={
                            "messages": tuple(messages),
                            "tools": self.registry.definitions,
                        }
                    )
                )
                response = ModelResponse.model_validate(raw_response.model_dump(mode="json"))
            except TimeoutError:
                emit(
                    "model_error",
                    {"type": "provider_timeout"},
                    began,
                    (perf_counter() - timer) * 1000,
                )
                return finish(error_type="provider_timeout")
            except Exception:
                emit(
                    "model_error",
                    {"type": "provider_error"},
                    began,
                    (perf_counter() - timer) * 1000,
                )
                return finish(error_type="provider_error")
            usages.append(response.usage)
            emit(
                "model_response",
                response.model_dump(mode="json"),
                began,
                (perf_counter() - timer) * 1000,
                response.usage,
            )
            if response.text is not None:
                return finish(output=response.text)
            call = response.tool_call
            assert call is not None
            emit(
                "tool_request",
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments},
            )
            began, timer = datetime.now(UTC), perf_counter()
            arguments: dict[str, JsonValue] = {}
            validated = False
            output: JsonValue = None
            error_type = None
            try:
                invocation = self.registry.prepare(call)
                arguments = invocation.validated_arguments
                validated = True
                output = invocation.execute()
            except UnknownTool:
                error_type = "unknown_tool"
            except InvalidToolArguments:
                error_type = "invalid_tool_arguments"
            except Exception:
                error_type = "tool_error"
            finished_at, elapsed = datetime.now(UTC), (perf_counter() - timer) * 1000
            safe_output = sanitizer.clean(output)
            on_tool(
                ToolObservation(
                    tool_count,
                    sanitizer.text(call.call_id)[:200] or "[REDACTED]",
                    sanitizer.text(call.name)[:200] or "[REDACTED]",
                    sanitizer.object(arguments),
                    validated,
                    safe_output,
                    began,
                    finished_at,
                    elapsed,
                    error_type,
                    ERROR_MESSAGES[error_type] if error_type else None,
                )
            )
            tool_count += 1
            emit(
                "tool_result",
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments_validated": validated,
                    "output": safe_output,
                    "error_type": error_type,
                },
                began,
                elapsed,
                ended=finished_at,
            )
            if error_type:
                return finish(error_type=error_type)
            messages.extend(
                (
                    Message(role="assistant", content=call.model_dump(mode="json")),
                    Message(role="tool", content=output, call_id=call.call_id),
                )
            )
        return finish(error_type="step_limit")
