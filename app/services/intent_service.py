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

CRITICAL RULE: If user asks to "create", "write", "generate", or "make" TEXT CONTENT (social media posts, articles, blog posts, captions, tweets, messages, emails), you MUST respond "none" to use chat. Image generation ONLY happens when user specifically asks for a VISUAL IMAGE/PICTURE/PHOTO.

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

CRITICAL: TEXT vs VISUAL CONTENT
- If asking to CREATE/WRITE/GENERATE TEXT (posts, articles, captions, messages) -> none (use chat)
- If asking to GENERATE/CREATE VISUAL IMAGES (pictures, photos, artwork) -> geni command

WRITING/TEXT CREATION (always respond "none" - let chat handle it):
"Create a viral social media post" -> none
"Generate a viral social media post" -> none
"Create a social media post for my project" -> none
"Generate a post about my website" -> none
"Write a blog post" -> none
"Generate a post for Twitter" -> none
"Make a Facebook post" -> none
"Draft an email" -> none
"Help me write a message" -> none
"Create content for my website" -> none

IMAGE GENERATION (ONLY for visual artwork - must explicitly request image/picture/photo):
"Generate image of a sunset" -> geni a sunset
"Create an image of a cat" -> geni a cat
"Make a picture of mountains" -> geni mountains
"Generate a photo of the beach" -> geni beach
"Draw a cyberpunk city" -> geni cyberpunk city

YOUTUBE:
"Summarize https://youtube.com/watch?v=abc123" -> yt https://youtube.com/watch?v=abc123
"Download https://youtu.be/xyz789" -> ytdl https://youtu.be/xyz789
"Download this song https://youtube.com/watch?v=xyz" -> ytdl https://youtube.com/watch?v=xyz
"Download this video" -> ytdl
"Get this song" -> ytdl
"Download the music from this link" -> ytdl

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
"list my bills" -> budget bills
"list bills" -> budget bills
"paid bill electric" -> budget paid electric
"add a new bill" -> budget add Name amount
"got a new bill" -> budget add Name amount
"new bill for" -> budget add Name amount

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
4. CRITICAL: "geni" is ONLY for VISUAL images/pictures/photos. If user wants TEXT (social media post, article, caption, message), respond "none" to use chat
5. Social media posts, blog posts, articles, captions, tweets, LinkedIn posts = TEXT CONTENT = ALWAYS respond "none"
6. Images, pictures, photos, artwork, drawings, illustrations = VISUAL CONTENT = use "geni" command
7. When in doubt about create/generate/make, ask yourself: "Is this TEXT or a PICTURE?" - TEXT = none, PICTURE = geni
8. DO NOT include any explanation - respond with ONLY the command or "none"
9. DO NOT add quotes, markdown, or extra text
10. For calendar events, pass the time description naturally - the system will parse it correctly and handle timezones
11. When paying a bill with: budget pay or budget paid, do NOT add extra commentary, emojis, or enthusiastic responses. Simply acknowledge the action professionally.

DOUBLE CHECK: If the request contains words like "post", "article", "tweet", "caption", "message" it is TEXT = respond "none"

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
            INTENT_TIMEOUT = 8  # seconds - keep it short to avoid delaying user experience
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

            # HARD SAFETY CHECK: Block geni command for text content creation
            # This overrides LLM decision if text-creation keywords are detected
            if command.lower().startswith("geni"):
                text_creation_keywords = [
                    "post", "article", "tweet", "caption", "message",
                    "blog", "content", "email", "draft", "write",
                    "facebook", "twitter", "instagram", "linkedin", "social media"
                ]
                user_lower = user_message.lower()

                # Check if user message contains text creation keywords
                for keyword in text_creation_keywords:
                    if keyword in user_lower:
                        logger.warning(f"Blocked geni command due to text creation keyword '{keyword}' in: {user_message}")
                        return None  # Return None to use chat instead

            logger.info(f"Intent detected command: {command}")

            return {
                "action": "command",
                "command": command,
                "confidence": 0.9,  # High confidence since LLM explicitly chose this
                "reasoning": f"Parsed command: {command}",
            }

        except asyncio.TimeoutError:
            logger.warning(f"Intent detection timed out after {INTENT_TIMEOUT}s for message: {user_message[:50]}")
            return None
        except Exception as e:
            logger.error(f"Intent detection failed for '{user_message[:50]}': {e}")
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
