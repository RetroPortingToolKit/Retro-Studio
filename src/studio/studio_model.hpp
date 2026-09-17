#pragma once

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <map>
#include <set>
#include <mutex>
#include <string>
#include <vector>

namespace retcomm::studio {

namespace fs = std::filesystem;

// ---------------------------------------------------------------------------
// Which console this window is working on.
//
// `None` is a real state, not a placeholder: it is what the platform picker
// renders on, and returning to it is how you switch consoles. Every repo list,
// framework submodule and scaffolder Studio reaches for follows from this one
// value, which is why it is chosen before anything else is shown.
// ---------------------------------------------------------------------------
// WHY THESE ARE SWITCHES AND NOT TERNARIES (2026-09-07, adding Nintendo 64).
// Every one of these used to read `p == Platform::SNES ? snes : psx`, where the
// second arm silently meant "PlayStation" AND "anything else". Adding a third
// console is what makes those two different claims: an N64 session would have
// been handed psxrecomp, a .cue filter and the PSX repo index without a single
// line changing. A switch over the enum makes the next console a compile-time
// question instead — see platforms.py, which carries the same note.
enum class Platform { None, PSX, SNES, N64 };

inline const char* platform_key(Platform p) {
    switch (p) {
    case Platform::SNES: return "snes";
    case Platform::N64: return "n64";
    default: return "psx";
    }
}

inline const char* platform_display(Platform p) {
    switch (p) {
    case Platform::SNES: return "Super Nintendo";
    case Platform::PSX: return "PlayStation";
    case Platform::N64: return "Nintendo 64";
    default: return "(none)";
    }
}

// The framework submodule a port of this platform carries.
inline const char* platform_framework(Platform p) {
    switch (p) {
    case Platform::SNES: return "snesrecomp";
    case Platform::N64: return "n64lle";
    default: return "psxrecomp";
    }
}

// UI label for the game image, and the file-dialog filter that matches it.
inline const char* platform_image_label(Platform p) {
    switch (p) {
    case Platform::SNES: return "ROM";
    case Platform::N64: return "ROM";
    default: return "Disc .cue";
    }
}

inline const char* platform_image_filter_name(Platform p) {
    switch (p) {
    case Platform::SNES: return "SNES ROM";
    case Platform::N64: return "N64 ROM";
    default: return "CUE files";
    }
}

inline const char* platform_image_filter_ext(Platform p) {
    switch (p) {
    case Platform::SNES: return "sfc;smc";
    case Platform::N64: return "z64;n64;v64";
    default: return "cue";
    }
}

// --- capabilities ----------------------------------------------------------
// The GUI's counterpart to PlatformProfile's capability fields, and they carry
// the same rule: a tab or button is ABSENT on a console whose framework cannot
// serve it, rather than present and dead. The CLI refuses the matching
// subcommands with a reason, so the two layers agree.

// A cartridge: one image, no BIOS, no Redump identity, no multi-disc.
inline bool platform_is_cartridge(Platform p) {
    return p == Platform::SNES || p == Platform::N64;
}

// psxrecomp stages a BIOS and builds separate emitters; no cartridge does.
inline bool platform_has_bios(Platform p) { return p == Platform::PSX; }

// The Functions and Diagnostics tabs. Each console that has one is backed by
// its own toolset — PSX reads psxrecomp's analysis bundle and speaks its debug
// protocol; SNES reads recomp/symbols.toml and drives tools/snes_analysis over
// the Mesen oracle; N64 drives n64lle's Ares oracle and its differential gates
// through tools/n64_analysis.
//
// FUNCTIONS is still absent on N64, and for the original reason: discovery is
// execution-derived inside the harvest and a port carries no symbol table, so
// there is nothing for that tab to read. The CLI refuses `analyze` on the same
// grounds.
//
// DIAGNOSTICS was absent for a reason that has since expired. The old note here
// said n64lle's debug server "implements only ping/ring_stats/ring_query/help",
// which is still true of the RUNTIME's server — but the tab is not about that
// server. n64lle's oracle (n64ref) speaks the full protocol, and as of
// 2026-09-09 it builds off Windows, which brought the whole differential
// toolset — command, pixel, frame and scanout — within reach of a Linux host
// for the first time (n64lle docs/evidence/ORACLE-LINUX-PORT-STUDY.md). A tab
// that opens onto nothing reads as a bug; so did the absence of one once there
// was something to open onto.
inline bool platform_has_functions(Platform p) {
    return p == Platform::PSX || p == Platform::SNES;
}
inline bool platform_has_diagnostics(Platform p) {
    return p == Platform::PSX || p == Platform::SNES || p == Platform::N64;
}

// A Windows cross-build script exists for this console.
inline bool platform_has_mingw(Platform p) {
    return p == Platform::PSX || p == Platform::SNES;
}

// The framework wires netplay (recomp-net + retcomm-rbengine). n64lle vendors
// neither, and its scaffolder has no flag for either.
inline bool platform_has_netplay(Platform p) {
    return p == Platform::PSX || p == Platform::SNES;
}

struct RepoEntry {
    std::string path;
    std::string name;
    std::string cue;
    std::string label;
    bool in_catalog = false;
    int players = 2; // from game.toml / CMakeLists MAX_PLAYERS
};

struct AuditCheck {
    std::string id;
    std::string title;
    std::string status; // pass|fail|warn|skip
    std::string detail;
    std::string fix_op;
};

struct PlanStep {
    std::string op_id;
    std::string title;
    std::string detail;
    bool selected = true;
};

// ---------------------------------------------------------------------------
// Functions tab — static function discovery
//
// These mirror psxrecomp's analysis.json / edges.json / indirect.json schemas.
// Studio is a VIEWER over those artifacts: the analysis itself always runs
// out-of-process in psxrecomp-analyze, so a headless or CI run produces exactly
// what the tab shows, and Studio never links the emulator runtime either. That
// is what keeps the tab's central claim — everything here was proven from the
// executable alone — true rather than merely intended.
// ---------------------------------------------------------------------------
struct FnRow {
    uint32_t addr = 0;
    uint32_t end = 0;
    uint32_t size = 0;
    uint32_t instructions = 0;
    std::string name;
    std::string status;
    std::string note;
    std::string confidence;
    std::string confidence_reason;
    std::string prototype;
    std::string bios_call;
    std::string saved;
    bool user_named = false;
    bool reachable = false;
    bool address_taken = false;
    bool partial = false;
    bool is_data = false;
    bool leaf = false;
    bool gte = false;
    bool mmio = false;
    bool syscall = false;
    bool returns = false;
    bool sig_confident = true;
    int  args = 0;
    int  stack_frame = 0;
    uint32_t in_degree = 0;
    uint32_t out_degree = 0;
    uint32_t unresolved_indirect = 0;
    uint32_t blocks = 0;
    int  conf_rank = 3;  // 0 verified .. 4 data; drives sorting and colour
    // Times this address was OBSERVED entering, from the live trace. Lives on
    // the row so the table can sort by it; it is not part of the analysis
    // bundle and is never written back to one. Provenance stays separate:
    // in_degree is proven, live_calls is watched.
    uint64_t live_calls = 0;
};

struct FnEdge {
    uint32_t from = 0;
    uint32_t pc = 0;
    uint32_t to = 0;
    std::string kind;
};

struct FnIndirect {
    uint32_t func = 0;
    uint32_t pc = 0;
    uint32_t reg = 0;
    uint32_t table_base = 0;
    uint32_t table_count = 0;
    std::string kind;
    std::string classification;
    std::vector<uint32_t> targets;
    std::vector<std::string> context;
};

// One [widescreen.cull] site candidate from psxrecomp-analyze --scan-widescreen.
struct WsSite {
    uint32_t pc = 0;
    uint32_t func = 0;
    uint32_t partner = 0;
    int      confidence = 0;
    std::string kind;        // the game.toml key: bias_sites, range_sites, …
    std::string instr;
    std::string evidence;
};

// ---------------------------------------------------------------------------
// Frames tab — live GP0 capture, attribution, diff, and per-function layers
//
// Mirror of the artifacts psxrecomp/tools/gpu_frame_*.py write. Studio reads
// the compact *.summary.json / diff.json / layers.json, never the raw frame
// dump: a busy frame's dump is tens of megabytes of primitives and nothing in
// this tab wants them individually.
//
// The provenance rule from the Functions tab applies here too, and matters
// more. Everything on this tab is OBSERVED from one execution of one frame. A
// function that issued primitives is proof that code ran; it says nothing
// about the call graph, and it must never be folded into a static claim.
// ---------------------------------------------------------------------------
struct FrameFuncRow {
    std::string func;              // "0x8004ABCD" as the tools emit it
    uint32_t addr = 0;
    uint32_t packets = 0;
    uint32_t drawing = 0;
    uint32_t semi = 0;
    uint32_t textured = 0;
    int      ot_min = -1;          // -1 when no primitive carried an OT rank
    int      ot_max = -1;
    std::string ops;               // "PolyG4+semi x12, Rect x3"
    std::string stp;               // "0.5B+0.5F x12"
    std::string name;              // from analysis.json, when the address is known
    std::vector<uint32_t> ras;
    int bbox[4] = {0, 0, 0, 0};
    bool has_bbox = false;
};

struct FrameSummary {
    bool        loaded = false;
    std::string path;              // the .summary.json this came from
    std::string dump;              // sibling raw dump filename
    std::string label;
    std::string error;
    uint32_t    frame = 0;
    uint32_t    packets = 0;
    uint32_t    drawing = 0;
    uint32_t    truncated = 0;
    bool        capped = false;
    int         area[4] = {0, 0, 0, 0};
    std::vector<FrameFuncRow> funcs;
    std::vector<std::pair<std::string, uint32_t>> ops;
    std::vector<std::pair<std::string, uint32_t>> modes;
};

// State of the machine-wide DuckStation oracle, from
// `duckstation_oracle.py status --json`.
//
// The oracle is deliberately NOT per-repo: it installs into the Retro data
// root and one build serves every title. Studio only ever reads this — the tool
// owns the install, so a headless setup and this row cannot disagree about what
// exists.
// Which emulator is playing oracle. They are not interchangeable and neither
// is a superset: DuckStation registers pause/step, read_vram, gpu_state and
// the breakpoint family; Beetle registers every trace family (wtrace, rtrace,
// fntrace, sio_trace, cdrom_cmd, devtrace) and none of DuckStation's. Which
// commands each one actually serves is generated into
// tools/psx_analysis/oracle_caps.json rather than written down here — see
// gen_oracle_caps.py for why a hand-kept list is wrong by the next commit.
enum class OracleKind { DuckStation, Beetle };

inline const char* oracle_key(OracleKind k) {
    return k == OracleKind::Beetle ? "beetle" : "duckstation";
}
inline const char* oracle_display(OracleKind k) {
    return k == OracleKind::Beetle ? "Beetle PSX" : "DuckStation";
}
// Each oracle has its own manager script, with its own subcommands and its own
// status shape. Studio drives both through the same buttons.
inline const char* oracle_tool(OracleKind k) {
    return k == OracleKind::Beetle ? "beetle_oracle.py" : "duckstation_oracle.py";
}

// One node from src/gen/program_manifest.json — a stretch of ROM snesrecomp's
// analyzer proved is a function, at one (m, x) flag state. `reasons` is only
// populated when it could not be proven AOT-eligible, and it is the useful
// half: it names what discovery could not settle, which is where the fix goes.
struct SnesFunc {
    std::string key;          // "008000:M1X1"
    std::string pc, end;      // 24-bit, hex
    std::string disposition;  // aot_eligible | lle_only
    std::string name;         // from recomp/symbols.toml, when it has one
    std::vector<std::string> reasons;
    int bank = 0;
    int instructions = 0;
    int demands = 0;
    int unresolved = 0;
    int emit = -1;            // -1 not in symbols.toml, 0 held, 1 promoted
};

struct SnesFunctions {
    bool present = false;     // false before the first tools/regen.sh
    bool loading = false;
    std::string path, error, root;
    std::vector<SnesFunc> funcs;
    std::vector<std::string> roots;
    std::map<std::string, int> counts;   // disposition -> n
    int promoted = 0;
};

struct OracleStatus {
    bool valid = false;
    OracleKind kind = OracleKind::DuckStation;
    std::string state;            // absent|fetched|built|installed|port-busy|answering
    std::string root, app, launcher;
    std::string upstream_base;
    std::string running_detail;
    std::string container_engine, container_reason;
    int  port = 4371;
    bool installed = false;
    bool built = false;
    bool running = false;
    bool answering = false;
    bool container_needed = false;
    // Beetle only: hooks its pinned beetle-psx tree is missing, named by
    // beetle_oracle.py rather than left to surface as a wall of C++ errors.
    // docs/beetle-linux.md records that the committed patches are incomplete;
    // this is that debt, reported instead of hidden.
    std::vector<std::string> blockers;
    std::string error;
};

// One image in a project's disc roster.
//
// A PSX set is one program on N images, and `[game] discs` is the roster in
// disc order -- index 0 is the disc that boots. `present` is kept rather than
// silently dropping a missing entry: the roster positions are what
// settings.toml's `[disc] selected` indexes, so dropping row 2 would renumber
// row 3 and point the oracle at the wrong image.
struct DiscEntry {
    std::string path;      // absolute, resolved against the repo root
    std::string label;     // the image's file stem -- what the dropdown shows
    std::string serial;    // its `[game] disc_serials` entry, or empty
    bool present = false;  // the image is actually on disk
};

// The memory card the runtime uses for a project, and where that was learned.
//
// ONE card serves a whole disc set. The runtime names it
// <memcard_dir>/card1.mcd whichever disc is mounted, and that is the point of
// a multi-disc save: the save that ends disc 1 is the save that starts disc 2.
// So this does not vary with the selected disc, and the UI says so.
struct MemcardRef {
    std::string path;
    std::string source;    // provenance, shown to the user
    bool present = false;
};

// Reply to {"cmd":"pause_state"}. `auto_resumed` means a park ended because the
// runtime stopped hearing from us — a Pause button showing "paused" has to be
// able to correct itself when that happens.
struct PauseState {
    bool valid = false;
    bool paused = false;
    bool auto_resumed = false;
    int  stepping = 0;
    uint32_t run_to = 0;
    uint64_t frame = 0;
    uint32_t timeout_ms = 0;
    bool supported = true;   // false when the runtime predates the command
    std::string error;
};

// One (opcode, blend mode) class that moved between two consecutive frames.
struct ScanChange {
    std::string key;          // "PolyG3+semi|B+F"
    int a = 0, b = 0, delta = 0;
    int cmax_a = 0, cmax_b = 0;
    std::string bbox_a, bbox_b;   // pre-rendered, they are only ever displayed
    std::string src_lo, src_hi;   // guest packet buffer — hand this to wtrace
    bool outside_draw_area = false;
    bool outside_vram = false;
    std::string overflow;         // "left 148, top 180, right 140, bottom 196 px over"
};

struct ScanTransition {
    int a = 0, b = 0;
    double score = 0.0;
    std::vector<ScanChange> changes;
};

// Result of walking the ring looking for where the picture turns. This is the
// answer to "which two frames should I diff", which a plain A/B diff assumes
// you already know — and getting it wrong is how a diff ends up reporting that
// an effect exists rather than that it broke.
struct FrameScan {
    bool loaded = false;
    std::string path, error;
    int lo = 0, hi = 0;
    std::vector<ScanTransition> transitions;
};

// One code site seen writing into a watched packet buffer. This is the answer
// function attribution cannot give on a DMA/ordering-table game: the packets
// exist at a known RAM address, and whoever writes that address builds them.
struct WtraceWriter {
    uint32_t pc = 0;
    uint32_t func = 0;
    uint32_t ra = 0;
    int      dma_ch = -1;    // >= 0 when a DMA wrote it, not the CPU
    int      count = 0;
};

// Result of comparing a range of guest RAM between the two emulators.
// The verdict is the point: it says which half of the codebase a rendering bug
// is in, and it needs no pixels — which matters while the oracle's VRAM
// readback is unreliable.
struct RamParity {
    bool loaded = false;
    bool identical = false;
    std::string addr, first_difference, error;
    int  length = 0, compared = 0;
    int  differing_words = 0, total_words = 0;
    uint64_t native_frame = 0, oracle_frame = 0;
    bool frames_aligned = true;
};

// One primitive decoded out of an ordering table read from guest RAM.
// This is a different act from watching the GP0 ring go by: the ring says what
// was drawn, this says what the game BUILT. The oracle has no ring, so walking
// RAM is the only way to see what DuckStation was about to draw -- and for
// decompilation, the buffer a routine fills is what tells you what it is.
struct DisplayPrim {
    std::string op;          // "PolyG4+semi"
    std::string blend;       // "B+F", "opaque", ...
    std::string src;         // guest address of the packet -- feeds wtrace
    std::string verts;       // pre-rendered, only ever displayed
    std::string colors;
    int cmax = 0;            // brightest channel over this primitive's vertices
};

struct DisplayList {
    bool loaded = false;
    std::string path, error, root;
    int nodes = 0, drawing = 0;
    std::vector<std::pair<std::string, int>> classes;   // class -> count
    std::vector<DisplayPrim> prims;
};

// Vertex-colour distribution for one emulator, and the verdict comparing two.
//
// Deliberately NOT a byte diff: an animating effect sampled at two different
// phases differs everywhere, which says nothing, and reading such a diff as
// meaningful is how an earlier attempt at this went wrong. A distribution does
// not care about phase -- if one side peaks at 199 and the other at 40, that is
// real regardless of which frame each was on.
struct ColourStats {
    bool loaded = false;
    int vertices = 0, peak = 0, p50 = 0, p90 = 0;
    double mean = 0.0;
    std::vector<double> hist;      // normalised, 16 buckets
};

struct ColourParity {
    bool loaded = false;
    std::string path, error, klass, verdict;
    ColourStats native, oracle;
    // Histogram intersection, 0..1. The verdict rests on this rather than on
    // peak: a peak is a maximum, so it grows with sample count, and deciding on
    // it once reported "differ" for two distributions whose medians agreed.
    double overlap = 0.0;
    double sample_ratio = 1.0;
};

// One instruction seen storing into a primitive's packet, split by which
// FIELD it wrote. Function attribution cannot answer this on a game that builds
// an ordering table and DMAs it -- every packet reports the same submit PC --
// so the handle is the address written, and the useful split is colour vs
// vertex. An instruction that writes only colour words is the one to read when
// colours are wrong and geometry is right.
struct DisasmLine {
    std::string pc, word, text;
    bool is_target = false;
};

struct PacketWriter {
    std::string pc, func;
    int colour = 0, vertex = 0, uv = 0;
    bool colour_only = false;
    // Captured while the emulator was parked for the trace. These PCs live
    // above the main EXE, so the code at a given address depends on which
    // overlay is resident — disassembling later, after the scene moved on,
    // would decode whatever replaced it and look perfectly plausible.
    std::vector<DisasmLine> listing;
};

struct PacketWriters {
    bool loaded = false;
    bool absent = false;          // the class was not on screen
    std::string path, error, klass, note, lo, hi;
    int packets = 0, colour_words = 0, vertex_words = 0;
    int writes = 0, unmapped = 0;
    // True when the buffer moved during the trace. Every row is then
    // an artefact, and a 50/50 colour:vertex split reads deceptively
    // like "this instruction writes both".
    bool aliased = false;
    int mixed_writers = 0;
    // The trace ran but recorded nothing. Distinct from `absent`:
    // the packets ARE on screen, so this means the trace window was
    // wrong, not that the search finished empty-handed.
    bool no_writes = false;
    std::vector<PacketWriter> writers;
    std::vector<std::pair<std::string,int>> present;   // what WAS on screen
};

// GTE self-check. Re-derives the depth-cue interpolation from its own recorded
// inputs against the hardware description, so it needs no oracle: a mismatch is
// a bug in our GTE, a match means the inputs were already wrong when they
// arrived and the fault is upstream of it.
struct GteFrameStat {
    uint32_t frame = 0, nproj = 0, nsat = 0, nflat = 0;
    uint32_t nintpl = 0, nintpl_tiny = 0;
};

struct GteCheck {
    bool loaded = false;
    std::string path, error, verdict, note;
    int checked = 0, bad = 0;
    uint64_t nintpl_total = 0, nsat_total = 0, nproj_last = 0;
    std::vector<GteFrameStat> frames;
};

// Result of comparing the two INPUTS to a packet's colour computation.
// The code scales a source RGB by a factor, so a wrong colour is either a wrong
// source or a wrong scale — and this says which, which is the difference
// between looking upstream and looking at the routine itself.
struct ColourInputs {
    bool loaded = false;
    std::string path, error, verdict, pc, source_addr, first_difference;
    std::string native_bytes, oracle_bytes;
    int scale = 0, differing = 0;
    bool source_identical = false;
    // Where the SAME table was found on psx-runtime. Addresses do not
    // carry between the two emulators — their allocators place the same
    // structures elsewhere — so this is located by content.
    std::string native_source_addr;
    int address_delta = 0;
    // How much of the table WAS found when it is not there whole. An
    // exact miss cannot tell absence from a different arrangement, and
    // those point at completely different places.
    int partial_len = 0, partial_stride = 0;
    std::vector<std::string> partial_hits;
    // How the two sides were actually compared. "native-own-pointer" means
    // psx-runtime's own $s4 was read and its OWN table examined — the direct
    // answer. Anything else is the content-search fallback, which is weaker
    // because it cannot know which table psx-runtime's code actually reads.
    std::string compared_by;
    // $s4 tracks the animation, so the two sides are rarely at the same
    // moment. When the scales differ the comparison falls back to the
    // whole enclosing region, which does not depend on phase.
    int native_scale = 0, region_differing = 0, region_bytes = 0;
    bool phase_aligned = false;
    std::string region_lo, region_hi, region_first_difference;
    // What KIND of difference: one side unwritten, one writing where the other
    // does not, or both holding values that disagree. Each points elsewhere.
    std::string region_shape;
    struct Cluster { std::string addr, native, oracle; int length = 0; };
    std::vector<Cluster> region_clusters;
    std::string native_s4, native_s6, native_block;
    std::string probe_error;
};

// Compiled-vs-interpreter comparison, run inside psx-runtime itself.
//
// The instrument that needs none of what every cross-emulator comparison here
// has had to fight: no frame alignment, no address correspondence between two
// allocators, no getting both emulators to the same point in the game. Each of
// those produced a confident wrong answer at least once.
struct Lockstep {
    bool loaded = false;
    bool found = false;
    std::string path, error, verdict, mode, kind, meaning;
    std::string frame, block, pc, addr, expected, actual;
    int reg = -1;
    uint64_t checked = 0, skipped = 0;
    // Which skip reason dominates. A bare count says how much was
    // missed but not whether the mode simply cannot cover this game.
    std::string dominant_skip;
    int window_lo = 0, window_hi = 0;
    std::vector<std::string> trace;
};

// Instructions writing an arbitrary span of RAM. packet_writers without the
// packet-layout assumption, because the colour investigation reached a REGION
// rather than a structure.
struct RangeWriter {
    std::string pc, lo, hi, common;
    int writes = 0;
};

struct RangeWriters {
    bool loaded = false;
    std::string path, error, lo, hi;
    int writes = 0, frames_advanced = 0;
    std::vector<RangeWriter> writers;
    std::vector<DisasmLine> listing;   // for the busiest writer
    std::string listing_pc;
};

// Does the colour scale animate on each side?
//
// Every vertex colour here is source_rgb * $s6 >> 7, so $s6 IS the fade: 128
// leaves the colour unchanged. This compares VARIATION rather than values,
// which is why it needs no frame alignment, no matching buffer half and no
// shared animation phase — the three things that have each produced a
// confident wrong answer in this investigation.
struct ScaleSide {
    bool loaded = false;
    int samples = 0, distinct = 0, min = 0, max = 0;
    bool constant = false, neutral_only = false;
    int median_step = 0, max_step = 0;
    std::string values;
};

struct ScaleTrace {
    bool loaded = false;
    std::string path, error, verdict, note, pc, reg;
    // Why a side produced nothing. A bare 'no samples' does not say
    // whether the emulator was unreachable, never reached the PC, or
    // refused the breakpoint — and those need different responses.
    std::string native_error, oracle_error;
    ScaleSide native, oracle;
    std::string granularity;
};

// What each emulator EVER draws, over many captures.
//
// A single capture is confounded by phase; maxima are not. Phase can hide a
// primitive in one frame, not in forty — so a class the runtime never once
// reaches is genuinely not being submitted.
struct CensusRow {
    std::string key;
    int native_max = 0, oracle_max = 0, native_med = 0, oracle_med = 0;
};

struct ClassCensus {
    bool loaded = false;
    std::string path, error, verdict;
    int samples = 0;
    std::vector<CensusRow> rows;
    std::vector<std::string> absent;
};

struct FrameDiffRow {
    std::string func;
    std::string verdict;
    uint32_t a_prims = 0, a_semi = 0, b_prims = 0, b_semi = 0;
};

struct FrameDiff {
    bool        loaded = false;
    std::string path;
    std::string error;
    std::vector<std::string> headlines;
    std::vector<FrameDiffRow> rows;
    std::vector<std::pair<std::string, uint32_t>> modes_a, modes_b;
    // op name, A count, B count
    std::vector<std::tuple<std::string, uint32_t, uint32_t>> ops;
};

struct FrameLayer {
    std::string func;
    std::string file;
    uint32_t prims = 0, semi = 0, textured = 0, pixels = 0;
    std::string stp;
    unsigned tex = 0;              // GL texture, 0 until uploaded
    int tw = 0, th = 0;
};

struct FrameLayers {
    bool        loaded = false;
    std::string dir;
    std::string error;
    std::string label;
    uint32_t    frame = 0;
    std::vector<FrameLayer> layers;
    FrameLayer  composite;         // file/tex/tw/th only
    FrameLayer  sheet;
};

struct FnStats {
    uint32_t total = 0, instructions = 0, bytes_covered = 0, bytes_image = 0;
    uint32_t verified = 0, high = 0, medium = 0, low = 0, data = 0;
    uint32_t reachable = 0, orphans = 0, named = 0;
    uint32_t direct_edges = 0, tables = 0, table_targets = 0, unresolved = 0;
    uint32_t partial_functions = 0, undecoded_words = 0;
};

// ---- SNES Diagnostics tab --------------------------------------------------
// Data shapes live here, beside FrameSummary and friends, because StudioModel
// holds them by value and studio_snes.hpp includes this header.

// Mesen's install state, from `mesen_oracle.py status --json`.
struct MesenStatus {
    bool        probed = false;
    bool        installed = false;
    std::string binary;
    std::string root;
    std::string provider;      // "release" | "system"
    std::string error;
    std::string summary;       // one line, ready to show
};

// ---- N64 Diagnostics tab ----------------------------------------------------
// Parsed from tools/n64_analysis/n64_oracle.py `status --json`. The N64 oracle
// is a THIRD kind from the other two: first-party (n64lle's own n64ref), BUILT
// from a submodule pinned by n64ref/ORACLE-PIN.md rather than downloaded, and
// it speaks the runtime's own protocol rather than answering through artifacts.
struct N64OracleStatus {
    bool        probed = false;
    bool        installed = false;
    // The one field a caller should gate "safe to grade with" on: the Ares tree
    // present is the one ORACLE-PIN.md records. A pin change is an owner-signed
    // event (n64lle docs/ORACLE-UPGRADE.md), never a build side effect, so a
    // mismatch is a refusal rather than a warning.
    bool        pin_ok = false;
    std::string binary;
    std::string build_dir;
    std::string n64lle;
    std::string root;
    std::string logfile;
    std::string ares_pinned;
    std::string ares_present;
    std::vector<std::string> patches;
    long        running_pid = 0;
    int         running_port = 0;
    std::string error;
    std::string summary;       // one line, ready to show
};

struct N64GateResult {
    std::string name;
    std::string status;        // "Passed" | "Failed" | "Skipped"
    double      seconds = 0.0;
};

// Outcome of one ctest run over the gate catalogue. A SKIP is a first-class
// outcome here and is never folded into either pass or fail: n64lle's gates
// skip (77) when an anchor ROM is absent, and "green because it did not run"
// is the failure mode this pane exists to make impossible to misread.
struct N64GateRun {
    bool        ran = false;
    int         passed = 0;
    int         failed = 0;
    int         skipped = 0;
    std::string summary;
    std::string error;
    std::vector<N64GateResult> results;
};

// 256 BGR555 entries decoded from a dump_cgram reply or a Mesen *_cgram.bin.
//
// The census is the point, not the swatches. A real SNES palette repeats a
// colour a handful of times; a *fill* repeats one across unrelated palettes,
// which is what a stale or never-uploaded palette block looks like.
struct CgramView {
    bool        loaded = false;
    std::string error;
    uint16_t    entry[256] = {};
    int         distinct = 0;
    uint16_t    top_value = 0;      // most repeated entry
    int         top_count = 0;
    int         zero_count = 0;
};

// A capture written by snes_frame_capture.py, plus whatever the decode and
// attribute steps have since produced beside it.
struct SnesBundle {
    std::string tag;
    std::string path;          // the <tag>.json manifest
    uint64_t    frame = 0;
    bool        decoded = false;
    bool        attributed = false;
};

// One visit to a scene, from snes_loop_compare.py. The tool decides what to
// capture and what counts as a difference; Studio only renders its verdict.
struct LoopVisit {
    int         visit = 0;
    uint64_t    entered_frame = 0;
    int         frame_lo = 0, frame_hi = 0;
    std::vector<std::pair<std::string,int>> irq_chain;   // label -> count
    int         cgram_distinct = 0, cgram_top_count = 0;
    std::string cgram_top_value;
    bool        cgram_fill = false;
    int         shadow_distinct = 0, shadow_top_count = 0;
    std::string shadow_top_value;
    bool        shadow_fill = false;
    bool        top_present_in_shadow = false;
    int         upload_frames = 0;
    std::vector<std::string> reg_writers;   // "reg  func  xN"
    int         block_ring_entries = 0;
    bool        chain_trustworthy = true;   // false => the zeros mean nothing
};

struct LoopDiffRow {
    std::string field, visit1, visit2;
};

struct LoopCompare {
    bool        loaded = false;
    std::string error;
    std::string path;
    std::string selector;
    std::vector<LoopVisit>   visits;
    std::vector<LoopDiffRow> diffs;
};

// One row of mesen_raster_trace.lua's CSV: a register write stamped with the
// beam position. This is the measurement our own runtime cannot make — its
// trace records (frame, addr, value) with no scanline, and for a title that
// raster-splits the screen the scanline IS the question.
struct RasterRow {
    int         frame = 0;
    int         scanline = 0;
    int         hclock = 0;
    std::string reg;
    std::string addr;
    std::string value;
};

struct StudioModel {
    std::mutex mu;

