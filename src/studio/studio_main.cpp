#include "studio/studio_model.hpp"
#include "studio/studio_runner.hpp"
#include "studio/studio_frames.hpp"
#include "studio/studio_n64.hpp"
#include "studio/studio_snes.hpp"
#include "studio/studio_functions.hpp"
#include "studio/studio_theme.hpp"

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "imgui_impl_opengl3.h"
#include "imgui_impl_sdl3.h"
#if defined(IMGUI_ENABLE_FREETYPE)
#include "misc/freetype/imgui_freetype.h"
#endif

#include <SDL3/SDL.h>
#include <SDL3/SDL_dialog.h>
#include <SDL3/SDL_opengl.h>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

#define STB_IMAGE_IMPLEMENTATION
#include "stb_image.h"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using retcomm::studio::Platform;
using retcomm::studio::platform_display;
using retcomm::studio::platform_framework;
using retcomm::studio::platform_image_filter_ext;
using retcomm::studio::platform_image_filter_name;
using retcomm::studio::platform_image_label;
using retcomm::studio::platform_key;
using retcomm::studio::RunResult;
using retcomm::studio::StudioModel;
using retcomm::studio::Theme;

namespace {

fs::path find_asset_file(const char* kind, const char* filename, const fs::path& exe_dir) {
    std::error_code ec;
    auto try_file = [&](const fs::path& p) -> fs::path {
        if (p.empty()) return {};
        const fs::path c = fs::weakly_canonical(p, ec);
        const fs::path& use = (!ec && !c.empty()) ? c : p;
        if (fs::is_regular_file(use, ec)) return use;
        return {};
    };
    std::vector<fs::path> dirs;
    if (const char* env = std::getenv("RETCOMM_STUDIO_ASSETS")) {
        dirs.push_back(fs::path(env));
        dirs.push_back(fs::path(env) / kind);
    }
    if (const char* appdir = std::getenv("APPDIR")) {
        const fs::path ad(appdir);
        dirs.push_back(ad / "usr" / "share" / "retcomm-studio" / kind);
        dirs.push_back(ad / "usr" / "share" / "retcomm-studio" / "assets");
        dirs.push_back(ad / "usr" / "bin" / kind);
    }
    if (const char* base = SDL_GetBasePath()) {
        fs::path b(base);
        dirs.push_back(b / kind);
        dirs.push_back(b / ".." / "share" / "retcomm-studio" / kind);
        dirs.push_back(b / ".." / "share" / "retcomm-studio" / "assets");
        fs::path walk = b;
        for (int i = 0; i < 6 && !walk.empty(); ++i) {
            dirs.push_back(walk / "assets" / kind);
            dirs.push_back(walk / "assets");
            dirs.push_back(walk / kind);
            walk = walk.parent_path();
        }
    }
    if (!exe_dir.empty()) {
        dirs.push_back(exe_dir / kind);
        dirs.push_back(exe_dir / "assets" / kind);
        dirs.push_back(exe_dir / ".." / "assets" / kind);
    }
    for (const auto& d : dirs) {
        if (auto hit = try_file(d / filename); !hit.empty()) return hit;
        if (std::strcmp(kind, "fonts") == 0) {
            if (auto hit = try_file(d / filename); !hit.empty()) return hit;
        }
    }
    return {};
}

void load_fonts(const fs::path& exe_dir) {
    ImGuiIO& io = ImGui::GetIO();
#if defined(IMGUI_ENABLE_FREETYPE)
    io.Fonts->FontBuilderFlags |= ImGuiFreeTypeBuilderFlags_LoadColor;
#endif
    const fs::path regular = find_asset_file("fonts", "LatoLatin-Regular.ttf", exe_dir);
    ImFontConfig cfg;
    cfg.OversampleH = 2;
    cfg.OversampleV = 2;
    static const ImWchar kRanges[] = {0x0020, 0x00FF, 0x2010, 0x2027, 0};
    constexpr float kBody = 17.0f;
    bool loaded = false;
    if (!regular.empty()) {
        loaded = io.Fonts->AddFontFromFileTTF(regular.string().c_str(), kBody, &cfg, kRanges) !=
                 nullptr;
    }
    if (!loaded) {
        cfg.SizePixels = kBody;
        io.Fonts->AddFontDefault(&cfg);
    }
}

void accent_button(const Theme& th) {
    ImGui::PushStyleColor(ImGuiCol_Button, th.accent_button);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, th.accent_button_hovered);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, th.accent_button_active);
}

void accent_button_pop() { ImGui::PopStyleColor(3); }

// Layout helpers — ImGui default labels sit to the RIGHT of widgets, which
// overflows when SameLine chains follow. Keep labels on the left and size
// fields from remaining content width.
const char* imgui_visible_label(const char* label, std::string* scratch) {
    scratch->assign(label ? label : "");
    const auto hash = scratch->find("##");
    if (hash != std::string::npos) scratch->resize(hash);
    return scratch->c_str();
}

float widget_label_width(const char* label) {
    std::string scratch;
    const char* vis = imgui_visible_label(label, &scratch);
    return ImGui::CalcTextSize(vis).x + ImGui::GetStyle().FramePadding.x * 2.f;
}

float trailing_checkbox_width(const char* label) {
    std::string scratch;
    const char* vis = imgui_visible_label(label, &scratch);
    const ImGuiStyle& s = ImGui::GetStyle();
    return ImGui::GetFrameHeight() + s.ItemInnerSpacing.x + ImGui::CalcTextSize(vis).x +
           s.ItemSpacing.x;
}

// Colour AND wrap. ImGui ships TextColored and TextWrapped but not both, and
// an unwrapped explanatory paragraph widens the content region until the page
// scrolls sideways. Same helper as the Frames / SNES panes.
void wrapped(const ImVec4& col, const char* fmt, ...) IM_FMTARGS(2);
void wrapped(const ImVec4& col, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::TextWrappedV(fmt, args);
    ImGui::PopStyleColor();
    va_end(args);
}

void left_label(const char* label, float col_w) {
    ImGui::AlignTextToFramePadding();
    const float x0 = ImGui::GetCursorPosX();
    ImGui::TextUnformatted(label);
    ImGui::SameLine(0.f, 0.f);
    const float pad = col_w - (ImGui::GetCursorPosX() - x0);
    ImGui::Dummy(ImVec2(pad > 0.f ? pad : ImGui::GetStyle().ItemSpacing.x, 0.f));
    ImGui::SameLine();
}

// Returns true when the browse button is clicked (not on text edits).
// `text_committed`, when given, reports a finished edit of the field itself —
// typing a path by hand has to reach the same code the Browse button does, or
// half the ways of setting a path silently do not persist.
bool path_row(const char* id, const char* label, char* buf, size_t buf_n, float label_w,
              const char* browse_label, bool* text_committed = nullptr) {
    left_label(label, label_w);
    const float browse_w = widget_label_width(browse_label) + ImGui::GetStyle().ItemSpacing.x;
    float field_w = ImGui::GetContentRegionAvail().x - browse_w;
    if (field_w < 80.f) field_w = 80.f;
    ImGui::SetNextItemWidth(field_w);
    ImGui::InputText(id, buf, buf_n);
    if (text_committed) *text_committed = ImGui::IsItemDeactivatedAfterEdit();
    ImGui::SameLine();
    return ImGui::Button(browse_label);
}

void field_row(const char* id, const char* label, char* buf, size_t buf_n, float label_w) {
    left_label(label, label_w);
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
    ImGui::InputText(id, buf, buf_n);
}

std::string cached_cmake_generator(const std::string& root, const char* build_dir) {
    if (root.empty() || !build_dir || !build_dir[0]) return {};
    fs::path bdir(build_dir);
    if (!bdir.is_absolute()) bdir = fs::path(root) / bdir;
    std::ifstream in(bdir / "CMakeCache.txt");
    if (!in) return {};
    std::string line;
    while (std::getline(in, line)) {
        constexpr const char* kKey = "CMAKE_GENERATOR:INTERNAL=";
        if (line.rfind(kKey, 0) == 0) return line.substr(std::strlen(kKey));
    }
    return {};
}

