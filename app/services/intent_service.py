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

        # Only run intent detection if message contains action keywords
        if not self._has_action_keywords(user_message):
            return None

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

CALENDAR COMMANDS:
"Add a meeting with Bob tomorrow at 3pm" -> cal add Meeting with Bob tomorrow at 3pm
"Schedule dentist appointment today at 2pm" -> cal add Dentist appointment today at 2pm
"Add lunch meeting Friday at noon" -> cal add Lunch meeting Friday at noon
"Add event: Team standup every Monday at 9am" -> cal add Team standup every Monday at 9am
"Schedule daily standup at 9am every weekday" -> cal add Daily standup at 9am every weekday
"Add weekly team meeting every Friday at 2pm" -> cal add Team meeting every Friday at 2pm
"Create recurring event: Gym every Monday Wednesday Friday at 6pm" -> cal add Gym every Monday Wednesday Friday at 6pm
"Schedule workout daily at 7am" -> cal add Workout daily at 7am
"Show my calendar today" -> cal today
"Show this week's events" -> cal week
"View my calendar" -> cal

CONTACT COMMANDS:
"Add John Doe 555-1234 john@example.com" -> contacts add John Doe 555-1234 john@example.com
"Save contact Jane Smith 555-5678" -> contacts add Jane Smith 555-5678
"Add new contact Bob Wilson" -> contacts add Bob Wilson
"Find contact named Sarah" -> contacts Sarah
"Search for Mike" -> contacts Mike
"Show all contacts" -> contacts all

TODO COMMANDS:
"Remind me to buy groceries" -> todo add Buy groceries
"Add task: call mom tomorrow" -> todo add Call mom tomorrow
"Don't forget to pay electric bill" -> todo add Pay electric bill
"Show my todos" -> todo
"Remove todo number 3" -> todo rm 3
"Delete todo 5" -> todo rm 5

EMAIL COMMANDS:
"Send email to john@example.com saying Hello there!" -> mail send john@example.com Hello there!
"Email Sarah: Meeting at 3pm tomorrow" -> mail send Sarah Meeting at 3pm tomorrow
"Check my email" -> mail
"Show unread emails" -> mail unread
"Read email 123" -> mail read 123
"Delete email 456" -> mail delete 456
"Archive this email 789" -> mail archive 789
"Reply to email 123: Thanks for the info!" -> mail reply verita84 123 Thanks for the info!
"Forward email 456 to john@example.com" -> mail forward verita84 456 john@example.com
"Show email folders" -> mail folders

SEARCH & IMAGES COMMANDS:
"Search for latest AI news" -> search latest AI news
"Look up weather forecast" -> search weather forecast
"Google Python tutorials" -> search Python tutorials
"Find images of cute cats" -> images cute cats
"Search images sunset" -> images sunset

IMAGE GENERATION:
"Generate image of a sunset over mountains" -> geni a sunset over mountains
"Create picture of a cute cat" -> geni a cute cat
"Draw a futuristic city" -> geni a futuristic city

YOUTUBE COMMANDS:
"Summarize https://youtube.com/watch?v=abc123" -> yt https://youtube.com/watch?v=abc123
"Download this song https://youtu.be/xyz789" -> ytdl https://youtu.be/xyz789

MUSIC COMMANDS:
"Play some music" -> music
"Play random music" -> music
"Play happy music" -> music mood happy
"Play chill vibes" -> music mood chill
"Search for Beatles songs" -> music search Beatles
"Browse my music" -> music browse
"Play track number 5" -> music play 5
"Skip this song" -> music skip
"Next track" -> music next

TRANSLATION:
"Translate to Spanish" -> translate Spanish
"Say that in French" -> translate French
"Translate email to German" -> translate email German

NEWS COMMANDS:
"Check the news" -> news
"News about technology" -> news technology
"Refresh news feed" -> news refresh
"Get daily news" -> dailynews

