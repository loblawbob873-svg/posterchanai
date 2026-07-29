"""Auto-poster — generate one in-character standalone post and publish it.

One-shot, the text twin of `imageposter`: `main.py --autopost` runs this once and exits,
and the bot manager (`bot_manager_service._reconcile_scheduled`) spawns it on a schedule.
Platform is chosen from whichever endpoint the manager configured in the env, exactly the
way `imageposter` picks its platform — so this works unchanged.
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


def _generate_post():
    """Generate one in-character post; returns stripped text, or None on empty/failure."""
    seed = _build_seed()
    print(f"[autopost] Generating post with seed: {seed[:120]}...")
    text = generate_reply(seed)
    if not text or not text.strip():
        print("[autopost] Generation returned empty; nothing to post.")
        return None
    return text.strip()


# Markers the manager parses out of --autopost-print output to show the Preview in Admin → Bots.
PREVIEW_BEGIN = "=== AUTOPOST PREVIEW BEGIN ==="
PREVIEW_END = "=== AUTOPOST PREVIEW END ==="



def autopost(print_only=False):
    """Generate one post from the bot's personality PROMPT and post it to its platform.

    print_only=True is the dry run (--autopost-print): generate one and print between the
    PREVIEW markers, but do NOT publish.

    Platform precedence is Pleroma → Nostr.
    """
    if not PROMPT or len(PROMPT.strip()) < 10:
        print("[autopost] No personality PROMPT set; skipping.")
        return

    if print_only:
        text = _generate_post()
        if text:
            print(PREVIEW_BEGIN)
            print(text)
            print(PREVIEW_END)
        return

    from config import PLEROMA_ENDPOINT, NOSTR_NSEC

    if PLEROMA_ENDPOINT:
        # Fediverse: one post, via post_to_fediverse(status_text) with its
        # BLOCK_PHRASE / length guards.
        text = _generate_post()
        if not text:
            return
        from pleroma import post_to_fediverse
        print(f"[autopost] Posting ({len(text)} chars): {text[:120]}...")
        post_to_fediverse(text)
    elif NOSTR_NSEC:
        # Nostr: one kind-1 text note via the bot's signer. post_image_to_fediverse with no
        # media is the plain-text publish path (same BLOCK_PHRASE/length guards live in the
        # underlying service).
        text = _generate_post()
        if not text:
            return
        from nostr import post_image_to_fediverse as post_to_nostr
        print(f"[autopost] Posting to Nostr ({len(text)} chars): {text[:120]}...")
        post_to_nostr(text)
    else:
        print("[autopost] No Pleroma/Nostr endpoint configured; skipping.")
        return

    print("[autopost] Done.")
