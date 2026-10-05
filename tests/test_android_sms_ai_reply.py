"""Texts ✨ on the phone: ThreadActivity's "suggest a reply", via SmsAiReply.

The native conversation screen has no app session, so it signs its own request (a kind-27235 bound
to `texts-ai-reply`, with the keystore key SmsArchive/SmsOutbox already use). Two things can drift
silently and both are checked by RUNNING code, not by reading it:

  * what the phone SENDS — the Java is compiled with javac and run against a real HTTP server, and
    the body it posted is then handed to the REAL endpoint (model stubbed): a proof the server's own
    verifier refuses, or a context shape it cannot parse, fails here instead of on somebody's phone
    as "AI replies are not available";
  * what the phone DOES with each answer — the probe's yes/no/unknown, a refusal (hide ✨), a failure
    (a sentence), an unreachable server (a sentence) — and the bounded {me, text} context.

The Activity wiring (✨ in the composer row, GONE until the server says yes, busy latch, draft goes
INTO THE COMPOSER and never onto the carrier) is pinned structurally: the Gradle build runs in CI.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from unittest import mock

import pytest

os.environ.setdefault("POSTERCHANAI_SKIP_DB", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app", "src", "main")
JAVA = os.path.join(ANDROID, "java", "place", "poster", "app")
STUBS = os.path.join(ROOT, "tests", "androidstubs")

SRC = [
    os.path.join(JAVA, "sms", "SmsAiReply.java"),
    os.path.join(JAVA, "sms", "SmsMsg.java"),
    os.path.join(JAVA, "sms", "SmsPart.java"),
    os.path.join(JAVA, "sms", "SmsKeys.java"),
    os.path.join(JAVA, "sync", "Json.java"),
    os.path.join(JAVA, "sync", "SyncCrypto.java"),
    os.path.join(JAVA, "signer", "Crypt.java"),
    os.path.join(JAVA, "signer", "Nostr.java"),
    os.path.join(JAVA, "signer", "Native.java"),
]

SEC = "1111111111111111111111111111111111111111111111111111111111111111"

DRIVER = r"""
    final java.util.List<String> seen = new java.util.ArrayList<String>();
    com.sun.net.httpserver.HttpServer srv =
        com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", 0), 0);
    srv.createContext("/", new com.sun.net.httpserver.HttpHandler() {
      public void handle(com.sun.net.httpserver.HttpExchange x) throws java.io.IOException {
        String path = x.getRequestURI().getPath();
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192]; int n;
        while ((n = x.getRequestBody().read(buf)) > 0) bo.write(buf, 0, n);
        String body = new String(bo.toByteArray(), "UTF-8");
        java.util.Map<String,Object> rec = new java.util.LinkedHashMap<String,Object>();
        rec.put("path", path); rec.put("ctype", x.getRequestHeaders().getFirst("Content-Type"));
        rec.put("body", body);
        seen.add(Json.write(rec));
        boolean probe = body.contains("\"probe\":true");
        int code = 200; String out;
        if (path.startsWith("/deny")) { code = 403; out = "{\"ok\":false,\"error\":\"AI access is not enabled for this account.\"}"; }
        else if (path.startsWith("/boom")) { code = 502; out = "{\"ok\":false,\"error\":\"The AI did not answer — try again in a moment.\"}"; }
        else if (path.startsWith("/garbage")) { out = "<html>proxy error</html>"; }
        else if (path.startsWith("/five")) out = "{\"ok\":true,\"content\":\"Yes! See you at 7\",\"choices\":[\"Yes! See you at 7\",\"Sounds good\",\" \",\"Sounds good\",\"Can we do 8?\",\"On my way\",\"Love that\",\"One too many\"]}";
        else if (probe) out = "{\"ok\":true,\"allowed\":true}";
        else out = "{\"ok\":true,\"content\":\"Yes! See you at 7\"}";
        byte[] b = out.getBytes("UTF-8");
        x.getResponseHeaders().add("Content-Type", "application/json");
        x.sendResponseHeaders(code, b.length); x.getResponseBody().write(b); x.close();
      }
    });
    srv.start();
    String base = "http://127.0.0.1:" + srv.getAddress().getPort();
    byte[] sec = place.poster.app.signer.Nostr.unhex("SECRET");

    java.util.List<SmsMsg> rows = new java.util.ArrayList<SmsMsg>();
    for (int i = 0; i < 14; i++) {
      SmsMsg m = new SmsMsg(); m.address = "+15550100"; m.date = 1000L * i;
      m.type = (i % 2 == 0) ? 1 : 2; m.body = "earlier   message\n" + i; rows.add(m);
    }
    SmsMsg blank = new SmsMsg(); blank.type = 1; blank.body = "   "; rows.add(blank);
    SmsMsg pic = new SmsMsg(); pic.type = 1; pic.mms = true; SmsPart p = new SmsPart(); p.ct = "image/jpeg";
    pic.parts.add(p); rows.add(pic);
    SmsMsg last = new SmsMsg(); last.type = 1; last.body = "Dinner at 7 tonight?"; rows.add(last);
    java.util.List<java.util.Map<String,Object>> ctx = SmsAiReply.context(rows);

    java.util.Map<String,Object> r = new java.util.LinkedHashMap<String,Object>();
    r.put("context", ctx);
    java.util.List<SmsMsg> rows2 = new java.util.ArrayList<SmsMsg>(rows);
    SmsMsg mine = new SmsMsg(); mine.type = 2; mine.body = "let me check"; rows2.add(mine);
    r.put("context_mine_newest", SmsAiReply.context(rows2));
    java.util.List<SmsMsg> rows3 = new java.util.ArrayList<SmsMsg>();
    SmsMsg only = new SmsMsg(); only.type = 2; only.body = "hello?"; rows3.add(only);
    r.put("context_only_mine", SmsAiReply.context(rows3));
    SmsAiReply.Result probe = SmsAiReply.call(base, sec, null);
    r.put("probe_allowed", probe.allowed); r.put("probe_ok", probe.ok);
    SmsAiReply.Result ok = SmsAiReply.call(base + "/", sec, ctx);
    r.put("ok", ok.ok); r.put("text", ok.text); r.put("old_server_choices", ok.choices);
    SmsAiReply.Result five = SmsAiReply.call(base + "/five", sec, ctx);
    r.put("five_ok", five.ok); r.put("five_text", five.text); r.put("five_choices", five.choices);
    SmsAiReply.Result deny = SmsAiReply.call(base + "/deny", sec, ctx);
    r.put("deny_refused", deny.refused); r.put("deny_error", deny.error);
    SmsAiReply.Result denyProbe = SmsAiReply.call(base + "/deny", sec, null);
    r.put("deny_probe_allowed", denyProbe.allowed);
    SmsAiReply.Result boom = SmsAiReply.call(base + "/boom", sec, ctx);
    r.put("boom_ok", boom.ok); r.put("boom_refused", boom.refused); r.put("boom_error", boom.error);
    SmsAiReply.Result garbage = SmsAiReply.call(base + "/garbage", sec, ctx);
    r.put("garbage_ok", garbage.ok); r.put("garbage_error", garbage.error);
    SmsAiReply.Result nokey = SmsAiReply.call(base, null, ctx);
    r.put("nokey_error", nokey.error);
    SmsAiReply.Result empty = SmsAiReply.call(base, sec, new java.util.ArrayList<java.util.Map<String,Object>>());
    r.put("empty_error", empty.error);
    int requestsBeforeDead = seen.size();
    srv.stop(0);
    SmsAiReply.Result dead = SmsAiReply.call(base, sec, ctx);
    r.put("dead_ok", dead.ok); r.put("dead_error", dead.error);
    r.put("requests_before_dead", requestsBeforeDead);

    r.put("vis_yes", SmsAiReply.visible("https://poster.place", true, Boolean.TRUE));
    r.put("vis_unknown", SmsAiReply.visible("https://poster.place", true, null));
    r.put("vis_no", SmsAiReply.visible("https://poster.place", true, Boolean.FALSE));
    r.put("vis_nokey", SmsAiReply.visible("https://poster.place", false, Boolean.TRUE));
    r.put("vis_noserver", SmsAiReply.visible("", true, Boolean.TRUE));

    SmsAiReply.remember("aa", null);
    r.put("cache_unknown_not_remembered", SmsAiReply.cached("aa") == null);
    SmsAiReply.remember("aa", Boolean.TRUE);
    r.put("cache_same_key", SmsAiReply.cached("aa"));
    r.put("cache_other_key", SmsAiReply.cached("bb") == null);

    java.util.Map<String,Object> all = new java.util.LinkedHashMap<String,Object>();
    all.put("results", r);
    java.util.List<Object> reqs = new java.util.ArrayList<Object>();
    for (String s : seen) reqs.add(Json.parse(s));
    all.put("requests", reqs);
    System.out.println(Json.write(all));
