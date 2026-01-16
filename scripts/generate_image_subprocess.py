#!/usr/bin/env python3
"""
Subprocess image generator for Intel XPU.
Runs as a separate process to ensure GPU memory is fully released on exit.
Usage: python generate_image_subprocess.py <config_json>
Output: JSON with base64 image or error
"""
import sys
import json
import gc
import warnings
import os
from PIL import Image

# Suppress warnings that would pollute stdout/stderr
warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

# Suppress PyTorch kernel registration warnings (they're harmless)
os.environ["PYTORCH_DISABLE_RUNNING_SCRIPT_CHK"] = "1"

# Try to import numpy for image validation
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


def is_image_blank(image: Image.Image, threshold: float = 0.99) -> bool:
    """
    Check if an image is blank (all pixels are the same or very similar).
    
    Args:
        image: PIL Image to check
        threshold: Threshold for considering image blank (0.99 = 99% of pixels must be similar)
    
    Returns:
        True if image appears blank, False otherwise
    """
    if not HAS_NUMPY:
        # Fallback: basic check using PIL histogram
        try:
            # Convert to RGB if needed
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            # Get histogram - if all pixels are the same, histogram will have very few non-zero bins
            hist = image.histogram()
            non_zero_bins = sum(1 for h in hist if h > 0)
            # For RGB, we have 256 bins per channel = 768 total
            # If most bins are zero, image is likely blank
            # Also check image size - very small images might have few bins but not be blank
            width, height = image.size
            if width * height < 100:  # Very small images (< 10x10) - be more lenient
                # For tiny images, require even fewer colors to be considered blank
                return non_zero_bins < 3
            return non_zero_bins < 10  # Very few colors for normal-sized images
        except Exception:
            return False
    
    try:
        # Convert to RGB if needed (handles RGBA, L, etc.)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        # Convert to numpy array
        img_array = np.array(image)
        
        # Check if image has valid dimensions
        if img_array.size == 0:
            return True
        
        # Check image dimensions are reasonable
        if len(img_array.shape) < 2 or img_array.shape[0] == 0 or img_array.shape[1] == 0:
            return True
        
        # Calculate standard deviation for each channel
        std_dev = np.std(img_array, axis=(0, 1))
        
        # If all channels have very low variance, image is likely blank
        max_std = np.max(std_dev)
        
        # Also check if most pixels are the same color
        # For large images, sample pixels to avoid memory issues with np.unique()
        pixels = img_array.reshape(-1, 3)
        total_pixels = pixels.shape[0]
        
        # Sample pixels for large images to avoid expensive np.unique() on millions of pixels
        max_sample_size = 100000
        if total_pixels > max_sample_size:
            # Randomly sample pixels
            sample_indices = np.random.choice(total_pixels, max_sample_size, replace=False)
            sample_pixels = pixels[sample_indices]
            unique_colors_in_sample = len(np.unique(sample_pixels, axis=0))
            # Use the ratio in the sample as an estimate for the full image
            # If 50 unique colors in 100k sample, ratio is 50/100k = 0.0005
            unique_ratio = unique_colors_in_sample / max_sample_size
            # For consistency with main service (though not used in subprocess logging)
            unique_colors = int(unique_colors_in_sample * (total_pixels / max_sample_size))
        else:
            unique_colors = len(np.unique(pixels, axis=0))
            unique_ratio = unique_colors / total_pixels if total_pixels > 0 else 0
        
        # Image is blank if:
        # 1. Very low standard deviation (all pixels nearly identical) - std_dev < 5.0
        # 2. Very few unique colors - less than (1 - threshold) of pixels are unique
        is_blank = max_std < 5.0 or unique_ratio < (1.0 - threshold)
        
        return is_blank
        
    except Exception:
        # If we can't check, assume it's not blank to avoid false positives
        return False

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No config provided"}))
        sys.exit(1)

    try:
        config = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON config: {e}"}))
        sys.exit(1)

    model_path = config.get("model_path")
    model_type = config.get("model_type", "sdxl")
    prompt = config.get("prompt", "")
    negative_prompt = config.get("negative_prompt", "")
    width = config.get("width", 1024)
    height = config.get("height", 1024)
    steps = config.get("steps", 20)
    cfg = config.get("cfg", 7.0)
    seed = config.get("seed")
    device = config.get("device", "xpu")

    if not model_path:
        print(json.dumps({"error": "No model_path provided"}))
        sys.exit(1)

    if not prompt:
        print(json.dumps({"error": "No prompt provided"}))
        sys.exit(1)

    try:
        # Import torch - kernel registration warnings are harmless and won't cause failures
        import torch
        # Initialize XPU early to get kernel registration warnings out of the way
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            try:
                # Create a dummy tensor to trigger kernel registration
                _ = torch.zeros(1, device="xpu")
            except Exception:
                pass  # Ignore any errors during initialization
        
        import base64
        import io
        import random
        from diffusers import StableDiffusionPipeline, StableDiffusionXLPipeline

        # Determine dtype
        if device == "cpu":
            dtype = torch.float32
        else:
            dtype = torch.float16

        # Load model
        if model_path.endswith(".safetensors") or model_path.endswith(".ckpt"):
            if model_type == "sdxl":
                pipe = StableDiffusionXLPipeline.from_single_file(
                    model_path,
                    torch_dtype=dtype,
                    use_safetensors=model_path.endswith(".safetensors")
                )
            else:
                pipe = StableDiffusionPipeline.from_single_file(
                    model_path,
                    torch_dtype=dtype,
                    use_safetensors=model_path.endswith(".safetensors")
                )
        else:
            from diffusers import AutoPipelineForText2Image
            pipe = AutoPipelineForText2Image.from_pretrained(
                model_path,
                torch_dtype=dtype,
            )

        # Move to device
        pipe = pipe.to(device)

        # Enable optimizations
        try:
            pipe.enable_attention_slicing()
        except Exception:
            pass
        try:
            pipe.enable_vae_slicing()
        except Exception:
            pass
        try:
            pipe.enable_vae_tiling()
        except Exception:
            pass

        # Generate seed
        if seed is None or seed < 0:
            seed = random.randint(0, 2**32 - 1)

        # For XPU, use CPU generator
        gen_device = "cpu" if device == "xpu" else device
        generator = torch.Generator(device=gen_device).manual_seed(seed)

        # Default negative prompt
        if not negative_prompt:
            negative_prompt = "bad quality, blurry, distorted, ugly, deformed, low resolution"

        # Generate
        result = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_inference_steps=steps,
            guidance_scale=cfg,
            generator=generator,
        )

        # Validate result contains images
        if not result.images or len(result.images) == 0:
            print(json.dumps({"error": "Pipeline returned no images"}))
            sys.exit(1)

        image = result.images[0]

        # Validate image is not blank
        if is_image_blank(image):
            print(json.dumps({"error": "Generated image is blank"}))
            sys.exit(1)

        # Convert to base64
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='PNG')
        img_bytes = img_byte_arr.getvalue()
        
        # Validate bytes are not empty
        if not img_bytes or len(img_bytes) < 100:  # PNG header is ~100 bytes minimum
            print(json.dumps({"error": f"Generated image bytes are empty or too small: {len(img_bytes)} bytes"}))
            sys.exit(1)
        
        img_base64 = base64.b64encode(img_bytes).decode()

        # Cleanup before exit
        del result
        del pipe
        gc.collect()

        if hasattr(torch, "xpu") and torch.xpu.is_available():
            torch.xpu.synchronize()
            torch.xpu.empty_cache()
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()

        print(json.dumps({"image": img_base64, "seed": seed}))

    except Exception as e:
        import traceback
        error_msg = str(e)
        # Include traceback for debugging, but truncate if too long
        tb = traceback.format_exc()
        if len(tb) > 1000:
            tb = tb[:1000] + "... (truncated)"
        print(json.dumps({"error": error_msg, "traceback": tb}))
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        error_msg = f"Fatal error in subprocess: {str(e)}"
        tb = traceback.format_exc()
        if len(tb) > 1000:
            tb = tb[:1000] + "... (truncated)"
        print(json.dumps({"error": error_msg, "traceback": tb}), file=sys.stderr)
        sys.exit(1)
