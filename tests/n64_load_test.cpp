// n64_load_test — the N64 Diagnostics tab's parsers, headless.
//
// Every fixture below is REAL output, captured on 2026-09-09 from the tools
// themselves against a live n64lle checkout — not hand-written to match the
// parser. A parser tested only against invented input tests the invention.
//
//   ./build/n64_load_test

#include "studio/studio_n64.hpp"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>

using namespace retcomm::studio;

static int failures = 0;

static void check(bool cond, const char* what) {
    if (!cond) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

// --------------------------------------------------------------------------
// oracle status
// --------------------------------------------------------------------------

// Verbatim `n64_oracle.py status --json` against a built, on-pin oracle.
static const char* kStatusBuilt = R"({
 "root": "/home/alex/.local/share/retcomm/oracle/n64ref",
 "n64lle": "/home/alex/Documents/GitHub/GloverRecomp/n64lle",
 "installed": true,
 "binary": "/home/alex/Documents/GitHub/GloverRecomp/n64lle/build-oracle/n64ref/n64ref",
 "build_dir": "/home/alex/Documents/GitHub/GloverRecomp/n64lle/build-oracle",
 "ares_pinned": "0aafd85789215e84e1e43415c07d4c88461b7899",
 "ares_present": "0aafd85789215e84e1e43415c07d4c88461b7899",
 "pin_ok": true,
 "patches": [
  "0001-deterministic-random-seed.patch",
  "0002-sp-dma-write-capture.patch",
  "0003-rdp-cmd-capture.patch"
 ],
 "running_pid": 0,
 "running_port": 0,
 "logfile": "/home/alex/.local/share/retcomm/oracle/n64ref/oracle.log",
 "manifest": {}
})";

static void test_oracle_status() {
    const N64OracleStatus st = parse_n64_oracle_status(kStatusBuilt);
    check(st.probed, "status: probed");
    check(st.installed, "status: installed");
    check(st.pin_ok, "status: pin_ok");
    check(st.patches.size() == 3, "status: all three patches listed");
    check(st.ares_pinned == st.ares_present, "status: pinned == present");
    check(st.running_pid == 0, "status: not running");
    check(st.summary.find("on-pin") != std::string::npos,
          "status: summary says on-pin");

    // An oracle that is BUILT but off-pin is the dangerous state: it will
    // answer every query and grade every gate, with a tree nothing in the
    // evidence trail describes. The summary must not read as merely a warning.
    std::string off = kStatusBuilt;
    const auto pos = off.find("\"pin_ok\": true");
    off.replace(pos, std::string("\"pin_ok\": true").size(), "\"pin_ok\": false");
    const N64OracleStatus bad = parse_n64_oracle_status(off);
    check(bad.installed && !bad.pin_ok, "off-pin: installed but pin_ok false");
    check(bad.summary.find("OFF-PIN") != std::string::npos,
          "off-pin: summary shouts OFF-PIN");

    const N64OracleStatus empty = parse_n64_oracle_status("");
    check(!empty.installed && !empty.error.empty(), "empty status: error, not installed");
    const N64OracleStatus junk = parse_n64_oracle_status("not json at all");
    check(!junk.installed && !junk.error.empty(), "junk status: error, not installed");
}

// --------------------------------------------------------------------------
// gate results
// --------------------------------------------------------------------------

// Verbatim `n64_gates.py --json` over one real pass and one real SKIP. The
// skip is the case that matters: n64lle's gates exit 77 when an anchor ROM is
// absent, and folding that into "passed" is how a suite reads green without
// having run.
static const char* kGatesMixed = R"({
 "results": [
  {
   "name": "cosim_gate2_oracle",
   "status": "Passed",
   "seconds": 55.83
  },
  {
   "name": "rsp_attract_invariant_snap",
   "status": "Skipped",
   "seconds": 0.05
  }
 ],
 "passed": 1,
 "failed": 0,
 "skipped": 1,
 "matched": 2,
 "ctest_exit": 0
})";

static void test_gate_results() {
    N64GateRun run;
    parse_n64_gate_json(run, kGatesMixed);
    check(run.ran, "gates: ran");
    check(run.passed == 1, "gates: one pass");
    check(run.skipped == 1, "gates: one skip");
    check(run.failed == 0, "gates: no failures");
    check(run.results.size() == 2, "gates: two rows");
    check(run.results[0].name == "cosim_gate2_oracle", "gates: first row name");
    check(run.results[1].status == "Skipped", "gates: skip kept as skip");
    check(run.summary.find("skipped") != std::string::npos,
          "gates: summary names the skip");
    check(run.summary.find("FAILED") == std::string::npos,
          "gates: a skip is not reported as a failure");

    // The tool reports its own inability to run as {"error": ...}; that must
    // surface as an error rather than as a clean zero-result pass.
    N64GateRun err;
    parse_n64_gate_json(err, R"({"error": "no configured build at /nope"})");
    check(!err.error.empty(), "gates: tool error surfaces");
    check(err.passed == 0 && err.results.empty(), "gates: no phantom results");

    N64GateRun none;
    parse_n64_gate_json(none, R"({"results": [], "passed": 0, "failed": 0, "skipped": 0})");
    check(!none.error.empty(), "gates: empty selection is an error, not a pass");

    N64GateRun empty;
    parse_n64_gate_json(empty, "");
    check(!empty.error.empty(), "gates: no output is an error");
}

// --------------------------------------------------------------------------

