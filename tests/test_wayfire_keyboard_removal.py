"""Execute the upstream removal callback before/after the packaged Wayfire patch.

The physical regression killed two Electron starts in xkb_state_update_mask after
a secondary input device disappeared. Wayfire has two keyboard selections: its
own current_keyboard and wlroots' seat keyboard. Adding a device can change only
the latter. Model that distinction, including wlroots clearing a destroyed device.
The callback itself comes from the unmodified MIT-licensed upstream source fixture.
"""
from pathlib import Path
import hashlib
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests/fixtures/wayfire-0.10.1-seat.cpp"
PATCH = ROOT / "os/overlay/gui-wm/wayfire/files/wayfire-0.10.1-preserve-keyboard.patch"


def test_fixture_is_the_exact_upstream_release_source():
    # src/core/seat/seat.cpp from the checksummed wayfire-0.10.1.tar.xz release.
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == (
        "b506d0a563cd52aef61f54041bc1155c9e5eef9159ca8063dfc7aee77d1e5927"
    )

HARNESS = r'''
#include <algorithm>
#include <functional>
#include <memory>
#include <string>
#include <vector>
constexpr int WLR_INPUT_DEVICE_KEYBOARD = 1;
struct Device { int type = WLR_INPUT_DEVICE_KEYBOARD; int id; };
Device* wlroots_keyboard = nullptr;
namespace wf {
struct keyboard_t {
    Device* device;
    explicit keyboard_t(Device* d) : device(d) {}
    ~keyboard_t() { if (wlroots_keyboard == device) wlroots_keyboard = nullptr; }
};
struct input_device_t {
    Device* value;
    Device* get_wlr_handle() { return value; }
};
struct input_device_removed_signal { input_device_t* device; };
}
struct Seat {
    std::vector<std::unique_ptr<wf::keyboard_t>> keyboards;
    wf::keyboard_t* current_keyboard = nullptr;
    std::function<void(wf::input_device_removed_signal*)> on_remove_device;
    int capability_updates = 0;
    void set_keyboard(wf::keyboard_t* keyboard) {
        current_keyboard = keyboard;
        wlroots_keyboard = keyboard ? keyboard->device : nullptr;
    }
    void update_capabilities() { ++capability_updates; }
};
int main(int argc, char** argv) {
    Seat seat;
    auto* priv = &seat;
    Device first{1, 1}, second{1, 2}, absent{1, 3}, pointer{2, 4};
    seat.keyboards.emplace_back(std::make_unique<wf::keyboard_t>(&first));
    seat.set_keyboard(seat.keyboards.front().get());
    const std::string scenario = argv[1];
    if (scenario != "last") {
        seat.keyboards.emplace_back(std::make_unique<wf::keyboard_t>(&second));
        // keyboard_t's upstream constructor updates wlroots, but does not change
        // the private current keyboard until this new device produces a key.
        wlroots_keyboard = &second;
    }
    Device* removed = &second;
    Device* expected = &first;
    if (scenario == "current") { removed = &first; expected = &second; }
    if (scenario == "last") { removed = &first; expected = nullptr; }
    if (scenario == "no_selection") { seat.set_keyboard(nullptr); }
    if (scenario == "absent") { removed = &absent; }
    if (scenario == "pointer") { removed = &pointer; expected = &second; }
    /* CALLBACK */
    wf::input_device_t wrapper{removed};
    wf::input_device_removed_signal event{&wrapper};
    seat.on_remove_device(&event);
    if (seat.capability_updates != 1) return 20;
    if (wlroots_keyboard != expected) return 21;
    auto* private_device = seat.current_keyboard ? seat.current_keyboard->device : nullptr;
    if (private_device != (scenario == "pointer" ? &first : expected)) return 22;
    for (const auto& keyboard : seat.keyboards) {
        if (keyboard->device == removed) return 23;
    }
    return 0;
}
'''

