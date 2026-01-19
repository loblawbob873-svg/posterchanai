# Bug Fixes Summary

## Issues Fixed (Code Changes Made)

### Issue #32: BUG: cal command
**Status**: Code fixed, needs verification
**Problem**: `cal week` and `cal month` not showing all events from all calendars
**Fixes Applied**:
- Updated date filtering logic to include overlapping events
- Fixed `cal week` to use proper end date (today + 7 days, 23:59:59)
- Fixed `cal month` to use `get_month_end()` utility for accurate month boundaries
- External CalDAV date filtering now uses same overlap logic as built-in
- All calendar discovery and aggregation logic updated

**Files Changed**:
- `app/services/caldav_service.py` - Date filtering and calendar discovery
- `app/services/command_service.py` - Calendar command date calculations
- `app/utils/date_utils.py` - New utility functions for month calculations

**Verification Needed**: Test `cal week` and `cal month` commands to confirm all events from all calendars are displayed

---

### Issue #31: BUG: Photo Gallery
**Status**: Code fixed, needs verification
**Problem**: SQLite error when accessing `api_key.user_id` (ObjectDeletedError)
**Fixes Applied**:
- Created `app/utils/auth_utils.py` with `query_api_key_with_retry` and `get_user_from_api_key`
- Reordered logic to access `user_id` BEFORE updating `last_used_at` and committing
- Added fallback refresh logic if APIKey becomes detached
- Applied fix to both `app/auth.py` and `app/routers/openai_api.py`

**Files Changed**:
- `app/auth.py` - API key handling
- `app/routers/openai_api.py` - API key handling
- `app/utils/auth_utils.py` - New utility functions

**Verification Needed**: Test Photo Gallery access to confirm no SQLite errors

---

### Issue #30: BUG: SCAN ALL USERS
**Status**: Code fixed, needs verification
**Problem**: Preview data not generated, photos showing old ones
**Fixes Applied**:
- Thumbnail skip logic updated: only skip if `thumbnail_mtime > media_mtime + 2` seconds
- This ensures thumbnails regenerate after EXIF restoration
- Fixed duplicate `stats['processed']` increment in EXIF utils

**Files Changed**:
- `app/services/thumbnail_service.py` - Thumbnail skip logic
- `app/utils/exif_utils.py` - Progress tracking

**Verification Needed**: Run SCAN ALL USERS and verify previews are generated correctly

---

### Issue #29: BUG: File Manager
**Status**: Code fixed, needs verification
**Problem**: Email and Share buttons too big, overlapping with icons
**Fixes Applied**:
- Reduced `.file-action-btn` size to 28px
- Added `flex-wrap: wrap` to `.file-selection-controls`
- Improved z-index layering
- Removed mobile upload area JavaScript

**Files Changed**:
- `static/css/file-manager.css` - Button sizing and layout
- `static/js/file-manager.js` - Removed mobile upload area logic

**Verification Needed**: Test File Manager UI on desktop and mobile to verify buttons don't overlap

---

### Issue #28: BUG: Notes
**Status**: Code fixed, needs verification
**Problem**: Notes UI launches fullscreen then resizes, content cut off
**Fixes Applied**:
- Added explicit media query to prevent fullscreen on desktop
- Increased modal sizes (max-width: 1600px, min-height: 700px)
- Added `animation: none !important` to prevent flash
- Forced browser reflow in JavaScript

**Files Changed**:
- `static/css/notes.css` - Modal sizing and animations
- `static/js/notes.js` - Reflow logic
- `templates/includes/modals/notes.html` - Initial display

**Verification Needed**: Test Notes UI to confirm no fullscreen flash and content fits properly

---

### Issue #36: BUG: CardDAV Address update
**Status**: Code fixed, needs verification
**Problem**: "Failed to update contact: CardDAV server rejected the update" - ADR field error
**Fixes Applied**:
- Fixed ADR field handling to support both single object and list of objects
- Updated all ADR reading locations to handle both cases
- Fixed ADR writing to properly handle existing ADR fields
- Fixed indentation error in address extraction

**Files Changed**:
- `app/services/caldav_service.py` - ADR field handling in all contact functions

**Verification Needed**: Test updating a contact with an address field to confirm it works

---

### Issue #37: LLM Training for Bills: Walmart
**Status**: Code fixed, needs verification
**Problem**: LLM not extracting bills from Walmart delivery order emails
**Fixes Applied**:
- Updated LLM prompts to recognize Walmart delivery order format
- Added handling for "Order total" with "Includes all fees, taxes and discounts"
- Added delivery date extraction from "Arrives Mon, Jan 19" format
- Works for both `budget extract` and `mail extract-bill` commands

**Files Changed**:
- `app/services/command_service.py` - Bill extraction prompts

**Verification Needed**: Test with actual Walmart delivery order email

---

### Issue #35: BUG: CARDDAV Address (UI)
**Status**: Code fixed, needs verification
**Problem**: User Settings shows "CardDAV server not enabled" when it is enabled
**Fixes Applied**:
- Fixed API field name mismatch (`cardav_url` vs `carddav_url`)
- JavaScript now checks both field names for backwards compatibility
- Added empty string handling with `.trim()`

**Files Changed**:
- `app/routers/auth.py` - API response field name
- `static/js/chat.js` - JavaScript field checking

**Verification Needed**: Check User Settings to confirm CardDAV address displays correctly

---

## Issues Not Yet Fixed

### Issue #27: Feature: Custom User LLM Training Prompt
**Status**: Not implemented
**Description**: Add ability for users to define custom LLM prompt training via User Settings

---

### Issue #38: StableDiffusionXLPipeline depreciation
**Status**: Not fixed
**Description**: Deprecation warning for StableDiffusionXLPipeline

---

## Testing Recommendations

1. **Issue #32**: Run `cal week` and `cal month` commands and verify all events from all calendars are shown
2. **Issue #31**: Access Photo Gallery and check logs for SQLite errors
3. **Issue #30**: Run SCAN ALL USERS and verify previews are generated
4. **Issue #29**: Open File Manager and verify button layout
5. **Issue #28**: Open Notes UI and verify no fullscreen flash
6. **Issue #36**: Update a contact with address field
7. **Issue #37**: Test with Walmart delivery order email
8. **Issue #35**: Check User Settings CardDAV address display

## Notes

- All code changes have been committed and pushed
- Some fixes require the storage server to be running for testing
- UI fixes need to be tested in the actual browser environment
- The fixes address the root causes identified in the code, but user testing is needed to confirm they work in practice
