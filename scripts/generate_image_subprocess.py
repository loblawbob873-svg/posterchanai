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

# Suppress warnings that would pollute stdout/stderr
warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

# Suppress PyTorch kernel registration warnings (they're harmless)
os.environ["PYTORCH_DISABLE_RUNNING_SCRIPT_CHK"] = "1"

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

        image = result.images[0]

        # Convert to base64
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='PNG')
        img_bytes = img_byte_arr.getvalue()
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
