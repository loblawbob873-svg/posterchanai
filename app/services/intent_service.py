"""
Intent Detection Service - AI-powered action detection and execution.

This service analyzes natural language input and automatically takes action
using built-in features (calendar, contacts, email, todo, music, etc.)

Example: User pastes an email with event info and says "Add this to my calendar"
The service will:
1. Detect the intent (add calendar event)
2. Extract data (date, time, location, title)
3. Execute the action
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)

# Define all available actions that the AI can detect and execute
AVAILABLE_ACTIONS = {
    "calendar_add": {
        "description": "Add an event to the calendar",
        "required_fields": ["summary", "start_time"],
        "optional_fields": ["end_time", "location", "description", "rrule"],
        "command": "cal add",
    },
    "calendar_view": {
        "description": "View calendar events",
        "required_fields": [],
        "optional_fields": ["period"],  # today, week, month
        "command": "cal",
    },
    "contact_add": {
        "description": "Add a new contact",
        "required_fields": ["name"],
        "optional_fields": ["phone", "email", "organization"],
        "command": "contacts add",
    },
    "contact_search": {
        "description": "Search for a contact",
        "required_fields": ["query"],
        "optional_fields": [],
        "command": "contacts",
    },
    "todo_add": {
        "description": "Add a task to the todo list",
        "required_fields": ["task"],
        "optional_fields": ["due_date", "priority"],
        "command": "todo add",
    },
    "todo_list": {"description": "List todo items", "required_fields": [], "optional_fields": [], "command": "todo"},
    "todo_remove": {
        "description": "Remove/complete a todo item",
        "required_fields": ["item_number"],
        "optional_fields": [],
        "command": "todo rm",
    },
    "email_send": {
        "description": "Send an email",
        "required_fields": ["recipient", "message"],
        "optional_fields": ["subject"],
        "command": "mail send",
    },
    "email_check": {
        "description": "Check/list emails",
        "required_fields": [],
        "optional_fields": ["folder", "unread_only"],
        "command": "mail",
    },
    "email_reply": {
        "description": "Reply to an email",
        "required_fields": ["account", "message_id", "reply_text"],
        "optional_fields": [],
        "command": "mail reply",
    },
    "music_play": {
        "description": "Play music based on criteria",
        "required_fields": [],
        "optional_fields": ["query", "mood", "number"],
        "command": "music",
    },
    "music_search": {
        "description": "Search for music",
        "required_fields": ["query"],
        "optional_fields": [],
        "command": "music search",
    },
    "search_web": {
        "description": "Search the web for information",
        "required_fields": ["query"],
        "optional_fields": [],
        "command": "search",
    },
    "generate_image": {
        "description": "Generate an AI image",
        "required_fields": ["prompt"],
        "optional_fields": [],
        "command": "geni",
    },
    "news_check": {
        "description": "Check news updates",
        "required_fields": [],
        "optional_fields": ["topic"],
        "command": "news",
    },
    "youtube_summarize": {
        "description": "Summarize a YouTube video",
        "required_fields": ["url"],
        "optional_fields": [],
        "command": "yt",
    },
    "translate": {
        "description": "Translate text to another language",
        "required_fields": ["language"],
        "optional_fields": ["text"],
        "command": "translate",
    },
    "pay_bill": {
        "description": "Pay Bill",
        "required_fields": ["bill_name"],
        "optional_fields": [],
        "command": "pay bill",
    },
    "budget_bills": {
        "description": "Show upcoming bills and budget information",
        "required_fields": [],
        "optional_fields": [],
        "command": "budget bills",
    },
    "none": {
        "description": "No specific action needed - regular chat",
        "required_fields": [],
        "optional_fields": [],
        "command": None,
    },
}

# Build action descriptions for the prompt
ACTION_DESCRIPTIONS = "\n".join(
    [f"- {action}: {details['description']}" for action, details in AVAILABLE_ACTIONS.items()]
)


class IntentService:
    """
    Detects user intent from natural language and extracts structured data.
    """

    def __init__(self, db: Session, user: Optional["User"] = None):
        self.db = db
        self.user = user
        from app.models import Setting
        from app.services.chat_service import ChatService

        self.chat_service = ChatService(db, user=user)

        # Load confidence threshold from settings
        threshold_setting = db.query(Setting).filter(Setting.key == "intent_confidence_threshold").first()
        self.confidence_threshold = float(threshold_setting.value if threshold_setting else "0.7")

    async def detect_intent(self, user_message: str, context: str = "") -> Optional[dict]:
        """
        Analyze user message to detect actionable intent.

        Args:
            user_message: The user's input message
            context: Additional context (e.g., pasted email content, previous messages)

        Returns:
            Dictionary with detected action and extracted data, or None for regular chat
        """
        # Quick checks for obvious non-action messages
        if self._is_simple_greeting(user_message):
            return None

        # Skip intent detection for very short messages (likely just chat)
        if len(user_message.strip()) < 10:
            return None

        # Only run intent detection if message contains action keywords
        # This prevents slow LLM calls on regular chat messages
        if not self._has_action_keywords(user_message):
            return None

        # Limit context size to prevent slow processing
        MAX_CONTEXT_CHARS = 4000
        if context and len(context) > MAX_CONTEXT_CHARS:
            # Truncate context but keep beginning (usually has key info like dates)
            context = context[:MAX_CONTEXT_CHARS] + "\n...[truncated]..."

        # Build the intent detection prompt
        today = datetime.now()
        tomorrow = today + timedelta(days=1)
        detection_prompt = f"""You are a smart assistant that extracts actionable data from messages and emails.

