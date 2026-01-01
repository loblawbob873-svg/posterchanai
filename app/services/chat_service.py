import httpx
import json
import re
import asyncio
import base64
from typing import AsyncGenerator, Optional
from sqlalchemy.orm import Session
from app.models import Setting

# Import WD14 tagger from posterchan's comfyui module
import sys
sys.path.insert(0, '/home/verita84/posterchan')
from comfyui import describe_image_with_wd14


class ChatService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        # Use Ollama directly
        self.ollama_url = settings.get("ollama_url", "http://localhost:11434")
        self.model = settings.get("ollama_model", "llama3")
        self.timeout = int(settings.get("ollama_timeout", "120000")) / 1000  # Convert to seconds
        self.system_prompt = settings.get("ollama_system_prompt", "You are a helpful, friendly AI assistant.")

        # Advanced model settings
        self.temperature = float(settings.get("ollama_temperature", "0.7"))
        self.top_p = float(settings.get("ollama_top_p", "0.9"))
        self.top_k = int(settings.get("ollama_top_k", "40"))
        self.repeat_penalty = float(settings.get("ollama_repeat_penalty", "1.1"))
        self.num_ctx = int(settings.get("ollama_num_ctx", "4096"))
        self.num_predict = int(settings.get("ollama_num_predict", "2048"))
        # keep_alive: -1 = forever, 0 = unload immediately, positive = seconds
        keep_alive_str = settings.get("ollama_keep_alive", "-1")
        self.keep_alive = int(keep_alive_str) if keep_alive_str.lstrip('-').isdigit() else -1
        self.stop_sequences = [s.strip() for s in settings.get("ollama_stop", "").split(",") if s.strip()]

        # Additional advanced settings (consistent with OllamaService)
        seed_str = settings.get("ollama_seed", "")
        self.seed = int(seed_str) if seed_str.strip() else None
        self.mirostat = int(settings.get("ollama_mirostat", "0"))
        self.mirostat_eta = float(settings.get("ollama_mirostat_eta", "0.1"))
        self.mirostat_tau = float(settings.get("ollama_mirostat_tau", "5.0"))
        self.tfs_z = float(settings.get("ollama_tfs_z", "1.0"))

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        matches = list(re.finditer(r'</think(?:ing)?>', response, re.IGNORECASE))
        if matches:
            last_match = matches[-1]
            return response[last_match.end():].strip()
        return response

    def _get_options(self) -> dict:
        """Get Ollama model options (consistent with OllamaService)"""
        options = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repeat_penalty": self.repeat_penalty,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
            "mirostat": self.mirostat,
            "mirostat_eta": self.mirostat_eta,
            "mirostat_tau": self.mirostat_tau,
            "tfs_z": self.tfs_z,
        }

        # Add seed if set
        if self.seed is not None:
            options["seed"] = self.seed

        # Add stop sequences if set
        if self.stop_sequences:
            options["stop"] = self.stop_sequences

        return options

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat completion using native Ollama API"""
        if not self.ollama_url:
            return "Error: Ollama not configured. Please ask an admin to configure it."

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.ollama_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "options": self._get_options(),
                        "keep_alive": self.keep_alive
                    }
                )
                response.raise_for_status()
                data = response.json()
                content = data["message"]["content"]
                return self.strip_thinking_tags(content)
            except httpx.HTTPStatusError as e:
                return f"Error: API returned status {e.response.status_code}"
            except Exception as e:
                return f"Error: {str(e)}"

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """Streaming chat completion using native Ollama API"""
        if not self.ollama_url:
            yield "Error: Ollama not configured. Please ask an admin to configure it."
            return

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{self.ollama_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": True,
                        "options": self._get_options(),
                        "keep_alive": self.keep_alive
                    }
                ) as response:
                    response.raise_for_status()
                    buffer = ""
                    thinking_done = False
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            content = data.get("message", {}).get("content", "")
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
                            # Check if done
                            if data.get("done", False):
                                break
                        except json.JSONDecodeError:
                            continue
            except httpx.HTTPStatusError as e:
                yield f"Error: API returned status {e.response.status_code}"
            except Exception as e:
                yield f"Error: {str(e)}"


    async def analyze_image_tags(self, image_base64: str) -> str:
        """
        Use WD14 Tagger in ComfyUI to analyze image and extract tags.
        Returns comma-separated tags or empty string on failure.
        """
        try:
            # Decode base64 to bytes
            image_bytes = base64.b64decode(image_base64)

            # Run WD14 tagger in thread pool (it's synchronous)
            loop = asyncio.get_event_loop()
            tags = await loop.run_in_executor(None, describe_image_with_wd14, image_bytes)

            if tags:
                print(f"[WD14] Analyzed image tags: {tags[:100]}...")
                return tags
            else:
                print("[WD14] No tags returned")
                return ""
        except Exception as e:
            print(f"[WD14] Image analysis failed: {e}")
            import traceback
            traceback.print_exc()
            return ""

    async def modify_prompt_for_img2img(self, user_prompt: str, original_tags: str = "") -> tuple[str, float, str]:
        """
        Use AI to create optimized img2img parameters from user's request.
        original_tags: tags describing the source image (from vision analysis)
        Returns: (prompt, denoise, negative_prompt)
        """
        if not self.ollama_url:
            # Fallback: use user prompt directly with high denoise
            return user_prompt + ", vibrant colors, sharp, high quality", 1.0, "bad quality, blurry, distorted"

        system_prompt = """Output EXACTLY 3 lines. NO explanations. ONLY tags.
