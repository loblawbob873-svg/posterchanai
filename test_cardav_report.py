#!/usr/bin/env python3
"""Test CardDAV REPORT handler."""
import sys
import asyncio
sys.path.insert(0, '/home/verita84/posterchanai')

from app.database import SessionLocal
from app.models import User
from app.services.cardav_server import handle_report
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

db = SessionLocal()
user = db.query(User).filter(User.username == 'verita84@poster.place').first()

# Test REPORT query for contacts addressbook
report_body = '''<?xml version="1.0" encoding="utf-8"?>
<C:addressbook-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:prop>
    <D:getetag/>
    <C:address-data/>
  </D:prop>
</C:addressbook-query>'''

class MockRequest:
    async def body(self):
        return report_body.encode('utf-8')
    def __getattr__(self, name):
        return None

request = MockRequest()
result = asyncio.run(handle_report('contacts', user, db, request))
print(f'\nResponse status: {result.status_code}')
body = result.body if hasattr(result, 'body') else b''
if isinstance(body, bytes):
    body = body.decode('utf-8', errors='ignore')
print(f'Response length: {len(body)} bytes')
response_count = body.count('<D:response>')
print(f'Number of D:response tags: {response_count}')
