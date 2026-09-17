#!/usr/bin/env bash
# setup_project.sh — scaffold a new n64lle per-game repo, end to end.
#
#   sh tools/new_project/setup_project.sh "/roms/Mario Kart 64 (USA).z64" --dir ~/src
#
# On a terminal the ROM is the only argument you need; with none at all it is
# the first question. Everything else is asked, with the PROBED ROM identity
# supplying the defaults. Flags are for scripting: anything passed explicitly
# skips its question, and --yes (or a non-TTY run) takes every default without
# asking, with the network- and build-touching steps off unless flagged.
#
# The N64 counterpart to psxrecomp's tools/new_project_layout/setup_project.sh
# and snesrecomp's tools/new_project/setup_project.sh, and it follows the same
# publish order for the same reason: scaffold -> commit -> `gh repo create` (no
# push) -> generate/build -> one push. Pushing earlier leaves a second "initial"
# commit that collides when the script is re-run.
#
# WHAT IT DOES NOT DO. It does not copy a host, a launcher, or an emulator into
# the new repo. A per-game repo is a game.toml, a CMakeLists that makes one
# n64lle_add_runtime_target() call, its gates, and its docs — everything else is
# the framework's, so a fix upstream reaches every port on a submodule bump.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
N64LLE_ROOT="$(cd "$HERE/../.." && pwd)"
TEMPLATES="$HERE/templates"

N64LLE_URL="https://github.com/RetroPortingToolKit/n64lle.git"
RECOMP_UI_URL="https://github.com/mstan/recomp-ui.git"

# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------
ROM=""
DIR=""
NAME=""
PROJECT=""
SLUG=""
EXE=""
PLAYERS=""
HARVEST_FRAMES=""
HARVEST_STEP_CAP_M=""
ASSUME_YES=0
DO_GENERATE=""
DO_GIT=""
DO_GH=""
GH_OWNER=""
GH_VISIBILITY="private"
COPY_ROM=""

usage() {
  cat <<'USAGE'
usage: setup_project.sh [rom] [options]

  --rom <path>          the dump to probe and scaffold around
  --dir <path>          where to create the project (default: the parent of
                        this n64lle checkout, i.e. beside your other repos)
  --name <text>         display name          (default: from the ROM filename)
  --project <text>      repo/CMake project    (default: <Name>Recomp)
  --slug <text>         target prefix         (default: lowercased name)
  --exe <text>          executable name       (default: the slug)
  --players <1-4>       controller ports to declare              (default: 1)
  --frames <n>          harvest window in frames               (default: 900)
  --step-cap <n>        harvest step cap, in millions         (default: 3000)
  --copy-rom            copy the dump into roms/ (default: symlink it)
  --generate            run the framework build + harvest + emit + gates
  --no-generate         skip that (default when non-interactive)
  --git / --no-git      initialize a git repo and commit    (default: yes)
  --gh                  create a GitHub repo with `gh` and push  (default: no)
  --gh-owner <owner>    owner for --gh
  --public              make the --gh repo public          (default: private)
  --yes                 take every default without asking
  --help

Nothing assumes a GitHub repository exists. Declining --gh skips creation, the
remote and the push; the project is still a complete local git repository.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --rom) ROM="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --slug) SLUG="$2"; shift 2 ;;
    --exe) EXE="$2"; shift 2 ;;
    --players) PLAYERS="$2"; shift 2 ;;
    --frames) HARVEST_FRAMES="$2"; shift 2 ;;
    --step-cap) HARVEST_STEP_CAP_M="$2"; shift 2 ;;
    --copy-rom) COPY_ROM=1; shift ;;
    --generate) DO_GENERATE=1; shift ;;
    --no-generate) DO_GENERATE=0; shift ;;
    --git) DO_GIT=1; shift ;;
    --no-git) DO_GIT=0; shift ;;
    --gh) DO_GH=1; shift ;;
    --gh-owner) GH_OWNER="$2"; shift 2 ;;
    --public) GH_VISIBILITY="public"; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -*) echo "unknown option: $1" >&2; usage; exit 2 ;;
    *) if [ -z "$ROM" ]; then ROM="$1"; else echo "unexpected: $1" >&2; exit 2; fi; shift ;;
  esac
done

INTERACTIVE=0
if [ -t 0 ] && [ "$ASSUME_YES" -eq 0 ]; then INTERACTIVE=1; fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

ask() {   # ask <prompt> <default> -> echoes the answer
  local prompt="$1" default="$2" reply=""
  if [ "$INTERACTIVE" -eq 0 ]; then printf '%s' "$default"; return; fi
  printf '%s [%s]: ' "$prompt" "$default" >&2
  IFS= read -r reply || true
  printf '%s' "${reply:-$default}"
}

