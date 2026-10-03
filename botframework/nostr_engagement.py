#!/usr/bin/env python3
"""Daily community posts for a NOSTR bot: the day's top posts by our members, and how many were
active. (A Pleroma bot runs engagement.py, against Pleroma's database.)

Read from this node (community_api -> app/services/community_stats.py) and posted as the bot's Nostr
account, which the node's fediverse server
delivers to its fediverse followers too.
"""
import datetime
import logging
import re
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


# Emoji the model wrote can reach the post as their invisible joiners only (a zero-width joiner or a
# variation selector with the picture gone), which renders as a stray box: "Let's dive in! ‍️".
_ORPHAN_JOINERS = re.compile(r"(?:(?<=\s)|^)[\u200d\ufe0e\ufe0f]+|[\u200d\ufe0e\ufe0f]+(?=\s|$)")


def _ai_line(prompt: str, limit: int = 240) -> str:
    """One short line of the model's commentary, or "". Never a list, a link, a name or markup: those are
    built below, in Python, because the model rewrote them -- `@name` instead of a tag that a client can
    open, and `[View post](url)`, which Nostr clients print as raw brackets."""
    try:
        text = (generate_reply(prompt + " /no_think") or "").replace("/no_think", "")
    except Exception as e:
        logging.warning(f"[ENGAGEMENT] AI wording failed: {e}")
        return ""
    text = _ORPHAN_JOINERS.sub("", " ".join(text.split())).strip().strip('"')
    if not text or "None" in text or len(text) > limit or any(c in text for c in "[]()#*`@") or "http" in text:
        return ""
    return text


def _top_posts_message(rows, today, intro="", outro=""):
    """The post, built here and never by the model. Each name is the author's `nostr:npub` reference
    (clients render the person's name, open their profile on a tap and notify them; the bot's poster
    adds the `p` tag) and each link a plain URL, which every client linkifies -- and which this app opens
    in-app."""
    parts = [f"Top Posts of the Day - {today}"]
    if intro:
        parts.append(intro)
    for i, r in enumerate(rows, 1):
        preview = " ".join((r.get("text") or "").split())
        preview = (preview[:100] + "...") if len(preview) > 100 else (preview or "[No text content]")
        who = r.get("ref") or r.get("handle") or "someone"
        medal = {1: "🏆 ", 2: "🥈 ", 3: "🥉 "}.get(i, "")
        parts.append(f"{medal}{i}. {who} - {r['score']} pts ({r['reactions']} reactions, {r['reposts']} reposts, "
                     f"{r['replies']} replies)\n{preview}\n{r['url']}")
    if outro:
        parts.append(outro)
    return "\n\n".join(parts)


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
    intro = outro = ""
    if _ai_on():
        summary = "; ".join(f"{(r.get('text') or '')[:80]} ({r['score']} pts)" for r in rows)
        intro = _ai_line("Write ONE fun, upbeat sentence in English introducing today's most popular community "
                         "posts. No names, no links, no hashtags, no markdown. Today's posts: " + summary)
        outro = _ai_line("Write ONE short, upbeat closing sentence in English inviting people to reply and keep "
                         "posting. No names, no links, no hashtags, no markdown.")
    final = _top_posts_message(rows, today, intro, outro)
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
