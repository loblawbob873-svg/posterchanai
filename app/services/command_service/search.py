"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import datetime, fetch_news_from_source, get_user_news_sources, logger, proxy_image_register, re
from app.utils import lb_auth


class _SearchMixin:
    async def _search_command(self, query: str) -> dict:
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `search latest AI news`"}

        clean_query, categories, time_range = self.search_service.detect_search_intent(query)
        is_news = (categories == "news")
        if is_news:
            # News: the dedicated `news` engines give the most relevant headlines, but the category
            # only returns ~12 here — so pull 15 from news AND top up from the general pool
            # (google/brave/ddg, which carry the freshest items + some dates), dedupe by URL, and
            # sort newest-first. Best of both: relevant + fresh + deeper.
            if not time_range:
                time_range = "week"
            news_res = await self.search_service.web_search(
                clean_query, limit=15, categories="news", time_range=time_range, sort_recent=True)
            gen_res = await self.search_service.web_search(
                clean_query, limit=15, categories=None, time_range=time_range, sort_recent=True)
            # News FIRST (more relevant for news), then top up with general to reach 15. Do NOT
            # re-sort the merged list — that would float general's dated items above the news ones.
            seen, merged = set(), []
            for r in news_res + gen_res:
                u = (r.get("url") or "").strip()
                if u and u not in seen:
                    seen.add(u)
                    merged.append(r)
            results = merged[:15]
        else:
            results = await self.search_service.web_search(
                clean_query, limit=5, categories=categories, time_range=time_range)
            # Fall back to a plain general search if a category/time search came up empty.
            if not results and (categories or time_range):
                results = await self.search_service.web_search(clean_query, limit=5)
        if not results:
            return {"type": "text", "content": f"No results found for: {query}"}

        scope = f" ({categories})" if categories else ""
        is_news = categories == "news"
        # Format results for AI summarization (include the publish date so the model can lead with
        # the most recent for news — results are already sorted newest-first by the search service).
        context = f"Search results for '{clean_query}'{scope}:\n\n"
        for i, r in enumerate(results, 1):
            _pub = f" (published {r['published']})" if r.get("published") else ""
            context += f"{i}. **{r['title']}**{_pub}\n{r['url']}\n{r['content']}\n\n"

        _sys = "You are a helpful assistant. Summarize the search results concisely and highlight key information."
        if is_news:
            _sys = ("You are a news assistant. The results are sorted newest-first. Lead with the "
                    "LATEST developments, mention each item's date when available, and don't invent "
                    "facts that aren't in the results.")
        # Get AI summary
        messages = [
            {"role": "system", "content": _sys},
            {"role": "user", "content": context},
        ]
        summary = await self.chat_service.chat(messages)

        return {"type": "search", "content": summary, "results": results}

    async def _images_command(self, query: str) -> dict:
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `images cute cats`"}

        results = await self.search_service.image_search(query, limit=10)
        if not results:
            return {"type": "text", "content": f"No images found for: {query}"}
        results = results[:10]
        # For Android: limit to 5 items and send both thumb_id and img_src so payload fits and direct fallback works.
        # (10 items + img_src truncates; 10 items without img_src = proxy fails = 0 images. 5 + img_src = 5 images.)
        images_payload = []
        for r in results[:5]:
            thumb_url = (r.get("img_src") or "").strip()
            if not thumb_url:
                continue
            page_url = (r.get("url") or thumb_url).strip()
            title = (r.get("title") or "Image")[:200]
            try:
                thumb_id = proxy_image_register(thumb_url, self.db)
                images_payload.append({"title": title, "url": page_url, "thumb_id": thumb_id, "img_src": thumb_url})
            except Exception:
                images_payload.append({"title": title, "url": page_url, "img_src": thumb_url})
        return {"type": "images", "content": f"Found {len(images_payload)} images for: {query}", "images": images_payload}

    async def _files_command(self, query: str) -> dict:
        """Search for files in user's storage."""
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `files image` or `files document.pdf`"}
        
        return await self._search_files_internal(query)

    async def _search_files_internal(self, query: str) -> dict:
        """Internal file search function - handles storage proxy correctly."""
        from pathlib import Path
        from app.services.storage_service import get_storage_service
        from app.services import settings_store
        import httpx

        # Check if using remote storage
        storage_value = settings_store.get("storage_server_url")
        if storage_value and storage_value.startswith(('http://', 'https://')):
            # Use remote storage API with async httpx (same as files router)
            url = storage_value.strip()
            try:
                headers = lb_auth.headers()
                
                # Try both endpoints (same as files router)
                search_urls = [
                    f"{url.rstrip('/')}/api/files/search",
                    f"{url.rstrip('/')}/api/storage/search"
                ]
                
                response = None
                async with httpx.AsyncClient(timeout=60.0) as client:
                    for search_url in search_urls:
                        try:
                            response = await client.get(
                                search_url,
                                params={"query": query, "username": self.user.username},
                                headers=headers
                            )
                            if response.status_code == 200:
                                break
                        except Exception as e:
                            logger.debug(f"Tried {search_url}, got error: {e}")
                            continue
                    
                    if response and response.status_code == 200:
                        data = response.json()
                        results = data.get('results', [])
                        return {
                            "type": "files",
                            "content": f"Found {len(results)} file(s) matching '{query}'",
                            "files": results[:50],  # Limit to 50 results
                            "query": query
                        }
                    else:
                        logger.warning(f"Storage server search failed, falling back to local search")
            except Exception as e:
                logger.warning(f"Error searching remote files: {e}, falling back to local search")

        # Local storage search (or fallback if remote search failed)
        storage = get_storage_service(self.db)
        user_path = storage.get_user_path(self.user.username)

        results = []
        query_lower = query.lower()

        try:
            # Recursively search through user's files
            for item in user_path.rglob('*'):
                try:
                    if item.is_dir():
                        continue

                    filename = item.name.lower()
                    relative_path = str(item.relative_to(user_path)).lower()

                    if query_lower in filename or query_lower in relative_path:
                        stat = item.stat()
                        results.append({
                            "name": item.name,
                            "path": str(item.relative_to(user_path)),
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        })
                except Exception as e:
                    logger.warning(f"Error processing file {item}: {e}")
                    continue

            # Sort by modified time (newest first)
            results.sort(key=lambda x: x.get('modified', 0), reverse=True)

            return {
                "type": "files",
                "content": f"Found {len(results)} file(s) matching '{query}'",
                "files": results[:50],  # Limit to 50 results
                "query": query
            }
        except Exception as e:
            logger.error(f"Error searching files locally: {e}", exc_info=True)
            return {"type": "text", "content": f"Error searching files: {str(e)}"}

    async def _news_command(self, arg: str) -> dict:
        """Get news from configured web sources"""
        return await self._dailynews_command(arg)

    def _add_copy_buttons_to_news(self, markdown: str) -> str:
        """Add copy buttons to news article links in markdown."""
        import re

        # Match markdown links in bullet points: - [title](url)
        # Add [Copy](cmd:tui-copy url) after each link
        def add_copy_button(match):
            title = match.group(1)
            url = match.group(2)
            # Return the link with a copy button
            return f"- [{title}]({url}) [Copy](cmd:tui-copy {url})"

        # Pattern: - [title](url)
        pattern = r"- \[([^\]]+)\]\(([^)]+)\)"
        result = re.sub(pattern, add_copy_button, markdown)

        return result

    async def _dailynews_command(self, arg: str) -> dict:
        """Get news from configured web sources (CNN, NPR, etc.)"""
        from datetime import datetime

        if not self.user:
            return {"type": "text", "content": "Please log in to use Daily News."}

        try:
            # Get news sources (user's custom sources or admin defaults)
            all_sources = get_user_news_sources(self.user, self.db)

            if not all_sources:
                # Now that sources are per-user with no global fallback, this is the NORMAL state for a
                # new account — so it has to read as a next step, not an error.
                return {"type": "text", "content":
                        "📰 You haven't added any news sources yet.\n\n"
                        "Add RSS or site URLs in Settings → News sources (one per line, `url|name`), "
                        "or tap ＋ in the News tab."}

            # If arg provided, filter to matching source
            if arg.strip():
                arg_lower = arg.strip().lower()
                sources = [s for s in all_sources if arg_lower in s["url"].lower() or arg_lower in s["name"].lower()]
                if not sources:
                    source_names = ", ".join(s["name"] for s in all_sources)
                    return {"type": "text", "content": f"No news source matching '{arg.strip()}'. Available sources: {source_names}"}
            else:
                sources = all_sources

            # Fetch news from sources concurrently with timeout
            import asyncio

            async def fetch_single_source(source):
                try:
                    # Add timeout per source to prevent hanging
                    async with asyncio.timeout(45):  # 45 second timeout per source (fetch + AI summary)
                        markdown = await fetch_news_from_source(source["url"], source["name"], self.db)
                        return markdown
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout fetching news from {source['name']}")
                    return f"**{source['name']}:** ⚠️ Timeout fetching headlines (took too long)"
                except Exception as e:
                    logger.error(f"Error fetching news from {source['name']}: {e}")
                    return f"**{source['name']}:** ❌ Error fetching headlines: {str(e)[:100]}"

            results = await asyncio.gather(*[fetch_single_source(s) for s in sources], return_exceptions=True)
            # Filter out any exception results
            results = [r if not isinstance(r, Exception) else f"Error: {str(r)}" for r in results]

            # Format response
            today = datetime.now().strftime("%B %d, %Y %H:%M")
            if len(sources) == 1:
                content = f"## {sources[0]['name']} - {today}\n\n" + results[0] if results else "No headlines found."
            else:
                content = f"## Daily News - {today}\n\n" + "\n\n---\n\n".join(results)

            return {"type": "text", "content": content}

        except Exception as e:
            logger.error(f"Daily news command error: {e}")
            return {"type": "text", "content": f"Error fetching daily news: {str(e)}"}