ask_yn() {  # ask_yn <prompt> <default y|n> -> returns 0 for yes
  local prompt="$1" default="$2" reply=""
  if [ "$INTERACTIVE" -eq 0 ]; then [ "$default" = "y" ]; return; fi
  printf '%s [%s/%s]: ' "$prompt" \
    "$([ "$default" = y ] && echo Y || echo y)" \
    "$([ "$default" = y ] && echo n || echo N)" >&2
  IFS= read -r reply || true
  reply="${reply:-$default}"
  case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

command -v python3 >/dev/null 2>&1 || die "python3 is required"

# ---------------------------------------------------------------------------
# 1. the ROM, and what it says about itself
# ---------------------------------------------------------------------------
if [ -z "$ROM" ]; then
  [ "$INTERACTIVE" -eq 1 ] || { usage; die "no ROM given"; }
  ROM="$(ask 'Path to your dump' '')"
fi
[ -f "$ROM" ] || die "no such file: $ROM"

step "Probing $(basename "$ROM")"
PROBE="$(mktemp)"
trap 'rm -f "$PROBE"' EXIT
python3 "$HERE/probe_rom.py" "$ROM" --shell > "$PROBE" || die "probe failed"
# shellcheck disable=SC1090
. "$PROBE"

[ -n "${ROM_CIC:-}" ] || die \
  "no CIC matched the IPL2 checksum for this dump. It may be modified, or a
   variant the table does not carry. Scaffolding a project around an
   unidentified cartridge would bake a guess into its contract."

python3 "$HERE/probe_rom.py" "$ROM"

# ---------------------------------------------------------------------------
# 2. the questions
# ---------------------------------------------------------------------------
default_name() {
  # The filename with its (Region) and (Rev) tags stripped reads better than
  # the 20-byte header title, which is often SHOUTED and abbreviated
  # ("MARIOKART64"). The header title is offered when the filename gives
  # nothing to work with.
  local base
  base="$(basename "$ROM")"
  base="${base%.*}"
  base="$(printf '%s' "$base" | sed -E 's/[[:space:]]*\([^)]*\)//g; s/[[:space:]]*\[[^]]*\]//g; s/[[:space:]]+$//')"
  if [ -z "$base" ]; then base="${ROM_HEADER_TITLE:-N64 game}"; fi
  printf '%s' "$base"
}

step "Project identity"
[ -n "$NAME" ] || NAME="$(ask 'Display name' "$(default_name)")"

slugify() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+//g'; }
pascal()  { printf '%s' "$1" | sed -E 's/[^A-Za-z0-9]+/ /g' | awk '{for(i=1;i<=NF;i++){$i=toupper(substr($i,1,1)) substr($i,2)}; print}' | tr -d ' '; }

[ -n "$SLUG" ]    || SLUG="$(ask 'Target prefix (slug)' "$(slugify "$NAME")")"
[ -n "$PROJECT" ] || PROJECT="$(ask 'Repo / CMake project name' "$(pascal "$NAME")Recomp")"
[ -n "$EXE" ]     || EXE="$(ask 'Executable name' "$SLUG")"
[ -n "$PLAYERS" ] || PLAYERS="$(ask 'Controller ports to declare (1-4)' '1')"
[ -n "$HARVEST_FRAMES" ] || HARVEST_FRAMES="$(ask 'Harvest window, in frames' '900')"
[ -n "$HARVEST_STEP_CAP_M" ] || HARVEST_STEP_CAP_M="$(ask 'Harvest step cap, in millions' '3000')"

case "$PLAYERS" in 1|2|3|4) ;; *) die "players must be 1-4 (got '$PLAYERS')" ;; esac
case "$SLUG" in ''|*[!a-z0-9_]*) die "slug must be lowercase letters/digits (got '$SLUG')" ;; esac

[ -n "$DIR" ] || DIR="$(ask 'Create the project in' "$(dirname "$N64LLE_ROOT")")"
ROOT="$DIR/$PROJECT"
[ -e "$ROOT" ] && die "$ROOT already exists"

if [ -z "$DO_GIT" ]; then ask_yn 'Initialize a git repository and commit?' y && DO_GIT=1 || DO_GIT=0; fi
if [ -z "$DO_GENERATE" ]; then
  if [ "$INTERACTIVE" -eq 1 ]; then
    ask_yn 'Build the framework and generate C from the ROM now? (minutes)' y && DO_GENERATE=1 || DO_GENERATE=0
  else
    DO_GENERATE=0
  fi
