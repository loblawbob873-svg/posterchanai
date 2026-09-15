"""Live SMS alerts work before opening Texts and respect history, ownership and notification policy."""
from pathlib import Path
import subprocess
import pytest

@pytest.mark.parametrize('scenario',['new_account_recipient','cached_account_switch','uncached_recipient','multiwindow_reload','startup','late_login','duplicates','history','outgoing','muted','phone','active_thread','account_switch','hidden_thread','distinct_messages','catchup_before_live'])
def test_sms_notification_runtime(scenario):
    result=subprocess.run(['node',str(Path(__file__).with_name('sms_live_notifications.cjs')),scenario],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr
