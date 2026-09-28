#pragma once

// studio_n64.hpp — the N64 Diagnostics tab: drive n64lle's Ares oracle, run its
// differential gates, and read the runtime's always-on rings.
//
// Why this tab exists NOW when it deliberately did not before
// -----------------------------------------------------------
// The old call — recorded in studio_model.hpp and README.md — was that N64 had
// no Diagnostics tab because "its debug server implements only
// ping/ring_stats/ring_query/help". That was true of the RUNTIME's server and
// is still true. What changed is that n64lle's Ares oracle (n64ref) now builds
// off Windows, so the whole differential toolset it gates — command, pixel,
// frame and scanout — is reachable from a Linux host for the first time
// (n64lle docs/evidence/ORACLE-LINUX-PORT-STUDY.md). The tab is about the
// ORACLE and the GATES, which is a different surface from the one that was
// missing, and the rings are a third thing that was always there.
//
// It is still NOT a Functions tab. Discovery on n64lle is execution-derived
// inside the harvest and a port carries no symbol table, so there remains
// nothing for that tab to read and it stays absent.
//
// Division of labour, same as the other two Diagnostics tabs
// ----------------------------------------------------------
// The tools own the protocol and the verdict; Studio is a launcher and a
// viewer. tools/n64_analysis/n64_oracle.py owns the oracle lifecycle and the
// pin check; n64lle's own coordinators (tools/rdp_*_differential.py,
// rdp_frame_gate.py, vi_scanout_differential.py, gate2_determinism.py) own
// every comparison. Nothing here decides whether two images match, so a
// headless gate run and this tab can never disagree about a result.
//
// Why no second debug client: n64lle's debug server and n64ref speak the SAME
// id-framed JSON-over-TCP protocol — that is n64ref's stated design, "every
// debug and co-simulation tool works against either backend by switching
// ports". DebugClient already frames exactly that way and heartbeats on
// `ping`, which n64lle implements; its PSX-only polling is gated behind
// live-trace, which N64 never turns on. So this tab reuses the existing
// clients rather than adding a third.

#include "studio/studio_model.hpp"

#include <string>
#include <vector>

struct SDL_Window;

namespace retcomm::studio {

struct Theme;

// ---- oracle ----------------------------------------------------------------

// Parsed from `n64_oracle.py status --json`. Never throws; a malformed reply
// comes back with installed=false and an error naming what happened.
N64OracleStatus parse_n64_oracle_status(const std::string& json_text);

// Where Studio's own N64 tools live (tools/n64_analysis beside the toolkit),
// or "" when the toolkit could not be located. Studio never ships a second
// copy inside a project.
std::string n64_tool_path(const std::string& tool);

// The n64lle checkout the selected project actually links: its own submodule
// first (a submodule port), then $N64LLE_ROOT, then the N64LLE_ROOT its
// .n64lle/local.env names (a game-package port — the same order its
// CMakeLists uses), then a sibling. Empty when none carries an ORACLE-PIN.md.
// The oracle MUST be built from the same framework tree the project links, or
// the gates grade a different engine than the one under test.
std::string n64lle_root_for(const StudioModel& model);

// One KEY='value' line of <port>/.n64lle/local.env, parsed with the port
// CMakeLists' own rule (^[A-Z0-9_]+='[^']*'$); "" when absent.
std::string n64_local_env_value(const std::string& port_root, const std::string& key);

// The first dump staged under <root>/roms/ (.z64 / .n64 / .v64), or "" when
// there is none. Gates SKIP (77) without one rather than failing, which is a
// normal outcome to show.
std::string n64_rom_for(const std::string& root);

// ---- gates -----------------------------------------------------------------

// One row of the Gates pane. `ctest_name` is the registered test; `needs_oracle`
// says whether it is one of the differentials that cannot run without n64ref
// built, which is what lets the pane grey a row with a reason instead of
// letting ctest fail deep inside somebody else's tool.
struct N64Gate {
    const char* ctest_name;
    const char* label;
    const char* what;          // one line: what a pass actually proves
    bool        needs_oracle;
};

// The gate catalogue, in the order a session should care about them: oracle
// determinism first (nothing downstream means anything until it is green),
// then the differentials outward from command transport to whole frames.
const std::vector<N64Gate>& n64_gates();

// Parse `n64_gates.py --json` into per-test outcomes. The TOOL owns the ctest
// parse and the pass/skip/fail split; this only lifts its answer into the model,
// so a headless gate run and this tab cannot disagree about a verdict.
void parse_n64_gate_json(N64GateRun& out, const std::string& json_text);

// ---- components (Build tab) ------------------------------------------------

// Parse `build components --json`: the core, runner and hub a game-package
// port runs on — each one's source checkout, dev build and default — plus the
// package. Never throws; a malformed reply comes back probed with an error.
N64Components parse_n64_components(const std::string& json_text);

void draw_n64(StudioModel& model, const Theme& th, SDL_Window* window);

} // namespace retcomm::studio