DENOISE: <number>
TAGS: <tags>
NEGATIVE: <tags>

DENOISE values:
- 0.85 = hair STYLE changes (afro, ponytail, straight, curly, short, long), ANIMAL changes (pig to cat, dog to wolf)
- 0.80 = color changes (hair, eyes, skin), clothing removal (naked/nude)
- 0.75 = background/scene changes
- 0.70 = object changes (holding different items)
- 0.65 = art style changes (anime, realistic)
- 0.50 = body modifications (breast size)
- 0.20 = minor changes (accessories)

RULES:
1. Output ONLY the 3 lines above, nothing else
1b. CRITICAL TAG ORDER: Put ALL weighted modification tags (the changes) at the VERY START of TAGS, BEFORE any original tags. This is essential for the model to apply the changes correctly.
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
17. Always add to NEGATIVE: "deformed, extra limbs, bad anatomy, blurry, distorted, extra people, 1other"
23. CRITICAL: REMOVE any stray person tags like "1other", "other", "ambiguous gender" from TAGS - these cause extra people to be generated. Only keep explicit person counts (1girl, 2girls, 1boy, etc.)
18. PRESERVE original clothing tags (shirt, dress, uniform, skirt, etc.) IN TAGS unless user asks to change/remove clothing (nude, naked, different outfit)
19. Multi-attribute changes (skin + hair + style): use DENOISE 0.80, weight 2.0 for each change, KEEP ALL original tags not being changed in TAGS
20. Style change with color changes: apply style tag with weight 1.5, color tags with weight 2.0, use higher DENOISE (0.80) to allow more change
21. CRITICAL: Only put in NEGATIVE what you are REPLACING - do NOT put clothing, accessories, or features in NEGATIVE unless user asked to change them
22. CRITICAL: COPY most original tags to TAGS - only modify/remove the specific attributes user asked to change
24. CLOTHING CHANGES (swimsuit, bikini, dress, etc.): weight 2.0, DENOISE 0.75, REMOVE original clothing/bondage tags from TAGS, add new clothing, put original clothing in NEGATIVE
25. For swimsuit/bikini: add (swimsuit:2.0), (bikini:1.5), optionally add wet, water droplets for beach/pool vibes
26. Bondage/rope to clothing: REMOVE rope, bondage, shibari, restraints from TAGS, add to NEGATIVE, add the new clothing type
27. CRITICAL: For multiple people (2girls, 3girls, etc.) ALWAYS preserve the exact count in TAGS, use lower DENOISE (0.70) to preserve composition
28. CRITICAL: ALWAYS preserve background/setting tags (indoor, outdoor, party, beach, chandelier, etc.) - do NOT change the scene unless explicitly asked
29. EYE COLOR changes: weight 2.0, DENOISE 0.75, add (new_color eyes:2.0), put original eye color in NEGATIVE
30. EXPRESSION changes (smile, angry, crying, blush): weight 1.5, DENOISE 0.65, add expression tag, put opposite expression in NEGATIVE if present
31. CLOTHING COLOR changes (e.g., "blue dress" when wearing red dress): weight 2.0, DENOISE 0.70, KEEP the clothing type, change only the color, add (blue dress:2.0), put "red dress" in NEGATIVE
32. GLASSES: to add glasses use (glasses:2.0), DENOISE 0.60; to remove use DENOISE 0.70 and put "glasses" in NEGATIVE
33. ACCESSORIES (hat, earrings, necklace, choker): weight 1.5, DENOISE 0.60 to add, 0.70 to remove
34. ANIMAL EARS (cat ears, fox ears, bunny ears): weight 2.0, DENOISE 0.70, add (cat ears:2.0), for removal put in NEGATIVE
35. WET/RAIN effect: add (wet:1.5), (wet skin:1.5), (water droplets:1.5), DENOISE 0.65
36. TAN/TAN LINES: weight 2.0, DENOISE 0.70, add (tan:2.0), (tan lines:2.0), (tanned skin:1.5)
37. PREGNANT: weight 2.5, DENOISE 0.65, add (pregnant:2.5), (large belly:2.0), keep all other features
38. BODY TYPE (muscular, thin, chubby): weight 2.0, DENOISE 0.70, add body type tags, put opposite in NEGATIVE
39. FRECKLES: weight 1.5, DENOISE 0.60, add (freckles:1.5)
40. TATTOO: weight 2.0, DENOISE 0.70, add (tattoo:2.0), specify location if given
41. HORNS/WINGS: weight 2.0, DENOISE 0.75, add (horns:2.0) or (wings:2.0), (angel wings:2.0), (demon wings:2.0)
42. BODY PARTS focus (ass, feet, hands, breasts): weight 1.5, DENOISE 0.60, add focus tag like (ass focus:1.5), (feet:1.5), (hand focus:1.5)
43. BREAST SIZE: for "big breasts" add (large breasts:2.0), (huge breasts:1.5), DENOISE 0.50; for "small breasts" add (small breasts:2.0), (flat chest:1.5), put "large breasts" in NEGATIVE
44. HOUSEHOLD ITEMS (chair, couch, bed, table): weight 1.5, DENOISE 0.70, add item tag, these are for adding props to scene
45. SITTING/LYING poses: weight 1.5, DENOISE 0.70, add (sitting:1.5) or (lying down:1.5), (on bed:1.5), (on couch:1.5)
46. SPREAD LEGS/POSES: weight 2.0, DENOISE 0.70, add (spread legs:2.0) or pose description
47. CLOTHING STATE (open shirt, lifted skirt): weight 1.5, DENOISE 0.65, add state tag, keep original clothing in TAGS

