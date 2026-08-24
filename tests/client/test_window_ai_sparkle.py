"""PosterChan's window-scoped AI affordance stays explicit, contextual, and non-destructive."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_every_posterchan_window_gets_an_accessible_sparkle_control():
    assert 'class="osw-b osw-ai" data-w="ai"' in OS
    assert 'aria-label="Ask AI about this window"' in OS
    assert "if(a === 'ai') toggleWindowAI(w, b, e)" in OS
    assert ".osw-ai-panel" in CSS


def test_context_is_collected_only_after_the_user_opens_the_control():
    button = OS.index("if(a === 'ai') toggleWindowAI")
    collect = OS.index("function windowAIContext")
    panel = OS.index("function toggleWindowAI")
    assert collect < panel
    assert button < panel
    assert "window.getSelection()" in OS
    assert ".slice(0,4000)" in OS


def test_native_apps_are_metadata_only_not_silently_screen_scraped():
    assert "if(!selection && w.native==null)" in OS
    assert "w.native!=null?'native app':'PosterChan app'" in OS
    assert "App name only · private by default" in OS


def test_actions_open_a_reviewable_ai_draft_instead_of_auto_sending_or_mutating():
    assert "Review before sending · no automatic changes" in OS
    assert "PC().askWindowContext({windows:contexts},instruction,{agent})" in OS
    assert "function askWindowContext(ctx,instruction,opts)" in APP
    assert "switchView('ai')" in APP
    assert "ta.value=_aiWindowDraft" in APP
    scope = APP[APP.index("function askWindowContext(ctx,instruction,opts)"):
                APP.index("function _cookie", APP.index("function askWindowContext(ctx,instruction,opts)"))]
    assert "aiSend()" not in scope


def test_terminal_and_file_windows_can_opt_into_the_existing_system_agent():
    assert "Use the system agent to run commands or change files" in OS
    assert "data-ai-agent" in OS
    assert "askWindowContext({windows:contexts},instruction,{agent})" in OS
    assert "(opts&&opts.agent)?'node agent local ':''" in APP


def test_suggestions_are_tailored_to_common_window_kinds():
    for kind in ("terminal|console|shell", "firefox|browser|web", "telegram|message|chat|mail",
                 "file|drive|folder", "settings"):
        assert kind in OS


def test_shift_click_and_drag_build_an_explicit_multi_window_context():
    assert "event&&event.shiftKey" in OS
    assert "application/x-pc-ai-window" in OS
    assert "_aiContextAdd(_aiDragWin)" in OS
    assert "Shift-click ✨ to collect windows" in OS
    assert "const windows=Array.isArray(ctx.windows)" in APP
    assert "while(_aiContextWins.size>=4)" in OS


def test_window_watching_is_explicit_visible_and_cleaned_up():
    assert "Watch this window and glow when its contents change" in OS
    assert "new MutationObserver" in OS
    assert "n===realFeed" in OS
    assert "w.el.classList.add('ai-alert')" in OS
    assert "if(w.aiWatch)w.aiWatch.disconnect()" in OS
    assert ".osw.ai-alert .osw-ai" in CSS
