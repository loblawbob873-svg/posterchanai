import httpx
import json
import re
from typing import AsyncGenerator, Optional
from sqlalchemy.orm import Session
from app.models import Setting


class ChatService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.openwebui_url = settings.get("openwebui_url", "")
        self.api_key = settings.get("openwebui_api_key", "")
        self.model = settings.get("openwebui_model", "")
        self.timeout = int(settings.get("openwebui_timeout", "60000")) / 1000  # Convert to seconds

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        matches = list(re.finditer(r'</think(?:ing)?>', response, re.IGNORECASE))
        if matches:
            last_match = matches[-1]
            return response[last_match.end():].strip()
        return response

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat completion"""
        if not self.openwebui_url or not self.api_key:
            return "Error: OpenWebUI not configured. Please ask an admin to configure it."

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.openwebui_url}/api/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False
                    }
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return self.strip_thinking_tags(content)
            except httpx.HTTPStatusError as e:
                return f"Error: API returned status {e.response.status_code}"
            except Exception as e:
                return f"Error: {str(e)}"

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """Streaming chat completion"""
        if not self.openwebui_url or not self.api_key:
            yield "Error: OpenWebUI not configured. Please ask an admin to configure it."
            return

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{self.openwebui_url}/api/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": True
                    }
                ) as response:
                    response.raise_for_status()
                    buffer = ""
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                if "choices" in data and len(data["choices"]) > 0:
                                    delta = data["choices"][0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        buffer += content
                                        # Check for thinking tags and strip them
                                        clean = self.strip_thinking_tags(buffer)
                                        if clean != buffer:
                                            buffer = clean
                                        yield content
                            except json.JSONDecodeError:
                                continue
            except httpx.HTTPStatusError as e:
                yield f"Error: API returned status {e.response.status_code}"
            except Exception as e:
                yield f"Error: {str(e)}"


def get_chat_service(db: Session) -> ChatService:
    return ChatService(db)
