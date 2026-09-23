# Retro Studio

Developer studio for Retro recomp titles: catalog-backed bulk Git/GitHub ops
and the Project Studio toolkit (migrate / audit / new project / GUI), for
**PlayStation** (psxrecomp), **Super Nintendo** (snesrecomp) and
**Nintendo 64** (n64lle).

## Choosing a platform

Studio opens on a platform picker and nothing else is shown until it is
answered. That is not ceremony: the choice decides which repo index is read,
which framework submodule Git and Bulk operate on, which scaffolder New Project
drives and which migration ops exist. There is no sensible default for those
before the question is asked, and loading "the PSX one for now" would race the
answer.

| | PlayStation | Super Nintendo | Nintendo 64 |
|---|---|---|---|
| Framework | `psxrecomp` | `snesrecomp` | `n64lle` |
| Repo index | `project_studio_repos.json` | `project_studio_repos_snes.json` | `project_studio_repos_n64.json` |
| Game image | Redump `.cue` | `.sfc` / `.smc` ROM | `.z64` / `.n64` / `.v64` ROM |
| Generate | `psxrecomp_cli generate` (ROM + BIOS C) | the project's own `tools/regen.sh` | the project's own `<slug>-generate` CMake target |
| Build target | `psx-runtime` | the repo's CMake `project()` name | the repo's `n64lle_add_runtime_target()` name |
| Tabs | Migrate · New Project · Git · Bulk · Build · Bulk Recomp · Functions · Diagnostics | Migrate · New Project · Git · Bulk · Build · Bulk Recomp · Functions · Diagnostics | Migrate · New Project · Git · Bulk · Build · Bulk Recomp · Diagnostics |

**Change** in the header returns to the picker. Each console keeps its own repo
list, so switching loses nothing.

**Functions** is absent rather than empty on N64: n64lle's discovery is
execution-derived inside the harvest, a port carries no symbol table, and its
debug server implements only `ping` / `ring_stats` / `ring_query` / `help` — no
`fn_stats`, no `fn_query`. The same rule applies to every button that would be a
dead end — no BIOS staging, no emitter build, no MinGW cross-build, no netplay,
no CI — and the CLI refuses those subcommands under `--platform n64` with the
reason, rather than failing deep inside somebody else's tool.

**Diagnostics** was absent on N64 for the same stated reason until 2026-09-09,
and that reason had expired. It was about the RUNTIME's debug server, which is
still thin — but the tab is about the ORACLE. n64lle's `n64ref` speaks the full
protocol, and once it built off Windows the whole differential toolset it gates
came within reach (n64lle `docs/evidence/ORACLE-LINUX-PORT-STUDY.md`). The N64
tab has three panes:

| Pane | What it drives |
|---|---|
| Oracle | `tools/n64_analysis/n64_oracle.py` — doctor / setup / start / stop for `n64ref`, and the ORACLE-PIN.md check |
| Gates | `tools/n64_analysis/n64_gates.py` — n64lle's own command / pixel / frame / scanout differentials through ctest |
| Rings | the always-on rings, over the same JSON protocol, on the runtime's port or the oracle's |

A third kind of oracle, and the manifest says which: PSX patches DuckStation to
speak our protocol, SNES takes Mesen2 unpatched and reads what its Lua wrote,
and N64 **builds** a first-party binary from a submodule pinned by
`ORACLE-PIN.md`. Only N64's records `speaks_runtime_protocol: true`. A pin
mismatch is a refusal, not a warning: an off-pin oracle answers every query and
grades every gate against a reference nothing in the evidence trail describes.

That rule is enforced by a table rather than by prose. `platforms.py` carries a
`PlatformProfile` per console whose capability fields (`has_bios`,
`has_disc_meta`, `has_netplay`, `generate_kind`, `analysis_kind`,
`mingw_script`, …) each replace a branch that used to read `key == "snes"` with
an `else` that meant both "PlayStation" and "anything else". Adding N64 is what
made those two different claims: without the table an N64 session would have
been handed psxrecomp's BIOS hunt, Redump lookup and MinGW script without a
line changing. `studio_model.hpp` carries the GUI's half of the same table.

Every Studio → toolkit call carries `--platform`, injected once in the runner
rather than at each of the ~90 call sites.

## Nintendo 64