bool generator_combo(const char* id, char* buf, size_t buf_n, float label_w, const std::string& root,
                     const char* build_dir) {
    struct Opt {
        const char* value;
        const char* label;
    };
    static const Opt kOpts[] = {
        {"", "Auto"},
        {"Ninja", "Ninja"},
        {"Unix Makefiles", "Unix Makefiles"},
#if defined(_WIN32)
        {"Visual Studio 17 2022", "Visual Studio 17 2022"},
#endif
    };
    left_label("Generator", label_w);
    const std::string cached = cached_cmake_generator(root, build_dir);
    char preview[160];
    if (!buf[0]) {
        if (!cached.empty()) {
            std::snprintf(preview, sizeof(preview), "Auto (%s)", cached.c_str());
        } else {
            std::snprintf(preview, sizeof(preview), "Auto");
        }
    } else {
        std::snprintf(preview, sizeof(preview), "%s", buf);
        for (const auto& o : kOpts) {
            if (o.value[0] && std::strcmp(buf, o.value) == 0) {
                std::snprintf(preview, sizeof(preview), "%s", o.label);
                break;
            }
        }
    }
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
    bool changed = false;
    if (ImGui::BeginCombo(id, preview, ImGuiComboFlags_HeightLarge)) {
        bool have_cur = (buf[0] == '\0');
        for (const auto& o : kOpts) {
            if (o.value[0] && std::strcmp(buf, o.value) == 0) {
                have_cur = true;
                break;
            }
        }
        if (buf[0] && !have_cur) {
            if (ImGui::Selectable(buf, true)) changed = false;
            ImGui::Separator();
        }
        for (const auto& o : kOpts) {
            const bool sel = (std::strcmp(buf, o.value) == 0);
            const char* lab = o.label;
            char auto_lab[160];
            if (!o.value[0] && !cached.empty()) {
                std::snprintf(auto_lab, sizeof(auto_lab), "Auto (%s)", cached.c_str());
                lab = auto_lab;
            }
            if (ImGui::Selectable(lab, sel)) {
                std::snprintf(buf, buf_n, "%s", o.value);
                changed = true;
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
        ImGui::SetTooltip(
            "CMake -G. Auto keeps the generator already in this build dir "
            "(else Ninja if ninja is on PATH, otherwise Unix Makefiles).\n"
            "Switching Ninja ↔ Unix Makefiles on an existing dir needs a matching "
            "choice here, or a wiped CMakeCache.txt / new build dir.");
    }
    return changed;
}

void int_field_row(const char* id, const char* label, int* v, float label_w, float field_w = 100.f) {
    left_label(label, label_w);
    ImGui::SetNextItemWidth(field_w);
    ImGui::InputInt(id, v);
}

// Players 1..8 as a wider combo (replaces InputInt +/-).
bool players_combo(const char* id, int* players, float width = 140.f) {
    if (*players < 1) *players = 1;
    if (*players > 8) *players = 8;
    char preview[8];
    std::snprintf(preview, sizeof(preview), "%d", *players);
    ImGui::SetNextItemWidth(width);
    bool changed = false;
    if (ImGui::BeginCombo(id, preview, ImGuiComboFlags_HeightLarge)) {
        for (int p = 1; p <= 8; ++p) {
            char lab[8];
            std::snprintf(lab, sizeof(lab), "%d", p);
            const bool sel = (*players == p);
            if (ImGui::Selectable(lab, sel)) {
                *players = p;
                changed = true;
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    return changed;
}

// Bulk parallel jobs 1..4 (dropdown; avoids InputInt +/-).
bool jobs_combo(const char* id, int* jobs, float width = 100.f) {
    if (*jobs < 1) *jobs = 1;
    if (*jobs > 4) *jobs = 4;
    char preview[8];
    std::snprintf(preview, sizeof(preview), "%d", *jobs);
    ImGui::SetNextItemWidth(width);
    bool changed = false;
    if (ImGui::BeginCombo(id, preview, ImGuiComboFlags_HeightLarge)) {
        for (int j = 1; j <= 4; ++j) {
            char lab[8];
            std::snprintf(lab, sizeof(lab), "%d", j);
            const bool sel = (*jobs == j);
            if (ImGui::Selectable(lab, sel)) {
                *jobs = j;
                changed = true;
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    return changed;
}

void apply_window_icon(SDL_Window* window, const fs::path& exe_dir) {
    if (!window) return;
    const fs::path png = find_asset_file("", "retcomm-studio.png", exe_dir);
    if (png.empty()) return;
    int w = 0, h = 0, comp = 0;
    stbi_uc* pixels = stbi_load(png.string().c_str(), &w, &h, &comp, 4);
    if (!pixels || w <= 0 || h <= 0) {
        if (pixels) stbi_image_free(pixels);
        return;
    }
    SDL_Surface* surf =
        SDL_CreateSurfaceFrom(w, h, SDL_PIXELFORMAT_RGBA32, pixels, w * 4);
    if (surf) {
        SDL_SetWindowIcon(window, surf);
        SDL_DestroySurface(surf);
    }
    stbi_image_free(pixels);
}

bool branch_combo(const char* id, const char* label, char* buf, size_t buf_n, float label_w,
                  const std::vector<std::string>& items, float width = 220.f) {
    left_label(label, label_w);
    ImGui::SetNextItemWidth(width > 0.f ? width : ImGui::GetContentRegionAvail().x);
    const char* preview = buf[0] ? buf : "(select…)";
    bool changed = false;
    if (ImGui::BeginCombo(id, preview, ImGuiComboFlags_HeightLarge)) {
        bool have_cur = false;
        for (const auto& s : items) {
            if (s == buf) {
                have_cur = true;
                break;
            }
        }
        if (buf[0] && !have_cur) {
            if (ImGui::Selectable(buf, true)) changed = false;
            ImGui::Separator();
        }
        if (items.empty()) {
            ImGui::TextDisabled("(no branches yet — Refresh branches)");
        }
        for (const auto& s : items) {
            const bool sel = (s == buf);
            if (ImGui::Selectable(s.c_str(), sel)) {
                std::snprintf(buf, buf_n, "%s", s.c_str());
                changed = true;
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    return changed;
}

void load_branches_json(StudioModel& model, const std::string& json_text, const std::string& root) {
    try {
        auto j = nlohmann::json::parse(json_text);
        auto fill = [](std::vector<std::string>& dst, const nlohmann::json& arr) {
            dst.clear();
            if (!arr.is_array()) return;
            for (const auto& v : arr) {
                if (v.is_string()) {
                    const std::string s = v.get<std::string>();
                    if (!s.empty()) dst.push_back(s);
                }
            }
        };
        auto set_cur = [](char* buf, size_t n, const std::string& s) {
            if (s.empty()) return;
            std::snprintf(buf, n, "%s", s.c_str());
        };
        std::lock_guard<std::mutex> lock(model.mu);
        fill(model.branches_game, j["game"]);
        // "framework" is the platform-neutral alias the CLI always emits;
        // "psxrecomp" is the pre-SNES spelling, kept as a fallback so an older
        // toolkit still populates the dropdown.
        fill(model.branches_psx,
             j.contains("framework") ? j["framework"] : j["psxrecomp"]);
        fill(model.branches_ui, j["recomp-ui"]);
        fill(model.branches_net, j["recomp-net"]);
        fill(model.branches_rb, j["rbengine"]);
        model.branches_root = root;
        model.branches_loading = false;
        // Pre-select live checkouts for the Git page dropdowns.
        if (!root.empty() && j.contains("current") && j["current"].is_object()) {
            const auto& cur = j["current"];
            set_cur(model.git_branch, sizeof(model.git_branch), cur.value("game", ""));
            set_cur(model.git_psx_branch, sizeof(model.git_psx_branch),
                    cur.value("framework", cur.value("psxrecomp", "")));
            set_cur(model.git_ui_branch, sizeof(model.git_ui_branch), cur.value("recomp-ui", ""));
            set_cur(model.git_net_branch, sizeof(model.git_net_branch),
                    cur.value("recomp-net", ""));
            set_cur(model.git_rb_branch, sizeof(model.git_rb_branch), cur.value("rbengine", ""));
        } else if (!model.git_branch[0] && !model.branches_game.empty()) {
            std::snprintf(model.git_branch, sizeof(model.git_branch), "%s",
                          model.branches_game.front().c_str());
        }
    } catch (const std::exception& ex) {
        model.branches_loading = false;
        model.append_log(std::string("[FAIL] branches parse: ") + ex.what());
    }
}

void refresh_branches(StudioModel& model, bool fetch = false) {
    if (model.branches_loading || model.busy.load()) return;
    model.branches_loading = true;
    const std::string root = model.selected_root();
    std::vector<std::string> args = {"git", "branches", "--json"};
    if (!root.empty()) {
        args.push_back("--root");
        args.push_back(root);
    }
    if (fetch) args.push_back("--fetch");
    retcomm::studio::run_project_studio_async(
        model, args,
        [&model, root](RunResult r) {
            if (!r.ok() && r.stdout_text.empty()) {
                model.branches_loading = false;
                model.append_log("[FAIL] git branches: " + r.stderr_text);
                return;
            }
            load_branches_json(model, r.stdout_text, root);
            model.set_status(root.empty() ? "Default module branches loaded"
                                          : "Branch lists updated");
        },
        false);
}

// Place a checkbox on the current line, wrapping if it would overflow.
void checkbox_wrapped(const char* label, bool* v) {
    const float need = trailing_checkbox_width(label);
    if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x &&
        ImGui::GetContentRegionAvail().x < need)
        ImGui::NewLine();
    ImGui::Checkbox(label, v);
    ImGui::SameLine();
}

void end_wrapped_line() {
    if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x) ImGui::NewLine();
}

void text_clipped(const char* text, const ImVec4& col) {
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::PushTextWrapPos(0.f); // wrap to window edge
    ImGui::TextUnformatted(text);
    ImGui::PopTextWrapPos();
    ImGui::PopStyleColor();
}

struct DialogCtx {
    StudioModel* model = nullptr;
    // Owns its target, deliberately. This was a `const char*`, which is safe
    // only while every caller passes a string literal — and the multi-disc
    // rows do not: they build "np_disc2".."np_discN" in a `char[32]` local
    // that dies with the loop iteration. The dialog callback then read a
    // dangling pointer whose stack slot the NEXT iteration had overwritten,
    // so picking a file for Disc 2 filed it under the last disc rendered.
    // Copying here fixes every present and future caller instead of that one.
    std::string target;
};

void SDLCALL folder_callback(void* userdata, const char* const* filelist, int /*filter*/) {
    auto* ctx = static_cast<DialogCtx*>(userdata);
    if (!ctx || !ctx->model) return;
    {
        std::lock_guard<std::mutex> lock(ctx->model->pick_mu);
        if (filelist && filelist[0]) {
            ctx->model->pending_folder = filelist[0];
            ctx->model->pending_pick_target = ctx->target;
        }
    }
    delete ctx;
}

void SDLCALL file_callback(void* userdata, const char* const* filelist, int /*filter*/) {
    auto* ctx = static_cast<DialogCtx*>(userdata);
    if (!ctx || !ctx->model) return;
    {
        std::lock_guard<std::mutex> lock(ctx->model->pick_mu);
        ctx->model->file_pick_busy = false;
        if (filelist && filelist[0]) {
            ctx->model->pending_file = filelist[0];
            ctx->model->pending_pick_target = ctx->target;
        }
    }
    delete ctx;
}

void pick_folder(StudioModel& model, SDL_Window* window, const char* target) {
    auto* ctx = new DialogCtx{&model, target};
    SDL_ShowOpenFolderDialog(folder_callback, ctx, window, nullptr, false);
}

void pick_file(StudioModel& model, SDL_Window* window, const char* target, const char* filter_name,
               const char* pattern) {
    {
        std::lock_guard<std::mutex> lock(model.pick_mu);
        if (model.file_pick_busy) return;
        model.file_pick_busy = true;
    }
    auto* ctx = new DialogCtx{&model, target};
    SDL_DialogFileFilter filters[1];
    filters[0].name = filter_name;
    filters[0].pattern = pattern;
    SDL_ShowOpenFileDialog(file_callback, ctx, window, filters, 1, nullptr, false);
}

void begin_export_activity_log(StudioModel& model, SDL_Window* window) {
    if (!window) return;
    {
        std::lock_guard<std::mutex> lock(model.pick_mu);
        if (model.file_pick_busy) return;
        model.file_pick_busy = true;
    }
#if defined(_WIN32)
    const char* home = std::getenv("USERPROFILE");
#else
    const char* home = std::getenv("HOME");
#endif
    static std::string default_loc;
    default_loc = (home && *home) ? (fs::path(home) / "retcomm-studio.log").string()
                                  : std::string("retcomm-studio.log");
    static SDL_DialogFileFilter filters[1];
    filters[0].name = "Log files";
    filters[0].pattern = "log";
    auto* ctx = new DialogCtx{&model, "export_log"};
    SDL_ShowSaveFileDialog(file_callback, ctx, window, filters, 1, default_loc.c_str());
}

// Native "Save as…" for a packaged build zip (local host build or MinGW cross).
void begin_export_zip(StudioModel& model, SDL_Window* window, const std::string& src_zip) {
    if (!window || src_zip.empty()) return;
    {
        std::lock_guard<std::mutex> lock(model.pick_mu);
        if (model.file_pick_busy) return;
        model.file_pick_busy = true;
        model.export_zip_src = src_zip;
    }
#if defined(_WIN32)
    const char* home = std::getenv("USERPROFILE");
#else
    const char* home = std::getenv("HOME");
#endif
    static std::string default_loc;
    const fs::path base = fs::path(src_zip).filename();
    if (home && *home)
        default_loc = (fs::path(home) / "Downloads" / base).string();
    else
        default_loc = base.string();
    static SDL_DialogFileFilter filters[1];
    filters[0].name = "Zip archives";
    filters[0].pattern = "zip";
    auto* ctx = new DialogCtx{&model, "export_zip"};
    SDL_ShowSaveFileDialog(file_callback, ctx, window, filters, 1, default_loc.c_str());
}

std::string parse_zip_from_output(const std::string& text) {
    // Prefer the last BUNDLE_ZIP= / MINGW_ZIP= line; fall back to the last JSON
    // object with a "zip" field (both packagers print the same trailer).
    std::string best;
    std::size_t pos = 0;
    while (pos < text.size()) {
        const std::size_t end = text.find('\n', pos);
        const std::string line =
            text.substr(pos, end == std::string::npos ? std::string::npos : end - pos);
        pos = (end == std::string::npos) ? text.size() : end + 1;
        bool matched = false;
        for (const char* prefix : {"BUNDLE_ZIP=", "MINGW_ZIP="}) {
            if (line.rfind(prefix, 0) != 0) continue;
            best = line.substr(std::strlen(prefix));
            while (!best.empty() && (best.back() == '\r' || best.back() == ' ')) best.pop_back();
            matched = true;
            break;
        }
        if (matched) continue;
        if (!line.empty() && line.front() == '{') {
            try {
                auto j = nlohmann::json::parse(line);
                if (j.contains("zip") && j["zip"].is_string()) {
                    const std::string z = j["zip"].get<std::string>();
                    if (!z.empty()) best = z;
                }
            } catch (...) {
            }
        }
    }
    return best;
}

// Write the Migrate tab's game image into the repo index.
//
// It used to live only in the model, so a ROM picked here was forgotten the
// moment Studio restarted — and the Build tab's "Regenerate C from ROM", which
// reads the same field, ran without a --rom against a repo whose ROM sits
// outside the working tree. Nothing else records where that ROM is: the
// scaffolder bakes its *filename* into tools/regen.sh and its directory
// nowhere.
void persist_selected_image(StudioModel& model) {
    const std::string root = model.selected_root();
    if (root.empty()) return;
    const std::string image = model.disc_cue;
    std::vector<std::string> args;
    if (image.empty()) {
        args = {"repos", "clear-cue", "--path", root, "--json"};
    } else {
        args = {"repos", "set-cue", "--path", root, "--cue", image, "--json"};
    }
    retcomm::studio::run_project_studio_async(
        model, std::move(args),
        [&model, image](RunResult r) {
            std::string err;
            if (!r.ok() || !retcomm::studio::load_repos_from_json(model, r.stdout_text, &err)) {
                model.append_log("[FAIL] Remember " +
                                 std::string(platform_image_label(model.platform)) + ": " +
                                 (err.empty() ? r.stderr_text : err));
                return;
            }
            model.append_log(image.empty() ? "Cleared game image for this repo"
                                           : "Remembered game image: " + image);
        },
        false);
}

void apply_pending_picks(StudioModel& model) {
    std::string folder, file, target;
    {
        std::lock_guard<std::mutex> lock(model.pick_mu);
        folder.swap(model.pending_folder);
        file.swap(model.pending_file);
        target.swap(model.pending_pick_target);
    }
    if (!folder.empty()) {
        if (target == "repo_add") {
            const std::string added = folder;
            retcomm::studio::run_project_studio_async(
                model, {"repos", "add", "--path", added, "--json"},
                [&model, added](RunResult r) {
                    std::string err;
                    if (r.ok() && retcomm::studio::load_repos_from_json(model, r.stdout_text, &err)) {
                        model.set_status("Repo added");
                        model.select_repo_by_path(added);
                    } else {
                        model.append_log("[FAIL] repos add: " + (err.empty() ? r.stderr_text : err));
                    }
                },
                false);
        } else if (target == "np_parent") {
            std::snprintf(model.np_parent, sizeof(model.np_parent), "%s", folder.c_str());
        }
    }
    if (!file.empty()) {
        if (target == "disc") {
            std::snprintf(model.disc_cue, sizeof(model.disc_cue), "%s", file.c_str());
            persist_selected_image(model);
        } else if (target == "np_disc") {
            std::snprintf(model.np_disc, sizeof(model.np_disc), "%s", file.c_str());
        } else if (target.rfind("np_disc", 0) == 0 && target.size() == 8 &&
                   target[7] >= '2' && target[7] <= '0' + StudioModel::kMaxDiscs) {
            // "np_disc2".."np_disc<kMaxDiscs>" — discs 2..N of a PSX set.
            const int idx = (target[7] - '0') - 2;
            std::snprintf(model.np_disc_extra[idx], sizeof(model.np_disc_extra[idx]),
                          "%s", file.c_str());
        } else if (target == "np_bios") {
            std::snprintf(model.np_bios, sizeof(model.np_bios), "%s", file.c_str());
        } else if (target == "build_scph") {
            std::snprintf(model.gen_scph_path, sizeof(model.gen_scph_path), "%s", file.c_str());
            model.gen_bios_mode = 1;
            model.gen_popup_open = true;
        } else if (target == "export_log") {
            std::string body;
            {
                std::lock_guard<std::mutex> lock(model.mu);
                for (const auto& line : model.log_lines) {
                    if (!body.empty()) body.push_back('\n');
                    body += line;
                }
            }
            if (body.empty()) body = "(no activity yet)";
            std::ofstream out(file, std::ios::binary | std::ios::trunc);
            if (!out) {
                model.append_log("[FAIL] Export activity log: cannot write " + file);
                model.set_status("Export activity log failed");
            } else {
                out.write(body.data(), static_cast<std::streamsize>(body.size()));
                if (!body.empty() && body.back() != '\n') out.put('\n');
                if (!out) {
                    model.append_log("[FAIL] Export activity log while writing " + file);
                    model.set_status("Export activity log failed");
                } else {
                    model.append_log("[OK] Exported activity log → " + file);
                    model.set_status("Exported activity log");
                }
            }
        } else if (target == "export_zip") {
            std::string src;
            {
                std::lock_guard<std::mutex> lock(model.pick_mu);
                src = model.export_zip_src;
            }
            if (src.empty() || !fs::is_regular_file(src)) {
                model.append_log("[FAIL] Export: packaged zip missing: " + src);
                model.set_status("Export failed");
            } else {
                std::error_code ec;
                fs::path dest(file);
                if (dest.extension() != ".zip") dest += ".zip";
                fs::create_directories(dest.parent_path(), ec);
                fs::copy_file(src, dest, fs::copy_options::overwrite_existing, ec);
                if (ec) {
                    model.append_log("[FAIL] Export copy: " + ec.message() + " → " +
                                     dest.string());
                    model.set_status("Export failed");
                } else {
                    model.append_log("[OK] Exported build zip → " + dest.string());
                    model.set_status("Exported build zip");
                }
            }
        }
    }
}

void refresh_repos(StudioModel& model) {
    // Which index to read is a property of the platform. Before the picker is
    // answered there is no answer, and loading "the PSX one for now" races the
    // pick: a startup update check that finishes after a SNES pick would
    // overwrite the SNES list with PSX repos.
    if (model.platform == Platform::None) return;
    retcomm::studio::run_project_studio_async(
        model, {"repos", "list", "--json"},
        [&model](RunResult r) {
            std::string err;
            if (!retcomm::studio::load_repos_from_json(model, r.stdout_text, &err)) {
                model.append_log("[FAIL] repos list: " + (err.empty() ? r.stderr_text : err));
                model.set_status("Failed to load repo index");
            } else {
                model.set_status("Repo index loaded");
            }
        },
        false);
}

// After catalog sync: reload in_catalog flags; optionally re-apply Bulk "Catalog only".
void refresh_repos_and_catalog_filters(StudioModel& model, bool reapply_bulk_catalog) {
    if (model.platform == Platform::None) return;  // see refresh_repos()
    retcomm::studio::run_project_studio_async(
        model, {"repos", "list", "--json"},
        [&model, reapply_bulk_catalog](RunResult r) {
            std::string err;
            if (!retcomm::studio::load_repos_from_json(model, r.stdout_text, &err)) {
                model.append_log("[FAIL] repos list: " + (err.empty() ? r.stderr_text : err));
                model.set_status("Failed to load repo index");
                return;
            }
            model.set_status("Repo index loaded");
            if (!reapply_bulk_catalog) return;
            retcomm::studio::run_project_studio_async(
                model, {"repos", "filter-catalog", "--json"},
                [&model](RunResult fr) {
                    try {
                        auto j = nlohmann::json::parse(fr.stdout_text);
                        const auto note = j.value("note", "");
                        for (auto& kv : model.bulk_selected) kv.second = false;
                        int n = 0;
                        if (j.contains("paths") && j["paths"].is_array()) {
                            for (const auto& p : j["paths"]) {
                                const std::string path = p.get<std::string>();
                                auto it = model.bulk_selected.find(path);
                                if (it != model.bulk_selected.end()) {
                                    it->second = true;
                                    ++n;
                                }
                            }
                        }
                        if (!note.empty()) model.append_log(note);
                        model.set_status("Catalog filters applied (" + std::to_string(n) +
                                         " bulk target(s))");
                    } catch (const std::exception& ex) {
                        model.append_log(std::string("[FAIL] filter-catalog: ") + ex.what());
                    }
                },
                false);
        },
        false);
}

void handle_updates_check_result(StudioModel& model, const RunResult& r) {
    try {
        auto j = nlohmann::json::parse(r.stdout_text);
        if (j.value("skipped", false)) {
            model.append_log(j.value("message", "Startup update check skipped."));
            // Catalog was not synced; keep the earlier repos list / bulk ticks.
            return;
        }
        const auto studio = j.value("studio", nlohmann::json::object());
        const auto tc = j.value("toolchain", nlohmann::json::object());
        const auto cat = j.value("catalog", nlohmann::json::object());
        model.update_studio_avail = studio.value("available", false);
        model.update_toolchain_avail = tc.value("available", false);
        std::string msg = j.value("message", "");
        if (msg.empty()) {
            msg = studio.value("message", "");
            const auto tm = tc.value("message", "");
            if (!tm.empty()) {
                if (!msg.empty()) msg += " · ";
                msg += tm;
            }
            const auto cm = cat.value("message", "");
            if (!cm.empty()) {
                if (!msg.empty()) msg += " · ";
                msg += cm;
            }
        }
        model.append_log(msg.empty() ? "Update check complete." : msg);
        if (model.update_studio_avail || model.update_toolchain_avail) {
            model.update_prompt_msg = msg;
            model.update_prompt_open = true;
        }
        // Always refresh in_catalog for the dropdown; re-tick Bulk catalog filter
        // when the zip was installed or the cache was first created this check.
        const bool catalog_changed =
            cat.value("downloaded", false) || !cat.value("skipped", true);
        refresh_repos_and_catalog_filters(model, /*reapply_bulk_catalog=*/catalog_changed);
    } catch (const std::exception& ex) {
        model.append_log(std::string("Update check: ") + ex.what());
        refresh_repos(model);
    }
}

std::vector<std::string> migrate_common_args(StudioModel& model) {
    std::vector<std::string> a;
    if (model.disc_cue[0]) {
        a.push_back("--disc");
        a.push_back(model.disc_cue);
    }
    a.push_back("--players");
    a.push_back(std::to_string(model.players));
    if (model.zip_prefix[0]) {
        a.push_back("--zip-prefix");
        a.push_back(model.zip_prefix);
    }
    if (model.game_id[0]) {
        a.push_back("--game-id");
        a.push_back(model.game_id);
    }
    if (model.github_owner[0]) {
        a.push_back("--github-owner");
        a.push_back(model.github_owner);
    }
    if (model.github_repo[0]) {
        a.push_back("--github-repo");
        a.push_back(model.github_repo);
    }
    if (model.migrate_netplay) a.push_back("--enable-netplay");
    if (!model.migrate_ci) a.push_back("--no-ci");
    // probe_disc = bool(disc) and not no_probe — pass --no-probe unless user wants probe.
    if (!model.migrate_probe) a.push_back("--no-probe");
    if (!model.migrate_readme) a.push_back("--no-readme");
    if (model.migrate_force) a.push_back("--force");
    if (model.migrate_dry_run) a.push_back("--dry-run");
    return a;
}

void do_audit_plan(StudioModel& model) {
    const std::string root = model.selected_root();
    if (root.empty()) {
        model.append_log("[FAIL] No game repo selected");
        return;
    }
    std::vector<std::string> audit_args = {"audit", "--root", root, "--json"};
    // The README switch gates the check as well as the op, so the audit has
    // to hear about it — otherwise the pane shows a warning for work the
    // plan below it will never do.
    if (!model.migrate_readme) audit_args.push_back("--no-readme");
    retcomm::studio::run_project_studio_async(
        model, audit_args,
        [&model, root](RunResult r) {
            std::string err;
            if (!retcomm::studio::load_audit_from_json(model, r.stdout_text, &err)) {
                model.append_log("[FAIL] audit parse: " + (err.empty() ? r.stderr_text : err));
                return;
            }
            model.append_log("Audited " + root + " → " + model.audit_layout);
            model.set_status("Layout: " + model.audit_layout);
            std::vector<std::string> plan_args = {"plan", "--root", root, "--json"};
            auto common = migrate_common_args(model);
            // plan should not force dry-run into options oddly; dry-run is apply-only
            common.erase(std::remove(common.begin(), common.end(), "--dry-run"), common.end());
            plan_args.insert(plan_args.end(), common.begin(), common.end());
            retcomm::studio::run_project_studio_async(
                model, plan_args,
                [&model](RunResult pr) {
                    std::string perr;
                    if (!retcomm::studio::load_plan_from_json(model, pr.stdout_text, &perr)) {
                        model.append_log("[FAIL] plan parse: " +
                                         (perr.empty() ? pr.stderr_text : perr));
                    }
                },
                false);
        },
        false);
}

void do_apply(StudioModel& model) {
    const std::string root = model.selected_root();
    if (root.empty()) return;
    std::vector<std::string> only;
    {
        std::lock_guard<std::mutex> lock(model.mu);
        for (const auto& s : model.plan_steps)
            if (s.selected) only.push_back(s.op_id);
    }
    if (only.empty()) {
        model.append_log("[FAIL] No plan steps selected");
        return;
    }
    std::string only_csv;
    for (size_t i = 0; i < only.size(); ++i) {
        if (i) only_csv += ",";
        only_csv += only[i];
    }
    std::vector<std::string> args = {"apply", "--root", root, "--json", "--only", only_csv};
    auto common = migrate_common_args(model);
    args.insert(args.end(), common.begin(), common.end());
    model.append_log(std::string("--- ") + (model.migrate_dry_run ? "DRY-RUN" : "APPLY") + " (" +
                     std::to_string(only.size()) + " ops) ---");
    retcomm::studio::run_project_studio_async(model, args, [&model](RunResult r) {
        model.set_status(r.ok() ? "Apply finished OK" : "Apply finished with errors");
        if (!model.migrate_dry_run && r.ok()) do_audit_plan(model);
    });
}

// ---------------------------------------------------------------------------
// Platform picker
//
// The console icons are drawn rather than loaded. Two reasons: Studio already
// ships an icon-less assets/ dir and adding console art to it means shipping
// (and licensing) photographs of hardware, and vector art scales with the
// user's font/DPI where a bitmap at one size does not. They are deliberately
// schematic — a top-down silhouette in the console's own colours, which is
// what makes them readable at a glance without pretending to be product shots.
// ---------------------------------------------------------------------------
// Both icons draw into a 4:3 box fitted inside the space given, so a wide card
// does not stretch a console into a hi-fi component.
ImVec4 fit_icon_box(const ImVec2& p, float w, float h) {
    const float want = 4.f / 3.f;
    float iw = w, ih = w / want;
    if (ih > h) {
        ih = h;
        iw = h * want;
    }
    return ImVec4(p.x + (w - iw) * 0.5f, p.y + (h - ih) * 0.5f, iw, ih);
}

void draw_psx_icon(ImDrawList* dl, const ImVec2& p, float w, float h) {
    const ImVec4 box = fit_icon_box(p, w, h);
    const float x = box.x, y = box.y, bw = box.z, bh = box.w;

    const ImU32 body = IM_COL32(214, 213, 206, 255);
    const ImU32 body_dk = IM_COL32(176, 175, 168, 255);
    const ImU32 recess = IM_COL32(150, 149, 144, 255);
    const ImU32 shadow = IM_COL32(120, 119, 114, 255);
    const ImU32 hole = IM_COL32(62, 62, 60, 255);

    // Chassis, seen from above: near-square with a shallow front lip.
    const ImVec2 a(x, y);
    const ImVec2 b(x + bw, y + bh * 0.86f);
    dl->AddRectFilled(a, b, body, bh * 0.10f);
    dl->AddRectFilled(ImVec2(a.x, b.y - bh * 0.20f), b, body_dk, bh * 0.10f,
                      ImDrawFlags_RoundCornersBottom);

    // The disc lid. Large and central — it is the whole silhouette of a PS1
    // from above, and shrinking it is what makes the shape read as a VCR.
    const ImVec2 c(x + bw * 0.42f, y + bh * 0.36f);
    const float r = bh * 0.30f;
    dl->AddCircleFilled(c, r * 1.06f, shadow, 56);
    dl->AddCircleFilled(c, r, recess, 56);
    dl->AddCircleFilled(c, r * 0.62f, body_dk, 48);
    dl->AddCircleFilled(c, r * 0.20f, hole, 24);

    // Open / power / reset column on the right shoulder.
    for (int i = 0; i < 3; ++i) {
        const float ry = y + bh * (0.13f + 0.17f * static_cast<float>(i));
        dl->AddRectFilled(ImVec2(x + bw * 0.80f, ry),
                          ImVec2(x + bw * 0.93f, ry + bh * 0.10f), body_dk, bh * 0.03f);
    }
    // Two controller ports, two memory-card slots, along the front lip.
    for (int i = 0; i < 2; ++i) {
        const float cx = x + bw * (0.28f + 0.44f * static_cast<float>(i));
        dl->AddRectFilled(ImVec2(cx - bw * 0.11f, b.y - bh * 0.15f),
                          ImVec2(cx + bw * 0.11f, b.y - bh * 0.06f), hole, bh * 0.03f);
        dl->AddRectFilled(ImVec2(cx - bw * 0.07f, b.y - bh * 0.05f),
                          ImVec2(cx + bw * 0.07f, b.y - bh * 0.01f), shadow, bh * 0.02f);
    }
}

void draw_snes_icon(ImDrawList* dl, const ImVec2& p, float w, float h) {
    const ImVec4 box = fit_icon_box(p, w, h);
    const float x = box.x, y = box.y, bw = box.z, bh = box.w;

    const ImU32 body = IM_COL32(212, 210, 200, 255);
    const ImU32 body_dk = IM_COL32(172, 170, 162, 255);
    const ImU32 shadow = IM_COL32(140, 138, 132, 255);
    const ImU32 slot = IM_COL32(58, 57, 57, 255);
    const ImU32 purple = IM_COL32(122, 104, 182, 255);
    const ImU32 purple_dk = IM_COL32(88, 74, 140, 255);
    const ImU32 hole = IM_COL32(62, 62, 60, 255);

    const ImVec2 a(x, y);
    const ImVec2 b(x + bw, y + bh * 0.88f);
    dl->AddRectFilled(a, b, body, bh * 0.13f);

    // Raised centre deck the cartridge drops into — the SNES's one big
    // rounded block, flanked by the ribbed shoulders.
    dl->AddRectFilled(ImVec2(x + bw * 0.18f, y + bh * 0.03f),
                      ImVec2(x + bw * 0.82f, y + bh * 0.60f), shadow, bh * 0.12f);
    dl->AddRectFilled(ImVec2(x + bw * 0.19f, y + bh * 0.03f),
                      ImVec2(x + bw * 0.81f, y + bh * 0.57f), body_dk, bh * 0.12f);
    dl->AddRectFilled(ImVec2(x + bw * 0.28f, y + bh * 0.12f),
                      ImVec2(x + bw * 0.72f, y + bh * 0.24f), slot, bh * 0.02f);

    // Purple power / eject. Oversized on purpose: the colour is what tells a
    // SNES apart from every other grey box at this size.
    dl->AddRectFilled(ImVec2(x + bw * 0.28f, y + bh * 0.34f),
                      ImVec2(x + bw * 0.46f, y + bh * 0.50f), purple, bh * 0.04f);
    dl->AddRectFilled(ImVec2(x + bw * 0.54f, y + bh * 0.34f),
                      ImVec2(x + bw * 0.72f, y + bh * 0.50f), purple_dk, bh * 0.04f);

    // Vent ribs on both shoulders.
    for (int i = 0; i < 4; ++i) {
        const float ry = y + bh * (0.12f + 0.11f * static_cast<float>(i));
        dl->AddRectFilled(ImVec2(x + bw * 0.04f, ry),
                          ImVec2(x + bw * 0.15f, ry + bh * 0.045f), body_dk, bh * 0.02f);
        dl->AddRectFilled(ImVec2(x + bw * 0.85f, ry),
                          ImVec2(x + bw * 0.96f, ry + bh * 0.045f), body_dk, bh * 0.02f);
    }
    // Two controller ports along the front.
    for (int i = 0; i < 2; ++i) {
        const float cx = x + bw * (0.33f + 0.34f * static_cast<float>(i));
        dl->AddRectFilled(ImVec2(cx - bw * 0.08f, b.y - bh * 0.19f),
                          ImVec2(cx + bw * 0.08f, b.y - bh * 0.05f), hole, bh * 0.07f);
    }
}

void draw_n64_icon(ImDrawList* dl, const ImVec2& p, float w, float h) {
    const ImVec4 box = fit_icon_box(p, w, h);
    const float x = box.x, y = box.y, bw = box.z, bh = box.w;

    const ImU32 body = IM_COL32(64, 64, 66, 255);
    const ImU32 body_dk = IM_COL32(44, 44, 46, 255);
    const ImU32 body_lt = IM_COL32(86, 86, 89, 255);
    const ImU32 slot = IM_COL32(26, 26, 27, 255);
    const ImU32 hole = IM_COL32(30, 30, 31, 255);
    // The four port colours, which is the one cue that reads as "N64" at this
    // size the way the purple buttons read as "SNES".
    const ImU32 ports[4] = {
        IM_COL32(196, 60, 56, 255),   // red
        IM_COL32(232, 176, 52, 255),  // yellow
        IM_COL32(58, 138, 200, 255),  // blue
        IM_COL32(70, 164, 92, 255),   // green
    };

    const ImVec2 a(x, y);
    const ImVec2 b(x + bw, y + bh * 0.88f);
    dl->AddRectFilled(a, b, body, bh * 0.16f);

    // The trapezoid deck: the N64's silhouette is a rounded block whose top is
    // narrower than its base, with the cartridge slot cut into the middle.
    dl->AddRectFilled(ImVec2(x + bw * 0.16f, y + bh * 0.04f),
                      ImVec2(x + bw * 0.84f, y + bh * 0.58f), body_dk, bh * 0.13f);
    dl->AddRectFilled(ImVec2(x + bw * 0.20f, y + bh * 0.06f),
                      ImVec2(x + bw * 0.80f, y + bh * 0.54f), body_lt, bh * 0.13f);
    dl->AddRectFilled(ImVec2(x + bw * 0.31f, y + bh * 0.15f),
                      ImVec2(x + bw * 0.69f, y + bh * 0.30f), slot, bh * 0.02f);

    // Vent ribs on both shoulders, as on the SNES card.
    for (int i = 0; i < 4; ++i) {
        const float ry = y + bh * (0.10f + 0.11f * static_cast<float>(i));
        dl->AddRectFilled(ImVec2(x + bw * 0.03f, ry),
                          ImVec2(x + bw * 0.13f, ry + bh * 0.045f), body_dk, bh * 0.02f);
        dl->AddRectFilled(ImVec2(x + bw * 0.87f, ry),
                          ImVec2(x + bw * 0.97f, ry + bh * 0.045f), body_dk, bh * 0.02f);
    }

    // FOUR controller ports along the front — the console's own headline
    // feature, and what distinguishes this card from the SNES's two.
    for (int i = 0; i < 4; ++i) {
        const float cx = x + bw * (0.20f + 0.20f * static_cast<float>(i));
        dl->AddRectFilled(ImVec2(cx - bw * 0.065f, b.y - bh * 0.21f),
                          ImVec2(cx + bw * 0.065f, b.y - bh * 0.04f), hole, bh * 0.07f);
        dl->AddRectFilled(ImVec2(cx - bw * 0.045f, b.y - bh * 0.19f),
                          ImVec2(cx + bw * 0.045f, b.y - bh * 0.15f), ports[i], bh * 0.02f);
    }
}

// One big pickable card. Returns true when clicked.
bool platform_card(StudioModel& model, const Theme& th, Platform which, const char* title,
                   const char* subtitle, const ImVec2& size) {
    ImGui::PushID(static_cast<int>(which));
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const bool clicked = ImGui::InvisibleButton("##card", size);
    const bool hot = ImGui::IsItemHovered();
    const bool held = ImGui::IsItemActive();
    if (hot) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 lo = origin;
    const ImVec2 hi(origin.x + size.x, origin.y + size.y);
    const ImVec4 fill = held ? th.accent_dim : (hot ? th.panel_hovered : th.panel);
    dl->AddRectFilled(lo, hi, ImGui::GetColorU32(fill), th.radius_lg);
    dl->AddRect(lo, hi, ImGui::GetColorU32(hot ? th.accent : th.border), th.radius_lg, 0,
                hot ? 2.f : 1.f);

    // Icon occupies the upper block; text sits under it.
    const float pad = th.spacing_md;
    const float icon_w = size.x - pad * 2.f;
    const float icon_h = size.y * 0.46f;
    const ImVec2 icon_at(lo.x + pad, lo.y + pad);
    switch (which) {
    case Platform::SNES: draw_snes_icon(dl, icon_at, icon_w, icon_h); break;
    case Platform::N64: draw_n64_icon(dl, icon_at, icon_w, icon_h); break;
    default: draw_psx_icon(dl, icon_at, icon_w, icon_h); break;
    }

    const float title_w = ImGui::CalcTextSize(title).x;
    dl->AddText(ImVec2(lo.x + (size.x - title_w) * 0.5f, icon_at.y + icon_h + pad * 0.5f),
                ImGui::GetColorU32(th.text), title);
    const float sub_w = ImGui::CalcTextSize(subtitle).x;
    dl->AddText(ImVec2(lo.x + (size.x - sub_w) * 0.5f,
                       icon_at.y + icon_h + pad * 0.5f + ImGui::GetTextLineHeight() + 4.f),
                ImGui::GetColorU32(th.text_muted), subtitle);
    ImGui::PopID();
    (void)model;
    return clicked;
}

void draw_platform_picker(StudioModel& model, const Theme& th) {
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    // Three cards now, so the width each may claim shrinks with the count
    // rather than staying at the two-card figure and overflowing the window.
    const int card_count = 3;
    const float card_w = std::clamp(avail.x * 0.22f, 170.f, 280.f);
    const float card_h = card_w * 0.86f;
    const float gap = th.spacing_lg;
    const float block_w = card_w * static_cast<float>(card_count) +
                          gap * static_cast<float>(card_count - 1);

    const float head_h = ImGui::GetTextLineHeight() * 3.f + th.spacing_lg;
    const float block_h = head_h + card_h + th.spacing_lg + ImGui::GetTextLineHeight() * 2.f;
    if (avail.y > block_h) ImGui::Dummy(ImVec2(0, (avail.y - block_h) * 0.45f));

    auto centered = [&](const char* text, const ImVec4& col) {
        const float w = ImGui::CalcTextSize(text).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + std::max(0.f, (avail.x - w) * 0.5f));
        ImGui::TextColored(col, "%s", text);
    };
    centered("Choose a platform", th.accent);
    centered("Each console has its own repo list, framework and scaffolder.", th.text_muted);
    ImGui::Dummy(ImVec2(0, th.spacing_md));

    const float x0 = ImGui::GetCursorPosX() + std::max(0.f, (avail.x - block_w) * 0.5f);
    ImGui::SetCursorPosX(x0);
    // Oldest console first, so the row reads generationally as more are added.
    Platform chosen = Platform::None;
    if (platform_card(model, th, Platform::SNES, "Super Nintendo", "snesrecomp",
                      ImVec2(card_w, card_h)))
        chosen = Platform::SNES;
    ImGui::SameLine(0.f, gap);
    if (platform_card(model, th, Platform::PSX, "PlayStation", "psxrecomp",
                      ImVec2(card_w, card_h)))
        chosen = Platform::PSX;
    ImGui::SameLine(0.f, gap);
    if (platform_card(model, th, Platform::N64, "Nintendo 64", "n64lle",
                      ImVec2(card_w, card_h)))
        chosen = Platform::N64;

    ImGui::Dummy(ImVec2(0, th.spacing_md));
    centered("Switch consoles any time from the header.", th.text_muted);

    if (chosen != Platform::None) {
        model.platform = chosen;
        model.platform_pending = false;
        model.apply_platform_defaults();
        model.append_log(std::string("Platform: ") + platform_display(chosen) + " (" +
                         platform_framework(chosen) + ")");
        model.set_status(std::string(platform_display(chosen)) + " — loading repos…");
        // Catalog flags as well as the list: the startup check that would
        // normally have fetched them ran before a platform existed.
        refresh_repos_and_catalog_filters(model, /*reapply_bulk_catalog=*/false);
    }
}

void refresh_module_urls(StudioModel& model) {
    const std::string root = model.selected_root();
    if (root.empty()) return;
    model.git_urls_loading = true;
    model.git_urls_root = root;
    retcomm::studio::run_project_studio_async(
        model, {"git", "module-urls", "--root", root, "--json"},
        [&model](RunResult r) {
            model.git_urls_loading = false;
            std::string err;
            if (!r.ok() || !retcomm::studio::load_module_urls_from_json(
                               model, r.stdout_text, &err)) {
                model.append_log("[FAIL] Git settings: " +
                                 (err.empty() ? r.stderr_text : err));
            }
        },
        false);
}

// Where each module is fetched from and pushed to. Three settings answer that
// question and they are not the same one, so the dialog makes the user say
// which they mean rather than picking for them: a fork contributor wants their
// own clone repointed and .gitmodules left alone, while re-homing a project
// wants the tracked URL changed for everyone.
void draw_git_settings_popup(StudioModel& model, const Theme& th) {
    if (model.git_settings_open) ImGui::OpenPopup("Git settings###git_settings");
    ImGui::SetNextWindowSize(ImVec2(760.f, 0.f), ImGuiCond_Appearing);
    if (!ImGui::BeginPopupModal("Git settings###git_settings", &model.git_settings_open,
                                ImGuiWindowFlags_AlwaysAutoResize))
        return;

    const std::string root = model.selected_root();
    if (root.empty()) {
        ImGui::TextUnformatted("Select a game repo first.");
        if (ImGui::Button("Close", ImVec2(120.f, 0))) model.git_settings_open = false;
        ImGui::EndPopup();
        return;
    }

    ImGui::TextWrapped(
        "Point a module at your own fork so push and pull go there. Leave these "
        "alone to use the upstream repos read-only — Restore pinned and Advance "
        "pins work either way, and need no write access.");
    ImGui::Spacing();

    ImGui::TextUnformatted("Apply to");
    ImGui::SameLine();
    ImGui::RadioButton("This clone only", &model.git_url_scope, 0);
    ImGui::SameLine();
    ImGui::RadioButton("Commit to .gitmodules", &model.git_url_scope, 1);
    ImGui::TextColored(
        th.text_muted,
        model.git_url_scope == 0
            ? "Nothing tracked changes — your fork stays private to this checkout."
            : "Rewrites the tracked .gitmodules: everyone who clones this port gets "
              "your URL. Commit it on the Git tab afterwards.");
    ImGui::Spacing();

    ImGui::BeginDisabled(model.busy.load() || model.git_urls_loading);
    if (ImGui::Button("Reload")) refresh_module_urls(model);
    ImGui::EndDisabled();
    ImGui::SameLine();
    if (model.git_urls_loading) ImGui::TextColored(th.text_muted, "Loading…");
    else ImGui::TextColored(th.text_muted, "%d module(s)",
                            static_cast<int>(model.git_urls.size()));
    ImGui::Separator();

    if (ImGui::BeginTable("git_urls_tbl", 3,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerV |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Module", ImGuiTableColumnFlags_WidthStretch, 0.30f);
        ImGui::TableSetupColumn("Remote URL", ImGuiTableColumnFlags_WidthStretch, 0.55f);
        ImGui::TableSetupColumn("", ImGuiTableColumnFlags_WidthFixed, 70.f);
        ImGui::TableHeadersRow();
        for (size_t i = 0; i < model.git_urls.size(); ++i) {
            auto& row = model.git_urls[i];
            ImGui::TableNextRow();
            ImGui::PushID(static_cast<int>(i));
            ImGui::TableSetColumnIndex(0);
            ImGui::TextUnformatted(row.path.c_str());
            if (row.nested)
                ImGui::TextColored(th.text_muted, "inside %s", model.framework());
            if (!row.present) ImGui::TextColored(th.warn, "not checked out");
            ImGui::TableSetColumnIndex(1);
            ImGui::SetNextItemWidth(-FLT_MIN);
            ImGui::InputText("##url", row.edit, sizeof(row.edit));
            // A local override is invisible in the repo, so say when one is in
            // force — otherwise "why is it pushing there" has no answer on screen.
            if (!row.local_url.empty() && row.local_url != row.gitmodules_url)
                ImGui::TextColored(th.text_muted, ".gitmodules says %s",
                                   row.gitmodules_url.c_str());
            // That repo changed owner or name. The old URL still works — GitHub
            // redirects it — so nothing looks broken, and the row would sit
            // there naming a dead pointer forever unless the dialog says so.
            if (!row.moved_to.empty())
                ImGui::TextColored(th.warn, "moved → %s (Reset uses it)",
                                   row.moved_to.c_str());
            ImGui::TableSetColumnIndex(2);
            ImGui::BeginDisabled(model.busy.load());
            if (ImGui::SmallButton("Reset")) {
                std::vector<std::string> args = {
                    "git", "set-module-url", "--root", root, "--path", row.path, "--reset"};
                if (row.nested) args.push_back("--nested");
                retcomm::studio::run_project_studio_async(
                    model, std::move(args),
                    [&model](RunResult) { refresh_module_urls(model); });
            }
            ImGui::EndDisabled();
            ImGui::PopID();
        }
        ImGui::EndTable();
    }

    ImGui::Separator();
    ImGui::BeginDisabled(model.busy.load() || model.git_urls_loading);
    accent_button(th);
    if (ImGui::Button("Save changes", ImVec2(140.f, 0))) {
        const char* scope = model.git_url_scope == 1 ? "gitmodules" : "local";
        int sent = 0;
        for (const auto& row : model.git_urls) {
            const std::string want = row.edit;
            // Only rows the user actually edited: re-setting an unchanged URL
            // would dirty .gitmodules for no reason under the tracked scope.
            if (want.empty() || want == row.effective_url) continue;
            std::vector<std::string> args = {
                "git", "set-module-url", "--root", root, "--path", row.path,
                "--url", want, "--scope", scope};
            if (row.nested) args.push_back("--nested");
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
            ++sent;
        }
        if (sent == 0) model.append_log("Git settings: no URLs changed.");
        else refresh_module_urls(model);
    }
    accent_button_pop();
    ImGui::EndDisabled();
    ImGui::SameLine();
    if (ImGui::Button("Close", ImVec2(120.f, 0))) model.git_settings_open = false;
    ImGui::EndPopup();
}

void draw_header(StudioModel& model, const Theme& th, SDL_Window* window) {
    constexpr float kLabelW = 88.f;

    // Title row — push Check updates to the right using remaining width.
    ImGui::TextColored(th.accent, "Retro Studio");
    ImGui::SameLine();
    ImGui::TextDisabled("v%s", model.version.c_str());
    const bool picked = model.platform != Platform::None;
    if (picked) {
        ImGui::SameLine();
        ImGui::TextDisabled("·");
        ImGui::SameLine();
        ImGui::TextColored(th.text, "%s", platform_display(model.platform));
        ImGui::SameLine();
        if (ImGui::SmallButton("Change##platform")) model.platform_pending = true;
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "Back to the platform picker.\n"
                "Each console keeps its own repo list, so nothing is lost by switching.");
        }
    }
    {
        const ImGuiStyle& st = ImGui::GetStyle();
        const float settings_w = widget_label_width("Git settings");
        const float bw = settings_w + st.ItemSpacing.x + widget_label_width("Check updates");
        ImGui::SameLine(0.f, 0.f);
        const float gap = ImGui::GetContentRegionAvail().x - bw;
        if (gap > st.ItemSpacing.x) ImGui::Dummy(ImVec2(gap - st.ItemSpacing.x, 0.f));
        ImGui::SameLine();
        ImGui::BeginDisabled(model.busy.load() || !picked || model.selected_root().empty());
        if (ImGui::Button("Git settings")) {
            model.git_settings_open = true;
            if (model.git_urls.empty() || model.git_urls_root != model.selected_root())
                refresh_module_urls(model);
        }
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "Where each submodule and nested module is fetched from and pushed to.\n"
                "Point them at your forks to contribute; leave them for read-only use.");
        }
        ImGui::SameLine();
        ImGui::BeginDisabled(model.busy.load());
        if (ImGui::Button("Check updates")) {
            retcomm::studio::run_project_studio_async(
                model, {"updates", "check", "--json"},
                [&model](RunResult r) { handle_updates_check_result(model, r); });
        }
        ImGui::EndDisabled();
    }

    // Repo row — size combo from trailing button/checkbox widths. Absent until
    // a platform names an index: an empty "(add a repo…)" combo above the
    // picker invites adding a repo to a list nobody has chosen yet.
    if (!picked) {
        text_clipped(model.status.c_str(), th.text_muted);
        return;
    }
    left_label("Game repo", kLabelW);
    {
        const ImGuiStyle& st = ImGui::GetStyle();
        const float trail = widget_label_width("Add…") + st.ItemSpacing.x +
                            widget_label_width("Remove") + st.ItemSpacing.x +
                            trailing_checkbox_width("Catalog only");
        float combo_w = ImGui::GetContentRegionAvail().x - trail;
        if (combo_w < 120.f) combo_w = 120.f;
        ImGui::SetNextItemWidth(combo_w);
        const char* preview = "(add a repo…)";
        if (model.selected_repo >= 0 &&
            model.selected_repo < static_cast<int>(model.repos.size()))
            preview = model.repos[static_cast<size_t>(model.selected_repo)].label.c_str();
        if (ImGui::BeginCombo("##repo", preview, ImGuiComboFlags_HeightLarge)) {
            int shown = 0;
            for (int i = 0; i < static_cast<int>(model.repos.size()); ++i) {
                const auto& e = model.repos[static_cast<size_t>(i)];
                if (model.catalog_only && !e.in_catalog) continue;
                ++shown;
                const bool sel = (i == model.selected_repo);
                if (ImGui::Selectable(e.label.c_str(), sel)) {
                    model.selected_repo = i;
                    std::snprintf(model.disc_cue, sizeof(model.disc_cue), "%s", e.cue.c_str());
                    model.apply_selected_players();
                    retcomm::studio::run_project_studio_async(
                        model, {"repos", "set-last", "--path", e.path, "--json"},
                        [&model](RunResult r) {
                            std::string err;
                            retcomm::studio::load_repos_from_json(model, r.stdout_text, &err);
                        },
                        false);
                    model.append_log("Selected repo: " + e.path + " (players=" +
                                     std::to_string(model.players) + ")");
                    refresh_branches(model, false);
                }
                if (sel) ImGui::SetItemDefaultFocus();
            }
            if (shown == 0) {
                ImGui::BeginDisabled();
                ImGui::Selectable(model.catalog_only ? "(no catalog repos indexed)"
                                                    : "(no repos indexed)",
                                 false);
                ImGui::EndDisabled();
            }
            ImGui::EndCombo();
        }
        ImGui::SameLine();
        if (ImGui::Button("Add…")) pick_folder(model, window, "repo_add");
        ImGui::SameLine();
        if (ImGui::Button("Remove") && model.selected_repo >= 0) {
            const std::string path = model.selected_root();
            retcomm::studio::run_project_studio_async(
                model, {"repos", "remove", "--path", path, "--json"},
                [&model](RunResult r) {
                    std::string err;
                    retcomm::studio::load_repos_from_json(model, r.stdout_text, &err);
                },
                false);
        }
        ImGui::SameLine();
                if (ImGui::Checkbox("Catalog only", &model.catalog_only)) {
            retcomm::studio::run_project_studio_async(
                model,
                {"repos", "set-flags", "--catalog-only", model.catalog_only ? "1" : "0", "--json"},
                [&model](RunResult r) {
                    std::string err;
                    retcomm::studio::load_repos_from_json(model, r.stdout_text, &err);
                },
                false);
        }
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "When checked, the Game repo list only shows titles that\n"
                "have an entry in retcomm-catalog.");
        }
    }

    if (model.selected_repo >= 0 && model.selected_repo < static_cast<int>(model.repos.size())) {
        text_clipped(model.repos[static_cast<size_t>(model.selected_repo)].path.c_str(),
                     th.text_muted);
    }
    text_clipped(model.status.c_str(), th.text_muted);
}

void draw_migrate(StudioModel& model, const Theme& th, SDL_Window* window) {
    constexpr float kLabelW = 110.f;
    const bool snes = model.is_snes();
    if (model.is_n64()) {
        wrapped(th.text_muted,
                "Brings an N64 port up to the n64lle scaffold: submodules, .gitignore, "
                "untracked generated C and ROM bytes, the scaffold stubs, and "
                "framework_pins.txt.");
        ImGui::Spacing();
        wrapped(th.text_muted,
                "It will NOT rewrite game.toml, CMakeLists.txt, README.md or "
                "docs/STATUS.md. game.toml is the hand-maintained contract whose "
                "[MEASURED] tags are only worth something because no program writes it, "
                "and STATUS.md is the honesty ledger — a fresh one asserts that nothing "
                "has been measured.");
        ImGui::Spacing();
    }
    if (snes) {
        ImGui::TextColored(th.text_muted,
                           "Brings a SNES port up to the snesrecomp scaffold: submodules, "
                           ".gitignore, untracked generated C, regen.sh / packager / CI, and "
                           "framework_pins.txt. The ROM is read for digests when a file has "
                           "to be re-emitted and is never copied into the repo — but the path "
                           "you set here is remembered for this repo, and is what Build's "
                           "\"Regenerate C from ROM\" hands to tools/regen.sh.");
        ImGui::Spacing();
    }
    ImGui::BeginDisabled(model.busy.load() || model.selected_root().empty());

    bool disc_typed = false;
    if (path_row("##disc", platform_image_label(model.platform), model.disc_cue,
                 sizeof(model.disc_cue), kLabelW, "Browse…##disc", &disc_typed))
        pick_file(model, window, "disc", platform_image_filter_name(model.platform),
                  platform_image_filter_ext(model.platform));
    if (disc_typed) persist_selected_image(model);

    // Options rows — Players combo (wider than old +/-) + zip field.
    // Both are absent on N64: n64ops reads neither. Players only ever gated
    // netplay, which n64lle does not have, and there is no packager to give a
    // zip a prefix. A control that reaches no op is worse than no control —
    // it reads as a setting that was applied.
    if (!model.is_n64()) {
        left_label("Players", kLabelW);
        if (players_combo("##players", &model.players, 140.f)) {
            if (model.players < 2) model.migrate_netplay = false;
        }
        ImGui::SameLine();
        left_label("Zip prefix", 80.f);
        float zw = ImGui::GetContentRegionAvail().x;
        if (zw < 80.f) {
            ImGui::NewLine();
            left_label("Zip prefix", kLabelW);
            zw = ImGui::GetContentRegionAvail().x;
        }
        ImGui::SetNextItemWidth(zw);
        ImGui::InputText("##zip", model.zip_prefix, sizeof(model.zip_prefix));
    }
    // game_id only exists in the SNES identity template, and only matters
    // when the repo records none: leave it blank and Studio reads the id the
    // port already commits to rather than minting a second one.
    if (model.is_snes()) {
        field_row("##game_id", "Game id", model.game_id, sizeof(model.game_id), kLabelW);
        ImGui::TextColored(th.text_muted,
                           "Blank = read from rom_identity.txt, or from the "
                           "game_id this port's mod manifests already name. "
                           "Only set it when Probe ROM says nothing records "
                           "one — it is matched exactly by every mod package.");
    }
    // GitHub owner/repo feed the README + About patch, which n64lle has no
    // template for and n64ops therefore has no op for.
    if (!model.is_n64()) {
        field_row("##gh_owner", "GitHub owner", model.github_owner,
                  sizeof(model.github_owner), kLabelW);
        field_row("##gh_repo", "GitHub repo", model.github_repo, sizeof(model.github_repo),
                  kLabelW);
    }

    // Every checkbox reaches an op on both consoles now. Netplay maps to
    // enable_netplay / snes_enable_netplay (build-side wiring; on SNES the
    // launcher button additionally needs host code — the op's detail says
    // so). Probe maps to probe_disc_refresh / snes_probe_rom_refresh with a
    // path in the image field. README & About maps to patch_readme_metrics /
    // snes_patch_readme_metrics, and unlike the others it also silences the
    // matching audit row: a port that writes its own README should not have to
    // read a warning about it on every run.
    // On N64 none of the four exist: n64lle has no netplay, no release
    // workflow, no packager and no README template, and its game.toml is
    // hand-maintained — so there is no probe-and-rewrite op either. Only the
    // two that govern how the plan is APPLIED remain.
    if (model.is_n64()) {
        model.migrate_netplay = false;
        model.migrate_ci = false;
        model.migrate_probe = false;
        model.migrate_readme = false;
    } else if (!snes) {
        checkbox_wrapped("Netplay", &model.migrate_netplay);
        checkbox_wrapped("CI", &model.migrate_ci);
        checkbox_wrapped("Probe disc", &model.migrate_probe);
        checkbox_wrapped("README & About", &model.migrate_readme);
    } else {
        checkbox_wrapped("Netplay", &model.migrate_netplay);
        checkbox_wrapped("CI", &model.migrate_ci);
        checkbox_wrapped("Probe ROM", &model.migrate_probe);
        checkbox_wrapped("README & About", &model.migrate_readme);
    }
    checkbox_wrapped("Dry-run", &model.migrate_dry_run);
    checkbox_wrapped("Force", &model.migrate_force);
    end_wrapped_line();
    if (model.is_n64()) {
        wrapped(th.text_muted,
                "No Netplay / CI / Probe / README options here: n64lle has none of "
                "those, so a tick would reach no op.");
    }

    accent_button(th);
    if (ImGui::Button("Audit + Plan")) do_audit_plan(model);
    accent_button_pop();
    ImGui::SameLine();
    if (ImGui::Button("Apply selected")) do_apply(model);

    ImGui::EndDisabled();

    const float avail_x = ImGui::GetContentRegionAvail().x;
    const float avail_y = ImGui::GetContentRegionAvail().y;
    const bool stack = avail_x < 720.f;
    const float gap = ImGui::GetStyle().ItemSpacing.x;
    const float half_w = stack ? avail_x : (avail_x - gap) * 0.5f;
    const float pane_h = stack ? avail_y * 0.5f - gap * 0.5f : avail_y;

    ImGui::BeginChild("audit", ImVec2(half_w, pane_h), ImGuiChildFlags_Borders);
    ImGui::TextUnformatted("Audit");
    ImGui::Separator();
    if (ImGui::BeginTable("audit_tbl", 3,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY |
                              ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Status", ImGuiTableColumnFlags_WidthFixed, 56.f);
        ImGui::TableSetupColumn("Check", ImGuiTableColumnFlags_WidthStretch, 0.45f);
        ImGui::TableSetupColumn("Detail", ImGuiTableColumnFlags_WidthStretch, 0.55f);
        ImGui::TableHeadersRow();
        ImGuiListClipper clipper;
        clipper.Begin(static_cast<int>(model.audit_checks.size()));
        while (clipper.Step()) {
            for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
                const auto& c = model.audit_checks[static_cast<size_t>(i)];
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImVec4 col = th.text_muted;
                if (c.status == "pass") col = th.good;
                else if (c.status == "fail") col = th.bad;
                else if (c.status == "warn") col = th.warn;
                ImGui::TextColored(col, "%s", c.status.c_str());
                ImGui::TableSetColumnIndex(1);
                ImGui::TextWrapped("%s", c.title.c_str());
                ImGui::TableSetColumnIndex(2);
                ImGui::TextWrapped("%s", c.detail.c_str());
            }
        }
        ImGui::EndTable();
    }
    ImGui::EndChild();
    if (!stack) ImGui::SameLine();
    ImGui::BeginChild("plan", ImVec2(stack ? avail_x : 0.f, pane_h), ImGuiChildFlags_Borders);
    ImGui::TextUnformatted("Plan");
    ImGui::Separator();
    if (model.plan_steps.empty()) {
        ImGui::TextColored(th.text_muted, "No migration steps (run Audit + Plan).");
    } else {
        for (auto& s : model.plan_steps) {
            ImGui::PushID(s.op_id.c_str());
            ImGui::Checkbox("##sel", &s.selected);
            ImGui::SameLine();
            ImGui::TextWrapped("%s — %s", s.op_id.c_str(), s.title.c_str());
            if (!s.detail.empty()) {
                ImGui::Indent();
                ImGui::TextColored(th.text_muted, "%s", s.detail.c_str());
                ImGui::Unindent();
            }
            ImGui::PopID();
        }
    }
    ImGui::EndChild();
}

void draw_new_project(StudioModel& model, const Theme& th, SDL_Window* window) {
    constexpr float kLabelW = 110.f;
    const bool snes = model.is_snes();
    const bool n64 = model.is_n64();
    const bool cart = model.is_cartridge();
    // First visit: load default module branch lists (ls-remote).
    if (model.branches_psx.empty() && !model.branches_loading)
        refresh_branches(model, false);
    ImGui::BeginChild("##np_scroll", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_None);
    ImGui::BeginDisabled(model.busy.load());
    if (n64) {
        wrapped(th.text_muted,
                "Drives n64lle's tools/new_project/setup_project.sh: probe the cartridge, "
                "lay out the repo, wire n64lle + recomp-ui, write game.toml, then "
                "generate / build / publish. The dump is probed where it lies and linked "
                "into roms/, never copied into git.");
        ImGui::Spacing();
        wrapped(th.text_muted,
                "The new repo carries no host/: the launcher, input, audio and run loop "
                "come from ONE n64lle_add_runtime_target() call, so host fixes reach it "
                "on a submodule bump.");
        ImGui::Spacing();
    }
    if (snes) {
        ImGui::TextColored(th.text_muted,
                           "Drives snesrecomp's tools/new_project/setup_project.sh: probe the "
                           "ROM, lay out the repo, wire submodules, seed recomp/*.cfg, then "
                           "generate / build / publish. The ROM is probed where it lies and "
                           "never enters the repository.");
        ImGui::Spacing();
    }
    if (path_row("##np_parent", "Parent folder", model.np_parent, sizeof(model.np_parent),
                 kLabelW, "…##np_parent"))
        pick_folder(model, window, "np_parent");
    field_row("##np_name", "Name", model.np_name, sizeof(model.np_name), kLabelW);
    // A cartridge is one image, so the whole disc-set group is PSX-only. On
    // both cartridge consoles np_disc carries the ROM and the count is 1.
    if (cart) model.np_disc_count = 1;
    if (!cart) {
        left_label("Discs", kLabelW);
        char disc_preview[8];
        std::snprintf(disc_preview, sizeof(disc_preview), "%d", model.np_disc_count);
        ImGui::SetNextItemWidth(140.f);
        if (ImGui::BeginCombo("##np_disc_count", disc_preview)) {
            for (int n = 1; n <= StudioModel::kMaxDiscs; ++n) {
                char lab[8];
                std::snprintf(lab, sizeof(lab), "%d", n);
                const bool sel = (model.np_disc_count == n);
                if (ImGui::Selectable(lab, sel)) {
                    model.np_disc_count = n;
                    // Clear the rows this hides: a path the user can no longer
                    // see must not still be submitted.
                    for (int i = n - 1; i < StudioModel::kMaxDiscs - 1; ++i)
                        model.np_disc_extra[i][0] = '\0';
                }
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
        if (model.np_disc_count > 1) {
            ImGui::SameLine();
            ImGui::TextColored(th.text_muted, "in disc order, boot disc first");
        }
    }
    {
        // With a set, the first field is "Disc 1" rather than just "Disc .cue".
        const char* disc1_label = (!cart && model.np_disc_count > 1)
                                      ? "Disc 1 (.cue)"
                                      : platform_image_label(model.platform);
        if (path_row("##np_disc", disc1_label, model.np_disc, sizeof(model.np_disc),
                     kLabelW, "…##np_disc"))
            pick_file(model, window, "np_disc", platform_image_filter_name(model.platform),
                      platform_image_filter_ext(model.platform));
    }
    if (!cart) {
        for (int i = 0; i + 1 < model.np_disc_count && i < StudioModel::kMaxDiscs - 1; ++i) {
            char row_id[32], browse_id[32], label[32], target[32];
            std::snprintf(row_id, sizeof(row_id), "##np_disc%d", i + 2);
            std::snprintf(browse_id, sizeof(browse_id), "…##np_disc%d", i + 2);
            std::snprintf(label, sizeof(label), "Disc %d (.cue)", i + 2);
            std::snprintf(target, sizeof(target), "np_disc%d", i + 2);
            if (path_row(row_id, label, model.np_disc_extra[i],
                         sizeof(model.np_disc_extra[i]), kLabelW, browse_id))
                pick_file(model, window, target,
                          platform_image_filter_name(model.platform),
                          platform_image_filter_ext(model.platform));
        }
    }
    // A cartridge boots from its own reset vector: there is no BIOS to supply.
    if (!cart) {
        if (path_row("##np_bios", "BIOS", model.np_bios, sizeof(model.np_bios), kLabelW,
                     "…##np_bios"))
            pick_file(model, window, "np_bios", "BIOS", "bin;rom");
    }

    left_label("Players", kLabelW);
    if (players_combo("##np_players", &model.np_players, 140.f)) {
        if (model.np_players >= 2)
            model.np_netplay = true;
        else
            model.np_netplay = false;
    }
    if (n64) {
        // The console has four ports and the scaffolder rejects anything else
        // outright. Clamping here means the seat count on screen is one the
        // run can actually take, rather than one the script dies on.
        if (model.np_players > 4) model.np_players = 4;
        if (model.np_players < 1) model.np_players = 1;
        ImGui::SameLine();
        ImGui::TextColored(th.text_muted, "four ports; no netplay on n64lle");
        model.np_netplay = false;
    } else {
    // Netplay lives beside the seat count that gates it: a 1-player title
    // cannot opt in, and bumping the count to 2+ suggests it by default.
    ImGui::SameLine();
    ImGui::BeginDisabled(model.np_players < 2);
    ImGui::Checkbox("Netplay##np", &model.np_netplay);
    ImGui::EndDisabled();
    if (model.np_players < 2) model.np_netplay = false;
    }
    if (snes) {
        // Seats above two need a Super Multitap; the script derives a layout
        // from the count, and this only overrides it.
        left_label("Multitap", kLabelW);
        ImGui::SetNextItemWidth(220.f);
        ImGui::Combo("##np_multitap", &model.np_multitap,
                     "Auto (from players)\0Port 1\0Port 2\0Both ports\0Off\0");
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "Auto: 3–5 seats use the port-2 tap (the layout commercial\n"
                "titles use), 6–8 use both ports, 1–2 use none.");
        }
    }
    // n64lle ships no packager, so there is no zip to prefix.
    if (!n64)
        field_row("##np_zip", "Zip prefix", model.np_zip, sizeof(model.np_zip), kLabelW);
    field_row("##np_gh_owner", "GitHub owner", model.np_gh_owner, sizeof(model.np_gh_owner),
              kLabelW);
    field_row("##np_gh_repo", "GitHub repo", model.np_gh_repo, sizeof(model.np_gh_repo), kLabelW);
    if (n64) {
        left_label("", kLabelW);
        ImGui::TextColored(th.text_muted,
                           "Also the CMake project() and the folder created. "
                           "Blank = <Name>Recomp.");
        // The other two names. Kept beside the repo name because the three are
        // read together, and getting one wrong is only visible next to the
        // other two: GloverRecomp / glover-runtime / glover.
        field_row("##np_n64_slug", "Target prefix", model.np_n64_slug,
                  sizeof(model.np_n64_slug), kLabelW);
        if (model.np_n64_slug[0] == '\0') {
            left_label("", kLabelW);
            ImGui::TextColored(th.text_muted,
                               "Blank = the name, lowercased. Every target is built from "
                               "it (<slug>-runtime, <slug>-cosim, <slug>-generate).");
        }
        field_row("##np_n64_exe", "Executable", model.np_n64_exe, sizeof(model.np_n64_exe),
                  kLabelW);
        if (model.np_n64_exe[0] == '\0') {
            left_label("", kLabelW);
            ImGui::TextColored(th.text_muted, "Blank = the target prefix.");
        }
        // The harvest window IS the coverage decision on this console:
        // discovery is execution-derived, so what did not run is not emitted.
        left_label("Harvest", kLabelW);
        ImGui::SetNextItemWidth(110.f);
        ImGui::InputInt("##np_n64_frames", &model.np_n64_frames, 0, 0);
        if (model.np_n64_frames < 0) model.np_n64_frames = 0;
        ImGui::SameLine();
        ImGui::TextColored(th.text_muted, "frames");
        ImGui::SameLine();
        ImGui::SetNextItemWidth(110.f);
        ImGui::InputInt("##np_n64_stepcap", &model.np_n64_step_cap, 0, 0);
        if (model.np_n64_step_cap < 0) model.np_n64_step_cap = 0;
        ImGui::SameLine();
        ImGui::TextColored(th.text_muted, "M steps");
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "n64lle's discovery is execution-derived: the harvester runs the real\n"
                "boot on the interpreter and records what executed. This window is\n"
                "therefore the coverage decision — code outside it is never emitted.\n"
                "0 takes the scaffolder's defaults (900 frames / 3000M steps).");
        }
    }
    // The SNES wizard prompts for these on a terminal, with the probed ROM
    // identity as each default. Studio runs it with --yes, which takes every
    // default silently — so the fields are here, and Probe ROM fills them with
    // the same values the prompts would have offered.
    if (snes) {
        field_row("##np_snes_region", "Region", model.np_snes_region,
                  sizeof(model.np_snes_region), kLabelW);
        if (model.np_snes_region[0] == '\0') {
            left_label("", kLabelW);
            ImGui::TextColored(th.text_muted, "Blank = from the cartridge header.");
        }
        field_row("##np_desc", "Description", model.np_desc, sizeof(model.np_desc), kLabelW);
        field_row("##np_pub", "Publisher", model.np_publisher, sizeof(model.np_publisher),
                  kLabelW);
        field_row("##np_year", "Year", model.np_year, sizeof(model.np_year), kLabelW);
    } else if (n64) {
        // Region, description, publisher and year are absent rather than
        // ignored: n64lle's scaffolder has no flag for any of them, and its
        // game.toml takes the region straight off the cartridge header as a
        // [MEASURED] row. A field Studio could not send would look honoured.
        left_label("Region", kLabelW);
        ImGui::TextColored(th.text_muted,
                           "From the cartridge header — game.toml [game].region, "
                           "measured by probe_rom.py.");
    } else {
        field_row("##np_region", "Region", model.np_region, sizeof(model.np_region), kLabelW);
        field_row("##np_desc", "Description", model.np_desc, sizeof(model.np_desc), kLabelW);
        field_row("##np_pub", "Publisher", model.np_publisher, sizeof(model.np_publisher),
                  kLabelW);
        field_row("##np_year", "Year", model.np_year, sizeof(model.np_year), kLabelW);
        field_row("##np_lobby", "Lobby", model.np_lobby, sizeof(model.np_lobby), kLabelW);
    }

    // recomp-ui is not optional on n64lle: setup_project.sh always adds the
    // submodule, and skipping the launcher is a build-time option in the
    // created repo (<SLUG>_BUILD_UI), not a scaffolding choice.
    if (!n64) checkbox_wrapped("recomp-ui", &model.np_ui);
    if (!cart) checkbox_wrapped("Wizard", &model.np_wizard);
    // No Rollback checkbox: retcomm-rbengine is part of every SNES desktop
    // host now (run-ahead, rewind, and the netplay rollback session), so it
    // is not a scaffolding choice any more.
    // n64lle ships no release workflow or packager template, so there is no CI
    // to emit and no boxart step in its scaffolder.
    if (!n64) checkbox_wrapped("CI##np", &model.np_ci);
    if (!cart) checkbox_wrapped("Boxart", &model.np_boxart);
    // SNES: one step fetches the launcher's boxart (libretro thumbnails) AND
    // the README's publisher / developer / year (libretro-database, by the
    // ROM's CRC32) -- the wizard's "Fetch boxart and metadata". Off, the
    // scaffold says where custom art goes.
    if (snes) checkbox_wrapped("Fetch boxart + metadata", &model.np_boxart);
    if (!snes) {
        // "Stage" on N64 is --copy-rom: the scaffolder SYMLINKS the dump into
        // roms/ by default, which is the better answer on a machine that keeps
        // its library locally. Ticking it copies instead, for a dump on
        // removable media.
        checkbox_wrapped(n64 ? "Copy ROM" : "Stage", &model.np_stage);
    }
    checkbox_wrapped("Generate", &model.np_generate);
    // n64lle's --generate IS the whole pipeline (framework build, harvest,
    // emit, compile, ctest); there is no separate build switch to offer.
    if (!n64) checkbox_wrapped("Build##np", &model.np_build);
    checkbox_wrapped("GitHub", &model.np_github);
    end_wrapped_line();

    {
        ImGui::BeginDisabled(model.branches_loading || model.busy.load());
        if (ImGui::Button(model.branches_loading ? "Loading branches…" : "Refresh branches##np"))
            refresh_branches(model, false);
        ImGui::EndDisabled();
        ImGui::SameLine();
        ImGui::TextDisabled("Module refs (scrollable)");
    }
    constexpr float kBranchW = 260.f;
    // branches_psx holds whichever framework this session is on — the CLI
    // returns it under a stable "framework" key precisely so this widget does
    // not have to know which.
    if (n64) {
        // n64lle's setup_project.sh grew --n64lle-ref / --recomp-ui-ref on
        // 2026-09-12. Leaving this blank is not "main": it keeps the
        // scaffolder's own behaviour, which is branch main pinned at the HEAD
        // of the checkout it was run from — "the SHA this scaffold was cut
        // against". A wizard older than the flag is not silently ignored: the
        // CLI checks the script's own case arms and says so in the log.
        branch_combo("##np_n64", "n64lle ref", model.np_n64_ref, sizeof(model.np_n64_ref),
                     kLabelW, model.branches_psx, kBranchW);
        branch_combo("##np_ui_n64", "recomp-ui ref", model.np_ui_ref, sizeof(model.np_ui_ref),
                     kLabelW, model.branches_ui, kBranchW);
        wrapped(th.text_muted,
                "Leave n64lle ref empty to keep the scaffolder's own pin: branch main at "
                "the HEAD of the n64lle checkout Studio drives. A branch is recorded in "
                ".gitmodules and tracked; a tag or SHA is pinned detached, with no "
                "branch= line for `submodule update --remote` to move it off.");
    } else if (snes) {
        branch_combo("##np_snes", "snesrecomp ref", model.np_snes_ref,
                     sizeof(model.np_snes_ref), kLabelW, model.branches_psx, kBranchW);
    } else {
        branch_combo("##np_psx", "psxrecomp ref", model.np_psx_ref, sizeof(model.np_psx_ref),
                     kLabelW, model.branches_psx, kBranchW);
    }
    if (!n64) {
        branch_combo("##np_ui", "recomp-ui ref", model.np_ui_ref, sizeof(model.np_ui_ref),
                     kLabelW, model.branches_ui, kBranchW);
    }
    // recomp-net and retcomm-rbengine live inside psxrecomp/snesrecomp; n64lle
    // vendors neither, so there is no ref to pick.
    if (platform_has_netplay(model.platform)) {
        branch_combo("##np_net", "recomp-net ref", model.np_net_ref, sizeof(model.np_net_ref),
                     kLabelW, model.branches_net, kBranchW);
        branch_combo("##np_rb", "rbengine ref", model.np_rb_ref, sizeof(model.np_rb_ref),
                     kLabelW, model.branches_rb, kBranchW);
    }

    // The cartridge equivalent of Autofill meta. Not a metadata *lookup* —
    // there is no Redump entry for a cartridge — but the ROM's own header,
    // which is exactly what the wizard's prompts offer as defaults.
    if (cart) {
        ImGui::BeginDisabled(!model.np_disc[0]);
        if (ImGui::Button("Probe ROM")) {
            std::vector<std::string> args = {"probe-rom", "--rom", model.np_disc};
            retcomm::studio::run_project_studio_async(
                model, std::move(args),
                [&model](RunResult r) {
                    if (!r.ok()) {
                        model.append_log(std::string("[FAIL] Probe failed — is this a ") +
                                         platform_display(model.platform) + " ROM?");
                        model.np_probe_note = "probe failed";
                        return;
                    }
                    try {
                        auto j = nlohmann::json::parse(r.stdout_text);
                        auto set = [&](char* buf, size_t n, const char* key) {
                            if (j.contains(key) && j[key].is_string()) {
                                const std::string v = j[key].get<std::string>();
                                if (!v.empty()) std::snprintf(buf, n, "%s", v.c_str());
                            }
                        };
                        // The name every cartridge probe agrees on. Everything
                        // below it is per-console: the two probes report
                        // different fields because the two cartridges carry
                        // different identities, and pretending otherwise is how
                        // a blank ends up in a scaffolded contract.
                        set(model.np_name, sizeof(model.np_name), "display_name");
                        if (model.is_n64()) {
                            set(model.np_gh_repo, sizeof(model.np_gh_repo), "project");
                            set(model.np_n64_slug, sizeof(model.np_n64_slug), "slug");
                            set(model.np_n64_exe, sizeof(model.np_n64_exe), "exe");
                            model.np_probe_note =
                                j.value("cartid", "?") + ", region " +
                                j.value("region_label", "?") + ", " + j.value("cic", "?") +
                                ", " + j.value("byte_order", "?");
                            model.append_log("Probed " + j.value("header_title", "ROM") +
                                             ": " + model.np_probe_note);
                            // KI-1 before a port is scaffolded around it, not
                            // after a day of debugging a black screen.
                            const std::string warn = j.value("warning", "");
                            if (!warn.empty()) model.append_log("[WARN] " + warn);
                            return;
                        }
                        set(model.np_snes_region, sizeof(model.np_snes_region), "region");
                        set(model.np_zip, sizeof(model.np_zip), "zip_prefix");
                        set(model.np_gh_repo, sizeof(model.np_gh_repo), "project_name");
                        const std::string mapping = j.value("mapping", "");
                        const std::string copro = j.value("coprocessor", "");
                        const bool ck = j.value("checksum_valid", false);
                        model.np_probe_note = mapping + ", region " +
                                              j.value("region_name", "?") + ", coprocessor " +
                                              (copro.empty() ? "?" : copro) +
                                              (ck ? ", checksum OK" : ", CHECKSUM BAD");
                        model.append_log("Probed " + j.value("rom_file", "ROM") + ": " +
                                         model.np_probe_note);
                        // A coprocessor the runner may not implement is worth
                        // saying out loud before a port is scaffolded around it.
                        if (!copro.empty() && copro != "none")
                            model.append_log("[WARN] Cartridge uses " + copro +
                                             " — check snesrecomp supports it before porting.");
                        if (!ck)
                            model.append_log(
                                "[WARN] Header checksum does not validate — the dump may be "
                                "bad, overdumped, or a hack.");
                    } catch (const std::exception& ex) {
                        model.append_log(std::string("Probe parse failed: ") + ex.what());
                    }
                },
                false);
        }
        ImGui::EndDisabled();
        if (!model.np_probe_note.empty()) {
            ImGui::SameLine();
            ImGui::TextColored(th.text_muted, "%s", model.np_probe_note.c_str());
        }
        ImGui::SameLine();
    }
    // Redump / libretro lookup is disc-keyed; a cartridge has no entry.
    if (!cart && model.np_disc[0] && ImGui::Button("Autofill meta")) {
        retcomm::studio::run_project_studio_async(
            model, {"lookup-disc-meta", "--disc", model.np_disc, "--json"},
            [&model](RunResult r) {
                try {
                    auto j = nlohmann::json::parse(r.stdout_text);
                    auto set = [&](char* buf, size_t n, const char* key) {
                        if (j.contains(key) && j[key].is_string()) {
                            const std::string v = j[key].get<std::string>();
                            if (!v.empty()) std::snprintf(buf, n, "%s", v.c_str());
                        }
                    };
                    set(model.np_name, sizeof(model.np_name), "name");
                    set(model.np_desc, sizeof(model.np_desc), "description");
                    set(model.np_publisher, sizeof(model.np_publisher), "publisher");
                    set(model.np_year, sizeof(model.np_year), "year");
                    set(model.np_region, sizeof(model.np_region), "region");
                    set(model.np_zip, sizeof(model.np_zip), "zip_prefix");
                    if (j.contains("players") && j["players"].is_number_integer()) {
                        model.np_players = j["players"].get<int>();
                    }
                    model.append_log("Autofill meta OK");
                } catch (const std::exception& ex) {
                    model.append_log(std::string("Autofill failed: ") + ex.what());
                }
            },
            false);
    }
    if (!snes) ImGui::SameLine();
    accent_button(th);
    if (ImGui::Button("Create project")) {
        int blank_disc = 0;
        if (!snes) {
            for (int i = 0; i + 1 < model.np_disc_count &&
                            i < StudioModel::kMaxDiscs - 1; ++i) {
                if (!model.np_disc_extra[i][0]) { blank_disc = i + 2; break; }
            }
        }
        if (!model.np_name[0] || !model.np_parent[0] || !model.np_disc[0]) {
            model.append_log(std::string("[FAIL] Need parent, name, and ") +
                             platform_image_label(model.platform));
        } else if (blank_disc) {
            model.append_log("[FAIL] Disc " + std::to_string(blank_disc) +
                             " is empty — pick it, or lower the disc count");
        } else if (model.is_n64()) {
            std::vector<std::string> args = {
                "new-project",
                "--name", model.np_name,
                "--dir", model.np_parent,
                "--rom", model.np_disc,
                "--players", std::to_string(model.np_players),
            };
            // The three names. Sent only when typed: blank means "let the
            // scaffolder derive it", and echoing its own rule back would be a
            // second copy of that rule to drift.
            if (model.np_gh_repo[0]) {
                args.push_back("--github-repo");   // == the CMake project name
                args.push_back(model.np_gh_repo);
            }
            if (model.np_n64_slug[0]) {
                args.push_back("--n64-slug");
                args.push_back(model.np_n64_slug);
            }
            if (model.np_n64_exe[0]) {
                args.push_back("--n64-exe");
                args.push_back(model.np_n64_exe);
            }
            if (model.np_n64_frames > 0) {
                args.push_back("--frames");
                args.push_back(std::to_string(model.np_n64_frames));
            }
            if (model.np_n64_step_cap > 0) {
                args.push_back("--step-cap");
                args.push_back(std::to_string(model.np_n64_step_cap));
            }
            if (model.np_gh_owner[0]) {
                args.push_back("--github-owner");
                args.push_back(model.np_gh_owner);
            }
            // The submodule revisions. Blank n64lle ref = the scaffolder's own
            // pin, so it is sent only when the user picked something.
            if (model.np_n64_ref[0]) {
                args.push_back("--n64lle-ref");
                args.push_back(model.np_n64_ref);
            }
            if (model.np_ui_ref[0]) {
                args.push_back("--recomp-ui-ref");
                args.push_back(model.np_ui_ref);
            }
            // "Copy ROM" — the scaffolder symlinks by default.
            if (model.np_stage) args.push_back("--stage-disc");
            // --generate on this scaffolder is the whole pipeline: framework
            // build, harvest, emit, compile and ctest. There is no separate
            // build step, so either switch asking for work maps onto it.
            if (model.np_generate || model.np_build) args.push_back("--generate");
            if (model.np_github) args.push_back("--create-github");
            model.append_log("--- New N64 project setup ---");
            retcomm::studio::run_project_studio_async(model, args, [&model](RunResult r) {
                model.set_status(r.ok() ? "New project created" : "New project failed");
                refresh_repos(model);
            });
        } else if (snes) {
            std::vector<std::string> args = {
                "new-project",
                "--name", model.np_name,
                "--dir", model.np_parent,
                "--rom", model.np_disc,
                "--players", std::to_string(model.np_players),
            };
            if (model.np_zip[0]) {
                args.push_back("--zip-prefix");
                args.push_back(model.np_zip);
            }
            if (model.np_gh_owner[0]) {
                args.push_back("--github-owner");
                args.push_back(model.np_gh_owner);
            }
            if (model.np_gh_repo[0]) {
                args.push_back("--github-repo");
                args.push_back(model.np_gh_repo);
            }
            if (model.np_desc[0]) {
                args.push_back("--description");
                args.push_back(model.np_desc);
            }
            if (model.np_publisher[0]) {
                args.push_back("--publisher");
                args.push_back(model.np_publisher);
            }
            if (model.np_year[0]) {
                args.push_back("--year");
                args.push_back(model.np_year);
            }
            // Omitted when blank so the cartridge header decides.
            if (model.np_snes_region[0]) {
                args.push_back("--region");
                args.push_back(model.np_snes_region);
            }
            static const char* kTaps[] = {"", "port1", "port2", "both", "off"};
            const int tap = (model.np_multitap < 0 || model.np_multitap > 4) ? 0
                                                                            : model.np_multitap;
            if (tap != 0) {
                args.push_back("--multitap");
                args.push_back(kTaps[tap]);
            }
            if (!model.np_ui) args.push_back("--no-recomp-ui");
            if (model.np_netplay) args.push_back("--enable-netplay");
            if (!model.np_ci) args.push_back("--no-ci");
            // Boxart + metadata: the wizard fetches both under one flag, and
            // --autofill-meta lets Studio fill the description from its
            // catalog (and publisher / year) into the form before the wizard
            // runs, so what the player typed always wins over what was fetched.
            if (!model.np_boxart) args.push_back("--no-fetch-boxart");
            else args.push_back("--autofill-meta");
            if (model.np_generate) args.push_back("--generate");
            if (model.np_build) args.push_back("--enable-build");
            if (model.np_github) args.push_back("--create-github");
            if (model.np_snes_ref[0]) {
                args.push_back("--snesrecomp-ref");
                args.push_back(model.np_snes_ref);
            }
            if (model.np_ui_ref[0]) {
                args.push_back("--recomp-ui-ref");
                args.push_back(model.np_ui_ref);
            }
            if (model.np_net_ref[0] && std::strcmp(model.np_net_ref, "(default)") != 0) {
                args.push_back("--recomp-net-ref");
                args.push_back(model.np_net_ref);
            }
            if (model.np_rb_ref[0] && std::strcmp(model.np_rb_ref, "(default)") != 0) {
                args.push_back("--rbengine-ref");
                args.push_back(model.np_rb_ref);
            }
            model.append_log("--- New SNES project setup ---");
            retcomm::studio::run_project_studio_async(model, args, [&model](RunResult r) {
                model.set_status(r.ok() ? "New project created" : "New project failed");
                refresh_repos(model);
            });
        } else {
            std::vector<std::string> args = {
                "new-project",
                "--name",
                model.np_name,
                "--dir",
                model.np_parent,
                "--disc",
                model.np_disc,
                "--players",
                std::to_string(model.np_players),
            };
            // Discs 2..N, in order. The toolkit verifies the set and refuses
            // one that needs N programs, so this only has to pass them along.
            for (int i = 0; i + 1 < model.np_disc_count &&
                            i < StudioModel::kMaxDiscs - 1; ++i) {
                args.push_back("--disc");
                args.push_back(model.np_disc_extra[i]);
            }
            if (model.np_bios[0]) {
                args.push_back("--bios");
                args.push_back(model.np_bios);
            }
            if (model.np_zip[0]) {
                args.push_back("--zip-prefix");
                args.push_back(model.np_zip);
            }
            if (model.np_gh_owner[0]) {
                args.push_back("--github-owner");
                args.push_back(model.np_gh_owner);
            }
            if (model.np_gh_repo[0]) {
                args.push_back("--github-repo");
                args.push_back(model.np_gh_repo);
            }
            if (model.np_desc[0]) {
                args.push_back("--description");
                args.push_back(model.np_desc);
            }
            if (model.np_publisher[0]) {
                args.push_back("--publisher");
                args.push_back(model.np_publisher);
            }
            if (model.np_year[0]) {
                args.push_back("--year");
                args.push_back(model.np_year);
            }
            if (model.np_region[0]) {
                args.push_back("--region");
                args.push_back(model.np_region);
            }
            if (model.np_lobby[0]) {
                args.push_back("--lobby-url");
                args.push_back(model.np_lobby);
            }
            if (!model.np_ui) args.push_back("--no-recomp-ui");
            if (!model.np_wizard) args.push_back("--no-wizard");
            if (model.np_netplay) args.push_back("--enable-netplay");
            if (!model.np_ci) args.push_back("--no-ci");
            if (!model.np_boxart) args.push_back("--no-fetch-boxart");
            if (model.np_stage) args.push_back("--stage-disc");
            if (model.np_generate) args.push_back("--generate");
            if (model.np_build) args.push_back("--enable-build");
            if (model.np_github) args.push_back("--create-github");
            if (model.np_psx_ref[0]) {
                args.push_back("--psxrecomp-ref");
                args.push_back(model.np_psx_ref);
            }
            if (model.np_ui_ref[0]) {
                args.push_back("--recomp-ui-ref");
                args.push_back(model.np_ui_ref);
            }
            if (model.np_net_ref[0] && std::strcmp(model.np_net_ref, "(default)") != 0) {
                args.push_back("--recomp-net-ref");
                args.push_back(model.np_net_ref);
            }
            if (model.np_rb_ref[0] && std::strcmp(model.np_rb_ref, "(default)") != 0) {
                args.push_back("--rbengine-ref");
                args.push_back(model.np_rb_ref);
            }
            model.append_log("--- New project setup ---");
            retcomm::studio::run_project_studio_async(model, args, [&model](RunResult r) {
                model.set_status(r.ok() ? "New project created" : "New project failed");
                refresh_repos(model);
            });
        }
    }
    accent_button_pop();
    ImGui::EndDisabled();
    ImGui::EndChild();
    (void)th;
}

void draw_git(StudioModel& model, const Theme& th) {
    constexpr float kLabelW = 100.f;
    const std::string root = model.selected_root();
    if (!root.empty() && model.branches_root != root && !model.branches_loading)
        refresh_branches(model, false);
    ImGui::BeginChild("##git_scroll", ImVec2(0, 0));
    ImGui::BeginDisabled(model.busy.load() || root.empty());

    // Action buttons wrap instead of overflowing.
    auto action = [](const char* label) -> bool {
        const float need = widget_label_width(label) + ImGui::GetStyle().ItemSpacing.x;
        if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x &&
            ImGui::GetContentRegionAvail().x < need)
            ImGui::NewLine();
        const bool hit = ImGui::Button(label);
        ImGui::SameLine();
        return hit;
    };
    if (action("Refresh status")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "status", "--root", root},
            [&model](RunResult) { model.set_status("Git status refreshed"); });
    }
    if (action("Ensure submodules")) {
        retcomm::studio::run_project_studio_async(
            model,
            {"git", "ensure-submodules", "--root", root, "--framework-branch",
             model.git_psx_branch, "--recomp-ui-branch", model.git_ui_branch},
            nullptr);
    }
    if (action("Ensure nested")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "ensure-nested", "--root", root}, nullptr);
    }
    // The cure for "checkout missing", addressed at the symptom by name.
    // A repo cloned without --recurse-submodules has the .gitmodules entry
    // and an empty directory, so Switch / Pull / Commit / Push / Advance pins
    // all report "checkout missing" on a repo whose config is perfect. This
    // clones what is registered — both scopes, framework first, because the
    // nested modules only become reachable once it is on disk.
    if (action("Init checkouts")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "init-modules", "--root", root},
            [&model](RunResult r) {
                model.set_status(r.ok() ? "Module checkouts initialised"
                                        : "Init checkouts failed — see log");
                if (r.ok()) refresh_branches(model, true);
            });
    }
    // Two different operations, and the old label ("Update submodules") read
    // like the wrong one. `git submodule update` checks out the gitlink the
    // repo ALREADY records — on a fork carrying a stale pin it puts the old
    // revision back, which is why "I tried updating the modules" leaves the pin
    // exactly where it was. Advancing is the other direction.
    if (action("Restore pinned")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "update-submodules", "--root", root}, nullptr);
    }
    if (action("Advance pins")) {
        // Honours the same Targets ticks as Switch / Pull / Commit / Push, so
        // a PSX port can advance recomp-net and rbengine inside psxrecomp —
        // which is where most of what it pins actually lives. Game is not a
        // pin, so it is ignored here.
        std::vector<std::string> args = {"git", "advance-pins", "--root", root};
        if (model.git_tgt_modules) args.push_back("--modules");
        if (model.git_tgt_nested) args.push_back("--nested");
        if (!model.git_tgt_modules && !model.git_tgt_nested) args.push_back("--modules");
        retcomm::studio::run_project_studio_async(
            model, std::move(args),
            [&model](RunResult r) {
                if (r.ok()) model.set_status("Pins advanced — review, then Commit");
            });
    }
    end_wrapped_line();
    ImGui::TextColored(th.text_muted,
                       "Restore pinned = check out the revision this repo already "
                       "records. Advance pins = move to each module's tracked "
                       "branch tip and stage the move (Modules / Nested per the "
                       "ticks below), then Commit.");
    ImGui::TextColored(th.text_muted,
                       "\"checkout missing\" = the submodule is registered in "
                       ".gitmodules but was never cloned, which is what a clone "
                       "without --recurse-submodules leaves behind. Init "
                       "checkouts fixes it; Refresh status marks such a module "
                       "UNINIT rather than OK.");

    ImGui::TextUnformatted("Targets");
    checkbox_wrapped("Game", &model.git_tgt_game);
    checkbox_wrapped("Modules", &model.git_tgt_modules);
    checkbox_wrapped("Nested", &model.git_tgt_nested);
    end_wrapped_line();
    ImGui::TextColored(th.text_muted,
                       "Tick which checkouts Switch / Pull / Commit / Push "
                       "should touch (Game / Modules / Nested).");

    {
        ImGui::BeginDisabled(model.branches_loading || model.busy.load() || root.empty());
        if (ImGui::Button(model.branches_loading ? "Loading branches…" : "Refresh branches##git"))
            refresh_branches(model, true);
        ImGui::EndDisabled();
    }
    {
        left_label("Branch", kLabelW);
        const float create_w = trailing_checkbox_width("Create");
        float fw = ImGui::GetContentRegionAvail().x - create_w;
        if (fw < 140.f) fw = 140.f;
        // Game branch combo sized to leave room for Create.
        ImGui::SetNextItemWidth(fw);
        const char* preview = model.git_branch[0] ? model.git_branch : "(select…)";
        if (ImGui::BeginCombo("##git_branch", preview, ImGuiComboFlags_HeightLarge)) {
            bool have = false;
            for (const auto& s : model.branches_game)
                if (s == model.git_branch) have = true;
            if (model.git_branch[0] && !have) ImGui::Selectable(model.git_branch, true);
            for (const auto& s : model.branches_game) {
                const bool sel = (s == model.git_branch);
                if (ImGui::Selectable(s.c_str(), sel))
                    std::snprintf(model.git_branch, sizeof(model.git_branch), "%s", s.c_str());
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
        ImGui::SameLine();
        ImGui::Checkbox("Create", &model.git_create_branch);
    }

    constexpr float kBranchW = 260.f;
    branch_combo("##git_psx", model.framework(), model.git_psx_branch,
                 sizeof(model.git_psx_branch), kLabelW, model.branches_psx, kBranchW);
    branch_combo("##git_ui", "recomp-ui", model.git_ui_branch, sizeof(model.git_ui_branch),
                 kLabelW, model.branches_ui, kBranchW);
    branch_combo("##git_net", "recomp-net", model.git_net_branch, sizeof(model.git_net_branch),
                 kLabelW, model.branches_net, kBranchW);
    branch_combo("##git_rb", "rbengine", model.git_rb_branch, sizeof(model.git_rb_branch),
                 kLabelW, model.branches_rb, kBranchW);

    if (ImGui::Button("Switch branch")) {
        if (!(model.git_tgt_game || model.git_tgt_modules || model.git_tgt_nested)) {
            model.append_log("[FAIL] Enable at least one Target (Game / Modules / Nested)");
        } else if (model.git_tgt_game && !model.git_branch[0]) {
            model.append_log("[FAIL] Select a game Branch (or uncheck Game)");
        } else {
            std::vector<std::string> args = {"git", "switch", "--root", root};
            if (model.git_tgt_game) {
                args.push_back("--game");
                args.push_back("--branch");
                args.push_back(model.git_branch);
            }
            if (model.git_tgt_modules) {
                args.push_back("--modules");
                args.push_back("--framework-branch");
                args.push_back(model.git_psx_branch);
                args.push_back("--ui-branch");
                args.push_back(model.git_ui_branch);
            }
            if (model.git_tgt_nested) {
                args.push_back("--nested");
                args.push_back("--net-branch");
                args.push_back(model.git_net_branch);
                args.push_back("--rb-branch");
                args.push_back(model.git_rb_branch);
            }
            if (model.git_create_branch) args.push_back("--create");
            retcomm::studio::run_project_studio_async(
                model, std::move(args),
                [&model](RunResult r) {
                    if (r.ok()) refresh_branches(model, false);
                });
        }
    }

    ImGui::Separator();
    field_row("##git_msg", "Commit msg", model.git_msg, sizeof(model.git_msg), kLabelW);
    {
        left_label("Pull mode", kLabelW);
        ImGui::SetNextItemWidth(140.f);
        const char* modes[] = {"ff-only", "rebase", "merge", "reset"};
        int mode = model.git_pull_mode;
        if (mode < 0 || mode > 3) mode = 0;
        if (ImGui::Combo("##git_pull_mode", &mode, modes, 4)) model.git_pull_mode = mode;
    }
    auto append_git_targets = [&](std::vector<std::string>& args) {
        if (model.git_tgt_game) args.push_back("--game");
        if (model.git_tgt_modules) args.push_back("--modules");
        if (model.git_tgt_nested) args.push_back("--nested");
    };
    auto require_git_targets = [&]() -> bool {
        if (model.git_tgt_game || model.git_tgt_modules || model.git_tgt_nested) return true;
        model.append_log("[FAIL] Enable at least one Target (Game / Modules / Nested)");
        return false;
    };
    if (action("Pull")) {
        if (require_git_targets()) {
            static const char* modes[] = {"ff-only", "rebase", "merge", "reset"};
            const int mi =
                (model.git_pull_mode < 0 || model.git_pull_mode > 3) ? 0 : model.git_pull_mode;
            std::vector<std::string> args = {"git", "pull", "--root", root, "--mode", modes[mi]};
            append_git_targets(args);
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
        }
    }
    if (action("Commit")) {
        if (require_git_targets()) {
            std::vector<std::string> args = {"git", "commit", "--root", root, "--message",
                                             model.git_msg};
            append_git_targets(args);
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
        }
    }
    if (action("Push")) {
        if (require_git_targets()) {
            std::vector<std::string> args = {"git", "push", "--root", root};
            append_git_targets(args);
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
        }
    }
    end_wrapped_line();

    ImGui::Separator();
    ImGui::TextUnformatted("Release");
    {
        left_label("Version", kLabelW);
        const float pub_w = trailing_checkbox_width("Publish");
        float fw = ImGui::GetContentRegionAvail().x - pub_w;
        if (fw < 80.f) fw = 80.f;
        ImGui::SetNextItemWidth(fw);
        ImGui::InputText("##rel_ver", model.release_version, sizeof(model.release_version));
        ImGui::SameLine();
        ImGui::Checkbox("Publish", &model.release_publish);
    }
    if (action("Install CI")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "install-ci", "--root", root}, nullptr);
    }
    accent_button(th);
    const bool dispatch = action("Dispatch release");
    accent_button_pop();
    if (dispatch) {
        std::vector<std::string> args = {"git", "release", "--root", root};
        if (model.release_version[0]) {
            args.push_back("--version");
            args.push_back(model.release_version);
        }
        if (!model.release_publish) args.push_back("--no-publish");
        retcomm::studio::run_project_studio_async(model, args, nullptr);
    }
    end_wrapped_line();
    ImGui::EndDisabled();
    ImGui::EndChild();
    (void)th;
}

