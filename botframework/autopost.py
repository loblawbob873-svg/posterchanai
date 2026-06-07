"""Auto-poster — generate one in-character standalone post and publish it.

One-shot, the text twin of `imageposter`: `main.py --autopost` runs this once and exits,
and the bot manager (`bot_manager_service._reconcile_scheduled`) spawns it on a schedule.
Platform is chosen from whichever endpoint the manager configured in the env, exactly the
way `imageposter` picks misskey vs pleroma — so this works for both platforms unchanged.
"""

import os
import random

from ai import generate_reply
from config import PROMPT


_DEFAULT_SEED = (
    "Write a short, original standalone post in your own voice — a spontaneous take, "
    "rant, or shower-thought. Do NOT address anyone, no @mentions, no greeting, no hashtags "
    "unless they fit naturally. Just the post text."
)


def _topics():
    """AUTO_POST_TOPICS is a free-form string: one topic per line and/or comma-separated."""
    raw = os.getenv("AUTO_POST_TOPICS", "")
    parts = [t.strip() for line in raw.splitlines() for t in line.split(",")]
    return [t for t in parts if t]


def _build_seed():
    seed = os.getenv("AUTO_POST_SEED", "").strip() or _DEFAULT_SEED
    topics = _topics()
    if topics:
        seed += f"\n\nToday's subject: {random.choice(topics)}."
    return seed


def autopost():
    """Generate one post from the bot's personality PROMPT and post it to its platform."""
    if not PROMPT or len(PROMPT.strip()) < 10:
        print("[autopost] No personality PROMPT set; skipping.")
        return

    seed = _build_seed()
    print(f"[autopost] Generating post with seed: {seed[:120]}...")
    text = generate_reply(seed)
    if not text or not text.strip():
        print("[autopost] Generation returned empty; nothing posted.")
        return
    text = text.strip()

    # Platform dispatch — both modules expose post_to_fediverse(status_text) with identical
    # BLOCK_PHRASE / length guards. Choose by whichever endpoint the manager configured.
    from config import MISSKEY_SERVER, PLEROMA_ENDPOINT
    if MISSKEY_SERVER:
        from misskey import post_to_fediverse
    elif PLEROMA_ENDPOINT:
        from pleroma import post_to_fediverse
    else:
        print("[autopost] Neither MISSKEY_SERVER nor PLEROMA_ENDPOINT configured; skipping.")
        return

    print(f"[autopost] Posting ({len(text)} chars): {text[:120]}...")
    post_to_fediverse(text)
    print("[autopost] Done.")
