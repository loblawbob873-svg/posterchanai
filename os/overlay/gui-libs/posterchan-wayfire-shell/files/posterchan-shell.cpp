/* Exact shell surfaces only: Electron's movable:false is not a Wayland capability. */
#include <wayfire/plugin.hpp>
#include <wayfire/toplevel-view.hpp>
#include <wayfire/plugins/ipc/ipc-helpers.hpp>
#include <wayfire/plugins/ipc/ipc-method-repository.hpp>
#include <wayfire/plugins/common/shared-core-data.hpp>
#include <wayland-server-core.h>
#include <map>
#include <set>
#include <limits>

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

  public:
    void init() override
    {
        methods->register_method("posterchan-shell/set-views", [this] (wf::json_t data)
        {
            return protect(data);
        });
    }
    void fini() override
    {
        methods->unregister_method("posterchan-shell/set-views");
        for (const auto& [id, actions] : saved) restore(id, actions);
        saved.clear();
    }
};
DECLARE_WAYFIRE_PLUGIN(posterchan_shell_t);