""".replace("SECRET", SEC)


@pytest.fixture(scope="module")
def wire():
    if shutil.which("javac") is None or shutil.which("java") is None:
        pytest.skip("no JDK")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "AiDriver.java")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("package place.poster.app.sms;\nimport place.poster.app.sync.Json;\n"
                     "public class AiDriver {\n  public static void main(String[] a) throws Exception {\n"
                     "%s\n  }\n}\n" % DRIVER)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-encoding", "UTF-8", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "java")] + SRC + [src],
                           capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-4000:]
        r = subprocess.run(["java", "-Dfile.encoding=UTF-8", "-cp", out, "place.poster.app.sms.AiDriver"],
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stderr[-4000:]
        return json.loads(r.stdout.strip())


def test_the_context_is_their_last_message_only(wire):
    """ "make sure ai for text message only generates a reply from the last message": one {me:false}
    -- their latest -- and nothing earlier, so the model cannot answer an old question instead."""
    r = wire["results"]
    assert r["context"] == [{"me": False, "text": "Dinner at 7 tonight?"}], r["context"]
    assert r["context_mine_newest"] == [{"me": False, "text": "Dinner at 7 tonight?"}], \
        "my own newest text is not what the reply answers"
    assert r["context_only_mine"] == [], "with nothing from them there is nothing to reply to"


def test_every_answer_becomes_what_the_screen_needs(wire):
    r = wire["results"]
    assert r["probe_ok"] is True and r["probe_allowed"] is True
    assert r["ok"] is True and r["text"] == "Yes! See you at 7"
    # An older server sends only `content`: still one usable draft.
    assert r["old_server_choices"] == ["Yes! See you at 7"]
    # "a popup of like 5 replies to choose, not one": distinct, non-blank, at most five, first = text.
    assert r["five_ok"] is True and r["five_text"] == "Yes! See you at 7"
    assert r["five_choices"] == ["Yes! See you at 7", "Sounds good", "Can we do 8?", "On my way", "Love that"], r["five_choices"]
    assert r["deny_refused"] is True and "not enabled" in r["deny_error"]
    assert r["deny_probe_allowed"] is False
    assert r["boom_ok"] is False and r["boom_refused"] is False and "did not answer" in r["boom_error"]
    assert r["garbage_ok"] is False and r["garbage_error"]
    assert "sign in" in r["nokey_error"]
    assert "no message" in r["empty_error"]
    assert r["dead_ok"] is False and "reach the server" in r["dead_error"]


def test_the_button_shows_only_on_a_yes_with_a_server_and_a_key(wire):
    r = wire["results"]
    assert r["vis_yes"] is True
    assert (r["vis_unknown"], r["vis_no"], r["vis_nokey"], r["vis_noserver"]) == (False, False, False, False)
    assert r["cache_unknown_not_remembered"] is True, "could-not-ask was remembered as an answer"
    assert r["cache_same_key"] is True and r["cache_other_key"] is True


def test_what_the_phone_sends_is_small_and_goes_to_one_path(wire):
    reqs = wire["requests"]
    # Neither the no-key nor the empty-context call touched the network.
    assert len(reqs) == wire["results"]["requests_before_dead"] == 7
    for q in reqs:
        assert q["path"].endswith("/api/texts/ai-reply") and "//api" not in q["path"], q["path"]
        assert q["ctype"] == "application/json"
        body = json.loads(q["body"])
        assert set(body) in ({"probe", "pubkey", "auth"}, {"messages", "count", "pubkey", "auth"}), body
        if "messages" in body:
            assert body["count"] == 5, "the phone asks for five drafts to choose from"
        assert "15550100" not in q["body"], "the phone number left the phone"


def test_the_real_endpoint_accepts_what_the_phone_sent(wire):
    """The bridge: the body the Java posted, fed to the REAL endpoint (model stubbed, the key a
    member). A proof this node's verifier rejects, or a context it cannot parse, fails here."""
    from app.routers import texts as T

    q = next(json.loads(x["body"]) for x in wire["requests"]
             if x["path"] == "/api/texts/ai-reply" and "messages" in json.loads(x["body"]))
    seen = []

    class _Chat:
        temperature = 0.7

        async def chat(self, msgs):
            seen.append(msgs)
            return "1. On my way\n2. Running late, 10 min\n3. Yes!\n4. Can we push to 8?\n5. See you there"

    class _CS:
        def __init__(self, db, user=None):
            self.chat_service = _Chat()

    async def member(pk):
        return pk == q["pubkey"]

    with mock.patch("app.services.command_service.CommandService", _CS), \
         mock.patch("app.services.nip05_access.is_member", member):
        out = asyncio.run(T.texts_ai_reply(T.TextsAiReplyReq(**q), db=None, session_user=None))
    # The phone asked for five: one model call, five distinct drafts, `content` = the first.
    assert out.get("ok") is True and out.get("content") == "On my way", getattr(out, "body", out)
    assert out.get("choices") == ["On my way", "Running late, 10 min", "Yes!", "Can we push to 8?", "See you there"], out
    assert len(seen) == 1, "five drafts must come from ONE model call"
    assert "Them: Dinner at 7 tonight?\nTEXTS" in seen[0][-1]["content"]