    // Platform::None until the picker is answered; `platform_pending` is set
    // when the user asks to change consoles mid-session, so the switch happens
    // between frames rather than while a tab is mid-draw.
    Platform platform = Platform::None;
    bool platform_pending = false;

    fs::path exe_dir;
    fs::path toolkit_dir;
    fs::path toolchain_root;
    std::string python_exe;
    bool toolchain_ready = false;
    bool toolchain_gate_open = false;
    bool update_prompt_open = false;
    bool startup_update_started = false;
    std::string update_prompt_msg;
    bool update_studio_avail = false;
    bool update_toolchain_avail = false;
    std::string status = "Ready";
    std::string version;

    // Header / index
    std::vector<RepoEntry> repos;
    int selected_repo = -1;
    bool catalog_only = false;
    int bulk_jobs = 2;
    int log_height = 160;
    char disc_cue[1024] = {};
    char zip_prefix[128] = {};
    // Override for the id a mod package's [[target]] matches. Normally blank:
    // Studio reads it from rom_identity.txt, or from the ids this port's own
    // mod manifests already name. It is here for the port where nothing
    // records one yet, because a derived id that matches no manifest ships a
    // build whose own mods silently refuse to apply.
    char game_id[128] = {};
    char github_owner[128] = {};
    char github_repo[128] = {};
    int players = 2;
    bool migrate_netplay = false;
    bool migrate_ci = true;
    bool migrate_probe = false;
    // README badges/boxart/launcher/RAID plus the GitHub About blurb. On by
    // default because that is the house style, but a port whose README is
    // hand-written wants it off — and off means the audit stops reporting it
    // too, not just the apply.
    bool migrate_readme = true;
    bool migrate_dry_run = true;
    bool migrate_force = false;

