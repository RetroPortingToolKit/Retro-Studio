// studio_n64.cpp — the N64 Diagnostics tab. See studio_n64.hpp for why it
// exists now when it deliberately did not before, and for the division of
// labour with n64lle's own coordinators.

#include "studio/studio_n64.hpp"

#include "studio/studio_debug.hpp"
#include "studio/studio_runner.hpp"
#include "studio/studio_theme.hpp"

#include "imgui.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace retcomm::studio {

namespace {

// The data half owns the catalogue (studio_n64_data.cpp); the UI reads it
// through the accessor so there is one table, not two that can disagree about
// which gates exist.
constexpr const char* kOracleTool = "n64_oracle.py";

void row(const Theme& th, const char* label, const std::string& value) {
    ImGui::TextColored(th.text_muted, "%s", label);
    ImGui::SameLine(190.f);
    ImGui::TextUnformatted(value.empty() ? "—" : value.c_str());
}

} // namespace

// ---- actions ---------------------------------------------------------------

namespace {

void oracle_cmd(StudioModel& model, std::vector<std::string> args, bool refresh_after);

void refresh_oracle(StudioModel& model) {
    const std::string tool = n64_tool_path(kOracleTool);
    if (tool.empty()) {
        model.n64_oracle.probed = true;
        model.n64_oracle.error = "tools/n64_analysis not found beside the toolkit";
        model.n64_oracle.summary = model.n64_oracle.error;
        return;
    }
    if (model.n64_oracle_probing) return;
    model.n64_oracle_probing = true;
    std::vector<std::string> args = {"status", "--json"};
    if (const std::string root = n64lle_root_for(model); !root.empty()) {
        args.insert(args.begin(), root);
        args.insert(args.begin(), "--n64lle");
    }
    run_python_script_async(
        model, tool, args,
        [&model, tool](RunResult r) {
            model.n64_oracle_probing = false;
            model.n64_oracle = parse_n64_oracle_status(r.stdout_text);
            if (!r.ok() && model.n64_oracle.error.empty()) {
                model.n64_oracle.error = tool_error(r);
                model.n64_oracle.summary = model.n64_oracle.error;
            }
        },
        /*log_stdout=*/false, JobSlot::Global);
}

void oracle_cmd(StudioModel& model, std::vector<std::string> args, bool refresh_after) {
    const std::string tool = n64_tool_path(kOracleTool);
    if (tool.empty()) {
        model.n64_oracle_error = "tools/n64_analysis not found beside the toolkit";
        return;
    }
    if (const std::string root = n64lle_root_for(model); !root.empty()) {
        args.insert(args.begin(), root);
        args.insert(args.begin(), "--n64lle");
    }
    model.n64_oracle_busy = true;
    model.n64_oracle_error.clear();
    run_python_script_async(
        model, tool, args,
        [&model, refresh_after](RunResult r) {
            model.n64_oracle_busy = false;
            if (!r.ok()) model.n64_oracle_error = tool_error(r);
            if (refresh_after) refresh_oracle(model);
        },
        /*log_stdout=*/true, JobSlot::Global);
}

void run_gates(StudioModel& model) {
    const std::string n64lle = n64lle_root_for(model);
    if (n64lle.empty()) {
        model.n64_gate_run.error = "no n64lle checkout found for this project";
        model.n64_gate_run.ran = true;
        return;
    }
    const std::string build = model.n64_oracle.build_dir.empty()
                                  ? (fs::path(n64lle) / "build-oracle").string()
                                  : model.n64_oracle.build_dir;
    std::error_code ec;
    if (!fs::is_regular_file(fs::path(build) / "CTestTestfile.cmake", ec)) {
        model.n64_gate_run.error = "no configured build at " + build +
                                   " — run Setup on the Oracle pane first";
        model.n64_gate_run.ran = true;
        return;
    }

    // One -R alternation over the SELECTED rows. Selecting nothing runs
    // nothing rather than everything: the frame gates are minutes of work and
    // an accidental full run is a worse surprise than an empty one.
    std::string re;
    for (size_t i = 0; i < n64_gates().size() && i < model.n64_gate_sel.size(); ++i) {
        if (!model.n64_gate_sel[i]) continue;
        if (!re.empty()) re += "|";
        re += std::string("^") + n64_gates()[i].ctest_name + "$";
    }
    if (re.empty()) {
        model.n64_gate_run.error = "nothing selected";
        model.n64_gate_run.ran = true;
        return;
    }

    const std::string tool = n64_tool_path("n64_gates.py");
    if (tool.empty()) {
        model.n64_gate_run.error = "tools/n64_analysis/n64_gates.py not found "
                                   "beside the toolkit";
        model.n64_gate_run.ran = true;
        return;
    }

    model.n64_gates_running = true;
    model.n64_gate_run = N64GateRun{};
    run_python_script_async(
        model, tool, {"--build-dir", build, "-R", re, "--json"},
        [&model](RunResult r) {
            model.n64_gates_running = false;
            parse_n64_gate_json(model.n64_gate_run, r.stdout_text);
            // Exit 2 means the tool could not run ctest at all; its own message
            // is better than anything inferred from an empty result set.
            if (r.exit_code == 2 && model.n64_gate_run.error.empty())
                model.n64_gate_run.error = tool_error(r);
        },
        /*log_stdout=*/false, JobSlot::Global);
}

} // namespace

