/* Exact shell surfaces only: Electron's movable:false is not a Wayland capability. */
#include <wayfire/plugin.hpp>
#include <wayfire/toplevel-view.hpp>
#include <wayfire/plugins/ipc/ipc-helpers.hpp>
#include <wayfire/plugins/ipc/ipc-method-repository.hpp>
#include <wayfire/plugins/common/shared-core-data.hpp>
#include <wayfire/core.hpp>
#include <wayfire/seat.hpp>
#include <wayfire/output.hpp>
#include <wayfire/output-layout.hpp>
#include <wayfire/config/config-manager.hpp>
#include <wayfire/config/option.hpp>
#include <wayfire/signal-definitions.hpp>
#include <wayfire/nonstd/wlroots-full.hpp>
#include <wayland-server-core.h>
#include <algorithm>
#include <map>
#include <set>
#include <limits>
#include <cmath>

class posterchan_shell_t : public wf::plugin_interface_t
{
    static constexpr uint32_t owned = wf::VIEW_ALLOW_MOVE | wf::VIEW_ALLOW_RESIZE;
    std::map<uint32_t, uint32_t> saved;
    wf::shared_data::ref_ptr_t<wf::ipc::method_repository_t> methods;

    void restore(uint32_t id, uint32_t previous)
    {
        auto view = wf::toplevel_cast(wf::ipc::find_view_by_id(id));
        if (view)
            view->set_allowed_actions((view->get_allowed_actions() & ~owned) | previous);
    }

    wf::json_t protect(wf::json_t data)
    {
        const auto pid = wf::ipc::json_get_uint64(data, "pid");
        if (!pid || pid > uint64_t(std::numeric_limits<pid_t>::max()) ||
            !data.has_member("ids") || !data["ids"].is_array() || data["ids"].size() > 64)
            return wf::ipc::json_error("Invalid shell registration");
        std::set<uint32_t> requested;
        // Validate the whole request before changing any capabilities. Never match an app-id
        // alone: ordinary PosterChan application windows use the same one as the desktop.
        for (size_t i = 0; i < data["ids"].size(); ++i)
        {
            auto value = data["ids"][i];
            if (!value.is_uint64() || !value.as_uint64() ||
                value.as_uint64() > std::numeric_limits<uint32_t>::max())
                return wf::ipc::json_error("Invalid shell view id");
            auto id = uint32_t(value.as_uint64());
            auto view = wf::toplevel_cast(wf::ipc::find_view_by_id(id));
            pid_t owner = -1;
            if (view && view->get_client())
                wl_client_get_credentials(view->get_client(), &owner, nullptr, nullptr);
            if (!view || !view->is_mapped() || uint64_t(owner) != pid ||
                view->get_title() != "PosterChan Desktop")
                return wf::ipc::json_error("View is not a desktop surface owned by this process");
            requested.insert(id);
        }
        for (auto it = saved.begin(); it != saved.end();)
        {
            if (!requested.count(it->first))
            {
                restore(it->first, it->second);
                it = saved.erase(it);
            } else ++it;
        }
        for (auto id : requested)
        {
            auto view = wf::toplevel_cast(wf::ipc::find_view_by_id(id));
            auto actions = view->get_allowed_actions();
            saved.emplace(id, actions & owned);
            view->set_allowed_actions(actions & ~owned);
        }
        return wf::ipc::json_ok();
    }

