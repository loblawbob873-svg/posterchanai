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
  String[][] c = {{"image/png","shot"},{"image/jpeg; charset=x","photo.jpeg"},{"video/mp4","clip.webm"},{"application/pdf","doc.pdf"},{"","../../etc/passwd"},{"IMAGE/PNG","a.png"}};
  StringBuilder o=new StringBuilder();
  for(String[] x:c){ String m=MediaSaveRules.mime(x[0]); String k=MediaSaveRules.kind(m);
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
    assert out[0] == "image/png|Pictures/PosterChan/|shot.png"
    assert out[1] == "image/jpeg|Pictures/PosterChan/|photo.jpg"
    assert out[2] == "video/mp4|Movies/PosterChan/|clip.mp4"
    assert out[3] == "application/pdf|Download/PosterChan/|doc.pdf"
    assert out[4].startswith("application/octet-stream|Download/PosterChan/|") and "/" not in out[4].split("|")[2]
    assert out[5] == "image/png|Pictures/PosterChan/|a.png"


def test_the_plugin_is_registered_and_never_asks_for_storage_permission():
    main = open(os.path.join(ROOT, "mobile/android/app/src/main/java/place/poster/app/MainActivity.java")).read()
    assert "registerPlugin(place.poster.app.preview.MediaSavePlugin.class);" in main
    plugin = open(os.path.join(SRC, "MediaSavePlugin.java")).read()
    assert "RELATIVE_PATH" in plugin and "IS_PENDING" in plugin and "SDK_INT >= 29" in plugin
    manifest = open(os.path.join(ROOT, "mobile/android/app/src/main/AndroidManifest.xml")).read()
    assert "WRITE_EXTERNAL_STORAGE" not in manifest