Examples:
Tags: "1girl, blonde hair" Change: "red hair"
DENOISE: 0.80
TAGS: 1girl, (red hair:2.0), red hair, vibrant colors, sharp, high quality
NEGATIVE: blonde hair, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, straight hair, twintails, silver hair" Change: "afro"
DENOISE: 0.85
TAGS: 1girl, (afro:2.5), (afro hair:2.5), curly hair, silver hair, vibrant colors, sharp, high quality
NEGATIVE: straight hair, twintails, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, brown hair" Change: "beach"
DENOISE: 0.75
TAGS: 1girl, brown hair, (beach:1.5), vibrant colors, sharp, high quality

Tags: "1girl, brown hair" Change: "anime style"
DENOISE: 0.65
TAGS: 1girl, brown hair, (anime:1.5), vibrant colors, sharp, high quality

Tags: "1girl, white skin, blonde hair" Change: "black skin"
DENOISE: 0.80
TAGS: 1girl, (dark skin:2.0), (black skin:2.0), dark skin, black skin, blonde hair, vibrant colors, sharp, high quality
NEGATIVE: white skin, pale skin, light skin, fair skin

Tags: "1girl, dark skin, black hair" Change: "white skin"
DENOISE: 0.80
TAGS: 1girl, (pale skin:2.0), (white skin:2.0), (fair skin:2.0), pale skin, white skin, black hair, vibrant colors, sharp, high quality
NEGATIVE: dark skin, black skin, tan skin, brown skin

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
NEGATIVE: brown skin, tan skin, dark skin, brown hair, brown eyes