    std::vector<AuditCheck> audit_checks;
    std::string audit_layout;
    std::string audit_boot;
    std::vector<PlanStep> plan_steps;

    // New project
    char np_name[256] = {};
    char np_parent[1024] = {};
    char np_disc[1024] = {};
    // PSX multi-disc. np_disc stays disc 1 (and the ROM path on SNES); these
    // are discs 2..kMaxDiscs, shown only when np_disc_count says so. A SNES
    // cartridge is one image, so the whole group is hidden there.
    static constexpr int kMaxDiscs = 4;
    char np_disc_extra[kMaxDiscs - 1][1024] = {};
    int np_disc_count = 1;
    char np_bios[1024] = {};
    char np_zip[128] = {};
    char np_desc[512] = {};
    char np_publisher[256] = {};
    char np_year[32] = {};
    char np_region[64] = "USA";
    char np_lobby[256] = "netplay.retcomm.net";
    char np_psx_ref[128] = "master";
    char np_ui_ref[128] = "master";
    char np_net_ref[128] = "(default)";
    char np_rb_ref[128] = "(default)";
    int np_players = 2;
    bool np_ui = true;
    bool np_wizard = true;
    bool np_netplay = true;
    bool np_ci = true;
    bool np_boxart = true;
    bool np_stage = true;
    bool np_generate = true;
    bool np_build = true;
    bool np_github = false;
    char np_gh_vis[32] = "private";
    char np_gh_owner[128] = "RetroPortingToolKit";
    char np_gh_repo[256] = {};
    // SNES-only scaffolder inputs. `np_disc` carries the ROM path on SNES —
    // one field for one game image, so the two can never disagree.
    char np_snes_ref[128] = "main";
    // --- N64 ---------------------------------------------------------------
    // Three names where the other consoles need one. n64lle's scaffolder asks
    // for all three because a port has no single string that serves: the CMake
    // project is GloverRecomp, every target is built from the prefix glover-,
    // and the executable is glover. Blank means "let the scaffolder derive
    // it", which is the default a terminal run would have offered.
    char np_n64_slug[128] = {};
    char np_n64_exe[128] = {};
    // The n64lle revision to cut the port against. Blank is NOT "main": it is
    // "let the scaffolder decide", which means branch main pinned at the HEAD
    // of the n64lle checkout the wizard was run from. Naming one here
    // overrides that pin, and an older wizard without the flag says so in the
    // log rather than silently ignoring it.
    char np_n64_ref[128] = {};
    // The execution-derived harvest window. n64lle records what actually ran
    // rather than following seeds, so these two ARE the coverage decision for
    // a new port; 0 takes the scaffolder's own defaults (900 / 3000M) rather
    // than carrying a second copy of them here to drift.
    int np_n64_frames = 0;
    int np_n64_step_cap = 0;
    int  np_multitap = 0;   // 0 auto (from players), 1 port1, 2 port2, 3 both, 4 off
    // Separate from np_region, which carries the PSX default "USA". Blank here
    // means "whatever the cartridge header says" — a habitual USA would
    // relabel a Japanese cartridge, and the header already knows.
    char np_snes_region[64] = {};
    // Set once a probe has run, so the page can say where the defaults on
    // screen came from rather than presenting guesses as facts.
    std::string np_probe_note;

