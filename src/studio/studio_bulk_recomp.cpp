// studio_bulk_recomp.cpp — the Bulk Recomp tab. See studio_bulk_recomp.hpp for
// the division of labour with project_studio/bulkrecomp.py.

#include "studio/studio_bulk_recomp.hpp"

#include "studio/studio_runner.hpp"
#include "studio/studio_theme.hpp"

#include "imgui.h"

#include <SDL3/SDL.h>
#include <SDL3/SDL_dialog.h>

#include <nlohmann/json.hpp>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace retcomm::studio {

namespace {

// Must match bulkrecomp.py.
constexpr const char* kLogDirName = "Log Output";
constexpr const char* kStatusFile = "bulk_status.json";
constexpr const char* kSummaryFile = "summary.txt";
constexpr const char* kCancelFile = ".cancel";
constexpr const char* kListFile = "images.txt";

struct ItemView {
    int index = 0;
    std::string image;
    std::string label;
    std::string name;
    std::string root;
    std::string state = "queued";
    std::string stage;
    int stage_index = -1;
    float progress = 0.f;
    std::string message;
    std::string log;
    std::string log_dir;
    std::vector<std::pair<std::string, std::string>> stages;
    double started = 0.0;
    double finished = 0.0;
};

struct BulkState {
    // Filled by the SDL dialog callbacks, which may run on another thread.
    std::mutex pick_mu;
    std::vector<std::string> picked_files;
    std::string picked_folder;
    bool pick_busy = false;

    Platform list_platform = Platform::None;
    std::vector<std::string> images;
    char out_dir[1024] = {};
    int parallel = 1;
    int build_jobs = 0;  // 0 = auto
    int build_type = 0;
    bool create_github = false;
    bool ci = false;
    bool boxart = false;
    bool reuse_existing = false;
    bool add_to_index = false;

    // The run.
    std::atomic<bool> running{false};
    std::atomic<bool> done_pending{false};
    std::atomic<int> exit_code{-1};
    std::mutex result_mu;
    std::string result_tail;  // last lines of the engine's stdout/stderr
    Platform run_platform = Platform::None;
    std::string run_out;      // the out dir of the run whose status is shown
    bool stop_requested = false;

