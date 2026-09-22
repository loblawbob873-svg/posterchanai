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
/* wlroots-full.hpp includes wlr_xdg_shell.h only if the GENERATED xdg-shell-protocol.h is on the
 * include path (wlroots does not install it), and silently skips it otherwise. The window border needs
 * it to find a client-decorated window's geometry, so a build without it must fail here, loudly --
 * the ebuild generates it next to the pointer-constraints header. */
#if !__has_include(<xdg-shell-protocol.h>)
#error "xdg-shell-protocol.h was not generated (wayland-scanner server-header stable/xdg-shell/xdg-shell.xml)"
#endif
#include <wayfire/scene.hpp>
#include <wayfire/scene-render.hpp>
#include <wayfire/scene-operations.hpp>
#include <wayfire/view.hpp>
#include <wayland-server-core.h>
#include <algorithm>
#include <map>
#include <set>
#include <limits>
#include <cmath>
#include <memory>
#include <string>

/* ------------------------------------------------------------------ the frame around every window
 *
 * REPORTED AS "Firefox, foot, maybe others ... no border around window like we do to mimic hyprland's
 * border", after two rounds of tuning [decoration] that could not answer it, for two reasons:
 *
 *  * Wayfire's decoration plugin only frames a client that asks for SERVER-side decoration, and
 *    Firefox never can: there is not one occurrence of zxdg_decoration_manager_v1 in its libxul
 *    (measured, see the firefox-policies note in posterchanos-shell). GTK apps draw their own title
 *    bar and get no compositor frame at all -- no border whatever [decoration] says.
 *  * where it DOES frame a window, `active_color` paints the title bar and the border in ONE colour,
 *    so a bright accent edge is also a bright accent title bar ("firefox is now a bright cyan window
 *    title?"). Measured on foot in a headless session with the shipped wayfire.ini: 29,266 pixels of
 *    one teal (#257281) -- title and edge the same, neither reading as a PosterChan window.
 *
 * Hyprland draws its border itself, around every window, whoever draws the title. So does this: a
 * scene node in each toplevel's surface tree, a ring of `border_size` in the accent. On a window
 * Wayfire decorates it covers the decoration's own border band exactly (keep [decoration] border_size
 * equal), so the title bar keeps its quiet surface colour; on one that decorates itself it sits just
 * outside the client geometry. PosterChan's own surfaces are skipped -- the desktop and every
 * popped-out window (one app_id) draw `#pc-oswin-frame` themselves -- and so is anything fullscreen.
 *
 * Options are LOOKED UP, not wrapped, for the reason given at `CONFINE_OPTION` below: an undeclared
 * option wrapped segfaults Wayfire 0.10.1 at startup. Absent, there is simply no border. */
namespace pc_border
{
static bool is_posterchan(wayfire_view view)
{
    const auto app = view->get_app_id();
    return app == "place.poster.desktop" || app == "posterchan-desktop" || app == "PosterChan" ||
           view->get_title() == "PosterChan Desktop" ||
           view->get_title().find("PosterChan Window") != std::string::npos;
}

struct options_t
{
    std::shared_ptr<wf::config::option_t<bool>> enabled;
    std::shared_ptr<wf::config::option_t<int>> size;
    std::shared_ptr<wf::config::option_t<wf::color_t>> active, inactive;
    std::shared_ptr<wf::config::option_t<int>> radius;
    int width() const
    {
        return (enabled && enabled->get_value() && size) ? std::clamp(size->get_value(), 0, 32) : 0;
    }

    /* The corner radius, matching `.osw` in client.css — a native window has to be the same shape as
     * a PosterChan one or the desktop looks like two desktops. */
    int round() const
    {
        return radius ? std::clamp(radius->get_value(), 0, 64) : 0;
    }
};

class ring_node_t : public wf::scene::node_t
{
    std::weak_ptr<wf::toplevel_view_interface_t> view;
    const options_t& opts;