    /* ------------------------------------------------- keeping the pointer inside a fullscreen view
     *
     * REPORTED AS "the cursor is leaving the monitor on a full screen game", AND NOTHING ON THIS
     * COMPOSITOR COULD ANSWER IT AUTOMATICALLY. Fullscreen is not confinement on any wlroots
     * compositor -- the one protocol mechanism is pointer-constraints-v1 and only the CLIENT can
     * ask for it, so a game with mouse-look IS held and the same game sitting in its own menu, or
     * running borderless, is not. Measured on this machine (wayfire 0.10.1-r1,
     * wayfire-plugins-extra 0.10.0): `grep -l constrain /usr/share/wayfire/metadata/*.xml` returns
     * force-fullscreen and nothing else, and `list-methods` over the running IPC returns 45 methods
     * of which NONE touch the pointer. So the shell genuinely could not arm the one existing
     * mechanism -- it is a key binding, Super+Alt+F, and nothing more.
     *
     * AND THAT MECHANISM IS NOT ENOUGH EVEN WHEN A PERSON PRESSES IT. force-fullscreen clamps only
     * while the view it fullscreened is the ACTIVE view for the output: it connects and DISCONNECTS
     * its motion hook from a focus signal. Measured with a second (headless) output and a real
     * uinput pointer: with the terminal focused the cursor stopped dead at x=1920, and after focus
     * moved to a transient popup the same drag walked to x=3200 on the neighbouring output and
     * stayed escapable until the view was focused again. A game that pops up an overlay, a launcher
     * or a notification therefore loses the confinement silently. It also re-scales the view through
     * a 2D transformer, which is not something to do to a game that is already at native fullscreen.
     *
     * So the rule here is re-derived from scratch on every motion event rather than latched:
     * the seat's active output, its active view, is that view fullscreen. Nothing to connect, and
     * nothing that can be left connected to a stale answer.
     *
     * TWO THINGS IT DELIBERATELY REFUSES TO DO.
     *
     * (1) It never touches the deltas while the focused surface holds a pointer-constraints-v1
     * lock or confinement. The relative-pointer motion a game's mouse-look reads is sent from
     * `wlr-surface-pointer-interaction`'s own handler on the SAME signal, out of the same
     * `delta_x`/`unaccel_dx` fields -- so clamping them at the edge of an output would put an
     * invisible wall in mouse-look, in the one case where the pointer was already held correctly
     * and this feature has nothing to add. Measured with a real locking client (a page calling
     * requestPointerLock in fullscreen Firefox): `client-constraint` goes false -> true on the
     * click and back to false on Escape, so the guard engages on exactly the grab it is about.
     *
     * Without a native constraint we confine accelerated cursor displacement only. Raw
     * (unaccelerated) relative motion stays unchanged for XWayland raw-input consumers.
     * An unconstrained client using accelerated relative motion still sees clipped deltas;
     * this is not a guarantee for every game's input mode.
     *
     * (2) It confines, it never captures: a pointer that is not already on that output is left
     * alone rather than yanked onto it.
     *
     * The clamp is to `width - 1`, not `width`. The output layout hands x == box.x + box.width to
     * the NEXT output, so clamping there parks the cursor on the neighbour's first pixel column --
     * which moves the seat's active output, which releases the confinement the same event applied.
     * (`wlr_box_closest_point`, which force-fullscreen uses, clamps to the inclusive edge and has
     * exactly that hole; the measured resting position was x=1920 on a 1920-wide output.)
     *
     * ONLY RELATIVE POINTERS ARE CONFINED, and that is a stated limit rather than an oversight.
     * An absolute device (a VM tablet, a drawing tablet, a touchscreen) delivers
     * wlr_pointer_motion_absolute with normalised coordinates whose mapping back through
     * `wlr_cursor_absolute_to_layout_coords` has no inverse available to a plugin. Every mouse and
     * every laptop touchpad is relative; force-fullscreen covers exactly the same set.
     *
     * ON BY DEFAULT (`confine_pointer_to_fullscreen`, settable live over
     * `wayfire/set-config-options`, which is what /usr/local/bin/pc-pointer-confine and System
     * Settings -> Displays use). It shipped off, on the argument that a pointer which cannot leave
     * a monitor is a trap when it fires on the wrong window -- and the answer to that was "no other
     * OS makes you fucking toggle the cursor guard". The trap is hypothetical and bounded: nothing
     * happens on a single monitor, nothing happens outside a fullscreen window, a client's own
     * pointer grab is left alone, and one command turns it off. The cursor walking out of a game
     * onto the second screen is what actually happens. The default lives in the .xml beside this
     * file -- never here, see below. */
    /* LOOKED UP, NOT WRAPPED, AND A MISSING OPTION IS SIMPLY "OFF".
     *
     * `wf::option_wrapper_t` throws when the option is not declared, and the throw reaches
     * `option_wrapper_debug_message` -- which on Wayfire 0.10.1 SEGFAULTS the compositor during
     * startup. Measured exactly that way by running this plugin against a metadata directory
     * without its XML: "No such option ... Fatal error: Segmentation fault" and no desktop at all.
     * The .so and the .xml ship in one package so they should never separate, but "should never"
     * is not a reason to let a stale or hand-copied metadata file cost somebody their machine with
     * only a text console as the way back.
     *
     * ABSENT, THE OPTION STILL RESOLVES TO FALSE -- deliberately, and NOT to the .xml's new `true`.
     * A missing declaration does not mean "the operator wants the default", it means this .so and
     * its metadata have come apart, and the safe reading of a broken install is the behaviour that
     * shipped before the feature existed. The default that people actually get is the one declared
     * in posterchan-shell.xml, which ships in this same package. */
    static constexpr const char *CONFINE_OPTION = "posterchan-shell/confine_pointer_to_fullscreen";
    std::shared_ptr<wf::config::option_t<bool>> confine_option;
    bool confine()
    {
        return confine_option && confine_option->get_value();
    }