void draw_bulk(StudioModel& model, const Theme& th) {
    constexpr float kLabelW = 72.f;
    constexpr float kBranchW = 160.f;
    if (!model.branches_loading && model.branches_game.empty() && model.branches_psx.empty() &&
        !model.busy.load())
        refresh_branches(model, false);

    ImGui::BeginDisabled(model.busy.load());
    left_label("Jobs", kLabelW);
    if (jobs_combo("##jobs", &model.bulk_jobs, 100.f)) {
        retcomm::studio::run_project_studio_async(
            model,
            {"repos", "set-flags", "--bulk-jobs", std::to_string(model.bulk_jobs), "--json"},
            nullptr, false);
    }
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
        ImGui::SetTooltip(
            "How many repos to work on at the same time.\n"
            "Higher = faster, but uses more of your computer.");
    }
    ImGui::SameLine();
    auto bulk_btn = [](const char* label) -> bool {
        const float need = widget_label_width(label) + ImGui::GetStyle().ItemSpacing.x;
        if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x &&
            ImGui::GetContentRegionAvail().x < need)
            ImGui::NewLine();
        const bool hit = ImGui::Button(label);
        ImGui::SameLine();
        return hit;
    };
    if (bulk_btn("Select all")) {
        for (auto& kv : model.bulk_selected) kv.second = true;
    }
    if (bulk_btn("Select none")) {
        for (auto& kv : model.bulk_selected) kv.second = false;
    }
    if (bulk_btn("Catalog only")) {
        retcomm::studio::run_project_studio_async(
            model, {"repos", "filter-catalog", "--json"},
            [&model](RunResult r) {
                try {
                    auto j = nlohmann::json::parse(r.stdout_text);
                    const auto note = j.value("note", "");
                    for (auto& kv : model.bulk_selected) kv.second = false;
                    int n = 0;
                    if (j.contains("paths") && j["paths"].is_array()) {
                        for (const auto& p : j["paths"]) {
                            const std::string path = p.get<std::string>();
                            auto it = model.bulk_selected.find(path);
                            if (it != model.bulk_selected.end()) {
                                it->second = true;
                                ++n;
                            }
                        }
                    }
                    if (!note.empty()) model.append_log(note);
                    model.set_status("Selected " + std::to_string(n) + " catalog repo(s)");
                } catch (const std::exception& ex) {
                    model.append_log(std::string("[FAIL] filter-catalog: ") + ex.what());
                }
            },
            false);
    }
    if (bulk_btn("Catalog + Contributors")) {
        retcomm::studio::run_project_studio_async(
            model, {"repos", "filter-catalog-contributors", "--json"},
            [&model](RunResult r) {
                try {
                    auto j = nlohmann::json::parse(r.stdout_text);
                    const auto note = j.value("note", "");
                    for (auto& kv : model.bulk_selected) kv.second = false;
                    int n = 0;
                    if (j.contains("paths") && j["paths"].is_array()) {
                        for (const auto& p : j["paths"]) {
                            const std::string path = p.get<std::string>();
                            auto it = model.bulk_selected.find(path);
                            if (it != model.bulk_selected.end()) {
                                it->second = true;
                                ++n;
                            }
                        }
                    }
                    if (!note.empty()) model.append_log(note);
                    model.set_status("Selected " + std::to_string(n) +
                                     " catalog contributor repo(s)");
                } catch (const std::exception& ex) {
                    model.append_log(std::string("[FAIL] filter-catalog-contributors: ") +
                                     ex.what());
                }
            },
            false);
    }
    if (bulk_btn("Refresh list")) refresh_repos(model);
    end_wrapped_line();

    ImGui::TextUnformatted("Targets");
    checkbox_wrapped("Game", &model.bulk_tgt_game);
    checkbox_wrapped("Modules", &model.bulk_tgt_modules);
    checkbox_wrapped(model.framework(), &model.bulk_tgt_psx);
    checkbox_wrapped("Nested", &model.bulk_tgt_nested);
    end_wrapped_line();
    ImGui::Text("Tick which checkouts Switch branches should move "
                "(Game / Modules / %s / Nested).",
                model.framework());

    ImGui::Separator();
    ImGui::TextUnformatted("Submodule / branch assignments");
    ImGui::TextColored(th.text_muted,
                       "Pick branches, then Switch branches. Fetch refreshes remote heads "
                       "from the selected Game repo (plus module defaults).");

    auto with_default = [](const std::vector<std::string>& src) {
        std::vector<std::string> out;
        out.emplace_back("(default)");
        for (const auto& s : src) {
            if (s != "(default)") out.push_back(s);
        }
        return out;
    };
    const auto game_branches = with_default(model.branches_game);
    const auto psx_branches = with_default(model.branches_psx);
    const auto ui_branches = with_default(model.branches_ui);
    const auto net_branches = with_default(model.branches_net);
    const auto rb_branches = with_default(model.branches_rb);

    branch_combo("##bulk_game", "game", model.bulk_game_branch, sizeof(model.bulk_game_branch),
                 kLabelW, game_branches, kBranchW);
    ImGui::SameLine();
    branch_combo("##bulk_psx", platform_key(model.platform), model.bulk_psx_branch,
                 sizeof(model.bulk_psx_branch), 36.f, psx_branches, kBranchW);
    branch_combo("##bulk_ui", "ui", model.bulk_ui_branch, sizeof(model.bulk_ui_branch), kLabelW,
                 ui_branches, kBranchW);
    ImGui::SameLine();
    branch_combo("##bulk_net", "net", model.bulk_net_branch, sizeof(model.bulk_net_branch), 36.f,
                 net_branches, kBranchW);
    branch_combo("##bulk_rb", "rb", model.bulk_rb_branch, sizeof(model.bulk_rb_branch), kLabelW,
                 rb_branches, kBranchW);

    checkbox_wrapped("Create branch", &model.bulk_create_branch);
    checkbox_wrapped("Set tracking", &model.bulk_set_tracking);
    end_wrapped_line();
    {
        ImGui::BeginDisabled(model.branches_loading);
        if (bulk_btn(model.branches_loading ? "Loading branches…" : "Fetch branches"))
            refresh_branches(model, true);
        ImGui::EndDisabled();
    }

    auto selected_paths = [&]() {
        std::vector<std::string> out;
        for (const auto& e : model.repos) {
            auto it = model.bulk_selected.find(e.path);
            if (it != model.bulk_selected.end() && it->second) out.push_back(e.path);
        }
        return out;
    };
    auto select_csv = [&]() {
        auto paths = selected_paths();
        std::string select;
        for (size_t i = 0; i < paths.size(); ++i) {
            if (i) select += ",";
            select += paths[i];
        }
        return select;
    };

    if (bulk_btn("Switch branches")) {
        auto paths = selected_paths();
        if (paths.empty()) {
            model.append_log("[FAIL] No repos selected");
        } else if (!(model.bulk_tgt_game || model.bulk_tgt_modules || model.bulk_tgt_psx ||
                     model.bulk_tgt_nested)) {
            model.append_log(std::string("[FAIL] Enable at least one Target (Game / Modules / ") +
                             model.framework() + " / Nested)");
        } else {
            std::vector<std::string> args = {"git", "bulk-switch", "--select", select_csv()};
            if (model.bulk_tgt_game) {
                args.push_back("--game");
                args.push_back("--branch");
                args.push_back(model.bulk_game_branch);
            }
            if (model.bulk_tgt_modules) {
                args.push_back("--modules");
                args.push_back("--framework-branch");
                args.push_back(model.bulk_psx_branch);
                args.push_back("--ui-branch");
                args.push_back(model.bulk_ui_branch);
            }
            if (model.bulk_tgt_psx && !model.bulk_tgt_modules) {
                args.push_back("--framework");
                args.push_back("--framework-branch");
                args.push_back(model.bulk_psx_branch);
            }
            if (model.bulk_tgt_nested) {
                args.push_back("--nested");
                args.push_back("--net-branch");
                args.push_back(model.bulk_net_branch);
                args.push_back("--rb-branch");
                args.push_back(model.bulk_rb_branch);
            }
            if (model.bulk_create_branch) args.push_back("--create");
            if (!model.bulk_set_tracking) args.push_back("--no-track");
            model.append_log("--- Bulk switch ---");
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
        }
    }
    end_wrapped_line();

    ImGui::Separator();
    ImGui::TextUnformatted("Release CI (dispatch)");
    {
        left_label("Version", kLabelW);
        ImGui::SetNextItemWidth(180.f);
        ImGui::InputTextWithHint("##bulk_rel_ver", "empty = auto-bump", model.release_version,
                                 sizeof(model.release_version));
        ImGui::SameLine();
        left_label("Bump", 44.f);
        ImGui::SetNextItemWidth(100.f);
        const char* bumps[] = {"patch", "minor", "major"};
        ImGui::Combo("##bulk_bump", &model.release_bump, bumps, 3);
        ImGui::SameLine();
        ImGui::Checkbox("Publish", &model.release_publish);
        ImGui::SameLine();
        ImGui::Checkbox("Reuse emitters", &model.bulk_reuse_emitters);
    }
    accent_button(th);
    const bool dispatch_ci = bulk_btn("Dispatch CI");
    accent_button_pop();
    if (dispatch_ci) {
        auto paths = selected_paths();
        if (paths.empty()) {
            model.append_log("[FAIL] No repos selected");
        } else {
            std::vector<std::string> args = {"git", "bulk-release", "--select", select_csv()};
            if (model.release_version[0]) {
                args.push_back("--version");
                args.push_back(model.release_version);
            }
            static const char* bumps[] = {"patch", "minor", "major"};
            const int bi = (model.release_bump < 0 || model.release_bump > 2) ? 0 : model.release_bump;
            args.push_back("--bump");
            args.push_back(bumps[bi]);
            if (!model.release_publish) args.push_back("--no-publish");
            if (!model.bulk_reuse_emitters) args.push_back("--no-reuse-cached-emitters");
            model.append_log("--- Bulk release dispatch ---");
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
        }
    }
    if (bulk_btn("Install & push CI")) {
        auto paths = selected_paths();
        if (paths.empty()) {
            model.append_log("[FAIL] No repos selected");
        } else {
            model.append_log("--- Bulk install CI ---");
            retcomm::studio::run_project_studio_async(
                model, {"git", "bulk-install-ci", "--select", select_csv()}, nullptr);
        }
    }
    end_wrapped_line();
    ImGui::TextColored(th.text_muted,
                       "Dispatch runs release.yml via gh on each selected game repo.");

    ImGui::Separator();
    field_row("##bulk_msg", "Message", model.bulk_msg, sizeof(model.bulk_msg), kLabelW);
    {
        left_label("Pull mode", kLabelW);
        ImGui::SetNextItemWidth(140.f);
        const char* modes[] = {"ff-only", "rebase", "merge", "reset"};
        int mode = model.bulk_pull_mode;
        if (mode < 0 || mode > 3) mode = 0;
        if (ImGui::Combo("##bulk_pull_mode", &mode, modes, 4)) model.bulk_pull_mode = mode;
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "Bulk pull strategy:\n"
                "ff-only — refuse if histories diverged\n"
                "rebase — replay local commits on origin\n"
                "merge — create a merge commit\n"
                "reset — match origin (hard)");
        }
    }

    auto run_bulk = [&](const char* sub) {
        auto paths = selected_paths();
        if (paths.empty()) {
            model.append_log("[FAIL] No repos selected");
            return;
        }
        std::vector<std::string> args = {"git", sub, "--select", select_csv()};
        if (std::strcmp(sub, "bulk-commit") == 0) {
            args.push_back("--message");
            args.push_back(model.bulk_msg);
        }
        // bulk-status has no target flags; others need at least one.
        if (std::strcmp(sub, "bulk-status") != 0) {
            if (!(model.bulk_tgt_game || model.bulk_tgt_modules || model.bulk_tgt_psx ||
                  model.bulk_tgt_nested)) {
                model.append_log(
                    std::string("[FAIL] Enable at least one Target (Game / Modules / ") +
                    model.framework() + " / Nested)");
                return;
            }
            if (model.bulk_tgt_game) args.push_back("--game");
            if (model.bulk_tgt_modules) args.push_back("--modules");
            if (model.bulk_tgt_psx) args.push_back("--framework");
            if (model.bulk_tgt_nested) args.push_back("--nested");
        }
        if (std::strcmp(sub, "bulk-pull") == 0) {
            static const char* modes[] = {"ff-only", "rebase", "merge", "reset"};
            const int mi =
                (model.bulk_pull_mode < 0 || model.bulk_pull_mode > 3) ? 0 : model.bulk_pull_mode;
            args.push_back("--mode");
            args.push_back(modes[mi]);
        }
        model.append_log(std::string("--- Bulk ") + sub + " ---");
        retcomm::studio::run_project_studio_async(model, args, nullptr);
    };

    if (bulk_btn("Bulk status")) run_bulk("bulk-status");
    if (bulk_btn("Bulk pull")) run_bulk("bulk-pull");
    if (bulk_btn("Bulk push")) run_bulk("bulk-push");
    if (bulk_btn("Bulk commit")) run_bulk("bulk-commit");
    end_wrapped_line();

    ImGui::BeginChild("bulk_list", ImVec2(0, 0), ImGuiChildFlags_Borders);
    if (ImGui::BeginTable("bulk_tbl", 2,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY |
                              ImGuiTableFlags_BordersInnerV |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("On", ImGuiTableColumnFlags_WidthFixed, 36.f);
        ImGui::TableSetupColumn("Repo", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableHeadersRow();
        ImGuiListClipper clipper;
        clipper.Begin(static_cast<int>(model.repos.size()));
        while (clipper.Step()) {
            for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
                const auto& e = model.repos[static_cast<size_t>(i)];
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                bool* sel = &model.bulk_selected[e.path];
                ImGui::Checkbox(("##b" + std::to_string(i)).c_str(), sel);
                ImGui::TableSetColumnIndex(1);
                ImGui::TextWrapped("%s\n%s", e.label.c_str(), e.path.c_str());
            }
        }
        ImGui::EndTable();
    }
    ImGui::EndChild();
    ImGui::EndDisabled();
}