    // Git
    std::string git_summary;
    char git_branch[256] = {};
    char git_psx_branch[128] = "master";
    char git_ui_branch[128] = "master";
    char git_net_branch[128] = "main";
    char git_rb_branch[128] = "main";
    char git_msg[512] = {};
    char git_sub_msg[256] = "chore: update submodule";
    bool git_remote_update = false;
    bool git_create_branch = false;
    // Scope filters for Switch / Pull / Commit / Push (like Bulk targets).
    bool git_tgt_game = true;
    bool git_tgt_modules = false;
    bool git_tgt_nested = false;
    // Populated by `git branches --json` (game + modules / defaults).
    std::vector<std::string> branches_game;
    std::vector<std::string> branches_psx;
    std::vector<std::string> branches_ui;
    std::vector<std::string> branches_net;
    std::vector<std::string> branches_rb;
    std::string branches_root; // root these lists were fetched for
    bool branches_loading = false;
    int git_pull_mode = 0; // 0=ff-only 1=rebase 2=merge 3=reset
    char release_version[64] = {};
    int release_bump = 0; // 0=patch 1=minor 2=major
    bool release_publish = true;

    // Bulk
    std::map<std::string, bool> bulk_selected;
    char bulk_msg[256] = "chore: sync";
    bool bulk_tgt_game = true;
    bool bulk_tgt_modules = false;
    bool bulk_tgt_psx = false;
    bool bulk_tgt_nested = false;
    char bulk_game_branch[128] = "(default)";
    char bulk_psx_branch[128] = "(default)";
    char bulk_ui_branch[128] = "(default)";
    char bulk_net_branch[128] = "(default)";
    char bulk_rb_branch[128] = "(default)";
    bool bulk_create_branch = false;
    bool bulk_set_tracking = true;
    bool bulk_reuse_emitters = true;
    int bulk_pull_mode = 0; // 0=ff-only 1=rebase 2=merge 3=reset

