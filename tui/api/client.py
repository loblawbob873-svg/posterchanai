"""
HTTP API client for Posterchanai server.
"""

import httpx
from typing import Optional, List
from .models import User, Conversation, ConversationWithMessages, LoginResponse, UserSettings


class APIError(Exception):
    """API request error."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API Error {status_code}: {detail}")


class APIClient:
    """
    HTTP client for Posterchanai REST API.

    Usage:
        client = APIClient("http://localhost:3051")
        token = await client.login("user", "pass")
        conversations = await client.list_conversations()
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _headers(self) -> dict:
        """Get request headers with auth."""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make authenticated request."""
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers.update(self._headers())

        response = await self.client.request(method, url, headers=headers, **kwargs)

        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise APIError(response.status_code, detail)

        if response.status_code == 204:
            return {}

        return response.json()

    async def get(self, path: str, **kwargs) -> dict:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> dict:
        return await self._request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs) -> dict:
        return await self._request("PUT", path, **kwargs)

    async def delete(self, path: str, **kwargs) -> dict:
        return await self._request("DELETE", path, **kwargs)

    # Auth endpoints

    async def login(self, username: str, password: str) -> str:
        """
        Login and get JWT token.

        Returns:
            JWT access token
        """
        response = await self.post("/api/auth/login", json={
            "username": username,
            "password": password
        })
        login_data = LoginResponse(**response)
        self.token = login_data.access_token
        return self.token

    async def get_current_user(self) -> User:
        """Get current authenticated user."""
        data = await self.get("/api/auth/me")
        return User(**data)

    async def logout(self):
        """Logout (clear token)."""
        self.token = None

    # Conversation endpoints

    async def list_conversations(self) -> List[Conversation]:
        """List all conversations for current user."""
        data = await self.get("/api/conversations")
        return [Conversation(**c) for c in data]

    async def create_conversation(self, title: str = "New Chat") -> Conversation:
        """Create a new conversation."""
        data = await self.post("/api/conversations", json={"title": title})
        return Conversation(**data)

    async def get_conversation(self, conversation_id: int) -> ConversationWithMessages:
        """Get conversation with messages."""
        data = await self.get(f"/api/conversations/{conversation_id}")
        return ConversationWithMessages(**data)

    async def delete_conversation(self, conversation_id: int):
        """Delete a conversation."""
        await self.delete(f"/api/conversations/{conversation_id}")

    async def delete_all_conversations(self):
        """Delete all conversations."""
        await self.delete("/api/conversations")

    # Command endpoint (for non-streaming commands)

    async def execute_command(self, command: str) -> dict:
        """Execute a command and get response."""
        return await self.post("/api/command", json={"command": command})

    # Settings endpoints

    async def get_user_settings(self) -> dict:
        """Get current user settings."""
        return await self.get("/api/auth/settings")

    async def update_user_settings(self, settings: dict) -> dict:
        """Update user settings."""
        return await self.put("/api/auth/settings", json=settings)

    async def get_contact_emails(self) -> list:
        """Get contact email addresses for autocomplete."""
        try:
            return await self.get("/api/mail/contacts/emails")
        except Exception:
            return []
