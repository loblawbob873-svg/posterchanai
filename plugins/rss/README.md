# Native RSS Plugin

A self-contained RSS feed reader plugin for Posterchanai.

## Features

- Subscribe to RSS/Atom feeds per user
- Automatic fetching every 30 minutes
- AI-powered article summarization
- Summaries stored in "RSS News" conversation

## Enable

1. Go to Admin → Services
2. Enable "Native RSS"
3. Restart the service

## User Setup

1. Go to User Settings → News & RSS
2. Enable "Native RSS"
3. Add feed URLs

## Commands

- `rss` - List your feeds
- `rss sync` - Manually fetch and summarize articles
- `rss add <url> [name]` - Add a new feed
- `rss remove <id>` - Remove a feed

## OPML Import/Export

Users can import and export feeds using OPML format:

**Import:**
1. Go to User Settings → News & RSS
2. Enable Native RSS
3. Click "Import OPML" button
4. Select your .opml or .xml file

**Export:**
1. Go to User Settings → News & RSS
2. Click "Export OPML" button
3. Save the downloaded file for backup or migration

Supported OPML features:
- Nested folders/categories (added as prefix to feed name)
- Standard RSS/Atom feed URLs
- Duplicate detection (existing feeds are skipped on import)

## API Endpoints

- `GET /api/rss/feeds` - List user's feeds
- `POST /api/rss/feeds` - Add a feed
- `DELETE /api/rss/feeds/{id}` - Remove a feed
- `POST /api/rss/feeds/{id}/toggle` - Enable/disable a feed
- `POST /api/rss/sync` - Sync all feeds
- `POST /api/rss/import/opml` - Import from OPML file
- `GET /api/rss/export/opml` - Export feeds as OPML file
- `GET /api/rss/entries/unread` - Get unread entries

## Files

- `__init__.py` - Plugin metadata
- `models.py` - Database models (RssFeed, RssEntry)
- `service.py` - Feed fetching and parsing
- `scheduler.py` - Background job (every 30 min)
- `router.py` - API endpoints
- `commands.py` - Chat command handler
