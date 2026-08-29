from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_dragging_keeps_native_surface_live_and_coalesces_position_moves():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    assert "if(nativeWins().length) nsync()" in drag
    assert "if(stash.has(it.native))" in src
    assert "if(it.w.gesturing && was !== 'hidden')" in src
    assert "await pcWM.move(it.native, rect.x, rect.y)" in src
    # Sway refuses `floating enable`/resize on a hidden scratchpad container. Restore first, then
    # place; the opposite order leaves the native app parked while only its HTML frame moves.
    assert src.index("pcWM.show(it.native") < src.index("pcWM.place(it.native")
    assert "_natMove(w)" not in drag
    assert "pcWM.place" not in drag
    assert "setPointerCapture(ev.pointerId)" in drag
    assert "if(w.native == null) window.addEventListener('blur', cancel)" in drag


def test_cancelled_drag_restores_geometry_and_never_snaps_or_hands_off():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    up = drag[drag.index("const up = (endEvent, cancelled) =>"):
              drag.index("document.addEventListener('pointermove'")]
    assert "document.addEventListener('pointercancel', cancel)" in drag
    assert "w.el.addEventListener('lostpointercapture', lostCapture)" in drag
    assert "w.el.removeEventListener('lostpointercapture', lostCapture)" in drag
    assert "Object.assign(w.el.style,{left:before.left,top:before.top,width:before.width,height:before.height})" in up
    assert "w.snap=before.snap; w.max=before.max; w.rect=before.rect" in up
    cancel = up[up.index("if(cancelled){", up.index("hideGhost()")):]
    assert cancel.index("_natGesture(w,false)") < cancel.index("return;")
    assert cancel.index("return;") < cancel.index("if(handoff && w.native != null")
    assert cancel.index("return;") < cancel.index("if(zone){ _natGesture(w,false); snapTo(w,zone); }")


def test_native_bridge_retains_move_for_non_gesture_placement_operations():
    preload = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    wm = (ROOT / "desktop/wm.js").read_text(encoding="utf-8")
    assert "pc:wm:move" in preload and "pc:wm:move" in main
    assert "move(id, x, y)" in wm


def test_snapping_ends_move_only_mode_before_the_full_native_resize():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    up = src[src.index("const up = (endEvent, cancelled) =>", src.index("function startDrag")):
             src.index("document.addEventListener('pointermove'", src.index("function startDrag"))]
    snap = "if(zone){ _natGesture(w,false); snapTo(w,zone); }"
    assert snap in up
    snap = src[src.index("function snapTo"):src.index("function unsnap")]
    assert "_natSent.delete(Number(w.native))" in snap
    assert "requestAnimationFrame(() => requestAnimationFrame(nsync))" in snap


def test_mouse_edge_snap_is_not_stolen_by_a_timed_monitor_handoff():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    assert "edgeHoldAt" not in drag
    assert "return realScreen&&clientAtEdge&&(edgeOverflow(e,dir)>8||clientOverflow>8)?dir:''" in drag
    assert "if(handoff && w.native != null && pcWM.handoff)" in drag


def test_scaled_screen_coordinates_need_outward_edge_travel_before_handoff():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    handoff = drag[drag.index("const handoffDirection ="):drag.index("const preview =")]
    assert "clientAtEdge" in handoff
    assert "edgeOverflow(e,dir)>8" in handoff


def test_cross_monitor_handoff_needs_two_move_samples_not_a_repeated_pointerup():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    move = drag[drag.index("const move = (e) =>"):drag.index("let ended = false")]
    up = drag[drag.index("const up = (endEvent, cancelled) =>"):]
    assert "candidate===crossDir" in move
    assert "crossSamples>=2?candidate:''" in move
    assert "handoffDirection(endEvent) || handoff" not in up


def test_native_drag_runtime_distinguishes_same_output_from_clamped_cross_output():
    """Execute the shipped edge state machine with Chromium-style clamped client coordinates."""
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    edge = drag[drag.index("const edgeDirection ="):drag.index("const preview =")]
    script = f"""
      global.window={{innerWidth:1000,innerHeight:700,screenX:0,screenY:0}};
      const run=(screenX,clientX)=>{{ const realScreen=true; {edge}
        return handoffDirection({{screenX,screenY:300,clientX,clientY:300}}); }};
      process.stdout.write(JSON.stringify([run(995,995),run(1040,999)]));
    """
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == ["", "right"]