Tags: "1girl, solo, large breasts, cleavage, lingerie, brown hair" Change: "small breasts"
DENOISE: 0.50
TAGS: 1girl, solo, (small breasts:3.0), (flat chest:2.5), (petite:2.0), small breasts, flat chest, brown hair, vibrant colors, sharp, high quality
NEGATIVE: large breasts, huge breasts, big breasts, cleavage, busty, curvy, lingerie, corset, breasts, bra, underwear

Tags: "1girl, small breasts" Change: "bigger chest"
DENOISE: 0.50
TAGS: 1girl, (large breasts:3.0), (huge breasts:2.5), (cleavage:2.0), large breasts, cleavage, vibrant colors, sharp, high quality
NEGATIVE: small breasts, flat chest, petite

Tags: "1girl, blue eyes, horns, choker" Change: "big breasts"
DENOISE: 0.50
TAGS: 1girl, blue eyes, horns, choker, (large breasts:3.0), (huge breasts:2.5), (cleavage:2.0), large breasts, cleavage, vibrant colors, sharp, high quality
NEGATIVE: small breasts, flat chest, petite

Tags: "1girl, blonde hair, shirt, skirt, sportswear, tennis uniform" Change: "naked"
DENOISE: 0.80
TAGS: 1girl, blonde hair, (naked:3.0), (nude:2.5), (bare skin:2.0), naked, nude, vibrant colors, sharp, high quality
NEGATIVE: shirt, skirt, dress, clothing, sportswear, uniform, bra, underwear, clothed

Tags: "1girl, blonde hair, blue eyes, grey top, large breasts" Change: "nude small breasts"
DENOISE: 0.65
TAGS: 1girl, blonde hair, blue eyes, (naked:2.0), (nude:2.0), (small breasts:2.0), natural skin, realistic skin tone, vibrant colors, sharp, high quality
NEGATIVE: grey top, clothing, clothed, large breasts, big breasts, pale skin, washed out, desaturated

Tags: "3girls, anime, purple hair, green hair, black hair, red shirt, uniform, mcdonalds, indoors" Change: "nude"
DENOISE: 0.80
TAGS: (naked:2.0), (nude:2.0), 3girls, anime, purple hair, green hair, black hair, mcdonalds, indoors, multiple girls, vibrant colors, sharp, high quality
NEGATIVE: red shirt, uniform, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, dark skin, pink hair, orange eyes, school uniform, skirt, thigh highs, anime" Change: "nude"
DENOISE: 0.80
TAGS: 1girl, dark skin, pink hair, orange eyes, (naked:2.0), (nude:2.0), anime, vibrant colors, sharp, high quality
NEGATIVE: school uniform, skirt, thigh highs, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted

Tags: "1girl, dark skin, pink hair, orange eyes, school uniform, skirt, anime" Change: "white skin blonde hair nude"
DENOISE: 0.65
TAGS: 1girl, (pale skin:2.0), (white skin:2.0), (blonde hair:2.0), blonde hair, orange eyes, (naked:2.0), (nude:2.0), anime, vibrant colors, sharp, high quality
NEGATIVE: dark skin, tan skin, pink hair, school uniform, skirt, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted

Tags: "1girl, long hair, blonde hair, orange eyes, school uniform, serafuku, skirt, pleated skirt, shirt, cardigan, pantyhose, shoes, loafers, wet clothes" Change: "nude"
DENOISE: 0.80
TAGS: 1girl, long hair, blonde hair, orange eyes, (naked:2.0), (nude:2.0), wet, vibrant colors, sharp, high quality
NEGATIVE: school uniform, serafuku, skirt, pleated skirt, shirt, cardigan, pantyhose, shoes, loafers, wet clothes, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted

