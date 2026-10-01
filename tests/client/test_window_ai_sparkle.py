"""PosterChan's window-scoped AI affordance stays explicit, contextual, and non-destructive."""

from pathlib import Path
from tests.client_source import client_source


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
APP = client_source()
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


def test_actions_are_buttons_in_the_panel_and_never_load_ai_chat():
    """ "we need interactive Agentic features with buttons, not loading up AI Chat": every request is
    answered in the panel (window_steps), every proposal is a button, and nothing calls the AI Chat
    handoff any more."""
    assert "Nothing runs until you press its button" in OS
    assert "action:'window_steps'" in OS
    toggle = OS[OS.index("function toggleWindowAI("):OS.index("function openDoc(")]
    assert "askWindowContext" not in toggle and "_aiTarget()" not in toggle, "the panel hands off to AI Chat again"
    assert "data-ai-open" not in OS and "Continue in AI" not in OS
    steps = OS[OS.index("async function _aiSteps("):OS.index("function toggleWindowAI(")]
    for wired in ("data-run", "data-type", "data-go", "data-ai-continue", "data-ai-tasks-note", "data-task-cal"):
        assert wired in steps, wired


def test_only_a_terminal_is_offered_commands_and_running_one_is_a_click():
    assert "Suggest commands I can run here with one click" in OS and "data-ai-cmds" in OS
    assert "const isTerm=/terminal|console|shell/i.test(ctx.title+' '+ctx.view);" in OS
    assert "${isTerm?'<label class=\"osw-ai-agent\"><input type=\"checkbox\" data-ai-cmds checked>" in OS
    steps = OS[OS.index("async function _aiSteps("):OS.index("function toggleWindowAI(")]
    assert "const cmds=!!(isTerm &&" in steps and "commands:cmds" in steps
    assert "window.PCTerm.run(st.text)" in steps

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