def test_cross_output_native_drop_geometry_reaches_destination_ack_frame():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    preload = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    begin = src.index("pcWM.onNativeHandoffPrepare(async")
    receiver = src[begin:src.index("if(!_nativeHandoffOff", begin)]
    assert "handoffDrop(handoff,endEvent||lastMove)" in drag
    assert "handoff: (id, direction, drop)" in preload
    assert "drop=drop&&typeof drop==='object'?Object.assign({},drop,{direction}):{direction}" in main
    assert "{token,row:before,direction,drop}" in main
    assert "const d=p&&p.drop" in receiver
    assert receiver.index("Object.assign(w.el.style") < receiver.index("pcWM.nativeHandoffAck(token,rect)")


def test_button_and_keyboard_native_handoff_preserve_managed_frame_geometry():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    helper = src[src.index("function nativeHandoffPlacement"):
                 src.index("async function moveToOtherMonitor")]
    assert "width=w.el.offsetWidth,height=w.el.offsetHeight" in helper
    assert "cross:Math.max(0,Math.min(1,cross))" in helper
    button = src[src.index("async function moveToOtherMonitor"):
                 src.index("async function moveWindowToMonitor")]
    keyboard = src[src.index("async function moveWindowToMonitor"):
                   src.index("function startDrag")]
    call = "pcWM.handoff(w.native,direction,nativeHandoffPlacement(w,direction))"
    assert call in button
    assert call in keyboard
    receiver = src[src.index("pcWM.onNativeHandoffPrepare(async"):
                   src.index("if(!_nativeHandoffOff", src.index("pcWM.onNativeHandoffPrepare(async"))]
    assert "if(d&&Number(d.width)>0&&Number(d.height)>0)" in receiver


def test_rejected_html_handoff_falls_back_to_the_requested_edge_snap():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    branch = drag[drag.index("if(handoff && w.native == null && pcWM.handoffFrame)"):
                  drag.index("if(zone){ _natGesture", drag.index("if(handoff && w.native == null"))]
    assert "zoneAt(dropEvent.clientX,dropEvent.clientY) : zone" in branch
    assert "if(snapZone)snapTo(w,snapZone)" in branch
    assert branch.index("if(snapZone)snapTo(w,snapZone)") < branch.index("sendFrameHandoff")


def test_rejected_native_right_handoff_commits_full_height_local_snap():
    """A right-adjacent output must not turn an unavailable transfer into a floating fallback."""
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    branch = drag[drag.index("if(handoff && w.native != null && pcWM.handoff)"):
                  drag.index("if(handoff && w.native == null && pcWM.handoffFrame)")]
    assert "zoneAt(dropEvent.clientX,dropEvent.clientY) : zone" in branch
    assert "const rejectNativeHandoff" in branch
    assert "if(snapZone)snapTo(w,snapZone)" in branch
    assert ".catch(rejectNativeHandoff)" in branch
    assert "catch(_){ rejectNativeHandoff(); }" in branch
    # snapTo is the shared left/right geometry path and therefore uses the measured desktop height.
    snap = src[src.index("function snapTo"):src.index("function unsnap")]
    assert "const css = rectOf(z)" in snap
    work = src[src.index("function snapWorkArea"):src.index("function zoneAt")]
    assert "desk.getBoundingClientRect" in work and "height:r&&r.height>0?r.height/k" in work


def test_capture_loss_at_monitor_seam_commits_local_mouse_snap_without_inventing_handoff():
    """A clamped edge is a snap gesture; only measured overflow may transfer displays."""
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    block = src[src.index("const lostCapture ="):src.index("w.el.addEventListener('lostpointercapture'", src.index("const lostCapture ="))]
    assert "up(lastMove,false)" in block
    assert "handoff=dir" not in block


def test_zero_screen_coordinates_cannot_glue_a_left_snapped_window_to_the_edge():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    assert "ev.screenX === 0 && ev.screenY === 0" in drag
    reject = drag.index("ev.screenX === 0 && ev.screenY === 0")
    proximity = drag.index("Math.abs((ev.screenX - (Number(window.screenX)||0)) - ev.clientX)")
    assert reject < proximity


def test_wayland_capture_loss_at_a_previewed_edge_commits_local_snap():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    lost = drag[drag.index("const lostCapture ="):drag.index("document.addEventListener('pointermove'")]
    assert "handoff||previewDir||edgeDirection(lastMove)" in lost
    assert "hadButtons && dir && edgeDirection(lastMove)===dir" in lost
    assert "up(lastMove,false)" in lost
    assert "handoff=dir" not in lost
    assert "else cancel(e)" in lost


