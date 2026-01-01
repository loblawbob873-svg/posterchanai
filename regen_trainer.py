#!/usr/bin/env python3
"""
Auto-trainer for regen examples.
Logs regen requests automatically and syncs to training files on demand.
"""

import json
import os
import re
from datetime import datetime

REGEN_LOG_FILE = "/home/verita84/posterchan/regen_log.json"
# Both projects have their own JSON file
IMG2IMG_TRAINING_FILES = [
    "/home/verita84/posterchan/img2img_training.json",
    "/home/verita84/posterchanai/img2img_training.json"
]

def load_regen_log():
    """Load the regen log file."""
    if os.path.exists(REGEN_LOG_FILE):
        try:
            with open(REGEN_LOG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"requests": []}
    return {"requests": []}

def save_regen_log(log):
    """Save the regen log file."""
    with open(REGEN_LOG_FILE, 'w') as f:
        json.dump(log, f, indent=2)

def log_regen_request(source_image_bytes, modification_prompt, source_tags=None):
    """
    Log a regen request for training. Called automatically after successful regen.
    Analyzes source image with WD14 if tags not provided.
    """
    # Get WD14 tags if not provided
    if not source_tags:
        try:
            from comfyui import describe_image_with_wd14
            source_tags = describe_image_with_wd14(source_image_bytes)
            if source_tags:
                print(f"[TRAINER] Got WD14 tags: {source_tags[:80]}...")
        except Exception as e:
            print(f"[TRAINER] Failed to get WD14 tags: {e}")
            return None

    if not source_tags:
        print("[TRAINER] No tags available, skipping log")
        return None

    # Clean up tags
    source_tags = source_tags.strip().rstrip(',').strip()

    log = load_regen_log()

    # Check for duplicate (same tags + modification)
    for entry in log["requests"]:
        if entry.get("source_tags") == source_tags and entry.get("modification") == modification_prompt:
            print(f"[TRAINER] Duplicate entry, skipping")
            return None

    entry = {
        "id": len(log["requests"]) + 1,
        "timestamp": datetime.now().isoformat(),
        "modification": modification_prompt,
        "source_tags": source_tags,
        "synced": False
    }

    log["requests"].append(entry)

    # Keep only last 200 entries
    if len(log["requests"]) > 200:
        log["requests"] = log["requests"][-200:]

    save_regen_log(log)
    print(f"[TRAINER] Logged regen #{entry['id']}: {modification_prompt}")
    return entry["id"]