void draw_build(StudioModel& model, const Theme& th, SDL_Window* window) {
    constexpr float kLabelW = 100.f;
    const bool snes = model.is_snes();
    const bool n64 = model.is_n64();
    const bool cart = model.is_cartridge();
    const std::string root = model.selected_root();
    // psx-runtime is every PSX port's target. Neither cartridge console has a
    // constant to use: a SNES port names its executable after the project, and
    // an N64 port names every target after a slug that appears in neither the
    // project() name nor the executable (GloverRecomp / glover-runtime /
    // glover). Leaving the field blank lets the CLI read the right one out of
    // the repo instead of guessing here.
    if (cart && std::strcmp(model.build_target, "psx-runtime") == 0)
        model.build_target[0] = '\0';
    if (!cart && model.build_target[0] == '\0')
        std::snprintf(model.build_target, sizeof(model.build_target), "psx-runtime");
    ImGui::BeginChild("##build_scroll", ImVec2(0, 0));
    ImGui::BeginDisabled(model.busy.load() || root.empty());
    field_row("##bdir", "Build dir", model.build_dir, sizeof(model.build_dir), kLabelW);
    field_row("##btype", "Build type", model.build_type, sizeof(model.build_type), kLabelW);
    field_row("##btarget", "Target", model.build_target, sizeof(model.build_target), kLabelW);
    if (cart && model.build_target[0] == '\0') {
        left_label("", kLabelW);
        ImGui::TextColored(
            th.text_muted,
            n64 ? "Blank = the n64lle_add_runtime_target() name in this repo."
                : "Blank = the CMake project() name in this repo.");
    }
    generator_combo("##bgen", model.build_generator, sizeof(model.build_generator), kLabelW, root,
                    model.build_dir);
    field_row("##bjobs", "Jobs", model.build_jobs, sizeof(model.build_jobs), kLabelW);
    field_row("##bextra", "Extra cmake", model.build_extra, sizeof(model.build_extra), kLabelW);

    // ---- debug tools -------------------------------------------------------
    // The Frames tab (and every tools/gpu_*.py capture) needs a runtime that
    // actually starts its TCP debug server, and a plain Release build does not.
    // Rather than leave that as folklore, show what the configured build dir
    // will do and make the flag a control.
    // SNES has a debug server too — snesrecomp/runner/src/debug_server.c, gated
    // by SNESRECOMP_ENABLE_TRACE instead of PSX_DEBUG_TOOLS. It used to be
    // hidden here on the claim that "the SNES runner does not have one".
    if (n64) {
        // n64lle has no build option here: the rings and the debug server are
        // always compiled in, and the port's own game.toml [runtime].debug_port
        // decides whether one listens. Offering a toggle that sends an unused
        // -D would be a control that does nothing.
        left_label("Debug tools", kLabelW);
        ImGui::TextColored(th.text_muted,
                           "Always built on n64lle; game.toml [runtime].debug_port "
                           "decides whether a server listens (0 = ephemeral).");
    } else {
    left_label("Debug tools", kLabelW);
    ImGui::SetNextItemWidth(240.f);
    ImGui::Combo("##bdbgtools", &model.build_debug_tools,
                 snes ? "Leave to build type\0ON  — TCP debug server\0OFF — lean release\0"
                      : "Leave to build type\0ON  — TCP debug server\0OFF — lean release\0");
    ImGui::SameLine();
    if (ImGui::Button("Configure for debugging")) {
        std::snprintf(model.build_type, sizeof(model.build_type), "Release");
        model.build_debug_tools = 1;
    }
    if (ImGui::IsItemHovered()) {
        ImGui::SetTooltip(
            "Sets Build type = Release and Debug tools = ON, then press Configure.\n"
            "A real Debug build of a recomp is far too slow to reach the frame you\n"
            "are chasing; Release with the debug server is what you want.");
    }
    {
        const auto dbg = retcomm::studio::probe_debug_tools(root, model.build_dir);
        left_label("", kLabelW);
        if (!dbg.configured) {
            ImGui::TextColored(th.text_muted, "%s", dbg.summary.c_str());
        } else if (dbg.enabled) {
            ImGui::TextColored(th.good, "%s", dbg.summary.c_str());
        } else {
            ImGui::TextColored(th.warn, "%s", dbg.summary.c_str());
        }
    }
    } // Debug-tools toggle: PSX_DEBUG_TOOLS on psxrecomp,
      // SNESRECOMP_ENABLE_TRACE on snesrecomp.

    // ---- execution policy (SNES only) --------------------------------------
    // snesrecomp layers optimized (HLE) replacements over the faithful LLE
    // floor, and OPTIMIZATION.md §2 fixes the vocabulary: off/on/force/verify/
    // auto. -DSNESRECOMP_EXECUTION_DEFAULT chooses which one a build STARTS
    // from; the policy itself is resolved at runtime, so the Env box below can
    // still change it without a rebuild. Shown only where the option exists —
    // psxrecomp and n64lle have no counterpart, and a port pinned to an older
    // snesrecomp would collect cmake's unused-variable warning.
    if (snes) {
        const auto exec = retcomm::studio::probe_exec_mode(root, model.build_dir);
        left_label("Execution", kLabelW);
        if (!exec.supported) {
            ImGui::TextColored(th.text_muted, "%s", exec.summary.c_str());
        } else {
            ImGui::SetNextItemWidth(240.f);
            ImGui::Combo("##bexecmode", &model.build_exec_mode,
                         "Leave to framework default\0"
                         "off     — force the faithful floor\0"
                         "on      — promoted paths, loud fallback\0"
                         "force   — a fallback is a FAILURE (gate runs)\0"
                         "verify  — run both, publish the floor\0"
                         "auto    — promoted when available, else floor\0");
            if (ImGui::IsItemHovered()) {
                ImGui::SetTooltip(
                    "Sets the build's DEFAULT policy (-DSNESRECOMP_EXECUTION_DEFAULT).\n"
                    "A run can still be pointed elsewhere without rebuilding:\n"
                    "  SNESRECOMP_EXECUTION_MODE=off|on|force|verify|auto\n"
                    "  SNESRECOMP_FORCE_FLOOR=1   (beats everything)\n"
                    "Put either in the Env box below to override a launch.");
            }
            left_label("", kLabelW);
            ImGui::TextColored(th.text_muted, "%s", exec.summary.c_str());
            if (model.build_exec_mode == 3) {
                // force makes a fallback fatal. That is a gate policy; a build
                // left on it will hard-fail a player on the first unsupported
                // input, so say so here rather than only in cmake's warning.
                left_label("", kLabelW);
                ImGui::TextColored(th.warn,
                                   "force is for gate runs — a fallback to the floor "
                                   "becomes a failure, not a recovery.");
            }
        }
    }

    field_row("##bexe", "Exe", model.build_exe, sizeof(model.build_exe), kLabelW);
    field_row("##bargs", "Launch args", model.build_launch_args, sizeof(model.build_launch_args),
              kLabelW);
    left_label("Env", kLabelW);
    ImGui::InputTextMultiline("##benv", model.build_env, sizeof(model.build_env),
                              ImVec2(ImGui::GetContentRegionAvail().x, 100.f));

    auto base = [&](const char* sub) {
        std::vector<std::string> args = {"build", sub, "--root", root, "--build-dir",
                                         model.build_dir};
        if (model.build_type[0] && std::strcmp(sub, "configure") == 0) {
            args.push_back("--build-type");
            args.push_back(model.build_type);
        }
        if (model.build_generator[0] && std::strcmp(sub, "configure") == 0) {
            args.push_back("--generator");
            args.push_back(model.build_generator);
        }
        if (std::strcmp(sub, "configure") == 0) {
            // --extra is one shell string, so the flag rides along with
            // whatever the user typed rather than replacing it.
            std::string extra = model.build_extra;
            if (model.build_debug_tools == 1 || model.build_debug_tools == 2) {
                const bool on = (model.build_debug_tools == 1);
                if (!extra.empty()) extra += " ";
                // Different option name per framework; same meaning to the
                // user. n64lle declares no such option — its rings and debug
                // server are always built — so on N64 nothing is appended
                // rather than a define cmake would warn about as unused.
                if (model.is_snes())
                    extra += on ? "-DSNESRECOMP_ENABLE_TRACE=ON"
                                : "-DSNESRECOMP_ENABLE_TRACE=OFF";
                else if (!model.is_n64())
                    extra += on ? "-DPSX_DEBUG_TOOLS=ON" : "-DPSX_DEBUG_TOOLS=OFF";
            }
            // Execution policy default. Index 0 leaves the framework's own
            // default alone; anything else pins it. Re-probed rather than
            // trusted from the draw pass, and skipped when the port's pinned
            // snesrecomp does not declare the option — sending it there would
            // be a define cmake reports as unused.
            if (model.is_snes() && model.build_exec_mode > 0) {
                static const char* kExecModes[] = {"off", "on", "force", "verify", "auto"};
                const int idx = model.build_exec_mode - 1;
                if (idx >= 0 && idx < 5 &&
                    retcomm::studio::probe_exec_mode(root, model.build_dir).supported) {
                    if (!extra.empty()) extra += " ";
                    extra += std::string("-DSNESRECOMP_EXECUTION_DEFAULT=") + kExecModes[idx];
                }
            }
            if (!extra.empty()) {
                // "--extra=<value>", not two argv entries: cmake args start
                // with '-', and argparse rejects a leading-dash value for an
                // option expecting one argument ("expected one argument").
                // The '=' form is the only spelling that survives it.
                args.push_back("--extra=" + extra);
            }
        }
        if (model.build_target[0] && std::strcmp(sub, "compile") == 0) {
            args.push_back("--target");
            args.push_back(model.build_target);
        }
        if (model.build_jobs[0] && std::strcmp(sub, "compile") == 0) {
            args.push_back("--jobs");
            args.push_back(model.build_jobs);
        }
        return args;
    };

    auto build_btn = [](const char* label) -> bool {
        const float need = widget_label_width(label) + ImGui::GetStyle().ItemSpacing.x;
        if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x &&
            ImGui::GetContentRegionAvail().x < need)
            ImGui::NewLine();
        const bool hit = ImGui::Button(label);
        ImGui::SameLine();
        return hit;
    };
    accent_button(th);
    const bool cfg = build_btn("Configure");
    accent_button_pop();
    if (cfg) {
        retcomm::studio::run_project_studio_async(model, base("configure"), nullptr);
    }
    if (snes) {
        // One button, because there is one step: the project's own
        // tools/regen.sh, which verifies the ROM against the digests this port
        // was pinned to before emitting anything. There is no BIOS half and no
        // separate emitter build on a cartridge.
        if (build_btn("Regenerate C from ROM")) {
            // Not a failure: regen.sh looks for a known filename at the repo
            // root when it is handed no --rom, so this run may still succeed.
            // Logging [FAIL] here and then running anyway was reporting a
            // verdict before there was one.
            if (!model.disc_cue[0]) {
                model.append_log(
                    "No ROM set on the Migrate tab — letting regen.sh look for "
                    "one at the repo root.");
            }
            std::vector<std::string> args = {"build", "generate", "--root", root};
            if (model.disc_cue[0]) {
                args.push_back("--rom");
                args.push_back(model.disc_cue);
            }
            if (!model.snes_regen_verify) args.push_back("--no-verify");
            if (model.snes_regen_cfg_roots) args.push_back("--cfg-roots");
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
        }
        // snesrecomp's runner/src is organised into layer folders, and moving
        // a file between them is a rename with no content change — so nothing
        // objects until a port configures and cmake reports "Cannot find
        // source file", ONE file per target, in the GAME's repo, for a defect
        // that lives in the framework. A stale list of thirty reads as three
        // unrelated bugs in the port. That is not a hypothetical: one
        // reorganisation reached three ports separately that way, each of
        // which debugged it alone.
        //
        // Two buttons rather than one control with a checkbox, because the
        // second one WRITES: Check is a question anybody can ask at any time,
        // Repair edits sources in both the framework and this port.
        const bool chk = build_btn("Check runner paths");
        if (ImGui::IsItemHovered()) {
            ImGui::SetTooltip(
                "Runs the pinned snesrecomp's tools/check_runner_paths.py over\n"
                "the framework and then over this port. Reports every reference\n"
                "to a runner source that is not where it says it is — build\n"
                "files, shell harnesses, and this port's own CMakeLists.\n"
                "Reads only; nothing is written.");
        }
        if (chk) {
            retcomm::studio::run_project_studio_async(
                model, {"build", "check-paths", "--root", root}, nullptr);
        }
        const bool rep = build_btn("Repair runner paths");
        if (ImGui::IsItemHovered()) {
            ImGui::SetTooltip(
                "The same audit with --fix: rewrites every reference whose file\n"
                "is findable under runner/src, in BOTH the pinned snesrecomp and\n"
                "this port. Anything ambiguous — two layer folders carrying the\n"
                "same basename — is reported and left alone rather than guessed.\n"
                "Edits tracked files: run it on a clean tree so `git diff` shows\n"
                "you exactly what moved.");
        }
        if (rep) {
            retcomm::studio::run_project_studio_async(
                model, {"build", "check-paths", "--root", root, "--fix"}, nullptr);
        }
    } else if (n64) {
        // Two buttons, and the ORDER is the point: n64lle is not
        // add_subdirectory()'d, so a port cannot even configure until the
        // framework has been built out of tree into build-n64lle/. Everything
        // on this row after that runs inside the port's own CMake graph.
        //
        // What is deliberately absent, and why:
        //   * Ensure BIOS       — a cartridge boots from its own reset vector.
        //                         There is no BIOS image, and the CLI refuses
        //                         the subcommand on a cartridge.
        //   * Generate emitters — psxrecomp builds psxrecomp-game and
        //                         psxrecomp-bios as separate binaries to run.
        //                         n64lle has one emitter, n64emit, and it is
        //                         produced by the framework build below; there
        //                         is no second one to keep in step.
        if (build_btn("Build framework")) {
            std::vector<std::string> args = {"build", "framework", "--root", root,
                                             "--build-type", model.build_type};
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
        }
        if (build_btn("Generate C from ROM")) {
            // n64lle harvests the real boot for a window of frames and then
            // emits; both are custom commands behind the port's own
            // <slug>-generate target, so this is a cmake build, not a script.
            std::vector<std::string> args = {"build", "generate", "--root", root,
                                             "--build-dir", model.build_dir,
                                             "--build-type", model.build_type};
            if (model.disc_cue[0]) {
                // Sets <SLUG>_ROM, the cache variable the port's CMakeLists
                // declares. Studio never moves a dump to make a build work.
                args.push_back("--rom");
                args.push_back(model.disc_cue);
            } else {
                model.append_log(
                    "No ROM set on the Migrate tab — the port's own game.toml "
                    "default dump will be harvested.");
            }
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
        }
    } else {
        if (build_btn("Generate ROM + BIOS C")) {
            model.gen_popup_open = true;
            ImGui::OpenPopup("Generate ROM + BIOS C###gen_rom_bios");
        }
        if (build_btn("Generate emitters")) {
            retcomm::studio::run_project_studio_async(
                model, {"build", "ensure-emitters", "--root", root, "--force"}, nullptr);
        }
        if (build_btn("Ensure BIOS")) {
            retcomm::studio::run_project_studio_async(
                model, {"build", "ensure-bios", "--root", root}, nullptr);
        }
    }
    if (build_btn("Build")) {
        retcomm::studio::run_project_studio_async(model, base("compile"), nullptr);
    }
    if (build_btn("Configure + Build")) {
        retcomm::studio::run_project_studio_async(
            model, base("configure"), [&model, root](RunResult r) {
                if (!r.ok()) return;
                std::vector<std::string> args = {"build", "compile", "--root", root,
                                                 "--build-dir", model.build_dir};
                if (model.build_target[0]) {
                    args.push_back("--target");
                    args.push_back(model.build_target);
                }
                if (model.build_jobs[0]) {
                    args.push_back("--jobs");
                    args.push_back(model.build_jobs);
                }
                retcomm::studio::run_project_studio_async(model, args, nullptr);
            });
    }
    if (build_btn("Launch")) {
        std::vector<std::string> args = {"build", "run", "--root", root, "--build-dir",
                                         model.build_dir};
        if (model.build_exe[0]) {
            args.push_back("--exe");
            args.push_back(model.build_exe);
        }
        if (model.build_env[0]) {
            args.push_back("--env");
            args.push_back(model.build_env);
        }
        // One --args, built here. Pushing a second one would silently win and
        // drop whatever the user typed in the Launch args field.
        std::string extra = model.build_launch_args;
        if (snes) {
            // Launching from Studio is a person pressing a button, so the
            // pre-boot launcher belongs on screen. A ROM on the argv used to
            // suppress it, which meant it never appeared from here at all.
            if (extra.find("--launcher") == std::string::npos &&
                extra.find("--no-launcher") == std::string::npos) {
                if (!extra.empty()) extra += " ";
                extra += "--launcher";
            }
        }
        if (!extra.empty()) {
            // One argv element, `--args=<value>`. Passed as two elements,
            // argparse reads a value that begins with '-' as the NEXT option
            // and fails with "argument --args: expected one argument" — which
            // is exactly what "--args" "--launcher" did. The = form has no
            // such ambiguity.
            args.push_back("--args=" + extra);
        }
        // A cartridge runner takes the ROM as a positional; without it the game
        // prints its usage and exits 1. This is the same path Migrate recorded
        // and Regenerate already uses, so a launch cannot run a different ROM
        // than the C was generated from.
        if (snes && model.disc_cue[0]) {
            args.push_back("--rom");
            args.push_back(model.disc_cue);
        }
        retcomm::studio::run_project_studio_async(model, args, nullptr);
    }
    if (build_btn("Bundle + Export##local_pkg")) {
        std::vector<std::string> args = {"build", "package", "--root", root, "--build-dir",
                                         model.build_dir};
        if (model.build_exe[0]) {
            args.push_back("--exe");
            args.push_back(model.build_exe);
        }
        retcomm::studio::run_project_studio_async(
            model, std::move(args), [&model, window](RunResult r) {
                if (!r.ok()) {
                    model.set_status("Bundle failed");
                    return;
                }
                const std::string zip = parse_zip_from_output(r.stdout_text);
                if (zip.empty() || !fs::is_regular_file(zip)) {
                    model.append_log("[FAIL] Bundle produced no zip under dist/");
                    model.set_status("Bundle: no zip");
                    return;
                }
                model.append_log("[OK] Packaged " + zip);
                model.set_status("Choose export location…");
                begin_export_zip(model, window, zip);
            });
    }
    // Stop must stay clickable while Launch holds busy (streams game diagnostics).
    ImGui::EndDisabled();
    ImGui::BeginDisabled(root.empty());
    if (build_btn("Stop")) {
        retcomm::studio::run_project_studio_async(model, {"build", "stop"}, nullptr);
    }
    ImGui::EndDisabled();
    ImGui::BeginDisabled(model.busy.load() || root.empty());
    end_wrapped_line();
    if (snes) {
        checkbox_wrapped("Verify ROM digests", &model.snes_regen_verify);
        checkbox_wrapped("Seed roots from recomp/*.cfg", &model.snes_regen_cfg_roots);
        end_wrapped_line();
        if (!model.snes_regen_verify) {
            ImGui::TextColored(th.warn,
                               "Digest verification off — generated C will not match what this "
                               "port was pinned against, and divergence you then chase will not "
                               "be in the recompiler.");
        }
        wrapped(th.text_muted,
                "Bundle + Export zips the build dir as it stands into dist/ and opens a "
                "save dialog. Build first — it does not rebuild, and it refuses to package "
                "ROM data. An ordinary (playable) build dir gives a "
                "<prefix>-<ver>-<tag>-local.zip: exe, assets, mods and translations, no "
                "source tree — a local build, not a release. A build dir configured "
                "-DSNESRECOMP_SETUP_HOST=ON instead gets the repo's own "
                "scripts/package_release.sh, i.e. the setup pack a release ships.");
    } else if (n64) {
        wrapped(th.text_muted,
                "Build framework runs n64lle/tools/build_framework.sh — the framework's "
                "OWN script, shared by every port — never a Studio reimplementation of "
                "it. On a port pinned to an n64lle from before that script existed it "
                "falls back to the port's tools/build_framework.sh, which is otherwise "
                "just a shim onto the shared one. Configure cannot succeed until it has "
                "run: a port resolves already-built libraries and tools out of "
                "build-n64lle/ rather than add_subdirectory()ing the framework.");
        ImGui::Spacing();
        wrapped(th.text_muted,
                "It used to run the port's own copy, on the reasoning that the copy "
                "carries the flags the pinned n64lle revision needs. That holds for what "
                "a port ADDS and fails for what the framework later REQUIRES: a private "
                "copy cannot inherit a fix. -DN64LLE_RSP_CENSUS=1 reached one port's copy "
                "and no other, so seven of nine N64 ports harvested no RSP microcode and "
                "ran the RSP fully interpreted while every build reported success.");
        ImGui::Spacing();
        wrapped(th.text_muted,
                "Bundle + Export zips the build dir as it stands (exe + assets, no ROM) "
                "into dist/ and opens a save dialog. Build first — it does not rebuild.");
    } else {
        ImGui::TextColored(th.text_muted,
                           "Bundle + Export zips the build dir as it stands (exe + assets + "
                           "bundled OpenBIOS, no disc) into dist/ and opens a save dialog. "
                           "Build first — it does not rebuild.");
    }