static void test_catalogue() {
    const auto& g = n64_gates();
    check(!g.empty(), "catalogue: non-empty");
    // Determinism first is load-bearing, not cosmetic: a red gate2 makes every
    // row below it meaningless, so it must be the row a reader sees first.
    check(std::string(g.front().ctest_name) == "cosim_gate2_oracle",
          "catalogue: oracle determinism leads");
    int needs_oracle = 0, standalone = 0;
    for (const auto& row : g) (row.needs_oracle ? needs_oracle : standalone)++;
    check(needs_oracle > 0, "catalogue: has oracle-backed gates");
    check(standalone > 0,
          "catalogue: keeps gates that run without the oracle, so the pane is "
          "useful before Setup");
    for (const auto& row : g) {
        check(row.label && row.label[0], "catalogue: every row has a label");
        check(row.what && row.what[0],
              "catalogue: every row says what a pass proves");
    }
}

// --------------------------------------------------------------------------
// components (Build tab)
// --------------------------------------------------------------------------

// Verbatim `build components --json` against PokemonStadiumRecomp on
// 2026-09-28: a generated core, fetched runner and hub, one dev hub built.
static const char* kComponents = R"({
  "platform": "linux-x86_64",
  "package_port": true,
  "core": {
    "label": "n64lle core",
    "checkout": "/home/alex/Documents/GitHub/n64lle",
    "dev": "/home/alex/Documents/GitHub/n64lle/out/local/linux-x86_64/n64lle_core.so",
    "dev_built": false,
    "default": "/home/alex/Documents/GitHub/n64lle/build-n64lle/runtime/n64lle_core.so",
    "default_from": "generate",
    "default_exists": true
  },
  "runner": {
    "label": "retro-core-runner",
    "checkout": "/home/alex/Documents/GitHub/Retro-Runtime",
    "dev": "/home/alex/Documents/GitHub/Retro-Runtime/out/local/linux-x86_64/retro-core-runner",
    "dev_built": false,
    "default": "/home/alex/Documents/GitHub/PokemonStadiumRecomp/.n64lle/player/retro-core-runner",
    "default_from": "release",
    "default_exists": true
  },
  "hub": {
    "label": "retro-hub",
    "checkout": "/home/alex/Documents/GitHub/retcomm-launcher",
    "dev": "/home/alex/Documents/GitHub/retcomm-launcher/out/local/linux-x86_64/retro-hub",
    "dev_built": true,
    "default": "/home/alex/Documents/GitHub/PokemonStadiumRecomp/.n64lle/player/retro-hub",
    "default_from": "release",
    "default_exists": true
  },
  "game": {
    "package": "/home/alex/Documents/GitHub/PokemonStadiumRecomp/build-release/package/pokemonstadium_game.so",
    "built": true,
    "configured": true,
    "rom": "/home/alex/Documents/GitHub/PokemonStadiumRecomp/roms/pokemonstadium.z64"
  }
})";

static void test_components() {
    const N64Components c = parse_n64_components(kComponents);
    check(c.probed && c.error.empty(), "components: parsed");
    check(c.package_port, "components: a game-package port");
    check(c.core.def_from == "generate" && !c.core.dev_built,
          "components: core default kept, no dev core");
    check(c.runner.def_from == "release", "components: runner default is the release");
    check(c.hub.dev_built && !c.hub.dev.empty(), "components: dev hub found");
    check(c.package_built && c.package.find("_game.so") != std::string::npos,
          "components: package path");

    const N64Components junk = parse_n64_components("nope");
    check(junk.probed && !junk.error.empty(), "components: junk is an error");
    check(!junk.hub.dev_built, "components: junk offers no dev build");
    const N64Components empty = parse_n64_components("");
    check(!empty.error.empty(), "components: no output is an error");
}

// --------------------------------------------------------------------------
// a game-package port's .n64lle/local.env (Diagnostics finds n64lle through it)
// --------------------------------------------------------------------------

static void test_local_env() {
    namespace fs = std::filesystem;
    const fs::path port = fs::temp_directory_path() / "n64_load_test_port";
    fs::remove_all(port);
    fs::create_directories(port / ".n64lle");
    fs::create_directories(port / "roms");
    {
        std::ofstream f(port / ".n64lle" / "local.env");
        f << "# comment\n"
             "N64LLE_ROOT='/src/n64lle'\n"
             "N64LLE_BUILD='/src/n64lle/build-n64lle'\n"
             "RETRO_HUB='it's quoted'\n"
             "N64LLE_CORE_FROM=generate\n";
    }
    check(n64_local_env_value(port.string(), "N64LLE_ROOT") == "/src/n64lle",
          "local.env: N64LLE_ROOT read");
    check(n64_local_env_value(port.string(), "N64LLE_ROOT_X").empty(),
          "local.env: a longer key is not a prefix match");
    check(n64_local_env_value(port.string(), "RETRO_HUB").empty(),
          "local.env: a line CMake's rule rejects is rejected here too");
    check(n64_local_env_value(port.string(), "N64LLE_CORE_FROM").empty(),
          "local.env: an unquoted value is not the format");
    check(n64_local_env_value((port / "nope").string(), "N64LLE_ROOT").empty(),
          "local.env: no file, no value");

    check(n64_rom_for(port.string()).empty(), "rom: none staged");
    { std::ofstream f(port / "roms" / "game.z64"); f << "x"; }
    check(n64_rom_for(port.string()).find("game.z64") != std::string::npos,
          "rom: the port's own staged dump");
    fs::remove_all(port);
}

int main() {
    test_local_env();
    test_oracle_status();
    test_gate_results();
    test_catalogue();
    test_components();
    if (failures) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("n64_load_test: all checks passed\n");
    return 0;
}
