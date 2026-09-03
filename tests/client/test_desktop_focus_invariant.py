from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]
OS=(ROOT/'static/js/client/os.js').read_text(encoding='utf-8')


def test_every_surface_pair_keeps_the_newest_user_focus():
    done=subprocess.run(['node',str(ROOT/'tests/client/desktop_focus_invariant_runtime.js')],
                        cwd=ROOT,capture_output=True,text=True,timeout=90)
    assert done.returncode==0,done.stdout+done.stderr
    assert 'OK focus invariant 17x17' in done.stdout


def test_dom_and_compositor_focus_share_one_generation():
    focus=OS[OS.index('function focusWin(w, render)'):OS.index('function minimise',OS.index('function focusWin(w, render)'))]
    stack=OS[OS.index('async function _stackDomAboveNative'):OS.index('async function _releaseDomCoveredNative')]
    assert 'const focusToken=_claimFocus()' in focus
    assert '_stackDomAboveNative(w,focusToken)' in focus
    assert '_focusCompositorCurrent(shell.id,focusToken)' in stack
    assert '!_focusCurrent(focusToken)' in stack
    assert focus.index("classList.toggle('focused', x === w)") < focus.index('_stackDomAboveNative(w,focusToken)')
    assert focus.index('nextZ()') < focus.index('_stackDomAboveNative(w,focusToken)')


def test_adoption_and_popup_restore_cannot_override_a_later_click():
    adopt=OS[OS.index('async function adoptAll()'):OS.index('function disposeWindow',OS.index('async function adoptAll()'))]
    menu=OS[OS.index('async function _nativeMenuLayer'):OS.index('let _natFocusHold')]
    assert 'pass === _nativeAdoptPass && focusedNative' in adopt
    assert 'nativeMenuFocusGeneration===_focusGeneration' in menu


def test_focus_changing_shell_actions_use_the_same_authority():
    for marker in ('switchFocusToken=_claimFocus()', 'taskFocusToken=_claimFocus()'):
        assert marker in OS
    assert OS.count('_focusCompositorCurrent(')>=4
