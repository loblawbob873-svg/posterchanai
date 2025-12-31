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


    async def analyze_image_tags(self, image_base64: str) -> str:
        """
        Use vision AI to analyze image and extract tags describing it.
        Returns comma-separated tags or empty string on failure.
        """
        if not self.openwebui_url or not self.api_key:
            return ""

        system_prompt = """Analyze this image and output ONLY comma-separated tags describing it.
Include: character count (1girl, 2girls, 1boy, etc.), hair color, eye color, clothing, accessories, background/setting, art style (anime, realistic, etc.), pose, expression.
Output ONLY tags, no sentences. Example: 1girl, orange hair, yellow eyes, black hoodie, stars pattern, white background, anime style, upper body"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": "Describe this image with tags:"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
            ]}
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
                        "temperature": 0.3,
                        "max_tokens": 200,
                        "stream": False
                    }
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                tags = self.strip_thinking_tags(content).strip()
                print(f"[VISION] Analyzed image tags: {tags[:100]}...")
                return tags
            except Exception as e:
                print(f"[VISION] Image analysis failed: {e}")
                return ""

    async def modify_prompt_for_img2img(self, user_prompt: str, original_tags: str = "") -> tuple[str, float, str]:
        """
        Use AI to create optimized img2img parameters from user's request.
        original_tags: tags describing the source image (from vision analysis)
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
3. Skin color changes: weight 2.0, add synonyms (dark skin, black skin for dark; pale skin, fair skin for light)
4. Small breasts: weight 3.0, REMOVE cleavage/large breasts/lingerie/corset from TAGS, add NEGATIVE
5. Big breasts: weight 3.0, add cleavage, NEGATIVE should be "small breasts, flat chest, petite" ONLY
6. Naked/nude: weight 3.0, REMOVE ALL clothing tags (shirt, skirt, dress, bra, underwear, sportswear, uniform, etc.) from TAGS, add clothing to NEGATIVE
7. Object changes (holding items): weight 2.5, REMOVE original object tags (racket, ball, phone, etc.) from TAGS, add new object, put original object in NEGATIVE
8. Keep original tags for people count (2girls, multiple girls, etc)
9. Always end TAGS with "vibrant colors, sharp, high quality"
10. NEVER put the same tag in both TAGS and NEGATIVE - they must be opposites
11. NEVER put character features in NEGATIVE (hair color, eye color, face features, body type) - only put things you want to REMOVE like clothing or objects
12. Combined changes (e.g., "nude small breasts"): use DENOISE 0.65, weight 2.0, add "natural skin, realistic skin tone" to TAGS, add "pale skin, washed out, desaturated" to NEGATIVE
13. CRITICAL: Keep background/setting tags IN TAGS (indoors, outdoors, beach, city, mcdonalds, etc.) - do NOT put them in NEGATIVE unless user asks to change background
14. CRITICAL: Keep ALL original character features (hair color, eye color, accessories) IN TAGS - only remove what user specifically asks to change
15. For anime: use weight 2.0 (NOT 3.0) for nude/body changes - anime models are sensitive to high weights
16. For multiple people (2girls, 3girls): keep exact count, avoid generating extra people
17. Always add to NEGATIVE: "deformed, extra limbs, bad anatomy, blurry, distorted, extra people"
18. PRESERVE original clothing tags (shirt, dress, uniform, skirt, etc.) IN TAGS unless user asks to change/remove clothing (nude, naked, different outfit)

Examples:
Tags: "1girl, blonde hair, blue eyes, red dress" Change: "red hair"
DENOISE: 0.80
TAGS: 1girl, (red hair:2.0), red hair, blue eyes, red dress, vibrant colors, sharp, high quality
NEGATIVE: blonde hair, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, orange hair, yellow eyes, black hoodie, stars, white background, anime" Change: "brown skin"
DENOISE: 0.80
TAGS: 1girl, (brown skin:2.0), (dark skin:2.0), brown skin, orange hair, yellow eyes, black hoodie, stars, anime, vibrant colors, sharp, high quality
NEGATIVE: light skin, pale skin, fair skin, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "3girls, anime, purple hair, green hair, black hair, red shirt, uniform, mcdonalds, indoors" Change: "nude"
DENOISE: 0.80
TAGS: 3girls, anime, purple hair, green hair, black hair, (naked:2.0), (nude:2.0), mcdonalds, indoors, multiple girls, vibrant colors, sharp, high quality
NEGATIVE: red shirt, uniform, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, blonde hair, blue eyes, grey top, large breasts" Change: "nude small breasts"
DENOISE: 0.65
TAGS: 1girl, blonde hair, blue eyes, (naked:2.0), (nude:2.0), (small breasts:2.0), natural skin, realistic skin tone, vibrant colors, sharp, high quality
NEGATIVE: grey top, clothing, clothed, large breasts, big breasts, pale skin, washed out, desaturated, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, holding tennis racket, sportswear" Change: "holding gun"
DENOISE: 0.70
TAGS: 1girl, sportswear, (holding gun:2.5), (pistol:2.0), holding weapon, vibrant colors, sharp, high quality
NEGATIVE: tennis racket, racket, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

User wants: "cyberpunk city at night"
DENOISE: 1.0
TAGS: (cyberpunk:1.5), city, night, neon lights, futuristic, vibrant colors, sharp, high quality
NEGATIVE: daytime, rural, deformed, blurry, distorted, extra people"""

        # Format message based on whether we have original tags
        if original_tags:
            user_message = f'Tags: "{original_tags}" Change: "{user_prompt}"'
        else:
            user_message = f'User wants: "{user_prompt}"'

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
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