CAPABILITIES_HARNESS = r'''
#include <cstdlib>
#include <cstdint>
#include <vector>
constexpr int WLR_INPUT_DEVICE_KEYBOARD = 1, WLR_INPUT_DEVICE_POINTER = 2,
              WLR_INPUT_DEVICE_TOUCH = 3;
constexpr uint32_t WL_SEAT_CAPABILITY_KEYBOARD = 2, WL_SEAT_CAPABILITY_POINTER = 1,
                   WL_SEAT_CAPABILITY_TOUCH = 4;
struct Device { int type; Device* get_wlr_handle() { return this; } };
struct Core {
    std::vector<Device*> devices;
    const auto& get_input_devices() { return devices; }
};
Core core;
namespace wf { Core& get_core() { return core; } }
uint32_t advertised = 0;
void wlr_seat_set_capabilities(int, uint32_t caps) { advertised = caps; }
struct Seat {
    std::vector<int> keyboards;
    int seat = 0;
    void update_capabilities() /* CAPABILITIES */
};
int main(int argc, char** argv) {
    const int survivors = std::atoi(argv[1]);
    const bool pointer = std::atoi(argv[2]);
    const bool touch = std::atoi(argv[3]);
    Device removed_keyboard{WLR_INPUT_DEVICE_KEYBOARD};
    Device mouse{WLR_INPUT_DEVICE_POINTER}, touchscreen{WLR_INPUT_DEVICE_TOUCH};
    // input_manager emits removal before erasing this stale device from its list.
    core.devices.push_back(&removed_keyboard);
    if (pointer) core.devices.push_back(&mouse);
    if (touch) core.devices.push_back(&touchscreen);
    Seat seat;
    seat.keyboards.resize(survivors);
    seat.update_capabilities();
    const auto expected = (survivors ? WL_SEAT_CAPABILITY_KEYBOARD : 0)
        | (pointer ? WL_SEAT_CAPABILITY_POINTER : 0)
        | (touch ? WL_SEAT_CAPABILITY_TOUCH : 0);
    return advertised == expected ? 0 : 31;
}
'''


def callback(source):
    start = source.index("priv->on_remove_device = [&]")
    end = source.index("\n    };", start) + len("\n    };")
    return source[start:end]


@pytest.fixture(scope="module")
def executables(tmp_path_factory):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("g++ required for actual Wayfire callback regression")
    directory = tmp_path_factory.mktemp("wayfire-keyboard")
    target = directory / "src/core/seat/seat.cpp"
    target.parent.mkdir(parents=True)
    target.write_bytes(SOURCE.read_bytes())
    builds = {}
    for name in ("original", "patched"):
        if name == "patched":
            subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(PATCH)],
                           cwd=directory, check=True, capture_output=True)
        code = directory / (name + ".cpp")
        code.write_text(HARNESS.replace("/* CALLBACK */", callback(target.read_text())))
        executable = directory / name
        subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", str(code), "-o", str(executable)],
                       check=True, capture_output=True)
        builds[name] = executable
        source = target.read_text()
        start = source.index("void wf::seat_t::impl::update_capabilities()")
        start = source.index("{", start)
        end = source.index("\n}", start) + 2
        capabilities = directory / (name + "-capabilities.cpp")
        capabilities.write_text(CAPABILITIES_HARNESS.replace("/* CAPABILITIES */", source[start:end]))
        executable = directory / (name + "-capabilities")
        subprocess.run([compiler, "-std=c++17", str(capabilities), "-o", str(executable)],
                       check=True, capture_output=True)
        builds[name + "-capabilities"] = executable
    return builds


@pytest.mark.parametrize("scenario", ["secondary", "current", "last", "no_selection", "absent", "pointer"])
def test_packaged_callback_preserves_a_valid_surviving_keyboard(executables, scenario):
    result = subprocess.run([str(executables["patched"]), scenario], capture_output=True)
    assert result.returncode == 0, (scenario, result.returncode, result.stderr)


@pytest.mark.parametrize("scenario", ["secondary", "no_selection", "absent"])
def test_original_callback_reproduces_loss_of_keyboard_state(executables, scenario):
    result = subprocess.run([str(executables["original"]), scenario], capture_output=True)
    assert result.returncode == 21, (scenario, result.returncode)


@pytest.mark.parametrize("survivors,pointer,touch", [(0,0,0),(0,1,0),(0,0,1),(0,1,1),(1,0,0),(2,1,1)])
def test_capabilities_reflect_surviving_keyboards_without_losing_pointer_or_touch(executables, survivors, pointer, touch):
    result = subprocess.run([str(executables["patched-capabilities"]), str(survivors), str(pointer), str(touch)])
    assert result.returncode == 0


def test_original_advertises_the_last_keyboard_after_it_is_removed(executables):
    result = subprocess.run([str(executables["original-capabilities"]), "0", "1", "1"])
    assert result.returncode == 31
