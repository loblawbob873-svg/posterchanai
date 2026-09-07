"""Manifest permission required for Android 9+ launcher uninstall requests."""
from pathlib import Path
import xml.etree.ElementTree as ET


def test_launcher_requests_confirmed_uninstall_without_silent_delete_privilege():
    manifest=Path(__file__).resolve().parents[1]/'mobile/android/app/src/main/AndroidManifest.xml'
    permissions={node.get('{http://schemas.android.com/apk/res/android}name')
                 for node in ET.parse(manifest).getroot().findall('uses-permission')}
    assert 'android.permission.REQUEST_DELETE_PACKAGES' in permissions
    assert 'android.permission.DELETE_PACKAGES' not in permissions
