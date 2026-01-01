import json
import re
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator, Optional
from sqlalchemy.orm import Session
from app.models import Setting
from app.services.inference_factory import get_inference_service, prepare_vram_for_llm

# Thread pool for running synchronous generators
_stream_executor = ThreadPoolExecutor(max_workers=4)

# Import WD14 tagger from posterchan's comfyui module
import sys
sys.path.insert(0, '/home/verita84/posterchan')
from comfyui import describe_image_with_wd14

# Path to training data (local to this project)
IMG2IMG_TRAINING_FILE = "/home/verita84/posterchanai/img2img_training.json"

def load_img2img_prompt():
    """Load and format the img2img training prompt from JSON file."""
    try:
        with open(IMG2IMG_TRAINING_FILE, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[WARN] Could not load img2img training data: {e}")
        return None

    # Build the prompt
    lines = [data["format"], ""]

    # Add denoise values
    lines.append("DENOISE values:")
    lines.append(f"- {data['denoise_values']['hair_style']} = hair STYLE changes (afro, ponytail, straight, curly, short, long), ANIMAL changes (pig to cat, dog to wolf)")
    lines.append(f"- {data['denoise_values']['color_change']} = color changes (hair, eyes, skin), clothing removal (naked/nude)")
    lines.append(f"- {data['denoise_values']['background_scene']} = background/scene changes")
    lines.append(f"- {data['denoise_values']['object_change']} = object changes (holding different items)")
    lines.append(f"- {data['denoise_values']['art_style']} = art style changes (anime, realistic)")
    lines.append(f"- {data['denoise_values']['body_modification']} = body modifications (breast size)")
    lines.append(f"- {data['denoise_values']['minor_change']} = minor changes (accessories)")
    lines.append("")

    # Add rules
    lines.append("RULES:")
    for rule in data["rules"]:
        lines.append(rule)
    lines.append("")

    # Add examples
    lines.append("Examples:")
    for ex in data["examples"]:
        lines.append(f'Tags: "{ex["tags"]}" Change: "{ex["change"]}"')
        lines.append(f'DENOISE: {ex["denoise"]:.2f}')
        lines.append(f'TAGS: {ex["output_tags"]}')
        if ex.get("negative"):
            lines.append(f'NEGATIVE: {ex["negative"]}')
        lines.append("")

    # Add txt2img example
    if "txt2img_example" in data:
        ex = data["txt2img_example"]
        lines.append(f'User wants: "{ex["prompt"]}"')
        lines.append(f'DENOISE: {ex["denoise"]:.1f}')
        lines.append(f'TAGS: {ex["output_tags"]}')
        lines.append(f'NEGATIVE: {ex["negative"]}')

    return "\n".join(lines)


class ChatService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        """Load settings - inference factory handles all backend-specific settings"""
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.system_prompt = settings.get("ollama_system_prompt", "You are a helpful, friendly AI assistant.")
        # These are used for chat_stream kwargs
        self.temperature = float(settings.get("ollama_temperature", "0.7"))
        self.top_p = float(settings.get("ollama_top_p", "0.9"))
        self.num_predict = int(settings.get("ollama_num_predict", "2048"))

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        matches = list(re.finditer(r'</think(?:ing)?>', response, re.IGNORECASE))
        if matches:
            last_match = matches[-1]
            return response[last_match.end():].strip()
        return response

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat completion using inference factory"""
        try:
            # Prepare VRAM for LLM (swap models if needed)
            prepare_vram_for_llm(self.db)
            service = get_inference_service(self.db)
            result = await service.chat_completion(
                messages=messages,
                temperature=self.temperature,
                top_p=self.top_p,
                max_tokens=self.num_predict
            )
            if "error" in result:
                return f"Error: {result['error'].get('message', 'Unknown error')}"
            content = result["choices"][0]["message"]["content"]
            return self.strip_thinking_tags(content)
        except Exception as e:
            return f"Error: {str(e)}"

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """Streaming chat completion - uses async queue to avoid blocking event loop"""
        try:
            # Prepare VRAM for LLM (swap models if needed)
            prepare_vram_for_llm(self.db)
            service = get_inference_service(self.db)

            # Use direct content streaming for native backend
            if hasattr(service, 'stream_chat_content'):
                queue = asyncio.Queue()
                loop = asyncio.get_event_loop()

                def run_streaming():
                    """Run synchronous generator in thread, put results in queue"""
                    try:
                        for content in service.stream_chat_content(
                            messages=messages,
                            temperature=self.temperature,
                            top_p=self.top_p,
                            max_tokens=self.num_predict
                        ):
                            loop.call_soon_threadsafe(queue.put_nowait, content)
                    except Exception as e:
                        loop.call_soon_threadsafe(queue.put_nowait, f"Error: {e}")
                    finally:
                        loop.call_soon_threadsafe(queue.put_nowait, None)  # Signal end

                # Start streaming in background thread
                _stream_executor.submit(run_streaming)

                # Process queue with think tag filtering
                buffer = ""
                thinking_mode = None  # None=unknown, True=in thinking, False=no thinking

                while True:
                    content = await queue.get()
                    if content is None:
                        break

                    if content.startswith("Error:"):
                        yield content
                        return

                    buffer += content

                    if thinking_mode is None:
                        # Check if model started with <think> tag
                        if '<think' in buffer.lower():
                            thinking_mode = True
                        elif len(buffer) > 20:
                            # No think tag in first 20 chars - assume no thinking
                            thinking_mode = False
                            yield buffer
                            buffer = ""

                    if thinking_mode is True:
                        # In thinking mode - look for end tag
                        match = re.search(r'</think(?:ing)?>', buffer, re.IGNORECASE)
                        if match:
                            thinking_mode = False
                            after_think = buffer[match.end():]
                            buffer = ""
                            if after_think.strip():
                                yield after_think
                    elif thinking_mode is False:
                        # Not thinking - stream directly
                        yield content
                        buffer = ""

                # Yield any remaining buffer
                if buffer:
                    # Strip think tags if present
                    clean = self.strip_thinking_tags(buffer)
                    if clean:
                        yield clean
            else:
                # Fallback to SSE parsing for Ollama
                async for chunk in service.chat_completion_stream(
                    messages=messages,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.num_predict
                ):
                    if chunk.startswith("data: "):
                        data_str = chunk[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
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

        # Load prompt from JSON file
        system_prompt = load_img2img_prompt()
        if not system_prompt:
            print("[IMG2IMG] Could not load training data, using fallback")
            return user_prompt + ", vibrant colors, sharp, high quality", 0.70, "bad quality, blurry, distorted"

        # Prompt is now loaded from img2img_training.json

        # Format message based on whether we have original tags
        if original_tags:
            user_message = f'Tags: "{original_tags}" Change: "{user_prompt}"'
        else:
            user_message = f'User wants: "{user_prompt}"'

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]

        try:
            # Prepare VRAM for LLM (swap models if needed)
            prepare_vram_for_llm(self.db)
            service = get_inference_service(self.db)
            result = await service.chat_completion(
                messages=messages,
                temperature=0.2,
                max_tokens=400
            )

            if "error" in result:
                print(f"[IMG2IMG] Inference error: {result['error']}")
                return user_prompt + ", vibrant colors, sharp, high quality", 1.0, "bad quality, blurry, distorted"

            content = result["choices"][0]["message"]["content"]
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


# Old hardcoded training data removed - now loaded from /home/verita84/posterchan/img2img_training.json


def get_chat_service(db: Session) -> ChatService:
    return ChatService(db)