// ---- panes -----------------------------------------------------------------

namespace {

void pane_oracle(StudioModel& model, const Theme& th) {
    const auto& o = model.n64_oracle;
    if (!o.probed && !model.n64_oracle_probing) refresh_oracle(model);

    ImGui::SeparatorText("n64ref — the Ares reference oracle");
    ImGui::TextWrapped(
        "First-party and BUILT, not downloaded: n64lle's own frontend around a "
        "pinned Ares core, which also carries paraLLEl-RDP — the only thing in "
        "this stack that produces reference pixels. Ares is ISC; paraLLEl-RDP "
        "is MIT. Both are developer tools and neither is linked into a port.");
    ImGui::Spacing();

    row(th, "n64lle checkout", o.n64lle);
    row(th, "binary", o.binary);
    row(th, "Ares pinned", o.ares_pinned);
    row(th, "Ares present", o.ares_present);

    ImGui::TextColored(th.text_muted, "pin");
    ImGui::SameLine(190.f);
    if (!o.probed)        ImGui::TextUnformatted("—");
    else if (o.pin_ok)    ImGui::TextColored(th.good, "matches ORACLE-PIN.md");
    else if (o.installed) ImGui::TextColored(th.bad, "OFF-PIN — do not grade with this");
    else                  ImGui::TextColored(th.text_muted, "not initialised");

    if (!o.patches.empty()) {
        std::string list;
        for (const auto& p : o.patches) { if (!list.empty()) list += ", "; list += p; }
        row(th, "patches applied", list);
    }
    row(th, "running", o.running_pid
                           ? ("pid " + std::to_string(o.running_pid) + " on port " +
                              std::to_string(o.running_port))
                           : std::string("no"));

    ImGui::Spacing();
    const bool busy = model.n64_oracle_busy || model.n64_oracle_probing;
    ImGui::BeginDisabled(busy);
    if (ImGui::Button("Doctor")) oracle_cmd(model, {"doctor"}, true);
    ImGui::SameLine();
    if (ImGui::Button("Setup")) oracle_cmd(model, {"setup"}, true);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Initialise the Ares submodule at the pinned SHA, build the "
                          "narrowed Ares core, then build n64ref. Minutes, once.");
    ImGui::SameLine();
    if (ImGui::Button("Refresh")) refresh_oracle(model);
    ImGui::EndDisabled();

