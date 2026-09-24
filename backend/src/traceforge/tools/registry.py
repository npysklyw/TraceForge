from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from traceforge.providers.base import ToolDefinition, ToolRequest

json_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class UnknownTool(Exception):
    pass


class InvalidToolArguments(Exception):
    pass


@dataclass(frozen=True)
class ToolInvocation:
    request: ToolRequest
    validated_arguments: dict[str, JsonValue]
    execute: Callable[[], JsonValue]


class RegisteredTool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def prepare(self, request: ToolRequest) -> ToolInvocation: ...


@dataclass(frozen=True)
class Tool[T: BaseModel]:
    name: str
    description: str
    arguments_type: type[T]
    handler: Callable[[T], JsonValue]

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition.model_validate(
            {
                "name": self.name,
                "description": self.description,
                "arguments_schema": self.arguments_type.model_json_schema(),
            }
        )

    def prepare(self, request: ToolRequest) -> ToolInvocation:
        try:
            arguments = self.arguments_type.model_validate(request.arguments)
        except ValidationError as exc:
            raise InvalidToolArguments from exc
        return ToolInvocation(
            request,
            arguments.model_dump(mode="json"),
            lambda: json_adapter.validate_python(self.handler(arguments)),
        )


class ToolRegistry:
    def __init__(self, tools: tuple[RegisteredTool, ...]) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Tool names must be unique")

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def prepare(self, request: ToolRequest) -> ToolInvocation:
        tool = self._tools.get(request.name)
        if tool is None:
            raise UnknownTool
        return tool.prepare(request)