#if !defined(_WIN32)
    // Both consoles cross-build, each through its own script: the PSX one also
    // cross-compiles two emitters, stages OpenBIOS and drives PSX_NETPLAY,
    // which a cartridge has none of. `build mingw` routes on the session's
    // platform, so the controls below are identical.
    //
    // n64lle ships no cross-build script at all (platform_has_mingw), so the
    // whole section is ABSENT there rather than present and dead — the same
    // rule the tabs follow.
    if (platform_has_mingw(model.platform)) {
    ImGui::Separator();
    ImGui::TextUnformatted("Windows (MinGW)");
    if (snes) {
        wrapped(th.text_muted,
                "Cross-compile a Windows .exe from Linux (no GitHub CI). Needs "
                "mingw-w64-gcc and a MinGW SDL — the backend is chosen from what "
                "the sysroot actually has, since distro MinGW packages are "
                "usually SDL2 while a native SNES build defaults to SDL3. "
                "Regenerate C from ROM first: a cartridge build links src/gen/ "
                "and there is nothing to compile without it. Bundle + Export "
                "packages the existing MinGW build into a zip.");
    } else {
    ImGui::TextColored(th.text_muted,
                       "Cross-compile a Windows .exe from Linux (no GitHub CI). "
                       "Needs mingw-w64-gcc + mingw-w64-sdl2. Full playable builds "
                       "need generated game C first (Generate / --ensure). "
                       "Netplay is ON when game.toml has [netplay] (reconfigure if "
                       "an older MinGW cache has PSX_NETPLAY=OFF). "
                       "Bundle + Export packages the existing MinGW build into a zip "
                       "and opens a save dialog.");
    }
    field_row("##mingw_bdir", "MinGW dir", model.mingw_build_dir, sizeof(model.mingw_build_dir),
              kLabelW);
    // Setup-host, Ensure and Dynamic SDL are psxrecomp concepts — a setup
    // wizard host, "generate the game C first", and a shared-SDL link. The
    // SNES script has none of them: it errors when src/gen/ is empty rather
    // than generating, and picks its SDL from the sysroot. Showing dead
    // checkboxes would just invite a run that ignores them.
    if (!snes) checkbox_wrapped("Setup-host", &model.mingw_setup_host);
    checkbox_wrapped("Package zip", &model.mingw_package);
    if (!snes) {
        checkbox_wrapped("Ensure first", &model.mingw_ensure);
        checkbox_wrapped("Dynamic SDL", &model.mingw_dynamic);
    }
    end_wrapped_line();
    auto mingw_build_dir_arg = [&]() -> std::string {
        // Match script defaults: setup-host → build-mingw-setup when UI still says build-mingw.
        if (model.mingw_setup_host &&
            (model.mingw_build_dir[0] == '\0' ||
             std::strcmp(model.mingw_build_dir, "build-mingw") == 0)) {
            return "build-mingw-setup";
        }
        return model.mingw_build_dir[0] ? std::string(model.mingw_build_dir) : std::string();
    };
    accent_button(th);
    const bool mingw_build = build_btn("MinGW Configure + Build");
    accent_button_pop();
    if (mingw_build) {
        std::vector<std::string> args = {"build", "mingw", "--root", root};
        const std::string bdir = mingw_build_dir_arg();
        if (!bdir.empty()) {
            args.push_back("--build-dir");
            args.push_back(bdir);
        }
        if (!snes && model.mingw_setup_host) args.push_back("--setup-host");
        if (model.mingw_package) args.push_back("--package");
        if (!snes && model.mingw_ensure) args.push_back("--ensure");
        if (!snes && model.mingw_dynamic) args.push_back("--dynamic");
        if (model.build_jobs[0]) {
            args.push_back("--jobs");
            args.push_back(model.build_jobs);
        }
        if (model.build_extra[0]) {
            // Same leading-dash argparse trap as the configure path above.
            args.push_back(std::string("--extra=") + model.build_extra);
        }
        retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
    }
    if (build_btn("Bundle + Export##mingw_pkg")) {
        std::vector<std::string> args = {"build", "mingw", "--root", root, "--package-only"};
        const std::string bdir = mingw_build_dir_arg();
        if (!bdir.empty()) {
            args.push_back("--build-dir");
            args.push_back(bdir);
        }
        if (!snes && model.mingw_setup_host) args.push_back("--setup-host");
        if (!snes && model.mingw_dynamic) args.push_back("--dynamic");
        retcomm::studio::run_project_studio_async(
            model, std::move(args), [&model, window](RunResult r) {
                if (!r.ok()) {
                    model.set_status("MinGW package failed");
                    return;
                }
                const std::string zip = parse_zip_from_output(r.stdout_text);
                if (zip.empty() || !fs::is_regular_file(zip)) {
                    model.append_log("[FAIL] MinGW package produced no zip under dist/");
                    model.set_status("MinGW package: no zip");
                    return;
                }
                model.append_log("[OK] Packaged " + zip);
                model.set_status("Choose export location…");
                begin_export_zip(model, window, zip);
            });
    }
    end_wrapped_line();
    } // !snes
