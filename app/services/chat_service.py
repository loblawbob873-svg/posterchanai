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
- 0.85 = hair STYLE changes (afro, ponytail, straight, curly, short, long), ANIMAL changes (pig to cat, dog to wolf)
- 0.80 = color changes (hair, eyes, skin), clothing removal (naked/nude)
- 0.75 = background/scene changes
- 0.70 = object changes (holding different items)
- 0.65 = art style changes (anime, realistic)
- 0.50 = body modifications (breast size)
- 0.20 = minor changes (accessories)

RULES:
1. Output ONLY the 3 lines above, nothing else
2. Hair/eye color changes: weight 2.0, add TWICE
2b. Hair STYLE changes (afro, ponytail, etc.): weight 2.5, DENOISE 0.85, put original style in NEGATIVE
2c. ANIMAL changes (pig to cat, etc.): weight 2.5, DENOISE 0.85, put original animal in NEGATIVE, keep background
3. Skin color changes: weight 2.0, add synonyms (dark skin, black skin for dark; pale skin, white skin, fair skin for light)
4. Small breasts: weight 3.0, REMOVE cleavage/large breasts/lingerie/corset from TAGS, add NEGATIVE
5. Big breasts: weight 3.0, add cleavage, NEGATIVE should be "small breasts, flat chest, petite" ONLY
6. Naked/nude: add (nude:2.0), (naked:2.0) to TAGS, REMOVE ALL clothing tags from TAGS, put clothing in NEGATIVE. KEEP original skin color - nude does NOT mean white skin!
7. Object changes (holding items): weight 2.5, REMOVE original object tags (racket, ball, phone, etc.) from TAGS, add new object, put original object in NEGATIVE
8. Keep original tags for people count (2girls, multiple girls, etc)
9. Always end TAGS with "vibrant colors, sharp, high quality"
10. NEVER put the same tag in both TAGS and NEGATIVE - they must be opposites
11. NEVER put character features in NEGATIVE (hair color, eye color, face features, body type) - only put things you want to REMOVE like clothing or objects
12. Combined changes (e.g., "nude big breasts", "white skin nude"): use DENOISE 0.65, apply ALL requested changes - remove clothing for nude, add breast tags for breasts, change colors as requested
13. CRITICAL: Keep background/setting tags IN TAGS (indoors, outdoors, beach, city, mcdonalds, etc.) - do NOT put them in NEGATIVE unless user asks to change background
14. CRITICAL: Keep ALL original character features (hair color, eye color, accessories) IN TAGS - only remove what user specifically asks to change
15. For anime: use weight 2.0 (NOT 3.0) for nude/body changes - anime models are sensitive to high weights
16. For multiple people (2girls, 3girls): keep exact count, avoid generating extra people
17. Always add to NEGATIVE: "deformed, extra limbs, bad anatomy, blurry, distorted, extra people"
18. PRESERVE original clothing tags (shirt, dress, uniform, skirt, etc.) IN TAGS unless user asks to change/remove clothing (nude, naked, different outfit)
19. Multi-attribute changes (skin + hair + style): use DENOISE 0.80, weight 2.0 for each change, KEEP ALL original tags not being changed in TAGS
20. Style change with color changes: apply style tag with weight 1.5, color tags with weight 2.0, use higher DENOISE (0.80) to allow more change
21. CRITICAL: Only put in NEGATIVE what you are REPLACING - do NOT put clothing, accessories, or features in NEGATIVE unless user asked to change them
22. CRITICAL: COPY most original tags to TAGS - only modify/remove the specific attributes user asked to change