CURRENT DATE/TIME: {today.strftime("%A, %B %d, %Y at %I:%M %p")} (LOCAL TIME)
TOMORROW: {tomorrow.strftime("%A, %B %d, %Y")}

USER REQUEST:
{user_message}

{f"CONTENT TO ANALYZE:{chr(10)}{context}" if context else ""}

TASK: Determine what action the user wants and extract ALL relevant data.

AVAILABLE ACTIONS AND REQUIRED FIELDS:
- calendar_add: summary (event title), start_time (ISO format LOCAL time), optional: end_time, location, description
- calendar_view: optional: period (today/week/month)
- contact_add: name, optional: phone, email, organization
- contact_search: query
- todo_add: task (the task description)
- todo_list: (no fields needed)
- email_send: recipient (email address), message
- email_check: optional: unread_only (true/false)
- music_play: optional: mood, query, number
- search_web: query
- generate_image: prompt (image description)
- youtube_summarize: url
- translate: language
- budget_bills: show upcoming bills (no fields needed)
- pay_bill: pay bill (bill_name)
- none: regular chat, no action needed

CRITICAL TIME RULES:
1. Output times in LOCAL time exactly as the user specifies - DO NOT convert to UTC
2. "6PM" = "18:00:00", "6pm" = "18:00:00", "6 PM" = "18:00:00"
3. "3pm" = "15:00:00", "9am" = "09:00:00", "12pm" = "12:00:00", "12am" = "00:00:00"
4. "tomorrow at 6PM" = "{tomorrow.strftime("%Y-%m-%d")}T18:00:00"
5. DO NOT add timezone suffix (no Z, no +00:00)
6. "night" typically means evening (6PM-9PM range)

EXTRACTION RULES:
1. For EMAILS about events/meetings, extract: event name, date, time, location from the email body
2. Convert relative dates: "tomorrow" = {tomorrow.strftime("%Y-%m-%d")}, "today" = {today.strftime("%Y-%m-%d")}
3. For event titles, use the main subject/purpose, not the email subject line

EXAMPLES:
- "tomorrow at 6PM" → start_time: "{tomorrow.strftime("%Y-%m-%d")}T18:00:00"
- "meeting Friday at 2pm" → start_time: "2026-01-17T14:00:00" (next Friday)
- "remind me to call mom" → action: todo_add, task: "Call mom"

