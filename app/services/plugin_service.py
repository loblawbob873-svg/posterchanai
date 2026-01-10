"""Plugin Service - Execute user-defined AI plugins"""

import json
import re
import httpx
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.models import Plugin


class PluginService:
    """Service for managing and executing AI plugins"""

    def __init__(self, db: Session):
        self.db = db

    def get_plugins_for_user(self, user_id: int) -> List[Plugin]:
        """Get all plugins available to a user (their own + global with access)"""
        all_plugins = self.db.query(Plugin).filter(
            Plugin.enabled == True,
            or_(Plugin.user_id == user_id, Plugin.user_id == None)
        ).all()

        # Filter by allowed_users if set
        result = []
        for plugin in all_plugins:
            # User's own plugins are always visible
            if plugin.user_id == user_id:
                result.append(plugin)
            # Global plugins: check allowed_users
            elif plugin.user_id is None:
                if plugin.allowed_users is None or plugin.allowed_users == '':
                    # No restriction - available to all
                    result.append(plugin)
                else:
                    # Check if user is in allowed list
                    allowed_ids = [int(x.strip()) for x in plugin.allowed_users.split(',') if x.strip()]
                    if user_id in allowed_ids:
                        result.append(plugin)

        return result

    def get_plugin_by_name(self, name: str, user_id: int) -> Optional[Plugin]:
        """Get a specific plugin by name"""
        return self.db.query(Plugin).filter(
            Plugin.name == name,
            Plugin.enabled == True,
            or_(Plugin.user_id == user_id, Plugin.user_id == None)
        ).first()

    def build_system_prompt_addition(self, user_id: int) -> str:
        """Build the system prompt addition describing available plugins"""
        plugins = self.get_plugins_for_user(user_id)
        if not plugins:
            return ""

        prompt = "\n\n## Available Plugins\n"
        prompt += "You have access to external plugins. When the user's request matches a plugin's capabilities, "
        prompt += "use the plugin by outputting a tool call in this exact format:\n"
        prompt += "<tool name=\"plugin_name\" action=\"action_name\">{\"param\": \"value\"}</tool>\n\n"
        prompt += "Available plugins:\n\n"

        for plugin in plugins:
            try:
                actions = json.loads(plugin.actions)
            except json.JSONDecodeError:
                continue

            prompt += f"### {plugin.name}\n"
            prompt += f"{plugin.description}\n"
            prompt += "Actions:\n"

            for action in actions:
                prompt += f"- **{action['name']}**: {action['description']}\n"
                if action.get('params'):
                    prompt += f"  Parameters: {', '.join(action['params'])}\n"

            prompt += "\n"

        prompt += "After calling a plugin, wait for the result before responding to the user. "
        prompt += "Summarize the result naturally in your response.\n"

        return prompt

    def parse_tool_calls(self, response: str) -> List[Dict[str, Any]]:
        """Parse tool calls from AI response"""
        pattern = r'<tool\s+name="([^"]+)"\s+action="([^"]+)">\s*(\{[^}]*\})\s*</tool>'
        matches = re.findall(pattern, response, re.DOTALL)

        tool_calls = []
        for match in matches:
            plugin_name, action_name, params_str = match
            try:
                params = json.loads(params_str)
            except json.JSONDecodeError:
                params = {}

            tool_calls.append({
                'plugin': plugin_name,
                'action': action_name,
                'params': params
            })

        return tool_calls

    def strip_tool_calls(self, response: str) -> str:
        """Remove tool calls from response text"""
        pattern = r'<tool\s+name="[^"]+"\s+action="[^"]+">\s*\{[^}]*\}\s*</tool>'
        return re.sub(pattern, '', response).strip()

    # Cache for login-based sessions
    _session_cache: Dict[str, str] = {}

    async def _get_login_session(self, plugin, client: httpx.AsyncClient) -> Optional[str]:
        """Authenticate and get session cookie for login-based auth"""
        cache_key = f"{plugin.id}_{plugin.base_url}"

        # Check cache first
        if cache_key in PluginService._session_cache:
            return PluginService._session_cache[cache_key]

        try:
            # Parse credentials from auth_value (JSON: {"username": "x", "password": "y"})
            creds = json.loads(plugin.auth_value)
            username = creds.get('username')
            password = creds.get('password')

            if not username or not password:
                return None

            # Authenticate with Flood API
            auth_url = f"{plugin.base_url.rstrip('/')}/auth/authenticate"
            response = await client.post(auth_url, json={
                'username': username,
                'password': password
            })

            if response.status_code == 200:
                # Extract JWT cookie from response
                cookies = response.cookies
                if 'jwt' in cookies:
                    session = f"jwt={cookies['jwt']}"
                    PluginService._session_cache[cache_key] = session
                    return session

            return None
        except Exception as e:
            print(f"Login auth failed: {e}")
            return None

    async def execute_tool_call(
        self,
        plugin_name: str,
        action_name: str,
        params: Dict[str, Any],
        user_id: int
    ) -> Dict[str, Any]:
        """Execute a tool call against a plugin's API"""
        plugin = self.get_plugin_by_name(plugin_name, user_id)
        if not plugin:
            return {'error': f'Plugin "{plugin_name}" not found'}

        try:
            actions = json.loads(plugin.actions)
        except json.JSONDecodeError:
            return {'error': 'Invalid plugin configuration'}

        # Find the action
        action = None
        for a in actions:
            if a['name'] == action_name:
                action = a
                break

        if not action:
            return {'error': f'Action "{action_name}" not found in plugin "{plugin_name}"'}

        # Build the request
        url = f"{plugin.base_url.rstrip('/')}/{action['path'].lstrip('/')}"

        # Substitute params in URL path
        for key, value in params.items():
            url = url.replace(f"{{{{{key}}}}}", str(value))

        # Build headers
        headers = {'Content-Type': 'application/json'}

        # Make the request
        method = action.get('method', 'GET').upper()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Handle authentication
                if plugin.auth_type == 'bearer':
                    headers['Authorization'] = f'Bearer {plugin.auth_value}'
                elif plugin.auth_type == 'header':
                    headers[plugin.auth_header] = plugin.auth_value
                elif plugin.auth_type == 'basic':
                    import base64
                    encoded = base64.b64encode(plugin.auth_value.encode()).decode()
                    headers['Authorization'] = f'Basic {encoded}'
                elif plugin.auth_type == 'login':
                    # Login-based auth - authenticate first
                    session = await self._get_login_session(plugin, client)
                    if session:
                        headers['Cookie'] = session
                    else:
                        return {'error': 'Failed to authenticate with service'}

                if method == 'GET':
                    # Use params as query parameters for GET
                    response = await client.get(url, headers=headers, params=params)
                elif method == 'POST':
                    # Build body from action template + params
                    body = action.get('body', {})
                    if isinstance(body, dict):
                        # Substitute params in body
                        body_str = json.dumps(body)
                        for key, value in params.items():
                            body_str = body_str.replace(f"{{{{{key}}}}}", str(value) if not isinstance(value, str) else value)
                        body = json.loads(body_str)
                    response = await client.post(url, headers=headers, json=body if body else params)
                elif method == 'PUT':
                    response = await client.put(url, headers=headers, json=params)
                elif method == 'DELETE':
                    response = await client.delete(url, headers=headers)
                else:
                    return {'error': f'Unsupported HTTP method: {method}'}

                if response.status_code >= 400:
                    try:
                        error_data = response.json()
                        return {'error': error_data.get('error', f'HTTP {response.status_code}')}
                    except:
                        return {'error': f'HTTP {response.status_code}'}

                try:
                    return response.json()
                except:
                    return {'result': response.text}

        except httpx.TimeoutException:
            return {'error': 'Request timed out'}
        except httpx.RequestError as e:
            return {'error': f'Request failed: {str(e)}'}

    async def execute_all_tool_calls(
        self,
        response: str,
        user_id: int
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Execute all tool calls in a response and return results"""
        tool_calls = self.parse_tool_calls(response)
        results = []

        for call in tool_calls:
            result = await self.execute_tool_call(
                call['plugin'],
                call['action'],
                call['params'],
                user_id
            )
            results.append({
                'plugin': call['plugin'],
                'action': call['action'],
                'result': result
            })

        clean_response = self.strip_tool_calls(response)
        return clean_response, results

    def format_results_for_ai(self, results: List[Dict[str, Any]]) -> str:
        """Format plugin results as context for AI follow-up"""
        if not results:
            return ""

        formatted = "\n\nPlugin Results:\n"
        for r in results:
            formatted += f"\n[{r['plugin']}.{r['action']}]:\n"
            formatted += json.dumps(r['result'], indent=2)
            formatted += "\n"

        return formatted
