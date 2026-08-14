"""
Intent Detection Service - Simplified LLM-based command detection.

This service uses direct LLM training to convert natural language into commands,
eliminating complex JSON parsing and field validation.
"""

import asyncio
import logging
import re
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
        from app.services import settings_store
        from app.services.chat_service import ChatService

        self.chat_service = ChatService(db, user=user)

        # Load confidence threshold from settings (kept for compatibility)
        self.confidence_threshold = settings_store.get_float("intent_confidence_threshold", 0.7)

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
"Download song https://youtube.com/watch?v=xyz" -> ytdl https://youtube.com/watch?v=xyz
"Download video https://youtube.com/watch?v=xyz" -> ytdl video https://youtube.com/watch?v=xyz
"Download this video https://youtube.com/watch?v=xyz" -> ytdl video https://youtube.com/watch?v=xyz
"Download the video from this link" -> ytdl video
"Download this video" -> ytdl video
"Get this song" -> ytdl
"Download the music from this link" -> ytdl

MUSIC:
"Play music" -> music
"Play happy music" -> music mood happy
"Skip song" -> music skip
"play song nugget" -> music search nugget
"play song Yesterday" -> music search Yesterday
"Play song Bohemian Rhapsody" -> music search Bohemian Rhapsody
"play the song Hotel California" -> music search Hotel California
"I want to listen to Stairway to Heaven" -> music search Stairway to Heaven
"put on some Beatles" -> music search Beatles

TRANSLATION:
"Translate to Spanish" -> translate Spanish
"Translate email to German" -> translate email German

NEWS:
"Check the news" -> news
"News about technology" -> news technology

TORRENT:
"Show torrents" -> torrents
"Search for Ubuntu torrents" -> torrents search Ubuntu
"Pause torrent 3" -> torrents pause 3

OTHER:
"Show help" -> help

NO ACTION (just chat):
"Hello" -> none
"How are you?" -> none

