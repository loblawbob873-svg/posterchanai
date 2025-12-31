import httpx
from typing import Optional
from sqlalchemy.orm import Session
from app.models import Setting


class SearchService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.searxng_url = settings.get("searxng_url", "https://search.poster.place")

    async def web_search(self, query: str, limit: int = 5) -> list[dict]:
        """Search the web using SearXNG"""
        if not self.searxng_url:
            return []

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    f"{self.searxng_url}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "language": "en"
                    }
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])[:limit]
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:300] if r.get("content") else ""
                    }
                    for r in results
                ]
            except Exception as e:
                print(f"Search error: {e}")
                return []

    async def image_search(self, query: str, limit: int = 10) -> list[dict]:
        """Search for images using SearXNG"""
        if not self.searxng_url:
            return []

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    f"{self.searxng_url}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "categories": "images"
                    }
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])[:limit]
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "img_src": r.get("img_src", r.get("thumbnail", ""))
                    }
                    for r in results
                    if r.get("img_src") or r.get("thumbnail")
                ]
            except Exception as e:
                print(f"Image search error: {e}")
                return []


def get_search_service(db: Session) -> SearchService:
    return SearchService(db)