Tags: "1girl, holding, tennis racket, racket" Change: "holding gun"
DENOISE: 0.70
TAGS: 1girl, (holding gun:2.5), (pistol:2.0), (handgun:2.0), holding weapon, vibrant colors, sharp, high quality
NEGATIVE: tennis racket, racket, sports equipment, ball

Tags: "1girl, holding, phone" Change: "holding coffee"
DENOISE: 0.70
TAGS: 1girl, (holding cup:2.5), (coffee cup:2.0), (coffee:2.0), holding, vibrant colors, sharp, high quality
NEGATIVE: phone, smartphone, mobile phone, cellphone

Tags: "1girl, holding, sword" Change: "holding food"
DENOISE: 0.70
TAGS: 1girl, (holding food:2.5), (eating:2.0), food, vibrant colors, sharp, high quality
NEGATIVE: sword, weapon, blade

Tags: "pig, barn, fireworks, night sky" Change: "cat"
DENOISE: 0.85
TAGS: (cat:2.5), (feline:2.0), barn, fireworks, night sky, vibrant colors, sharp, high quality
NEGATIVE: pig, swine, deformed, extra limbs, bad anatomy, blurry

Tags: "dog, park, grass" Change: "wolf"
DENOISE: 0.85
TAGS: (wolf:2.5), (grey wolf:2.0), park, grass, vibrant colors, sharp, high quality
NEGATIVE: dog, domestic dog, deformed, extra limbs, bad anatomy, blurry

Tags: "1boy 1girl, silver hair, elf ears, bikini, hat, military uniform" Change: "blonde hair"
DENOISE: 0.80
TAGS: 1boy 1girl, (blonde hair:2.0), blonde hair, elf ears, bikini, hat, military uniform, vibrant colors, sharp, high quality
NEGATIVE: silver hair, deformed, extra limbs, bad anatomy, blurry

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

Tags: "1girl, long black hair, brown eyes, tan skin, traditional Japanese kimono, cherry blossom accessory, anime" Change: "nude"
DENOISE: 0.80
TAGS: 1girl, long black hair, brown eyes, tan skin, (nude:2.0), (naked:2.0), cherry blossom accessory, anime, vibrant colors, sharp, high quality
NEGATIVE: traditional Japanese kimono, kimono, clothing, clothed, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, long black hair, brown eyes, tan skin, traditional kimono, cherry blossom, anime" Change: "white skin"
DENOISE: 0.80
TAGS: 1girl, long black hair, brown eyes, (pale skin:2.0), (white skin:2.0), (fair skin:2.0), traditional kimono, cherry blossom, anime, vibrant colors, sharp, high quality
NEGATIVE: tan skin, dark skin, brown skin, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, brown hair, blue eyes, light skin, casual outfit, anime, sitting pose" Change: "dark skin"
DENOISE: 0.80
TAGS: (dark skin:2.0), (brown skin:2.0), 1girl, brown hair, blue eyes, casual outfit, anime, sitting pose, vibrant colors, sharp, high quality
NEGATIVE: light skin, pale skin, white skin, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, long black hair, brown eyes, light skin, white dress, flower accessory, garden, anime" Change: "nude"
DENOISE: 0.80
TAGS: 1girl, long black hair, brown eyes, light skin, (nude:2.0), (naked:2.0), flower accessory, garden, anime, vibrant colors, sharp, high quality
NEGATIVE: white dress, dress, clothing, clothed, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, brown hair, green eyes, tan skin, casual outfit, anime" Change: "blonde hair"
DENOISE: 0.80
TAGS: 1girl, (blonde hair:2.0), blonde hair, green eyes, tan skin, casual outfit, anime, vibrant colors, sharp, high quality
NEGATIVE: brown hair, deformed, extra limbs, bad anatomy, blurry

