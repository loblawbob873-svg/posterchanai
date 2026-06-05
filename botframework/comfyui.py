import json
import time
import random
import re
from urllib import request
from urllib.parse import quote
from config import ANIME_IMAGE_MODEL
from config import BASIC_IMAGE_MODEL
from config import COMFYUI_API_ENDPOINT

# Validate API endpoint at module load
if COMFYUI_API_ENDPOINT:
    from urllib.parse import urlparse
    _parsed = urlparse(COMFYUI_API_ENDPOINT)
    if _parsed.scheme not in ('http', 'https'):
        raise ValueError(f"COMFYUI_API_ENDPOINT must use http or https scheme, got: {_parsed.scheme}")


def extract_prompt_from_image(image_bytes):
    """
    Extract the generation prompt from PNG metadata.
    ComfyUI and other SD tools embed the prompt in PNG tEXt chunks.

    Returns the prompt string if found, None otherwise.
    """
    try:
        from io import BytesIO
        import struct

        # PNG files start with an 8-byte signature
        if image_bytes[:8] != b'\x89PNG\r\n\x1a\n':
            print("[METADATA] Not a PNG file, cannot extract prompt")
            return None

        # Parse PNG chunks looking for tEXt, iTXt, or zTXt chunks
        pos = 8  # Skip PNG signature
        prompt = None

        while pos < len(image_bytes):
            if pos + 8 > len(image_bytes):
                break

            # Read chunk length and type
            chunk_length = struct.unpack('>I', image_bytes[pos:pos+4])[0]
            chunk_type = image_bytes[pos+4:pos+8].decode('ascii', errors='ignore')

            if chunk_type == 'IEND':
                break

            chunk_data = image_bytes[pos+8:pos+8+chunk_length]

            # Check tEXt chunks (uncompressed text)
            if chunk_type == 'tEXt':
                # Format: keyword\0text
                null_pos = chunk_data.find(b'\x00')
                if null_pos != -1:
                    keyword = chunk_data[:null_pos].decode('latin-1', errors='ignore')
                    text = chunk_data[null_pos+1:].decode('latin-1', errors='ignore')

                    # ComfyUI uses "prompt" key, A1111 uses "parameters"
                    if keyword.lower() in ('prompt', 'parameters', 'positive'):
                        print(f"[METADATA] Found prompt in tEXt chunk (keyword: {keyword})")
                        # For ComfyUI, the prompt might be JSON - try to extract the positive prompt
                        if text.startswith('{'):
                            try:
                                import json
                                data = json.loads(text)
                                # Look for positive prompt in ComfyUI workflow format
                                for node_id, node in data.items():
                                    if isinstance(node, dict):
                                        inputs = node.get('inputs', {})
                                        if 'text' in inputs and node.get('class_type') == 'CLIPTextEncode':
                                            # Check if this is likely the positive prompt (not negative)
                                            meta = node.get('_meta', {})
                                            title = meta.get('title', '').lower()
                                            if 'negative' not in title:
                                                prompt = inputs['text']
                                                print(f"[METADATA] Extracted prompt from ComfyUI workflow: {prompt[:100]}...")
                                                return prompt
                            except json.JSONDecodeError:
                                pass
                        else:
                            prompt = text
                            return prompt

            # Check iTXt chunks (international text, UTF-8)
            elif chunk_type == 'iTXt':
                # Format: keyword\0compression_flag\0compression_method\0language_tag\0translated_keyword\0text
                null_pos = chunk_data.find(b'\x00')
                if null_pos != -1:
                    keyword = chunk_data[:null_pos].decode('utf-8', errors='ignore')
                    if keyword.lower() in ('prompt', 'parameters', 'positive', 'workflow'):
                        # Skip to the text part (after multiple null separators)
                        remaining = chunk_data[null_pos+1:]
                        # Skip compression flag, method, language tag, translated keyword
                        for _ in range(3):
                            next_null = remaining.find(b'\x00')
                            if next_null == -1:
                                break
                            remaining = remaining[next_null+1:]

                        text = remaining.decode('utf-8', errors='ignore')
                        if text:
                            print(f"[METADATA] Found prompt in iTXt chunk (keyword: {keyword})")
                            prompt = text
                            return prompt

            # Move to next chunk (length + type + data + CRC)
            pos += 4 + 4 + chunk_length + 4

        if not prompt:
            print("[METADATA] No prompt found in PNG metadata")

        return prompt

    except Exception as e:
        print(f"[METADATA] Error extracting prompt from image: {e}")
        import traceback
        traceback.print_exc()
        return None


def strip_character_name(tags):
    """Remove character names from WD14 tags (they override other attributes)."""
    if not tags:
        return tags
    parts = tags.split(", ")
    for i, p in enumerate(parts):
        if p in ["1girl", "1boy", "solo", "1other", "multiple girls", "multiple boys"]:
            return ", ".join(parts[i:])
    return tags


def describe_image_with_wd14(image_bytes, threshold=0.35):
    """
    Use WD14 Tagger in ComfyUI to get tags/description from an image.
    Returns a comma-separated string of tags, or None on failure.

    Args:
        image_bytes: Raw bytes of the image
        threshold: Minimum confidence threshold for tags (default 0.35)
    """
    try:
        # First upload the image
        uploaded_name = upload_image_to_comfyui(image_bytes, f"wd14_input_{int(time.time())}.png")
        if not uploaded_name:
            print("[WD14] Failed to upload image")
            return None

        # Build WD14 Tagger workflow
        workflow = {
            "1": {
                "class_type": "LoadImage",
                "inputs": {
                    "image": uploaded_name
                }
            },
            "2": {
                "class_type": "WD14Tagger|pysssss",
                "inputs": {
                    "image": ["1", 0],
                    "model": "wd-v1-4-moat-tagger-v2",
                    "threshold": threshold,
                    "character_threshold": threshold,
                    "replace_underscore": True,
                    "trailing_comma": True,
                    "exclude_tags": ""
                }
            }
        }

        # Submit the workflow
        url = f"{COMFYUI_API_ENDPOINT}/prompt"
        data = json.dumps({"prompt": workflow}).encode('utf-8')
        req = request.Request(url, data=data, headers={'Content-Type': 'application/json'})

        with request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode('utf-8'))
            prompt_id = result.get('prompt_id')

        if not prompt_id:
            print("[WD14] No prompt_id returned")
            return None

        print(f"[WD14] Submitted tagging request, prompt_id: {prompt_id}")

        # Wait for completion and get the tags from history
        max_wait = 60
        start_time = time.time()

        while time.time() - start_time < max_wait:
            time.sleep(1)
            history_url = f"{COMFYUI_API_ENDPOINT}/history/{prompt_id}"

            try:
                with request.urlopen(history_url, timeout=10) as response:
                    history = json.loads(response.read().decode('utf-8'))

                if prompt_id in history:
                    outputs = history[prompt_id].get('outputs', {})
                    # WD14 Tagger outputs tags as text in node 2
                    if '2' in outputs and 'tags' in outputs['2']:
                        tags = outputs['2']['tags'][0]  # First result
                        tags = strip_character_name(tags)  # Remove character names
                        print(f"[WD14] Got tags: {tags[:100]}...")
                        return tags
                    # Check if completed but no tags
                    status = history[prompt_id].get('status', {})
                    if status.get('completed', False):
                        print("[WD14] Completed but no tags found in output")
                        return None
            except Exception as e:
                print(f"[WD14] Polling error (continuing): {e}")

        print("[WD14] Timeout waiting for tags")
        return None

    except Exception as e:
        print(f"[WD14] Error: {e}")
        import traceback
        traceback.print_exc()
        return None