BUDGET COMMANDS:
"Show my bills" -> budget bills
"What bills are due" -> budget bills
"View budget" -> budget
"Pay the electric bill" -> budget pay electric
"Pay Netflix" -> budget pay Netflix
"I paid the Anthropic bill" -> budget pay Anthropic
"I paid the bill from Anthropic" -> budget pay Anthropic
"Paid my electric bill" -> budget pay electric
"Mark Netflix as paid" -> budget pay Netflix
"Add bill: Internet $80" -> budget add Internet 80

IMPORTANT: For budget/bill commands, respond ONLY with the command result. Do not add extra commentary, emojis, or enthusiastic responses. Keep it professional and brief.

TORRENT COMMANDS:
"Show torrents" -> torrents
"Search movies torrents" -> torrents movies
"Search anime torrents" -> torrents anime
"Add torrent magnet:..." -> torrents add magnet:...
"Pause torrent 3" -> torrents pause 3
"Resume torrent 2" -> torrents resume 2
"Delete torrent 5" -> torrents rm 5
"Search nyaa for anime" -> nyaa anime
"Download nyaa result 2" -> nyaa download 2

FIREWALL & LOGS (Admin):
"Check firewall" -> firewall
"Search firewall logs for 192.168.1.1" -> firewall search 192.168.1.1
"Analyze IP 10.0.0.5" -> firewall analyze 10.0.0.5
"Check system logs" -> logs

OTHER COMMANDS:
"Show help" -> help
"Refresh Miniflux" -> miniflux

NO ACTION (just chat):
"Hello" -> none
"How are you?" -> none
"Tell me a joke" -> none
"What's 2+2?" -> none
"Thanks!" -> none

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

    def _has_action_keywords(self, message: str) -> bool:
        """Check if message contains keywords that suggest an action request."""
        message_lower = message.lower()

        # Action keywords that suggest user wants to DO something
        action_keywords = {
            # Calendar
            "add to calendar",
            "add event",
            "schedule",
            "create event",
            "add to my calendar",
            "put on calendar",
            "calendar event",
            "make calendar",
            "make event",
            "new event",
            "add this to calendar",
            "add this to my calendar",
            "calendar from this",
            "event from this",
            # Contacts
            "save contact",
            "add contact",
            "save number",
            "save phone",
            "new contact",
            "create contact",
            # Todo
            "remind me",
            "add todo",
            "add task",
            "add to list",
            "don't forget",
            "remember to",
            "todo from",
            "task from",
            # Email
            "send email",
            "send mail",
            "email to",
            "mail to",
            "check email",
            "check mail",
            "my emails",
            "my mail",
            "compose email",
            # Music
            "play music",
            "play song",
            "play something",
            "play some",
            # Search
            "search for",
            "look up",
            "google",
            "find info",
            # Image
            "generate image",
            "create image",
            "make image",
            "draw",
            "generate picture",
            # YouTube
            "summarize video",
            "summarize this video",
            "youtube",
            "download song",
            "download video",
            # Translation
            "translate to",
            "translate this",
            "say that in",
            # News
            "check news",
            "what's the news",
            "news about",
            # Budget/Bills
            "show my bills",
            "my bills",
            "upcoming bills",
            "budget",
            "what bills",
            "pay bill",
        }

        for keyword in action_keywords:
            if keyword in message_lower:
                return True

        # Also check for patterns like "add X to calendar" or "email X saying"
        import re

        action_patterns = [
            r"\badd\b.*\b(calendar|todo|contact|task|list)\b",
            r"\b(create|make|schedule)\b.*\b(event|meeting|appointment)\b",
            r"\b(send|email|mail)\b.*\b(to|saying)\b",
            r"\bplay\b.*\b(music|song|track)\b",
            r"\bsearch\b",
            r"\bgenerate?\b.*\b(image|picture|art)\b",
        ]

        for pattern in action_patterns:
            if re.search(pattern, message_lower):
                return True

        return False


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