**New Project** drives n64lle's `tools/new_project/setup_project.sh`: probe the
cartridge, lay out the repo, wire the `n64lle` + `recomp-ui` submodules, write
`game.toml`, then generate / build / run the gates. Studio prefers a live
n64lle checkout (`$N64LLE_ROOT`, the selected project's own submodule, or a
sibling checkout) and falls back to the copy vendored under
`tools/new_project_layout/n64/`. The dump is probed where it lies and
**symlinked** into `roms/` — Copy ROM (`--copy-rom`) is for a dump on removable
media, and is off by default because the framework says a link "makes it
impossible to do by accident" to commit ROM bytes.

An n64lle port needs **three names**, which is why the page asks for three: the
CMake project (`GloverRecomp`), the target prefix every target is built from
(`glover-runtime`, `glover-cosim`, `glover-generate`) and the executable
(`glover`). None of them can be derived from either of the others. Blank means
"let the scaffolder derive it", and **Probe ROM** fills all three with the same
values `--yes` would have taken.

The page also carries the **harvest window** (frames / step cap), because on
this console that *is* the coverage decision: discovery is execution-derived —
the harvester runs the real boot on the interpreter and records what executed —
so code outside the window is never emitted. Probe ROM additionally warns when
a cartridge is **CIC-6105**, which walks into n64lle's KI-1 and renders black
forever on today's framework; knowing that before scaffolding is the difference
between a known issue and a lost day.

There is deliberately **no n64lle ref** to choose: `setup_project.sh` pins the
new project at the HEAD of the checkout it was run from — "the SHA this scaffold
was cut against" — and has no flag to override it.

**Build** knows two things this console needs that the others do not. n64lle is
resolved as a **pre-built tree** rather than `add_subdirectory()`'d, so
Configure first checks that `build-n64lle/` actually holds an `n64emit` and, if
not, names `tools/build_framework.sh` instead of letting cmake die inside
`n64lle_runtime_resolve_framework()`. And **Generate** is `cmake --build
--target <slug>-generate` — the harvest and emit live in the port's own CMake
graph, not in a script or a framework CLI.

**Migrate** audits an N64 port against that scaffold — submodules, `.gitignore`,
untracked generated C *and* ROM bytes, `tools/build_framework.sh`, the contract's
`[MEASURED]` identity rows, the single `n64lle_add_runtime_target()` call, the
scaffold stubs and `framework_pins.txt` — and applies the fixes.

What it will **not** write is the point:

* `game.toml` — n64lle's scaffolder writes it once from a probed ROM and tags
  every row `[MEASURED]` / `[DECLARED]` / `[UNKNOWN]`. Its own header says no
  program in the repo writes it, and that is what makes those tags worth
  anything. A migration that regenerated it would launder Studio's guesses into
  a provenance record.
* `docs/STATUS.md` — the honesty ledger. A freshly cut one asserts that nothing
  has been measured; writing that over a port that *has* measured things would
  replace findings with a claim of ignorance.
* `CMakeLists.txt` and `README.md` — the port's build graph and its prose.

Migrate also reports, without a fix op, a port that still carries its own
`host/`: the scaffolded layout has none, because the launcher, input, audio and
run loop come from one `n64lle_add_runtime_target()` call and reach every port
on a submodule bump. Deleting a port's host is a decision with a measurement
behind it, not a mechanical sweep.

**Packaging** is a local zip only — n64lle ships no release workflow and no
packager template, so `git release-setup` refuses rather than writing a
psxrecomp workflow into an N64 port. The zip carries the executable, the staged
launcher assets, `game.toml` and `VERSION`; never ROM bytes, never `generated/`
(whose distribution posture n64lle has explicitly not settled), never the
user's `settings.toml` or `input.cfg`.

## Super Nintendo

**New Project** drives snesrecomp's `tools/new_project/setup_project.sh`:
probe the ROM, lay out the repo, wire the submodules, seed `recomp/*.cfg`, then
generate / build / publish. Studio prefers a live snesrecomp checkout
(`$SNESRECOMP_ROOT`, the selected project's own submodule, or a sibling
checkout) and falls back to the copy vendored under
`tools/new_project_layout/snes/` so a packaged install works with no framework
on disk. The ROM is probed where it lies and never enters the repository.
Fields the SNES scaffolder has no flag for (BIOS, boot EXE, lobby, boxart) are
named in the log rather than silently dropped.

