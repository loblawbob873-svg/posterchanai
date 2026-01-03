#!/usr/bin/env python3
"""Test img2img with different parameters."""
import base64
import requests
import sys
from pathlib import Path

API_URL = "http://nas.lan:3051/api/img2img"

def test_img2img(input_path: str, prompt: str, output_path: str, denoise: float = 0.60):
    """Run img2img and save result."""
    # Read input image
    with open(input_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    # Call API
    response = requests.post(
        API_URL,
        json={
            "prompt": prompt,
            "image": image_b64,
            "denoise": denoise,
        },
        timeout=120.0
    )

    data = response.json()
    if data.get("error"):
        print(f"Error: {data['error']}")
        return False

    if data.get("image"):
        result_bytes = base64.b64decode(data["image"])
        with open(output_path, "wb") as f:
            f.write(result_bytes)
        print(f"Saved: {output_path}")
        return True

    print("No image returned")
    return False

if __name__ == "__main__":
    # Test cases
    tests = [
        ("/tmp/blonde_single.jpg", "nude", "/tmp/blonde_inpaint_test.png", 0.65),
    ]

    for input_path, prompt, output_path, denoise in tests:
        if not Path(input_path).exists():
            print(f"Skipping {input_path} - file not found")
            continue
        print(f"\nTesting: {input_path} -> {output_path} (denoise={denoise}, prompt={prompt})")
        test_img2img(input_path, prompt, output_path, denoise)

    print("\nDone! Check /tmp/blonde_*.png and /tmp/anime_*.png")