#else
    if (!snes) {
        ImGui::Separator();
        ImGui::TextUnformatted("Windows (MinGW)");
        ImGui::TextColored(th.text_muted,
                           "MinGW cross-build is for Linux hosts. On Windows use the "
                           "native Configure / Build buttons above (or CI).");
    }
#endif

    ImGui::EndDisabled();

    if (snes) model.gen_popup_open = false;
    if (model.gen_popup_open) ImGui::OpenPopup("Generate ROM + BIOS C###gen_rom_bios");
    if (ImGui::BeginPopupModal("Generate ROM + BIOS C###gen_rom_bios", &model.gen_popup_open,
                               ImGuiWindowFlags_AlwaysAutoResize)) {
        ImGui::TextWrapped(
            "Regenerate BIOS backends and game C from the disc (psxrecomp_cli generate).");
        ImGui::Spacing();
        ImGui::TextUnformatted("BIOS source");
        ImGui::RadioButton("OpenBIOS (bundled, MIT)", &model.gen_bios_mode, 0);
        ImGui::RadioButton("Retail SCPH1001.BIN", &model.gen_bios_mode, 1);
        if (model.gen_bios_mode == 1) {
            ImGui::SetNextItemWidth(360.f);
            ImGui::InputText("##gen_scph", model.gen_scph_path, sizeof(model.gen_scph_path));
            ImGui::SameLine();
            ImGui::BeginDisabled(model.file_pick_busy);
            if (ImGui::Button("Browse…##gen_scph_browse")) {
                pick_file(model, window, "build_scph", "BIOS dump", "bin;BIN;rom");
            }
            ImGui::EndDisabled();
            ImGui::TextColored(th.text_muted, "Select your SCPH1001.bin dump.");
        } else {
            ImGui::TextColored(th.text_muted, "No retail dump needed — uses bundled OpenBIOS.");
        }
        ImGui::Spacing();
        ImGui::Separator();
        const bool can_run =
            !root.empty() &&
            (model.gen_bios_mode == 0 || model.gen_scph_path[0] != '\0') && !model.busy.load();
        ImGui::BeginDisabled(!can_run);
        accent_button(th);
        if (ImGui::Button("Generate", ImVec2(120.f, 0))) {
            std::vector<std::string> args = {"build", "generate", "--root", root};
            if (model.disc_cue[0]) {
                args.push_back("--disc");
                args.push_back(model.disc_cue);
            }
            if (model.gen_bios_mode == 1 && model.gen_scph_path[0]) {
                args.push_back("--bios");
                args.push_back(model.gen_scph_path);
            }
            model.gen_popup_open = false;
            ImGui::CloseCurrentPopup();
            retcomm::studio::run_project_studio_async(model, std::move(args), nullptr);
        }
        accent_button_pop();
        ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(120.f, 0))) {
            model.gen_popup_open = false;
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    } else if (!ImGui::IsPopupOpen("Generate ROM + BIOS C###gen_rom_bios")) {
        // Closed via X / Escape — keep flag in sync.
        model.gen_popup_open = false;
    }

    ImGui::EndChild();
}

