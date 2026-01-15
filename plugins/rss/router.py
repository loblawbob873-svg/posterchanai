"""
RSS Plugin API Router

API endpoints for managing RSS feed subscriptions.
"""
import xml.etree.ElementTree as ET
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user
from plugins.rss.models import RssFeed, RssEntry
from plugins.rss.service import RssService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/rss", tags=["rss"])


class AddFeedRequest(BaseModel):
    url: str
    custom_name: Optional[str] = None


class FeedResponse(BaseModel):
    id: int
    url: str
    title: Optional[str]
    custom_name: Optional[str]
    display_name: str
    enabled: bool
    last_error: Optional[str]

    class Config:
        from_attributes = True


class EntryResponse(BaseModel):
    id: int
    title: str
    url: Optional[str]
    summary: Optional[str]
    feed_name: str
    published_at: Optional[str]
    is_read: bool
    is_posted: bool = False


@router.get("/feeds")
async def get_feeds(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> List[FeedResponse]:
    """Get all RSS feeds for the current user"""
    rss_service = RssService(db)
    feeds = rss_service.get_user_feeds(current_user.id)
    return [
        FeedResponse(
            id=f.id,
            url=f.url,
            title=f.title,
            custom_name=f.custom_name,
            display_name=f.display_name,
            enabled=f.enabled,
            last_error=f.last_error
        )
        for f in feeds
    ]


@router.post("/feeds")
async def add_feed(
    request: AddFeedRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> FeedResponse:
    """Add a new RSS feed subscription"""
    rss_service = RssService(db)
    feed = await rss_service.add_feed(
        current_user.id,
        request.url,
        request.custom_name
    )
    return FeedResponse(
        id=feed.id,
        url=feed.url,
        title=feed.title,
        custom_name=feed.custom_name,
        display_name=feed.display_name,
        enabled=feed.enabled,
        last_error=feed.last_error
    )


@router.delete("/feeds/{feed_id}")
async def remove_feed(
    feed_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove an RSS feed subscription"""
    rss_service = RssService(db)
    if not rss_service.remove_feed(current_user.id, feed_id):
        raise HTTPException(status_code=404, detail="Feed not found")
    return {"status": "ok"}


@router.post("/feeds/{feed_id}/toggle")
async def toggle_feed(
    feed_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Toggle a feed's enabled status"""
    feed = db.query(RssFeed).filter(
        RssFeed.id == feed_id,
        RssFeed.user_id == current_user.id
    ).first()
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")

    feed.enabled = not feed.enabled
    db.commit()
    return {"enabled": feed.enabled}


@router.post("/sync")
async def sync_feeds(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Manually sync all feeds - fetch new entries"""
    rss_service = RssService(db)
    feeds = rss_service.get_user_feeds(current_user.id)

    total_new = 0
    for feed in feeds:
        if feed.enabled:
            new_count = await rss_service.sync_feed(feed)
            total_new += new_count

    return {"new_entries": total_new}


@router.get("/entries/unread")
async def get_unread_entries(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> List[EntryResponse]:
    """Get unread RSS entries"""
    rss_service = RssService(db)
    entries = rss_service.get_unread_entries(current_user.id, limit)
    return [
        EntryResponse(
            id=e.id,
            title=e.title,
            url=e.url,
            summary=e.summary,
            feed_name=e.feed.display_name,
            published_at=e.published_at.isoformat() if e.published_at else None,
            is_read=e.is_read
        )
        for e in entries
    ]


@router.post("/entries/read")
async def mark_entries_read(
    entry_ids: List[int],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Mark entries as read"""
    rss_service = RssService(db)
    count = rss_service.mark_entries_read(entry_ids)
    return {"marked_read": count}


@router.get("/entries/unposted")
async def get_unposted_entries(
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> List[EntryResponse]:
    """
    Get summarized entries that haven't been posted to external services.
    
    Used by bots (like posterchan) to get articles ready for posting to Fediverse.
    Returns entries that have summaries but is_posted=False.
    """
    entries = db.query(RssEntry).join(RssFeed).filter(
        RssFeed.user_id == current_user.id,
        RssFeed.enabled == True,
        RssEntry.is_summarized == True,
        RssEntry.is_posted == False
    ).order_by(RssEntry.published_at.desc()).limit(limit).all()
    
    return [
        EntryResponse(
            id=e.id,
            title=e.title,
            url=e.url,
            summary=e.summary,
            feed_name=e.feed.display_name,
            published_at=e.published_at.isoformat() if e.published_at else None,
            is_read=e.is_read,
            is_posted=e.is_posted
        )
        for e in entries
    ]


@router.post("/entries/{entry_id}/posted")
async def mark_entry_posted(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Mark an entry as posted to external services.
    
    Called by bots after successfully posting to Fediverse to prevent duplicates.
    """
    entry = db.query(RssEntry).join(RssFeed).filter(
        RssEntry.id == entry_id,
        RssFeed.user_id == current_user.id
    ).first()
    
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    
    entry.is_posted = True
    entry.is_read = True  # Also mark as read
    db.commit()
    
    return {"status": "ok", "entry_id": entry_id}


def parse_opml(content: str) -> List[dict]:
    """
    Parse OPML file content and extract feed URLs.
    
    Returns list of dicts with:
    - url: Feed URL (xmlUrl)
    - title: Feed title
    - category: Category/folder name (if any)
    """
    feeds = []
    try:
        root = ET.fromstring(content)
        
        def process_outline(outline, category=None):
            """Recursively process outline elements"""
            xml_url = outline.get('xmlUrl')
            if xml_url:
                # This is a feed
                feeds.append({
                    'url': xml_url,
                    'title': outline.get('title') or outline.get('text') or '',
                    'category': category
                })
            else:
                # This might be a folder/category
                folder_name = outline.get('title') or outline.get('text')
                for child in outline.findall('outline'):
                    process_outline(child, folder_name)
        
        # Find the body element
        body = root.find('body')
        if body is not None:
            for outline in body.findall('outline'):
                process_outline(outline)
        
    except ET.ParseError as e:
        logger.error(f"OPML parse error: {e}")
        raise ValueError(f"Invalid OPML file: {e}")
    
    return feeds


@router.post("/import/opml")
async def import_opml(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Import feeds from an OPML file.
    
    Accepts .opml or .xml files containing RSS feed subscriptions.
    """
    # Validate file type
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    
    if not (file.filename.endswith('.opml') or file.filename.endswith('.xml')):
        raise HTTPException(status_code=400, detail="File must be .opml or .xml")
    
    try:
        # Read file content
        content = await file.read()
        content_str = content.decode('utf-8')
        
        # Parse OPML
        feeds_data = parse_opml(content_str)
        
        if not feeds_data:
            return {"imported": 0, "skipped": 0, "message": "No feeds found in OPML file"}
        
        # Import feeds
        rss_service = RssService(db)
        imported = 0
        skipped = 0
        
        for feed_data in feeds_data:
            url = feed_data['url']
            title = feed_data['title']
            category = feed_data['category']
            
            # Build custom name with category if present
            custom_name = None
            if category and title:
                custom_name = f"[{category}] {title}"
            elif title:
                custom_name = title
            
            # Check if feed already exists
            existing = db.query(RssFeed).filter(
                RssFeed.user_id == current_user.id,
                RssFeed.url == url
            ).first()
            
            if existing:
                skipped += 1
                continue
            
            # Add feed (without fetching to speed up import)
            feed = RssFeed(
                user_id=current_user.id,
                url=url,
                title=title or None,
                custom_name=custom_name,
                enabled=True
            )
            db.add(feed)
            imported += 1
        
        db.commit()
        
        return {
            "imported": imported,
            "skipped": skipped,
            "total": len(feeds_data),
            "message": f"Imported {imported} feeds, skipped {skipped} duplicates"
        }
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded")
    except Exception as e:
        logger.error(f"OPML import error: {e}")
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


def generate_opml(feeds: List[RssFeed], title: str = "RSS Feeds") -> str:
    """
    Generate OPML XML content from a list of feeds.
    
    Groups feeds by category (extracted from custom_name if present).
    """
    # Create root element
    opml = ET.Element('opml', version='2.0')
    
    # Head section
    head = ET.SubElement(opml, 'head')
    title_el = ET.SubElement(head, 'title')
    title_el.text = title
    date_el = ET.SubElement(head, 'dateCreated')
    date_el.text = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    
    # Body section
    body = ET.SubElement(opml, 'body')
    
    # Group feeds by category
    categories = {}
    uncategorized = []
    
    for feed in feeds:
        # Extract category from custom_name if it has [Category] prefix
        category = None
        display_title = feed.title or feed.url
        
        if feed.custom_name and feed.custom_name.startswith('['):
            bracket_end = feed.custom_name.find(']')
            if bracket_end > 0:
                category = feed.custom_name[1:bracket_end]
                display_title = feed.custom_name[bracket_end + 1:].strip() or display_title
        
        feed_info = {
            'url': feed.url,
            'title': display_title,
            'html_url': ''  # We don't store htmlUrl
        }
        
        if category:
            if category not in categories:
                categories[category] = []
            categories[category].append(feed_info)
        else:
            uncategorized.append(feed_info)
    
    # Add categorized feeds
    for category_name, category_feeds in sorted(categories.items()):
        folder = ET.SubElement(body, 'outline', text=category_name, title=category_name)
        for feed_info in category_feeds:
            ET.SubElement(folder, 'outline',
                type='rss',
                text=feed_info['title'],
                title=feed_info['title'],
                xmlUrl=feed_info['url']
            )
    
    # Add uncategorized feeds
    for feed_info in uncategorized:
        ET.SubElement(body, 'outline',
            type='rss',
            text=feed_info['title'],
            title=feed_info['title'],
            xmlUrl=feed_info['url']
        )
    
    # Convert to string with XML declaration
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(opml, encoding='unicode')


@router.get("/export/opml")
async def export_opml(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Export all feeds as an OPML file.
    
    Returns an OPML XML file containing all the user's RSS feed subscriptions.
    """
    rss_service = RssService(db)
    feeds = rss_service.get_user_feeds(current_user.id)
    
    if not feeds:
        raise HTTPException(status_code=404, detail="No feeds to export")
    
    opml_content = generate_opml(feeds, f"{current_user.username}'s RSS Feeds")
    
    return Response(
        content=opml_content,
        media_type="application/xml",
        headers={
            "Content-Disposition": f"attachment; filename=feeds-{current_user.username}.opml"
        }
    )
