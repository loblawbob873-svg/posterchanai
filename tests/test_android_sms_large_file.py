"""Run the shipped streaming sender with a 24 MB heap and a 25 MB attachment."""
import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from tests.test_android_mms_link import SRC

HARNESS = r'''
package place.poster.app.sms;
import java.io.*;
import java.nio.file.*;
import java.security.*;
public class LargeLinkHarness {
  public static void main(String[] args) throws Exception {
    File dir = new File(args[0]);
    File plain = new File(dir, "plain");
    try(FileOutputStream out = new FileOutputStream(plain)) {
      byte[] b = new byte[65536];
      for(int i=0;i<b.length;i++) b[i]=(byte)(i*31+7);
      for(int i=0;i<400;i++) out.write(b);
    }
    final boolean fail = args.length > 1;
    MmsLink.FileIo io = new MmsLink.FileIo() {
      int uploads;
      public String apiBase(){return "https://node.example";}
      public String mediaBase(){return "https://node.example/blossom";}
      public String upload(byte[] b) {throw new AssertionError("whole-file upload");}
      public String upload(File file) throws Exception {
        if(file.length() > 4*1024*1024) throw new AssertionError("blob exceeds server limit");
        if(fail && ++uploads == 2) throw new IOException("connection lost");
        MessageDigest d = MessageDigest.getInstance("SHA-256");
        try(InputStream in = new FileInputStream(file)) {
          byte[] b=new byte[65536]; int n; while((n=in.read(b))!=-1) d.update(b,0,n);
        }
        StringBuilder sha=new StringBuilder(); for(byte b:d.digest())sha.append(String.format("%02x",b&255));
        Files.copy(file.toPath(),new File(dir,sha.toString()).toPath());
        return sha.toString();
      }
      public String sendText(String body) {
        if(fail) throw new AssertionError("sent a link after failed upload");
        return "";
      }
    };
    MmsLink.Result r = MmsLink.send(io,"caption",plain,"video/mp4","movie.mp4",dir);
    System.out.println(place.poster.app.sync.Json.write(new java.util.LinkedHashMap<String,Object>() {{
      put("ok",r.ok);put("link",r.link);put("error",r.error);
    }}));
  }
}
'''

@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    if not shutil.which("javac"):
        pytest.skip("JDK unavailable")
    build = tmp_path_factory.mktemp("large-link-java")
    source = build / "LargeLinkHarness.java"
    source.write_text(HARNESS)
    proc = subprocess.run(["javac", "-d", str(build), *SRC, str(source)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return build

@pytest.mark.parametrize("fail", [False, True])
def test_large_file_streams_without_heap_or_carrier_limit(compiled, tmp_path, fail):
    proc = subprocess.run(["java", "-Xmx24m", "-cp", str(compiled),
                           "place.poster.app.sms.LargeLinkHarness", str(tmp_path), *(["fail"] if fail else [])],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert not list(tmp_path.glob("sms-link-*.enc")), "temporary ciphertext leaked"
    if fail:
        assert not got["ok"]
        assert "connection lost" in got["error"]
        assert not got["link"]
        return
    assert got["ok"], got
    token = got["link"].split("#pcenc1=")[1]
    meta = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert meta["c"] == 1
    key = AESGCM(base64.urlsafe_b64decode(meta["k"] + "="))
    def decrypt(sha):
        blob = (tmp_path / sha).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == sha
        return key.decrypt(blob[:12], blob[12:], None)
    sha = got["link"].split("/f/")[1].split("#")[0]
    manifest = json.loads(decrypt(sha))
    assert manifest["size"] == 25 * 1024 * 1024
    digest = hashlib.sha256()
    total = 0
    for item in manifest["chunks"]:
        plain = decrypt(item["sha"])
        assert len(plain) == item["size"]
        digest.update(plain)
        total += len(plain)
    assert total == manifest["size"]
    assert digest.hexdigest() == hashlib.sha256((tmp_path / "plain").read_bytes()).hexdigest()