// Draw one activity line; http(s) URLs become ImGui::TextLinkOpenURL (SDL_OpenURL).
void draw_log_line_with_links(const std::string& line, const ImVec4& col) {
    if (line.empty()) {
        ImGui::TextUnformatted("");
        return;
    }
    auto url_start_at = [&](size_t from) -> size_t {
        const size_t a = line.find("https://", from);
        const size_t b = line.find("http://", from);
        if (a == std::string::npos) return b;
        if (b == std::string::npos) return a;
        return std::min(a, b);
    };
    auto url_end_at = [&](size_t start) -> size_t {
        size_t end = start;
        while (end < line.size()) {
            const unsigned char c = static_cast<unsigned char>(line[end]);
            if (c <= 32 || c == '"' || c == '\'' || c == '<' || c == '>' || c == '`') break;
            ++end;
        }
        while (end > start) {
            const char t = line[end - 1];
            if (t == '.' || t == ',' || t == ';' || t == ':' || t == '!' || t == '?' || t == ')' ||
                t == ']' || t == '}')
                --end;
            else
                break;
        }
        return end;
    };

    size_t i = 0;
    bool first = true;
    auto emit_text = [&](size_t from, size_t to) {
        if (to <= from) return;
        if (!first) ImGui::SameLine(0.f, 0.f);
        const std::string piece = line.substr(from, to - from);
        ImGui::TextColored(col, "%s", piece.c_str());
        first = false;
    };
    while (i < line.size()) {
        const size_t start = url_start_at(i);
        if (start == std::string::npos) {
            emit_text(i, line.size());
            break;
        }
        emit_text(i, start);
        const size_t end = url_end_at(start);
        if (end <= start) {
            emit_text(start, start + 1);
            i = start + 1;
            continue;
        }
        const std::string url = line.substr(start, end - start);
        if (!first) ImGui::SameLine(0.f, 0.f);
        ImGui::TextLinkOpenURL(url.c_str(), url.c_str());
        first = false;
        i = end;
    }
    if (first) ImGui::TextColored(col, "%s", line.c_str());
}