    ImGui::Spacing();
    // The selected port's own dump first -- the one its game package was
    // harvested from -- then whatever the n64lle checkout stages.
    std::string rom = model.n64_rom_override[0] ? std::string(model.n64_rom_override)
                                                : n64_rom_for(model.selected_root());
    if (rom.empty()) rom = n64_rom_for(n64lle_root_for(model));
    ImGui::SetNextItemWidth(520.f);
    ImGui::InputTextWithHint("##n64rom", rom.empty() ? "no ROM staged in roms/" : rom.c_str(),
                             model.n64_rom_override, sizeof(model.n64_rom_override));
    ImGui::SameLine();
    ImGui::BeginDisabled(busy || !o.installed || rom.empty() || o.running_pid != 0);
    if (ImGui::Button("Start oracle")) oracle_cmd(model, {"start", "--rom", rom}, true);
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered() && o.running_pid == 0 && rom.empty())
        ImGui::SetTooltip("n64ref boots a ROM; stage one in the project's roms/ or "
                          "give a path here.");
    ImGui::SameLine();
    ImGui::BeginDisabled(busy || o.running_pid == 0);
    if (ImGui::Button("Stop oracle")) oracle_cmd(model, {"stop"}, true);
    ImGui::EndDisabled();

    ImGui::TextColored(th.text_muted,
                       "Port 0 — the OS assigns a free one and n64ref announces it. "
                       "Fixed localhost ports collide with sibling projects' debug "
                       "servers on this machine (n64lle KI-3), so none is hard-coded.");

    if (!model.n64_oracle_error.empty())
        ImGui::TextColored(th.bad, "%s", model.n64_oracle_error.c_str());
    if (!o.error.empty()) ImGui::TextColored(th.bad, "%s", o.error.c_str());
}