RESPOND WITH ONLY THIS JSON:
{{
    "action": "<action_name>",
    "confidence": <0.0-1.0>,
    "extracted_data": {{
        "summary": "Event title here",
        "start_time": "YYYY-MM-DDTHH:MM:SS",
        "location": "optional location",
        "description": "optional details"
    }},
    "reasoning": "brief explanation of what was extracted"
}}"""

        messages = [
            {
                "role": "system",
                "content": "You are a data extraction assistant. Your job is to parse emails, messages, and text to extract structured information like event dates, contact details, and task descriptions. Always return valid JSON. Be thorough - scan the entire content for relevant data like dates, times, names, and locations.",
            },
            {"role": "user", "content": detection_prompt},
        ]

        try:
            # Use timeout to prevent hanging on slow LLM responses
            INTENT_TIMEOUT = 30  # seconds
            response = await asyncio.wait_for(self.chat_service.chat(messages), timeout=INTENT_TIMEOUT)
            result = self._parse_json_response(response)

            if not result:
                return None

            action = result.get("action", "none")
            confidence = float(result.get("confidence", 0))

            # Only act on high-confidence detections (using configurable threshold)
            if action == "none" or confidence < self.confidence_threshold:
                return None

            # Validate the action exists
            if action not in AVAILABLE_ACTIONS:
                logger.warning(f"Unknown action detected: {action}")
                return None

            # Validate required fields
            action_def = AVAILABLE_ACTIONS[action]
            extracted_data = result.get("extracted_data", {})

            missing_fields = [field for field in action_def["required_fields"] if not extracted_data.get(field)]

            if missing_fields:
                logger.debug(f"Missing required fields for {action}: {missing_fields}")
                # Return partial result so we can ask user for missing info
                return {
                    "action": action,
                    "confidence": confidence,
                    "data": extracted_data,
                    "missing_fields": missing_fields,
                    "command": action_def["command"],
                    "reasoning": result.get("reasoning", ""),
                }

            return {
                "action": action,
                "confidence": confidence,
                "data": extracted_data,
                "missing_fields": [],
                "command": action_def["command"],
                "reasoning": result.get("reasoning", ""),
            }

        except asyncio.TimeoutError:
            logger.warning(f"Intent detection timed out after {INTENT_TIMEOUT}s")
            return None
        except Exception as e:
            logger.error(f"Intent detection failed: {e}")
            return None

    def build_command_string(self, intent_result: dict) -> Optional[str]:
        """
        Convert detected intent into an executable command string.
        """
        if not intent_result or intent_result.get("action") == "none":
            return None

        action = intent_result["action"]
        data = intent_result.get("data", {})
        command = intent_result.get("command")

        if not command:
            return None

        # Build command based on action type
        if action == "calendar_add":
            # Build natural language string for cal add
            summary = data.get("summary", "Event")
            start_time = data.get("start_time", "")
            location = data.get("location", "")
            description = data.get("description", "")

            event_str = summary
            if start_time:
                event_str += f" {start_time}"
            if location:
                event_str += f" at {location}"
            if description:
                event_str += f" - {description}"

            return f"cal add {event_str}"

        elif action == "calendar_view":
            period = data.get("period", "week")
            return f"cal {period}"

        elif action == "contact_add":
            name = data.get("name", "")
            phone = data.get("phone", "")
            email = data.get("email", "")

            cmd = f"contacts add {name}"
            if phone:
                cmd += f" {phone}"
            if email:
                cmd += f" {email}"
            return cmd

        elif action == "contact_search":
            return f"contacts {data.get('query', '')}"

        elif action == "todo_add":
            task = data.get("task", "")
            return f"todo add {task}"

        elif action == "todo_list":
            return "todo"

        elif action == "todo_remove":
            return f"todo rm {data.get('item_number', '')}"

        elif action == "email_send":
            recipient = data.get("recipient", "")
            message = data.get("message", "")
            return f"mail send {recipient} {message}"

        elif action == "email_check":
            if data.get("unread_only"):
                return "mail unread"
            return "mail"

        elif action == "email_reply":
            account = data.get("account", "")
            msg_id = data.get("message_id", "")
            reply = data.get("reply_text", "")
            return f"mail reply {account} {msg_id} {reply}"

        elif action == "music_play":
            if data.get("mood"):
                return f"music mood {data['mood']}"
            if data.get("query"):
                return f"music search {data['query']}"
            if data.get("number"):
                return f"music play {data['number']}"
            return "music random"

        elif action == "music_search":
            return f"music search {data.get('query', '')}"

        elif action == "search_web":
            return f"search {data.get('query', '')}"

        elif action == "generate_image":
            return f"geni {data.get('prompt', '')}"

        elif action == "news_check":
            topic = data.get("topic", "")
            if topic:
                return f"news {topic}"
            return "news"

        elif action == "youtube_summarize":
            return f"yt {data.get('url', '')}"

        elif action == "translate":
            lang = data.get("language", "")
            return f"translate {lang}"

        elif action == "pay_bill":
            bill_name = data.get("bill_name", "")
            return f"budget pay {bill_name}"

        elif action == "budget_bills":
            return "budget bills"

        # Fallback: use the command directly from AVAILABLE_ACTIONS if it exists
        if action in AVAILABLE_ACTIONS and AVAILABLE_ACTIONS[action].get("command"):
            return AVAILABLE_ACTIONS[action]["command"]

        return None

    async def _execute_calendar_add(self, data: dict) -> Optional[dict]:
        """
        Execute calendar add directly with extracted data.
        This avoids double LLM parsing and timezone issues.
        """
        from dateutil import parser as date_parser

        from app.models import UserSetting
        from app.services.caldav_service import add_event_to_calendar

        if not self.user:
            return {"type": "text", "content": "Please log in to add calendar events."}

        # Get user's calendar settings
        cal_settings = (
            self.db.query(UserSetting)
            .filter(UserSetting.user_id == self.user.id, UserSetting.key == "caldav_calendars")
            .first()
        )

        if not cal_settings or not cal_settings.value:
            return {"type": "text", "content": "No calendar configured. Go to Settings > Calendar to add one."}

        try:
            import json

            calendars = json.loads(cal_settings.value)
            if not calendars:
                return {"type": "text", "content": "No calendar configured. Go to Settings > Calendar to add one."}
        except Exception:
            return {"type": "text", "content": "Invalid calendar configuration."}

        # Extract event data
        summary = data.get("summary", "Event")
        description = data.get("description", "")
        location = data.get("location")
        start_str = data.get("start_time", "")
        end_str = data.get("end_time", "")
        rrule = data.get("rrule")

        logger.info(
            f"Calendar add - extracted data: summary={summary}, start={start_str}, end={end_str}, location={location}"
        )

        if not start_str:
            return {"type": "text", "content": "Could not determine event time."}

        try:
            # Parse the ISO datetime string
            # The LLM outputs local time, so parse as naive and treat as local
            start_time = date_parser.parse(start_str)
            logger.info(f"Calendar add - parsed start_time: {start_time} (tzinfo={start_time.tzinfo})")

            if start_time.tzinfo is not None:
                # If timezone included, convert to local naive
                start_time = start_time.astimezone().replace(tzinfo=None)
                logger.info(f"Calendar add - converted to naive local: {start_time}")

            # Default end time to 1 hour after start
            if end_str:
                end_time = date_parser.parse(end_str)
                if end_time.tzinfo is not None:
                    end_time = end_time.astimezone().replace(tzinfo=None)
            else:
                end_time = start_time + timedelta(hours=1)

            # Add to first calendar
            cal = calendars[0]
            logger.info(f"Calendar add - calling add_event_to_calendar with start_time={start_time}")
            success = add_event_to_calendar(
                cal["url"],
                cal["username"],
                cal["password"],
                summary,
                description,
                start_time,
                end_time,
                location,
                rrule,
            )

            if success:
                time_str = start_time.strftime("%A, %B %d at %I:%M %p")
                logger.info(f"Calendar add - success! Displaying time_str={time_str}")
                location_str = f"\n📍 {location}" if location else ""
                recurrence_str = f"\n🔁 {rrule}" if rrule else ""
                return {
                    "type": "text",
                    "content": f"✅ Event added: **{summary}**\n\n📅 {time_str}{location_str}{recurrence_str}",
                }
            else:
                return {"type": "text", "content": "❌ Failed to add event to calendar."}

        except Exception as e:
            logger.error(f"Calendar add error: {e}")
            return {"type": "text", "content": f"Error adding event: {str(e)}"}

    async def execute_intent(self, intent_result: dict) -> Optional[dict]:
        """
        Execute the detected intent directly without building a command string.
        This allows for more precise control and better error handling.
        """
        from app.services.command_service import CommandService

        if not intent_result or intent_result.get("action") == "none":
            return None

        # Check for missing fields first
        if intent_result.get("missing_fields"):
            return {
                "type": "clarification_needed",
                "action": intent_result["action"],
                "missing_fields": intent_result["missing_fields"],
                "content": self._build_clarification_message(intent_result),
            }

        action = intent_result.get("action")
        data = intent_result.get("data", {})

        # Handle calendar_add directly to avoid double LLM parsing and timezone issues
        if action == "calendar_add":
            return await self._execute_calendar_add(data)

        command_str = self.build_command_string(intent_result)
        if not command_str:
            return None

        # Execute via CommandService
        command_service = CommandService(self.db, user=self.user)
        command, arg = command_service.parse_command(command_str)

        if command:
            result = await command_service.execute_command(command, arg)

            # Add context about what was done
            action_name = AVAILABLE_ACTIONS.get(intent_result["action"], {}).get("description", intent_result["action"])
            result["intent_action"] = action_name
            result["intent_data"] = intent_result.get("data", {})

            return result

        return None

    def _parse_json_response(self, response: str) -> Optional[dict]:
        """Parse JSON from LLM response, handling markdown code blocks."""
        response = response.strip()

        # Handle markdown code blocks
        code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
        if code_block_match:
            response = code_block_match.group(1).strip()
        else:
            # Try to extract JSON object directly
            json_match = re.search(r"\{[\s\S]*\}", response)
            if json_match:
                response = json_match.group(0)

        try:
            return json.loads(response)
        except json.JSONDecodeError as e:
            logger.debug(f"Failed to parse intent JSON: {e}")
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

    def _build_clarification_message(self, intent_result: dict) -> str:
        """Build a message asking for missing information."""
        action = intent_result["action"]
        missing = intent_result["missing_fields"]
        action_desc = AVAILABLE_ACTIONS.get(action, {}).get("description", action)

        if len(missing) == 1:
            return f"I understood you want to {action_desc.lower()}, but I need the **{missing[0]}**. Could you provide that?"
        else:
            missing_str = ", ".join(f"**{f}**" for f in missing)
            return f"I understood you want to {action_desc.lower()}, but I'm missing some information: {missing_str}. Could you provide these details?"


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
        logger.info(f"Detected intent: {intent['action']} (confidence: {intent['confidence']:.2f})")
        return await service.execute_intent(intent)

    return None