IMPORTANT RULES:
1. CRITICAL: "geni" is ONLY for VISUAL images/pictures/photos. If user wants TEXT (social media post, article, caption, message), respond "none" to use chat
5. Social media posts, blog posts, articles, captions, tweets, LinkedIn posts = TEXT CONTENT = ALWAYS respond "none"
6. Images, pictures, photos, artwork, drawings, illustrations = VISUAL CONTENT = use "geni" command
7. When in doubt about create/generate/make, ask yourself: "Is this TEXT or a PICTURE?" - TEXT = none, PICTURE = geni
8. DO NOT include any explanation - respond with ONLY the command or "none"
9. DO NOT add quotes, markdown, or extra text
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
            # Suppress Qwen3 thinking for intent detection — it needs a fast single-token answer
            # (a command keyword or "none"), not extended reasoning. Without /no_think the model
            # can spend 10-30s in <think> mode and hit the timeout below, silently degrading all
            # intent-based commands to regular chat. inject_no_think is safe for non-Qwen3 models
            # (they simply treat it as trailing text they ignore).
            from app.services.text_utils import inject_no_think
            messages = inject_no_think(messages)

            # Use timeout to prevent hanging
            INTENT_TIMEOUT = 8  # seconds - keep it short to avoid delaying user experience
            response = await asyncio.wait_for(self.chat_service.chat(messages), timeout=INTENT_TIMEOUT)

            # Clean up response
            command = response.strip()
            logger.info(f"Intent raw response: {command[:100]!r}")
            
            # Parse and validate command from LLM response
            command = self._parse_command(command)
            
            # Validate command is not garbage/none/emoji spam
            if not self._is_valid_command(command):
                return None
            
            # A LINK IS SOMETHING TO READ, NOT SOMETHING TO LOOK UP. "Summarize this page:
            # https://www.cnn.com/" came back from this classifier as `search https://www.cnn.com/`,
            # so instead of fetching the page the node ran a WEB SEARCH FOR THE URL STRING and the
            # model summarized whatever that returned. Chat already fetches every URL in a message,
            # so dropping the intent here is what makes the page get read.
            #
            # Only the lookup verbs are dropped — `yt`/`ytdl`/`pin screenshot` are real things to do
            # WITH a link, and they keep working.
            if command.split()[0].lower() in ("search", "images", "news"):
                from app.services.search_service import SearchService as _SS
                if _SS.extract_urls(user_message):
                    logger.info("Intent %r dropped: the message links a page, and reading it is "
                                "chat's job", command)
                    return None

            # HARD SAFETY CHECK: Block geni command for text content creation
            if command.lower().startswith("geni"):
                text_creation_keywords = [
                    "post", "article", "tweet", "caption", "message", "blog",
                    "content", "email", "draft", "write", "facebook", "twitter",
                    "instagram", "linkedin", "social media",
                ]
                user_lower = user_message.lower()
                for keyword in text_creation_keywords:
                    if keyword in user_lower:
                        logger.warning(f"Blocked geni command due to text creation keyword '{keyword}'")
                        return None

            logger.info(f"Intent detected command: {command}")

            return {
                "action": "command",
                "command": command,
                "confidence": 0.9,  # High confidence since LLM explicitly chose this
                "reasoning": f"Parsed command: {command}",
            }

        except asyncio.TimeoutError:
            logger.warning(f"Intent detection timed out after {INTENT_TIMEOUT}s "
                           f"({len(user_message or '')} chars)")
            return None
        except Exception as e:
            logger.error(f"Intent detection failed ({len(user_message or '')} chars): {e}")
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

    def _parse_command(self, raw_response: str) -> Optional[str]:
        """Parse and validate command from LLM response."""
        if not raw_response:
            return None
            
        command = raw_response.strip()
        
        # Remove markdown code blocks
        if command.startswith("```"):
            lines = [l for l in command.split("\n") if not l.startswith("```")]
            command = "\n".join(lines).strip()
        
        # Take first non-empty, non-comment line
        for line in command.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                command = line
                break
        
        # Remove common prefixes
        for prefix in ["command:", "output:", "result:", "answer:"]:
            if command.lower().startswith(prefix):
                command = command[len(prefix):].strip()
        
        # Remove quotes
        if (command.startswith('"') and command.endswith('"')) or \
           (command.startswith("'") and command.endswith("'")):
            command = command[1:-1].strip()
        
        return command if command else None
    
    def _is_valid_command(self, command: str) -> bool:
        """Check if command is valid (not garbage/none/emoji spam or model greeting)."""
        if not command:
            return False
        
        # Filter invalid cases
        if not self._is_valid_command_format(command):
            return False
        
        # Filter model greetings (model responded with a greeting instead of command)
        if self._is_model_greeting(command):
            return False
        
        return True
    
    def _is_valid_command_format(self, command: str) -> bool:
        """Check basic format validity (not none/short/emoji garbage or a full sentence)."""
        cmd_lower = command.lower()
        if "none" in cmd_lower:
            return False
        if len(command) <= 1:
            return False
        alphanumeric = re.sub(r'[^\w]', '', command)
        if len(alphanumeric) < 3:
            return False
        # Commands are short — reject anything that looks like a full sentence response
        # (model produced a chat answer instead of a command keyword)
        words = command.split()
        if len(words) > 10:
            return False
        # Commands don't start with "to convert", "to calculate", "to", etc.
        first_word = cmd_lower.split()[0].strip("!.,🔎") if words else ""
        if first_word in {"to", "the", "i", "it", "this", "that", "you", "your", "please", "sorry", "of", "is", "are", "was"}:
            return False
        return True
    
    def _is_model_greeting(self, command: str) -> bool:
        """Check if model responded with a greeting or farewell instead of a command."""
        words = command.lower().split()
        greeting_words = {"hi", "hello", "hey", "here", "sure", "okay", "how", "what", "let",
                          "bye", "goodbye", "farewell", "ciao", "later", "cheers"}

        # Strip leading emoji/punctuation from first word for matching
        first_word = words[0].strip("!.,👋🖐✋") if words else ""

        # Single-word farewell/greeting (e.g. "bye", "👋")
        if len(words) == 1 and (first_word in greeting_words or not first_word.isalpha()):
            return True

        # Check first few words for greeting/farewell indicators
        if len(words) >= 1 and (first_word in greeting_words or any(w.strip("!.,") in greeting_words for w in words[:3])):
            return True
        return False
    
    def _is_simple_greeting(self, message: str) -> bool:
        """Check if message is a simple greeting (no action needed)."""
        msg_lower = message.lower().strip()
        
        # Common greeting words (must start the message)
        greeting_words = (
            "hi", "hey", "yo", "sup", "hello", "wassup", "wsup", 
            "hiya", "greetings", "howdy", "hola", "namaste"
        )
        
        # Check exact match OR starts with greeting word followed by punctuation/space
        if msg_lower in greeting_words:
            return True
        
        # Must start with greeting word (not just contain it)
        for g in greeting_words:
            if msg_lower.startswith(g):
                # After greeting, should have only punctuation or short words (max 3 more)
                remaining = msg_lower[len(g):].strip()
                if not remaining or len(remaining.split()) <= 2:
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