# ------------------------------------------------------------------ the Activity (structural)

LAYOUT = open(os.path.join(ANDROID, "res", "layout", "sms_thread.xml"), encoding="utf-8").read()
THREAD = open(os.path.join(JAVA, "sms", "ThreadActivity.java"), encoding="utf-8").read()
STRINGS = open(os.path.join(ANDROID, "res", "values", "strings.xml"), encoding="utf-8").read()


def _method(src, name):
    i = src.index("private void " + name + "(")
    j = src.index("\n    }\n", i)
    return src[i:j]


def test_the_sparkle_sits_in_the_composer_row_and_starts_hidden():
    compose = LAYOUT[LAYOUT.index('android:id="@+id/pc_th_compose"'):]
    block = compose[compose.index('android:id="@+id/pc_th_ai"') - 200:compose.index('android:id="@+id/pc_th_ai"') + 600]
    assert 'android:visibility="gone"' in block
    assert 'android:layout_height="44dp"' in block, "a touch target the size of its neighbours"
    assert compose.index("pc_th_emoji") < compose.index("pc_th_ai") < compose.index("pc_th_input")
    assert "@drawable/ic_pc_ai" in block, "the sprite sparkle, not an emoji"
    for s in ("sms_ai_reply", "sms_ai_busy", "sms_ai_ready", "sms_ai_nothing",
              "sms_ai_replace", "sms_ai_replace_ok"):
        assert re.search(r'<string name="%s">[^<]+</string>' % s, STRINGS), s


