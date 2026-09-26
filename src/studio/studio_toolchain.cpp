// studio_toolchain.cpp — see studio_toolchain.hpp.

#include "studio/studio_toolchain.hpp"

#include "studio/studio_runner.hpp"

#include "imgui.h"

#include <SDL3/SDL.h>
#include <SDL3/SDL_dialog.h>

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

using json = nlohmann::json;

namespace retcomm::studio {

namespace {

// One row per n64_toolchain.TOOLS entry, in the CLI's order. The CLI is the
// authority on the list; this copy only gives the rows stable ImGui ids before
// the first answer arrives, and is overwritten by it.
struct ToolRow {
    std::string key;
    std::string label;
    char buf[1024] = {};      // what this surface stores (project file / form)
    std::string path;         // resolved
    std::string source;       // project | studio | env | path | none
    std::string source_label;
    std::string version;
    std::string error;
    std::string warning;
    std::string env_var;
    bool is_path = true;      // false: the CMake generator (a name, no browse)
};

struct Scope {
    const char* id;           // ImGui id prefix: "tcb" (Build) / "tcn" (New Project)
    bool new_project;
    std::vector<ToolRow> rows;
    std::string loaded_for;   // root (Build) or "studio" (New Project)
    bool loading = false;
    bool loaded = false;
    std::string project_file;
    bool project_file_exists = false;
    std::string load_error;
    // Detect popup
    int detect_row = -1;
    bool detect_open = false;
    ImVec2 detect_anchor{0.f, 0.f};  // under the row's field, not at the mouse
    bool detect_loading = false;
    std::vector<std::string> detect_candidates;
    // A request that arrived while another job held `busy` (a `set` must not
    // be dropped just because a build started the same frame).
    std::vector<std::string> pending_args;
    bool pending_fill = false;
};

Scope g_build{"tcb", false, {}};
Scope g_np{"tcn", true, {}};

void seed_rows(Scope& s) {
    if (!s.rows.empty()) return;
    static const char* kKeys[][2] = {
        {"python", "Python"}, {"cc", "C compiler"}, {"cxx", "C++ compiler"},
        {"cmake", "CMake"},   {"generator", "Generator"}, {"ninja", "Ninja"},
        {"cargo", "Cargo"},   {"git", "Git"},             {"gh", "GitHub CLI"},
    };
    for (const auto& k : kKeys) {
        ToolRow r;
        r.key = k[0];
        r.label = k[1];
        s.rows.push_back(std::move(r));
    }
}

ToolRow* row_by_key(Scope& s, const std::string& key) {
    for (auto& r : s.rows)
        if (r.key == key) return &r;
    return nullptr;
}

void set_buf(ToolRow& r, const std::string& v) {
    std::snprintf(r.buf, sizeof(r.buf), "%s", v.c_str());
}

// Take the CLI's JSON. `fill_bufs`: overwrite the text fields from the stored
// layer (the project file on the Build tab, the Studio default on New
// Project) -- done on load, NOT after a New Project validate, where the fields
// are the user's unsaved form and must survive.
bool apply_json(Scope& s, const std::string& text, bool fill_bufs, std::string* err) {
    json j;
    try {
        j = json::parse(text);
    } catch (const std::exception& e) {
        if (err) *err = e.what();
        return false;
    }
    if (!j.is_object() || !j.contains("tools") || !j["tools"].is_array()) {
        if (err) *err = "no tools[] in toolchain JSON";
        return false;
    }
    s.project_file = j.value("project_file", "");
    s.project_file_exists = j.value("project_file_exists", false);
    std::vector<ToolRow> rows;
    for (const auto& t : j["tools"]) {
        ToolRow r;
        r.key = t.value("key", "");
        r.label = t.value("label", r.key);
        r.path = t.value("path", "");
        r.source = t.value("source", "none");
        r.source_label = t.value("source_label", r.source);
        r.version = t.value("version", "");
        r.error = t.value("error", "");
        r.warning = t.value("warning", "");
        r.env_var = t.value("env_var", "");
        r.is_path = t.value("is_path", true);
        const ToolRow* old = row_by_key(s, r.key);
        if (fill_bufs) {
            set_buf(r, s.new_project ? t.value("studio", "") : t.value("project", ""));
        } else if (old) {
            std::memcpy(r.buf, old->buf, sizeof(r.buf));
        }
        rows.push_back(std::move(r));
    }
    s.rows = std::move(rows);
    s.loaded = true;
    s.load_error.clear();
    return true;
}

std::vector<std::string> form_tool_args(const Scope& s) {
    std::vector<std::string> out;
    for (const auto& r : s.rows) {
        if (!r.buf[0]) continue;
        out.push_back("--tool");
        out.push_back(r.key + "=" + r.buf);
    }
    return out;
}

// Ask the CLI. Never while another job holds `busy`: a toolchain read that
// failed with "Another job is already running" would be noise in the log, and
// running it alongside (allow_when_busy) would clear the other job's busy flag
// when this one finished. It is retried on a later frame instead.
void request(StudioModel& model, Scope& s, std::vector<std::string> args, bool fill_bufs) {
    if (s.loading || model.busy.load()) {
        // A later `set` supersedes an earlier queued one only for the same
        // row, and rows commit one at a time, so keeping the newest is right.
        s.pending_args = std::move(args);
        s.pending_fill = fill_bufs;
        return;
    }
    s.pending_args.clear();
    s.loading = true;
    Scope* sp = &s;
    run_project_studio_async(
        model, std::move(args),
        [sp, &model, fill_bufs](RunResult r) {
            sp->loading = false;
            std::string err;
            if (!r.ok() || !apply_json(*sp, r.stdout_text, fill_bufs, &err)) {
                sp->load_error = err.empty() ? r.stderr_text : err;
                sp->loaded = true;  // stop retrying every frame; Reload retries
                model.append_log("[FAIL] Toolchain: " + sp->load_error);
            }
        },
        false);
}

void load_build(StudioModel& model, const std::string& root) {
    g_build.loaded_for = root;
    request(model, g_build, {"toolchain", "show", "--root", root, "--json", "--compact"}, true);
}

void load_np(StudioModel& model, bool fill_bufs) {
    g_np.loaded_for = "studio";
    std::vector<std::string> args = {"toolchain", "show", "--studio", "--json", "--compact"};
    if (!fill_bufs) {
        auto extra = form_tool_args(g_np);
        args.insert(args.end(), extra.begin(), extra.end());
    }
    request(model, g_np, std::move(args), fill_bufs);
}

// Record one row. Build tab: into the port's tools/toolchain.cmake, then the
// CLI answers with the re-resolved state. New Project: only the form changed,
// so re-validate it.
void commit_row(StudioModel& model, Scope& s, const ToolRow& r, const std::string& root) {
    if (s.new_project) {
        load_np(model, false);
        return;
    }
    if (root.empty()) return;
    std::vector<std::string> args = {"toolchain", "set", "--root", root,
                                     "--tool", r.key + "=" + r.buf, "--json", "--compact"};
    model.append_log("Toolchain: " + r.label + " = " + (r.buf[0] ? r.buf : "(cleared)") +
                     " -> tools/toolchain.cmake");
    request(model, s, std::move(args), true);
}

// ---- file dialog ------------------------------------------------------------
// Its own callback and pending slot rather than studio_main's pick_file(): the
// target here is (scope, row), and routing that through the string-keyed
// pending_pick_target would put toolchain plumbing into every other picker.
std::mutex g_pick_mu;
struct PendingPick {
    Scope* scope = nullptr;
    std::string key;
    std::string path;
    bool ready = false;
};
PendingPick g_pick;
bool g_pick_busy = false;

struct PickCtx {
    Scope* scope;
    std::string key;
};

void SDLCALL pick_cb(void* userdata, const char* const* files, int /*filter*/) {
    auto* ctx = static_cast<PickCtx*>(userdata);
    std::lock_guard<std::mutex> lock(g_pick_mu);
    g_pick_busy = false;
    if (ctx && files && files[0]) {
        g_pick.scope = ctx->scope;
        g_pick.key = ctx->key;
        g_pick.path = files[0];
        g_pick.ready = true;
    }
    delete ctx;
}

void browse(SDL_Window* window, Scope& s, const ToolRow& r) {
    {
        std::lock_guard<std::mutex> lock(g_pick_mu);
        if (g_pick_busy) return;
        g_pick_busy = true;
    }
    auto* ctx = new PickCtx{&s, r.key};
#if defined(_WIN32)
    static const SDL_DialogFileFilter kFilter[] = {{"Programs", "exe;bat;cmd"}, {"All", "*"}};
    SDL_ShowOpenFileDialog(pick_cb, ctx, window, kFilter, 2, nullptr, false);
#else
    // No extension filter: a Linux compiler is `clang-19`, not `clang.exe`.
    SDL_ShowOpenFileDialog(pick_cb, ctx, window, nullptr, 0, nullptr, false);
#endif
}

void apply_pick(StudioModel& model, const std::string& root) {
    PendingPick p;
    {
        std::lock_guard<std::mutex> lock(g_pick_mu);
        if (!g_pick.ready) return;
        p = g_pick;
        g_pick = PendingPick{};
    }
    if (!p.scope) return;
    if (ToolRow* r = row_by_key(*p.scope, p.key)) {
        set_buf(*r, p.path);
        commit_row(model, *p.scope, *r, root);
    }
}

// ---- drawing ----------------------------------------------------------------

void label_col(const char* label, float w) {
    ImGui::AlignTextToFramePadding();
    const float x0 = ImGui::GetCursorPosX();
    ImGui::TextUnformatted(label);
    ImGui::SameLine(0.f, 0.f);
    const float pad = w - (ImGui::GetCursorPosX() - x0);
    ImGui::Dummy(ImVec2(pad > 0.f ? pad : ImGui::GetStyle().ItemSpacing.x, 0.f));
    ImGui::SameLine();
}

float button_w(const char* label) {
    const char* end = std::strstr(label, "##");
    const ImVec2 sz = ImGui::CalcTextSize(label, end);
    return sz.x + ImGui::GetStyle().FramePadding.x * 2.f + ImGui::GetStyle().ItemSpacing.x;
}

void draw_detect_popup(StudioModel& model, Scope& s, const std::string& root) {
    char pid[48];
    std::snprintf(pid, sizeof(pid), "Detect###%s_detect", s.id);
    if (s.detect_open) {
        ImGui::OpenPopup(pid);
        s.detect_open = false;
        // Anchored to the field it fills: opened at the mouse, the list sits
        // on the Detect button at the right edge and its paths are clipped.
        ImGui::SetNextWindowPos(s.detect_anchor, ImGuiCond_Appearing);
    }
    if (!ImGui::BeginPopup(pid)) return;
    if (s.detect_row < 0 || s.detect_row >= static_cast<int>(s.rows.size())) {
        ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
        return;
    }
    ToolRow& r = s.rows[static_cast<size_t>(s.detect_row)];
    ImGui::TextDisabled(r.is_path ? "%s on PATH" : "CMake %s", r.label.c_str());
    ImGui::Separator();
    if (s.detect_loading) {
        ImGui::TextDisabled("Searching…");
    } else if (s.detect_candidates.empty()) {
        ImGui::TextDisabled("Nothing found on PATH. Use … to browse.");
    }
    for (size_t i = 0; i < s.detect_candidates.size(); ++i) {
        const std::string& c = s.detect_candidates[i];
        char sid[40];
        std::snprintf(sid, sizeof(sid), "##%s_cand%zu", s.id, i);
        if (ImGui::Selectable((c + sid).c_str(), c == r.buf)) {
            set_buf(r, c);
            commit_row(model, s, r, root);
            ImGui::CloseCurrentPopup();
        }
    }
    ImGui::EndPopup();
}

void start_detect(StudioModel& model, Scope& s, int row) {
    if (model.busy.load()) return;
    s.detect_row = row;
    s.detect_open = true;
    s.detect_loading = true;
    s.detect_candidates.clear();
    Scope* sp = &s;
    run_project_studio_async(
        model, {"toolchain", "detect", s.rows[static_cast<size_t>(row)].key, "--json"},
        [sp](RunResult r) {
            sp->detect_loading = false;
            try {
                const json j = json::parse(r.stdout_text);
                for (const auto& c : j.value("candidates", json::array()))
                    if (c.is_string()) sp->detect_candidates.push_back(c.get<std::string>());
            } catch (const std::exception&) {
            }
        },
        false);
}

void flush_pending(StudioModel& model, Scope& s) {
    if (s.pending_args.empty() || s.loading || model.busy.load()) return;
    std::vector<std::string> args = std::move(s.pending_args);
    s.pending_args.clear();
    request(model, s, std::move(args), s.pending_fill);
}

void draw_rows(StudioModel& model, const Theme& th, SDL_Window* window, Scope& s,
               const std::string& root, float label_w) {
    // The tab's label column, widened to the longest tool name so every field
    // starts at the same x ("C++ compiler" is wider than the Build tab's 100).
    label_w = std::max(label_w, ImGui::CalcTextSize("C++ compiler").x +
                                    ImGui::GetStyle().ItemSpacing.x * 2.f);
    const float bw = button_w("…") + button_w("Detect") + button_w("Clear");
    for (size_t i = 0; i < s.rows.size(); ++i) {
        ToolRow& r = s.rows[i];
        ImGui::PushID(s.id);
        ImGui::PushID(r.key.c_str());
        label_col(r.label.c_str(), label_w);
        float fw = ImGui::GetContentRegionAvail().x - bw;
        if (fw < 120.f) fw = 120.f;
        ImGui::SetNextItemWidth(fw);
        // Blank field = nothing recorded here; the hint shows what WILL be
        // used instead, so an empty row is never read as "no tool".
        std::string hint = r.path.empty() ? std::string("(not found)")
                                          : r.path + "  [" + r.source_label + "]";
        ImGui::InputTextWithHint("##path", hint.c_str(), r.buf, sizeof(r.buf));
        if (ImGui::IsItemDeactivatedAfterEdit()) commit_row(model, s, r, root);
        const ImVec2 field_anchor(ImGui::GetItemRectMin().x, ImGui::GetItemRectMax().y);
        ImGui::SameLine();
        ImGui::BeginDisabled(!r.is_path);
        if (ImGui::Button("…")) browse(window, s, r);
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered() && r.is_path)
            ImGui::SetTooltip("Browse for the %s", r.label.c_str());
        ImGui::SameLine();
        if (ImGui::Button("Detect")) {
            s.detect_anchor = field_anchor;
            start_detect(model, s, static_cast<int>(i));
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(r.is_path ? "List every %s on PATH and pick one"
                                        : "Pick a CMake %s", r.label.c_str());
        ImGui::SameLine();
        ImGui::BeginDisabled(r.buf[0] == '\0');
        if (ImGui::Button("Clear")) {
            r.buf[0] = '\0';
            commit_row(model, s, r, root);
        }
        ImGui::EndDisabled();
        ImGui::PopID();
        ImGui::PopID();

        // Status: resolved path, where it came from, version -- then the
        // row's own error / warning, so a bad pick is named beside its field.
        label_col("", label_w);
        if (!r.error.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, th.bad);
            ImGui::TextWrapped("%s", r.error.c_str());
            ImGui::PopStyleColor();
        } else if (!r.path.empty()) {
            const bool chosen = r.source == "project" || r.source == "studio";
            std::string src = r.source_label;
            if (s.new_project && r.source == "project") src = "this form";
            ImGui::PushStyleColor(ImGuiCol_Text, chosen ? th.good : th.text_muted);
            ImGui::TextWrapped("%s  ·  %s%s%s", r.path.c_str(), src.c_str(),
                               r.version.empty() ? "" : "  ·  ", r.version.c_str());
            ImGui::PopStyleColor();
        } else {
            ImGui::TextColored(th.text_muted, "%s", s.loaded ? "not found" : "…");
        }
        if (!r.warning.empty()) {
            label_col("", label_w);
            ImGui::PushStyleColor(ImGuiCol_Text, th.warn);
            ImGui::TextWrapped("%s", r.warning.c_str());
            ImGui::PopStyleColor();
        }
    }
    draw_detect_popup(model, s, root);
}

} // namespace

