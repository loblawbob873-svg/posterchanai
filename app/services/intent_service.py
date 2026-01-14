"""
Intent Detection Service - Simplified LLM-based command detection.

This service uses direct LLM training to convert natural language into commands,
eliminating complex JSON parsing and field validation.
"""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)


class IntentService:
    """
    Detects user intent from natural language using LLM training.
    """

    def __init__(self, db: Session, user: Optional["User"] = None):
        self.db = db
        self.user = user
        from app.models import Setting
        from app.services.chat_service import ChatService

        self.chat_service = ChatService(db, user=user)

        # Load confidence threshold from settings (kept for compatibility)
        threshold_setting = db.query(Setting).filter(Setting.key == "intent_confidence_threshold").first()
        self.confidence_threshold = float(threshold_setting.value if threshold_setting else "0.7")

    async def detect_intent(self, user_message: str, context: str = "") -> Optional[dict]:
        """
        Analyze user message to detect actionable intent.

        Args:
            user_message: The user's input message
            context: Additional context (e.g., pasted email content, previous messages)

        Returns:
            Dictionary with detected command, or None for regular chat
        """
        # Quick checks for obvious non-action messages
        if self._is_simple_greeting(user_message):
            return None

        # Skip intent detection for very short messages (likely just chat)
        if len(user_message.strip()) < 10:
            return None

        # Let LLM be smart - no keyword filtering

        # Limit context size to prevent slow processing
        MAX_CONTEXT_CHARS = 4000
        if context and len(context) > MAX_CONTEXT_CHARS:
            context = context[:MAX_CONTEXT_CHARS] + "\n...[truncated]..."

        # Build the simplified intent detection prompt
        today = datetime.now()

        detection_prompt = f"""You are a command assistant. Analyze the user's message and respond with ONLY the appropriate command.

CURRENT DATE/TIME: {today.strftime("%A, %B %d, %Y at %I:%M %p")}

USER MESSAGE:
{user_message}

{f"CONTEXT TO ANALYZE:{chr(10)}{context}" if context else ""}

TRAINING - Learn these command formats by example:

CALENDAR:
"Add meeting tomorrow at 3pm" -> cal add meeting tomorrow at 3pm
"Team standup every Monday at 9am" -> cal add team standup every Monday at 9am
"Show my calendar" -> cal

CONTACTS:
"Add John Doe 555-1234 john@example.com" -> contacts add John Doe 555-1234 john@example.com
"Find Sarah" -> contacts Sarah

TODO:
"Remind me to buy groceries" -> todo add buy groceries
"Show my todos" -> todo

EMAIL:
"Send email to john@example.com saying Hello" -> mail send john@example.com Hello
"Check my email" -> mail
"Reply to email 123: Thanks!" -> mail reply verita84 123 Thanks!

SEARCH & IMAGES:
"Search for AI news" -> search AI news
"Find images of cats" -> images cats

IMAGE GENERATION (visual images only - must include "image", "picture", "photo"):
"Generate image of a sunset" -> geni a sunset
"Create an image of a cat" -> geni a cat
"Make a picture of mountains" -> geni mountains
"Generate a photo of the beach" -> geni beach

WRITING/TEXT CREATION (use chat, not commands - posts, articles, emails, etc.):
"Create a viral social media post" -> none
"Generate a viral social media post" -> none
"Write a blog post" -> none
"Generate a post for Twitter" -> none
"Draft an email" -> none
"Help me write a message" -> none
"Create content for my website" -> none

YOUTUBE:
"Summarize https://youtube.com/watch?v=abc123" -> yt https://youtube.com/watch?v=abc123
"Download https://youtu.be/xyz789" -> ytdl https://youtu.be/xyz789

MUSIC:
"Play music" -> music
"Play happy music" -> music mood happy
"Skip song" -> music skip

TRANSLATION:
"Translate to Spanish" -> translate Spanish
"Translate email to German" -> translate email German

NEWS:
"Check the news" -> news
"News about technology" -> news technology

BUDGET:
"Show my bills" -> budget bills
"Pay electric bill" -> budget pay electric
"I paid Netflix" -> budget pay Netflix

TORRENT:
"Show torrents" -> torrents
"Search for Ubuntu torrents" -> torrents search Ubuntu
"Pause torrent 3" -> torrents pause 3

FIREWALL & LOGS:
"Check firewall" -> firewall
"Search firewall logs for 192.168.1.1" -> firewall search 192.168.1.1

OTHER:
"Show help" -> help
"Refresh Miniflux" -> miniflux

NO ACTION (just chat):
"Hello" -> none
"How are you?" -> none

IMPORTANT RULES:
1. For calendar events, preserve the natural time description (e.g., "tomorrow at 3pm", "Friday at noon")
2. For recurring events, preserve recurrence patterns naturally (e.g., "every Monday", "daily", "every weekday", "every Monday Wednesday Friday")
3. Extract the key information from emails/context for event titles and details
4. DO NOT include any explanation - respond with ONLY the command or "none"
5. DO NOT add quotes, markdown, or extra text
6. For calendar events, pass the time description naturally - the system will parse it correctly and handle timezones

RESPOND WITH THE COMMAND ONLY!"""

        messages = [
            {
                "role": "system",
                "content": "You are a command parser. Your ONLY job is to output commands in the exact format specified. Output ONLY the command string, nothing else. No explanations, no markdown, no quotes.",
            },
            {"role": "user", "content": detection_prompt},
        ]

        try:
            # Use timeout to prevent hanging
            INTENT_TIMEOUT = 15  # seconds (reduced from 30 since we're expecting simple output)
            response = await asyncio.wait_for(self.chat_service.chat(messages), timeout=INTENT_TIMEOUT)

            # Clean up response
            command = response.strip()

            # Remove common formatting artifacts
            if command.startswith("```"):
                # Remove markdown code blocks
                command = command.replace("```", "").strip()
            if command.startswith('"') and command.endswith('"'):
                command = command[1:-1].strip()
            if command.startswith("'") and command.endswith("'"):
                command = command[1:-1].strip()

            # Check if it's "none" (no action)
            if command.lower() == "none" or not command:
                return None

            logger.info(f"Intent detected command: {command}")

            return {
                "action": "command",
                "command": command,
                "confidence": 0.9,  # High confidence since LLM explicitly chose this
                "reasoning": f"Parsed command: {command}",
            }

        except asyncio.TimeoutError:
            logger.warning(f"Intent detection timed out after {INTENT_TIMEOUT}s")
            return None
        except Exception as e:
            logger.error(f"Intent detection failed: {e}")
            return None

    async def execute_intent(self, intent_result: dict) -> Optional[dict]:
        """
        Execute the detected intent by running the command.
        """
        from app.services.command_service import CommandService

        if not intent_result or intent_result.get("action") != "command":
            return None

        command_str = intent_result.get("command", "").strip()
        if not command_str:
            return None

        # Execute via CommandService
        command_service = CommandService(self.db, user=self.user)
        command, arg = command_service.parse_command(command_str)

        if command:
            logger.info(f"Executing intent command: {command} with arg: {arg}")
            result = await command_service.execute_command(command, arg)

            # Add context about what was done
            result["intent_action"] = f"Executed: {command_str}"

            return result

        return None

    def _is_simple_greeting(self, message: str) -> bool:
        """Check if message is just a simple greeting (no action needed)."""
        greetings = {
            "hi",
            "hello",
            "hey",
            "yo",
            "sup",
            "what's up",
            "whats up",
            "good morning",
            "good afternoon",
            "good evening",
            "good night",
            "how are you",
            "how's it going",
            "hows it going",
        }
        return message.lower().strip() in greetings



async def detect_and_execute(db: Session, user: "User", message: str, context: str = "") -> Optional[dict]:
    """
    Convenience function to detect intent and execute in one call.

    Args:
        db: Database session
        user: Current user
        message: User's message
        context: Additional context (pasted content, etc.)

    Returns:
        Execution result or None if no action detected
    """
    service = IntentService(db, user)
    intent = await service.detect_intent(message, context)

    if intent:
        logger.info(f"Detected intent: {intent.get('command', 'unknown')}")
        return await service.execute_intent(intent)

    return None