def generate_training_example(source_tags, modification):
    """Generate the training example text based on the modification type."""
    source_tags = source_tags.strip().rstrip(',').strip()
    mod_lower = modification.lower()

    # Determine denoise based on modification type
    if any(x in mod_lower for x in ['afro', 'ponytail', 'straight hair', 'curly', 'short hair', 'long hair', 'twintails', 'braid']):
        denoise = 0.85
    elif any(x in mod_lower for x in ['skin', 'nude', 'naked', 'hair color', 'blonde', 'red hair', 'black hair', 'white hair']):
        denoise = 0.80
    elif any(x in mod_lower for x in ['beach', 'background', 'scene', 'city', 'forest', 'indoor', 'outdoor']):
        denoise = 0.75
    elif any(x in mod_lower for x in ['holding', 'gun', 'sword', 'food', 'coffee']):
        denoise = 0.70
    elif any(x in mod_lower for x in ['anime', 'realistic', 'style']):
        denoise = 0.65
    elif any(x in mod_lower for x in ['breast', 'chest', 'boob']):
        denoise = 0.50
    else:
        denoise = 0.70

    # Parse source tags
    tag_list = [t.strip() for t in source_tags.split(',') if t.strip()]

    # Build new tags and negative tags
    new_tags = []
    negative_tags = []

    # Handle afro hair style
    if 'afro' in mod_lower:
        new_tags.extend(["(afro:2.5)", "(afro hair:2.5)"])
        for t in tag_list:
            t_lower = t.lower()
            if any(h in t_lower for h in ['long hair', 'short hair', 'straight', 'twintails', 'ponytail', 'braid', 'bangs']):
                negative_tags.append(t)

    # Handle skin color changes
    if 'dark' in mod_lower and 'skin' in mod_lower or 'brown skin' in mod_lower or 'black skin' in mod_lower:
        new_tags.extend(["(dark skin:2.0)", "(brown skin:2.0)", "(dark-skinned female:2.0)"])
        negative_tags.extend(["pale skin", "white skin", "light skin", "fair skin"])
    elif 'white skin' in mod_lower or 'pale skin' in mod_lower or 'light skin' in mod_lower:
        new_tags.extend(["(pale skin:2.0)", "(white skin:2.0)", "(fair skin:2.0)"])
        negative_tags.extend(["dark skin", "tan skin", "brown skin", "black skin"])

    # Handle hair color changes
    hair_colors = {
        'blonde': "(blonde hair:2.0), blonde hair",
        'red hair': "(red hair:2.0), red hair",
        'black hair': "(black hair:2.0), black hair",
        'white hair': "(white hair:2.0), white hair",
        'blue hair': "(blue hair:2.0), blue hair",
        'pink hair': "(pink hair:2.0), pink hair",
        'purple hair': "(purple hair:2.0), purple hair",
        'green hair': "(green hair:2.0), green hair",
        'silver hair': "(silver hair:2.0), silver hair",
    }
    for color, tag in hair_colors.items():
        if color in mod_lower:
            new_tags.append(tag)
            # Add original hair color to negative
            for t in tag_list:
                if 'hair' in t.lower() and any(c in t.lower() for c in ['blonde', 'black', 'brown', 'red', 'blue', 'pink', 'purple', 'green', 'silver', 'white', 'orange']):
                    if color not in t.lower():
                        negative_tags.append(t)

    # Handle nude/naked
    if 'nude' in mod_lower or 'naked' in mod_lower:
        new_tags.extend(["(nude:2.0)", "(naked:2.0)"])
        clothing_keywords = ['dress', 'shirt', 'skirt', 'uniform', 'bikini', 'swimsuit', 'outfit', 'costume',
                           'sweater', 'top', 'pants', 'shorts', 'kimono', 'jacket', 'coat', 'hoodie',
                           'blouse', 'leotard', 'pantyhose', 'bra', 'underwear', 'lingerie', 'clothes']
        for t in tag_list:
            t_lower = t.lower()
            if any(c in t_lower for c in clothing_keywords):
                negative_tags.append(t)
        negative_tags.extend(["clothing", "clothed"])

    # Handle anime style
    if 'anime' in mod_lower:
        new_tags.append("(anime:1.5)")
        if 'realistic' in ' '.join(tag_list).lower():
            negative_tags.extend(["realistic", "photorealistic"])

    # Add remaining original tags (excluding ones we're changing)
    skip_patterns = []
    if any(x in mod_lower for x in ['hair', 'afro']):
        skip_patterns.append('hair')
    if 'skin' in mod_lower:
        skip_patterns.append('skin')

    for t in tag_list:
        t_lower = t.lower()
        skip = False
        for pattern in skip_patterns:
            if pattern in t_lower:
                skip = True
                break
        # Also skip if already in negative
        if t in negative_tags or t.lower() in [n.lower() for n in negative_tags]:
            skip = True
        if not skip:
            new_tags.append(t)

    # Add quality tags
    new_tags.extend(["vibrant colors", "sharp", "high quality"])

    # Add standard negative tags
    negative_tags.extend(["deformed", "extra limbs", "bad anatomy", "blurry", "distorted", "extra people"])

    # Remove duplicates while preserving order
    seen = set()
    unique_new_tags = []
    for t in new_tags:
        t_clean = t.lower().strip()
        if t_clean not in seen:
            seen.add(t_clean)
            unique_new_tags.append(t)

    seen = set()
    unique_negative = []
    for t in negative_tags:
        t_clean = t.lower().strip()
        if t_clean not in seen:
            seen.add(t_clean)
            unique_negative.append(t)

    tags_str = ", ".join(unique_new_tags)
    negative_str = ", ".join(unique_negative)

    example = f'''Tags: "{source_tags}" Change: "{modification}"
DENOISE: {denoise:.2f}
TAGS: {tags_str}
NEGATIVE: {negative_str}'''

    return example

def load_training_json(filepath):
    """Load an img2img training JSON file."""
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[TRAINER] Error loading {filepath}: {e}")
            return None
    return None

def save_training_json(filepath, data):
    """Save an img2img training JSON file."""
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