    // Build
    char build_dir[256] = "build-release";
    char build_type[64] = "Release";
    char build_target[128] = "psx-runtime";
    // Empty = Auto: reuse CMAKE_GENERATOR from the build dir, else Ninja / Unix Makefiles.
    char build_generator[128] = {};
    char build_jobs[32] = {};
    char build_extra[512] = {};
    char build_exe[1024] = {};
    char build_launch_args[512] = {};
    // Debug tools: 0 = leave to the build type, 1 = force ON, 2 = force OFF.
    // Injected as -DPSX_DEBUG_TOOLS=... on Configure. "Leave to the build type"
    // is not the same as OFF: CMake's option() default depends on
    // CMAKE_BUILD_TYPE, and an existing cache keeps whatever it already has.
    int  build_debug_tools = 0;
    // SNES execution policy DEFAULT: 0 = leave to the framework default, then
    // 1..5 = off / on / force / verify / auto. Injected as
    // -DSNESRECOMP_EXECUTION_DEFAULT=... on Configure.
    //
    // This is a BUILD default, not the policy a run uses: snesrecomp resolves
    // the policy at runtime, where SNESRECOMP_EXECUTION_MODE and
    // SNESRECOMP_FORCE_FLOOR override whatever the build was configured with.
    // Deliberately not a compile-time split — PRINCIPLES.md requires the
    // faithful floor to stay forceable in the shipped binary.
    int  build_exec_mode = 0;
    char build_env[4096] =
        "# KEY=VALUE pairs (space or newline separated)\n"
        "# Example:\n"
        "# RBE_CROSS_OS_PACING_DIAG=1 PSX_RB_ZERO_DELAY=0\n";

