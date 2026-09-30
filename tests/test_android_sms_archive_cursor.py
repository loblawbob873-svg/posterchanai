"""Run Java-generated provider SQL against SQLite: page boundaries and MMS date units.

The selection arguments are TEXT, as ContentResolver binds them -- see the comment below.
"""
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
            # AS TEXT, the way ContentResolver binds every selection argument. Passing Python ints
            # here is how the MMS half shipped broken: SQLite converts text for a comparison with
            # an INTEGER column but not with the MMS date EXPRESSION, where an integer is always
            # less than text -- so every picture message was skipped and this test still passed.
            page = db.execute('select _id,date from rows where ' + where + ' order by ' + order + ' limit 25',
                              (str(mark), str(mark), str(boundary))).fetchall()
            if not page:
                break
            seen.extend(row[0] for row in page)
            boundary, raw = page[-1]
            mark = raw * 1000 if mms and raw <= 100_000_000_000 else raw
        assert seen == [i for i, _ in rows], 'archive skipped/repeated a boundary row or mixed-unit date'
        assert len(seen) == len(set(seen))


def test_a_picture_message_sent_today_is_read_past_the_mark(tmp_path):
    """"i sent my mom a picture today and its not in texts also". The phone's MMS table stores dates
    in SECONDS; the sweep mark is milliseconds and reaches the provider as TEXT. The native archive
    must return the picture message, and a text message beside it."""
    driver = tmp_path / 'CursorQuery.java'
    driver.write_text("""import place.poster.app.sms.SmsArchiveCursor;
public class CursorQuery {public static void main(String[]a){System.out.println(SmsArchiveCursor.where(true));
System.out.println(SmsArchiveCursor.where(false));System.out.println(String.join(",",SmsArchiveCursor.args(1790700000000L,-1)));}}""")
    subprocess.run(['javac', '-d', str(tmp_path), str(JAVA / 'SmsArchiveCursor.java'), str(driver)], check=True, capture_output=True, timeout=30)
    mms_where, sms_where, args = subprocess.check_output(['java', '-cp', str(tmp_path), 'CursorQuery'], text=True, timeout=10).splitlines()
    args = args.split(',')
    db = sqlite3.connect(':memory:')
    db.execute('create table pdu (_id integer, date integer)')
    db.execute('create table sms (_id integer, date integer)')
    db.execute('insert into pdu values (7, 1790790000)')            # the picture, seconds
    db.execute('insert into pdu values (6, 1790600000)')            # an older one, before the mark
    db.execute('insert into sms values (9, 1790790500000)')         # a text, milliseconds
    assert db.execute('select _id from pdu where ' + mms_where, args).fetchall() == [(7,)], \
        'the picture message sent after the mark was not read'
    assert db.execute('select _id from sms where ' + sms_where, args).fetchall() == [(9,)]