Tags: "1girl, solo, long hair, breasts, looking at viewer, blush, blue eyes, bow, animal ears, cleavage, jewelry, tail, ponytail, purple hair, earrings, parted lips, dark skin, bowtie, armpits, rabbit ears, arm up, leotard, dark-skinned female, wrist cuffs, black bow, covered navel, detached collar, fake animal ears, highleg, playboy bunny, rabbit tail, black leotard, strapless leotard, black bowtie" Change: "nude"
DENOISE: 0.80
TAGS: (naked:2.0), (nude:2.0), 1girl, solo, long hair, breasts, looking at viewer, blush, blue eyes, bow, animal ears, cleavage, jewelry, tail, ponytail, purple hair, earrings, parted lips, dark skin, bowtie, armpits, rabbit ears, arm up, dark-skinned female, wrist cuffs, black bow, detached collar, fake animal ears, playboy bunny, rabbit tail, vibrant colors, sharp, high quality
NEGATIVE: leotard, black leotard, strapless leotard, covered navel, highleg, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, solo, long hair, breasts, looking at viewer, blush, blue eyes, bow, animal ears, cleavage, jewelry, tail, ponytail, purple hair, earrings, parted lips, dark skin, bowtie, armpits, rabbit ears, arm up, leotard, dark-skinned female, wrist cuffs, black bow, covered navel, detached collar, fake animal ears, highleg, playboy bunny, rabbit tail, black leotard, strapless leotard, black bowtie" Change: "white skin"
DENOISE: 0.80
TAGS: (pale skin:2.0), (white skin:2.0), (fair skin:2.0), 1girl, solo, long hair, breasts, looking at viewer, blush, blue eyes, bow, animal ears, cleavage, jewelry, tail, ponytail, purple hair, earrings, parted lips, bowtie, armpits, rabbit ears, arm up, leotard, wrist cuffs, black bow, covered navel, detached collar, fake animal ears, highleg, playboy bunny, rabbit tail, black leotard, strapless leotard, black bowtie, vibrant colors, sharp, high quality
NEGATIVE: dark skin, dark-skinned female, tan skin, brown skin, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, long hair, breasts, looking at viewer, blush, large breasts, red eyes, animal ears, bare shoulders, jewelry, tail, purple hair, ass, pantyhose, earrings, solo focus, looking back, from behind, rabbit ears, leotard, wrist cuffs, strapless, detached collar, fake animal ears, colored skin, playboy bunny, rabbit tail, fishnets, black leotard, strapless leotard, hands on hips, hoop earrings, blue skin, fishnet pantyhose, purple skin, purple leotard" Change: "nude"
DENOISE: 0.80
TAGS: (naked:2.0), (nude:2.0), 1girl, long hair, breasts, looking at viewer, blush, large breasts, red eyes, animal ears, bare shoulders, jewelry, tail, purple hair, ass, earrings, solo focus, looking back, from behind, rabbit ears, wrist cuffs, detached collar, fake animal ears, colored skin, playboy bunny, rabbit tail, hands on hips, hoop earrings, blue skin, purple skin, vibrant colors, sharp, high quality
NEGATIVE: pantyhose, leotard, black leotard, strapless leotard, purple leotard, fishnets, fishnet pantyhose, strapless, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, long hair, breasts, looking at viewer, blush, large breasts, red eyes, animal ears, bare shoulders, jewelry, tail, purple hair, ass, pantyhose, earrings, solo focus, looking back, from behind, rabbit ears, leotard, wrist cuffs, strapless, detached collar, fake animal ears, colored skin, playboy bunny, rabbit tail, fishnets, black leotard, strapless leotard, hands on hips, hoop earrings, blue skin, fishnet pantyhose, purple skin, purple leotard" Change: "dark skin"
DENOISE: 0.80
TAGS: (dark skin:2.0), (brown skin:2.0), (dark-skinned female:2.0), 1girl, long hair, breasts, looking at viewer, blush, large breasts, red eyes, animal ears, bare shoulders, jewelry, tail, purple hair, ass, pantyhose, earrings, solo focus, looking back, from behind, rabbit ears, leotard, wrist cuffs, strapless, detached collar, fake animal ears, playboy bunny, rabbit tail, fishnets, black leotard, strapless leotard, hands on hips, hoop earrings, fishnet pantyhose, purple leotard, vibrant colors, sharp, high quality
NEGATIVE: colored skin, blue skin, purple skin, pale skin, white skin, light skin, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, solo, breasts, looking at viewer, blush, smile, open mouth, bangs, large breasts, black hair, hair ornament, cleavage, bare shoulders, sitting, collarbone, yellow eyes, white hair, thighs, multicolored hair, japanese clothes, sky, kimono, off shoulder, nail polish, sash, black nails, new year, happy new year, black kimono, fireworks, sparkler" Change: "nude"
DENOISE: 0.80
TAGS: (naked:2.0), (nude:2.0), 1girl, solo, breasts, looking at viewer, blush, smile, open mouth, bangs, large breasts, black hair, hair ornament, cleavage, bare shoulders, sitting, collarbone, yellow eyes, white hair, thighs, multicolored hair, sky, off shoulder, nail polish, black nails, new year, happy new year, fireworks, sparkler, vibrant colors, sharp, high quality
NEGATIVE: japanese clothes, kimono, black kimono, sash, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, solo, breasts, looking at viewer, blush, smile, open mouth, bangs, large breasts, black hair, hair ornament, cleavage, bare shoulders, sitting, collarbone, yellow eyes, white hair, thighs, multicolored hair, japanese clothes, sky, kimono, off shoulder, nail polish, sash, black nails, new year, happy new year, black kimono, fireworks, sparkler" Change: "dark skin"
DENOISE: 0.80
TAGS: (dark skin:2.0), (brown skin:2.0), (dark-skinned female:2.0), 1girl, solo, breasts, looking at viewer, blush, smile, open mouth, bangs, large breasts, black hair, hair ornament, cleavage, bare shoulders, sitting, collarbone, yellow eyes, white hair, thighs, multicolored hair, japanese clothes, sky, kimono, off shoulder, nail polish, sash, black nails, new year, happy new year, black kimono, fireworks, sparkler, vibrant colors, sharp, high quality
NEGATIVE: pale skin, white skin, light skin, fair skin, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, solo, breasts, looking at viewer, blush, smile, open mouth, bangs, large breasts, black hair, hair ornament, cleavage, bare shoulders, sitting, collarbone, yellow eyes, white hair, thighs, multicolored hair, japanese clothes, sky, kimono, off shoulder, nail polish, sash, black nails, new year, happy new year, black kimono, fireworks, sparkler" Change: "dark brown skin afro hair, anime"
DENOISE: 0.85
TAGS: (afro:2.5), (afro hair:2.5), (dark brown skin:2.0), (brown skin:2.0), (dark-skinned female:2.0), 1girl, solo, breasts, looking at viewer, blush, smile, open mouth, large breasts, hair ornament, cleavage, bare shoulders, sitting, collarbone, yellow eyes, thighs, japanese clothes, sky, kimono, off shoulder, nail polish, sash, black nails, new year, happy new year, black kimono, fireworks, sparkler, (anime:1.5), vibrant colors, sharp, high quality
NEGATIVE: white hair, multicolored hair, black hair, bangs, straight hair, pale skin, white skin, light skin, fair skin, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, breasts, looking at viewer, bangs, simple background, hair ornament, hat, animal ears, cleavage, purple eyes, swimsuit, braid, open clothes, shorts, huge breasts, twin braids, open jacket, black jacket, denim shorts, striped bikini, straw hat, grey bikini, 1other" Change: "dark brown skin afro hair, anime"
DENOISE: 0.85
TAGS: (afro:2.5), (afro hair:2.5), (dark brown skin:2.0), (brown skin:2.0), (dark-skinned female:2.0), 1girl, breasts, looking at viewer, simple background, animal ears, cleavage, purple eyes, swimsuit, braid, open clothes, shorts, huge breasts, twin braids, open jacket, black jacket, denim shorts, striped bikini, straw hat, grey bikini, (anime:1.5), vibrant colors, sharp, high quality
NEGATIVE: bangs, straight hair, hat, hair ornament, pale skin, white skin, light skin, fair skin, 1other, extra people, deformed, extra limbs, bad anatomy, blurry, distorted