    // Windows MinGW cross-build (Build tab — Linux host only)
    char mingw_build_dir[256] = "build-mingw";
    bool mingw_setup_host = false;
    bool mingw_package = false;
    bool mingw_ensure = false;
    bool mingw_dynamic = false;
    // Source zip path for Bundle+Export save dialog (local package or MinGW
    // package-only). Shared by both Build-tab export buttons.
    std::string export_zip_src;

    // Generate ROM + BIOS C dialog (Build tab)
    bool gen_popup_open = false;
    int gen_bios_mode = 0; // 0=OpenBIOS 1=SCPH1001 dump
    char gen_scph_path[1024] = {};

    // SNES regen (Build tab). Verification defaults ON: regen.sh checks the
    // ROM against the digests this port was pinned to, and skipping that is
    // how a mismatched dump becomes hours of chasing a divergence that was
    // never in the recompiler.
    bool snes_regen_verify = true;
    bool snes_regen_cfg_roots = false;

    // Functions (static discovery)
    std::vector<FnRow>      fn_rows;
    std::vector<FnEdge>     fn_edges;
    std::vector<FnIndirect> fn_indirect;
    FnStats                 fn_stats;
    std::string             fn_image;         // image name from analysis.json
    std::string             fn_loaded_root;   // repo the loaded data belongs to
    std::string             fn_error;
    std::vector<int>        fn_view;          // filtered+sorted indices into fn_rows
    bool     fn_view_dirty = true;
    int      fn_selected = -1;                // index into fn_rows
    char     fn_filter[128] = {};
    char     fn_exe_override[512] = {};
    char     fn_rename[128] = {};
    char     fn_rename_note[256] = {};
    int      fn_rename_status = 0;            // 0 guessed 1 confirmed 2 hot
    int      fn_conf_filter = 0;              // 0 all, else conf_rank+1
    int      fn_sort_col = 0;   // 0 addr 1 name 2 size 3 conf 4 callers 5 live
    // ds.consumed value already folded into fn_rows[].live_calls, so the sync
    // is O(rows) per new batch rather than per frame.
    uint64_t fn_live_applied = UINT64_MAX;
    bool     fn_sort_desc = false;
    bool     fn_only_named = false;
    bool     fn_only_orphans = false;
    bool     fn_only_unresolved = false;
    bool     fn_hide_data = true;
    bool     fn_exact = false;
    bool     fn_with_refs = false;
    bool     fn_emit_ghidra = false;
    bool     fn_emit_symbol_addrs = false;
    bool     fn_emit_symbols = false;
    int      fn_min_conf = 1;                 // index into verified|high|medium|low
    int      fn_pane = 0;                     // 0 detail, 1 indirect, 2 widescreen
    bool     fn_widescreen = false;           // pass --widescreen to the next run

    // Live runtime connection (Functions tab). The client owns its own thread
    // and mutex; the UI reads a snapshot each frame.
    char     dbg_host[64] = "127.0.0.1";
    int      dbg_port = 4370;
    bool     dbg_live_trace = false;