    // What bulk_status.json last said.
    std::vector<ItemView> items;
    std::vector<std::string> stages;
    std::string run_state;
    double run_started = 0.0;
    double run_finished = 0.0;
    fs::file_time_type status_mtime{};
    double last_poll = 0.0;
};

BulkState& S() {
    static BulkState s;
    return s;
}

const char* const kBuildTypes[] = {"Release", "RelWithDebInfo", "Debug"};

unsigned cores() {
    const unsigned n = std::thread::hardware_concurrency();
    return n ? n : 4;
}

// cmake --build under Ninja takes every core by default. Four of those at
// once is four times the machine, so "auto" divides the cores between the
// projects running together.
int effective_build_jobs(const BulkState& s) {
    if (s.build_jobs > 0) return s.build_jobs;
    if (s.parallel <= 1) return 0;
    return std::max(1, static_cast<int>(cores()) / s.parallel);
}

std::string default_out_dir() {
#if defined(_WIN32)
    const char* home = std::getenv("USERPROFILE");
#else
    const char* home = std::getenv("HOME");
#endif
    const fs::path base = (home && *home) ? fs::path(home) / "Documents" : fs::current_path();
    return (base / "BulkRecompTest").string();
}

fs::path log_dir_for(const std::string& out) { return fs::path(out) / kLogDirName; }

// file:// URL for SDL_OpenURL. "Log Output" has a space in it, and a raw space
// in a URL is refused by some openers.
std::string file_url(const fs::path& p) {
    std::string s = p.generic_string();
    std::string enc;
    for (unsigned char c : s) {
        if (std::isalnum(c) || std::strchr("/-_.~:", c)) enc.push_back(static_cast<char>(c));
        else {
            char buf[4];
            std::snprintf(buf, sizeof(buf), "%%%02X", c);
            enc += buf;
        }
    }
    return (enc.rfind('/', 0) == 0 ? "file://" : "file:///") + enc;
}

void open_path(StudioModel& model, const fs::path& p) {
    std::error_code ec;
    if (!fs::exists(p, ec)) {
        model.append_log("[FAIL] Not there (yet): " + p.string());
        return;
    }
    if (!SDL_OpenURL(file_url(p).c_str()))
        model.append_log(std::string("[FAIL] Could not open ") + p.string() + ": " +
                         SDL_GetError());
}

// ---- dialogs ---------------------------------------------------------------

void SDLCALL files_cb(void* userdata, const char* const* filelist, int /*filter*/) {
    auto* s = static_cast<BulkState*>(userdata);
    std::lock_guard<std::mutex> lock(s->pick_mu);
    s->pick_busy = false;
    if (!filelist) return;
    for (int i = 0; filelist[i]; ++i) s->picked_files.emplace_back(filelist[i]);
}

void SDLCALL folder_cb(void* userdata, const char* const* filelist, int /*filter*/) {
    auto* s = static_cast<BulkState*>(userdata);
    std::lock_guard<std::mutex> lock(s->pick_mu);
    s->pick_busy = false;
    if (filelist && filelist[0]) s->picked_folder = filelist[0];
}

bool claim_picker(BulkState& s) {
    std::lock_guard<std::mutex> lock(s.pick_mu);
    if (s.pick_busy) return false;
    s.pick_busy = true;
    return true;
}

void apply_picks(BulkState& s) {
    std::vector<std::string> files;
    std::string folder;
    {
        std::lock_guard<std::mutex> lock(s.pick_mu);
        files.swap(s.picked_files);
        folder.swap(s.picked_folder);
    }
    if (!folder.empty()) std::snprintf(s.out_dir, sizeof(s.out_dir), "%s", folder.c_str());
    if (files.empty()) return;
    for (auto& f : files) {
        if (std::find(s.images.begin(), s.images.end(), f) == s.images.end())
            s.images.push_back(std::move(f));
    }
    std::sort(s.images.begin(), s.images.end());
    if (!s.running.load()) s.items.clear();  // the list changed; old results no longer match
}

// ---- status ----------------------------------------------------------------

std::string jstr(const json& j, const char* k) {
    auto it = j.find(k);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

double jnum(const json& j, const char* k) {
    auto it = j.find(k);
    return (it != j.end() && it->is_number()) ? it->get<double>() : 0.0;
}

void load_status(BulkState& s, bool force) {
    if (s.run_out.empty()) return;
    const double now = ImGui::GetTime();
    if (!force && now - s.last_poll < 0.25) return;
    s.last_poll = now;
    const fs::path p = log_dir_for(s.run_out) / kStatusFile;
    std::error_code ec;
    const auto mt = fs::last_write_time(p, ec);
    if (ec || (!force && mt == s.status_mtime)) return;
    std::ifstream in(p, std::ios::binary);
    if (!in) return;
    std::stringstream ss;
    ss << in.rdbuf();
    json j;
    try {
        j = json::parse(ss.str());
    } catch (...) {
        return;  // replaced atomically, so this is a file that is not ours
    }
    s.status_mtime = mt;
    s.run_state = jstr(j, "state");
    s.run_started = jnum(j, "started");
    s.run_finished = jnum(j, "finished");
    s.stages.clear();
    if (j.contains("stages") && j["stages"].is_array())
        for (const auto& st : j["stages"])
            if (st.is_string()) s.stages.push_back(st.get<std::string>());
    std::vector<ItemView> items;
    if (j.contains("items") && j["items"].is_array()) {
        for (const auto& ji : j["items"]) {
            ItemView v;
            v.index = static_cast<int>(jnum(ji, "index"));
            v.image = jstr(ji, "image");
            v.label = jstr(ji, "label");
            v.name = jstr(ji, "name");
            v.root = jstr(ji, "root");
            v.state = jstr(ji, "state");
            v.stage = jstr(ji, "stage");
            v.stage_index = static_cast<int>(jnum(ji, "stage_index"));
            if (!ji.contains("stage_index")) v.stage_index = -1;
            v.progress = static_cast<float>(jnum(ji, "progress"));
            v.message = jstr(ji, "message");
            v.log = jstr(ji, "log");
            v.log_dir = jstr(ji, "log_dir");
            v.started = jnum(ji, "started");
            v.finished = jnum(ji, "finished");
            if (ji.contains("stages") && ji["stages"].is_object())
                for (const auto& st : s.stages)
                    v.stages.emplace_back(st, ji["stages"].value(st, std::string("pending")));
            items.push_back(std::move(v));
        }
    }
    s.items = std::move(items);
}

// ---- run -------------------------------------------------------------------

void start_run(StudioModel& model, BulkState& s) {
    const std::string out = s.out_dir;
    std::error_code ec;
    const fs::path logs = log_dir_for(out);
    fs::create_directories(logs, ec);
    if (ec) {
        model.append_log("[FAIL] Bulk Recomp: cannot create " + logs.string() + ": " +
                         ec.message());
        return;
    }
    // Stale files from a previous batch in this folder: the old status would be
    // drawn as this run's until the engine's first write, and an old .cancel
    // would stop it before it started.
    fs::remove(logs / kStatusFile, ec);
    fs::remove(logs / kCancelFile, ec);

    // A list file rather than one --rom per image: a few hundred paths on one
    // command line is past what Windows accepts.
    const fs::path list = logs / kListFile;
    {
        std::ofstream f(list, std::ios::binary | std::ios::trunc);
        for (const auto& img : s.images) f << img << "\n";
        if (!f) {
            model.append_log("[FAIL] Bulk Recomp: cannot write " + list.string());
            return;
        }
    }

    std::vector<std::string> args = {"bulk-recomp",  "--out", out, "--rom-list", list.string(),
                                     "--parallel",   std::to_string(std::max(1, s.parallel)),
                                     "--build-type", kBuildTypes[s.build_type]};
    const int jobs = effective_build_jobs(s);
    if (jobs > 0) {
        args.push_back("--build-jobs");
        args.push_back(std::to_string(jobs));
    }
    if (s.create_github) args.push_back("--create-github");
    if (s.ci) args.push_back("--ci");
    if (s.boxart) args.push_back("--boxart");
    if (s.reuse_existing) args.push_back("--reuse-existing");
    if (s.add_to_index) args.push_back("--index");

    // Seed the table so the list is on screen from the first frame, before
    // the engine has written anything.
    s.items.clear();
    for (size_t i = 0; i < s.images.size(); ++i) {
        ItemView v;
        v.index = static_cast<int>(i);
        v.image = s.images[i];
        v.name = fs::path(s.images[i]).stem().string();
        s.items.push_back(std::move(v));
    }
    s.stages.clear();
    s.run_state = "starting";
    s.run_out = out;
    s.run_platform = model.platform;
    s.status_mtime = {};
    s.stop_requested = false;
    s.exit_code.store(-1);
    s.running.store(true);

    model.append_log("Bulk Recomp: " + std::to_string(s.images.size()) + " image(s) → " + out +
                     " — stage output goes to " + logs.string() + ", not here.");
    std::thread([&model, &s, args = std::move(args)]() {
        // log_stdout=false: the engine prints one line per verdict, which would
        // be fine here, but the stage output it is keeping out of this log
        // would not — and the table already shows every verdict.
        RunResult r = run_project_studio(model, args, false);
        {
            std::lock_guard<std::mutex> lock(s.result_mu);
            std::string text = r.stdout_text;
            if (!r.stderr_text.empty()) text += r.stderr_text;
            // Keep the tail: the summary line, or the validation errors.
            std::vector<std::string> lines;
            std::stringstream ss(text);
            for (std::string line; std::getline(ss, line);)
                if (!line.empty()) lines.push_back(line);
            const size_t from = lines.size() > 6 ? lines.size() - 6 : 0;
            s.result_tail.clear();
            for (size_t i = from; i < lines.size(); ++i) s.result_tail += lines[i] + "\n";
        }
        s.exit_code.store(r.exit_code);
        s.running.store(false);
        s.done_pending.store(true);
    }).detach();
}

void request_stop(StudioModel* model, BulkState& s) {
    if (!s.running.load() || s.run_out.empty()) return;
    std::ofstream(log_dir_for(s.run_out) / kCancelFile) << "stop\n";
    s.stop_requested = true;
    if (model) model->append_log("Bulk Recomp: stop requested — running stages are being killed.");
}

void finish_run(StudioModel& model, BulkState& s) {
    load_status(s, true);
    int passed = 0, failed = 0;
    for (const auto& it : s.items) {
        if (it.state == "passed") ++passed;
        else if (it.state == "failed" || it.state == "cancelled") ++failed;
    }
    std::string tail;
    {
        std::lock_guard<std::mutex> lock(s.result_mu);
        tail = s.result_tail;
    }
    const int code = s.exit_code.load();
    if (code == 2 || s.run_state.empty() || s.run_state == "starting") {
        // Refused before it started (validation) — nothing in the table is real.
        model.append_log("[FAIL] Bulk Recomp did not start:");
        std::stringstream ss(tail);
        for (std::string line; std::getline(ss, line);) model.append_log("  " + line);
        s.items.clear();
        return;
    }
    const std::string summary = (log_dir_for(s.run_out) / kSummaryFile).string();
    model.append_log(std::string(code == 0 ? "[OK]" : "[FAIL]") + " Bulk Recomp: " +
                     std::to_string(passed) + " passed, " + std::to_string(failed) +
                     " failed of " + std::to_string(s.items.size()) +
                     (s.run_state == "cancelled" ? " (stopped)" : "") + " — " + summary);
    model.set_status("Bulk Recomp: " + std::to_string(passed) + "/" +
                     std::to_string(s.items.size()) + " passed");
}

// ---- drawing ---------------------------------------------------------------

void accent_on(const Theme& th) {
    ImGui::PushStyleColor(ImGuiCol_Button, th.accent_button);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, th.accent_button_hovered);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, th.accent_button_active);
}
void accent_off() { ImGui::PopStyleColor(3); }