On a terminal that wizard **prompts** for name, region, description, publisher
and year, with the probed cartridge identity as each default. Studio runs it
with `--yes`, which takes every default silently — so the page carries those
fields and **Probe ROM** fills them with the same values the prompts would have
offered (and warns about a coprocessor or a header checksum that does not
validate). Anything typed there is passed as a flag; a field that never became
one would be lost without a word.

**Region is blank by default on SNES**, meaning *use the cartridge header*.
Studio does not carry the PSX habit of `USA` here: sending it would relabel a
Japanese cartridge in the README and in the packaged zip name.

**Migrate** audits a SNES port against that scaffold — framework and recomp-ui
submodules, nested `lib/recomp-net` + `lib/retcomm-rbengine`, `.gitignore`,
committed generated C, `recomp/` analysis config, `VERSION`, `tools/regen.sh`,
`scripts/package_release.sh`, CI, and whether `framework_pins.txt` still
matches the gitlinks — and applies the fixes.

It also checks two things a **fork** gets wrong that a repo scaffolded here
never does: whether the `snesrecomp` gitlink the fork inherited can actually run
the `tools/regen.sh` in the same repo, and which wizard Studio is driving when
that submodule ships no `tools/new_project` of its own. Those two together are
how a port ends up with a regen.sh that dies on `invalid choice: 'verify-rom'`
— so Migrate now refuses to *write* a regen.sh the port's own framework could
not run, rather than emitting it and leaving the failure for the Build tab.
Moving a framework pin is never done for you — but the **Git** tab can do it
when you ask. The two buttons there are opposite operations, and the difference
is the thing that costs people an afternoon:

* **Restore pinned** — `git submodule update`. Checks out the revision this
  repo *already records*. On a fork carrying a stale pin it puts the old
  revision back, which is why reaching for it leaves the pin where it was.
* **Advance pins** — fetches, moves each module to its tracked branch tip, and
  **stages the new gitlink**. That staging is the half that is easy to forget:
  without it nothing about the superproject has changed. It reports
  `snesrecomp: a64932f1a → f624c9f12 (main)` and stops there — review with
  `git diff --cached`, then Commit and Push on the same tab. On the command
  line: `git advance-pins [--modules] [--nested] [--paths …] [--ref <rev>]`.

Both consoles, one tab. Advance pins honours the same **Targets** ticks as
Switch / Pull / Commit / Push, so a PSX port can advance `recomp-net` and
`retcomm-rbengine` *inside* `psxrecomp` — where most of what it pins actually
lives. Nested gitlinks are staged in the framework checkout, so that repo needs
its own commit before advancing the game repo's framework pin is worth doing;
the result says so rather than leaving it to be deduced from a confusing diff.

"Tracked branch tip" means the branch `.gitmodules` declares, not `master` by
assumption — a port tracking `feat/…` follows that. Use `--ref` for a specific
revision instead.

**Git settings** (header, beside Check updates) is where a contributor points a
module at their own fork. Three settings answer "which repo is this", and they
are not the same one:

| | scope | what it drives |
|---|---|---|
| `.gitmodules submodule.<p>.url` | tracked | what everyone who clones this port gets |
| `.git/config submodule.<p>.url` | this clone | what `submodule update` fetches |
| the checkout's `origin` | this clone | what **push and pull** use |

The dialog lists every submodule and nested module with its effective URL, and
makes you say which scope you mean. **This clone only** (the default) moves the
last two: push and pull go to your fork, nothing tracked changes, and nobody
else sees it. **Commit to .gitmodules** additionally rewrites the tracked URL —
that repoints the port for everyone who clones it, so it is never the default
and has to be committed. Reset drops a local override and goes back to
`.gitmodules`. On the command line: `git module-urls` and
`git set-module-url --path … --url … [--nested] [--scope …] [--reset]`.

None of this is needed to *use* the upstream repos. Restore pinned, Advance
pins, and pulling all work read-only, with no write access to `psxrecomp` or
`recomp-ui`.

Re-run Audit afterwards: `framework_pins.txt` is stale the moment a pin moves,
and the port now builds against a different framework.

Two things it deliberately will **not** do:

* **Untracking generated C keeps the working tree** (`git rm --cached` only), so
  a developer mid-build does not lose the C they just generated.
* **It refuses to emit `tools/regen.sh` when the ROM digests cannot be
  resolved.** Digests are carried across from what a previous scaffold already
  baked in, or probed from a ROM you point it at — never invented. A regen.sh
  carrying made-up digests verifies a ROM nobody owns and fails at the least
  useful moment.

