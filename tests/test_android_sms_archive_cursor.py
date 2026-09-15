"""Run Java-generated provider SQL against SQLite: page boundaries and MMS date units."""
from pathlib import Path
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / 'mobile/android/app/src/main/java/place/poster/app/sms'


def test_archive_cursor_keeps_timestamp_ties_and_orders_mixed_mms_units(tmp_path):
    driver = tmp_path / 'CursorQuery.java'
    driver.write_text('''import place.poster.app.sms.SmsArchiveCursor;
public class CursorQuery {public static void main(String[]args){
 for(boolean mms:new boolean[]{false,true}){
  System.out.println(SmsArchiveCursor.where(mms));System.out.println(SmsArchiveCursor.order(mms));
 }
}}''')
    subprocess.run(['javac', '-d', str(tmp_path), str(JAVA / 'SmsArchiveCursor.java'), str(driver)], check=True, capture_output=True, timeout=30)
    lines = subprocess.check_output(['java', '-cp', str(tmp_path), 'CursorQuery'], text=True, timeout=10).splitlines()
    for mms, (where, order) in enumerate(zip(lines[::2], lines[1::2])):
        db = sqlite3.connect(':memory:')
        db.execute('create table rows (_id integer, date integer)')
        # More than one 25-row page shares a timestamp. MMS contains both legal OEM units.
        date = 1_800_000_000_000
        rows = [(i, (date // 1000 if mms and i % 2 else date)) for i in range(1, 72)]
        rows += [(80, date + 1000), (81, (date + 2000) // 1000 if mms else date + 2000)]
        db.executemany('insert into rows values (?,?)', reversed(rows))
        seen, mark, boundary = [], 0, -1
        for _ in range(10):
            page = db.execute('select _id,date from rows where ' + where + ' order by ' + order + ' limit 25', (mark, mark, boundary)).fetchall()
            if not page:
                break
            seen.extend(row[0] for row in page)
            boundary, raw = page[-1]
            mark = raw * 1000 if mms and raw <= 100_000_000_000 else raw
        assert seen == [i for i, _ in rows], 'archive skipped/repeated a boundary row or mixed-unit date'
        assert len(seen) == len(set(seen))
