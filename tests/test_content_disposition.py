"""Every download's Content-Disposition survives a filename that is not ASCII.

Header values are encoded latin-1; a name like «Отчёт.docx» or 写真.png put straight in the header
turned the download into a 500 (Telegram attachments, mail attachments, addressbook/calendar exports,
Office exports). app/services/content_disposition.py is the one builder they all use now.
"""
from urllib.parse import unquote

import pytest

from app.services.content_disposition import content_disposition


@pytest.mark.parametrize("name", ["Отчёт за май.docx", "写真.png", "café – résumé.pdf", "it’s.txt", "plain.pdf"])
def test_the_header_encodes_and_keeps_the_real_name(name):
    cd = content_disposition("attachment", name)
    cd.encode("latin-1")                      # what the ASGI layer does -- raised before
    assert cd.startswith('attachment; filename="')
    if name.isascii():
        assert cd == f'attachment; filename="{name}"'
    else:
        assert unquote(cd.split("filename*=UTF-8''")[1]) == name


def test_an_all_foreign_name_keeps_its_extension_in_the_fallback():
    assert content_disposition("inline", "写真.png").startswith('inline; filename="download.png"')


@pytest.mark.parametrize("bad", ['a"b.pdf', "a\r\nSet-Cookie: x.pdf", "../../etc/passwd", "a\\b.pdf", ""])
def test_nothing_can_break_out_of_the_header(bad):
    cd = content_disposition("attachment", bad, "file")
    assert "\r" not in cd and "\n" not in cd and "/" not in cd.split("filename*=")[0]
    ascii_part = cd.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in ascii_part and "\\" not in ascii_part and ascii_part


@pytest.mark.parametrize("mod,call", [
    ("app.routers.contacts", lambda m: content_disposition("attachment", f"{m._slug('Семья')}.vcf", "contacts")),
    ("app.routers.calendar", lambda m: content_disposition("attachment", f"{m._slug('Работа')}.ics", "calendar")),
])
def test_exports_named_in_another_script_encode(mod, call):
    import importlib
    call(importlib.import_module(mod)).encode("latin-1")