Tags: "1girl, orange hair, red eyes, anime, portrait, looking at viewer" Change: "black skin afro hair, anime"
DENOISE: 0.85
TAGS: (afro:2.5), (afro hair:2.5), (black afro:2.0), (dark skin:2.0), (black skin:2.0), (dark-skinned female:2.0), 1girl, red eyes, anime, portrait, looking at viewer, (anime:1.5), vibrant colors, sharp, high quality
NEGATIVE: orange hair, red hair, straight hair, pale skin, white skin, light skin, fair skin, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, brown hair, brown eyes, anime, portrait" Change: "gawr gura face, anime"
DENOISE: 0.85
TAGS: (gawr gura:2.5), (gawr gura \(hololive\):2.0), 1girl, blue hair, blue eyes, shark hair ornament, shark hoodie, anime, portrait, vibrant colors, sharp, high quality
NEGATIVE: brown hair, brown eyes, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "1girl, blue hair, purple hair, blue eyes, bamboo forest, red rope, bondage, shibari, topless, nude, breasts, anime" Change: "swimsuit, anime"
DENOISE: 0.75
TAGS: 1girl, blue hair, purple hair, blue eyes, bamboo forest, (swimsuit:2.0), (bikini:1.5), wet, water droplets, anime, vibrant colors, sharp, high quality
NEGATIVE: red rope, bondage, shibari, topless, nude, naked, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