    // ---- Frames (live GP0 capture / attribution / layers) ------------------
    char     frm_host[64] = "127.0.0.1";
    int      frm_port = 4370;
    bool     frm_connected = false;
    char     frm_tag[64] = "bad";
    char     frm_dir[512] = {};        // resolved to <root>/analysis/frames
    int      frm_count = 65536;        // gpu_frame_dump packet cap
    // Which frame to capture: <= 0 means "the newest the ring holds".
    // There is no run-to-frame: psx-runtime removed pause / step /
    // run_to_frame, so the ring is reached backwards, not steered forwards.
    int      frm_at_frame = 0;
    // GP0 ring span from gpu_ring_stats -- the frames still capturable.
    bool     frm_ring_valid = false;
    uint32_t frm_ring_oldest = 0;
    uint32_t frm_ring_newest = 0;
    uint64_t frm_ring_total = 0;
    uint32_t frm_ring_capacity = 0;
    double   frm_ring_next_poll = 0.0;
    std::string frm_ring_error;
    bool     frm_shot = true;
    bool     frm_tex_tint = false;
    int      frm_layer_order = 0;      // 0 issue, 1 OT rank
    int      frm_max_layers = 48;
    std::vector<std::string> frm_tags; // *.summary.json basenames in frm_dir
    int      frm_sel_a = -1;           // index into frm_tags (reference / good)
    int      frm_sel_b = -1;           // index into frm_tags (suspect / bad)
    FrameSummary frm_a, frm_b;
    FrameDiff    frm_diff;
    FrameScan    frm_scan;
    int          frm_scan_last = 400;   // how many recent frames to walk
    // Two slots, not one. Walking a list is only half the point; the other
    // half is "does the oracle build the same one", and a single slot
    // overwritten by whichever side ran last cannot answer that.
    DisplayList  frm_dlist;         // psx-runtime
    DisplayList  frm_dlist_oracle_r; // the oracle
    char         frm_dlist_addr[32] = {};   // blank means scan for it
    bool         frm_dlist_oracle = false;  // which side the button reads
    PacketWriters frm_writers_pkt;
    ColourInputs  frm_cinputs;
    Lockstep      frm_lockstep;
    RangeWriters  frm_range;
    ScaleTrace    frm_scale;
    ClassCensus   frm_census;
    int           frm_census_samples = 40;
    int           frm_scale_samples = 12;
    // Step one frame between reads. Free-running samples land ~90
    // frames apart, so their differences are aliasing rather than the
    // animation increment.
    bool          frm_scale_per_frame = true;
    char          frm_range_lo[24] = {};
    char          frm_range_hi[24] = {};
    int           frm_range_frames = 1;
    int           frm_ls_frames = 120;
    bool          frm_ls_func = false;
    GteCheck      frm_gte;
    char          frm_writer_class[64] = "PolyG4+semi|B+F";
    int           frm_writer_watch = 5;      // seconds of tracing
    int           frm_writer_sel = -1;
    std::string   frm_writer_disasm;
    ColourParity frm_cparity;
    char         frm_cparity_class[64] = "PolyG4+semi|B+F";
    int          frm_cparity_samples = 20;
    std::vector<WtraceWriter> frm_writers;
    RamParity   frm_ram;
    std::string frm_disasm;        // output of disasm_ram.py, shown verbatim
    uint32_t    frm_disasm_pc = 0;
    std::string  frm_watch_lo, frm_watch_hi;   // the range currently watched
    std::string  frm_scan_note;
    FrameLayers  frm_layers;
    // One-shot pane request from a finished job: -1 none, 0 attribution,
    // 1 diff, 2 layers, 3 opcodes. Consumed and cleared once per draw.
    int      frm_pane = -1;
    int      frm_func_sel = -1;        // index into the shown summary's funcs
    int      frm_layer_sel = -1;
    char     frm_rename[128] = {};
    char     frm_rename_note[256] = {};
    bool     frm_capture_pending = false;
    std::string frm_capture_label;     // debug-client request label in flight
    std::string frm_status;

    // Game launched from this tab. Deliberately NOT a job slot: the process
    // lives as long as you play, and holding a lock for that whole time is what
    // disabled Capture at exactly the moment it would have worked.
    long        frm_launch_pid = 0;
    std::string frm_launch_log;      // <root>/frames.log
    std::string frm_launch_error;
    int         frm_slot = 1;        // savestate slot
    PauseState  frm_pause;           // from {"cmd":"pause_state"}
    int         frm_step_n = 1;
    // ---- SNES Diagnostics tab ------------------------------------------
    // Two emulators, never one: the runtime under test and the Mesen oracle
    // run as separate processes on the SAME rom.cfg ROM. Neither is ever
    // paused to line them up — they are compared by scene, from always-on
    // history, which is the standing ruling in the workspace rules.
    char        snes_host[64] = "127.0.0.1";
    int         snes_port = 4370;
    long        snes_launch_pid = 0;
    std::string snes_launch_log;
    std::string snes_launch_error;
    std::string snes_rom;            // resolved from <build>/rom.cfg
    char        snes_rom_override[512] = {};
    std::string snes_status;

    // ---- N64 Diagnostics tab -------------------------------------------
    // Two processes, never one, same as SNES: the runtime under test and the
    // n64ref oracle are separate processes and neither is ever paused to line
    // them up. n64lle's own coordinators do every comparison; this tab starts
    // them and reads what they wrote.
    char        n64_host[64] = "127.0.0.1";
    int         n64_port = 4370;
    std::string n64_status;

    N64OracleStatus n64_oracle;
    bool        n64_oracle_probing = false;
    bool        n64_oracle_busy = false;
    std::string n64_oracle_error;
    char        n64_rom_override[512] = {};

    N64GateRun        n64_gate_run;
    bool              n64_gates_running = false;
    std::vector<char> n64_gate_sel;      // per-catalogue-row selection
    int               n64_pane = 0;
    std::string       n64_ring_reply;    // last ring_stats / ring_query payload

    MesenStatus snes_mesen;
    bool        snes_mesen_probing = false;
    long        snes_mesen_pid = 0;
    std::string snes_mesen_log;
    std::string snes_mesen_error;
    int         snes_mesen_script = 0;   // index into the Lua script table
    int         snes_mesen_provider = 1; // 0 = release, 1 = system
    bool        snes_mesen_was_running = false;
    int         snes_lua_frame = 2500;   // GW_FRAME
    int         snes_lua_span = 1;       // GW_SPAN
    char        snes_lua_out[512] = {};  // GW_DIR / GW_OUT

    std::string snes_frames_dir;         // <root>/analysis/frames
    std::vector<SnesBundle> snes_bundles;
    int         snes_bundle_sel = -1;
    char        snes_capture_tag[64] = "bad";
    int         snes_capture_frame = 0;  // 0 = newest in the ring
    bool        snes_capture_wram = false;

    CgramView   snes_cgram;              // ours, live
    CgramView   snes_cgram_ref;          // Mesen *_cgram.bin
    std::string snes_cgram_ref_path;
    bool        snes_cgram_pending = false;

    std::vector<RasterRow> snes_raster;
    std::string snes_raster_error;
    std::string snes_raster_path;