void draw_n64_toolchain_build(StudioModel& model, const Theme& th, SDL_Window* window,
                              float label_w) {
    const std::string root = model.selected_root();
    seed_rows(g_build);
    apply_pick(model, root);
    flush_pending(model, g_build);
    if (!root.empty() && g_build.loaded_for != root && !g_build.loading) {
        g_build.loaded = false;
        load_build(model, root);
    } else if (!root.empty() && !g_build.loaded && !g_build.loading &&
               g_build.pending_args.empty()) {
        load_build(model, root);  // deferred by a busy job
    }

    ImGui::SeparatorText("Toolchain");
    ImGui::PushStyleColor(ImGuiCol_Text, th.text_muted);
    ImGui::TextWrapped(
        "One path per tool for every n64lle command Studio runs here (Build framework, "
        "Configure, Build, Generate, Migrate). A blank field falls back to the Studio "
        "default, then the environment, then PATH -- the grey text says which. Choices "
        "are recorded in %s, which n64lle's own scripts read too, so a terminal build "
        "uses the same tools. What each tool is for: n64lle docs/PROJECT-SETUP.md.",
        g_build.project_file.empty() ? "tools/toolchain.cmake"
                                     : g_build.project_file.c_str());
    ImGui::PopStyleColor();
    draw_rows(model, th, window, g_build, root, label_w);

    label_col("", label_w);
    if (ImGui::Button("Reload##tcb")) {
        g_build.loaded = false;
        load_build(model, root);
    }
    ImGui::SameLine();
    if (ImGui::Button("Save as Studio default##tcb")) {
        // The resolved path of every row, not the blank fields: "what this
        // port builds with now" is the useful thing to reuse for the next one.
        std::vector<std::string> args = {"toolchain", "set", "--studio", "--json", "--compact"};
        for (const auto& r : g_build.rows) {
            if (r.path.empty() || !r.error.empty()) continue;
            args.push_back("--tool");
            args.push_back(r.key + "=" + r.path);
        }
        model.append_log("Toolchain: saved this port's tools as the Studio default");
        run_project_studio_async(model, std::move(args), nullptr, false);
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Store every resolved path above as the default for new N64\n"
                          "projects (and for ports that record nothing).");
    ImGui::SameLine();
    ImGui::TextColored(g_build.project_file_exists ? th.good : th.text_muted, "%s",
                       g_build.project_file_exists ? "recorded in the port"
                                                   : "nothing recorded yet");
    if (!g_build.load_error.empty()) {
        label_col("", label_w);
        ImGui::TextColored(th.bad, "%s", g_build.load_error.c_str());
    }
}