  public:
    wf::geometry_t last = {0, 0, 0, 0};

    ring_node_t(wayfire_toplevel_view v, const options_t& o) : node_t(false), view(v->weak_from_this()), opts(o) {}

    /* The ring's OUTER box, in the surface tree's coordinates.
     *
     * THAT ORIGIN IS THE MAIN SURFACE'S CORNER, NOT THE WINDOW'S. A client that decorates itself puts
     * its window geometry somewhere inside its surfaces -- GTK inside a margin of drop shadow, foot
     * ABOVE its main surface, in a title-bar subsurface. Measured with a client-decorated foot: a ring
     * placed at the surface origin started under the title bar, framing the text area and leaving
     * the bar outside. xdg-shell reports where the geometry is (`wlr_xdg_surface.geometry`), and a
     * server-decorated or XWayland window has none, so its offset is simply zero -- the space the
     * decoration node draws in, at (-left, -top). */
    wf::geometry_t outer()
    {
        auto v = view.lock();
        const int b = opts.width();
        if (!v || !b || !v->is_mapped()) return {0, 0, 0, 0};
        const auto& st = v->toplevel()->current();
        if (st.fullscreen || st.geometry.width <= 0 || st.geometry.height <= 0) return {0, 0, 0, 0};
        const auto m = st.margins;
        wf::point_t at{0, 0};
        if (auto surface = v->get_wlr_surface())
        {
            if (auto xdg = wlr_xdg_surface_try_from_wlr_surface(surface))
            {
                at = wf::point_t{xdg->geometry.x, xdg->geometry.y};
            }
        }
        wf::geometry_t box{at.x - m.left, at.y - m.top, st.geometry.width, st.geometry.height};
        if (m.left >= b && m.right >= b && m.bottom >= b && m.top >= b) return box;  // over the band
        return {box.x - b, box.y - b, box.width + 2 * b, box.height + 2 * b};
    }

    /* THE BAND, WITH ROUNDED CORNERS — "Telegram, Terminal and Firefox window borders are not curved
     * like the regular PosterChan windows".
     *
     * A PosterChan window is a `.osw` div with `border-radius:12px`; a native app is a real toplevel,
     * and this ring was a rectangle XOR a rectangle, so every native window on the desktop had square
     * corners beside rounded ones. There is no clipping available here — this node paints, it cannot
     * cut a surface — but it is added in FRONT of the surface, so painting the corner as an ARC both
     * rounds the frame and covers the app's own square corner underneath it. What is left showing
     * outside the arc is the ~1px of app content between the outer arc and the window's inset corner,
     * which at a 2px band and a 12px radius is under a pixel and a half diagonally.
     *
     * Built as an explicit union of spans rather than XOR of two rectangles: a wedge subtracted from
     * the old region would have landed partly in the hole the XOR had already made, and XOR would
     * have put it back. */
    wf::region_t ring()
    {
        auto o = outer();
        const int b = opts.width();
        if (o.width <= 2 * b || o.height <= 2 * b) return {};
        const int R = std::min({opts.round(), o.width / 2, o.height / 2});
        if (R <= b)
        {
            wf::region_t r{o};
            r ^= wf::geometry_t{o.x + b, o.y + b, o.width - 2 * b, o.height - 2 * b};
            return r;
        }
        wf::region_t r;
        // The four straight runs, between the corner arcs.
        r |= wf::geometry_t{o.x + R, o.y, o.width - 2 * R, b};
        r |= wf::geometry_t{o.x + R, o.y + o.height - b, o.width - 2 * R, b};
        r |= wf::geometry_t{o.x, o.y + R, b, o.height - 2 * R};
        r |= wf::geometry_t{o.x + o.width - b, o.y + R, b, o.height - 2 * R};
        // The arcs: one span per row, between the outer radius and the inner one.
        const double ri = R - b;
        for (int row = 0; row < R; row++)
        {
            const double dy = R - row - 0.5;
            const double outer_dx = std::sqrt(std::max(0.0, (double)R * R - dy * dy));
            const int x0 = (int)std::floor(R - outer_dx);
            const int x1 = dy < ri ? (int)std::ceil(R - std::sqrt(std::max(0.0, ri * ri - dy * dy))) : R;
            const int w = std::max(0, x1 - x0);
            if (!w) continue;
            r |= wf::geometry_t{o.x + x0, o.y + row, w, 1};                                  // top-left
            r |= wf::geometry_t{o.x + o.width - x0 - w, o.y + row, w, 1};                    // top-right
            r |= wf::geometry_t{o.x + x0, o.y + o.height - row - 1, w, 1};                   // bottom-left
            r |= wf::geometry_t{o.x + o.width - x0 - w, o.y + o.height - row - 1, w, 1};     // bottom-right
        }
        return r;
    }