    FrameLayer  snes_shot;               // our screenshot
    FrameLayer  snes_shot_ref;           // a Mesen screenshot
    std::string snes_shot_info;
    std::string snes_shot_ref_info;
    bool        snes_shot_pending = false;

    std::string snes_apu;                // last get_apu_state reply
    bool        snes_apu_pending = false;
    LoopCompare snes_loops;
    int         snes_loop_visits = 2;
    char        snes_loop_sel[16] = "0010";
    int         snes_loop_selval = 2;
    int         snes_pane = -1;          // a finished job asking for its pane

    int         frm_run_to = 0;
    double      frm_pause_next_poll = 0.0;
    bool        frm_turbo = false;

    // DuckStation oracle (machine-wide, not per-repo). Refreshed on demand
    // rather than polled: asking costs a python subprocess, and the answer only
    // changes when you press one of these buttons.
    OracleStatus frm_oracle;
    // Screenshots of both emulators. The oracle runs offscreen, so a captured
    // frame is the only way to see what it is doing at all.
    FrameLayer  frm_shot_native;
    FrameLayer  frm_shot_oracle;
    bool        frm_shot_auto = false;
    double      frm_shot_next = 0.0;
    std::string frm_shot_error;
    uint32_t    frm_pad_mask = 0xFFFFu;   // PS1 pad, active-low
    int         frm_oracle_run_to = 0;
    // SNES Functions tab.
    SnesFunctions snf;
    char snf_filter[128] = {};
    int  snf_show = 0;        // 0 all, 1 lle_only, 2 aot_eligible, 3 named
    int  snf_sel = -1;

    bool     frm_oracle_queried = false;
    // Which oracle the Frames tab is pointed at. Per-session, not per-project:
    // it is a statement about what you are investigating right now (traces vs
    // frames and memory), not a property of the port.
    OracleKind frm_oracle_kind = OracleKind::DuckStation;
    // Generated capability tables, loaded once from
    // tools/psx_analysis/oracle_caps.json. Empty when that file is missing, and
    // an empty table gates NOTHING — a missing file must not silently grey out
    // every button and look like an oracle that answers nothing.
    std::map<std::string, std::set<std::string>> oracle_caps;
    std::map<std::string, std::vector<std::string>> oracle_tool_needs; // tool -> kind keys
    std::string oracle_caps_error;
    bool oracle_caps_loaded = false;
    std::string frm_oracle_note;

    // Multi-disc. The roster and the shared card are read off disk once per
    // project (sync_dirs, and the oracle Refresh button) rather than every
    // frame -- the oracle row would otherwise open game.toml sixty times a
    // second to draw one dropdown.
    std::vector<DiscEntry> frm_discs;
    MemcardRef  frm_memcard;
    // 0-based index into frm_discs: which image the oracle will be started on.
    // Seeded from the disc psx-runtime last booted, because that is the only
    // disc a comparison is meaningful on; the user may then override it.
    int         frm_oracle_disc = 0;
    // 1-based disc psx-runtime last booted (settings.toml [disc] selected), or
    // 0 when it has not said. Kept beside the selection so the row can show a
    // disagreement instead of quietly comparing two different discs.
    int         frm_runtime_disc = 0;
    // Which roster row the RUNNING oracle was started on, or -1 when Studio
    // did not start it and therefore does not know.
    int         frm_oracle_disc_started = -1;

    // Widescreen site scan
    std::vector<WsSite>   ws_sites;
    std::vector<uint32_t> ws_w_imms, ws_h_imms, ws_extent_funcs;
    bool ws_discovered = false;
    bool ws_loaded = false;
    std::string ws_toml_path;

    // Git settings — where each module is fetched from and pushed to. A
    // contributor working from a fork repoints these; everyone else never
    // opens the dialog. `edit` is the text box, kept beside the values it was
    // seeded from so Save can tell which rows the user actually changed.
    struct ModuleUrlRow {
        std::string path;
        bool nested = false;
        std::string gitmodules_url;
        std::string local_url;
        std::string origin_url;
        std::string effective_url;
        std::string moved_to;
        bool present = false;
        char edit[512] = {};
    };
    std::vector<ModuleUrlRow> git_urls;
    std::string git_urls_root;
    bool git_urls_loading = false;
    bool git_settings_open = false;
    // 0 = this clone only, 1 = commit to .gitmodules. Defaults to the scope
    // that cannot surprise a collaborator.
    int git_url_scope = 0;

    // Log (ring)
    static constexpr size_t kMaxLogLines = 4000;
    std::vector<std::string> log_lines;
    // Bumped on every change to log_lines. The Activity log word-wraps into a
    // cache of display rows; this is how the drawing code knows the cache is
    // stale without hashing four thousand strings every frame.
    std::uint64_t log_revision = 0;
    bool log_scroll_bottom = true;
    bool log_expanded = true;
    float log_h_pref = 160.f;

    // Two job slots, because they guard different things.
    //
    // `busy` is the PROJECT slot: configure, build, analyze, git — everything
    // scoped to the selected repo, where letting two run at once would race the
    // same build dir or the same working tree.
    //
    // `busy_global` is the MACHINE slot: work that belongs to the toolbox
    // rather than to a game, and that takes long enough to be intolerable as a
    // whole-app lock. Building the DuckStation oracle is ~10 minutes; sharing
    // one flag would freeze Configure, Git and everything else for its whole
    // duration for no reason — the two touch nothing in common.
    std::atomic<bool> busy{false};
    std::atomic<bool> busy_global{false};
    // `busy_frames` is the OBSERVATION slot: capture / diff / layer render.
    // They read a running game and write only under analysis/frames, so they
    // race nothing the project slot guards — and crucially, they must stay
    // usable while a game is running, which is precisely when the project slot
    // is held by Launch.
    std::atomic<bool> busy_frames{false};
    std::atomic<bool> request_exit{false};

    // Pending file/folder picks from SDL dialogs (applied on main thread).
    std::mutex pick_mu;
    std::string pending_folder;
    std::string pending_file;
    std::string pending_pick_target; // disc|np_parent|np_disc|np_bios|repo_add|build_exe|export_log|build_scph|export_mingw_zip
    bool file_pick_busy = false;

    void append_log(std::string line);
    void set_status(std::string s);
    std::string selected_root() const;
    void select_repo_by_path(const std::string& path);
    // When Catalog only is on, ensure selected_repo is a catalog-backed entry.
    void coerce_catalog_only_selection();
    // Apply detected player count from the selected repo to the Migrate UI.
    void apply_selected_players();

    bool is_snes() const { return platform == Platform::SNES; }
    bool is_n64() const { return platform == Platform::N64; }
    bool is_cartridge() const { return platform_is_cartridge(platform); }
    bool has_functions_tab() const { return platform_has_functions(platform); }
    bool has_diagnostics_tab() const { return platform_has_diagnostics(platform); }
    const char* framework() const { return platform_framework(platform); }
    // Clear per-project state that belongs to the platform being left, so a
    // PSX repo path or branch list can never survive into a cartridge session.
    void reset_for_platform_switch();
    // Seed the per-console New Project defaults, once a console is chosen.
    void apply_platform_defaults();
};

} // namespace retcomm::studio