    /* The output whose fullscreen view owns the pointer right now, or nullptr for "nobody does". */
    wf::output_t *confining_output()
    {
        if (!confine()) return nullptr;
        // One monitor cannot be left, so there is nothing to hold the pointer on.
        if (wf::get_core().output_layout->get_outputs().size() < 2) return nullptr;
        auto output = wf::get_core().seat->get_active_output();
        if (!output) return nullptr;
        auto view = wf::toplevel_cast(wf::get_active_view_for_output(output));
        // A DIALOG OF A FULLSCREEN GAME IS STILL THAT GAME. A launcher, a settings window or an
        // overlay opened by the game is its own toplevel and is not itself fullscreen, so keying on
        // the focused view alone released the pointer the moment one appeared -- which is the same
        // complaint ("the cursor left the game") with an extra window on screen. Walk the toplevel
        // parent chain, bounded, so a malformed or cyclic chain cannot spin here.
        for (int depth = 0; view && (depth < 8); ++depth, view = view->parent)
        {
            if (!view->is_mapped()) return nullptr;
            // Never the desktop itself: it is full-output on every monitor, and confining the
            // pointer to one of them would make the desktop the thing it cannot leave.
            if (view->role == wf::VIEW_ROLE_DESKTOP_ENVIRONMENT) return nullptr;
            if (view->get_title() == "PosterChan Desktop") return nullptr;
            if (view->get_output() != output) return nullptr;
            if (view->toplevel()->current().fullscreen) return output;
        }
        return nullptr;
    }

    /* A client holding pointer-constraints-v1 already owns the pointer. See (1) above. */
    bool client_holds_the_pointer()
    {
        auto seat = wf::get_core().get_current_seat();
        auto surface = seat ? seat->pointer_state.focused_surface : nullptr;
        if (!surface) return false;
        return wlr_pointer_constraints_v1_constraint_for_surface(
            wf::get_core().protocols.pointer_constraints, surface, seat) != nullptr;
    }

    wf::signal::connection_t<wf::input_event_signal<wlr_pointer_motion_event>> on_pointer_motion =
        [=] (wf::input_event_signal<wlr_pointer_motion_event> *ev)
    {
        auto output = confining_output();
        if (!output || client_holds_the_pointer()) return;
        auto box = output->get_layout_geometry();
        if ((box.width < 2) || (box.height < 2)) return;
        auto at = wf::get_core().get_cursor_position();
        if (!(box & at)) return;   // confine, never capture
        double x = at.x + ev->event->delta_x;
        double y = at.y + ev->event->delta_y;
        double cx = std::clamp(x, double(box.x), double(box.x + box.width - 1));
        double cy = std::clamp(y, double(box.y), double(box.y + box.height - 1));
        if ((cx == x) && (cy == y)) return;
        // Only cursor displacement is confined. Wayfire forwards these same events to
        // relative-pointer clients; XWayland uses the unaccelerated fields for raw input.
        // Rewriting them makes mouse-look stop at screen edges, and also corrupts the
        // untouched axis when acceleration differs. A native lock still bypasses both.
        ev->event->delta_x = cx - at.x;
        ev->event->delta_y = cy - at.y;
    };

    /* Read-only: the setting is written through wayfire/set-config-options, so there is one writer
     * and this cannot disagree with it. `confined` is the answer the next motion event would get. */
    wf::json_t pointer_confinement()
    {
        wf::json_t out = wf::ipc::json_ok();
        auto output = confining_output();
        out["enabled"] = confine();
        out["confined"] = (output != nullptr);
        out["client-constraint"] = client_holds_the_pointer();
        out["outputs"] = (uint64_t)wf::get_core().output_layout->get_outputs().size();
        if (output)
        {
            out["output"] = std::string(output->handle->name ?: "");
            out["box"] = wf::ipc::geometry_to_json(output->get_layout_geometry());
        }
        return out;
    }

  public:
    void init() override
    {
        confine_option = wf::get_core().config->get_option<bool>(CONFINE_OPTION);
        methods->register_method("posterchan-shell/set-cursor", [] (wf::json_t data)
        {
            if (!data.has_member("x") || !data.has_member("y") ||
                !(data["x"].is_double() || data["x"].is_int64() || data["x"].is_uint64()) ||
                !(data["y"].is_double() || data["y"].is_int64() || data["y"].is_uint64()))
                return wf::ipc::json_error("Invalid cursor position");
            const double x = data["x"].as_double(), y = data["y"].as_double();
            if (!std::isfinite(x) || !std::isfinite(y) || std::abs(x) > 100000 || std::abs(y) > 100000)
                return wf::ipc::json_error("Invalid cursor position");
            wf::get_core().warp_cursor({x, y});
            const auto at = wf::get_core().get_cursor_position();
            auto out = wf::ipc::json_ok();
            out["x"] = at.x; out["y"] = at.y;
            return out;
        });
        methods->register_method("posterchan-shell/set-views", [this] (wf::json_t data)
        {
            return protect(data);
        });
        methods->register_method("posterchan-shell/pointer-confinement", [this] (wf::json_t)
        {
            return pointer_confinement();
        });
        wf::get_core().connect(&on_pointer_motion);
    }
    void fini() override
    {
        on_pointer_motion.disconnect();
        methods->unregister_method("posterchan-shell/set-cursor");
        methods->unregister_method("posterchan-shell/pointer-confinement");
        methods->unregister_method("posterchan-shell/set-views");
        for (const auto& [id, actions] : saved) restore(id, actions);
        saved.clear();
    }
};
DECLARE_WAYFIRE_PLUGIN(posterchan_shell_t);