Examples:
Tags: "1girl, blonde hair, blue eyes, red dress" Change: "red hair"
DENOISE: 0.80
TAGS: 1girl, (red hair:2.0), red hair, blue eyes, red dress, vibrant colors, sharp, high quality
NEGATIVE: blonde hair, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, straight hair, twintails, silver hair" Change: "afro"
DENOISE: 0.85
TAGS: 1girl, (afro:2.5), (afro hair:2.5), curly hair, silver hair, vibrant colors, sharp, high quality
NEGATIVE: straight hair, twintails, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, orange hair, yellow eyes, black hoodie, stars, white background, anime" Change: "brown skin"
DENOISE: 0.80
TAGS: 1girl, (brown skin:2.0), (dark skin:2.0), brown skin, orange hair, yellow eyes, black hoodie, stars, anime, vibrant colors, sharp, high quality
NEGATIVE: light skin, pale skin, fair skin, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, dark skin, black hair" Change: "white skin"
DENOISE: 0.80
TAGS: 1girl, (pale skin:2.0), (white skin:2.0), (fair skin:2.0), pale skin, white skin, black hair, vibrant colors, sharp, high quality
NEGATIVE: dark skin, black skin, tan skin, brown skin, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, dark skin, black hair, realistic, dress, smile" Change: "white skin blonde hair anime"
DENOISE: 0.80
TAGS: 1girl, (pale skin:2.0), (white skin:2.0), (blonde hair:2.0), blonde hair, (anime:1.5), anime style, dress, smile, vibrant colors, sharp, high quality
NEGATIVE: dark skin, black skin, tan skin, black hair, realistic, photorealistic, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, solo, dark skin, blue hair, yellow eyes, cat ears, dress, skirt, thighhighs" Change: "white skin blonde hair"
DENOISE: 0.80
TAGS: 1girl, solo, (pale skin:2.0), (white skin:2.0), (blonde hair:2.0), blonde hair, yellow eyes, cat ears, dress, skirt, thighhighs, vibrant colors, sharp, high quality
NEGATIVE: dark skin, tan skin, brown skin, blue hair, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, brown skin, brown hair, brown eyes" Change: "pale skin red hair green eyes"
DENOISE: 0.80
TAGS: 1girl, (pale skin:2.0), (white skin:2.0), (red hair:2.0), red hair, (green eyes:2.0), green eyes, vibrant colors, sharp, high quality
NEGATIVE: brown skin, tan skin, dark skin, brown hair, brown eyes, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "3girls, anime, purple hair, green hair, black hair, red shirt, uniform, mcdonalds, indoors" Change: "nude"
DENOISE: 0.80
TAGS: 3girls, anime, purple hair, green hair, black hair, (naked:2.0), (nude:2.0), mcdonalds, indoors, multiple girls, vibrant colors, sharp, high quality
NEGATIVE: red shirt, uniform, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, dark skin, pink hair, orange eyes, school uniform, skirt, thigh highs, anime" Change: "nude"
DENOISE: 0.80
TAGS: 1girl, dark skin, pink hair, orange eyes, (naked:2.0), (nude:2.0), anime, vibrant colors, sharp, high quality
NEGATIVE: school uniform, skirt, thigh highs, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted

Tags: "1girl, dark skin, pink hair, orange eyes, school uniform, skirt, anime" Change: "white skin blonde hair nude"
DENOISE: 0.65
TAGS: 1girl, (pale skin:2.0), (white skin:2.0), (blonde hair:2.0), blonde hair, orange eyes, (naked:2.0), (nude:2.0), anime, vibrant colors, sharp, high quality
NEGATIVE: dark skin, tan skin, pink hair, school uniform, skirt, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted

