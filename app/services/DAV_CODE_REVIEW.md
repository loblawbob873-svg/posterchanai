# CalDAV/CardDAV Code Review

## Critical Issues Found

### 1. **Duplicate Code in DELETE Handler**
**Location**: `caldav_server.py:1309-1328`
**Issue**: Duplicate proxy initialization and filepath building
```python
# Line 1314-1315: First proxy initialization
proxy = DAVStorageProxy(db, user.username, 'caldav')

# Line 1322-1323: Duplicate proxy initialization (unused)
from app.services.dav_storage_proxy import DAVStorageProxy
proxy = DAVStorageProxy(db, user.username, 'caldav')

# Line 1317-1320: Duplicate filepath building
if cal_name == 'calendar':
    filepath = f"{event_uid}.ics"
else:
    filepath = f"{cal_name}/{event_uid}.ics"

# Line 1323-1326: Duplicate filepath building (unused)
if cal_name == 'calendar':
    filepath = f"{event_uid}.ics"
else:
    filepath = f"{cal_name}/{event_uid}.ics"
```
**Fix**: Remove duplicate code

### 2. **Sync-Token State Race Condition**
**Location**: `caldav_server.py:1022-1038`
**Issue**: Deletes ALL old tokens before creating new one. If two sync-collection requests happen simultaneously, the second one might delete the token created by the first, causing deletion detection to fail.
```python
# Delete old tokens for this calendar (keep only the latest)
db.query(CalDAVSyncToken).filter(...).delete()
# ... then create new token
```
**Fix**: Keep last N tokens (e.g., 5) instead of deleting all, or use a transaction with proper locking

### 3. **Missing Transaction Handling**
**Location**: Multiple places
**Issue**: Database operations not wrapped in transactions. If an error occurs mid-operation, database can be left in inconsistent state.
**Fix**: Wrap critical operations in try/except with rollback

### 4. **Path Normalization Issues**
**Location**: Multiple handlers
**Issue**: Inconsistent path handling:
- Some places use `unquote()` on path components
- Some places don't handle URL encoding
- Calendar name extraction inconsistent
**Fix**: Create a helper function for consistent path parsing

### 5. **Error Handling - Silent Failures**
**Location**: Multiple places
**Issue**: Many `except Exception as e: pass` or `except: pass` blocks that silently swallow errors
**Examples**:
- `caldav_server.py:728: except: pass`
- `caldav_server.py:1551: except:`
**Fix**: At minimum log errors, better to handle specific exceptions

### 6. **Sync-Token Format Inconsistency**
**Location**: `caldav_server.py:986-993`
**Issue**: Code handles both dict and list formats, but this creates complexity and potential bugs. Should standardize on one format.
**Fix**: Migrate all old format tokens to new format on first access

### 7. **Missing Validation**
**Location**: PUT handlers
**Issue**: No validation of:
- iCalendar/vCard format before saving
- UID format
- Calendar/addressbook name (could contain path traversal)
- File size limits
**Fix**: Add validation

### 8. **Timezone Issues**
**Location**: `caldav_server.py:519-523, 593-635`
**Issue**: Timezone handling is complex and error-prone:
- Mix of naive and timezone-aware datetimes
- Default to UTC might not match user's timezone
- All-day events converted to UTC might shift dates
**Fix**: Use user's timezone preference, handle all-day events specially

### 9. **XML Injection Risk**
**Location**: `create_caldav_response`, `create_cardav_response`
**Issue**: Using `html.escape()` but CDATA sections might not be properly escaped
**Fix**: Ensure all user data is properly escaped, validate CDATA content

### 10. **CardDAV Missing Sync-Token Support**
**Location**: `cardav_server.py`
**Issue**: CardDAV doesn't have sync-token support like CalDAV, so deletions won't sync properly
**Fix**: Implement sync-token support for CardDAV similar to CalDAV

### 11. **404 Response Format Issue**
**Location**: `caldav_server.py:136`
**Issue**: 404 responses might not be in correct CalDAV format. The propstat structure might be wrong.
**Fix**: Verify against CalDAV RFC, test with iPhone

### 12. **Calendar Name Defaulting**
**Location**: `caldav_server.py:1206-1209`
**Issue**: Defaulting to 'main' calendar might hide bugs. Should validate calendar exists.
**Fix**: Validate calendar name or return error

### 13. **Missing ETag in Some Responses**
**Location**: Various handlers
**Issue**: Not all responses include ETag, which iPhone needs for proper sync
**Fix**: Ensure all file responses include ETag

### 14. **No Rate Limiting**
**Location**: All handlers
**Issue**: No protection against rapid requests that could overwhelm server
**Fix**: Add rate limiting per user

### 15. **Memory Issues with Large Calendars**
**Location**: `caldav_server.py:1046-1100`
**Issue**: sync-collection reads ALL events into memory. For large calendars (1000+ events), this could cause memory issues.
**Fix**: Stream response or paginate

## Medium Priority Issues

### 16. **Inconsistent Logging Levels**
**Location**: Throughout
**Issue**: Mix of `logger.info`, `logger.debug`, `logger.warning` without clear pattern
**Fix**: Standardize logging levels

### 17. **Magic Strings**
**Location**: Throughout
**Issue**: Hard-coded strings like 'calendar', 'main', 'contacts' scattered throughout
**Fix**: Use constants

### 18. **No Input Sanitization**
**Location**: Path parsing
**Issue**: Path components not validated for dangerous characters
**Fix**: Add path sanitization

### 19. **Missing Content-Type Validation**
**Location**: PUT handlers
**Issue**: No check that content is actually valid iCalendar/vCard
**Fix**: Validate format before saving

### 20. **Database Query N+1 Problem**
**Location**: Sync-token lookups
**Issue**: Multiple queries instead of single query with join
**Fix**: Optimize queries

## Recommendations

1. **Add comprehensive unit tests** for all handlers
2. **Add integration tests** with actual iPhone client
3. **Implement proper transaction management**
4. **Add request validation middleware**
5. **Standardize error responses**
6. **Add monitoring/metrics**
7. **Document all edge cases**
8. **Add type hints throughout**
9. **Refactor duplicate code into helpers**
10. **Add request/response logging for debugging**