fi
if [ -z "$DO_GH" ]; then
  if [ "$INTERACTIVE" -eq 1 ] && command -v gh >/dev/null 2>&1; then
    ask_yn 'Create a GitHub repository with gh and push?' n && DO_GH=1 || DO_GH=0
  else
    DO_GH=0
  fi
fi
if [ "$DO_GH" -eq 1 ] && [ -z "$GH_OWNER" ]; then
  GH_OWNER="$(ask 'GitHub owner' "$(gh api user --jq .login 2>/dev/null || echo '')")"
  [ -n "$GH_OWNER" ] || die "--gh needs an owner"
fi

# ---------------------------------------------------------------------------
# 3. derived values + the token table
# ---------------------------------------------------------------------------
ROM_EXT="${ROM##*.}"
ROM_BASENAME="$SLUG.$ROM_EXT"
ROM_FILE="roms/$ROM_BASENAME"
SLUG_UPPER="$(printf '%s' "$SLUG" | tr '[:lower:]' '[:upper:]')"
DATE="$(date +%Y-%m-%d)"
ROM_SIZE_H="$(python3 - "$ROM_SIZE" <<'PY'
import sys
n = int(sys.argv[1])
print(f"{n/1048576:.0f} MiB" if n % 1048576 == 0 else f"{n/1048576:.2f} MiB")
PY
)"
WINDOW_TITLE="$NAME (n64lle)"

PORT2=none; PORT3=none; PORT4=none
[ "$PLAYERS" -ge 2 ] && PORT2=gamepad
[ "$PLAYERS" -ge 3 ] && PORT3=gamepad
[ "$PLAYERS" -ge 4 ] && PORT4=gamepad

# A 6105 cartridge walks into n64lle's one known-blocking bug. Saying so in the
# scaffolded STATUS.md is the difference between a day of confused debugging and
# a known issue with a name.
# The vars file is KEY=VALUE, one per line, so every value here must be a single
# line. Markdown reflows, so that costs nothing in the rendered document.
if [ "$ROM_CIC" = "CIC-NUS-6105" ]; then
  CIC_NOTE="**This is a CIC-6105 cartridge, and that matters.** n64lle's HLE boot zero-fills RSP IMEM, and 6105's IPL3 XOR-decrypts IMEM in place — over zeros that yields garbage, the boot RSP task never runs, and the game renders black forever. That is n64lle KI-1 (\`docs/evidence/KNOWN-ISSUES.md\`), it is open, and it is not something this project can fix from here. Expect a black screen until the framework's PIF boot FSM lands."
else
  CIC_NOTE="\`$ROM_CIC\` is not CIC-6105, so n64lle KI-1 — the 6105 IPL3 IMEM staging bug that renders Majora's Mask black forever — does not apply to this title. That is a real advantage, and worth knowing before spending a day debugging a blank frame."
fi

VARS="$(mktemp)"
trap 'rm -f "$PROBE" "$VARS"' EXIT
cat > "$VARS" <<VARSEOF
NAME=$NAME
PROJECT=$PROJECT
SLUG=$SLUG
SLUG_UPPER=$SLUG_UPPER
EXE=$EXE
DATE=$DATE
WINDOW_TITLE=$WINDOW_TITLE
CARTID=$ROM_CARTID
REGION=$ROM_REGION
REGION_LABEL=$ROM_REGION_LABEL
REVISION=$ROM_REVISION
ENTRY_PC=$ROM_ENTRY_PC
ROM_SIZE=$ROM_SIZE
ROM_SIZE_H=$ROM_SIZE_H
BYTE_ORDER=$ROM_BYTE_ORDER
CIC=$ROM_CIC
CIC_SEED=$ROM_CIC_SEED
CIC_CHALLENGE=$ROM_CIC_CHALLENGE
CIC_NOTE=$CIC_NOTE
CRC1=$ROM_CRC1
CRC2=$ROM_CRC2
SHA256=$ROM_SHA256
ROM_FILE=$ROM_FILE
ROM_BASENAME=$ROM_BASENAME
HARVEST_FRAMES=$HARVEST_FRAMES
HARVEST_STEP_CAP_M=$HARVEST_STEP_CAP_M
PLAYERS=$PLAYERS
PORT2=$PORT2
PORT3=$PORT3
PORT4=$PORT4
VARSEOF

# ---------------------------------------------------------------------------
# 4. lay the tree out
# ---------------------------------------------------------------------------
step "Creating $ROOT"
mkdir -p "$ROOT"/{docs,tools,roms,generated}

fill() {  # fill <template> <destination>
  python3 "$HERE/fill_tokens.py" "$TEMPLATES/$1" "$ROOT/$2" --vars "$VARS"
}

