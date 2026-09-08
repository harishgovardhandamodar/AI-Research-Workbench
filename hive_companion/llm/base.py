from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Iterator


@dataclass
class ChatMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class ChatResponse:
    content: str
    model: str
    provider: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMProvider:
    """Minimal sync interface — streaming built on top."""

    name: str

    def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        raise NotImplementedError

    def chat_stream(self, messages: list[ChatMessage], **kwargs) -> Iterator[str]:
        # default: non-streaming fallback
        resp = self.chat(messages, **kwargs)
        yield resp.content

    def list_models(self) -> list[str]:
        return []

    def health(self) -> tuple[bool, str]:
        try:
            self.list_models()
            return True, "ok"
        except Exception as e:
            return False, str(e)