def is_safe_path_component(value):
    """Validate that a path component is safe (no path traversal)"""
    if not value or not isinstance(value, str):
        return False
    # Block path traversal sequences and absolute paths
    if '..' in value or value.startswith('/') or value.startswith('\\'):
        return False
    # Block null bytes and other dangerous characters
    if '\x00' in value or '\n' in value or '\r' in value:
        return False
    return True

def generate_image_bytes_with_retries(prompt, max_retries=None, retry_delay=30):
    """
    Generate image with retries (max_retries if specified, defaults to 50).
    Will keep trying until successful to ensure no requests are missed.
    Uses exponential backoff with base delay of retry_delay.
    """
    # Cap max_retries to prevent infinite loops
    if max_retries is None:
        max_retries = 50
    attempt = 0
    max_delay = 300  # Cap at 5 minutes
    while attempt < max_retries:
        attempt += 1
        print(f"Image generation attempt {attempt}...")

        try:
            image_bytes = generate_image_bytes(prompt)
            if image_bytes:
                print(f"Image generation successful on attempt {attempt}")
                return image_bytes
            else:
                print(f"Image generation returned None on attempt {attempt}")
        except Exception as e:
            print(f"Image generation exception on attempt {attempt}: {e}")
            import traceback
            traceback.print_exc()

        # Exponential backoff: 30s, 60s, 120s, 240s... capped at max_delay
        delay = min(retry_delay * (2 ** (attempt - 1)), max_delay)
        print(f"Retrying in {delay} seconds...")
        time.sleep(delay)

    print(f"Image generation failed after {max_retries} attempts")
    return None

def is_valid_model_name(model_name):
    """Validate model name to prevent injection attacks"""
    if not model_name or not isinstance(model_name, str):
        return False
    # Allow only alphanumeric, underscores, hyphens, dots, and forward slashes
    # This covers typical model names like "sd_xl_base_1.0.safetensors"
    if not re.match(r'^[a-zA-Z0-9_\-\./]+$', model_name):
        return False
    # Block path traversal
    if '..' in model_name:
        return False
    return True


def generate_image_bytes(data):
    clean_prompt = sanitize_prompt(data.replace("geni", "").strip())
    random_seed = random.getrandbits(31)  # Use 31-bit for signed 32-bit compatibility
    if "anime" in clean_prompt.lower():
      model = ANIME_IMAGE_MODEL
    else:
      model = BASIC_IMAGE_MODEL

    # Validate model name before using in workflow
    if not is_valid_model_name(model):
        print(f"Invalid model name configured: {model}")
        return None
    workflow_with_api_nodes = """
{
  "3": {
    "inputs": {
      "seed": %(random_seed)d,
      "steps": 30,
      "cfg": 9,
      "sampler_name": "euler",
      "scheduler": "normal",
      "denoise": 1,
      "model": [
        "4",
        0
      ],
      "positive": [
        "6",
        0
      ],
      "negative": [
        "7",
        0
      ],
      "latent_image": [
        "5",
        0
      ]
    },
    "class_type": "KSampler",
    "_meta": {
      "title": "KSampler"
    }
  },
  "4": {
    "inputs": {
      "ckpt_name": "%(model)s"
    },
    "class_type": "CheckpointLoaderSimple",
    "_meta": {
      "title": "Load Checkpoint"
    }
  },
  "5": {
    "inputs": {
      "width": 1024,
      "height": 1024,
      "batch_size": 1
    },
    "class_type": "EmptyLatentImage",
    "_meta": {
      "title": "Empty Latent Image"
    }
  },
  "6": {
    "inputs": {
      "text": "%(clean_prompt)s",
      "clip": [
        "4",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "CLIP Text Encode (Prompt)"
    }
  },
  "7": {
    "inputs": {
      "text": "distorted, warped, stretched, squished, wrong proportions, extra fingers, ugly, disfigured, missing fingers, censored, blury face, ugly face, low quality, blury, low res, low resolution, Cropped, Out of frame, Out of focus, watermark, banner, extra digits, Jpeg artifacts, Grainy, Bad anatomy, Bad proportions, Deformed, Disfigured, Extra arms, Extra limbs, Extra hands, Fused fingers, Gross proportions, Long neck, Malformed limbs, Mutated, Mutated hands, Mutated limbs, Missing arms, bad quality hands, Poorly drawn hands, Poorly drawn face, low saturation, harsh lighting, underexposed, bad photography, bad photo, food in mouth, food touching tongue, food touching lips, tongue outside mouth, extra limbs, extra arms, extra legs",
      "clip": [
        "4",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "CLIP Text Encode (Prompt)"
    }
  },
  "8": {
    "inputs": {
      "samples": [
        "3",
        0
      ],
      "vae": [
        "4",
        2
      ]
    },
    "class_type": "VAEDecode",
    "_meta": {
      "title": "VAE Decode"
    }
  },
  "9": {
    "inputs": {
      "filename_prefix": "ComfyUI",
      "images": [
        "8",
        0
      ]
    },
    "class_type": "SaveImage",
    "_meta": {
      "title": "Save Image"
    }
  }
}
""" % {"random_seed": random_seed, "model": model, "clean_prompt": clean_prompt}

    try:
        print(f"Submitting image generation request to ComfyUI...")
        print(f"Using model: {model}")
        print(f"Prompt: {clean_prompt[:100]}{'...' if len(clean_prompt) > 100 else ''}")

        prompt = json.loads(workflow_with_api_nodes)
        payload = {"prompt": prompt}
        data_bytes = json.dumps(payload).encode("utf-8")

        req = request.Request(f"{COMFYUI_API_ENDPOINT}/prompt", data=data_bytes, headers={"Content-Type": "application/json"})
        response = request.urlopen(req, timeout=30)
        result = json.loads(response.read())

        if 'prompt_id' not in result:
            print(f"ComfyUI did not return a prompt_id. Response: {result}")
            return None

        prompt_id = result['prompt_id']
        print(f"ComfyUI accepted request, prompt_id: {prompt_id}")
        return fetch_image_from_history(prompt_id)

    except Exception as e:
        print(f"Error generating image: {e}")
        import traceback
        traceback.print_exc()
        return None