def generate_training_example_dict(source_tags, modification):
    """Generate a training example dict for the JSON file."""
    source_tags = source_tags.strip().rstrip(',').strip()
    mod_lower = modification.lower()

    # Determine denoise based on modification type
    if any(x in mod_lower for x in ['afro', 'ponytail', 'straight hair', 'curly', 'short hair', 'long hair', 'twintails', 'braid']):
        denoise = 0.85
    elif any(x in mod_lower for x in ['skin', 'nude', 'naked', 'hair color', 'blonde', 'red hair', 'black hair', 'white hair']):
        denoise = 0.80
    elif any(x in mod_lower for x in ['beach', 'background', 'scene', 'city', 'forest', 'indoor', 'outdoor']):
        denoise = 0.75
    elif any(x in mod_lower for x in ['holding', 'gun', 'sword', 'food', 'coffee']):
        denoise = 0.70
    elif any(x in mod_lower for x in ['anime', 'realistic', 'style']):
        denoise = 0.65
    elif any(x in mod_lower for x in ['breast', 'chest', 'boob']):
        denoise = 0.50
    else:
        denoise = 0.70

    # Parse source tags
    tag_list = [t.strip() for t in source_tags.split(',') if t.strip()]

    # Build new tags and negative tags
    new_tags = []
    negative_tags = []

    # Handle afro hair style
    if 'afro' in mod_lower:
        new_tags.extend(["(afro:2.5)", "(afro hair:2.5)"])
        for t in tag_list:
            t_lower = t.lower()
            if any(h in t_lower for h in ['long hair', 'short hair', 'straight', 'twintails', 'ponytail', 'braid', 'bangs']):
                negative_tags.append(t)

    # Handle skin color changes
    if 'dark' in mod_lower and 'skin' in mod_lower or 'brown skin' in mod_lower or 'black skin' in mod_lower:
        new_tags.extend(["(dark skin:2.0)", "(brown skin:2.0)", "(dark-skinned female:2.0)"])
        negative_tags.extend(["pale skin", "white skin", "light skin", "fair skin"])
    elif 'white skin' in mod_lower or 'pale skin' in mod_lower or 'light skin' in mod_lower:
        new_tags.extend(["(pale skin:2.0)", "(white skin:2.0)", "(fair skin:2.0)"])
        negative_tags.extend(["dark skin", "tan skin", "brown skin", "black skin"])

    # Handle hair color changes
    hair_colors = {
        'blonde': "(blonde hair:2.0), blonde hair",
        'red hair': "(red hair:2.0), red hair",
        'black hair': "(black hair:2.0), black hair",
        'white hair': "(white hair:2.0), white hair",
        'blue hair': "(blue hair:2.0), blue hair",
        'pink hair': "(pink hair:2.0), pink hair",
        'purple hair': "(purple hair:2.0), purple hair",
        'green hair': "(green hair:2.0), green hair",
        'silver hair': "(silver hair:2.0), silver hair",
    }
    for color, tag in hair_colors.items():
        if color in mod_lower:
            new_tags.append(tag)
            for t in tag_list:
                if 'hair' in t.lower() and any(c in t.lower() for c in ['blonde', 'black', 'brown', 'red', 'blue', 'pink', 'purple', 'green', 'silver', 'white', 'orange']):
                    if color not in t.lower():
                        negative_tags.append(t)

    # Handle nude/naked
    if 'nude' in mod_lower or 'naked' in mod_lower:
        new_tags.extend(["(nude:2.0)", "(naked:2.0)"])
        clothing_keywords = ['dress', 'shirt', 'skirt', 'uniform', 'bikini', 'swimsuit', 'outfit', 'costume',
                           'sweater', 'top', 'pants', 'shorts', 'kimono', 'jacket', 'coat', 'hoodie',
                           'blouse', 'leotard', 'pantyhose', 'bra', 'underwear', 'lingerie', 'clothes']
        for t in tag_list:
            t_lower = t.lower()
            if any(c in t_lower for c in clothing_keywords):
                negative_tags.append(t)
        negative_tags.extend(["clothing", "clothed"])

    # Handle anime style
    if 'anime' in mod_lower:
        new_tags.append("(anime:1.5)")
        if 'realistic' in ' '.join(tag_list).lower():
            negative_tags.extend(["realistic", "photorealistic"])

    # Add remaining original tags (excluding ones we're changing)
    skip_patterns = []
    if any(x in mod_lower for x in ['hair', 'afro']):
        skip_patterns.append('hair')
    if 'skin' in mod_lower:
        skip_patterns.append('skin')

    for t in tag_list:
        t_lower = t.lower()
        skip = False
        for pattern in skip_patterns:
            if pattern in t_lower:
                skip = True
                break
        if t in negative_tags or t.lower() in [n.lower() for n in negative_tags]:
            skip = True
        if not skip:
            new_tags.append(t)

    # Add quality tags
    new_tags.extend(["vibrant colors", "sharp", "high quality"])

    # Add standard negative tags
    negative_tags.extend(["deformed", "extra limbs", "bad anatomy", "blurry", "distorted", "extra people"])

    # Remove duplicates while preserving order
    seen = set()
    unique_new_tags = []
    for t in new_tags:
        t_clean = t.lower().strip()
        if t_clean not in seen:
            seen.add(t_clean)
            unique_new_tags.append(t)

    seen = set()
    unique_negative = []
    for t in negative_tags:
        t_clean = t.lower().strip()
        if t_clean not in seen:
            seen.add(t_clean)
            unique_negative.append(t)

    return {
        "tags": source_tags,
        "change": modification,
        "denoise": denoise,
        "output_tags": ", ".join(unique_new_tags),
        "negative": ", ".join(unique_negative)
    }