def test_pointerup_recomputes_the_snap_zone_from_its_final_coordinate():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    up = drag[drag.index("const up = (endEvent, cancelled) =>"):drag.index("document.addEventListener('pointermove'")]
    assert "zone = zoneAt(endEvent.clientX,endEvent.clientY)" in up
    assert up.index("zone = zoneAt(endEvent.clientX,endEvent.clientY)") < up.index("if(zone){ _natGesture")


def test_mouse_snap_geometry_uses_the_measured_desktop_work_area():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    work = src[src.index("function snapWorkArea"):src.index("function zoneAt")]
    assert "desk.getBoundingClientRect" in work
    assert "r.height/k" in work
    assert "const work=snapWorkArea(), vw=work.width, vh=work.height" in work


def test_bottom_corner_hit_testing_uses_rendered_work_area_at_runtime():
    """A flex/scaled taskbar can make the desktop bottom differ from the nominal TASKBAR constant."""
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    block = src[src.index("function snapPointerArea"):
                src.index("function rectOf", src.index("function snapPointerArea"))]
    script = f"""
      const EDGE=26,TASKBAR=48,zf=()=>1;
      global.window={{innerWidth:1000,innerHeight:720}};
      const desk={{getBoundingClientRect:()=>({{left:0,top:0,right:1000,bottom:640,width:1000,height:640}})}};
      {block}
      process.stdout.write(JSON.stringify([
        zoneAt(998,638), zoneAt(2,638), zoneAt(998,300), zoneAt(500,2)
      ]));
    """
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == ["br", "bl", "right", "max"]


def test_free_drag_is_clamped_before_native_gesture_commits_placement():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    branch = drag[drag.index("if(zone){ _natGesture"):drag.index("const cancel =")]
    free = branch[branch.index("else {"):]
    assert free.index("keepFrameReachable(w)") < free.index("_natGesture(w,false)")


def test_taskbar_is_icon_only():
    css = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
    assert ".os-task span{display:none}" in css
    assert ".os-task .ic{width:20px;height:20px" in css


def test_native_task_buttons_have_an_existing_fallback_icon():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    assert "${appIcon(w)}" in src
    assert "(a && a.icon) || 'i-grid'" in src
    assert 'data-kind="native"' in src


def test_native_programs_are_adopted_once_into_real_posterchan_frames():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    native = src[src.index("function adoptNative(nw)"):src.index("async function adoptAll()")]
    assert "openApp(view" in native
    assert "w.native=id" in native
    assert "osw-native" in native
    adopt = src[src.index("async function adoptAll"):src.index("function closeWin(w, opts)")]
    assert "nativeTasks = rows" in adopt
    assert "adoptNative(r)" in adopt
    assert "nativeTasks=rows.filter" in adopt
    assert "pcWM.place" not in adopt


def test_maximise_is_geometry_only_and_never_recreates_the_app():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    block = src[src.index("function snapTo"):src.index("// The Snap Layouts flyout")]
    for destructive in ("renderView(", "openApp(", "closeWin(", "innerHTML"):
        assert destructive not in block, f"maximise/restore recreates app state through {destructive}"
    assert "Object.assign(w.el.style" in block


def test_native_apps_inherit_the_dark_gtk_chrome():
    for path in (
        ROOT / "os/bin/pc-shell-start",
        ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-start",
    ):
        start = path.read_text(encoding="utf-8")
        assert 'GTK_THEME="${GTK_THEME:-Adwaita:dark}"' in start
        assert "GTK_APPLICATION_PREFER_DARK_THEME=1" in start


def test_stale_native_tree_reads_cannot_recreate_a_black_frame_after_quit_or_handoff():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    adopt = src[src.index("async function adoptAll()"):
                src.index("function closeWin(w, opts)")]
    assert "const pass = ++_nativeAdoptPass" in adopt
    guard = adopt.index("if(pass !== _nativeAdoptPass) return false")
    assert guard < adopt.index("nativeTasks = rows")
    assert guard < adopt.index("adoptNative(r)")
    receiver = src[src.index("pcWM.onNativeHandoff"):
                   src.index("if(pcWM.onHandoffFrame")]
    assert receiver.index("_nativeAdoptPass++") < receiver.index("adoptNative(row)")


def test_resized_and_rejected_handoff_windows_stay_inside_the_desktop():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    assert "function keepFrameReachable(w)" in src
    resize = src[src.index("function startResize"):src.index("// ---- desktop, taskbar")]
    assert "vwL()-left-12" in resize
    assert "vhL()-TASKBAR-top-12" in resize
    send = src[src.index("function sendFrameHandoff"):src.index("/* A cross-output drag", src.index("function sendFrameHandoff"))]
    assert "keepFrameReachable(w)" in send
