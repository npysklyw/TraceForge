from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Count = Annotated[int, Field(ge=0, strict=True)]


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    total_tokens: Count | None = None


class ToolRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    call_id: Annotated[str, Field(min_length=1, max_length=200)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    arguments: dict[str, JsonValue]


class ModelResponse(BaseModel):
    """Only public output is representable; vendor internals are discarded."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    text: str | None = None
    tool_call: ToolRequest | None = None
    usage: Usage = Field(default_factory=Usage)

    @model_validator(mode="after")
    def one_action(self) -> Self:
        if (self.text is None) == (self.tool_call is None):
            raise ValueError("A response must contain exactly one final text or tool call")
        return self


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: Literal["assistant", "tool"]
    content: JsonValue
    call_id: str | None = None


class ToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    description: str
    arguments_schema: dict[str, JsonValue]


class ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str
    system_prompt: str
    parameters: dict[str, JsonValue]
    input: dict[str, JsonValue]
    messages: tuple[Message, ...] = ()
    tools: tuple[ToolDefinition, ...] = ()


class ProviderError(Exception):
    """Provider failure; raw exception text is never included in a trace."""


class ModelProvider(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...