    wf::geometry_t get_bounding_box() override { return outer(); }

    /* Damage where the ring WAS as well as where it is: a moved window leaves its old edge behind. */
    void refresh()
    {
        auto self = shared_from_this();
        if (last.width > 0) wf::scene::damage_node(self, last);
        last = outer();
        if (last.width > 0) wf::scene::damage_node(self, last);
    }

    void render(const wf::scene::render_instruction_t& data)
    {
        auto v = view.lock();
        if (!v || !opts.active || !opts.inactive) return;
        const auto color = v->activated ? opts.active->get_value() : opts.inactive->get_value();
        for (const auto& box : data.damage & ring())
        {
            data.pass->add_rect(color, data.target, wlr_box_from_pixman_box(box), data.damage);
        }
    }

    class instance_t : public wf::scene::render_instance_t
    {
        std::shared_ptr<ring_node_t> self;
        wf::scene::damage_callback push_damage;
        wf::signal::connection_t<wf::scene::node_damage_signal> on_damage = [=] (wf::scene::node_damage_signal *ev)
        {
            push_damage(ev->region);
        };

      public:
        instance_t(ring_node_t *node, wf::scene::damage_callback damage) :
            self(std::dynamic_pointer_cast<ring_node_t>(node->shared_from_this())), push_damage(damage)
        {
            node->connect(&on_damage);
        }

        void schedule_instructions(std::vector<wf::scene::render_instruction_t>& instructions,
            const wf::render_target_t& target, wf::region_t& damage) override
        {
            wf::region_t ours = damage & self->ring();
            if (!ours.empty())
            {
                instructions.push_back(wf::scene::render_instruction_t{
                    .instance = this, .target = target, .damage = std::move(ours)});
            }
        }

        void render(const wf::scene::render_instruction_t& data) override { self->render(data); }
    };

    void gen_render_instances(std::vector<wf::scene::render_instance_uptr>& instances,
        wf::scene::damage_callback push_damage, wf::output_t *output = nullptr) override
    {
        instances.push_back(std::make_unique<instance_t>(this, push_damage));
    }
};

/* One per framed window. Owns the node and the three signals that move or recolour it. */
struct framed_t
{
    std::shared_ptr<ring_node_t> node;
    wf::signal::connection_t<wf::view_geometry_changed_signal> on_geometry = [=] (auto) { node->refresh(); };
    wf::signal::connection_t<wf::view_activated_state_signal> on_activated = [=] (auto) { node->refresh(); };
    wf::signal::connection_t<wf::view_fullscreen_signal> on_fullscreen = [=] (auto) { node->refresh(); };
};

class manager_t
{
    options_t opts;
    std::map<wf::view_interface_t*, std::unique_ptr<framed_t>> framed;

    void detach(wf::view_interface_t *view)
    {
        auto it = framed.find(view);
        if (it == framed.end()) return;
        it->second->node->refresh();
        wf::scene::remove_child(it->second->node);
        framed.erase(it);
    }

