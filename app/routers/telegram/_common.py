"""Auto-split from the original telegram.py monolith. No behavior change."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import logging
import re
import asyncio
import time
from datetime import datetime, timedelta
_MEDIA_GROUP_CACHE: dict = {}
from app.database import get_db, SessionLocal
from app.models import User, Conversation, Message
from app.auth import get_current_user, get_admin_user
from app.services.telegram_service import telegram_service, configure_from_settings as _configure_telegram
from app.services.chat_service import ChatService
from app.services.command_service import CommandService
logger = logging.getLogger(__name__)
_HELP_SECTIONS = {
    "pins": (
        "📌 *Pins*\n\n"
        "Pin anything you run often — a search or any command — then re-run it with one tap:\n\n"
        "• `pin ai news`\n"
        "• `pin latest xrp news and price`\n"
        "• `pin screenshot https://google.com`\n\n"
        "Send `pins` to see your pins — each has a ▶ Run and a 🗑️ Delete button\\."
    ),
    "reminders": (
        "⏰ *Reminders*\n\n"
        "Set a reminder in plain language — I work out the time and ping you here "
        "\\(and in the web UI\\):\n\n"
        "• `remind open the oven in 10m`\n"
        "• `remind me next tuesday to call mom`\n\n"
        "Send `reminders` to list your pending ones, each with a 🗑️ Cancel button\\. "
        "You can also `remind cancel <id>`\\."
    ),
    "files": (
        "📎 *Files — compress, convert, extractaudio, circlecrop, meme, dildo, poo, cum, blood, fire & bulletholes*\n\n"
        "Just upload a file (no caption needed) and tap a button:\n\n"
        "*Images:*\n"
        "• 🗜 Compress — shrink the image\n"
        "• 📄 To PDF — combine your image(s) into one PDF\n"
        "• 🔤 Read text — OCR the text out of the image\n"
        "• ✨ Effects → 🖼 Meme — add outlined white caption text (I'll ask for it)\n"
        "• ✨ Effects → 🍆 Dildo — scatter dildos all over the image\n"
        "• ✨ Effects → 💩 Poo — scatter poop all over the image\n"
        "• ✨ Effects → 💦 Cum — scatter cum all over the image\n"
        "• ✨ Effects → 🩸 Blood — splatter blood all over the image\n"
        "• ✨ Effects → 🔥 Fire — set the image on fire\n"
        "• ✨ Effects → 🕺 Naked man — a fat cartoon man dances (huge penis) over the image → 8s clip\n"
        "• ✨ Effects → 🕳️ Bullet holes — punch bullet holes into the image\n"
        "• ✨ Effects → 🏳️‍🌈 Gay — stamp a big red GAY on the image\n"
        "• ✨ Effects → 🥷 Blacked — slap the BLACKED logo on the image\n"
        "• ✨ Effects → ✡️ Kosher — stamp a 100% KOSHER seal on the image\n"
        "• ✨ Effects → 🐶 Barked — drop a smirking dog + #BARKED on the image\n"
        "• ✨ Effects → 🎻 Hava — turn the image into a 6s Hava Nagila video\n"
        "• ✨ Effects → 🇮🇳 Indian — turn the image into a 6s Indian-song video\n"
        "• ✨ Effects → 🎷 Yakety — turn the image into a 9s Yakety Sax video\n"
        "• ✨ Effects → 🛑 Yamete — turn the image into a 6s yamete video\n"
        "• ✨ Effects → 😬 Curb — turn the image into a Curb Your Enthusiasm video\n"
        "• ✨ Effects → 😢 Depressing — turn the image into a 10s depressing video\n"
        "• ✨ Effects → 🌀 Fahh — turn the image into a fahh video\n"
        "• ✨ Effects → 🆘 Helpme — turn the image into a 5s helpme video\n"
        "• ✨ Effects → 🔔 Gong — turn the image into a gong video\n"
        "• ✨ Effects → 🚨 FBI — turn the image into an FBI open up video\n"
        "• ✨ Effects → 💳 Redeem — turn the image into a do not redeem video\n"
        "• ✨ Effects → 😏 Gigity — turn the image into a giggity video\n"
        "• ✨ Effects → 🤤 Beavis — turn the image into a Beavis laugh video\n"
        "• ✨ Effects → 👃 Smell — turn the image into a can you imagine the smell video\n"
        "• ✨ Effects → 🏚️ Hood — turn the image into a 10s hood video\n"
        "• ✨ Effects → 🕌 Akbar — turn the image into an akbar video\n"
        "• ✨ Effects → ⚠️ Retard — turn the image into a retard-alert video\n"
        "• ✨ Effects → 🤠 Whoabuddy — turn the image into a whoa buddy video\n"
        "• ✨ Effects → 🦅 Freebird — turn the image into a Free Bird video\n"
        "• ✨ Effects → 🐻 Kanye — turn the image into a Kanye video\n"
        "• ✨ Effects → 🌑 Darkness — turn the image into a darkness video\n"
        "• ✨ Effects → 🚲 Bike — turn the image into a bike video\n"
        "• ✨ Effects → 💼 Jobs — turn the image into a they-took-our-jobs video\n"
        "• ✨ Effects → 😡 Ree — turn the image into a REEEE video\n"
        "• ✨ Effects → 🗽 Liberal — turn the image into a liberal video\n"
        "• ✨ Effects → 📦 Moving — turn the image into a moving video\n"
        "• ✨ Effects → 🕺 Harlem — turn the image into a Harlem Shake video\n"
        "• ✨ Effects → 🐵 Chimp — overlay the animated chimp gif on the lower third\n"
        "• ✨ Effects → 🤔 Consider — overlay the 'consider the following' cutout\n"
        "• ✨ Effects → 🗣️ Clay — overlay the Clay Davis 'Shiiiit' clip (bg removed)\n"
        "• ✨ Effects → 💖 Vibe — a cute anime girl dances over the image → 8s clip\n"
        "• ✨ Effects → 👍 Rebecca — Rebecca dances with a thumbs up over the image → 8s clip\n"
        "• ✨ Effects → 🎸 Wasteland — turn the image into a Teenage Wasteland video\n"
        "• ✨ Effects → 🍑 Mixalot — turn the image into a Baby Got Back video\n"
        "• ✨ Effects → 😎 Thug — turn the image into a THUG LIFE video\n"
        "• 📣 Post to social — share it to your connected platforms\n\n"
        "*Video:*\n"
        "• 🗜 Compress — re-encode smaller (H.264, up to 1080p)\n"
        "• ✂️ Clip — trim to a start/end time (I'll ask for both)\n\n"
        "*PDF:*\n"
        "• 🖼 To images — one PNG per page\n"
        "• 📝 Summarize — AI summary of the document\n\n"
        "Tips:\n"
        "• Send several images, then tap *To PDF*, to merge them into one PDF.\n"
        "• You can also skip the buttons: send the file with `compress`, `clip 0:10 0:30`, `convert`, `extractaudio`, `circlecrop`, `meme <text>`, `dildo`, `poo`, `cum`, `blood`, `bullethole`, `fire`, `nakedman`, `gay`, `blacked`, `kosher`, `blue`, `barked`, `hava`, `indian`, `yakety`, `yamete`, `curb`, `depressing`, `fahh`, `helpme`, `gong`, `fbi`, `redeem`, `gigity`, `beavis`, `smell`, `hood`, `akbar`, `retard`, `whoabuddy`, `diarrhea`, `seth`, `robocop`, `titan`, `terminator`, `reze`, `vibe`, `rebecca`, `sopranos`, `cheers`, `munsters`, `happydays`, `dontwanttowait`, `strangerthings`, `adamsfamily`, `xmen`, `futurama`, `charliesangles`, `differentstroke`, `seinfeld`, `onepiece`, `overtaken`, `freebird`, `kanye`, `darkness`, `bike`, `jobs`, `ree`, `liberal`, `moving`, `harlem`, `chimp`, `consider`, `clay`, `wasteland`, `mixalot`, `thug`, `feltedtables`, `feliz`, `sleepwell`, `horse` or `knightrider` as the caption.\n"
        "• Telegram limits bot downloads to 20 MB — use the web UI for bigger files."
    ),
    "youtube": (
        "🎬 *YouTube*\n\n"
        "Paste a YouTube link (or `yt <url>`) and choose:\n"
        "• 📋 Summary — AI summary of the video\n"
        "• 🎵 MP3 — download the audio\n"
        "• 🎬 Movie — download the video\n"
        "• 📣 Post — generate & share a social post\n\n"
        "Or use `ytdl <url>` for audio, `ytdl video <url>` for video.\n"
        "Trim and/or shrink a video in one go: `ytdl video <url> clip 0:10 0:30 compress`"
    ),
    "4chan": (
        "🍀 *4chan Browser*\n\n"
        "`4chan` — Select a board to browse\n"
        "`4chan g` — View /g/ (Technology) catalog\n"
        "`4chan pol` — View /pol/ catalog\n"
        "`4chan a` — View /a/ (Anime) catalog\n"
        "`4chan h` — View /h/ (Hentai) catalog\n\n"
        "*Features:*\n"
        "• Browse thread catalog with reply counts\n"
        "• Tap any thread to view posts with images\n"
        "• Summarize long threads with AI\n"
        "• Navigate with inline buttons\n"
        "• Open threads directly on 4chan"
    ),
    "chat": (
        "💬 *Chat & URLs*\n\n"
        "Just send any message to chat with the AI\\.\n\n"
        "• Reply to a message to use it as context\n"
        "• Send any URL to get a summary\n"
        "• Send a YouTube link for a video summary\n"
        "• Forward any article or link — auto\\-summarized\n"
        "• Send a photo to describe it or extract text \\(OCR\\)\n"
        "• The bot remembers recent conversation context"
    ),
    "search": (
        "🔍 *Web Search*\n\n"
        "`search <query>`\n"
        "Searches the web and returns an AI\\-written summary with source links\\.\n\n"
        "*Examples:*\n"
        "`search latest SpaceX launch`\n"
        "`search best Python frameworks 2025`"
    ),
    "images": (
        "🖼 *Image Search*\n\n"
        "`images <query>`\n"
        "Searches for images and sends them directly in the chat\\.\n\n"
        "*Examples:*\n"
        "`images northern lights`\n"
        "`images cyberpunk city art`"
    ),
    "translate": (
        "🌐 *Translation*\n\n"
        "`translate <text> to <language>`\n"
        "Translates text to any language\\.\n\n"
        "• Reply to any message with `translate` to translate it\n"
        "• Reply with `translate to Spanish` to specify the language\n"
        "• Send a photo with `translate` to OCR and translate the text in the image\n\n"
        "*Examples:*\n"
        "`translate hello world to Japanese`\n"
        "\\(reply to a message\\) `translate to French`"
    ),
    "news": (
        "📰 *News*\n\n"
        "`news` — Latest headlines from all sources\n"
        "`news <source>` — Headlines from a specific source\n\n"
        "*Examples:*\n"
        "`news`\n"
        "`news bbc`\n"
        "`news techcrunch`"
    ),
    "geni": (
        "🎨 *Image Generation*\n\n"
        "`geni <prompt>`\n"
        "Generates an image from your description using the configured AI backend\\.\n\n"
        "*Examples:*\n"
        "`geni a sunset over a cyberpunk city`\n"
        "`geni portrait of a samurai in watercolor style`"
    ),
    "torrents": (
        "🧲 *Torrents*\n\n"
        "`torrents` — Browse categories \\(Movies, TV, Music, Anime\\)\n"
        "`torrents search <query>` — Search by title\n"
        "`torrents list` — View & manage active downloads\n"
        "`torrents pause/resume/rm <#>` — Manage a download\n\n"
        "• Tap category buttons to browse top results\n"
        "• Each result has its own Download button\n"
        "• Send a magnet link directly to add it instantly\n\n"
        "*Examples:*\n"
        "`torrents search dark knight 1080p`\n"
        "`torrents list`"
    ),
    "nyaa": (
        "🎌 *Nyaa \\(Anime Torrents\\)*\n\n"
        "`nyaa <query>` — Search nyaa\\.si for anime torrents\n\n"
        "• Tap the *🔎 Nyaa Search* button and type your query when prompted\n"
        "• Each result has its own Download button\n\n"
        "*Examples:*\n"
        "`nyaa one piece 1080p`\n"
        "`nyaa attack on titan s4`"
    ),
    "mail": (
        "✉️ *Email*\n\n"
        "`mail <to> <body>`\n"
        "Sends an email using your configured mail settings\\.\n\n"
        "*Examples:*\n"
        "`mail alice@example.com Hey, just checking in\\!`"
    ),
    "post": (
        "📱 *Social Media Post Generator*\n\n"
        "Reply to any message \\(a bot answer, a link, a photo\\) and send a `post` command:\n\n"
        "• `post` — rewrite it into a viral, engaging post\n"
        "• `post raw` — share it *exactly as written*, no rewrite \\(also `verbatim`\\)\n"
        "• `post <instructions>` — rewrite it your way\n\n"
        "I then show share buttons for your connected platforms \\(Misskey / Pleroma\\)\\.\n\n"
        "*Examples:*\n"
        "\\(reply to a good answer\\) `post raw`\n"
        "\\(reply to an article\\) `post professional`\n"
        "\\(reply to a link\\) `post don't include links`"
    ),
    "logs": (
        "📋 *System Logs*\n\n"
        "`logs` — Shows recent system log entries\\.\n"
        "Useful for checking errors or monitoring activity\\."
    ),
}
router = APIRouter(prefix="/api/telegram", tags=["telegram"])
_MAX_SEEN_IDS = 500  # Keep a bounded window; Telegram won't replay further back
_misskey_post_cache: dict = {}
_pleroma_post_cache: dict = {}
_nostr_post_cache: dict = {}
_CONSUMED = "__consumed__"
_geni_image_cache: dict = {}
_link_action_cache: dict = {}
_youtube_action_cache: dict = {}
_media_action_cache: dict = {}
_MEDIA_ACTION_TTL = 600  # seconds
_flashcard_decks_cache: dict = {}
_FLASHCARD_TTL = 1800  # 30 min
_clip_pending: dict = {}
_CLIP_START_PROMPT = "✂️ Clip — reply with the START time (e.g. 0:10 or 90):"
_CLIP_END_PROMPT = "✂️ Clip — reply with the END time (e.g. 0:30 or 1:30):"
_SOCIAL_CAPTION_PROMPT = "✍️ Add a caption for your post? Reply with text, or send - to post without any."
_POST_PROMPTS = (
    "📣 *Post this?*", "📣 Post this?",
    "📣 *Post this (as written)?*", "📣 Post this (as written)?",
    "📣 Post this to your timeline?",
    "📣 *Share this image?*", "📣 Share this image?",
    "📣 *Share this?*", "📣 Share this?",
    "📣 *Post this glowing image?*", "📣 Post this glowing image?",
)
_MEME_PROMPT = "🖼 Meme — reply with the caption text to add:"
_EFFECT_CAPTION_PROMPT = "✍️ Reply with the caption text for this effect:"
_effect_caption_pending: dict = {}
_effect_char_pending: dict = {}
_FC_LETTERS = ["A", "B", "C", "D", "E"]
_FX_THEMES = [
    ("🇮🇹 Sopranos", "sopranos"), ("🍻 Cheers", "cheers"),
    ("🧛 Munsters", "munsters"), ("😃 Happy Days", "happydays"),
    ("🌊 Don't Wait", "dontwanttowait"), ("🔦 Stranger Things", "strangerthings"),
    ("🖤 Addams Family", "adamsfamily"), ("❌ X-Men", "xmen"),
    ("🚀 Futurama", "futurama"), ("👼 Charlie's Angels", "charliesangles"),
    ("🌍 Diff'rent Strokes", "differentstroke"), ("🎤 Seinfeld", "seinfeld"),
    ("🦅 Freebird", "freebird"), ("🕺 Harlem", "harlem"),
    ("🎻 Hava", "hava"), ("🎷 Yakety", "yakety"),
    ("😬 Curb", "curb"), ("🎸 Wasteland", "wasteland"),
    ("🍑 Mixalot", "mixalot"), ("🏴‍☠️ One Piece", "onepiece"),
    ("🤖 Robocop", "robocop"), ("🗿 Titan", "titan"),
    ("🦾 Terminator", "terminator"), ("💣 Reze", "reze"),
]
_FX_SOUNDS = [
    ("🤠 Whoabuddy", "whoabuddy"), ("🎬 Seth", "seth"),
    ("🚽 Diarrhea", "diarrhea"),
    ("🕌 Akbar", "akbar"),
    ("⚠️ Retard", "retard"), ("🔔 Gong", "gong"),
    ("🚨 FBI", "fbi"), ("💳 Redeem", "redeem"),
    ("😏 Gigity", "gigity"), ("🤤 Beavis", "beavis"),
    ("👃 Smell", "smell"), ("🏚️ Hood", "hood"),
    ("🇮🇳 Indian", "indian"), ("🛑 Yamete", "yamete"),
    ("😢 Depressing", "depressing"), ("🌀 Fahh", "fahh"),
    ("🆘 Helpme", "helpme"), ("🐶 Barked", "barked"),
    ("😡 Ree", "ree"), ("🐻 Kanye", "kanye"),
    ("🌑 Darkness", "darkness"), ("🚲 Bike", "bike"),
    ("💼 Jobs", "jobs"), ("🗽 Liberal", "liberal"),
    ("📦 Moving", "moving"), ("🏎️ Overtaken", "overtaken"),
    ("🎱 Felted Tables", "feltedtables"), ("🙏 Prayer", "prayer"),
    ("🎉 Feliz", "feliz"), ("😴 Sleep Well", "sleepwell"),
    ("🐴 Horse", "horse"), ("🚗 Knight Rider", "knightrider"),
]
_FX_MEMES = [
    ("🍆 Dildo", "dildo"), ("💩 Poo", "poo"),
    ("💦 Cum", "cum"), ("🩸 Blood", "blood"),
    ("🔥 Fire", "fire"), ("🕳️ Bullet holes", "bullethole"),
    ("🕺 Naked man", "nakedman"),
    ("🏳️‍🌈 Gay", "gay"), ("🥷 Blacked", "blacked"),
    ("✡️ Kosher", "kosher"), ("🤔 Consider", "consider"),
    ("🐵 Chimp", "chimp"), ("🗣️ Clay", "clay"),
    ("💖 Vibe", "vibe"), ("👍 Rebecca", "rebecca"),
    ("😎 Thug", "thug"), ("🔵 Blue", "blue"),
]
_TRANSLATE_LANGS = [
    "English", "Spanish", "French",
    "German", "Italian", "Portuguese",
    "Russian", "Chinese", "Japanese",
    "Korean", "Arabic", "Thai",
]
_news_post_cache: dict = {}
_news_source_cache: dict = {}
_4chan_cache: dict = {}
_4chan_thread_cache: dict = {}
class TelegramWebhookUpdate(BaseModel):
    update_id: int
    message: Optional[dict] = None
    callback_query: Optional[dict] = None
    edited_message: Optional[dict] = None
    inline_query: Optional[dict] = None
    chosen_inline_result: Optional[dict] = None
class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[dict] = None
    callback_query: Optional[dict] = None
    edited_message: Optional[dict] = None
    inline_query: Optional[dict] = None
    chosen_inline_result: Optional[dict] = None
    my_chat_member: Optional[dict] = None
    chat_member: Optional[dict] = None
    
    class Config:
        extra = "allow"
class TelegramBotConfig(BaseModel):
    bot_token: Optional[str] = None
    webhook_url: Optional[str] = None
    enabled: bool = False
class TelegramChatSetup(BaseModel):
    chat_id: str
    notifications: str = "news,downloads,mentions"
