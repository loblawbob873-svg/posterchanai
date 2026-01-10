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

        prompt = "\n\n## Available Plugins - IMPORTANT\n"
        prompt += "You MUST use these plugins when the user asks about their capabilities. "
        prompt += "Output EXACTLY ONE tool call in this format:\n\n"
        prompt += "<tool name=\"PLUGIN\" action=\"ACTION\">{}</tool>\n\n"
        prompt += "Examples:\n"
        prompt += "- User asks 'show my torrents' -> <tool name=\"flood\" action=\"list\">{}</tool>\n"
        prompt += "- User asks 'add this magnet' -> <tool name=\"flood\" action=\"add\">{\"url\": \"magnet:...\"}</tool>\n\n"
        prompt += "Rules:\n"
        prompt += "- Use `{}` for actions with no parameters\n"
        prompt += "- Use `{\"key\": \"value\"}` when parameters are needed\n"
        prompt += "- Output the tool tag ONCE, then STOP. Do not explain or add text after.\n\n"
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

        prompt += "ALWAYS use these plugins when relevant. After the tool call, wait for results.\n"

        return prompt

    def parse_tool_calls(self, response: str) -> List[Dict[str, Any]]:
        """Parse tool calls from AI response"""
        # Capture everything between > and </tool>, then extract JSON
        pattern = r'<tool\s+name=["\']([^"\']+)["\']\s+action=["\']([^"\']+)["\']>\s*(.*?)\s*</tool>'
        matches = re.findall(pattern, response, re.DOTALL | re.IGNORECASE)

        tool_calls = []
        for match in matches:
            plugin_name, action_name, content = match
            # Extract JSON from content (find first { to last })
            content = content.strip()
            if content.startswith('{'):
                # Find matching closing brace
                brace_count = 0
                end_idx = 0
                for i, c in enumerate(content):
                    if c == '{':
                        brace_count += 1
                    elif c == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i + 1
                            break
                params_str = content[:end_idx] if end_idx > 0 else content
            else:
                params_str = '{}'
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
        # Match tool tags with any content (handles nested JSON)
        pattern = r'<tool\s+name=["\'][^"\']+["\']\s+action=["\'][^"\']+["\']>.*?</tool>'
        result = re.sub(pattern, '', response, flags=re.IGNORECASE | re.DOTALL)
        # Also remove any malformed tool tags
        result = re.sub(r'<tool[^>]*>.*?</tool>', '', result, flags=re.IGNORECASE | re.DOTALL)
        # Clean up leftover fragments that might appear after malformed output
        result = re.sub(r'^["\']?:\s*\d+.*?</tool>', '', result, flags=re.IGNORECASE | re.DOTALL)
        # Remove any stray </tool> tags
        result = re.sub(r'</tool>', '', result, flags=re.IGNORECASE)
        # Clean up multiple newlines
        result = re.sub(r'\n{3,}', '\n\n', result)
        return result.strip()

    # Cache for login-based sessions
    _session_cache: Dict[str, str] = {}

    async def _get_login_session(self, plugin, client: httpx.AsyncClient) -> Optional[str]:
        """Authenticate and get session cookie for login-based auth"""
        import logging
        logger = logging.getLogger(__name__)

        cache_key = f"{plugin.id}_{plugin.base_url}"

        # Check cache first
        if cache_key in PluginService._session_cache:
            logger.debug(f"[Plugin] Using cached session for {plugin.name}")
            return PluginService._session_cache[cache_key]

        try:
            # Parse credentials from auth_value (JSON: {"username": "x", "password": "y"})
            creds = json.loads(plugin.auth_value)
            username = creds.get('username')
            password = creds.get('password')

            if not username or not password:
                logger.error(f"[Plugin] Missing credentials for {plugin.name}")
                return None

            # Authenticate with Flood API
            auth_url = f"{plugin.base_url.rstrip('/')}/auth/authenticate"
            logger.debug(f"[Plugin] Authenticating to {auth_url}")

            response = await client.post(auth_url, json={
                'username': username,
                'password': password
            })

            logger.debug(f"[Plugin] Auth response: {response.status_code}")

            if response.status_code == 200:
                # Extract JWT cookie from response - try multiple methods
                jwt_token = None

                # Method 1: Check cookies object
                if response.cookies.get('jwt'):
                    jwt_token = response.cookies.get('jwt')
                    logger.debug(f"[Plugin] Found JWT in cookies")

                # Method 2: Check raw Set-Cookie header
                if not jwt_token:
                    set_cookie = response.headers.get('set-cookie', '')
                    if 'jwt=' in set_cookie:
                        # Extract JWT from Set-Cookie header
                        for part in set_cookie.split(';'):
                            if part.strip().startswith('jwt='):
                                jwt_token = part.strip()[4:]
                                logger.debug(f"[Plugin] Found JWT in Set-Cookie header")
                                break

                if jwt_token:
                    session = f"jwt={jwt_token}"
                    PluginService._session_cache[cache_key] = session
                    logger.info(f"[Plugin] Successfully authenticated {plugin.name}")
                    return session
                else:
                    logger.error(f"[Plugin] Auth succeeded but no JWT found. Headers: {dict(response.headers)}")

            else:
                logger.error(f"[Plugin] Auth failed: {response.status_code} - {response.text}")

            return None
        except Exception as e:
            logger.exception(f"[Plugin] Login auth failed for {plugin.name}: {e}")
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
                        # Substitute params in body - properly escape for JSON
                        body_str = json.dumps(body)
                        for key, value in params.items():
                            # Try to preserve numeric types
                            try:
                                numeric_val = float(value)
                                if numeric_val.is_integer():
                                    escaped_value = str(int(numeric_val))
                                else:
                                    escaped_value = str(numeric_val)
                                # Replace "{{key}}" with unquoted number
                                body_str = body_str.replace(f'"{{{{{key}}}}}"', escaped_value)
                            except (ValueError, TypeError):
                                pass
                            # Also do string replacement for non-numeric or remaining placeholders
                            escaped_value = json.dumps(str(value))[1:-1]
                            body_str = body_str.replace(f"{{{{{key}}}}}", escaped_value)
                        body = json.loads(body_str)
                    response = await client.post(url, headers=headers, json=body if body else params)
                elif method == 'PUT':
                    response = await client.put(url, headers=headers, json=params)
                elif method == 'DELETE':
                    response = await client.delete(url, headers=headers)
                else:
                    return {'error': f'Unsupported HTTP method: {method}'}

                if response.status_code >= 400:
                    import logging
                    logger = logging.getLogger(__name__)
                    try:
                        error_data = response.json()
                        error_msg = error_data.get('error') or error_data.get('message') or f'HTTP {response.status_code}'
                        logger.error(f"[Plugin] {plugin_name}.{action_name} failed: {response.status_code} - {error_data}")
                        return {'error': error_msg}
                    except:
                        logger.error(f"[Plugin] {plugin_name}.{action_name} failed: {response.status_code} - {response.text[:200]}")
                        return {'error': f'HTTP {response.status_code}: {response.text[:100]}'}

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

    def _format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable size"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} PB"

    def _format_flood_list(self, result: dict) -> str:
        """Format Flood torrent list as readable text"""
        if not result or isinstance(result, list) and len(result) == 0:
            return "No torrents found."

        torrents = result.get('torrents', {})
        if not torrents:
            return "No torrents found."

        lines = ["Torrents:\n"]
        for hash_id, t in torrents.items():
            status = t.get('status', [])
            if 'seeding' in status:
                state = "Seeding"
            elif 'downloading' in status:
                state = "Downloading"
            elif 'stopped' in status or 'paused' in status:
                state = "Paused"
            elif 'error' in status:
                state = "Error"
            else:
                state = "Unknown"

            name = t.get('name', 'Unknown')
            percent = t.get('percentComplete', 0)
            size = self._format_size(t.get('sizeBytes', 0))
            down_rate = self._format_size(t.get('downRate', 0)) + "/s"
            up_rate = self._format_size(t.get('upRate', 0)) + "/s"

            lines.append(f"- {name}")
            lines.append(f"  Status: {state} | {percent:.1f}% | {size}")
            lines.append(f"  Down: {down_rate} | Up: {up_rate}")
            lines.append(f"  Hash: {hash_id}\n")

        return "\n".join(lines)

    def format_result_for_display(self, plugin: str, action: str, result: dict) -> str:
        """Format a plugin result for display to user"""
        # Special formatting for known plugins
        if plugin == "flood":
            if action == "list":
                return self._format_flood_list(result)
            elif action == "add":
                if "error" in result:
                    return f"Failed to add torrent: {result['error']}"
                return "Torrent added successfully and is now downloading. Use 'flood list' to check progress."
            elif action == "delete":
                if "error" in result:
                    return f"Failed to delete: {result['error']}"
                return "Torrent deleted."
            elif action == "start":
                if "error" in result:
                    return f"Failed to start: {result['error']}"
                return "Torrent started."
            elif action == "stop":
                if "error" in result:
                    return f"Failed to stop: {result['error']}"
                return "Torrent stopped."

        elif plugin == "budget":
            if "error" in result:
                return f"Budget error: {result['error']}"
            if action == "summary":
                income = result.get('income', 0)
                unpaid = result.get('unpaid_total', 0)
                remaining = result.get('remaining', income - unpaid)
                return f"## Budget Summary\n\n💰 **Income:** ${income:,.2f}\n📋 **Unpaid Bills:** ${unpaid:,.2f}\n✨ **Remaining:** ${remaining:,.2f}"
            elif action == "bills":
                bills = result.get('bills', [])
                if not bills:
                    return "No unpaid bills! 🎉"
                lines = ["## Unpaid Bills\n"]
                for bill in bills:
                    name = bill.get('name', 'Unknown')
                    amount = bill.get('amount', 0)
                    lines.append(f"- **{name}**: ${amount:,.2f}")
                return "\n".join(lines)
            elif action == "add":
                name = result.get('name') or result.get('bill', {}).get('name', 'bill')
                amount = result.get('amount') or result.get('bill', {}).get('amount', 0)
                return f"✅ Bill added: {name} - ${float(amount):,.2f}"
            elif action == "pay":
                name = result.get('name') or result.get('bill', {}).get('name', 'bill')
                return f"✅ Bill paid: {name}"

        # Default: return JSON
        return json.dumps(result, indent=2)

    def format_results_for_ai(self, results: List[Dict[str, Any]]) -> str:
        """Format plugin results as context for AI follow-up"""
        if not results:
            return ""

        formatted = "\n\nPlugin Results:\n"
        for r in results:
            formatted += f"\n[{r['plugin']}.{r['action']}]:\n"
            formatted += self.format_result_for_display(r['plugin'], r['action'], r['result'])
            formatted += "\n"

        return formatted