Tags: "2girls, green hair, black hair, black dress, champagne glass, holding, cleavage, indoor, chandelier, party, anime" Change: "nude naked red hair, anime"
DENOISE: 0.70
TAGS: 2girls, (red hair:2.0), red hair, (nude:2.0), (naked:2.0), champagne glass, holding, cleavage, indoor, chandelier, party, anime, vibrant colors, sharp, high quality
NEGATIVE: green hair, black hair, black dress, dress, clothing, clothed, deformed, extra limbs, bad anatomy, blurry, distorted, extra people, 1other

Tags: "2girls, green hair, black hair, black dress, champagne glass, holding, cleavage, indoor, chandelier, party, anime" Change: "red hair coke cans, anime"
DENOISE: 0.65
TAGS: 2girls, (red hair:2.0), red hair, (coke can:2.0), (coca cola:1.5), holding, black dress, cleavage, indoor, chandelier, party, anime, vibrant colors, sharp, high quality
NEGATIVE: green hair, black hair, champagne glass, champagne, wine glass, deformed, extra limbs, bad anatomy, blurry, distorted, extra people, 1other

Tags: "1girl, solo, long hair, breasts, looking at viewer, blush, smile, open mouth, large breasts, black hair, dress, holding, cleavage, bare shoulders, jewelry, standing, collarbone, earrings, choker, fang, nail polish, black eyes, black dress, hands up, strapless, covered navel, black choker, strapless dress, red nails" Change: "dark brown skin afro hair, anime"
DENOISE: 0.85
TAGS: (afro:2.5), (afro hair:2.5), (dark brown skin:2.0), (brown skin:2.0), (dark-skinned female:2.0), 1girl, solo, breasts, looking at viewer, blush, smile, open mouth, large breasts, black eyes, dress, holding, cleavage, bare shoulders, jewelry, standing, collarbone, earrings, choker, fang, nail polish, black dress, hands up, strapless, covered navel, black choker, strapless dress, red nails, (anime:1.5), vibrant colors, sharp, high quality
NEGATIVE: long hair, black hair, straight hair, pale skin, white skin, light skin, fair skin, deformed, extra limbs, bad anatomy, blurry, distorted, extra people

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
                    f"{self.ollama_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "options": {
                            "temperature": 0.2,
                            "num_predict": 400,
                            "num_ctx": self.num_ctx
                        },
                        "stream": False,
                        "keep_alive": self.keep_alive
                    }
                )
                response.raise_for_status()
                data = response.json()
                content = data["message"]["content"]

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
