#!/usr/bin/env python3
"""Daily community posts for a NOSTR bot: the day's top posts by our members, and how many were
active. (A Pleroma bot runs engagement.py, against Pleroma's database.)

Read from this node (community_api -> app/services/community_stats.py) and posted as the bot's Nostr
account, which the node's fediverse server
delivers to its fediverse followers too.
"""
import datetime
import logging
import sys

import pytz

import community_api
from ai import generate_reply
from config import OPENAI_ENDPOINT, TIMEZONE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _post(text):
    from nostr import post_image_to_fediverse
    post_image_to_fediverse(text)


def _ai_on() -> bool:
    return bool(OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")))


def _today() -> str:
    tz = pytz.timezone(TIMEZONE) if TIMEZONE else pytz.UTC
    return datetime.datetime.now(tz).strftime("%Y-%m-%d")


def _is_ai_top_posts_response_complete(ai_msg, num_posts):
    """Use the AI version only if it kept a link for every post (a truncated answer is intro only)."""
    if not ai_msg or "None" in ai_msg or num_posts <= 0:
        return False
    return ai_msg.count("[View post]") >= num_posts


def daily_top_posts(print_only=False):
    try:
        rows = community_api.get("activity", top=5)["top_posts"]
    except community_api.Unavailable as e:
        logging.warning(f"[ENGAGEMENT] could not read today's posts: {e}")
        return
    today = _today()
    if not rows:
        logging.info("No engagement today, skipping the report")
        if print_only:
            print(f"Top Posts of the Day - {today}\n\nNo engagement today.")
        return
    lines = []
    for i, r in enumerate(rows, 1):
        preview = (r.get("text") or "").strip().replace("\n", " ")
        preview = (preview[:100] + "...") if len(preview) > 100 else (preview or "[No text content]")
        lines.append(f"{i}. {r['handle']} - {r['score']} pts ({r['reactions']} reactions, {r['reposts']} reposts, "
                     f"{r['replies']} replies)\n   {preview}\n   🔗 [View post]({r['url']})")
    final = f"Top Posts of the Day - {today}\n\n" + "\n\n".join(lines)
    if _ai_on():
        try:
            prompt = f"""Generate an entertaining daily social media report highlighting the top posts.

Posts are ranked by engagement: reactions + (reposts x 2) + (replies x 2)

Requirements:
- Write ONLY in English.
- Create a fun introduction (1-2 sentences)
- Present ALL {len(rows)} posts with rankings and scores
- Add BRIEF witty commentary (keep each post's description short)
- Use emojis (🏆 for top, 🔥 for high engagement)
- End with brief encouragement (1 sentence)
- Keep the tone positive and fun
- Don't change the names

IMPORTANT:
- Keep all engagement numbers visible
- Keep the ranking order
- Include every post
- MUST include each post's link as [View post](url), with the exact URL
- KEEP IT CONCISE - under 2500 characters

Data:
{final}

/no_think"""
            ai_msg = generate_reply(prompt)
            if _is_ai_top_posts_response_complete(ai_msg, len(rows)):
                final = ai_msg
        except Exception as e:
            logging.warning(f"[ENGAGEMENT] AI wording failed, using the plain list: {e}")
    print(final)
    if not print_only:
        _post(final)


def get_active_user_stats():
    a = community_api.get("activity", top=1)
    return int(a.get("dau") or 0), int(a.get("mau") or 0)


def post_active_user_stats(print_only=False):
    try:
        dau, mau = get_active_user_stats()
    except community_api.Unavailable as e:
        logging.warning(f"[ENGAGEMENT] could not read activity: {e}")
        return
    if dau == 0 and mau == 0:
        logging.info("No active members, skipping the stats post")
        return
    today = _today()
    final = (f"📊 Community Activity - {today}\n\n🔥 {dau} people active today\n📈 {mau} people active this month"
             "\n\nThanks for being part of our community!")
    if _ai_on():
        try:
            prompt = f"""Generate an engaging social media post about our community's activity.

Stats for {today}:
- {dau} people were active today
- {mau} people were active this month

Requirements:
- Short and punchy (2-4 sentences)
- Relevant emojis (📊, 🔥, 📈, 🎉)
- Use plain language like "X people active today"; no acronyms like DAU or MAU
- A brief thank you to the community
- No hashtags
- Respond ONLY with the post

/no_think"""
            ai_msg = generate_reply(prompt)
            if ai_msg and "None" not in ai_msg and len(ai_msg) > 20:
                final = ai_msg
        except Exception as e:
            logging.warning(f"[ENGAGEMENT] AI wording failed, using the plain post: {e}")
    print(final)
    if not print_only:
        _post(final)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    pr = len(sys.argv) > 2 and sys.argv[2] == "print"
    if cmd == "top":
        daily_top_posts(pr)
    elif cmd == "stats":
        post_active_user_stats(pr)
    else:
        print("Usage: engagement.py top [print] | stats [print]")
