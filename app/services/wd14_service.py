"""
WD14 Tagger Service - Image tagging using WD14 model.
Uses ONNX runtime for efficient inference on CPU/GPU.
"""
import io
import logging
import os
from typing import Optional, Dict, List, Tuple
from PIL import Image
import numpy as np

logger = logging.getLogger("wd14")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [WD14] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)

# Global state
_model = None
_tags = None
_model_path = None


def _get_model_path() -> str:
    """Get the path to store the WD14 model."""
    # Use the posterchanai models directory
    models_dir = os.path.join(os.path.dirname(__file__), "../../models/wd14")
    os.makedirs(models_dir, exist_ok=True)
    return models_dir


def _download_model():
    """Download the WD14 model if not present."""
    global _model_path

    model_dir = _get_model_path()
    model_file = os.path.join(model_dir, "model.onnx")
    tags_file = os.path.join(model_dir, "selected_tags.csv")

    if os.path.exists(model_file) and os.path.exists(tags_file):
        _model_path = model_dir
        return True

    logger.info("Downloading WD14 model...")
    try:
        from huggingface_hub import hf_hub_download

        # Download from SmilingWolf's WD14 tagger
        repo_id = "SmilingWolf/wd-v1-4-moat-tagger-v2"

        hf_hub_download(
            repo_id=repo_id,
            filename="model.onnx",
            local_dir=model_dir,
            local_dir_use_symlinks=False
        )

        hf_hub_download(
            repo_id=repo_id,
            filename="selected_tags.csv",
            local_dir=model_dir,
            local_dir_use_symlinks=False
        )

        _model_path = model_dir
        logger.info("WD14 model downloaded successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to download WD14 model: {e}")
        return False


def _load_model():
    """Load the ONNX model and tags."""
    global _model, _tags

    if _model is not None:
        return True

    if not _download_model():
        return False

    try:
        import onnxruntime as ort
        import csv

        model_file = os.path.join(_model_path, "model.onnx")
        tags_file = os.path.join(_model_path, "selected_tags.csv")

        # Load ONNX model
        logger.info("Loading WD14 ONNX model...")

        # Try GPU first, fall back to CPU
        providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        try:
            _model = ort.InferenceSession(model_file, providers=providers)
        except Exception:
            _model = ort.InferenceSession(model_file, providers=['CPUExecutionProvider'])

        # Load tags
        _tags = []
        with open(tags_file, 'r') as f:
            reader = csv.reader(f)
            next(reader)  # Skip header
            for row in reader:
                if len(row) >= 2:
                    _tags.append({
                        'name': row[0],
                        'category': int(row[1]) if len(row) > 1 else 0
                    })

        logger.info(f"WD14 model loaded with {len(_tags)} tags")
        return True

    except Exception as e:
        logger.error(f"Failed to load WD14 model: {e}")
        _model = None
        _tags = None
        return False


def _preprocess_image(image: Image.Image) -> np.ndarray:
    """Preprocess image for WD14 model."""
    # Resize to 448x448 (WD14 input size)
    image = image.convert('RGB')
    image = image.resize((448, 448), Image.Resampling.LANCZOS)

    # Convert to numpy array and normalize
    arr = np.array(image, dtype=np.float32)

    # BGR order (WD14 uses BGR)
    arr = arr[:, :, ::-1]

    # Add batch dimension
    arr = np.expand_dims(arr, 0)

    return arr


def tag_image(image_bytes: bytes, threshold: float = 0.35,
              general_threshold: float = None,
              character_threshold: float = None) -> Optional[str]:
    """
    Tag an image using WD14 model.

    Args:
        image_bytes: Raw image bytes
        threshold: Default confidence threshold for all tags
        general_threshold: Threshold for general tags (overrides threshold)
        character_threshold: Threshold for character tags (overrides threshold)

    Returns:
        Comma-separated string of tags, or None on failure
    """
    if not _load_model():
        logger.error("WD14 model not available")
        return None

    general_threshold = general_threshold or threshold
    character_threshold = character_threshold or threshold

    try:
        # Load and preprocess image
        image = Image.open(io.BytesIO(image_bytes))
        input_data = _preprocess_image(image)

        # Run inference
        input_name = _model.get_inputs()[0].name
        output = _model.run(None, {input_name: input_data})[0]

        # Process results
        probs = output[0]

        # Separate tags by category
        # Category 0: General tags
        # Category 4: Character tags
        # Category 9: Rating tags

        general_tags = []
        character_tags = []
        rating_tags = []

        for i, prob in enumerate(probs):
            if i >= len(_tags):
                break

            tag = _tags[i]
            category = tag['category']
            name = tag['name']

            if category == 0:  # General
                if prob >= general_threshold:
                    general_tags.append((name, prob))
            elif category == 4:  # Character
                if prob >= character_threshold:
                    character_tags.append((name, prob))
            elif category == 9:  # Rating
                if prob >= threshold:
                    rating_tags.append((name, prob))

        # Sort by probability
        general_tags.sort(key=lambda x: x[1], reverse=True)
        character_tags.sort(key=lambda x: x[1], reverse=True)

        # Combine tags (skip character names as they can override attributes)
        all_tags = []

        # Add rating first
        for tag, _ in rating_tags:
            all_tags.append(tag)

        # Add general tags
        for tag, _ in general_tags:
            all_tags.append(tag)

        result = ", ".join(all_tags)
        logger.info(f"Tagged image: {result[:100]}...")
        return result

    except Exception as e:
        logger.error(f"Error tagging image: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_tag_details(image_bytes: bytes, threshold: float = 0.35) -> Optional[Dict]:
    """
    Get detailed tag information including probabilities.

    Returns:
        Dict with 'general', 'character', 'rating' lists of (tag, probability) tuples
    """
    if not _load_model():
        return None

    try:
        image = Image.open(io.BytesIO(image_bytes))
        input_data = _preprocess_image(image)

        input_name = _model.get_inputs()[0].name
        output = _model.run(None, {input_name: input_data})[0]
        probs = output[0]

        result = {
            'general': [],
            'character': [],
            'rating': []
        }

        for i, prob in enumerate(probs):
            if i >= len(_tags):
                break

            if prob < threshold:
                continue

            tag = _tags[i]
            category = tag['category']
            name = tag['name']

            if category == 0:
                result['general'].append({'tag': name, 'probability': float(prob)})
            elif category == 4:
                result['character'].append({'tag': name, 'probability': float(prob)})
            elif category == 9:
                result['rating'].append({'tag': name, 'probability': float(prob)})

        # Sort by probability
        for key in result:
            result[key].sort(key=lambda x: x['probability'], reverse=True)

        return result

    except Exception as e:
        logger.error(f"Error getting tag details: {e}")
        return None


def is_available() -> bool:
    """Check if WD14 tagging is available."""
    return _load_model()
