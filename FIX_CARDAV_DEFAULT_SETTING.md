# ✅ FIX: CardDAV/CalDAV Default Setting

## Issue

When users opened User Settings → Calendar & Contacts tab, they saw:
- **"CardDAV Contacts: Not enabled"** even though admin enabled the server
- This was confusing because the built-in server was enabled and working

## Root Cause

The JavaScript code in `static/js/chat.js` was loading user preferences and defaulting to **"external"** mode when the `use_builtin_cardav` or `use_builtin_caldav` settings were `undefined` (i.e., user never explicitly set a preference).

```javascript
// BEFORE (Line 907, 918):
const useBuiltin = data.use_builtin_caldav === 'true' || data.use_builtin_caldav === true;
// If data.use_builtin_caldav is undefined, useBuiltin = false
// Therefore: dropdown shows "external", and "Not enabled" message appears
```

## Solution

Changed the logic to **default to "builtin"** when the setting is not explicitly set:

```javascript
// AFTER:
const useBuiltin = data.use_builtin_caldav === undefined || 
                   data.use_builtin_caldav === null || 
                   data.use_builtin_caldav === 'true' || 
                   data.use_builtin_caldav === true;
// Now defaults to builtin mode (which is the intended default)
```

## Changes Made

**File**: `static/js/chat.js`

1. **Line ~905-911**: Calendar server type loading
   - Added checks for `undefined` and `null`
   - Now defaults to `'builtin'`

2. **Line ~916-923**: Contacts server type loading
   - Added checks for `undefined` and `null`
   - Now defaults to `'builtin'`

## User Experience

### Before Fix
```
User Settings → Calendar & Contacts
  [Contacts Server Type: ▼ External CardDAV Server]
  
  ❌ CardDAV Contacts: Not enabled (enable in Admin → Site Settings)
```

### After Fix
```
User Settings → Calendar & Contacts
  [Contacts Server Type: ▼ Built-in CardDAV Server]
  
  ✅ Built-in Contacts Info
     Using the built-in CardDAV server. Contacts are stored locally and accessible via:
     • Chat commands: contacts add, contacts list, contacts edit
     • CardDAV protocol: See "Storage & Cloud" tab for sync URL
     • File storage: {storage}/carddav/*.vcf
```

## Why This Makes Sense

1. **Admin Enabled = Should Work**: If admin enables CalDAV/CardDAV in site settings, users should see it as available by default

2. **Simplicity**: Most users don't have external CalDAV/CardDAV servers - built-in is the common case

3. **Discoverability**: Users can immediately see that contacts/calendar commands are available

4. **Backwards Compatible**: Users who explicitly set external mode still have their preference loaded correctly

## Deployment

- ✅ Committed: `649a122` - "Fix CardDAV/CalDAV default to builtin when not explicitly set"
- ✅ Pushed to git.poster.place
- ✅ Deployed to 192.168.0.85
- ⚠️ 192.168.0.72 is offline (No route to host)

## Testing

1. **Fresh User** (no preferences set):
   - Open User Settings → Calendar & Contacts
   - Should see "Built-in CalDAV Server" selected
   - Should see "Built-in Contacts Info" section visible

2. **Existing User** (has external config):
   - Preferences should load correctly
   - "External" option should remain selected

3. **Commands Work**:
   ```bash
   cal add test event tomorrow at 3pm
   contacts add John Doe john@example.com 555-1234
   ```
   Both should work immediately without additional configuration

---

**Status**: 🎉 **FIXED!**

Users will now see the built-in CardDAV/CalDAV as available by default when the admin has enabled these services.
