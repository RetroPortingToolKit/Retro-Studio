# New project toolkit

Scaffold a new n64lle per-game repo end to end: probe the cartridge, lay out the
repo, wire the submodules, write the contract, generate C, build, and run the
gates.

```sh
sh tools/new_project/setup_project.sh "/roms/Mario Kart 64 (USA).z64"
```

```powershell
powershell -File tools\new_project\setup_project.ps1 -Rom C:\roms\game.z64 -Dir C:\src
```

On a terminal the ROM is the only argument you need — and with none at all it is
the first question. Everything else is asked, with the **probed** ROM identity
supplying the defaults. Flags are for scripting: anything passed explicitly
skips its question, and `--yes` (or a non-TTY run) takes every default without
asking, with the build- and network-touching steps off unless flagged.

`sh setup_project.sh --help` lists every flag. The PowerShell entry point is a
thin launcher: it finds Git for Windows' bash and runs the same script with the
same arguments, so there is one scaffolder to fix.

## What it asks

| Question | Default |
|---|---|
| Path to your dump | asked first when no ROM was given |
| Display name | the dump filename with its `(Region)` / `[tag]` parts stripped; the cartridge header when that leaves nothing |
| Target prefix (slug) | the name, lowercased and stripped to `[a-z0-9]` |
| Repo / CMake project name | `<Name>Recomp` |
| Executable name | the slug |
| Controller ports to declare (1-4) | 1 |
| Harvest window, in frames | 900 |
| Harvest step cap, in millions | 3000 |
| Where to create it | beside this n64lle checkout |
| Initialize a git repo and commit? | yes |
| Build the framework and generate C now? | yes on a terminal; off when non-interactive |
| Create a GitHub repo with `gh` and push? | **no** |

Nothing assumes a GitHub repository exists. Declining that question skips
creation, the remote and the push; the project is still a complete local git
repository.

## What it produces

```text
<Title>Recomp/
├── CMakeLists.txt          harvest -> emit -> compile, the gates, and ONE
│                           n64lle_add_runtime_target() call
├── game.toml               the contract: ROM identity [MEASURED], runtime and
│                           controller declarations the host reads at startup
├── VERSION
├── CLAUDE.md               repo rules; delegates, never restates
├── README.md
├── docs/STATUS.md          the honesty ledger — empty of measurements on purpose
├── tools/build_framework.sh   # shim onto n64lle/tools/build_framework.sh
├── roms/README.md          the identity to check a dump against
├── generated/README.md     ROM-derived, gitignored, never hand-edited
├── n64lle/                 framework submodule
└── recomp-ui/              launcher submodule
```

**There is no `host/` directory, and that is the point.** The executable comes
from one call to `n64lle_add_runtime_target()` (`runtime/runtime.cmake`), so a
fix to the launcher, the input layer, the audio drain or the run loop reaches
every port on a submodule bump. The first N64 port carried ~1,500 lines of host
C, about 1,300 of which had nothing to do with that game; a scaffolder that
stamped copies of it would have made that permanent.

## Pieces

| File | Role |
|------|------|
| `setup_project.sh` | the end-to-end driver |
| `probe_rom.py` | cartridge identity: header, region, CRCs, digests, and the CIC resolved from the IPL2 checksum |
| `fill_tokens.py` | `@TOKEN@` substitution; an unresolved token is an error, not a blank |
| `templates/` | everything written into the new repo |

`probe_rom.py` is usable on its own:

```sh
python3 probe_rom.py "game.z64"          # human-readable
python3 probe_rom.py "game.z64" --json   # machine-readable
```

## Two places this deliberately distrusts itself

**The CIC.** `probe_rom.py` re-implements the IPL2 checksum in Python, because
scaffolding runs before the framework is built — naming the project is the first
thing that happens. A second implementation can drift from the first, and a
wrong CIC would sit in `game.toml` under a `[MEASURED]` tag, which is worse than
no tag at all. So the `--generate` step cross-checks the probed value against
n64lle's own detector and **fails loudly** if they disagree. (They agree on
Glover, whose contract was established independently before this tool existed:
every field matches.)

**Unresolved tokens.** `fill_tokens.py` refuses to write a template that still
has an `@TOKEN@` in it. A scaffolder that quietly emits `@SHA256@` produces a
project whose ROM verification can never succeed, and the failure surfaces much
later — in the launcher, as "not verified", with no clue why.

## What the generated STATUS.md says

Almost nothing, and on purpose. A freshly cut project has **measured nothing**,
so its status document records the ROM identity (which really was measured, with
the date) and then a list of what has not been established yet. Placeholders
that read like results are worse than blanks — they get quoted.

The one thing it does assert beyond identity is whether the title's CIC walks
into n64lle's KI-1: a **CIC-6105** cartridge renders black forever today,
because the HLE boot zero-fills RSP IMEM and 6105's IPL3 XOR-decrypts IMEM in
place. Knowing that up front is the difference between a day of confused
debugging and a known issue with a name.
