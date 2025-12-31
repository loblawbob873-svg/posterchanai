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
                    thinking_done = False
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
                                        # Check if we're past thinking tags
                                        if not thinking_done:
                                            match = re.search(r'</think(?:ing)?>', buffer, re.IGNORECASE)
                                            if match:
                                                thinking_done = True
                                                # Yield everything after the closing tag
                                                after_think = buffer[match.end():]
                                                if after_think:
                                                    yield after_think
                                                buffer = after_think
                                            # Don't yield if we're still in thinking mode
                                        else:
                                            yield content
                            except json.JSONDecodeError:
                                continue
            except httpx.HTTPStatusError as e:
                yield f"Error: API returned status {e.response.status_code}"
            except Exception as e:
                yield f"Error: {str(e)}"


    async def modify_prompt_for_img2img(self, user_prompt: str) -> tuple[str, float, str]:
        """
        Use AI to create optimized img2img parameters from user's request.
        Returns: (prompt, denoise, negative_prompt)
        """
        if not self.openwebui_url or not self.api_key:
            # Fallback: use user prompt directly with high denoise
            return user_prompt + ", vibrant colors, sharp, high quality", 1.0, "bad quality, blurry, distorted"

        system_prompt = """Output EXACTLY 3 lines. NO explanations. ONLY tags.
DENOISE: <number>
TAGS: <tags>
NEGATIVE: <tags>

DENOISE values:
- 1.0 = completely new image from prompt (ignore source image structure)
- 0.80 = color changes (hair, eyes, skin), clothing removal (naked/nude)
- 0.75 = background/scene changes
- 0.70 = object changes (holding different items)
- 0.65 = style changes (anime, realistic)
- 0.50 = body modifications (breast size)
- 0.20 = minor changes (accessories)

RULES:
1. Output ONLY the 3 lines above, nothing else
2. Hair/eye color changes: weight 2.0, add TWICE
3. Skin color changes: weight 2.0, add synonyms
4. Small breasts: weight 3.0, add to NEGATIVE: large breasts, cleavage
5. Big breasts: weight 3.0, add cleavage, NEGATIVE: small breasts, flat chest
6. Naked/nude: weight 3.0, add clothing to NEGATIVE
7. Object changes: weight 2.5, put original object in NEGATIVE
8. Always end TAGS with "vibrant colors, sharp, high quality"
9. NEVER put the same tag in both TAGS and NEGATIVE
10. Always add to NEGATIVE: "deformed, extra limbs, bad anatomy, blurry, distorted"

Examples:
User wants: "red hair girl"
DENOISE: 0.80
TAGS: 1girl, (red hair:2.0), red hair, vibrant colors, sharp, high quality
NEGATIVE: deformed, extra limbs, bad anatomy, blurry, distorted

User wants: "anime style beach scene"
DENOISE: 0.75
TAGS: (anime:1.5), (beach:1.5), ocean, sand, sunny, vibrant colors, sharp, high quality
NEGATIVE: deformed, extra limbs, bad anatomy, blurry, distorted

User wants: "naked woman"
DENOISE: 0.80
TAGS: 1girl, (naked:3.0), (nude:2.5), bare skin, vibrant colors, sharp, high quality
NEGATIVE: clothing, clothed, shirt, dress, deformed, extra limbs, bad anatomy, blurry, distorted

User wants: "cyberpunk city at night"
DENOISE: 1.0
TAGS: (cyberpunk:1.5), city, night, neon lights, futuristic, vibrant colors, sharp, high quality
NEGATIVE: daytime, rural, deformed, blurry, distorted"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User wants: \"{user_prompt}\""}
        ]

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
                        "temperature": 0.2,
                        "max_tokens": 400,
                        "stream": False
                    }
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]

                # Strip thinking tags
                content = self.strip_thinking_tags(content)

                # Parse response
                denoise = 1.0
                tags = user_prompt
                negative = "bad quality, blurry, distorted, deformed"

                denoise_match = re.search(r'DENOISE:\s*([\d.]+)', content)
                tags_match = re.search(r'TAGS:\s*(.+?)(?:\n|$)', content)
                negative_match = re.search(r'NEGATIVE:\s*(.+?)(?:\n|$)', content)

                if denoise_match:
                    try:
                        denoise = float(denoise_match.group(1))
                        denoise = max(0.20, min(1.0, denoise))
                    except ValueError:
                        pass

                if tags_match:
                    tags = tags_match.group(1).strip().strip('"').strip("'")

                if negative_match:
                    negative = negative_match.group(1).strip().strip('"').strip("'")

                print(f"[IMG2IMG] Denoise: {denoise}, Tags: {tags[:80]}...")
                print(f"[IMG2IMG] Negative: {negative[:80]}...")
                return tags, denoise, negative

            except Exception as e:
                print(f"[IMG2IMG] Prompt modification failed: {e}")
                return user_prompt + ", vibrant colors, sharp, high quality", 1.0, "bad quality, blurry, distorted"


def get_chat_service(db: Session) -> ChatService:
    return ChatService(db)
