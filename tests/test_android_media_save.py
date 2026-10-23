"""MediaSave (APK): where a saved picture goes and what it is called -- the pure rules RUN in Java.

The image viewer's Save used to be a second share sheet on Android; it now writes into MediaStore. A
gallery hides a file without the right extension, so the name must end in one that matches the type,
and pictures / videos / other files each go to their own folder.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "mobile/android/app/src/main/java/place/poster/app/preview")
JAVAC, JAVA = shutil.which("javac"), shutil.which("java")

H = r"""
import place.poster.app.preview.MediaSaveRules;
public class MS { public static void main(String[] a){
  String[][] c = {{"image/png","shot",""},{"image/jpeg; charset=x","photo.jpeg",""},{"video/mp4","clip.webm",""},{"application/pdf","doc.pdf",""},{"","../../etc/passwd",""},{"IMAGE/PNG","a.png",""},
                  {"audio/mpeg","posterchan-song-2026","" },{"audio/wav","take.wav","voice"},{"video/mp4","voice-line","voice"},{"audio/mp4","tune.m4a",""},{"audio/ogg","x.oga",""},{"audio/flac","a.flac","VOICE"}};
  StringBuilder o=new StringBuilder();
  for(String[] x:c){ String m=MediaSaveRules.mime(x[0]); String k=MediaSaveRules.kind(m, x[2]);
    o.append(m).append('|').append(MediaSaveRules.folder(k)).append('|').append(MediaSaveRules.name(x[1],m)).append('\n'); }
  System.out.print(o);
}}
"""


@pytest.mark.skipif(not (JAVAC and JAVA), reason="JDK not installed")
def test_saved_files_land_in_the_right_folder_with_the_right_name():
    tmp = tempfile.mkdtemp()
    open(os.path.join(tmp, "MS.java"), "w").write(H)
    r = subprocess.run([JAVAC, "-d", tmp, os.path.join(SRC, "MediaSaveRules.java"), os.path.join(tmp, "MS.java")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([JAVA, "-cp", tmp, "MS"], capture_output=True, text=True).stdout.splitlines()
    # ONE PosterChan tree with a subfolder per type (operator, 2026-10-11) -- it used to be
    # Pictures/PosterChan, Movies/PosterChan and Download/PosterChan: three places to look. A top-level
    # /PosterChan needs MANAGE_EXTERNAL_STORAGE on Android 10+, so the tree lives under Download/.
    assert out[0] == "image/png|Download/PosterChan/Images/|shot.png"
    assert out[1] == "image/jpeg|Download/PosterChan/Images/|photo.jpg"
    assert out[2] == "video/mp4|Download/PosterChan/Videos/|clip.mp4"
    assert out[3] == "application/pdf|Download/PosterChan/Files/|doc.pdf"
    assert out[4].startswith("application/octet-stream|Download/PosterChan/Files/|") and "/" not in out[4].split("|")[2]
    assert out[5] == "image/png|Download/PosterChan/Images/|a.png"
    # Songs go to Music, with the extension a music app needs to list them.
    assert out[6] == "audio/mpeg|Download/PosterChan/Music/|posterchan-song-2026.mp3"
    # A voice is SAID by the page (the hint), never guessed from the bytes -- audio OR an MP4 line.
    assert out[7] == "audio/wav|Download/PosterChan/Voices/|take.wav"
    assert out[8] == "video/mp4|Download/PosterChan/Voices/|voice-line.mp4"
    assert out[9] == "audio/mp4|Download/PosterChan/Music/|tune.m4a"
    assert out[10] == "audio/ogg|Download/PosterChan/Music/|x.ogg"
    assert out[11] == "audio/flac|Download/PosterChan/Voices/|a.flac"


def test_every_save_goes_into_the_one_downloads_tree():
    """Images/Videos/Music collections refuse a RELATIVE_PATH under Download/; the Downloads collection
    takes any type there. So every save is inserted into MediaStore.Downloads, and the page's hint
    reaches the rule."""
    plugin = open(os.path.join(SRC, "MediaSavePlugin.java")).read()
    save = plugin[plugin.index("public void save("):]
    # The write itself is MediaSaveWriter's, shared with the native Messages screen (one tree, one
    # set of rules); the plugin hands it the page's hint.
    assert 'call.getString("hint"' in save and "MediaSaveWriter.write(" in save and "name, mime, hint)" in save
    writer = open(os.path.join(SRC, "MediaSaveWriter.java")).read()
    assert "MediaStore.Downloads.EXTERNAL_CONTENT_URI" in writer
    assert "Images.Media.EXTERNAL_CONTENT_URI" not in writer and "Video.Media.EXTERNAL_CONTENT_URI" not in writer
    assert "MediaSaveRules.kind(mime, hint)" in writer


def test_the_plugin_is_registered_and_never_asks_for_storage_permission():
    main = open(os.path.join(ROOT, "mobile/android/app/src/main/java/place/poster/app/MainActivity.java")).read()
    assert "registerPlugin(place.poster.app.preview.MediaSavePlugin.class);" in main
    writer = open(os.path.join(SRC, "MediaSaveWriter.java")).read()
    assert "RELATIVE_PATH" in writer and "IS_PENDING" in writer and "SDK_INT >= 29" in writer
    manifest = open(os.path.join(ROOT, "mobile/android/app/src/main/AndroidManifest.xml")).read()
    assert "WRITE_EXTERNAL_STORAGE" not in manifest
