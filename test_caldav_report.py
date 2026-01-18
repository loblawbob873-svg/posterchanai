#!/usr/bin/env python3
"""Test CalDAV REPORT handler."""
import sys
import asyncio
sys.path.insert(0, '/home/verita84/posterchanai')

from app.database import SessionLocal
from app.models import User
from app.services.caldav_server import handle_report
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

db = SessionLocal()
user = db.query(User).filter(User.username == 'verita84@poster.place').first()

# Test REPORT query for main calendar with wide time range
report_body = '''<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="20000101T000000Z" end="21000101T000000Z"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>'''

class MockRequest:
    async def body(self):
        return report_body.encode('utf-8')
    def __getattr__(self, name):
        return None

request = MockRequest()
result = asyncio.run(handle_report('main', user, db, request))
print(f'\nResponse status: {result.status_code}')
body = result.body if hasattr(result, 'body') else b''
if isinstance(body, bytes):
    body = body.decode('utf-8', errors='ignore')
print(f'Response length: {len(body)} bytes')
response_count = body.count('<D:response>')
print(f'Number of D:response tags: {response_count}')