fill CMakeLists.txt.in      CMakeLists.txt
fill game.toml.in           game.toml
fill gitignore.in           .gitignore
fill README.md.in           README.md
fill CLAUDE.md.in           CLAUDE.md
fill VERSION.in             VERSION
fill STATUS.md.in           docs/STATUS.md
fill build_framework.sh.in  tools/build_framework.sh
fill roms_README.md.in      roms/README.md
fill generated_README.md.in generated/README.md
chmod +x "$ROOT/tools/build_framework.sh"
say "  laid out: CMakeLists.txt game.toml README.md CLAUDE.md VERSION docs/ tools/ roms/ generated/"

# The dump. A symlink by default: a per-game repo must never hold ROM bytes,
# and a link makes that impossible to do by accident. --copy-rom is for a
# machine where the dump lives on removable media.
if [ "${COPY_ROM:-0}" = "1" ]; then
  cp "$ROM" "$ROOT/$ROM_FILE"
  say "  copied the dump to $ROM_FILE"
else
  ln -s "$(cd "$(dirname "$ROM")" && pwd)/$(basename "$ROM")" "$ROOT/$ROM_FILE"
  say "  linked the dump at $ROM_FILE"
fi

# ---------------------------------------------------------------------------
# 5. git + submodules
# ---------------------------------------------------------------------------
if [ "$DO_GIT" -eq 1 ]; then
  step "Git"
  git -C "$ROOT" init -q
  git -C "$ROOT" submodule add -q -b main "$N64LLE_URL" n64lle
  git -C "$ROOT" submodule add -q -b master "$RECOMP_UI_URL" recomp-ui
  # Pin the framework at the SHA this scaffold was cut against, not at whatever
  # main happens to be later: a project should build the same way tomorrow.
  PIN="$(git -C "$N64LLE_ROOT" rev-parse HEAD 2>/dev/null || echo '')"
  if [ -n "$PIN" ] && git -C "$ROOT/n64lle" cat-file -e "$PIN^{commit}" 2>/dev/null; then
    git -C "$ROOT/n64lle" checkout -q "$PIN"
    say "  n64lle pinned at $PIN (the SHA this scaffold was cut against)"
  else
    say "  NOTE n64lle left at the branch tip — the local checkout's HEAD is not"
    say "       reachable from the remote yet (unpushed work?). Pin it by hand"
    say "       once it is pushed."
  fi
  git -C "$ROOT" submodule update --init --recursive -q n64lle recomp-ui || true
  git -C "$ROOT" add -A
  git -C "$ROOT" -c user.useConfigOnly=false commit -q -m "Scaffold $PROJECT: $NAME on n64lle

Cut by n64lle tools/new_project/setup_project.sh on $DATE.

ROM identity, read off the dump and not inferred from its filename:
  cart id  $ROM_CARTID
  region   $ROM_REGION ($ROM_REGION_LABEL)
  CIC      $ROM_CIC (seed $ROM_CIC_SEED, challenge $ROM_CIC_CHALLENGE)
  size     $ROM_SIZE bytes
  sha256   $ROM_SHA256

Nothing is measured yet beyond that identity. docs/STATUS.md says so rather
than carrying placeholders that read like results."
  say "  committed the scaffold"
fi

# ---------------------------------------------------------------------------
# 6. GitHub (before generating, so a long build is not between you and a repo)
# ---------------------------------------------------------------------------
if [ "$DO_GH" -eq 1 ]; then
  step "GitHub"
  command -v gh >/dev/null 2>&1 || die "gh is not installed"
  gh repo create "$GH_OWNER/$PROJECT" "--$GH_VISIBILITY" \
     --source "$ROOT" --remote origin --description "$NAME on n64lle"
  say "  created $GH_OWNER/$PROJECT ($GH_VISIBILITY); not pushed yet"
fi

