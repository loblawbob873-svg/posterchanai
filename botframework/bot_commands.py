"""Shared bot command constants (used by the Nostr listener; the older
pleroma listener still inlines its own copies).

MEDIA_COMMANDS — commands that operate on an attached/linked file.
NO_CAPTION_COMMANDS — of those, the ones whose result IS the media (no text caption).
"""

# Image-stamp + effect commands whose output is the media itself (no caption).
NO_CAPTION_COMMANDS = (
    "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "nakedman", "glow", "gay", "blacked",
    "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing",
    "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "heat", "smell", "hood", "akbar",
    "retard", "whoabuddy", "diarrhea", "seth", "robocop", "titan", "terminator", "reze", "vibe", "rebecca", "makima", "sopranos",
    "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily",
    "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "jerry", "onepiece",
    "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving",
    "harlem", "chimp", "consider", "clay", "uwu", "wasteland", "mixalot", "nonematters", "thug", "feltedtables",
    "prayer", "feliz", "sleepwell", "horse", "knightrider", "hugebitch",
)

# All commands that consume a file: the no-caption ones plus the generic transforms.
MEDIA_COMMANDS = ("compress", "clip", "convert") + NO_CAPTION_COMMANDS

BOT_HELP_TEXT = (
    "🤖 Poster-Chan — mention me with any of these:\n\n"
    "🔎 Info:  search <q> · images <q> · news <source> · geni <prompt> · screenshot <url>\n"
    "📥 Media (attach/link a file):  compress · clip <start> <end> · convert (img↔PDF)\n"
    "     ytdl <url> = audio, ytdl video <url> = video\n"
    "🖼 Image stamps (attach/link an image):  meme <text> · dildo · poo · cum · blood · fire · …\n"
    "🎬 Effects (image → music/clip video):  hava · curb · yakety · whoabuddy · sopranos · …\n"
    "🌟 Glow:  glow (on an image) · glow <text> (a glowing neon text post)\n"
    "🗣 /narrate <message> — reply as a short TTS video\n\n"
    "Or just talk to me and I'll reply. 💕"
)
