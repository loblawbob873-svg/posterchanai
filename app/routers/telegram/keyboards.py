"""Auto-split from the original telegram.py monolith. No behavior change."""


def _help_main_keyboard() -> dict:
    """Inline keyboard for the help main menu -- what the bot still does (2026-10-08; the rest is in PosterChan)."""
    return {
        "inline_keyboard": [
            [
                {"text": "💬 Chat & links", "callback_data": "help:chat"},
                {"text": "🎨 Image Gen",    "callback_data": "help:geni"},
            ],
            [
                {"text": "📸 Screenshot",   "callback_data": "prompt:screenshot"},
                {"text": "📋 Logs",         "callback_data": "help:logs"},
            ],
            [
                {"text": "⏰ Reminders",    "callback_data": "help:reminders"},
                {"text": "📌 Pins",         "callback_data": "help:pins"},
            ],
        ]
    }