# ---------------------------------------------------------------------------
# 7. build + generate + gates
# ---------------------------------------------------------------------------
if [ "$DO_GENERATE" -eq 1 ]; then
  step "Building the framework (out of tree, low priority)"
  # THE FRAMEWORK OWNS THIS SCRIPT. n64lle/tools/build_framework.sh is shared by
  # every port; tools/build_framework.sh is a shim onto it (see the template).
  # Run the shared one directly when the pinned n64lle has it, so a scaffold is
  # never at the mercy of the copy it just wrote, and fall back to the shim for
  # a pin older than 2026-09-15.
  if [ -f "$ROOT/n64lle/tools/build_framework.sh" ]; then
    N64LLE_PORT_ROOT="$ROOT" sh "$ROOT/n64lle/tools/build_framework.sh" Release
  else
    say "  note: this n64lle pin predates n64lle/tools/build_framework.sh;"
    say "        using the port's shim, which builds inline."
    "$ROOT/tools/build_framework.sh" Release
  fi

  # THE RSP CENSUS HAS TO BE ON, AND A SCAFFOLD IS THE RIGHT PLACE TO SAY SO.
  #
  # -DN64LLE_RSP_CENSUS=1 gates the harvest's RSP microcode capture. Without it
  # the harvest writes no <image>.n64img.rsp/ sidecar, rspemit compiles no
  # programs, the binary links zero n64_rspgen_ symbols, and the RSP runs fully
  # interpreted -- 44.2% of the emulation thread on the title where it was
  # finally caught (2026-09-15), on which seven of nine N64 ports were then
  # found to be in the same state.
  #
  # The harvester refuses to finish without it now, so the build below would
  # fail anyway. Checking here turns that into a sentence about the framework
  # build rather than a fatal error three steps later in a log nobody reads yet.
  if [ -f "$ROOT/build-n64lle/CMakeCache.txt" ] &&
     ! grep -q 'N64LLE_RSP_CENSUS=1' "$ROOT/build-n64lle/CMakeCache.txt"; then
    say ""
    say "  WARNING: the framework was built WITHOUT -DN64LLE_RSP_CENSUS=1."
    say "           The harvest will refuse to finish, and if it were forced"
    say "           past that the RSP would run fully interpreted with no"
    say "           build step reporting it. Rebuild with the shared"
    say "           n64lle/tools/build_framework.sh, or set"
    say "           N64LLE_HARVEST_ALLOW_NO_RSP=1 if this title truly runs no"
    say "           RSP tasks."
    say ""
  fi

  step "Harvest -> emit -> compile -> gates"
  cmake -S "$ROOT" -B "$ROOT/build" -DCMAKE_BUILD_TYPE=Release \
        $(command -v ninja >/dev/null 2>&1 && echo "-G Ninja")
  cmake --build "$ROOT/build" -j "$(nproc 2>/dev/null || sysctl -n hw.ncpu)"

  # TOOL SKEPTICISM: probe_rom.py re-implements the IPL2 checksum in Python
  # because scaffolding runs before the framework is built. Now that the real
  # detector exists, make the two agree or say so loudly — a contract that
  # names the wrong CIC would be a silent lie in a [MEASURED] row.
  step "Cross-checking the probed CIC against the framework's own detector"
  PROBE_LINE="$("$ROOT/build/$EXE" --check-config 2>/dev/null | head -1 || true)"
  DETECTED="$(N64_RSP_EXEC=1 "$ROOT/build/$SLUG-frame-probe" "$ROOT/$ROM_FILE" \
                /dev/null 1 1 2>/dev/null | sed -n 's/.*cic=\([A-Za-z0-9-]*\).*/\1/p' | head -1)"
  if [ -n "$DETECTED" ] && [ "$DETECTED" != "$ROM_CIC" ]; then
    die "CIC MISMATCH: probe_rom.py said $ROM_CIC, n64lle's detector says $DETECTED.
   game.toml carries the probe's value as [MEASURED] and is now WRONG.
   Fix probe_rom.py's table before trusting this project."
  fi
  say "  agreed: $ROM_CIC"
  [ -n "$PROBE_LINE" ] && say "  contract: $PROBE_LINE"

  step "Gates"
  ( cd "$ROOT/build" && ctest --output-on-failure ) || \
    say "  NOTE some gates did not pass. That is information about this title,
       not necessarily a scaffolding failure — read the output before assuming."
fi

# ---------------------------------------------------------------------------
# 8. push, last
# ---------------------------------------------------------------------------
if [ "$DO_GH" -eq 1 ] && [ "$DO_GIT" -eq 1 ]; then
  step "Pushing"
  git -C "$ROOT" add -A
  git -C "$ROOT" diff --cached --quiet || git -C "$ROOT" commit -q -m "Generated tree from the first pipeline run"
  git -C "$ROOT" push -u origin HEAD
fi

step "Done: $ROOT"
cat <<DONE

Next, in that directory:

  tools/build_framework.sh Release        # if you skipped --generate
                                          #   (a shim onto the shared
                                          #    n64lle/tools/build_framework.sh)
  cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
  cmake --build build -j
  ctest --test-dir build --output-on-failure
  ./build/$EXE                            # the launcher

Then read docs/STATUS.md — it is empty of measurements on purpose, and filling
it in with your own is the first real task. The run report's dispatch-miss line
is where the family says to start.
DONE
