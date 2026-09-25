"""Conservative trace projection, not a general-purpose secret detector."""

import math
import re

from pydantic import JsonValue

OMIT = {
    "apikey",
    "secret",
    "password",
    "authorization",
    "credentials",
    "accesstoken",
    "refreshtoken",
    "token",
    "reasoning",
    "internalreasoning",
    "chainofthought",
    "reasoningcontent",
    "thinking",
    "analysis",
    "providerinternal",
    "rawresponse",
}


def protected_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in OMIT or normalized.endswith(("apikey", "password", "secret"))


class TraceSanitizer:
    def __init__(self, *sources: JsonValue) -> None:
        self._values: set[str] = set()
        for source in sources:
            self._collect(source)

    def _collect(self, value: JsonValue, protected: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                self._collect(item, protected or protected_key(key))
        elif isinstance(value, list):
            for item in value:
                self._collect(item, protected)
        elif isinstance(value, str) and value:
            if protected:
                self._values.add(value)
            for match in re.finditer(
                r"(?i)\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*([^\s,;]+)", value
            ):
                self._values.add(match.group(1))
            for match in re.finditer(r"(?i)\bBearer\s+([^\s,;]+)", value):
                self._values.add(match.group(1))

    def text(self, value: str) -> str:
        for secret in sorted(self._values, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", value, flags=re.S | re.I)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "[REDACTED]", value)
        value = re.sub(
            r"(?i)\b(api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+", "[REDACTED]", value
        )
        return value.replace("\x00", "").encode("utf-8", errors="replace").decode()

    def clean(self, value: JsonValue) -> JsonValue:
        self._collect(value)
        return self._clean(value)

    def _clean(self, value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            return {
                self.text(key): self._clean(item)
                for key, item in value.items()
                if not protected_key(key)
            }
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    def object(self, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        cleaned = self.clean(value)
        assert isinstance(cleaned, dict)
        return cleaned