**README & About** is a switch, not a step. README badges, boxart, the Retro
Launcher section, the R.A.I.D. footer and the repository's GitHub About blurb
are one op, so one checkbox governs them — and it governs the *audit* as well
as the apply, which none of the other checkboxes do. A port whose README is
hand-written should not be reading a warning about it on every run. The row
stays visible as `skip` rather than disappearing, because a row that vanishes
reads as "nothing to do here". On the command line: `--no-readme`, on `audit`
as well as `plan` / `apply`. Asking for the op by name (`--only
patch_readme_metrics`) still runs it.

**Build → Regenerate C from ROM** runs the project's own `tools/regen.sh` rather
than reimplementing generation, because regen.sh verifies the ROM against the
digests that port was pinned against first. Turning that check off is possible
and says so in orange.

It preflights that script before running it, because the thing most likely to
be wrong in a freshly forked port is not the ROM. Studio resolves the framework
regen.sh will actually use (`$SNESRECOMP_ROOT`, else the repo's own
`snesrecomp/`), asks that CLI what subcommands it offers, and refuses with the
mismatch named when the port's `snesrecomp` pin predates its own regen.sh — the
skew that otherwise arrives as `invalid choice: 'verify-rom'` attributed to the
Generate button. It refuses the same way when `rom_identity.txt` is missing or
carries empty digests, since `--verify` would then be checking the ROM against
nothing.

## Bulk Recomp — many images, one folder, for testing

**Bulk Recomp** takes a multi-selection of game images and runs every one of
them through the whole pipeline into one output folder, to answer "which of
these dumps get through today?". It is a test harness, not a way to make ports.

| Console | Stages, per image |
|---|---|
| PlayStation | probe (`lookup-disc-meta`) → scaffold → generate → configure → compile |
| Super Nintendo | probe (`probe-rom`) → scaffold → generate → configure → compile |
| Nintendo 64 | probe (`probe-rom`) → scaffold → framework → generate → compile |

Each stage is the same CLI subcommand the New Project and Build tabs run for
one project, so a batch tests what the buttons do. The orchestration lives in
the toolkit (`project_studio bulk-recomp`), and the tab only starts it and draws
what it reports, so a batch also runs headless:

```bash
cd tools/new_project_layout
python3 -m project_studio --platform snes bulk-recomp --out ~/BulkTest \
    --rom a.sfc --rom b.sfc --parallel 2
```

Everything a batch writes is under the output folder:

| Path | What |
|---|---|
| `<out>/<Project>/` | each scaffolded port |
| `<out>/Log Output/<NN-image>/<n>-<stage>.log` | the full output of every stage |
| `<out>/Log Output/summary.txt` | PASSED / FAILED per image, with the failing stage and its log |
| `<out>/Log Output/bulk_status.json` | live state; the tab polls it |