void pane_gates(StudioModel& model, const Theme& th) {
    ImGui::SeparatorText("Differential gates");
    ImGui::TextWrapped(
        "Every comparison belongs to n64lle's own coordinators; this runs them "
        "through ctest and reports what they said. A SKIP is shown as a skip and "
        "never folded into a pass — n64lle's gates skip when an anchor ROM is "
        "absent, and \"green because it did not run\" is the one reading this "
        "pane exists to prevent.");
    ImGui::Spacing();

    if (model.n64_gate_sel.size() != n64_gates().size())
        model.n64_gate_sel.assign(n64_gates().size(), 0);

    const bool oracle_ready = model.n64_oracle.installed && model.n64_oracle.pin_ok;
    if (!oracle_ready)
        ImGui::TextColored(th.warn,
                           "Oracle not built or off-pin — the differential rows are "
                           "unavailable until the Oracle pane is green.");

    if (ImGui::BeginTable("n64gates", 4,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("", ImGuiTableColumnFlags_WidthFixed, 28.f);
        ImGui::TableSetupColumn("Gate", ImGuiTableColumnFlags_WidthFixed, 230.f);
        ImGui::TableSetupColumn("Result", ImGuiTableColumnFlags_WidthFixed, 130.f);
        ImGui::TableSetupColumn("What a pass proves");
        ImGui::TableHeadersRow();

        for (size_t i = 0; i < n64_gates().size(); ++i) {
            const N64Gate& g = n64_gates()[i];
            const bool blocked = g.needs_oracle && !oracle_ready;
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::PushID((int)i);
            ImGui::BeginDisabled(blocked || model.n64_gates_running);
            bool sel = model.n64_gate_sel[i] != 0;
            if (ImGui::Checkbox("##sel", &sel)) model.n64_gate_sel[i] = sel ? 1 : 0;
            ImGui::EndDisabled();
            ImGui::PopID();

            ImGui::TableNextColumn();
            if (blocked) ImGui::TextColored(th.text_muted, "%s", g.label);
            else         ImGui::TextUnformatted(g.label);

            ImGui::TableNextColumn();
            const N64GateResult* res = nullptr;
            for (const auto& r : model.n64_gate_run.results)
                if (r.name == g.ctest_name) { res = &r; break; }
            if (!res)                          ImGui::TextColored(th.text_muted, "—");
            else if (res->status == "Passed")  ImGui::TextColored(th.good, "Passed  %.1fs", res->seconds);
            else if (res->status == "Skipped") ImGui::TextColored(th.warn, "Skipped");
            else                               ImGui::TextColored(th.bad, "%s", res->status.c_str());

            ImGui::TableNextColumn();
            ImGui::TextWrapped("%s", g.what);
        }
        ImGui::EndTable();
    }

    ImGui::Spacing();
    ImGui::BeginDisabled(model.n64_gates_running);
    if (ImGui::Button("Run selected")) run_gates(model);
    ImGui::SameLine();
    if (ImGui::Button("Select all available"))
        for (size_t i = 0; i < n64_gates().size(); ++i)
            model.n64_gate_sel[i] = (!n64_gates()[i].needs_oracle || oracle_ready) ? 1 : 0;
    ImGui::SameLine();
    if (ImGui::Button("Clear")) std::fill(model.n64_gate_sel.begin(), model.n64_gate_sel.end(), 0);
    ImGui::EndDisabled();

    if (model.n64_gates_running) {
        ImGui::SameLine();
        ImGui::TextColored(th.warn, "running…");
    }
    if (model.n64_gate_run.ran && !model.n64_gate_run.summary.empty()) {
        ImGui::Spacing();
        const bool bad = model.n64_gate_run.failed > 0;
        ImGui::TextColored(bad ? th.bad : th.good, "%s", model.n64_gate_run.summary.c_str());
    }
    if (!model.n64_gate_run.error.empty())
        ImGui::TextColored(th.bad, "%s", model.n64_gate_run.error.c_str());
}

void pane_rings(StudioModel& model, const Theme& th) {
    ImGui::SeparatorText("Always-on rings");
    ImGui::TextWrapped(
        "n64lle's observability model is always-on rings queried after the fact "
        "— never arm-then-capture, and never pausing two observers to line them "
        "up. The runtime and n64ref speak the SAME id-framed JSON protocol, so "
        "this connects to either by port.");
    ImGui::Spacing();

    DebugClient& c = shared_debug_client();
    const auto snap = c.snapshot();

    ImGui::SetNextItemWidth(160.f);
    ImGui::InputText("host", model.n64_host, sizeof(model.n64_host));
    ImGui::SameLine();
    ImGui::SetNextItemWidth(100.f);
    ImGui::InputInt("port", &model.n64_port);
    ImGui::SameLine();
    if (model.n64_oracle.running_port &&
        ImGui::Button("Use oracle port")) {
        model.n64_port = model.n64_oracle.running_port;
    }

    ImGui::SameLine();
    if (!c.running()) {
        if (ImGui::Button("Connect")) c.start(model.n64_host, model.n64_port);
    } else {
        if (ImGui::Button("Disconnect")) c.stop();
    }

    const char* state = "disconnected";
    ImVec4 col = th.text_muted;
    switch (snap.state) {
        case DebugClient::State::Connected:  state = "connected";  col = th.good;   break;
        case DebugClient::State::Connecting: state = "connecting"; col = th.warn; break;
        case DebugClient::State::Failed:     state = "failed";     col = th.bad;  break;
        default: break;
    }
    ImGui::TextColored(col, "%s", state);
    if (!snap.error.empty()) {
        ImGui::SameLine();
        ImGui::TextColored(th.text_muted, "— %s", snap.error.c_str());
    }

    ImGui::Spacing();
    ImGui::BeginDisabled(!c.running());
    if (ImGui::Button("ring_stats")) c.request(R"("cmd":"ring_stats")", "n64_rings");
    ImGui::SameLine();
    if (ImGui::Button("help")) c.request(R"("cmd":"help")", "n64_rings");
    ImGui::SameLine();
    if (ImGui::Button("status")) c.request(R"("cmd":"status")", "n64_rings");
    ImGui::EndDisabled();
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted,
                       "ring_query takes a ring name; run help to see which this "
                       "build carries.");

    std::string reply;
    if (c.take_reply("n64_rings", reply)) model.n64_ring_reply = std::move(reply);
    if (!model.n64_ring_reply.empty()) {
        ImGui::Spacing();
        ImGui::InputTextMultiline("##n64ringreply", model.n64_ring_reply.data(),
                                  model.n64_ring_reply.size() + 1,
                                  ImVec2(-1, 260.f), ImGuiInputTextFlags_ReadOnly);
    }
}

} // namespace

// ---- the tab ---------------------------------------------------------------

void draw_n64(StudioModel& model, const Theme& th, SDL_Window* window) {
    (void)window;
    if (ImGui::BeginTabBar("n64diag")) {
        if (ImGui::BeginTabItem("Oracle")) { pane_oracle(model, th); ImGui::EndTabItem(); }
        if (ImGui::BeginTabItem("Gates"))  { pane_gates(model, th);  ImGui::EndTabItem(); }
        if (ImGui::BeginTabItem("Rings"))  { pane_rings(model, th);  ImGui::EndTabItem(); }
        ImGui::EndTabBar();
    }
}

} // namespace retcomm::studio