void draw_n64_toolchain_new_project(StudioModel& model, const Theme& th, SDL_Window* window,
                                    float label_w) {
    seed_rows(g_np);
    apply_pick(model, "");
    flush_pending(model, g_np);
    if (!g_np.loaded && !g_np.loading && g_np.pending_args.empty()) load_np(model, true);

    ImGui::SeparatorText("Toolchain");
    ImGui::PushStyleColor(ImGuiCol_Text, th.text_muted);
    ImGui::TextWrapped(
        "The tools the scaffold is cut and first built with, passed to setup_project.sh "
        "and recorded in the new port's tools/toolchain.cmake. Prefilled from the Studio "
        "default; blank = the scaffolder finds it on PATH.");
    ImGui::PopStyleColor();
    draw_rows(model, th, window, g_np, "", label_w);
    label_col("", label_w);
    if (ImGui::Button("Save as Studio default##tcn")) {
        std::vector<std::string> args = {"toolchain", "set", "--studio", "--json", "--compact"};
        for (const auto& r : g_np.rows) {
            args.push_back("--tool");
            args.push_back(r.key + "=" + r.buf);  // blank clears that default
        }
        model.append_log("Toolchain: saved the New Project rows as the Studio default");
        run_project_studio_async(model, std::move(args), nullptr, false);
    }
    ImGui::SameLine();
    if (ImGui::Button("Reset to Studio default##tcn")) {
        g_np.loaded = false;
        load_np(model, true);
    }
}

std::vector<std::string> n64_new_project_tool_args() { return form_tool_args(g_np); }

} // namespace retcomm::studio