    /* Decided again whenever the app_id changes: Proton and Electron name a window after mapping it. */
    void evaluate(wayfire_view any)
    {
        auto view = wf::toplevel_cast(any);
        const bool wanted = view && view->is_mapped() && view->role == wf::VIEW_ROLE_TOPLEVEL && !is_posterchan(any);
        if (!wanted)
        {
            detach(any.get());
            return;
        }
        if (framed.count(any.get())) return;
        auto entry = std::make_unique<framed_t>();
        entry->node = std::make_shared<ring_node_t>(view, opts);
        view->connect(&entry->on_geometry);
        view->connect(&entry->on_activated);
        view->connect(&entry->on_fullscreen);
        // FRONT, not back: the decoration node paints its whole band with the title-bar colour, and a
        // ring added behind it was measured invisible on every Wayfire-decorated window. The ring
        // never covers client content -- it is the decoration's band or outside the geometry.
        wf::scene::add_front(view->get_surface_root_node(), entry->node);
        entry->node->refresh();
        framed.emplace(any.get(), std::move(entry));
    }

    wf::signal::connection_t<wf::view_mapped_signal> on_mapped = [=] (wf::view_mapped_signal *ev) { evaluate(ev->view); };
    wf::signal::connection_t<wf::view_unmapped_signal> on_unmapped = [=] (wf::view_unmapped_signal *ev) { detach(ev->view.get()); };
    wf::signal::connection_t<wf::view_app_id_changed_signal> on_app_id = [=] (wf::view_app_id_changed_signal *ev) { evaluate(ev->view); };
    wf::signal::connection_t<wf::view_title_changed_signal> on_title = [=] (wf::view_title_changed_signal *ev) { evaluate(ev->view); };
    /* A live `wayfire/set-config-options` (or an edited ini) recolours or resizes every ring now. */
    wf::signal::connection_t<wf::reload_config_signal> on_reload = [=] (auto) { for (auto& [_, f] : framed) f->node->refresh(); };

  public:
    void init()
    {
        auto& config = wf::get_core().config;
        opts.enabled = config->get_option<bool>("posterchan-shell/window_border");
        opts.size = config->get_option<int>("posterchan-shell/window_border_size");
        opts.active = config->get_option<wf::color_t>("posterchan-shell/window_border_active_color");
        opts.inactive = config->get_option<wf::color_t>("posterchan-shell/window_border_inactive_color");
        opts.radius = config->get_option<int>("posterchan-shell/window_border_radius");
        wf::get_core().connect(&on_mapped);
        wf::get_core().connect(&on_unmapped);
        wf::get_core().connect(&on_app_id);
        wf::get_core().connect(&on_title);
        wf::get_core().connect(&on_reload);
        for (auto& view : wf::get_core().get_all_views()) evaluate(view);
    }

    void fini()
    {
        on_mapped.disconnect(); on_unmapped.disconnect(); on_app_id.disconnect();
        on_title.disconnect(); on_reload.disconnect();
        while (!framed.empty()) detach(framed.begin()->first);
    }
};
}

class posterchan_shell_t : public wf::plugin_interface_t
{
    static constexpr uint32_t owned = wf::VIEW_ALLOW_MOVE | wf::VIEW_ALLOW_RESIZE;
    std::map<uint32_t, uint32_t> saved;
    pc_border::manager_t borders;
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
        borders.init();
    }
    void fini() override
    {
        on_pointer_motion.disconnect();
        borders.fini();
        methods->unregister_method("posterchan-shell/set-cursor");
        methods->unregister_method("posterchan-shell/pointer-confinement");
        methods->unregister_method("posterchan-shell/set-views");
        for (const auto& [id, actions] : saved) restore(id, actions);
        saved.clear();
    }
};
DECLARE_WAYFIRE_PLUGIN(posterchan_shell_t);
