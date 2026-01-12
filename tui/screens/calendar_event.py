"""
Calendar event add/edit screen for TUI.
"""
from datetime import datetime, date
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, Button, Input
from textual.containers import Vertical, Horizontal


class CalendarEventScreen(ModalScreen):
    """Modal screen for adding or editing a calendar event."""

    CSS = """
    CalendarEventScreen {
        align: center middle;
    }

    #calendar-event-container {
        width: 60;
        max-height: 90%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        overflow-y: auto;
    }

    #calendar-event-title {
        text-align: center;
        text-style: bold;
        color: $text;
    }

    .form-row {
        width: 100%;
        height: auto;
    }

    .form-label {
        width: 100%;
        color: $text-muted;
    }

    .form-input {
        width: 100%;
    }

    #time-row {
        layout: horizontal;
    }

    #time-row > .form-input {
        width: 50%;
    }

    #event-buttons {
        width: 100%;
        height: auto;
        layout: horizontal;
        margin-top: 1;
    }

    #event-save-btn {
        width: 50%;
        background: $primary;
    }

    #event-cancel-btn {
        width: 50%;
        background: $surface;
        color: $text-muted;
    }
    """

    def __init__(self, event_data: dict = None):
        """
        Args:
            event_data: Optional dict with event fields for editing.
                        Keys: uid, title, date, time, end_time, location, description
        """
        super().__init__()
        self.event_data = event_data or {}
        self.is_edit = bool(event_data and event_data.get('uid'))

    def compose(self) -> ComposeResult:
        title = "Edit Event" if self.is_edit else "Add Event"

        with Vertical(id="calendar-event-container"):
            yield Static(title, id="calendar-event-title")

            with Vertical(classes="form-row"):
                yield Static("Title *", classes="form-label")
                yield Input(
                    value=self.event_data.get('title', ''),
                    placeholder="Event title",
                    id="event-title-input",
                    classes="form-input"
                )

            with Vertical(classes="form-row"):
                yield Static("Date * (YYYY-MM-DD)", classes="form-label")
                default_date = self.event_data.get('date', date.today().isoformat())
                yield Input(
                    value=default_date,
                    placeholder=date.today().isoformat(),
                    id="event-date-input",
                    classes="form-input"
                )

            with Vertical(classes="form-row"):
                yield Static("Time (HH:MM)", classes="form-label")
                with Horizontal(id="time-row"):
                    yield Input(
                        value=self.event_data.get('time', ''),
                        placeholder="Start 09:00",
                        id="event-time-input",
                        classes="form-input"
                    )
                    yield Input(
                        value=self.event_data.get('end_time', ''),
                        placeholder="End 10:00",
                        id="event-end-time-input",
                        classes="form-input"
                    )

            with Vertical(classes="form-row"):
                yield Static("Location", classes="form-label")
                yield Input(
                    value=self.event_data.get('location', ''),
                    placeholder="Location (optional)",
                    id="event-location-input",
                    classes="form-input"
                )

            with Vertical(classes="form-row"):
                yield Static("Description", classes="form-label")
                yield Input(
                    value=self.event_data.get('description', ''),
                    placeholder="Description (optional)",
                    id="event-description-input",
                    classes="form-input"
                )

            with Vertical(classes="form-row"):
                yield Static("Repeat", classes="form-label")
                yield Input(
                    value=self.event_data.get('recurrence', ''),
                    placeholder="daily, weekly Mon Wed Fri, monthly",
                    id="event-recurrence-input",
                    classes="form-input"
                )

            with Horizontal(id="event-buttons"):
                yield Button("Save", id="event-save-btn", variant="primary")
                yield Button("Cancel", id="event-cancel-btn")

    def on_mount(self) -> None:
        """Focus the title input on mount."""
        self.query_one("#event-title-input", Input).focus()

    def on_key(self, event) -> None:
        """Handle key press."""
        if event.key == "escape":
            self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "event-cancel-btn":
            self.dismiss(None)
        elif event.button.id == "event-save-btn":
            self._save_event()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in any input - move to next field or save."""
        inputs = [
            "#event-title-input",
            "#event-date-input",
            "#event-time-input",
            "#event-end-time-input",
            "#event-location-input",
            "#event-description-input",
            "#event-recurrence-input"
        ]
        current_id = f"#{event.input.id}"
        try:
            idx = inputs.index(current_id)
            if idx < len(inputs) - 1:
                # Move to next field
                self.query_one(inputs[idx + 1], Input).focus()
            else:
                # Last field - save
                self._save_event()
        except ValueError:
            self._save_event()

    def _save_event(self) -> None:
        """Save the event and dismiss."""
        title = self.query_one("#event-title-input", Input).value.strip()
        date_str = self.query_one("#event-date-input", Input).value.strip()
        time_str = self.query_one("#event-time-input", Input).value.strip()
        end_time_str = self.query_one("#event-end-time-input", Input).value.strip()
        location = self.query_one("#event-location-input", Input).value.strip()
        description = self.query_one("#event-description-input", Input).value.strip()
        recurrence = self.query_one("#event-recurrence-input", Input).value.strip()

        if not title:
            self.query_one("#event-title-input", Input).focus()
            return

        if not date_str:
            self.query_one("#event-date-input", Input).focus()
            return

        # Build command
        if self.is_edit:
            uid = self.event_data.get('uid')
            # Generate the first valid change command
            # Backend handles: title X, location X, description X, time/move X
            command = None

            if title != self.event_data.get('title', ''):
                command = f"cal edit {uid} title {title}"
            elif time_str != self.event_data.get('time', '') or date_str != self.event_data.get('date', ''):
                # Time or date changed - use move command with natural language
                try:
                    parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
                    date_formatted = parsed_date.strftime("%B %d, %Y")
                except ValueError:
                    date_formatted = date_str
                time_desc = f"{date_formatted}"
                if time_str:
                    time_desc += f" at {time_str}"
                if end_time_str:
                    time_desc += f" until {end_time_str}"
                command = f"cal edit {uid} move to {time_desc}"
            elif location != self.event_data.get('location', ''):
                command = f"cal edit {uid} location {location}"
            elif description != self.event_data.get('description', ''):
                command = f"cal edit {uid} description {description}"

            if not command:
                # No changes made
                self.dismiss(None)
                return
        else:
            # Build natural language add command
            event_desc = title

            # Parse date
            try:
                parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
                date_formatted = parsed_date.strftime("%B %d, %Y")  # e.g., "January 15, 2025"
                event_desc += f" on {date_formatted}"
            except ValueError:
                event_desc += f" on {date_str}"

            # Add time if provided
            if time_str:
                event_desc += f" at {time_str}"

            # Add end time if provided
            if end_time_str:
                event_desc += f" until {end_time_str}"

            # Add location if provided
            if location:
                event_desc += f" at {location}"

            # Add description if provided
            if description:
                event_desc += f" - {description}"

            # Add recurrence if provided
            if recurrence:
                event_desc += f", repeating {recurrence}"

            command = f"cal add {event_desc}"

        self.dismiss(command)