def sanitize_prompt(text):
    # Remove control characters and special chars that can break JSON
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
    # Escape special JSON characters
    text = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
    return text
  
def upload_image_to_comfyui(image_bytes, filename="input.png"):
    """
    Upload an image to ComfyUI's input directory for img2img.
    Returns the filename if successful, None otherwise.
    Handles PNG, JPEG, GIF, and WebP formats. GIFs are converted to PNG (first frame).
    """
    import uuid
    from urllib.parse import urlparse

    # Detect image format from magic bytes
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        content_type = "image/png"
        ext = ".png"
    elif image_bytes[:2] == b'\xff\xd8':
        content_type = "image/jpeg"
        ext = ".jpg"
    elif image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        # GIF - convert to PNG (extract first frame) since ComfyUI doesn't handle GIF well
        print("[UPLOAD] Converting GIF to PNG (first frame)...")
        try:
            from PIL import Image
            import io
            gif = Image.open(io.BytesIO(image_bytes))
            # Get first frame
            gif.seek(0)
            # Convert to RGB if needed (GIFs can be palette mode)
            if gif.mode != 'RGB':
                gif = gif.convert('RGB')
            # Save as PNG
            png_buffer = io.BytesIO()
            gif.save(png_buffer, format='PNG')
            image_bytes = png_buffer.getvalue()
            print(f"[UPLOAD] GIF converted to PNG ({len(image_bytes)} bytes)")
        except Exception as e:
            print(f"[UPLOAD] Failed to convert GIF: {e}")
            return None
        content_type = "image/png"
        ext = ".png"
    elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        content_type = "image/webp"
        ext = ".webp"
    else:
        # Default to PNG
        content_type = "image/png"
        ext = ".png"

    # Generate unique filename to avoid collisions
    base_filename = filename.rsplit('.', 1)[0] if '.' in filename else filename
    unique_filename = f"{uuid.uuid4().hex}_{base_filename}{ext}"

    try:
        # Create multipart form data
        boundary = uuid.uuid4().hex
        body = []
        body.append(f'--{boundary}'.encode())
        body.append(f'Content-Disposition: form-data; name="image"; filename="{unique_filename}"'.encode())
        body.append(f'Content-Type: {content_type}'.encode())
        body.append(b'')
        body.append(image_bytes)
        body.append(f'--{boundary}--'.encode())

        body_bytes = b'\r\n'.join(body)

        req = request.Request(
            f"{COMFYUI_API_ENDPOINT}/upload/image",
            data=body_bytes,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        response = request.urlopen(req, timeout=60)
        result = json.loads(response.read())

        uploaded_name = result.get("name")
        if uploaded_name:
            print(f"Image uploaded to ComfyUI: {uploaded_name}")
            return uploaded_name
        else:
            print(f"ComfyUI upload response missing 'name': {result}")
            return None

    except Exception as e:
        print(f"Error uploading image to ComfyUI: {e}")
        import traceback
        traceback.print_exc()
        return None


def generate_img2img_bytes(prompt_text, source_image_bytes, denoise=0.55, negative_prompt=None):
    """
    Generate an image-to-image transformation using ComfyUI.
    Takes a source image and a prompt describing the desired changes.

    Args:
        prompt_text: Text prompt describing what to change/generate
        source_image_bytes: Raw bytes of the source image
        denoise: Denoise strength (0.0-1.0). Lower = more like original. Default 0.45
        negative_prompt: Optional additional negative prompt terms

    Returns:
        Image bytes if successful, None otherwise
    """
    # Upload source image to ComfyUI
    uploaded_filename = upload_image_to_comfyui(source_image_bytes)
    if not uploaded_filename:
        print("Failed to upload source image for img2img")
        return None

    return _generate_img2img_with_uploaded(prompt_text, uploaded_filename, denoise, negative_prompt)


def _generate_img2img_with_uploaded(prompt_text, uploaded_filename, denoise=0.55, negative_prompt=None):
    """
    Internal function: Generate img2img using an already-uploaded image.
    This avoids re-uploading on retries.
    """
    clean_prompt = sanitize_prompt(re.sub(r'\bregen\b', '', prompt_text, flags=re.IGNORECASE).strip())
    random_seed = random.getrandbits(31)

    if "anime" in clean_prompt.lower():
        model = ANIME_IMAGE_MODEL
    else:
        model = BASIC_IMAGE_MODEL

    # Validate model name before using in workflow
    if not is_valid_model_name(model):
        print(f"Invalid model name configured: {model}")
        return None

    # img2img workflow: LoadImage -> ImageScale -> VAEEncode -> KSampler -> VAEDecode -> SaveImage
    workflow_img2img = """
{
  "3": {
    "inputs": {
      "seed": %(random_seed)d,
      "steps": 18,
      "cfg": 6,
      "sampler_name": "dpmpp_2m",
      "scheduler": "karras",
      "denoise": %(denoise)s,
      "model": [
        "4",
        0
      ],
      "positive": [
        "6",
        0
      ],
      "negative": [
        "7",
        0
      ],
      "latent_image": [
        "12",
        0
      ]
    },
    "class_type": "KSampler",
    "_meta": {
      "title": "KSampler"
    }
  },
  "4": {
    "inputs": {
      "ckpt_name": "%(model)s"
    },
    "class_type": "CheckpointLoaderSimple",
    "_meta": {
      "title": "Load Checkpoint"
    }
  },
  "6": {
    "inputs": {
      "text": "%(clean_prompt)s",
      "clip": [
        "4",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "CLIP Text Encode (Prompt)"
    }
  },
  "7": {
    "inputs": {
      "text": "%(full_negative_prompt)s",
      "clip": [
        "4",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "CLIP Text Encode (Negative)"
    }
  },
  "8": {
    "inputs": {
      "samples": [
        "3",
        0
      ],
      "vae": [
        "4",
        2
      ]
    },
    "class_type": "VAEDecode",
    "_meta": {
      "title": "VAE Decode"
    }
  },
  "9": {
    "inputs": {
      "filename_prefix": "ComfyUI_img2img",
      "images": [
        "8",
        0
      ]
    },
    "class_type": "SaveImage",
    "_meta": {
      "title": "Save Image"
    }
  },
  "10": {
    "inputs": {
      "image": "%(uploaded_filename)s",
      "upload": "image"
    },
    "class_type": "LoadImage",
    "_meta": {
      "title": "Load Image"
    }
  },
  "11": {
    "inputs": {
      "upscale_method": "lanczos",
      "width": 1024,
      "height": 1536,
      "crop": "disabled",
      "image": [
        "10",
        0
      ]
    },
    "class_type": "ImageScale",
    "_meta": {
      "title": "Scale Image"
    }
  },
  "12": {
    "inputs": {
      "pixels": [
        "11",
        0
      ],
      "vae": [
        "4",
        2
      ]
    },
    "class_type": "VAEEncode",
    "_meta": {
      "title": "VAE Encode"
    }
  }
}
"""
    # Build full negative prompt
    base_negative = "distorted, warped, disfigured, deformed, mutated, extra fingers, ugly, missing fingers, censored, blury face, ugly face, low quality, blury, low res, low resolution, Cropped, Out of frame, Out of focus, watermark, banner, extra digits, Jpeg artifacts, Grainy, Bad anatomy, Bad proportions, Extra arms, Extra limbs, Extra hands, Fused fingers, Gross proportions, Long neck, Malformed limbs, Mutated hands, Mutated limbs, Missing arms, bad quality hands, Poorly drawn hands, Poorly drawn face, bad photography, bad photo, stretched, squished, wrong proportions"
    if negative_prompt:
        full_negative_prompt = f"{negative_prompt}, {base_negative}"
        print(f"[IMG2IMG] Added to negative: {negative_prompt}")
    else:
        full_negative_prompt = base_negative

    workflow_img2img = workflow_img2img % {"random_seed": random_seed, "model": model, "clean_prompt": clean_prompt,
       "uploaded_filename": uploaded_filename, "denoise": denoise, "full_negative_prompt": full_negative_prompt}

    try:
        print(f"Submitting img2img request to ComfyUI...")
        print(f"Using model: {model}")
        print(f"Source image: {uploaded_filename}")
        print(f"Denoise: {denoise}")
        print(f"[IMG2IMG] FULL PROMPT BEING SENT TO COMFYUI:")
        print(f"[IMG2IMG] {clean_prompt}")

        prompt = json.loads(workflow_img2img)
        payload = {"prompt": prompt}
        data_bytes = json.dumps(payload).encode("utf-8")

        req = request.Request(f"{COMFYUI_API_ENDPOINT}/prompt", data=data_bytes, headers={"Content-Type": "application/json"})
        response = request.urlopen(req, timeout=30)
        result = json.loads(response.read())

        if 'prompt_id' not in result:
            print(f"ComfyUI did not return a prompt_id. Response: {result}")
            return None

        prompt_id = result['prompt_id']
        print(f"ComfyUI accepted img2img request, prompt_id: {prompt_id}")
        return fetch_image_from_history(prompt_id)

    except Exception as e:
        print(f"Error generating img2img: {e}")
        import traceback
        traceback.print_exc()
        return None


def generate_img2img_bytes_with_retries(prompt_text, source_image_bytes, denoise=0.55, max_retries=10, retry_delay=30, negative_prompt=None, **kwargs):
    """
    Generate img2img with retries.
    Uses exponential backoff with base delay of retry_delay.
    Uploads image only once to avoid duplicate uploads.
    Note: face_swap and auto_identity kwargs are ignored (handled by posterchanai backend only).
    """
    if max_retries is None:
        max_retries = 10
    attempt = 0
    max_delay = 300  # Cap at 5 minutes

    # Upload image ONCE before retry loop
    print(f"Uploading source image to ComfyUI...")
    uploaded_filename = upload_image_to_comfyui(source_image_bytes)
    if not uploaded_filename:
        print("Failed to upload source image for img2img")
        return None

    while attempt < max_retries:
        attempt += 1
        print(f"Img2img generation attempt {attempt}...")

        try:
            image_bytes = _generate_img2img_with_uploaded(prompt_text, uploaded_filename, denoise, negative_prompt)
            if image_bytes:
                print(f"Img2img generation successful on attempt {attempt}")
                return image_bytes
            else:
                print(f"Img2img generation returned None on attempt {attempt}")
        except Exception as e:
            print(f"Img2img generation exception on attempt {attempt}: {e}")
            import traceback
            traceback.print_exc()

        # Exponential backoff
        delay = min(retry_delay * (2 ** (attempt - 1)), max_delay)
        print(f"Retrying in {delay} seconds...")
        time.sleep(delay)

    print(f"Img2img generation failed after {max_retries} attempts")
    return None


def generate_inpaint_with_sam(prompt_text, source_image_bytes, detect_prompt="face", denoise=0.55):
    """
    Generate an inpainted image using SAM segmentation.
    Only modifies the detected object (e.g., "person") while preserving the rest.

    Args:
        prompt_text: Text prompt describing the desired changes (e.g., "dark skin woman")
        source_image_bytes: Raw bytes of the source image
        detect_prompt: What to detect and inpaint (default: "person")
        denoise: Denoise strength for inpainted area (0.0-1.0). Default 0.75 for good changes

    Returns:
        Image bytes if successful, None otherwise
    """
    # Upload source image to ComfyUI
    uploaded_filename = upload_image_to_comfyui(source_image_bytes)
    if not uploaded_filename:
        print("Failed to upload source image for inpainting")
        return None

    return _generate_inpaint_with_uploaded(prompt_text, uploaded_filename, detect_prompt, denoise)


def _generate_inpaint_with_uploaded(prompt_text, uploaded_filename, detect_prompt="face", denoise=0.55):
    """
    Internal function: Generate inpaint using an already-uploaded image.
    """
    clean_prompt = sanitize_prompt(re.sub(r'\bregen\b', '', prompt_text, flags=re.IGNORECASE).strip())
    random_seed = random.getrandbits(31)

    if "anime" in clean_prompt.lower():
        model = ANIME_IMAGE_MODEL
    else:
        model = BASIC_IMAGE_MODEL

    if not is_valid_model_name(model):
        print(f"Invalid model name configured: {model}")
        return None

    # Sanitize detect_prompt to prevent injection
    detect_prompt = sanitize_prompt(detect_prompt)

    # Inpainting workflow with SAM + GroundingDINO
    # Flow: LoadImage -> SAM/DINO segment -> get MASK -> process mask -> SetLatentNoiseMask -> KSampler -> output
    workflow_inpaint = """
{
  "1": {
    "inputs": {
      "image": "%(uploaded_filename)s",
      "upload": "image"
    },
    "class_type": "LoadImage",
    "_meta": {"title": "Load Image"}
  },
  "2": {
    "inputs": {
      "model_name": "sam_vit_b (375MB)"
    },
    "class_type": "SAMModelLoader (segment anything)",
    "_meta": {"title": "SAM Model Loader"}
  },
  "3": {
    "inputs": {
      "model_name": "GroundingDINO_SwinT_OGC (694MB)"
    },
    "class_type": "GroundingDinoModelLoader (segment anything)",
    "_meta": {"title": "GroundingDINO Model Loader"}
  },
  "4": {
    "inputs": {
      "sam_model": ["2", 0],
      "grounding_dino_model": ["3", 0],
      "image": ["1", 0],
      "prompt": "%(detect_prompt)s",
      "threshold": 0.3
    },
    "class_type": "GroundingDinoSAMSegment (segment anything)",
    "_meta": {"title": "Segment Person"}
  },
  "5": {
    "inputs": {
      "ckpt_name": "%(model)s"
    },
    "class_type": "CheckpointLoaderSimple",
    "_meta": {"title": "Load Checkpoint"}
  },
  "6": {
    "inputs": {
      "upscale_method": "lanczos",
      "width": 1024,
      "height": 1024,
      "crop": "center",
      "image": ["1", 0]
    },
    "class_type": "ImageScale",
    "_meta": {"title": "Scale Image"}
  },
  "7": {
    "inputs": {
      "pixels": ["6", 0],
      "vae": ["5", 2]
    },
    "class_type": "VAEEncode",
    "_meta": {"title": "VAE Encode"}
  },
  "8": {
    "inputs": {
      "mask": ["4", 1]
    },
    "class_type": "MaskToImage",
    "_meta": {"title": "Mask To Image"}
  },
  "9": {
    "inputs": {
      "upscale_method": "nearest-exact",
      "width": 1024,
      "height": 1024,
      "crop": "center",
      "image": ["8", 0]
    },
    "class_type": "ImageScale",
    "_meta": {"title": "Scale Mask Image"}
  },
  "10": {
    "inputs": {
      "channel": "red",
      "image": ["9", 0]
    },
    "class_type": "ImageToMask",
    "_meta": {"title": "Image To Mask"}
  },
  "11": {
    "inputs": {
      "mask": ["10", 0],
      "expand": 5,
      "tapered_corners": true
    },
    "class_type": "GrowMask",
    "_meta": {"title": "Grow Mask"}
  },
  "12": {
    "inputs": {
      "mask": ["11", 0],
      "left": 4,
      "top": 4,
      "right": 4,
      "bottom": 4
    },
    "class_type": "FeatherMask",
    "_meta": {"title": "Feather Mask"}
  },
  "13": {
    "inputs": {
      "samples": ["7", 0],
      "mask": ["12", 0]
    },
    "class_type": "SetLatentNoiseMask",
    "_meta": {"title": "Set Latent Noise Mask"}
  },
  "14": {
    "inputs": {
      "text": "%(clean_prompt)s",
      "clip": ["5", 1]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {"title": "CLIP Text Encode (Prompt)"}
  },
  "15": {
    "inputs": {
      "text": "distorted, warped, disfigured, deformed, mutated, extra fingers, ugly, missing fingers, censored, blury face, ugly face, low quality, blury, bad anatomy, wrong proportions, extra limbs, missing arms, bad hands, poorly drawn",
      "clip": ["5", 1]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {"title": "CLIP Text Encode (Negative)"}
  },
  "16": {
    "inputs": {
      "seed": %(random_seed)d,
      "steps": 20,
      "cfg": 7,
      "sampler_name": "dpmpp_2m",
      "scheduler": "karras",
      "denoise": %(denoise)s,
      "model": ["5", 0],
      "positive": ["14", 0],
      "negative": ["15", 0],
      "latent_image": ["13", 0]
    },
    "class_type": "KSampler",
    "_meta": {"title": "KSampler"}
  },
  "17": {
    "inputs": {
      "samples": ["16", 0],
      "vae": ["5", 2]
    },
    "class_type": "VAEDecode",
    "_meta": {"title": "VAE Decode"}
  },
  "18": {
    "inputs": {
      "filename_prefix": "ComfyUI_inpaint",
      "images": ["17", 0]
    },
    "class_type": "SaveImage",
    "_meta": {"title": "Save Image"}
  }
}
""" % {"random_seed": random_seed, "model": model, "clean_prompt": clean_prompt,
       "uploaded_filename": uploaded_filename, "detect_prompt": detect_prompt, "denoise": denoise}

    try:
        print(f"Submitting inpaint request to ComfyUI...")
        print(f"Using model: {model}")
        print(f"Detecting: {detect_prompt}")
        print(f"Denoise: {denoise}")
        print(f"Prompt: {clean_prompt[:100]}{'...' if len(clean_prompt) > 100 else ''}")

        prompt = json.loads(workflow_inpaint)
        payload = {"prompt": prompt}
        data_bytes = json.dumps(payload).encode("utf-8")

        req = request.Request(f"{COMFYUI_API_ENDPOINT}/prompt", data=data_bytes, headers={"Content-Type": "application/json"})
        response = request.urlopen(req, timeout=30)
        result = json.loads(response.read())

        if 'prompt_id' not in result:
            print(f"ComfyUI did not return a prompt_id. Response: {result}")
            return None

        prompt_id = result['prompt_id']
        print(f"ComfyUI accepted inpaint request, prompt_id: {prompt_id}")
        return fetch_image_from_history(prompt_id)

    except Exception as e:
        print(f"Error generating inpaint: {e}")
        import traceback
        traceback.print_exc()
        return None


def generate_inpaint_with_retries(prompt_text, source_image_bytes, detect_prompt="person", denoise=0.75, max_retries=5, retry_delay=30):
    """
    Generate inpainted image with retries.
    Uses exponential backoff with base delay of retry_delay.
    """
    if max_retries is None:
        max_retries = 5
    attempt = 0
    max_delay = 300

    # Upload image ONCE before retry loop
    print(f"Uploading source image to ComfyUI for inpainting...")
    uploaded_filename = upload_image_to_comfyui(source_image_bytes)
    if not uploaded_filename:
        print("Failed to upload source image for inpainting")
        return None

    while attempt < max_retries:
        attempt += 1
        print(f"Inpaint generation attempt {attempt}...")

        try:
            image_bytes = _generate_inpaint_with_uploaded(prompt_text, uploaded_filename, detect_prompt, denoise)
            if image_bytes:
                print(f"Inpaint generation successful on attempt {attempt}")
                return image_bytes
            else:
                print(f"Inpaint generation returned None on attempt {attempt}")
        except Exception as e:
            print(f"Inpaint generation exception on attempt {attempt}: {e}")
            import traceback
            traceback.print_exc()

        delay = min(retry_delay * (2 ** (attempt - 1)), max_delay)
        print(f"Retrying in {delay} seconds...")
        time.sleep(delay)

    print(f"Inpaint generation failed after {max_retries} attempts")
    return None


def generate_body_inpaint(prompt_text, source_image_bytes, denoise=0.55, negative_prompt=None):
    """
    Generate an inpainted image that modifies BODY only, preserving FACE.
    Uses SAM to detect face, inverts the mask, then inpaints the body.
    """
    uploaded_filename = upload_image_to_comfyui(source_image_bytes)
    if not uploaded_filename:
        print("Failed to upload source image for body inpainting")
        return None
    return _generate_body_inpaint_with_uploaded(prompt_text, uploaded_filename, denoise, negative_prompt)


def _generate_body_inpaint_with_uploaded(prompt_text, uploaded_filename, denoise=0.55, negative_prompt=None):
    """
    Internal function: Generate body inpaint using an already-uploaded image.
    Detects face, inverts mask, inpaints body only.
    """
    clean_prompt = sanitize_prompt(re.sub(r'\bregen\b', '', prompt_text, flags=re.IGNORECASE).strip())
    random_seed = random.getrandbits(31)

    if "anime" in clean_prompt.lower():
        model = ANIME_IMAGE_MODEL
    else:
        model = BASIC_IMAGE_MODEL

    if not is_valid_model_name(model):
        print(f"[BODY INPAINT] Invalid model name: {model}")
        return None

    base_negative = "distorted, warped, disfigured, deformed, mutated, extra fingers, ugly, missing fingers, censored, blury face, ugly face, low quality, blury, bad anatomy, wrong proportions, extra limbs, missing arms, bad hands, poorly drawn"
    if negative_prompt:
        full_negative = f"{base_negative}, {negative_prompt}"
    else:
        full_negative = base_negative

    # Same workflow as inpaint but with InvertMask node (node 19) after FeatherMask
    workflow_body_inpaint = """
{
  "1": {
    "inputs": {
      "image": "%(uploaded_filename)s",
      "upload": "image"
    },
    "class_type": "LoadImage",
    "_meta": {"title": "Load Image"}
  },
  "2": {
    "inputs": {
      "model_name": "sam_vit_b (375MB)"
    },
    "class_type": "SAMModelLoader (segment anything)",
    "_meta": {"title": "SAM Model Loader"}
  },
  "3": {
    "inputs": {
      "model_name": "GroundingDINO_SwinT_OGC (694MB)"
    },
    "class_type": "GroundingDinoModelLoader (segment anything)",
    "_meta": {"title": "GroundingDINO Model Loader"}
  },
  "4": {
    "inputs": {
      "sam_model": ["2", 0],
      "grounding_dino_model": ["3", 0],
      "image": ["1", 0],
      "prompt": "face",
      "threshold": 0.3
    },
    "class_type": "GroundingDinoSAMSegment (segment anything)",
    "_meta": {"title": "Segment Face"}
  },
  "5": {
    "inputs": {
      "ckpt_name": "%(model)s"
    },
    "class_type": "CheckpointLoaderSimple",
    "_meta": {"title": "Load Checkpoint"}
  },
  "6": {
    "inputs": {
      "upscale_method": "lanczos",
      "width": 1024,
      "height": 1024,
      "crop": "center",
      "image": ["1", 0]
    },
    "class_type": "ImageScale",
    "_meta": {"title": "Scale Image"}
  },
  "7": {
    "inputs": {
      "pixels": ["6", 0],
      "vae": ["5", 2]
    },
    "class_type": "VAEEncode",
    "_meta": {"title": "VAE Encode"}
  },
  "8": {
    "inputs": {
      "mask": ["4", 1]
    },
    "class_type": "MaskToImage",
    "_meta": {"title": "Mask To Image"}
  },
  "9": {
    "inputs": {
      "upscale_method": "nearest-exact",
      "width": 1024,
      "height": 1024,
      "crop": "center",
      "image": ["8", 0]
    },
    "class_type": "ImageScale",
    "_meta": {"title": "Scale Mask Image"}
  },
  "10": {
    "inputs": {
      "channel": "red",
      "image": ["9", 0]
    },
    "class_type": "ImageToMask",
    "_meta": {"title": "Image To Mask"}
  },
  "11": {
    "inputs": {
      "mask": ["10", 0],
      "expand": 20,
      "tapered_corners": true
    },
    "class_type": "GrowMask",
    "_meta": {"title": "Grow Mask (expand face area)"}
  },
  "12": {
    "inputs": {
      "mask": ["11", 0],
      "left": 10,
      "top": 10,
      "right": 10,
      "bottom": 10
    },
    "class_type": "FeatherMask",
    "_meta": {"title": "Feather Mask"}
  },
  "19": {
    "inputs": {
      "mask": ["12", 0]
    },
    "class_type": "InvertMask",
    "_meta": {"title": "Invert Mask (protect face, modify body)"}
  },
  "13": {
    "inputs": {
      "samples": ["7", 0],
      "mask": ["19", 0]
    },
    "class_type": "SetLatentNoiseMask",
    "_meta": {"title": "Set Latent Noise Mask"}
  },
  "14": {
    "inputs": {
      "text": "%(clean_prompt)s",
      "clip": ["5", 1]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {"title": "CLIP Text Encode (Prompt)"}
  },
  "15": {
    "inputs": {
      "text": "%(full_negative)s",
      "clip": ["5", 1]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {"title": "CLIP Text Encode (Negative)"}
  },
  "16": {
    "inputs": {
      "seed": %(random_seed)d,
      "steps": 20,
      "cfg": 7,
      "sampler_name": "dpmpp_2m",
      "scheduler": "karras",
      "denoise": %(denoise)s,
      "model": ["5", 0],
      "positive": ["14", 0],
      "negative": ["15", 0],
      "latent_image": ["13", 0]
    },
    "class_type": "KSampler",
    "_meta": {"title": "KSampler"}
  },
  "17": {
    "inputs": {
      "samples": ["16", 0],
      "vae": ["5", 2]
    },
    "class_type": "VAEDecode",
    "_meta": {"title": "VAE Decode"}
  },
  "18": {
    "inputs": {
      "filename_prefix": "ComfyUI_body_inpaint",
      "images": ["17", 0]
    },
    "class_type": "SaveImage",
    "_meta": {"title": "Save Image"}
  }
}
""" % {"random_seed": random_seed, "model": model, "clean_prompt": clean_prompt,
       "uploaded_filename": uploaded_filename, "denoise": denoise, "full_negative": full_negative}

    try:
        print(f"[BODY INPAINT] Submitting request to ComfyUI...")
        print(f"[BODY INPAINT] Using model: {model}")
        print(f"[BODY INPAINT] Denoise: {denoise}")
        print(f"[BODY INPAINT] Prompt: {clean_prompt[:100]}{'...' if len(clean_prompt) > 100 else ''}")

        prompt = json.loads(workflow_body_inpaint)
        payload = {"prompt": prompt}
        data_bytes = json.dumps(payload).encode("utf-8")

        req = request.Request(f"{COMFYUI_API_ENDPOINT}/prompt", data=data_bytes, headers={"Content-Type": "application/json"})
        response = request.urlopen(req, timeout=30)
        result = json.loads(response.read())

        if 'prompt_id' not in result:
            print(f"[BODY INPAINT] ComfyUI did not return a prompt_id. Response: {result}")
            return None

        prompt_id = result['prompt_id']
        print(f"[BODY INPAINT] ComfyUI accepted request, prompt_id: {prompt_id}")
        return fetch_image_from_history(prompt_id)

    except Exception as e:
        print(f"[BODY INPAINT] Error: {e}")
        import traceback
        traceback.print_exc()
        return None


def generate_body_inpaint_with_retries(prompt_text, source_image_bytes, denoise=0.55, negative_prompt=None, max_retries=5, retry_delay=30):
    """
    Generate body-only inpainted image with retries.
    Preserves face while modifying body.
    """
    if max_retries is None:
        max_retries = 5
    attempt = 0
    max_delay = 300

    print(f"[BODY INPAINT] Uploading source image...")
    uploaded_filename = upload_image_to_comfyui(source_image_bytes)
    if not uploaded_filename:
        print("[BODY INPAINT] Failed to upload source image")
        return None

    while attempt < max_retries:
        attempt += 1
        print(f"[BODY INPAINT] Generation attempt {attempt}...")

        try:
            image_bytes = _generate_body_inpaint_with_uploaded(prompt_text, uploaded_filename, denoise, negative_prompt)
            if image_bytes:
                print(f"[BODY INPAINT] Generation successful on attempt {attempt}")
                return image_bytes
            else:
                print(f"[BODY INPAINT] Generation returned None on attempt {attempt}")
        except Exception as e:
            print(f"[BODY INPAINT] Exception on attempt {attempt}: {e}")
            import traceback
            traceback.print_exc()

        delay = min(retry_delay * (2 ** (attempt - 1)), max_delay)
        print(f"[BODY INPAINT] Retrying in {delay} seconds...")
        time.sleep(delay)

    print(f"[BODY INPAINT] Failed after {max_retries} attempts")
    return None


def generate_img2img_with_face_restore(prompt_text, source_image_bytes, denoise=0.55, negative_prompt=None, max_retries=3):
    """
    Generate img2img then composite the original face back on top.
    1. Run normal img2img for body modification
    2. Detect face in ORIGINAL image using SAM
    3. Blend original face onto modified image
    """
    # Upload original image
    original_filename = upload_image_to_comfyui(source_image_bytes)
    if not original_filename:
        print("[FACE RESTORE] Failed to upload original image")
        return None

    # First, run normal img2img
    print("[FACE RESTORE] Step 1: Running img2img...")
    modified_image = generate_img2img_bytes_with_retries(
        prompt_text, source_image_bytes, denoise=denoise,
        max_retries=max_retries, negative_prompt=negative_prompt
    )
    if not modified_image:
        print("[FACE RESTORE] img2img failed")
        return None

    # Upload modified image
    modified_filename = upload_image_to_comfyui(modified_image)
    if not modified_filename:
        print("[FACE RESTORE] Failed to upload modified image")
        return modified_image  # Return unrestored image as fallback

    # Now composite original face onto modified image
    print("[FACE RESTORE] Step 2: Compositing original face...")
    return _composite_face(original_filename, modified_filename)


def _composite_face(original_filename, modified_filename):
    """
    Detect face in original, blend onto modified image.
    Uses SAM to detect face, then ImageCompositeMasked to blend.
    """
    random_seed = random.getrandbits(31)

    workflow_composite = """
{
  "1": {
    "inputs": {
      "image": "%(original_filename)s",
      "upload": "image"
    },
    "class_type": "LoadImage",
    "_meta": {"title": "Load Original Image"}
  },
  "2": {
    "inputs": {
      "image": "%(modified_filename)s",
      "upload": "image"
    },
    "class_type": "LoadImage",
    "_meta": {"title": "Load Modified Image"}
  },
  "3": {
    "inputs": {
      "model_name": "sam_vit_b (375MB)"
    },
    "class_type": "SAMModelLoader (segment anything)",
    "_meta": {"title": "SAM Model Loader"}
  },
  "4": {
    "inputs": {
      "model_name": "GroundingDINO_SwinT_OGC (694MB)"
    },
    "class_type": "GroundingDinoModelLoader (segment anything)",
    "_meta": {"title": "GroundingDINO Model Loader"}
  },
  "5": {
    "inputs": {
      "sam_model": ["3", 0],
      "grounding_dino_model": ["4", 0],
      "image": ["1", 0],
      "prompt": "face, head",
      "threshold": 0.3
    },
    "class_type": "GroundingDinoSAMSegment (segment anything)",
    "_meta": {"title": "Segment Face in Original"}
  },
  "6": {
    "inputs": {
      "mask": ["5", 1]
    },
    "class_type": "MaskToImage",
    "_meta": {"title": "Mask To Image"}
  },
  "7": {
    "inputs": {
      "upscale_method": "lanczos",
      "width": 1024,
      "height": 1024,
      "crop": "center",
      "image": ["1", 0]
    },
    "class_type": "ImageScale",
    "_meta": {"title": "Scale Original"}
  },
  "8": {
    "inputs": {
      "upscale_method": "lanczos",
      "width": 1024,
      "height": 1024,
      "crop": "center",
      "image": ["2", 0]
    },
    "class_type": "ImageScale",
    "_meta": {"title": "Scale Modified"}
  },
  "9": {
    "inputs": {
      "upscale_method": "nearest-exact",
      "width": 1024,
      "height": 1024,
      "crop": "center",
      "image": ["6", 0]
    },
    "class_type": "ImageScale",
    "_meta": {"title": "Scale Mask"}
  },
  "10": {
    "inputs": {
      "channel": "red",
      "image": ["9", 0]
    },
    "class_type": "ImageToMask",
    "_meta": {"title": "Image To Mask"}
  },
  "11": {
    "inputs": {
      "mask": ["10", 0],
      "expand": 10,
      "tapered_corners": true
    },
    "class_type": "GrowMask",
    "_meta": {"title": "Grow Face Mask"}
  },
  "12": {
    "inputs": {
      "mask": ["11", 0],
      "left": 8,
      "top": 8,
      "right": 8,
      "bottom": 8
    },
    "class_type": "FeatherMask",
    "_meta": {"title": "Feather Mask"}
  },
  "13": {
    "inputs": {
      "destination": ["8", 0],
      "source": ["7", 0],
      "x": 0,
      "y": 0,
      "resize_source": false,
      "mask": ["12", 0]
    },
    "class_type": "ImageCompositeMasked",
    "_meta": {"title": "Composite Original Face onto Modified"}
  },
  "14": {
    "inputs": {
      "filename_prefix": "ComfyUI_face_restore",
      "images": ["13", 0]
    },
    "class_type": "SaveImage",
    "_meta": {"title": "Save Image"}
  }
}
""" % {"original_filename": original_filename, "modified_filename": modified_filename}

    try:
        print(f"[FACE RESTORE] Submitting composite request...")
        prompt = json.loads(workflow_composite)
        payload = {"prompt": prompt}
        data_bytes = json.dumps(payload).encode("utf-8")

        req = request.Request(f"{COMFYUI_API_ENDPOINT}/prompt", data=data_bytes, headers={"Content-Type": "application/json"})
        response = request.urlopen(req, timeout=30)
        result = json.loads(response.read())

        if 'prompt_id' not in result:
            print(f"[FACE RESTORE] No prompt_id returned: {result}")
            return None

        prompt_id = result['prompt_id']
        print(f"[FACE RESTORE] ComfyUI accepted, prompt_id: {prompt_id}")
        return fetch_image_from_history(prompt_id)

    except Exception as e:
        print(f"[FACE RESTORE] Error: {e}")
        import traceback
        traceback.print_exc()
        return None


def fetch_image_from_history(prompt_id, max_wait_seconds=300, poll_interval=5):
    """
    Poll ComfyUI for completed image with timeout.
    max_wait_seconds: Maximum time to wait (default 5 minutes)
    poll_interval: Time between polls (default 5 seconds)
    """
    # Validate parameters to prevent division by zero and ensure reasonable values
    poll_interval = max(1, poll_interval)
    max_wait_seconds = max(poll_interval, max_wait_seconds)

    start_time = time.time()
    attempts = 0
    max_attempts = max_wait_seconds // poll_interval

    print(f"Waiting for image generation (prompt_id: {prompt_id}, max wait: {max_wait_seconds}s)...")

    while attempts < max_attempts:
        attempts += 1
        elapsed = time.time() - start_time

        try:
            time.sleep(poll_interval)
            history_req = request.urlopen(f"{COMFYUI_API_ENDPOINT}/history/{prompt_id}", timeout=30)
            history = json.loads(history_req.read())

            if prompt_id in history:
                status_info = history[prompt_id].get('status', {})

                # Check if job failed
                if status_info.get('status_str') == 'error':
                    print(f"ComfyUI job failed with error: {status_info}")
                    return None

                # Check if completed
                if status_info.get('completed', False):
                    output = history[prompt_id].get('outputs', {})
                    if not output:
                        print("ComfyUI completed but no outputs found")
                        return None

                    image_node_id = next(iter(output), None)
                    if not image_node_id:
                        print("No image node found in outputs")
                        return None

                    images = output[image_node_id].get('images', [])
                    if not images:
                        print("No images in output node")
                        return None

                    image_data = images[0]
                    filename = image_data.get('filename', '')
                    subfolder = image_data.get('subfolder', '')
                    img_type = image_data.get('type', '')

                    # Validate path components to prevent path traversal
                    if not is_safe_path_component(filename):
                        print(f"Invalid filename received: {filename}")
                        return None
                    if subfolder and not is_safe_path_component(subfolder):
                        print(f"Invalid subfolder received: {subfolder}")
                        return None

                    # URL-encode parameters for safety
                    image_url = f"{COMFYUI_API_ENDPOINT}/view?filename={quote(filename)}&subfolder={quote(subfolder)}&type={quote(img_type)}"
                    print(f"Image ready after {elapsed:.1f}s, fetching from: {image_url}")
                    image_response = request.urlopen(image_url, timeout=60)
                    return image_response.read()

            # Progress logging every 30 seconds
            if attempts % 6 == 0:
                print(f"Still waiting for image... ({elapsed:.0f}s elapsed, attempt {attempts}/{max_attempts})")

        except Exception as e:
            print(f"Error polling ComfyUI history (attempt {attempts}): {e}")
            # Continue polling unless we've exceeded max attempts
            if attempts >= max_attempts:
                break

    print(f"Image generation timed out after {max_wait_seconds} seconds")
    return None