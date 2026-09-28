// studio_n64_data.cpp — the N64 Diagnostics tab's data half: tool paths, the
// gate catalogue, and the two parsers.
//
// Split from studio_n64.cpp for the same reason studio_snes_data.cpp is split
// from studio_snes.cpp: none of this needs ImGui, so all of it can be tested
// headlessly (tests/n64_load_test.cpp) against REAL tool output. A parser that
// is only ever exercised by clicking is a parser nobody has checked.

#include "studio/studio_n64.hpp"

#include <nlohmann/json.hpp>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace retcomm::studio {

namespace {

constexpr const char* kOracleTool = "n64_oracle.py";

// The gate catalogue, ordered the way a session should care about them.
//
// Determinism first, and that ordering is not cosmetic: n64lle's own doctrine
// is that an oracle which is not bit-reproducible contaminates every fixture
// compared against it, so a red gate2 makes every row below it meaningless
// rather than merely unlucky.
const std::vector<N64Gate> kGates = {
    {"cosim_gate2_oracle", "Oracle determinism",
     "Ares answers identically run to run. Nothing below means anything until "
     "this is green.", true},
    {"rdp_cmd_differential_mm", "RDP command transport",
     "our DPC walk matches Ares' real RDP::render() advance across all 64 "
     "opcodes.", true},
    {"rdp_pixel_differential_fill", "RDP pixels (fill)",
     "three-way: our rasterizer / paraLLEl-RDP / a hand-computed model.", true},
    {"rdp_pixel_differential_simd", "RDP pixels (SIMD)",
     "the SIMD raster ladder against the same three-way.", true},
    {"rdp_frame_gate_mm", "Frame gate (interpreter)",
     "real game frames replayed through paraLLEl-RDP, byte-compared.", true},
    {"rdp_frame_gate_compiled_mm", "Frame gate (compiled)",
     "the same, on the compiled tier.", true},
    {"vi_scanout_differential", "VI scanout",
     "our scanout against Ares' real VI::refresh.", true},
    {"rsp_attract_invariant_mm", "RSP attract invariants",
     "per-task RSP invariants against the oracle.", true},
    {"cosim_gate1_mm", "Cosim gate 1",
     "compiled tier matches the interpreter floor at every checkpoint. Needs "
     "no oracle.", false},
    {"cosim_gate3_inject_mm", "Cosim gate 3 (fault injection)",
     "the harness can still DETECT an injected fault. A green gate 1 without "
     "this proves nothing.", false},
};

std::string trim(std::string s) {
    const auto ws = " \t\r\n";
    const auto b = s.find_first_not_of(ws);
    if (b == std::string::npos) return {};
    const auto e = s.find_last_not_of(ws);
    return s.substr(b, e - b + 1);
}

} // namespace

// ---- paths -----------------------------------------------------------------

std::string n64_tool_path(const std::string& tool) {
    const char* env = std::getenv("RETCOMM_STUDIO_TOOLKIT");
    if (!env || !*env) return {};
    std::error_code ec;
    const fs::path p = fs::path(env).parent_path() / "n64_analysis" / tool;
    return fs::is_regular_file(p, ec) ? p.string() : std::string();
}

std::string n64lle_root_for(const StudioModel& model) {
    std::error_code ec;
    auto ok = [&](const fs::path& p) {
        return !p.empty() && fs::is_regular_file(p / "n64ref" / "ORACLE-PIN.md", ec);
    };
    // The project's OWN submodule first. The oracle has to be built from the
    // same framework tree the project links, or the gates grade a different
    // engine than the one under test.
    if (!model.selected_root().empty()) {
        const fs::path sub = fs::path(model.selected_root()) / "n64lle";
        if (ok(sub)) return sub.string();
    }
    if (const char* env = std::getenv("N64LLE_ROOT"); env && *env) {
        if (ok(fs::path(env))) return fs::path(env).string();
    }
    if (!model.selected_root().empty()) {
        const std::string named = n64_local_env_value(model.selected_root(), "N64LLE_ROOT");
        if (!named.empty() && ok(fs::path(named))) return fs::path(named).string();
    }
    if (!model.selected_root().empty()) {
        const fs::path sib = fs::path(model.selected_root()).parent_path() / "n64lle";
        if (ok(sib)) return sib.string();
    }
    return {};
}

std::string n64_local_env_value(const std::string& port_root, const std::string& key) {
    if (port_root.empty() || key.empty()) return {};
    std::ifstream in(fs::path(port_root) / ".n64lle" / "local.env");
    if (!in) return {};
    const std::string prefix = key + "='";
    std::string line;
    while (std::getline(in, line)) {
        line = trim(line);
        if (line.size() < prefix.size() + 1 || line.compare(0, prefix.size(), prefix) != 0 ||
            line.back() != '\'')
            continue;
        const std::string val = line.substr(prefix.size(), line.size() - prefix.size() - 1);
        if (val.find('\'') != std::string::npos) continue;  // not ^KEY='[^']*'$
        return val;
    }
    return {};
}