Stage output goes to those files, **not** the Activity log — several builds'
output interleaved in one pane is not readable. The table shows one row per
image with a progress bar (stage *n*/*N*, and inside configure / compile the
build tool's own `[n/m]` count), then PASSED or FAILED; hover a bar for every
stage's state, and **Log** opens the stage log.

Defaults, and why:

* **No GitHub repos, no CI, no boxart.** Each reaches outside the output folder,
  and a test batch that created forty private repos is a cleanup job. The
  checkboxes are there if you want them.
* **Not added to the repo index.** Throwaway scaffolds would bury the ports in
  the dropdown. Tick *Add to repo index* to keep them.
* **Parallel = 1.** Raise it to run projects at once. *Build jobs* on auto
  divides the cores between them (`cores / parallel` per compile), since each
  cmake build otherwise takes every core.
* **An existing project folder FAILS** that image rather than rebuilding an old
  scaffold and calling it a pass. *Reuse existing projects* rebuilds it instead
  and marks the scaffold `skipped`.
* **Two images that probe to one project** (two revisions of the same game)
  are not both scaffolded into it: the second is FAILED with the reason. Run it
  in another output folder.

**Stop** kills the running stages' whole process trees and marks the rest
stopped; closing Studio does the same. On PSX each `.cue` is its own project —
multi-disc sets are not grouped.

## Setup

```bash
cd ~/Documents/GitHub/retcomm-studio
cp studio.toml.example studio.toml
# edit catalog / checkout_roots / [titles] as needed
```

Requires Python 3.11+ (toolkit engine), `git`, and `gh` (for release dispatch).  
GUI is a native **Dear ImGui** app (SDL3 + OpenGL3), same stack as Retro Hub.

```bash
# Build + run GUI
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
./build/Retro-Studio

# Or via Python entry (locates the binary / prints build help)
python3 project_studio_gui.py
```

## Frames tab — which function drew this?

A rendering bug in a recomp is usually a question about authorship: *which guest
function issued these primitives, and with which blend mode?* The Frames tab
connects to a running game, captures a frame's GP0 stream, and answers it.

* **Runtime / Ring** — connect to the debug server (default 4370) and see which
  frames the GP0 ring can still be asked for, plus savestate slot 1. There is no
  pause/step here and cannot be: psx-runtime removed those commands, because it
  is designed to be read from rather than steered. The ring holds ~1M packets —
  several hundred frames — so you play until the bug is on screen and then reach
  *backwards* for that frame and the ones before it, which is the only thing
  that works for a glitch you cannot reliably stop on.
* **Capture** — take the frame as `good` or `bad`, at the newest frame or any
  frame still in the ring (out-of-ring frames are flagged, because a dump of an
  evicted frame is empty and reads like "drew nothing"). Runs the game's own
  `psxrecomp/tools/gpu_frame_capture.py`, which writes the dump, a compact
  summary, the opcode histogram and a screenshot into `<root>/analysis/frames`.
* **Attribution** — one row per guest function: primitives issued, how many
  carried the semi-transparency bit, which blend modes, OT rank span, and the
  static name if the analysis bundle knows the address. **Save name** writes
  straight into `symbols.toml` through the same CLI the Functions tab uses.
* **Diff** — good vs bad, headlining the two findings that name a bug on their
  own: a blend mode that disappeared, and a function that stopped drawing.
* **Layers** — one image per function, so you can see each one's contribution
  in isolation.
* **Oracle** — set up, start and stop the patched DuckStation used for parity.
  It installs **machine-wide**, into the Retro data root
  (`~/.local/share/retcomm/oracle/duckstation`, the same root as the toolchains
  and catalog), so one build serves every title and none of these buttons take a
  repo. Start uses the selected project's `[game] disc`, because parity is
  meaningless unless both emulators boot the same image.
* **DuckStation parity** — same frame on both emulators; identical images mean
  the guest ran the same and the bug is in the renderer. Enabled only when the
  oracle is actually answering.

**The game must be built with the debug server in it.** `PSX_DEBUG_TOOLS`
defaults OFF for Release builds, and without it the runtime never calls
`debug_server_init()` — nothing listens on any port, so no amount of retrying
the connection helps. The **Build tab → Debug tools** row shows what the
configured build dir will do, injects `-DPSX_DEBUG_TOOLS=ON/OFF` into Configure,
and has a **Configure for debugging** button that sets Release + ON in one click
(a real Debug build of a recomp is far too slow to reach the frame you want).
The Frames tab prints the same verdict when it cannot connect. The port itself
is a runtime setting — `game.toml` `[runtime] debug_port` — and the Frames tab
defaults to whatever the selected project declares.

Studio never decodes a GP0 packet itself. The tools live in the game's engine
submodule (they must match the runtime that produced the dump) and Studio is a
viewer over the JSON and PNG they write, so a headless capture and this tab can
never disagree. If the tab reports the tools are missing, the project needs
`git submodule update --init`. See `psxrecomp/docs/GPU_FRAME_TOOLS.md`.

Everything the tab shows is **observed** from one execution of one frame, and is
labelled as such — it is never folded into the Functions tab's static claims.

### Two job slots

Studio serialises work so two jobs cannot race the same build directory. There
are two locks, not one:

| Slot | Guards | Examples |
|---|---|---|
| **project** | the selected repo | Configure, Build, Analyze, Git, capture, diff |
| **global** | machine-wide toolbox work | building the DuckStation oracle |

The oracle build takes about ten minutes and touches nothing a project job
touches, so it runs on the global slot and the rest of Studio stays usable while
it works. A job that would race another in the same slot is refused with a
message rather than queued.

## Updates (Studio + shared toolchain + catalog)

On startup (and via **Check updates** in the header) Studio checks GitHub for:

1. A newer **Retro Studio** release (`RetroPortingToolKit/Retro-Studio`)
2. A newer shared **retcomm-toolchain** pack (`RetroPortingToolKit/RetroPorting-Toolchains`)
3. A newer **retcomm-catalog** zip (same shared cache as Retro Hub/Launcher)

Catalog sync writes into the shared data root and immediately refreshes:

- Game repo dropdown **Catalog only** membership (`in_catalog`)
- Bulk **Catalog only** selection ticks

| OS | Catalog cache |
|----|---------------|
| Linux / macOS | `~/.local/share/retcomm/catalog/` |
| Windows | `%LOCALAPPDATA%\retcomm\catalog\` |

Toolchain packs install into the same cache as Retro Launcher and game apps:

| OS | Path |
|----|------|
| Linux / macOS | `~/.local/share/retcomm/toolchains/cmake-clang-v1/<tag>/` |
| Windows | `%LOCALAPPDATA%\retcomm\toolchains\cmake-clang-v1\<tag>\` |

Override roots with `RETCOMM_DATA_DIR` / `RETCOMM_TOOLCHAIN_DIR` / `RETCOMM_CATALOG_DIR`
(same as the launcher).  
Disable startup checks via `~/.config/retcomm/studio.json`:

```json
{ "check_updates_on_startup": false }
```

(Also honors launcher `config.json` → `check_updates_on_startup` when studio.json is unset.)

## Releases (GUI packages)

Manual workflow: Actions → **Release**. Leave **version** empty to auto-bump
`x.x.x` from the latest `vX.Y.Z` tag (or `VERSION` on first release).

| Asset | Platform |
|-------|----------|
| `Retro-Studio-linux-x86_64.AppImage` | Linux |
| `Retro-Studio-portable-windows.zip` | Windows portable |
| `Retro-Studio-windows-x64-setup.exe` | Windows installer |
| `Retro-Studio-macos-arm64.dmg` | macOS Apple Silicon |
| `Retro-Studio-macos-x86_64.dmg` | macOS Intel |

Icon: `assets/retcomm-studio.svg` (teal-on-dark, launcher-matched). Packaging under `packaging/`.

## Layout

```
retcomm-studio/
  studio.toml.example
  retcomm_studio_cli.py
  migrate_project.py
  project_studio_gui.py      # launches native ImGui binary
  CMakeLists.txt             # Retro-Studio (Dear ImGui)
  src/studio/                # ImGui shell + Python runner
    studio_functions.*       #   Functions tab (static discovery)
    studio_frames.*          #   Frames tab (live GP0 capture / attribution)
    studio_frames_data.*     #     its loaders — ImGui-free, unit tested
    studio_debug.*           #   TCP debug-server client (shared by both)
  VERSION
  assets/                    # icons + fonts/
  packaging/
  .github/workflows/release.yml
  retcomm_studio/            # catalog bulk CLI
  tools/new_project_layout/
    project_studio/          # Python engine (migrate / git / build / …)
      platforms.py           #   the psx/snes profile — one per process
      snesops.py             #   SNES audit / plan / apply
      snes_paths.py          #   locate the snesrecomp wizard
    templates/               # PSX scaffold templates
    snes/                    # vendored snesrecomp wizard (see snes/VENDOR.md)
    ci_templates/
  tests/
    snes_platform_test.py    # platform split + SNES migration, no GPU needed
    json_null_test.cpp       # null-vs-absent at the toolkit JSON boundary
```

## Tests

```bash
cmake --build build --target snes_platform_test   # or: python3 tests/snes_platform_test.py
./build/json_null_test                            # toolkit JSON → model boundary
./build/analysis_load_test <repo-root>
./build/frames_load_test [analysis/frames]
./build/spawn_test
./build/debug_client_test
```


## License

Retro Studio is licensed under the **PolyForm Noncommercial License 1.0.0** —
use, modification and distribution are permitted for **noncommercial purposes
only**. See [`LICENSE`](LICENSE).

**Commercial use requires a separate license.** Contact
[alex@technicallycomputers.ca](mailto:alex@technicallycomputers.ca) — see
[`COMMERCIAL.md`](COMMERCIAL.md) for what counts as commercial and what to
include when you get in touch.

Not everything in this repository is covered by those terms. Third-party
components keep their own licenses, and the emulator patches under
`tools/psx_analysis/` are derivative works of the emulators they patch. See
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

Versions published before this change remain available under the MIT license
they were released under.