void label(const Theme& th, const char* text, float w) {
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(th.text_muted, "%s", text);
    ImGui::SameLine(w);
}

std::string fmt_elapsed(double secs) {
    if (secs <= 0) return "";
    const int s = static_cast<int>(secs);
    char buf[32];
    if (s >= 3600) std::snprintf(buf, sizeof(buf), "%dh%02dm", s / 3600, (s / 60) % 60);
    else std::snprintf(buf, sizeof(buf), "%dm%02ds", s / 60, s % 60);
    return buf;
}

double now_epoch() {
    return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch())
        .count();
}

void status_cell(const Theme& th, const ItemView& it, bool stopping) {
    ImVec4 col = th.text_muted;
    const char* text = "QUEUED";
    if (it.state == "passed") { col = th.good; text = "PASSED"; }
    else if (it.state == "failed") { col = th.bad; text = "FAILED"; }
    else if (it.state == "cancelled") { col = th.warn; text = "STOPPED"; }
    else if (it.state == "running") { col = th.accent; text = stopping ? "STOPPING" : "RUNNING"; }
    ImGui::TextColored(col, "%s", text);
    if (!it.message.empty() && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", it.message.c_str());
}

void draw_table(StudioModel& model, const Theme& th, BulkState& s, bool results) {
    const ImGuiTableFlags flags = ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH |
                                  ImGuiTableFlags_ScrollY | ImGuiTableFlags_SizingStretchProp |
                                  ImGuiTableFlags_Resizable;
    if (!ImGui::BeginTable("##bulk_items", 6, flags, ImVec2(0, 0))) return;
    ImGui::TableSetupScrollFreeze(0, 1);
    ImGui::TableSetupColumn("#", ImGuiTableColumnFlags_WidthFixed, 28.f);
    ImGui::TableSetupColumn("Project", ImGuiTableColumnFlags_WidthStretch, 2.2f);
    ImGui::TableSetupColumn("Progress", ImGuiTableColumnFlags_WidthStretch, 2.4f);
    ImGui::TableSetupColumn("Status", ImGuiTableColumnFlags_WidthFixed, 76.f);
    ImGui::TableSetupColumn("Detail", ImGuiTableColumnFlags_WidthStretch, 2.0f);
    ImGui::TableSetupColumn("", ImGuiTableColumnFlags_WidthFixed, 96.f);
    ImGui::TableHeadersRow();

    const int n_stages = static_cast<int>(s.stages.size());
    int remove_idx = -1;
    const size_t rows = results ? s.items.size() : s.images.size();
    for (size_t i = 0; i < rows; ++i) {
        ImGui::PushID(static_cast<int>(i));
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        ImGui::TextColored(th.text_muted, "%zu", i + 1);

        if (!results) {
            const fs::path p(s.images[i]);
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(p.stem().string().c_str());
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", s.images[i].c_str());
            ImGui::TableNextColumn();
            ImGui::TextColored(th.text_muted, "—");
            ImGui::TableNextColumn();
            ImGui::TextColored(th.text_muted, "QUEUED");
            ImGui::TableNextColumn();
            ImGui::TextColored(th.text_muted, "%s", p.parent_path().string().c_str());
            ImGui::TableNextColumn();
            if (ImGui::SmallButton("Remove")) remove_idx = static_cast<int>(i);
            ImGui::PopID();
            continue;
        }

        const ItemView& it = s.items[i];
        ImGui::TableNextColumn();
        const std::string shown =
            it.name.empty() ? fs::path(it.image).stem().string() : it.name;
        ImGui::TextUnformatted(shown.c_str());
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("%s%s%s", it.image.c_str(), it.root.empty() ? "" : "\n→ ",
                              it.root.c_str());

        // Progress: whole-pipeline fraction, labelled with the stage in hand.
        ImGui::TableNextColumn();
        char overlay[128];
        if (it.state == "running" && it.stage_index >= 0)
            std::snprintf(overlay, sizeof(overlay), "%s  %d/%d  %d%%", it.stage.c_str(),
                          it.stage_index + 1, n_stages, static_cast<int>(it.progress * 100));
        else if (it.state == "failed" || it.state == "cancelled")
            std::snprintf(overlay, sizeof(overlay), "%s at %s", it.state == "failed" ? "failed"
                                                                                    : "stopped",
                          it.stage.empty() ? "start" : it.stage.c_str());
        else if (it.state == "passed")
            std::snprintf(overlay, sizeof(overlay), "done");
        else
            std::snprintf(overlay, sizeof(overlay), "queued");
        ImVec4 bar = th.accent;
        if (it.state == "passed") bar = th.good;
        else if (it.state == "failed") bar = th.bad;
        else if (it.state == "cancelled") bar = th.warn;
        ImGui::PushStyleColor(ImGuiCol_PlotHistogram, bar);
        ImGui::ProgressBar(it.state == "failed" || it.state == "cancelled"
                               ? std::max(it.progress, 0.02f)
                               : it.progress,
                           ImVec2(-1, 0), overlay);
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered() && !it.stages.empty()) {
            ImGui::BeginTooltip();
            for (const auto& [name, st] : it.stages) {
                ImVec4 c = th.text_muted;
                if (st == "passed") c = th.good;
                else if (st == "failed") c = th.bad;
                else if (st == "running") c = th.accent;
                else if (st == "skipped") c = th.warn;
                ImGui::TextColored(c, "%-10s %s", name.c_str(), st.c_str());
            }
            ImGui::EndTooltip();
        }

        ImGui::TableNextColumn();
        status_cell(th, it, s.stop_requested);

        ImGui::TableNextColumn();
        std::string detail = it.message;
        if (it.state == "running" && it.started > 0)
            detail = fmt_elapsed(now_epoch() - it.started);
        else if (it.finished > 0 && it.started > 0 && it.state == "passed")
            detail = "built in " + fmt_elapsed(it.finished - it.started);
        ImGui::TextColored(it.state == "failed" ? th.bad : th.text_muted, "%s", detail.c_str());
        if (!it.message.empty() && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", it.message.c_str());

        ImGui::TableNextColumn();
        ImGui::BeginDisabled(it.log.empty());
        if (ImGui::SmallButton("Log")) open_path(model, it.log);
        ImGui::EndDisabled();
        if (!it.log.empty() && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
            ImGui::SetTooltip("%s", it.log.c_str());
        ImGui::SameLine();
        ImGui::BeginDisabled(it.log_dir.empty());
        if (ImGui::SmallButton("Logs")) open_path(model, it.log_dir);
        ImGui::EndDisabled();
        ImGui::PopID();
    }
    ImGui::EndTable();
    if (remove_idx >= 0) s.images.erase(s.images.begin() + remove_idx);
}

} // namespace

void draw_bulk_recomp(StudioModel& model, const Theme& th, SDL_Window* window) {
    BulkState& s = S();
    constexpr float kLabelW = 150.f;
    apply_picks(s);

    // Images are one console's: a PSX list is meaningless in a SNES session.
    // A running batch keeps its list — the engine was started with its own
    // --platform and does not care what the header now says.
    if (!s.running.load() && s.list_platform != model.platform) {
        s.images.clear();
        s.items.clear();
        s.list_platform = model.platform;
    }
    if (!s.out_dir[0]) std::snprintf(s.out_dir, sizeof(s.out_dir), "%s", default_out_dir().c_str());

    if (s.done_pending.exchange(false)) finish_run(model, s);
    const bool running = s.running.load();
    if (running || !s.run_out.empty()) load_status(s, false);

    ImGui::TextColored(th.text_muted,
                       "Scaffold, generate and build every image into one folder, to test the "
                       "pipeline across a set of dumps. Each stage's output is written to "
                       "<output>/%s/, not to the Activity log.",
                       kLogDirName);
    ImGui::Spacing();

    ImGui::BeginDisabled(running);
    // ---- images --------------------------------------------------------
    label(th, platform_image_label(model.platform), kLabelW);
    if (ImGui::Button("Add images…")) {
        if (claim_picker(s)) {
            SDL_DialogFileFilter filters[1];
            filters[0].name = platform_image_filter_name(model.platform);
            filters[0].pattern = platform_image_filter_ext(model.platform);
            SDL_ShowOpenFileDialog(files_cb, &s, window, filters, 1, nullptr, true);
        }
    }
    ImGui::SameLine();
    ImGui::BeginDisabled(s.images.empty());
    if (ImGui::Button("Clear")) {
        s.images.clear();
        s.items.clear();
    }
    ImGui::EndDisabled();
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted, "%zu selected", s.images.size());
    if (!model.is_cartridge()) {
        ImGui::SameLine();
        ImGui::TextColored(th.text_muted,
                           "— one project per .cue; a multi-disc set is not grouped here.");
    }

    // ---- output --------------------------------------------------------
    label(th, "Output folder", kLabelW);
    ImGui::SetNextItemWidth(-110.f);
    ImGui::InputText("##bulk_out", s.out_dir, sizeof(s.out_dir));
    ImGui::SameLine();
    if (ImGui::Button("Browse…##bulk_out") && claim_picker(s))
        SDL_ShowOpenFolderDialog(folder_cb, &s, window, s.out_dir, false);

    // ---- options -------------------------------------------------------
    label(th, "Parallel projects", kLabelW);
    ImGui::SetNextItemWidth(160.f);
    ImGui::SliderInt("##bulk_par", &s.parallel, 1, std::max(2, static_cast<int>(cores())));
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted, "%s",
                       s.parallel <= 1 ? "one after another" : "run at the same time");

    label(th, "Build jobs", kLabelW);
    ImGui::SetNextItemWidth(160.f);
    ImGui::InputInt("##bulk_jobs", &s.build_jobs);
    s.build_jobs = std::max(0, s.build_jobs);
    ImGui::SameLine();
    if (s.build_jobs == 0) {
        const int eff = effective_build_jobs(s);
        if (eff > 0)
            ImGui::TextColored(th.text_muted, "auto: %d per build (%u cores / %d projects)", eff,
                               cores(), s.parallel);
        else
            ImGui::TextColored(th.text_muted, "auto: cmake's default");
    }

    label(th, "Build type", kLabelW);
    ImGui::SetNextItemWidth(160.f);
    ImGui::Combo("##bulk_bt", &s.build_type, kBuildTypes, IM_ARRAYSIZE(kBuildTypes));

    label(th, "Scaffold", kLabelW);
    ImGui::Checkbox("Create GitHub repos", &s.create_github);
    if (s.create_github) {
        ImGui::SameLine();
        ImGui::TextColored(th.warn, "one private repo PER image");
    }
    ImGui::SameLine();
    ImGui::Checkbox("CI workflows", &s.ci);
    ImGui::SameLine();
    ImGui::Checkbox("Fetch boxart", &s.boxart);
    label(th, "", kLabelW);
    ImGui::Checkbox("Reuse existing projects", &s.reuse_existing);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip(
            "A project folder that already exists in the output folder is rebuilt\n"
            "(generate / configure / compile) instead of being FAILED. Off, a\n"
            "re-run into the same folder fails those projects rather than\n"
            "building an old scaffold and calling it a pass.");
    ImGui::SameLine();
    ImGui::Checkbox("Add to repo index", &s.add_to_index);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Off: the test projects stay out of the game repo dropdown.");
    ImGui::EndDisabled();

    ImGui::Spacing();
    // ---- actions -------------------------------------------------------
    if (!running) {
        accent_on(th);
        ImGui::BeginDisabled(s.images.empty() || !s.out_dir[0]);
        if (ImGui::Button("Run Bulk Recomp", ImVec2(180.f, 0))) start_run(model, s);
        ImGui::EndDisabled();
        accent_off();
    } else {
        ImGui::BeginDisabled(s.stop_requested);
        ImGui::PushStyleColor(ImGuiCol_Button, th.bad);
        if (ImGui::Button(s.stop_requested ? "Stopping…" : "Stop", ImVec2(180.f, 0)))
            request_stop(&model, s);
        ImGui::PopStyleColor();
        ImGui::EndDisabled();
    }
    ImGui::SameLine();
    const std::string shown_out = s.run_out.empty() ? std::string(s.out_dir) : s.run_out;
    if (ImGui::Button("Open Log Output")) open_path(model, log_dir_for(shown_out));
    ImGui::SameLine();
    if (ImGui::Button("Open output folder")) open_path(model, shown_out);
    if (!running && !s.items.empty() && !s.run_out.empty()) {
        ImGui::SameLine();
        if (ImGui::Button("Summary")) open_path(model, log_dir_for(s.run_out) / kSummaryFile);
    }

    // ---- overall -------------------------------------------------------
    const bool results = !s.items.empty();
    if (results) {
        int passed = 0, failed = 0, active = 0;
        float sum = 0.f;
        for (const auto& it : s.items) {
            sum += it.progress;
            if (it.state == "passed") ++passed;
            else if (it.state == "failed" || it.state == "cancelled") ++failed;
            else if (it.state == "running") ++active;
        }
        const int n = static_cast<int>(s.items.size());
        char overlay[160];
        std::snprintf(overlay, sizeof(overlay), "%d/%d finished — %d passed, %d failed%s", passed + failed,
                      n, passed, failed,
                      running ? (active ? "" : " — starting") : "");
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_PlotHistogram,
                              !running ? (failed ? th.bad : th.good) : th.accent);
        ImGui::ProgressBar(n ? sum / static_cast<float>(n) : 0.f, ImVec2(-1, 0), overlay);
        ImGui::PopStyleColor();
        if (s.run_platform != model.platform && s.run_platform != Platform::None) {
            ImGui::TextColored(th.warn, "This batch is %s, started before the console changed.",
                               platform_display(s.run_platform));
        }
        const double end = s.run_finished > 0 ? s.run_finished : now_epoch();
        if (s.run_started > 0)
            ImGui::TextColored(th.text_muted, "%s %s — %s", running ? "Running" : "Finished",
                               fmt_elapsed(end - s.run_started).c_str(), s.run_out.c_str());
    }
    ImGui::Spacing();
    draw_table(model, th, s, results);
}

void bulk_recomp_shutdown() {
    BulkState& s = S();
    request_stop(nullptr, s);
}

} // namespace retcomm::studio