Tags: "1girl, blonde hair, blue eyes, grey top, large breasts" Change: "nude small breasts"
DENOISE: 0.65
TAGS: 1girl, blonde hair, blue eyes, (naked:2.0), (nude:2.0), (small breasts:2.0), natural skin, realistic skin tone, vibrant colors, sharp, high quality
NEGATIVE: grey top, clothing, clothed, large breasts, big breasts, pale skin, washed out, desaturated, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, holding tennis racket, sportswear" Change: "holding gun"
DENOISE: 0.70
TAGS: 1girl, sportswear, (holding gun:2.5), (pistol:2.0), holding weapon, vibrant colors, sharp, high quality
NEGATIVE: tennis racket, racket, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "pig, barn, fireworks, night sky" Change: "cat"
DENOISE: 0.85
TAGS: (cat:2.5), (feline:2.0), barn, fireworks, night sky, vibrant colors, sharp, high quality
NEGATIVE: pig, swine, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, blonde hair, green eyes, red dress, cosplay, realistic" Change: "black hair"
DENOISE: 0.80
TAGS: 1girl, (black hair:2.0), black hair, green eyes, red dress, cosplay, realistic, vibrant colors, sharp, high quality
NEGATIVE: blonde hair, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, dark skin, gray dress, realistic" Change: "white skin"
DENOISE: 0.80
TAGS: 1girl, (pale skin:2.0), (white skin:2.0), (fair skin:2.0), gray dress, realistic, vibrant colors, sharp, high quality
NEGATIVE: dark skin, tan skin, brown skin, black skin, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, dark skin, gray dress, realistic" Change: "nude"
DENOISE: 0.80
TAGS: 1girl, dark skin, (nude:2.0), (naked:2.0), realistic, vibrant colors, sharp, high quality
NEGATIVE: gray dress, clothing, clothed, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, silver hair, twintails, elf ears, green eyes, red earrings, anime" Change: "dark skin"
DENOISE: 0.80
TAGS: 1girl, (dark skin:2.0), (brown skin:2.0), silver hair, twintails, elf ears, green eyes, red earrings, anime, vibrant colors, sharp, high quality
NEGATIVE: light skin, pale skin, white skin, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, purple hair, white dress, venice, water, buildings" Change: "beach"
DENOISE: 0.75
TAGS: 1girl, purple hair, white dress, (beach:2.0), (ocean:1.5), sand, sunny, vibrant colors, sharp, high quality
NEGATIVE: venice, buildings, canal, deformed, extra limbs, bad anatomy, blurry

Tags: "1boy 1girl, blonde hair, black hair, school uniform, blue sky, anime" Change: "red hair"
DENOISE: 0.80
TAGS: 1boy 1girl, (red hair:2.0), red hair, black hair, school uniform, blue sky, anime, vibrant colors, sharp, high quality
NEGATIVE: blonde hair, deformed, extra limbs, bad anatomy, blurry

Tags: "4girls, blonde hair, red uniform, hats, eating, night, anime" Change: "dark skin"
DENOISE: 0.80
TAGS: 4girls, (dark skin:2.0), (brown skin:2.0), blonde hair, red uniform, hats, eating, night, anime, vibrant colors, sharp, high quality
NEGATIVE: light skin, pale skin, white skin, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, brown hair, blue eyes, santa outfit, christmas tree, anime" Change: "blonde hair"
DENOISE: 0.80
TAGS: 1girl, (blonde hair:2.0), blonde hair, blue eyes, santa outfit, christmas tree, anime, vibrant colors, sharp, high quality
NEGATIVE: brown hair, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, orange hair, blue eyes, santa costume, realistic" Change: "nude"
DENOISE: 0.80
TAGS: 1girl, orange hair, blue eyes, (nude:2.0), (naked:2.0), realistic, vibrant colors, sharp, high quality
NEGATIVE: santa costume, clothing, clothed, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, black hair, blue eyes, fox ears, fox tail, brown sweater, skirt, anime" Change: "blonde hair"
DENOISE: 0.80
TAGS: 1girl, (blonde hair:2.0), blonde hair, blue eyes, fox ears, fox tail, brown sweater, skirt, anime, vibrant colors, sharp, high quality
NEGATIVE: black hair, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, black hair, fox ears, fox tail, sweater, skirt, anime" Change: "nude"
DENOISE: 0.80
TAGS: 1girl, black hair, fox ears, fox tail, (nude:2.0), (naked:2.0), anime, vibrant colors, sharp, high quality
NEGATIVE: sweater, skirt, clothing, clothed, deformed, extra limbs, bad anatomy, blurry

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