def test_the_activity_wires_it_through_smsaireply():
    assert "findViewById(R.id.pc_th_ai)" in THREAD
    assert "suggestReply();" in THREAD
    refresh = _method(THREAD, "refreshAi")
    assert "SmsAiReply.visible(" in refresh and "SmsAiReply.cached(" in refresh
    assert "SignerKey.load(" in refresh and "new Thread(" in refresh, "the keystore read left the main thread"
    assert "refreshAi();" in _method(THREAD, "reload")


def test_a_draft_goes_into_the_composer_and_never_onto_the_carrier():
    suggest = _method(THREAD, "suggestReply")
    fill = _method(THREAD, "fillComposer")
    for body in (suggest, fill):
        assert "send(" not in body.replace("suggestReply", ""), "the ✨ path can send"
        assert "SmsSender" not in body and "MmsSender" not in body
    assert "input.setText(text)" in fill and "MmsDraft.setText(this, address, text)" in fill
    # Several drafts: a pick list; the pick only fills the composer, asking before it overwrites.
    assert "R.string.sms_ai_pick" in suggest and ".setItems(" in suggest and "useDraft(" in suggest
    use = _method(THREAD, "useDraft")
    assert "R.string.sms_ai_replace" in use and "fillComposer(text)" in use
    assert "send(" not in use and "SmsSender" not in use and "MmsSender" not in use


def test_a_second_tap_while_busy_does_nothing_even_across_a_rebuild():
    assert re.search(r"private static final java\.util\.Set<String> aiBusy", THREAD), \
        "the busy latch must outlive the Activity (rotation / notification hand-over)"
    suggest = _method(THREAD, "suggestReply")
    guard = suggest.index("aiBusy.contains(who)) return;")
    assert guard < suggest.index("aiBusy.add(who)") < suggest.index("new Thread(")
    assert "aiBusy.remove(who)" in suggest
    busy = _method(THREAD, "paintAiBusy")
    assert "setEnabled(!busy)" in busy