// One display row of the Activity log: a byte range inside one source line,
// after word-wrapping to the current panel width. `line` stays with the row so
// the colour can be judged from the whole line — a continuation of a [FAIL]
// must not turn grey halfway through.
struct LogRow {
    int line = 0;
    int begin = 0;
    int end = 0;
};

// Wrapping and ImGuiListClipper want opposite things: the clipper needs items
// of a known uniform height, and a wrapped line is not one item. So wrap once
// into rows and clip over *rows*, which are one text line each. Rebuilt only
// when the log changes or the panel is resized — laying out four thousand
// lines every frame to draw thirty of them is what the clipper is here to
// avoid, and dropping it to get wrapping would trade one for the other.
struct LogWrapCache {
    std::uint64_t revision = ~0ull;
    float width = -1.f;
    std::vector<LogRow> rows;
};

// Continuation rows are indented so a wrapped line reads as one line rather
// than two entries. They wrap that much narrower to pay for it.
constexpr float kLogWrapIndent = 16.f;

void rebuild_log_rows(LogWrapCache& cache, const std::vector<std::string>& lines, float wrap_w) {
    cache.rows.clear();
    if (wrap_w <= kLogWrapIndent * 2.f) wrap_w = kLogWrapIndent * 2.f + 1.f;
    ImFont* font = ImGui::GetFont();
    const float scale = font->FontSize > 0.f ? ImGui::GetFontSize() / font->FontSize : 1.f;
    for (int i = 0; i < static_cast<int>(lines.size()); ++i) {
        const std::string& src = lines[static_cast<size_t>(i)];
        if (src.empty()) {
            cache.rows.push_back({i, 0, 0});
            continue;
        }
        const char* begin = src.c_str();
        const char* end = begin + src.size();
        const char* p = begin;
        while (p < end) {
            const float w = (p == begin) ? wrap_w : wrap_w - kLogWrapIndent;
            const char* stop = font->CalcWordWrapPositionA(scale, p, end, w);
            // A single glyph wider than the panel would otherwise loop forever.
            if (stop <= p) stop = p + 1;
            cache.rows.push_back({i, static_cast<int>(p - begin), static_cast<int>(stop - begin)});
            p = stop;
            // ImGui's own renderer eats the blanks a wrap lands on; matching it
            // keeps a wrapped line from starting with a stray space.
            while (p < end && (*p == ' ' || *p == '\t')) ++p;
        }
    }
}

void draw_log_collapsed_bar(StudioModel& model, const Theme& th) {
    constexpr float kBarH = 40.f;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12.f, 6.f));
    ImGui::BeginChild("log_collapsed", ImVec2(0, kBarH), ImGuiChildFlags_Borders);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(10.f, 3.f));
    const float btn_h = ImGui::GetFrameHeight();
    const float y = std::max(0.f, (ImGui::GetContentRegionAvail().y - btn_h) * 0.5f);
    ImGui::SetCursorPosY(ImGui::GetCursorPosY() + y);
    ImGui::PushStyleColor(ImGuiCol_Text, th.text_muted);
    ImGui::TextUnformatted("ACTIVITY");
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted, "(collapsed)");
    {
        constexpr float kShowW = 64.f;
        const float right = ImGui::GetWindowContentRegionMax().x;
        ImGui::SameLine();
        ImGui::SetCursorPosX(std::max(ImGui::GetCursorPosX() + 8.f, right - kShowW));
        if (ImGui::Button("Show", ImVec2(kShowW, 0))) model.log_expanded = true;
    }
    ImGui::PopStyleVar();
    ImGui::EndChild();
    ImGui::PopStyleVar();
}

void draw_log(StudioModel& model, const Theme& th, float height, SDL_Window* window) {
    if (height < 60.f) height = 60.f;
    ImGui::BeginChild("log", ImVec2(0, height), ImGuiChildFlags_Borders);
    ImGui::PushStyleColor(ImGuiCol_Text, th.text_muted);
    ImGui::TextUnformatted("ACTIVITY");
    ImGui::PopStyleColor();

    std::vector<std::string> lines;
    bool stick = false;
    std::uint64_t revision = 0;
    {
        std::lock_guard<std::mutex> lock(model.mu);
        lines = model.log_lines;
        revision = model.log_revision;
        stick = model.log_scroll_bottom;
        if (stick) model.log_scroll_bottom = false;
    }

    ImGui::SameLine();
    {
        const float hide_w =
            ImGui::CalcTextSize("Hide").x + ImGui::GetStyle().FramePadding.x * 2.f;
        const float export_w =
            ImGui::CalcTextSize("Export").x + ImGui::GetStyle().FramePadding.x * 2.f;
        const float copy_w =
            ImGui::CalcTextSize("Copy").x + ImGui::GetStyle().FramePadding.x * 2.f;
        const float clear_w =
            ImGui::CalcTextSize("Clear").x + ImGui::GetStyle().FramePadding.x * 2.f;
        const float gap = ImGui::GetStyle().ItemSpacing.x;
        const float right = ImGui::GetWindowContentRegionMax().x;
        ImGui::SetCursorPosX(std::max(
            ImGui::GetCursorPosX(),
            right - hide_w - gap - export_w - gap - copy_w - gap - clear_w));
        if (ImGui::SmallButton("Hide")) model.log_expanded = false;
        ImGui::SameLine();
        bool file_busy = false;
        {
            std::lock_guard<std::mutex> lock(model.pick_mu);
            file_busy = model.file_pick_busy;
        }
        ImGui::BeginDisabled(file_busy || window == nullptr);
        if (ImGui::SmallButton("Export")) begin_export_activity_log(model, window);
        ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::SmallButton("Copy")) {
            constexpr size_t kCopyLines = 100;
            const size_t n = lines.size();
            const size_t start = n > kCopyLines ? n - kCopyLines : 0;
            std::string clip;
            for (size_t i = start; i < n; ++i) {
                if (!clip.empty()) clip.push_back('\n');
                clip += lines[i];
            }
            ImGui::SetClipboardText(clip.empty() ? "(no activity yet)" : clip.c_str());
        }
        ImGui::SameLine();
        if (ImGui::SmallButton("Clear")) {
            std::lock_guard<std::mutex> lock(model.mu);
            model.log_lines.clear();
            ++model.log_revision;
        }
    }
    ImGui::Separator();

    // AlwaysVerticalScrollbar, so the wrap width does not change the moment a
    // scrollbar appears. Without it a log sitting exactly at the boundary
    // oscillates: wrapping adds rows, rows add a scrollbar, the scrollbar
    // narrows the panel, which adds more rows.
    ImGui::BeginChild("activity_scroll", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_AlwaysVerticalScrollbar);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(4, 2));

    // One panel, drawn once a frame, so the cache lives with the drawing code
    // rather than in the model — nothing else can observe it.
    static LogWrapCache wrap_cache;
    const float wrap_w = ImGui::GetContentRegionAvail().x;
    if (wrap_cache.revision != revision || wrap_cache.width != wrap_w) {
        rebuild_log_rows(wrap_cache, lines, wrap_w);
        wrap_cache.revision = revision;
        wrap_cache.width = wrap_w;
    }

    ImGuiListClipper clipper;
    clipper.Begin(static_cast<int>(wrap_cache.rows.size()));
    while (clipper.Step()) {
        for (int r = clipper.DisplayStart; r < clipper.DisplayEnd; ++r) {
            const LogRow& row = wrap_cache.rows[static_cast<size_t>(r)];
            const std::string& line = lines[static_cast<size_t>(row.line)];
            // Colour is judged from the whole line, never the fragment: a
            // continuation row of a failure is still part of that failure.
            ImVec4 col = th.text;
            if (line.find("[FAIL]") != std::string::npos ||
                line.find("error:") != std::string::npos)
                col = th.bad;
            else if (line.find("[OK]") != std::string::npos)
                col = th.good;
            else if (!line.empty() && line[0] == '$')
                col = th.text_muted;
            const bool cont = row.begin > 0;
            if (cont) ImGui::Indent(kLogWrapIndent);
            draw_log_line_with_links(
                line.substr(static_cast<size_t>(row.begin),
                            static_cast<size_t>(row.end - row.begin)),
                col);
            if (cont) ImGui::Unindent(kLogWrapIndent);
        }
    }
    if (stick) ImGui::SetScrollHereY(1.0f);
    ImGui::PopStyleVar();
    ImGui::EndChild();
    ImGui::EndChild();
}

} // namespace

int main(int argc, char** argv) {
    (void)argc;

    if (!SDL_Init(SDL_INIT_VIDEO)) {
        std::fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
        return 1;
    }

    const char* glsl = "#version 150";
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_FLAGS, 0);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_CORE);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 2);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 1);
    SDL_GL_SetAttribute(SDL_GL_DEPTH_SIZE, 24);
    SDL_GL_SetAttribute(SDL_GL_STENCIL_SIZE, 8);

    SDL_Window* window =
        SDL_CreateWindow("Retro Studio", 1280, 860, SDL_WINDOW_OPENGL | SDL_WINDOW_RESIZABLE |
                                                          SDL_WINDOW_HIGH_PIXEL_DENSITY);
    if (!window) {
        std::fprintf(stderr, "SDL_CreateWindow failed: %s\n", SDL_GetError());
        return 1;
    }
    SDL_GLContext gl = SDL_GL_CreateContext(window);
    if (!gl) {
        std::fprintf(stderr, "SDL_GL_CreateContext failed: %s\n", SDL_GetError());
        return 1;
    }
    SDL_GL_MakeCurrent(window, gl);
    SDL_GL_SetSwapInterval(1);

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    io.IniFilename = nullptr;

    StudioModel model;
#if defined(RETCOMM_STUDIO_VERSION)
    model.version = RETCOMM_STUDIO_VERSION;
#else
    model.version = "0.0.0";
#endif
    if (argc > 0 && argv[0] && argv[0][0] != '\0') {
        std::error_code ec;
        model.exe_dir = fs::weakly_canonical(fs::path(argv[0]).parent_path(), ec);
        if (ec || model.exe_dir.empty()) model.exe_dir = fs::path(argv[0]).parent_path();
    }
    apply_window_icon(window, model.exe_dir);
    load_fonts(model.exe_dir);

    const Theme th = retcomm::studio::crt_theme();
    retcomm::studio::apply_imgui_style(th);

    ImGui_ImplSDL3_InitForOpenGL(window, gl);
    ImGui_ImplOpenGL3_Init(glsl);

    {
        std::string err;
        if (!retcomm::studio::resolve_runtime(model, &err)) {
            model.append_log("[FAIL] " + err);
            model.set_status(err);
        } else {
            model.append_log("Toolkit: " + model.toolkit_dir.string());
            model.append_log("Python: " + model.python_exe);
            if (!model.toolchain_root.empty())
                model.append_log("Toolchain: " + model.toolchain_root.string());
            if (!model.toolchain_ready) {
                model.toolchain_gate_open = true;
                model.append_log(
                    "[WARN] Portable toolchain Python missing — install required before using Studio.");
                model.set_status("Toolchain required");
            } else {
#if defined(_WIN32)
                const char* home = std::getenv("USERPROFILE");
#else
                const char* home = std::getenv("HOME");
#endif
                const fs::path parent =
                    (home && *home) ? fs::path(home) / "Documents" / "GitHub" : fs::path(".");
                std::snprintf(model.np_parent, sizeof(model.np_parent), "%s",
                              parent.string().c_str());
                // No refresh_repos() here: which index to read is not known
                // until the platform picker is answered, and reading the PSX
                // one "for now" would show a list the user then has to watch
                // get replaced.
                // Startup update check uses github.com tag redirects + a local TTL
                // cache (not api.github.com listing) to avoid rate limits. Also syncs
                // retcomm-catalog into the shared cache and refreshes filters.
                if (!model.startup_update_started) {
                    model.startup_update_started = true;
                    retcomm::studio::run_project_studio_async(
                        model, {"updates", "check", "--json", "--startup"},
                        [&model](RunResult r) { handle_updates_check_result(model, r); },
                        false);
                }
            }
        }
    }

    bool running = true;
    while (running) {
        SDL_Event e;
        while (SDL_PollEvent(&e)) {
            ImGui_ImplSDL3_ProcessEvent(&e);
            if (e.type == SDL_EVENT_QUIT) running = false;
            if (e.type == SDL_EVENT_WINDOW_CLOSE_REQUESTED &&
                e.window.windowID == SDL_GetWindowID(window))
                running = false;
        }
        if (model.request_exit.load()) running = false;

        apply_pending_picks(model);
        retcomm::studio::pump_async_jobs(model);

        // Honour a "Change platform" click here, between frames — clearing the
        // model mid-draw would leave a tab holding an index into a list that
        // no longer exists.
        if (model.platform_pending && !model.busy.load()) {
            model.platform_pending = false;
            model.platform = Platform::None;
            model.reset_for_platform_switch();
            model.set_status("Choose a platform");
        }

        ImGui_ImplOpenGL3_NewFrame();
        ImGui_ImplSDL3_NewFrame();
        ImGui::NewFrame();

        const ImGuiViewport* vp = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(vp->WorkPos);
        ImGui::SetNextWindowSize(vp->WorkSize);
        ImGui::Begin("##studio", nullptr,
                     ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                         ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoBringToFrontOnFocus |
                         ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);

        draw_header(model, th, window);
        // Modal, so it belongs to the frame rather than to whichever tab is up.
        draw_git_settings_popup(model, th);
        ImGui::Separator();

        const float spacing = ImGui::GetStyle().ItemSpacing.y;
        const float avail = ImGui::GetContentRegionAvail().y;
        constexpr float kMinLog = 64.f;
        constexpr float kCollapsed = 40.f;
        constexpr float kMinBody = 180.f;
        constexpr float kSplitH = 6.f;
        float log_h = 0.f;
        float body_h = 0.f;
        if (model.log_expanded) {
            const float chrome = kSplitH + spacing * 2.f;
            const float max_log = std::max(kMinLog, avail - kMinBody - chrome);
            log_h = std::clamp(model.log_h_pref, kMinLog, max_log);
            body_h = avail - log_h - chrome;
            if (body_h < kMinBody) body_h = kMinBody;
        } else {
            body_h = avail - kCollapsed - spacing;
            if (body_h < kMinBody) body_h = kMinBody;
        }

        ImGui::BeginChild("body", ImVec2(0, body_h), ImGuiChildFlags_None);
        if (model.toolchain_gate_open) {
            ImGui::TextColored(th.warn, "Portable toolchain required");
            ImGui::TextWrapped(
                "Retro Studio uses the shared cmake-clang-v1 toolchain (portable Python + "
                "cmake/ninja/clang). It is not installed yet. Install once to continue — "
                "downloads use github.com release files (not the GitHub API).");
            ImGui::Spacing();
            accent_button(th);
            ImGui::BeginDisabled(model.busy.load());
            if (ImGui::Button("Install toolchain", ImVec2(180.f, 0))) {
                retcomm::studio::run_project_studio_async(
                    model, {"updates", "ensure-toolchain"},
                    [&model](RunResult r) {
                        std::string err;
                        retcomm::studio::resolve_runtime(model, &err);
                        if (model.toolchain_ready) {
                            model.toolchain_gate_open = false;
                            model.append_log("[OK] Toolchain ready — " + model.python_exe);
                            model.set_status("Toolchain ready");
#if defined(_WIN32)
                            const char* home = std::getenv("USERPROFILE");
#else
                            const char* home = std::getenv("HOME");
#endif
                            const fs::path parent =
                                (home && *home) ? fs::path(home) / "Documents" / "GitHub"
                                                : fs::path(".");
                            std::snprintf(model.np_parent, sizeof(model.np_parent), "%s",
                                          parent.string().c_str());
                            // Repos load once the platform picker names an index.
                            if (!model.startup_update_started) {
                                model.startup_update_started = true;
                                retcomm::studio::run_project_studio_async(
                                    model, {"updates", "check", "--json", "--startup"},
                                    [&model](RunResult ur) {
                                        handle_updates_check_result(model, ur);
                                    },
                                    false);
                            }
                        } else {
                            model.append_log("[FAIL] Toolchain still missing after install.");
                            if (!r.stdout_text.empty()) model.append_log(r.stdout_text);
                            if (!r.stderr_text.empty()) model.append_log(r.stderr_text);
                        }
                    });
            }
            ImGui::EndDisabled();
            accent_button_pop();
            ImGui::SameLine();
            if (ImGui::Button("Quit", ImVec2(100.f, 0))) model.request_exit.store(true);
        } else if (model.platform == Platform::None) {
            draw_platform_picker(model, th);
        } else if (ImGui::BeginTabBar("##tabs")) {
            if (ImGui::BeginTabItem("Migrate")) {
                draw_migrate(model, th, window);
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("New Project")) {
                draw_new_project(model, th, window);
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("Git / GitHub")) {
                draw_git(model, th);
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("Bulk")) {
                draw_bulk(model, th);
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("Build")) {
                draw_build(model, th, window);
                ImGui::EndTabItem();
            }
            // Same two tabs on the two consoles that have them, in the same
            // order — find the functions, then diagnose what they do — but
            // each backed by its own toolset. PSX reads psxrecomp's analysis
            // bundle and speaks its debug protocol against DuckStation or
            // Beetle; SNES reads recomp/symbols.toml and drives
            // tools/snes_analysis over the Mesen oracle. Different protocols
            // and different tools, not a reskin, so they stay separate draw
            // functions behind matching labels.
            //
            // On N64 only FUNCTIONS is absent: discovery is execution-derived
            // inside the harvest and a port carries no symbol table, so there
            // is nothing for that tab to read, and the CLI refuses `analyze` on
            // the same grounds. Diagnostics is present — it drives n64lle's
            // Ares oracle and its differential gates, which is a different
            // surface from the runtime debug server whose thinness was the
            // original reason both were missing.
            if (model.has_functions_tab() && ImGui::BeginTabItem("Functions")) {
                if (model.is_snes()) draw_snes_functions(model, th, window);
                else                 draw_functions(model, th, window);
                ImGui::EndTabItem();
            }
            if (model.has_diagnostics_tab() && ImGui::BeginTabItem("Diagnostics")) {
                if (model.is_snes())     draw_snes(model, th, window);
                else if (model.is_n64()) draw_n64(model, th, window);
                else                     draw_frames(model, th, window);
                ImGui::EndTabItem();
            }
            ImGui::EndTabBar();
        }
        ImGui::EndChild();

        if (model.log_expanded) {
            ImGui::InvisibleButton("##logsash", ImVec2(-1, kSplitH));
            const ImVec2 sash_min = ImGui::GetItemRectMin();
            const ImVec2 sash_max = ImGui::GetItemRectMax();
            const bool sash_hot = ImGui::IsItemHovered() || ImGui::IsItemActive();
            if (ImGui::IsItemActive()) {
                const float chrome = kSplitH + spacing * 2.f;
                model.log_h_pref = std::clamp(model.log_h_pref - io.MouseDelta.y, kMinLog,
                                              std::max(kMinLog, avail - kMinBody - chrome));
                log_h = std::clamp(model.log_h_pref, kMinLog,
                                   std::max(kMinLog, avail - kMinBody - chrome));
            }
            if (sash_hot) ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeNS);
            {
                // Visible grab line across the sash (brighter while hovered / dragging).
                ImDrawList* dl = ImGui::GetWindowDrawList();
                const float mid_y = 0.5f * (sash_min.y + sash_max.y);
                const ImU32 line_col =
                    sash_hot ? ImGui::GetColorU32(th.text_muted)
                             : ImGui::GetColorU32(ImVec4(th.text_muted.x, th.text_muted.y,
                                                         th.text_muted.z, 0.55f));
                const float thickness = sash_hot ? 2.f : 1.f;
                dl->AddLine(ImVec2(sash_min.x + 8.f, mid_y), ImVec2(sash_max.x - 8.f, mid_y),
                            line_col, thickness);
            }
            draw_log(model, th, log_h, window);
        } else {
            draw_log_collapsed_bar(model, th);
        }

        if (model.update_prompt_open) ImGui::OpenPopup("Updates available###studio_updates");
        if (ImGui::BeginPopupModal("Updates available###studio_updates", &model.update_prompt_open,
                                   ImGuiWindowFlags_AlwaysAutoResize)) {
            ImGui::TextWrapped("%s", model.update_prompt_msg.empty()
                                         ? "Updates are available."
                                         : model.update_prompt_msg.c_str());
            ImGui::Spacing();
            accent_button(th);
            ImGui::BeginDisabled(model.busy.load());
            if (ImGui::Button("Update now", ImVec2(140.f, 0))) {
                std::vector<std::string> args = {"updates", "apply"};
                if (model.update_toolchain_avail && !model.update_studio_avail)
                    args.push_back("--toolchain-only");
                if (model.update_studio_avail && !model.update_toolchain_avail)
                    args.push_back("--studio-only");
                model.update_prompt_open = false;
                ImGui::CloseCurrentPopup();
                retcomm::studio::run_project_studio_async(
                    model, std::move(args),
                    [&model](RunResult) {
                        std::string err;
                        retcomm::studio::resolve_runtime(model, &err);
                    });
            }
            ImGui::EndDisabled();
            accent_button_pop();
            ImGui::SameLine();
            if (ImGui::Button("Later", ImVec2(100.f, 0))) {
                model.update_prompt_open = false;
                ImGui::CloseCurrentPopup();
            }
            ImGui::EndPopup();
        }

        ImGui::End();

        ImGui::Render();
        glViewport(0, 0, (int)(io.DisplaySize.x * io.DisplayFramebufferScale.x),
                   (int)(io.DisplaySize.y * io.DisplayFramebufferScale.y));
        glClearColor(th.background.x, th.background.y, th.background.z, 1.f);
        glClear(GL_COLOR_BUFFER_BIT);
        ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());
        SDL_GL_SwapWindow(window);
    }

    ImGui_ImplOpenGL3_Shutdown();
    ImGui_ImplSDL3_Shutdown();
    ImGui::DestroyContext();
    SDL_GL_DestroyContext(gl);
    SDL_DestroyWindow(window);
    SDL_Quit();
    return 0;
}