def sync_training_files():
    """Sync all pending regen examples to all JSON training files."""
    log = load_regen_log()
    pending = [e for e in log["requests"] if not e.get("synced")]

    if not pending:
        print("[TRAINER] No pending examples to sync")
        return 0

    print(f"[TRAINER] Syncing {len(pending)} examples to {len(IMG2IMG_TRAINING_FILES)} JSON files...")

    # Generate examples once
    new_examples = []
    for entry in pending:
        example = generate_training_example_dict(entry["source_tags"], entry["modification"])
        new_examples.append((entry, example))

    # Update each training file
    total_added = 0
    for filepath in IMG2IMG_TRAINING_FILES:
        training_data = load_training_json(filepath)
        if not training_data:
            print(f"[TRAINER] Could not load {filepath}, skipping")
            continue

        added_count = 0
        for entry, example in new_examples:
            # Check for duplicate in this file
            is_duplicate = False
            for existing in training_data.get("examples", []):
                if existing.get("tags") == example["tags"] and existing.get("change") == example["change"]:
                    is_duplicate = True
                    break

            if not is_duplicate:
                training_data["examples"].append(example)
                added_count += 1

        if added_count > 0:
            save_training_json(filepath, training_data)
            print(f"  {filepath}: added {added_count} examples")
            total_added += added_count
        else:
            print(f"  {filepath}: no new examples (all duplicates)")

    # Mark all as synced
    for entry in pending:
        entry["synced"] = True
    save_regen_log(log)
    print(f"[TRAINER] Marked {len(pending)} entries as synced")

    return total_added

def list_pending():
    """List pending (unsynced) regen examples."""
    log = load_regen_log()
    pending = [e for e in log["requests"] if not e.get("synced")]

    if not pending:
        print("[TRAINER] No pending examples")
        return

    print(f"[TRAINER] {len(pending)} pending examples:")
    for e in pending:
        print(f"  #{e['id']}: {e['modification'][:60]}...")
        print(f"       Tags: {e['source_tags'][:60]}...")

def clear_pending():
    """Clear all pending (mark as synced without writing)."""
    log = load_regen_log()
    count = 0
    for entry in log["requests"]:
        if not entry.get("synced"):
            entry["synced"] = True
            count += 1
    save_regen_log(log)
    print(f"[TRAINER] Cleared {count} pending examples")

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Regen Auto-Trainer")
        print("=" * 40)
        print("Usage:")
        print("  python regen_trainer.py sync    - Write all pending examples to training files")
        print("  python regen_trainer.py list    - List pending examples")
        print("  python regen_trainer.py clear   - Clear pending without writing")
        print()
        list_pending()
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "sync":
        sync_training_files()
    elif cmd == "list":
        list_pending()
    elif cmd == "clear":
        clear_pending()
    else:
        print(f"Unknown command: {cmd}")
        print("Use: sync, list, or clear")
