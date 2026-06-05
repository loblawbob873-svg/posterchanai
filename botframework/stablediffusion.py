import base64
import json
import time
import requests
from config import STABLE_DIFFUSION_ENDPOINT


def generate_image_bytes_with_retries(prompt, max_retries=None, retry_delay=30):
    """
    Generate image with retries (max_retries if specified, defaults to 50).
    Will keep trying until successful to ensure no requests are missed.
    """
    # Cap max_retries to prevent infinite loops
    if max_retries is None:
        max_retries = 50
    attempt = 0
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

        print(f"Retrying in {retry_delay} seconds...")
        time.sleep(retry_delay)

    print(f"Image generation failed after {max_retries} attempts")
    return None


def generate_image_bytes(prompt):
    # Remove any trigger token from prompt
    clean_prompt = prompt.replace("geni", "").strip()
    payload = {
        "prompt": clean_prompt,
        "negative_prompt": "extra fingers, ugly, disfigured, missing fingers, censored, blury face, ugly face, low  quality, blurry, low res,low resolution,Cropped, Out of frame, Out of focus, watermark, banner, extra digits, Jpeg artifacts, Grainy, Bad anatomy, Bad proportions, Deformed, Disconnected limbs, Disfigured, Extra arms, Extra limbs, Extra hands, Fused fingers, Gross proportions, Long neck, Malformed limbs, Mutated, Mutated hands, Mutated limbs, Missing arms, Missing fingers, Poorly drawn hands, Poorly drawn face,low saturation,harsh lighting,underexposed, bad photography,bad photo.",
        "width": 1024,
        "height": 1024,
        "steps": 40,
        "cfg_scale": 11,
        "sampler_name": "DPM++ 2M SDE",
    }
    url = STABLE_DIFFUSION_ENDPOINT
    headers = {"Content-Type": "application/json"}
    max_retries = 15  # Number of times to retry
    retry_delay = 10  # seconds
    resp = None  # Initialize to prevent NameError if all attempts fail

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
            break  # Exit retry loop if successful
        except requests.exceptions.RequestException as e:
            print(
                f"Stable Diffusion request failed on attempt {attempt + 1}/{max_retries}: {e}"
            )
            if attempt < max_retries - 1:
                print(f"Waiting {retry_delay} seconds before retrying...")
                time.sleep(retry_delay)
            else:
                print("Max retries reached for Stable Diffusion. Giving up.")
                return None

    # Safety check: ensure resp was set before accessing
    if resp is None:
        print("No successful response received from Stable Diffusion")
        return None

    try:
        data = resp.json()
    except ValueError:
        print("Stable Diffusion returned non-JSON response:", resp.text)
        return None

    # Expecting base64 images in data["images"]
    images = data.get("images") or []
    if not images:
        print("Stable Diffusion returned no images:", data)
        return None

    b64 = images[0]
    try:
        image_bytes = base64.b64decode(b64)
        return image_bytes
    except Exception as e:
        print("Failed to decode image from Stable Diffusion:", e)
        return None