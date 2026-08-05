"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import _format_bt_list_from_dicts, _nyaa_cache, _torrent_cache, format_all_categories, format_nyaa_results, format_torrent_results, logger, re, scrape_all_categories, scrape_torrents, search_nyaa, search_torrents
from app.utils import lb_auth


class _TorrentsMixin:
    def _get_remote_bt_url(self):
        """Get remote torrent server URL if configured."""
        from app.services import settings_store

        return settings_store.get("bt_server_url") or None

    async def _remote_bt_request(self, endpoint: str, method: str = "GET", json_body: dict = None):
        """Make request to remote torrent server."""
        import httpx

        server_url = self._get_remote_bt_url()
        if not server_url:
            return None

        # Server-to-server requests don't need authentication
        url = f"{server_url.rstrip('/')}/api/torrent{endpoint}"
        headers = lb_auth.headers()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"[TORRENT] TUI request to {url} (load-balanced)")
                if method == "GET":
                    response = await client.get(url, headers=headers)
                else:
                    response = await client.post(url, headers=headers, json=json_body)

                logger.info(f"[TORRENT] Remote response: {response.status_code}")

                if response.status_code == 200:
                    try:
                        return response.json()
                    except Exception as e:
                        logger.error(f"[TORRENT] Failed to parse JSON: {e}, body: {response.text[:500]}")
                        return {"error": "Remote server returned invalid response"}
                else:
                    # Try to get error detail from JSON, fall back to text
                    try:
                        error = response.json().get("detail", "Remote server error")
                    except Exception:
                        error = response.text[:200] if response.text else f"HTTP {response.status_code}"
                    logger.error(f"[TORRENT] Remote error: {response.status_code} - {error}")
                    return {"error": error}
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to remote torrent server: {e}")
            return {"error": f"Cannot reach remote torrent server: {e}"}

    def _get_bt_service(self):
        """Get built-in torrent service if enabled, or None. Returns (service, error_msg)."""
        from app.services import settings_store

        # Check for remote server first
        if self._get_remote_bt_url():
            return "remote", None  # Special marker for remote server

        if not settings_store.get_bool("bt_enabled"):
            return None, "Built-in torrent client is disabled. Enable it in Admin Settings."

        def get_setting(key: str, default: str = "") -> str:
            return settings_store.get(key) or default

        proxy_host = get_setting("bt_proxy_host")
        if not proxy_host:
            return None, "HTTP Proxy Host not configured. Set it in Admin Settings (required for torrenting)."

        try:
            from app.services.libtorrent_service import LibtorrentService

            service = LibtorrentService.get_instance(
                download_dir=get_setting("bt_download_dir", "/var/lib/posterchanai/torrents"),
                proxy_host=proxy_host,
                proxy_port=int(get_setting("bt_proxy_port", "8118")),
                listen_port=int(get_setting("bt_listen_port", "6881")),
            )
            return service, None
        except ImportError as e:
            return None, f"libtorrent not installed: {e}. Run: pip install libtorrent"
        except Exception as e:
            return None, f"Failed to start torrent service: {e}"

    async def _torrents_command(self, arg: str) -> dict:
        """Browse torrents and manage downloads."""
        global _torrent_cache

        # Import formatting functions - use local fallback if libtorrent not installed
        try:
            from app.services.libtorrent_service import format_torrent_list, format_torrent_list_from_dicts
        except Exception as e:
            logger.warning(f"Could not import libtorrent formatting: {e}")
            format_torrent_list = lambda torrents: _format_bt_list_from_dicts(
                [
                    {
                        "name": t.name,
                        "size": t.size,
                        "progress": t.progress,
                        "download_rate": t.download_rate,
                        "upload_rate": t.upload_rate,
                        "state": t.state,
                        "seeders": t.seeders,
                        "peers": t.peers,
                        "is_paused": getattr(t, "is_paused", False),
                    }
                    for t in torrents
                ]
            )
            format_torrent_list_from_dicts = _format_bt_list_from_dicts

        parts = arg.strip().split()
        subcommand = parts[0].lower() if parts else ""
        categories = ("movies", "tv", "music", "anime", "search")

        # Get built-in service (None if disabled or not configured)
        bt_service, bt_error = self._get_bt_service()

        # Client management subcommands - require built-in client or remote server
        if subcommand in ("list", "ls"):
            if not bt_service:
                return {"type": "text", "content": bt_error}
            if bt_service == "remote":
                result = await self._remote_bt_request("/list")
                if result and "error" in result:
                    return {"type": "text", "content": result["error"]}
                if result and "torrents" in result:
                    return {"type": "text", "content": _format_bt_list_from_dicts(result["torrents"])}
                return {"type": "text", "content": "No response from remote server"}
            torrents = bt_service.list_torrents()
            return {"type": "text", "content": format_torrent_list(torrents)}

        elif subcommand == "add" and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            # Capture the WHOLE remainder, not just parts[1] — a magnet URI can contain spaces
            # (unencoded &dn=/&tr= values), and splitting on whitespace truncated it past the
            # info-hash ("missing info-hash from URI"). Strip surrounding angle brackets too.
            target = arg.strip().split(None, 1)[1].strip().strip("<>").strip()

            # A `.torrent` URL: add it (magnets fall through to the parse_magnet path below).
            if not target.startswith("magnet:") and re.match(r'^https?://', target, re.IGNORECASE):
                if bt_service == "remote":
                    # The remote server owns the torrent client — let IT download + add the URL.
                    result = await self._remote_bt_request("/add", method="POST", json_body={"torrent_url": target})
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    if result and "info_hash" in result:
                        return {"type": "text", "content": f"Added torrent: `{result['info_hash']}`\n\nUse `torrents list` to check progress."}
                    return {"type": "text", "content": "Failed to add .torrent to remote server"}
                # Local client: download the .torrent here and add it.
                import httpx
                try:
                    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as _c:
                        _resp = await _c.get(target, headers={"User-Agent": "Mozilla/5.0"})
                        _resp.raise_for_status()
                        _data = _resp.content
                except Exception as e:
                    return {"type": "text", "content": f"Couldn't download that .torrent: {e}"}
                try:
                    info_hash = bt_service.add_torrent_file(_data, user_id=self.user.id if self.user else None)
                except Exception as e:
                    return {"type": "text", "content": f"Couldn't add that .torrent: {e}"}
                return {"type": "text", "content": f"Added torrent: `{info_hash}`\n\nUse `torrents list` to check progress."}

            magnet = target
            if not magnet.startswith("magnet:"):
                return {"type": "text", "content": "Please provide a magnet link (`magnet:…`) or a `.torrent` URL."}
            if bt_service == "remote":
                result = await self._remote_bt_request("/add", method="POST", json_body={"magnet": magnet})
                if result and "error" in result:
                    return {"type": "text", "content": result["error"]}
                if result and "info_hash" in result:
                    return {
                        "type": "text",
                        "content": f"Added torrent: `{result['info_hash']}`\n\nUse `torrents list` to check progress.",
                    }
                return {"type": "text", "content": "Failed to add torrent to remote server"}
            try:
                info_hash = bt_service.add_magnet(magnet, user_id=self.user.id if self.user else None)
            except Exception as e:
                logger.warning(f"[torrents] add_magnet failed: {e}")
                return {"type": "text", "content": (
                    "Couldn't add that magnet — it looks malformed (no info-hash). "
                    "Paste the full `magnet:?xt=urn:btih:…` link.")}
            return {
                "type": "text",
                "content": f"Added torrent: `{info_hash}`\n\nUse `torrents list` to check progress.",
            }

        elif subcommand in ("start", "resume") and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request("/resume", method="POST", json_body={"num": num})
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"▶️ Started torrent #{num}\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"▶️ Started torrent #{num}"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.resume(info_hash):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"▶️ Started torrent #{num}\n\n" + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents resume <number>`"}

        elif subcommand in ("stop", "pause") and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request("/pause", method="POST", json_body={"num": num})
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"⏸️ Paused torrent #{num}\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"⏸️ Paused torrent #{num}"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.pause(info_hash):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"⏸️ Paused torrent #{num}\n\n" + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents pause <number>`"}

        elif subcommand in ("del", "delete", "rm") and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request(
                        "/remove", method="POST", json_body={"num": num, "delete_files": False}
                    )
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"🗑️ Removed torrent #{num} (files kept)\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"🗑️ Removed torrent #{num} (files kept)"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.remove(info_hash, delete_files=False):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"🗑️ Removed torrent #{num} (files kept)\n\n"
                        + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents rm <number>`"}

        elif subcommand == "purge" and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request(
                        "/remove", method="POST", json_body={"num": num, "delete_files": True}
                    )
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"🗑️ Purged torrent #{num} (files deleted)\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"🗑️ Purged torrent #{num} (files deleted)"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.remove(info_hash, delete_files=True):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"🗑️ Purged torrent #{num} (files deleted)\n\n"
                        + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents purge <number>`"}

        elif subcommand == "info" and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request(f"/info/{num}")
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    if not result or "info_hash" not in result:
                        return {"type": "text", "content": f"Torrent #{num} not found"}
                    # Format remote response
                    files = result.get("files", [])
                    file_list = "\n".join([f"  - {f['path']} ({f['size'] / 1024 / 1024:.1f} MB)" for f in files[:10]])
                    if len(files) > 10:
                        file_list += f"\n  ... and {len(files) - 10} more files"
                    info = f"""## {result["name"]}

**Hash:** `{result["info_hash"]}`
**Status:** {result["state"]} {"(paused)" if result.get("is_paused") else ""}
**Progress:** {result["progress"]:.1f}%
**Size:** {result["size"] / 1024 / 1024:.1f} MB
**Downloaded:** {result["downloaded"] / 1024 / 1024:.1f} MB
**Uploaded:** {result["uploaded"] / 1024 / 1024:.1f} MB
**Speed:** ↓{result["download_rate"] / 1024:.1f} KB/s ↑{result["upload_rate"] / 1024:.1f} KB/s
**Peers:** {result["seeders"]} seeders, {result["peers"]} peers
**Save Path:** {result["save_path"]}

**Files:**
{file_list}
"""
                    return {"type": "text", "content": info}
                info_hash = bt_service.get_hash_by_number(num)
                if not info_hash:
                    return {"type": "text", "content": f"Torrent #{num} not found"}

                t = bt_service.get_torrent(info_hash)
                if not t:
                    return {"type": "text", "content": f"Torrent #{num} not found"}

                files = bt_service.get_files(info_hash)
                file_list = "\n".join([f"  - {f['path']} ({f['size'] / 1024 / 1024:.1f} MB)" for f in files[:10]])
                if len(files) > 10:
                    file_list += f"\n  ... and {len(files) - 10} more files"

                info = f"""## {t.name}

**Hash:** `{t.info_hash}`
**Status:** {t.state} {"(paused)" if t.is_paused else ""}
**Progress:** {t.progress:.1f}%
**Size:** {t.size / 1024 / 1024:.1f} MB
**Downloaded:** {t.downloaded / 1024 / 1024:.1f} MB
**Uploaded:** {t.uploaded / 1024 / 1024:.1f} MB
**Speed:** ↓{t.download_rate / 1024:.1f} KB/s ↑{t.upload_rate / 1024:.1f} KB/s
**Peers:** {t.seeders} seeders, {t.peers} peers
**Save Path:** {t.save_path}

**Files:**
{file_list}
"""
                return {"type": "text", "content": info}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents info <number>`"}

        # Handle download subcommand: torrents download <category> <number>
        if subcommand in ("download", "dl", "get"):
            if len(parts) < 3:
                return {
                    "type": "text",
                    "content": "Usage: `torrents download <category> <number>`\n\nExample: `torrents download anime 5`",
                }

            category = parts[1].lower()
            if category not in categories:
                return {
                    "type": "text",
                    "content": f"Unknown category: `{category}`\n\nAvailable: movies, tv, music, anime, search",
                }

            try:
                num = int(parts[2])
            except ValueError:
                return {
                    "type": "text",
                    "content": "Please provide a valid number. Example: `torrents download anime 5`",
                }

            # Get cached results for this category
            user_id = self.user.id if self.user else 0
            user_cache = _torrent_cache.get(user_id, {})
            cached = user_cache.get(category, [])

            if not cached:
                return {
                    "type": "text",
                    "content": f"No {category} results cached. Run `torrents` first to load results.",
                }

            if num < 1 or num > len(cached):
                return {"type": "text", "content": f"Invalid number. Choose between 1 and {len(cached)}."}

            torrent = cached[num - 1]
            magnet = torrent.magnet

            if not self.user:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\nLogin required to download.",
                }

            if not bt_service:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\n{bt_error}",
                }

            if bt_service == "remote":
                result = await self._remote_bt_request("/add", method="POST", json_body={"magnet": magnet})
                if result and "error" in result:
                    return {"type": "text", "content": f"**Selected:** {torrent.title}\n\n{result['error']}"}
                if result and "info_hash" in result:
                    return {
                        "type": "text",
                        "content": f"**Downloading:** {torrent.title}\n\nAdded: `{result['info_hash']}`\n\nUse `torrents list` to check progress.",
                    }
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\nFailed to add torrent to remote server",
                }

            info_hash = bt_service.add_magnet(magnet, user_id=self.user.id if self.user else None)
            return {
                "type": "text",
                "content": f"**Downloading:** {torrent.title}\n\nAdded: `{info_hash}`\n\nUse `torrents list` to check progress.",
            }

        # Handle search subcommand
        if subcommand in ("search", "s") and len(parts) > 1:
            query = " ".join(parts[1:])
            try:
                import asyncio

                # Add timeout to prevent hanging
                results = await asyncio.wait_for(search_torrents(self.db, query, limit=15), timeout=20)

                if not results:
                    return {"type": "text", "content": f"No results found for '{query}' on torrent site"}

                # Cache results for download command
                user_id = self.user.id if self.user else 0
                _torrent_cache[user_id] = {"search": results}

                formatted = format_torrent_results(results, category="search", title=f"SEARCH: {query.upper()}")
                return {"type": "text", "content": formatted}
            except asyncio.TimeoutError:
                logger.error(f"Torrent search timed out for query: {query}")
                return {"type": "text", "content": f"Search timed out. The torrent site may be slow or unavailable."}
            except ValueError as e:
                msg = str(e)
                suffix = "\n\nConfigure proxy in Admin → Network → HTTP Proxy (outbound)" if "requires http proxy" in msg.lower() else ""
                return {"type": "text", "content": f"{msg}{suffix}"}
            except Exception as e:
                logger.error(f"Torrent search error: {e}")
                return {"type": "text", "content": f"Error searching torrents: {str(e)}"}

        # No subcommand - show all categories overview
        if not subcommand:
            try:
                all_results = await scrape_all_categories(self.db, limit_per_category=10)

                # Cache all results by category
                user_id = self.user.id if self.user else 0
                _torrent_cache[user_id] = all_results

                formatted = format_all_categories(all_results)
                return {"type": "text", "content": formatted}
            except ValueError as e:
                msg = str(e)
                suffix = "\n\nConfigure proxy in Admin → Network → HTTP Proxy (outbound)" if "requires http proxy" in msg.lower() else ""
                return {"type": "text", "content": f"{msg}{suffix}"}
            except Exception as e:
                logger.error(f"Torrents command error: {e}")
                return {"type": "text", "content": f"Error fetching torrents: {str(e)}"}

        # Handle category browsing
        category = subcommand
        if category not in categories:
            return {
                "type": "text",
                "content": f"Unknown category: `{subcommand}`\n\nAvailable: `torrents movies`, `torrents tv`, `torrents music`, `torrents anime`",
            }

        try:
            results = await scrape_torrents(self.db, category, limit=10)

            if not results:
                return {
                    "type": "text",
                    "content": f"No {category} torrents found. The site may be unavailable or not configured.\n\nAdmin can set `torrent_site_url` in settings.",
                }

            # Cache results for download command
            user_id = self.user.id if self.user else 0
            if user_id not in _torrent_cache:
                _torrent_cache[user_id] = {}
            _torrent_cache[user_id][category] = results

            formatted = format_torrent_results(results, category)
            return {"type": "text", "content": formatted}

        except ValueError as e:
            msg = str(e)
            suffix = "\n\nConfigure proxy in Admin → Network → HTTP Proxy (outbound)" if "requires http proxy" in msg.lower() else ""
            return {"type": "text", "content": f"{msg}{suffix}"}
        except Exception as e:
            logger.error(f"Torrents command error: {e}")
            return {"type": "text", "content": f"Error fetching torrents: {str(e)}"}

    async def _nyaa_command(self, arg: str) -> dict:
        """Search nyaa.si for anime torrents"""
        global _nyaa_cache

        parts = arg.strip().split()
        if not parts:
            return {"type": "text", "content": "Usage: `nyaa <search query>`\n\nExample: `nyaa one piece 1080p`"}

        subcommand = parts[0].lower()

        # Handle download subcommand
        if subcommand in ("download", "dl", "get"):
            if len(parts) < 2:
                return {"type": "text", "content": "Usage: `nyaa download <number>`\nFirst search with `nyaa <query>`."}

            try:
                num = int(parts[1])
            except ValueError:
                return {"type": "text", "content": "Please provide a valid number. Example: `nyaa download 3`"}

            # Get cached results
            user_id = self.user.id if self.user else 0
            cached = _nyaa_cache.get(user_id, [])

            if not cached:
                return {"type": "text", "content": "No nyaa results cached. Search first with `nyaa <query>`."}

            if num < 1 or num > len(cached):
                return {"type": "text", "content": f"Invalid number. Choose between 1 and {len(cached)}."}

            torrent = cached[num - 1]
            magnet = torrent.magnet

            if not self.user:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\nLogin required to download.",
                }

            # Use built-in torrent client
            bt_service, bt_error = self._get_bt_service()
            if not bt_service:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\n{bt_error}",
                }

            if bt_service == "remote":
                result = await self._remote_bt_request("/add", method="POST", json_body={"magnet": magnet})
                if result and "error" in result:
                    return {"type": "text", "content": f"**Selected:** {torrent.title}\n\n{result['error']}"}
                if result and "info_hash" in result:
                    return {
                        "type": "text",
                        "content": f"**Downloading:** {torrent.title}\n\nAdded: `{result['info_hash']}`\n\nUse `torrents list` to check progress.",
                    }
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\nFailed to add torrent to remote server",
                }

            info_hash = bt_service.add_magnet(magnet, user_id=self.user.id if self.user else None)
            return {
                "type": "text",
                "content": f"**Downloading:** {torrent.title}\n\nAdded: `{info_hash}`\n\nUse `torrents list` to check progress.",
            }

        # Search query
        query = arg.strip()

        try:
            results = await search_nyaa(query, limit=20)

            if not results:
                return {"type": "text", "content": f"No results found for '{query}' on nyaa.si"}

            # Cache results for download command
            user_id = self.user.id if self.user else 0
            _nyaa_cache[user_id] = results

            formatted = format_nyaa_results(results, query)

            return {"type": "text", "content": formatted}

        except ValueError as e:
            msg = str(e)
            suffix = "\n\nConfigure proxy in Admin → Network → HTTP Proxy (outbound)" if "requires http proxy" in msg.lower() else ""
            return {"type": "text", "content": f"{msg}{suffix}"}
        except Exception as e:
            logger.error(f"Nyaa command error: {e}")
            return {"type": "text", "content": f"Error searching nyaa.si: {str(e)}"}