std::string n64_rom_for(const std::string& root) {
    if (root.empty()) return {};
    std::error_code ec;
    const fs::path roms = fs::path(root) / "roms";
    if (!fs::is_directory(roms, ec)) return {};
    // The staged dump is a SYMLINK in every scaffolded project (the framework
    // says a link "makes it impossible to do by accident" to commit ROM bytes),
    // so follow rather than require a regular file.
    for (const auto& e : fs::directory_iterator(roms, ec)) {
        const auto ext = e.path().extension().string();
        if (ext == ".z64" || ext == ".n64" || ext == ".v64") return e.path().string();
    }
    return {};
}

// ---- parsing ---------------------------------------------------------------

N64OracleStatus parse_n64_oracle_status(const std::string& json_text) {
    N64OracleStatus st;
    st.probed = true;
    if (trim(json_text).empty()) {
        st.error = "n64_oracle.py produced no output";
        st.summary = st.error;
        return st;
    }
    try {
        const json j = json::parse(json_text);
        st.root = j.value("root", "");
        st.n64lle = j.value("n64lle", "");
        st.binary = j.value("binary", "");
        st.build_dir = j.value("build_dir", "");
        st.logfile = j.value("logfile", "");
        st.ares_pinned = j.value("ares_pinned", "");
        st.ares_present = j.value("ares_present", "");
        st.installed = j.value("installed", false);
        st.pin_ok = j.value("pin_ok", false);
        st.running_pid = j.value("running_pid", 0L);
        st.running_port = j.value("running_port", 0);
        if (j.contains("patches") && j["patches"].is_array())
            for (const auto& p : j["patches"]) st.patches.push_back(p.get<std::string>());
    } catch (const std::exception& e) {
        st.error = std::string("could not parse status: ") + e.what();
        st.summary = st.error;
        return st;
    }
    if (!st.installed)          st.summary = "not built — run Setup";
    else if (!st.pin_ok)        st.summary = "BUILT BUT OFF-PIN — do not grade with this";
    else if (st.running_pid)    st.summary = "running on port " + std::to_string(st.running_port);
    else                        st.summary = "built and on-pin; not running";
    return st;
}

N64Components parse_n64_components(const std::string& json_text) {
    N64Components c;
    c.probed = true;
    if (trim(json_text).empty()) {
        c.error = "build components produced no output";
        return c;
    }
    try {
        const json j = json::parse(json_text);
        c.package_port = j.value("package_port", false);
        auto one = [&](const char* key, N64Component& out) {
            if (!j.contains(key) || !j[key].is_object()) return;
            const json& o = j[key];
            out.label = o.value("label", "");
            out.checkout = o.value("checkout", "");
            out.dev = o.value("dev", "");
            out.dev_built = o.value("dev_built", false);
            out.def = o.value("default", "");
            out.def_from = o.value("default_from", "");
            out.def_exists = o.value("default_exists", false);
        };
        one("core", c.core);
        one("runner", c.runner);
        one("hub", c.hub);
        if (j.contains("game") && j["game"].is_object()) {
            c.package = j["game"].value("package", "");
            c.package_built = j["game"].value("built", false);
        }
    } catch (const std::exception& e) {
        c.error = std::string("could not parse components: ") + e.what();
    }
    return c;
}

const std::vector<N64Gate>& n64_gates() { return kGates; }

void parse_n64_gate_json(N64GateRun& out, const std::string& json_text) {
    out = N64GateRun{};
    out.ran = true;
    if (trim(json_text).empty()) {
        out.error = "n64_gates.py produced no output";
        out.summary = out.error;
        return;
    }
    try {
        const json j = json::parse(json_text);
        if (j.contains("error")) {
            out.error = j.value("error", "");
            out.summary = out.error;
            return;
        }
        out.passed  = j.value("passed", 0);
        out.failed  = j.value("failed", 0);
        out.skipped = j.value("skipped", 0);
        if (j.contains("results") && j["results"].is_array()) {
            for (const auto& r : j["results"]) {
                N64GateResult g;
                g.name = r.value("name", "");
                g.status = r.value("status", "");
                g.seconds = r.value("seconds", 0.0);
                if (!g.name.empty()) out.results.push_back(std::move(g));
            }
        }
    } catch (const std::exception& e) {
        out.error = std::string("could not parse gate results: ") + e.what();
        out.summary = out.error;
        return;
    }
    if (out.results.empty()) {
        out.error = "no test matched the selection";
        out.summary = out.error;
        return;
    }
    std::ostringstream sum;
    sum << out.passed << " passed";
    if (out.failed)  sum << ", " << out.failed << " FAILED";
    if (out.skipped) sum << ", " << out.skipped << " skipped";
    out.summary = sum.str();
}

} // namespace retcomm::studio
