"""Local CMake configure / build / launch for game repos.

Cross-platform (Windows / macOS / Linux). No force flags; builds stay under
the chosen build directory (default ``build-release``).

Before configure, missing OpenBIOS generated C under ``psxrecomp/generated/``
is regenerated (MIT OpenBIOS — no retail dump required) so runtime.cmake can
link a BIOS backend.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import platforms
from . import n64_paths as _n64_paths, snes_paths as _snes_paths
from .gitops import CmdResult
from .paths import find_bash

DEFAULT_BUILD_DIR = "build-release"
DEFAULT_TARGET = "psx-runtime"
DEFAULT_BUILD_TYPE = "Release"
OPENBIOS_PROFILE = "bios/OpenBIOS.toml"
OPENBIOS_STEM = "OpenBIOS"
SCPH1001_PROFILE = "bios/SCPH1001.toml"
SCPH1001_STEM = "SCPH1001"
LogFn = Callable[[str], None]


@dataclass
class BuildHost:
    system: str  # Windows | Darwin | Linux | …
    label: str  # windows | macos | linux | other
    cmake: str | None
    ninja: str | None
    jobs: int


@dataclass
class LaunchHandle:
    proc: subprocess.Popen
    exe: Path
    cwd: Path
    env_overlay: dict[str, str] = field(default_factory=dict)

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def poll(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


_active_launch: LaunchHandle | None = None
_launch_lock = threading.Lock()


def _launch_pid_path() -> Path:
    """Cross-process launch PID file (Studio Stop vs blocked ``build run``)."""
    try:
        from .retcomm_paths import default_paths

        base = default_paths().cache_dir
    except Exception:
        base = Path.home() / ".local" / "share" / "retcomm" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    return base / "studio-launch.pid"


def _write_launch_pid(pid: int, *, exe: Path, root: Path) -> None:
    path = _launch_pid_path()
    path.write_text(
        f"pid={pid}\nexe={exe}\nroot={root}\n",
        encoding="utf-8",
    )


def _read_launch_pid() -> tuple[int, str, str] | None:
    path = _launch_pid_path()
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pid = 0
    exe = ""
    root = ""
    for line in text.splitlines():
        if line.startswith("pid="):
            try:
                pid = int(line[4:].strip())
            except ValueError:
                pid = 0
        elif line.startswith("exe="):
            exe = line[4:].strip()
        elif line.startswith("root="):
            root = line[5:].strip()
    if pid <= 0:
        return None
    return pid, exe, root


def _clear_launch_pid() -> None:
    try:
        _launch_pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """Is `pid` still running? Asks; never signals.

    ``os.kill(pid, 0)`` is the POSIX idiom and is NOT portable: on Windows
    CPython implements os.kill as OpenProcess + TerminateProcess(handle, sig),
    so signal 0 does not probe the process — it KILLS it, with exit code 0.
    Every caller here is a liveness question asked on the way to something
    else (Launch's "already running?" guard, Stop's cross-process lookup,
    ``build status``), so on Windows the probe itself was terminating the
    running game — or, after PID reuse, an unrelated process. Ask the kernel
    instead, by opening a handle and asking whether it has been signalled.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        ERROR_INVALID_PARAMETER = 87  # no such pid (as opposed to "not yours")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            # No handle is two different answers. ERROR_INVALID_PARAMETER means
            # there is no such process; anything else (access denied) means one
            # exists that is not ours to open — which is the PermissionError
            # arm below, and must not read as "dead".
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
        try:
            # A process handle is signalled when the process EXITS, so a wait
            # that times out immediately is a process still running. Preferred
            # over GetExitCodeProcess because STILL_ACTIVE is 259 and a game
            # that genuinely exits 259 would otherwise read as alive forever.
            return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kill_launch_pid(pid: int) -> None:
    """Terminate a launched game (process group when possible)."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        # Best-effort tree kill; fall back to terminate.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
            return
        except OSError:
            pass
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        return
    try:
        os.killpg(pid, 15)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    # Brief wait then escalate.
    for _ in range(20):
        if not _pid_alive(pid):
            return
        threading.Event().wait(0.1)
    try:
        os.killpg(pid, 9)
    except OSError:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _flush_log(log: LogFn | None, msg: str) -> None:
    if not log:
        return
    try:
        log(msg)
    except TypeError:
        # Plain print works; callers should prefer flush wrappers.
        print(msg, flush=True)


def detect_host() -> BuildHost:
    system = platform.system()
    if system == "Windows":
        label = "windows"
    elif system == "Darwin":
        label = "macos"
    elif system == "Linux":
        label = "linux"
    else:
        label = "other"
    jobs = os.cpu_count() or 4
    return BuildHost(
        system=system,
        label=label,
        cmake=shutil.which("cmake"),
        ninja=shutil.which("ninja") or shutil.which("ninja-build"),
        jobs=jobs,
    )


# --- retcomm toolchain packs -------------------------------------------------
#
# The cmake-clang-v1 pack ships its own clang, its own sysroot, and its own
# pinned dependencies (SDL3, zlib) under ``<pack>/deps``. The pack's clang.cfg
# passes ``--sysroot=<pack>/sysroot``, so the host distribution's /usr/include
# is NOT on the compiler's search path. A find_package() that resolves to the
# host's SDL3 therefore configures cleanly and then fails every compile with
# "'SDL3/SDL.h' file not found" — the header is real, the compiler just cannot
# see it. The pack's env.sh exports SDL3_DIR / ZLIB_ROOT to prevent that, but
# Studio invokes cmake directly and cannot assume env.sh was sourced, so it
# supplies the same contract itself.

_TOOLCHAIN_MARKER = "retcomm-toolchain.json"


def _toolchain_root_from(path: str | Path | None) -> Path | None:
    """Walk up from a file inside a toolchain pack to the pack root."""
    if not path:
        return None
    try:
        cur = Path(path).expanduser().resolve()
    except OSError:
        return None
    for cand in (cur, *cur.parents):
        if (cand / _TOOLCHAIN_MARKER).is_file():
            return cand
    return None


def toolchain_root(host: BuildHost | None = None) -> Path | None:
    """Active retcomm toolchain pack, or None when building with host tools.

    Only a pack that owns the running interpreter or the cmake we are about to
    invoke counts — that is the pack whose sysroot the compile will use. An
    installed-but-unused pack is deliberately ignored: its dependencies are
    built against its own sysroot and would be the wrong answer for a build
    driven by host clang.
    """
    env_dir = (os.environ.get("RETCOMM_TOOLCHAIN_DIR") or "").strip()
    if env_dir:
        cand = Path(env_dir).expanduser()
        if (cand / _TOOLCHAIN_MARKER).is_file():
            return cand.resolve()
    host = host or detect_host()
    for probe in (sys.executable, host.cmake):
        found = _toolchain_root_from(probe)
        if found is not None:
            return found
    return None


def toolchain_env(host: BuildHost | None = None) -> dict[str, str]:
    """Environment overlay pointing find_package() at the pack's deps.

    Mirrors ``<pack>/env.sh``. Empty when no pack drives the build. These go
    in the environment rather than on the command line because cmake warns
    about ``-D`` variables a project never reads, and a project that links
    SDL3 but not zlib would otherwise warn on every configure.
    """
    pack = toolchain_root(host)
    if pack is None:
        return {}
    deps = pack / "deps"
    if not deps.is_dir():
        return {}
    overlay: dict[str, str] = {}
    sdl3_cfg = deps / "lib" / "cmake" / "SDL3"
    if (sdl3_cfg / "SDL3Config.cmake").is_file() or (
        sdl3_cfg / "SDL3-config.cmake"
    ).is_file():
        overlay["SDL3_DIR"] = str(sdl3_cfg)
    if (deps / "include" / "zlib.h").is_file():
        overlay["ZLIB_ROOT"] = str(deps)
    prior = (os.environ.get("CMAKE_PREFIX_PATH") or "").strip()
    overlay["CMAKE_PREFIX_PATH"] = (
        f"{deps}{os.pathsep}{prior}" if prior else str(deps)
    )
    return overlay


def toolchain_cache_repairs(
    build_dir: Path,
    extra_args: list[str] | None = None,
    host: BuildHost | None = None,
) -> list[str]:
    """``-D`` pins that re-point a build tree already cached to host deps.

    The environment overlay is only a *hint*: find_package() prefers an
    existing ``<pkg>_DIR`` cache entry, so a tree configured before this fix
    keeps resolving to the host's SDL3 and keeps failing to compile. Override
    those entries explicitly. Only entries that are present and point outside
    the pack are touched, so this never introduces an unused-variable warning:
    the entry exists precisely because the project read it.
    """
    pack = toolchain_root(host)
    if pack is None:
        return []
    already = _explicit_cache_vars(extra_args)
    repairs: list[str] = []
    for name, want in toolchain_env(host).items():
        if name == "CMAKE_PREFIX_PATH" or name in already:
            continue
        have = cache_entry(build_dir, name)
        if have and Path(have) != Path(want):
            repairs.append(f"-D{name}:PATH={want}")
    return repairs


def _explicit_cache_vars(extra_args: list[str] | None) -> set[str]:
    """Names of ``-DVAR[:TYPE]=…`` entries a caller already passed."""
    names: set[str] = set()
    for arg in extra_args or []:
        m = re.match(r"-D([A-Za-z0-9_]+)(?::[A-Za-z]+)?=", arg)
        if m:
            names.add(m.group(1))
    return names


def merged_env(overlay: dict[str, str]) -> dict[str, str] | None:
    """os.environ plus ``overlay``, or None when there is nothing to add."""
    if not overlay:
        return None
    env = os.environ.copy()
    env.update(overlay)
    return env


def default_generator(host: BuildHost | None = None) -> str:
    host = host or detect_host()
    if host.ninja:
        return "Ninja"
    if host.label == "windows":
        # Leave empty → cmake picks VS / default generator.
        return ""
    return "Unix Makefiles"


def cache_entry(build_dir: Path, name: str) -> str:
    """Value of ``name`` in an existing CMakeCache.txt, or empty.

    Matches any cache type, so ``SDL3_DIR`` (PATH) and ``CMAKE_GENERATOR``
    (INTERNAL) read the same way.
    """
    cache = Path(build_dir) / "CMakeCache.txt"
    if not cache.is_file():
        return ""
    try:
        text = cache.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    prefix = f"{name}:"
    for line in text.splitlines():
        if line.startswith(prefix) and "=" in line:
            head, val = line.split("=", 1)
            if head.split(":", 1)[0] == name:
                return val.strip()
    return ""


def cached_cmake_generator(build_dir: Path) -> str:
    """CMAKE_GENERATOR from an existing CMakeCache.txt, or empty."""
    return cache_entry(build_dir, "CMAKE_GENERATOR")


def normalize_generator_request(generator: str | None) -> str | None:
    """None / empty / 'auto' → Auto. Otherwise the cmake -G name."""
    if generator is None:
        return None
    g = generator.strip()
    if not g or g.lower() == "auto":
        return None
    return g


def resolve_configure_generator(
    host: BuildHost,
    build_dir: Path,
    generator: str | None,
) -> str:
    """Explicit -G, else the cache's generator, else host default."""
    requested = normalize_generator_request(generator)
    if requested is not None:
        return requested
    cached = cached_cmake_generator(build_dir)
    if cached:
        return cached
    return default_generator(host)


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VAL`` pairs from free text (space / newline / ``;`` separated).

    Values may be quoted with single or double quotes. Lines starting with ``#``
    are ignored.
    """
    env: dict[str, str] = {}
    if not text or not text.strip():
        return env
    # Normalize separators to newlines, but keep quoted spans intact via a
    # simple token walk on KEY=VAL forms.
    cleaned: list[str] = []
    for raw_line in text.replace(";", "\n").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned.append(line)
    blob = "\n".join(cleaned)
    # Match KEY=VALUE where VALUE is "…", '…', or non-space / until next KEY=
    pattern = re.compile(
        r"""(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<val>"[^"]*"|'[^']*'|\S+)"""
    )
    for m in pattern.finditer(blob):
        key = m.group("key")
        val = m.group("val")
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        env[key] = val
    return env


def _run_stream(
    cmd: list[str],
    cwd: Path,
    *,
    log: LogFn | None = None,
    env: dict[str, str] | None = None,
) -> CmdResult:
    if log:
        log("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )
    except OSError as exc:
        return CmdResult(False, f"Failed to start: {cmd[0]}", str(exc))

    assert proc.stdout is not None
    lines: list[str] = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        if log:
            log(line)
    code = proc.wait()
    detail = "\n".join(lines[-40:])
    if code != 0:
        return CmdResult(False, f"Command failed (exit {code})", detail)
    return CmdResult(True, "OK", detail)


def resolve_framework_root(root: Path) -> Path | None:
    """Return the psxrecomp framework root (bios/ + recompiler/), or None."""
    root = root.expanduser().resolve()
    for cand in (root / "psxrecomp", root):
        if (cand / "bios" / "OpenBIOS.toml").is_file() and (cand / "recompiler").is_dir():
            return cand
    return None


_PROJECT_RE = re.compile(r"^\s*project\s*\(\s*([A-Za-z0-9_.+-]+)", re.MULTILINE)


# `n64lle_add_runtime_target(glover-runtime` — the one call an n64lle port makes
# to build its executable. Its argument is the target name, and there is no
# other place that carries it: the project() name is GloverRecomp, the OUTPUT
# NAME is glover, and the target is glover-runtime. All three differ.
_N64_RUNTIME_TARGET_RE = re.compile(
    r"^\s*n64lle_add_runtime_target\s*\(\s*([A-Za-z0-9_.+-]+)", re.MULTILINE
)


def default_target(root: Path) -> str:
    """The CMake target to build for this platform's projects.

    Three different rules, because the three scaffolds genuinely differ:

    * PSX ports all build one shared runtime target (``psx-runtime``).
    * SNES ports name their executable after the project, so the target is read
      out of the repo's own ``project()`` call.
    * N64 ports name it after the SLUG, which appears in neither — the project
      is ``GloverRecomp`` and the executable is ``glover``, but the target is
      ``glover-runtime``. It is read from the ``n64lle_add_runtime_target()``
      call that defines it, which is the only place it is written down.

    This is the PRODUCT's target: what the executable is called, and what
    n64_project_slug() strips to recover the port's target prefix. It is NOT
    necessarily what a Build should build — see default_build_target().
    """
    profile = platforms.current()
    if profile.default_target:
        return profile.default_target
    cml = Path(root).expanduser().resolve() / "CMakeLists.txt"
    text = ""
    if cml.is_file():
        try:
            text = cml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if profile.key == "n64":
        m = _N64_RUNTIME_TARGET_RE.search(text)
        if m:
            return m.group(1)
        # No runtime target: either <SLUG>_BUILD_UI is off or this is not a
        # scaffolded port. `all` still builds the gates, which is the useful
        # answer rather than a guessed target name.
        return "all"
    if text:
        m = _PROJECT_RE.search(text)
        if m:
            return m.group(1)
    # `all` builds everything the project defines — correct, if not minimal.
    return "all"


def default_build_target(root: Path) -> str:
    """What an unqualified Build should build — not always the product.

    On N64 it is ``all``. The product target is ``<slug>-runtime``, but a port
    also declares ``<slug>-cosim`` and ``<slug>-bench``, and those are the
    executables its gates run. MEASURED 2026-09-12 on PokemonStadiumRecomp:
    Studio built the runtime target, ctest then reported "6 tests failed out
    of 7" — five ***Not Run for a missing ``pokemonstadium-cosim`` and the
    sixth a contract check needing the full build's staging. Nothing was wrong
    with the port. On a console whose doctrine is gates-first, a Build that
    cannot then be tested is the wrong default.

    Cheap, too: the runtime links ``<slug>_gen``, which already depends on
    ``<slug>-generate``, so the harvest is pulled in either way. ``all`` adds
    two small drivers.

    PSX and SNES keep their product target: their gates are built by it, or
    are not executables of their own.
    """
    if platforms.current().key == "n64":
        return "all"
    return default_target(root)


def n64_project_slug(root: Path) -> str:
    """The port's target prefix, derived from its runtime target name."""
    tgt = default_target(root)
    return tgt[: -len("-runtime")] if tgt.endswith("-runtime") else ""


def n64_generate_target(root: Path) -> str:
    """The CMake target that harvests the ROM and emits C: ``<slug>-generate``."""
    slug = n64_project_slug(root)
    return f"{slug}-generate" if slug else ""


def snes_regen_script(root: Path) -> Path | None:
    """``tools/regen.sh`` — the SNES ROM → C step, owned by the project."""
    p = Path(root).expanduser().resolve() / "tools" / "regen.sh"
    return p if p.is_file() else None


# Locating the pinned framework and asking what its CLI offers live in
# snes_paths, because the migration ops need the same two answers before they
# emit a regen.sh that framework would not be able to run.
snes_framework_root = _snes_paths.regen_framework_root
snes_cli_commands = _snes_paths.cli_commands


def framework_gap_message(cli: Path, missing: list[str], have: set[str]) -> str:
    """One wording for the skew, wherever it is noticed.

    Both the Generate preflight and the migration ops that write regen.sh have
    to say this, and a user who meets it twice should not have to work out that
    it is the same problem.
    """
    return (
        f"The snesrecomp this port is pinned to is older than its own "
        f"tools/regen.sh: {cli.name} has no "
        + ", ".join(repr(c) for c in missing)
        + f" (it offers {', '.join(sorted(have)) or 'nothing'}). Update the "
        "snesrecomp submodule to a revision that has "
        + ("it" if len(missing) == 1 else "them")
        + ", or re-emit regen.sh from the framework you are actually pinned to."
    )


def preflight_snes_generate(
    root: Path,
    *,
    verify: bool = True,
    rom: str = "",
    cfg_roots: bool = False,
) -> CmdResult | None:
    """Reasons regen.sh cannot succeed, found before it is run.

    Every one of these otherwise surfaces as somebody else's error text — an
    argparse "invalid choice", an empty digest silently accepted — attributed
    to Studio's Generate button. Naming the actual mismatch, and what to do
    about it, is the whole point.
    """
    root = Path(root).expanduser().resolve()
    script = snes_regen_script(root)
    if script is None:
        return None  # generate_snes_c reports the missing script itself.
    try:
        regen_text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        regen_text = ""

    fw = snes_framework_root(root)
    cli = fw / "snesrecomp_cli.py"
    if not cli.is_file():
        return CmdResult(
            False,
            f"{cli} is missing — the snesrecomp checkout regen.sh needs is not "
            "there. Run: git submodule update --init --recursive snesrecomp",
        )

    # tools/regen.sh belongs to the PORT, and not every port owns the wizard's
    # copy. MegaManXSNESRecomp hand-wrote a multi-variant driver that selects a
    # region positionally and stages each variant's ROM at a fixed path, so the
    # `--rom <path>` Studio adds is `unknown argument: --rom` to it — relayed
    # as a Generate failure, which is precisely what this preflight exists to
    # stop. Ask the script what it accepts before handing it a flag.
    want: list[str] = []
    if rom:
        want.append("--rom")
    if not verify:
        want.append("--no-verify")
    if cfg_roots:
        want.append("--cfg-roots")
    if want and regen_text:
        accepts = _snes_paths.regen_options(regen_text)
        unknown = [flag for flag in want if flag not in accepts]
        if unknown:
            listed = ", ".join(sorted(accepts)) or "no options at all"
            # What to do next depends on whether this port CAN adopt the
            # framework's script. Naming a Migrate step unconditionally sent
            # people looking for a checkbox the audit had deliberately not
            # offered, so the advice comes from the same report the Migrate
            # tab renders.
            try:
                from .snesops import regen_ownership_guidance

                advice = regen_ownership_guidance(root)
            except ImportError:  # pragma: no cover
                advice = ""
            return CmdResult(
                False,
                f"{script} does not accept {', '.join(unknown)} — this port "
                f"owns its regen.sh, and it accepts: {listed}."
                + (f" {advice}" if advice else ""),
            )

    # The skew that produced "invalid choice: 'verify-rom'": a regen.sh emitted
    # from a newer wizard than the framework the port is pinned to. Read out of
    # the script's own call sites rather than a list kept here, which would go
    # stale the next time regen.sh grows a step.
    gap = _snes_paths.regen_framework_gap(root, regen_text, verify=verify)
    if gap is not None:
        missing, have = gap
        return CmdResult(False, framework_gap_message(cli, missing, have))

    # regen.sh reads its digests out of rom_identity.txt on current wizards. An
    # absent or empty file makes --verify a check against nothing, which is the
    # failure mode the digests exist to prevent.
    if verify and "rom_identity.txt" in regen_text:
        from .snesops import parse_identity_file

        ident = root / "rom_identity.txt"
        data = parse_identity_file(ident) if ident.is_file() else {}
        if not (data.get("expected_crc32") and data.get("expected_sha256")):
            why = "is missing" if not ident.is_file() else "carries no digests"
            return CmdResult(
                False,
                f"tools/regen.sh reads its ROM digests from rom_identity.txt, "
                f"which {why}, so --verify would check the ROM against nothing. "
                "Run Migrate with the ROM path to write it (Probe ROM), or "
                "generate with verification off if you accept that the C may "
                "not match what this port was pinned against.",
            )
    return None


_GENERATE_HINTS: tuple[tuple[str, str], ...] = (
    ("invalid choice: 'verify-rom'",
     "The pinned snesrecomp predates `verify-rom`. Update the snesrecomp "
     "submodule, or re-emit tools/regen.sh from the framework this port is "
     "actually pinned to (Migrate → Emit tools/regen.sh)."),
    ("invalid choice: 'generate'",
     "The pinned snesrecomp predates the standalone `generate` command — it "
     "only scaffolds via `build`. Update the snesrecomp submodule."),
    ("missing — run: git submodule update",
     "The snesrecomp submodule is not checked out. Run: "
     "git submodule update --init --recursive"),
    ("no ROM found",
     "Point Generate at the ROM (the image field), or set SNESRECOMP_ROM."),
)


def diagnose_generate_failure(detail: str, root: Path) -> str | None:
    """Extra hint appended to a failed SNES generate message."""
    blob = detail or ""
    for needle, hint in _GENERATE_HINTS:
        if needle in blob:
            return hint
    return None


def generate_snes_c(
    root: Path,
    *,
    rom: str = "",
    cfg_roots: bool = False,
    verify: bool = True,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Run the project's own tools/regen.sh.

    Studio deliberately does not reimplement generation: regen.sh carries the
    ROM digests this port was pinned against and verifies them before emitting
    anything. Calling snesrecomp_cli directly would skip that check, which is
    the one thing standing between a mismatched dump and hours of chasing
    divergence that was never in the recompiler.
    """
    root = Path(root).expanduser().resolve()
    script = snes_regen_script(root)
    if script is None:
        return CmdResult(
            False,
            f"No tools/regen.sh in {root} — run Migrate → Emit tools/regen.sh first",
        )
    # Preflight before the ROM path is even resolved: a framework that cannot
    # run this regen.sh will not start running it correctly once a ROM is named.
    pre = preflight_snes_generate(
        root, verify=verify, rom=rom, cfg_roots=cfg_roots)
    if pre is not None:
        if log:
            log(pre.message)
        return pre
    shell = find_bash()
    if shell is None:
        return CmdResult(
            False,
            "No POSIX shell found to run tools/regen.sh. On Windows install "
            "Git for Windows (it ships bash.exe); on Linux/macOS put bash on PATH.",
        )
    cmd = [shell, str(script)]
    if rom:
        rom_p = Path(rom).expanduser()
        if not rom_p.is_file():
            return CmdResult(False, f"ROM not found: {rom}")
        cmd.extend(["--rom", str(rom_p.resolve())])
    if not verify:
        cmd.append("--no-verify")
    if cfg_roots:
        cmd.append("--cfg-roots")
    if dry_run:
        msg = "dry-run: " + " ".join(cmd)
        if log:
            log(msg)
        return CmdResult(True, msg)
    r = _run_stream(cmd, root, log=log)
    if r.ok:
        gen = root / "src" / "gen"
        n = len(list(gen.glob("*.c"))) if gen.is_dir() else 0
        return CmdResult(True, f"Generated {n} C file(s) into src/gen", r.detail)
    # Anything the preflight could not foresee still gets read rather than
    # passed through as somebody else's stack trace.
    hint = diagnose_generate_failure(r.detail or "", root)
    if hint:
        return CmdResult(False, f"{r.message} — {hint}", r.detail)
    return r


# ---------------------------------------------------------------------------
# N64: generation is a target in the port's OWN CMake graph
# ---------------------------------------------------------------------------
# There is no regen.sh here and no framework CLI to call. A port's CMakeLists
# declares two custom commands — n64lle-harvest (execution-derived discovery)
# then n64emit --image — behind one target, <slug>-generate. So "generate" is
# `cmake --build . --target <slug>-generate`, and the thing that has to be true
# first is not a CLI vocabulary but a BUILT FRAMEWORK: the port includes
# n64lle/runtime/runtime.cmake and calls n64lle_runtime_resolve_framework(),
# which looks for already-built libraries and tools under build-n64lle/.
#
# That prerequisite is the whole reason this preflight exists. Configure with
# it missing and CMake dies inside a resolve function, several files away from
# the step that was actually skipped.


SNES_RUNNER_PATH_TOOL = "tools/check_runner_paths.py"


def _tool_accepts(tool: Path, flag: str) -> bool:
    """Does this copy of `tool` accept `flag`?

    Asked of the tool's own --help rather than grepped out of its source, and
    for the same reason snes_paths.cli_commands() does it: the question is what
    argparse will accept, and argparse is the only thing that knows. A port is
    pinned to a framework revision, so Studio is routinely newer than the tool
    it drives — sending a flag the pinned copy has never heard of turns a clean
    audit into "unrecognized arguments".
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(tool), "--help"],
            capture_output=True, text=True, timeout=60, cwd=str(tool.parent),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return flag in ((proc.stdout or "") + (proc.stderr or ""))


def snes_runner_path_tool(root: Path) -> Path:
    """The framework's runner/src path auditor, in the pinned snesrecomp.

    Deliberately read out of the framework rather than vendored into Studio:
    the tool's whole premise is that the layout on disk is the ground truth,
    and the layout that matters is the one the port is pinned to. A vendored
    copy would audit against whatever snesrecomp Studio shipped with.
    """
    return snes_framework_root(root) / SNES_RUNNER_PATH_TOOL


def check_snes_runner_paths(
    root: Path,
    *,
    fix: bool = False,
    include_docs: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Audit (``--fix``: repair) runner/src references in framework and port.

    snesrecomp's runner/src is organised into layer folders, and moving a file
    between them is a rename with no content change: nothing objects until a
    port configures and cmake reports "Cannot find source file" -- one file per
    target, in the GAME's repo, for a defect that lives in the framework. That
    is how one reorganisation reached three ports separately.

    Both halves are checked, in the order that makes the log readable:

    1. The framework itself. This is where the defect almost always is, and a
       stale framework breaks every port pinned to it, not just this one.
    2. The port. A game that names a runner source directly -- a test target
       compiling snes_overlay_draw.c, a script quoting a path -- carries its
       own copy of the same breakage.

    A pin older than the tool is reported as a pin problem, not as a pass: the
    question could not be put, and a caller may not read its own inability to
    ask as a clean bill of health.
    """
    root = Path(root).expanduser().resolve()
    framework = snes_framework_root(root)
    tool = framework / SNES_RUNNER_PATH_TOOL
    if not tool.is_file():
        if not framework.is_dir():
            return CmdResult(
                False,
                f"No snesrecomp checkout at {framework} — the submodule is not "
                "initialised, so there is no runner/src to audit against.",
            )
        return CmdResult(
            False,
            f"The snesrecomp this port is pinned to has no {SNES_RUNNER_PATH_TOOL}. "
            "It landed with the runner/src layer-folder guard; a pin older than "
            "that cannot be audited from here. Update the snesrecomp submodule, "
            "or run the check from a checkout that has the tool with "
            "--runner-src pointed at this one.",
        )

    def _run(label: str, target: Path, aim: bool) -> CmdResult:
        cmd = [sys.executable, str(tool), "--repo", str(target)]
        if aim:
            cmd += ["--runner-src", str(framework)]
        if fix:
            cmd.append("--fix")
        if include_docs:
            cmd.append("--include-docs")
        if log:
            log(f"--- {label}: {target}")
        return _run_stream(cmd, framework, log=log)

    # The framework audits itself: --repo <framework> finds <framework>/runner/src
    # natively, so this half never needs the flag and never meets the skew below.
    fw_res = _run("framework", framework, aim=False)

    # The port half only needs aiming when the framework is NOT at
    # <root>/snesrecomp — a port built against SNESRECOMP_ROOT, or a worktree.
    # Everywhere else the tool's own rule resolves to the same directory, so
    # the flag would be a requirement bought for nothing.
    needs_aim = framework != (root / "snesrecomp")
    if needs_aim and not _tool_accepts(tool, "--runner-src"):
        # Same shape as framework_gap_message(): the port is pinned to a
        # framework older than the flag this needs. Say which half could not be
        # asked rather than passing the port silently.
        port_res = CmdResult(
            False,
            f"This port builds against {framework}, not ./snesrecomp, and the "
            f"pinned {SNES_RUNNER_PATH_TOOL} has no --runner-src to aim it "
            "there. Update the snesrecomp submodule to a revision that has the "
            "flag; the framework half above was still checked.",
        )
        if log:
            log("--- port: SKIPPED — " + port_res.message)
    else:
        # Checked even when the framework half failed: the two are independent,
        # and stopping at the first finding means a second run to see the rest.
        port_res = _run("port", root, aim=needs_aim)

    verb = "repaired" if fix else "checked"
    if fw_res.ok and port_res.ok:
        return CmdResult(True, f"runner/src references {verb}: framework and port OK")

    # A half that could not be ASKED is reported as that, not folded into
    # "broken": Repair cannot help with a pin that has no flag, and offering it
    # would send the user round a loop that changes nothing.
    skipped = port_res if (not port_res.ok and not port_res.detail) else None
    if skipped is not None and fw_res.ok:
        return CmdResult(False, skipped.message)

    bad = []
    parts = []
    if not fw_res.ok:
        bad.append(f"framework ({framework.name})")
        parts.append(fw_res.detail or fw_res.message)
    if not port_res.ok and skipped is None:
        bad.append("port")
        parts.append(port_res.detail or port_res.message)
    # The tool's own output carries the distinction: a repairable finding is
    # printed as "old -> new", one that is not says why (no such file under
    # runner/src, or ambiguous between two layer folders). Offering Repair for
    # a finding it cannot touch sends the user round a loop that changes
    # nothing and then reports the same thing.
    blob = "\n".join(parts)
    repairable = any(" -> " in line for line in blob.splitlines())
    if fix:
        hint = ""
    elif repairable:
        hint = " — press Repair to rewrite the repairable ones"
    else:
        hint = (" — none are auto-repairable (the files are gone, not moved); "
                "these need a human")
    msg = f"runner/src references still broken in {' and '.join(bad)}{hint}"
    if skipped is not None:
        # Both things are true and the user needs both: the framework has real
        # findings AND the port half never ran.
        msg += f"; port half skipped — {skipped.message}"
        parts.append(skipped.message)
    return CmdResult(False, msg, "\n".join(p for p in parts if p))


def _missing_summary(root: Path) -> str:
    """What resolve_framework() would name first, plus how much else is gone.

    Naming the SAME artifact cmake would is the point: a preflight that says
    "no n64emit" while cmake dies on libn64lle-runtime-devices.a reads as two
    different problems.
    """
    missing = _n64_paths.framework_missing_artifacts(root)
    if not missing:
        return "framework artifacts"
    head = f"{_n64_paths.FRAMEWORK_BUILD_DIR}/{missing[0]}"
    if len(missing) == 1:
        return head
    return f"{head} (and {len(missing) - 1} more required artifact(s))"


def _n64_framework_missing(root: Path, fw: Path) -> CmdResult:
    """The framework the port resolves to is not a checkout."""
    if fw == root / "n64lle":
        return CmdResult(
            False,
            "The n64lle submodule is not checked out — a port includes "
            "n64lle/runtime/runtime.cmake and cannot configure without it. "
            "Run: git submodule update --init --recursive n64lle",
        )
    return CmdResult(
        False,
        f"This port builds against {fw} (from "
        f"{_n64_paths.framework_root_source(root)}), which has no "
        f"{_n64_paths.MARKER}. Point N64LLE_ROOT at an n64lle checkout, or "
        "unset it to use the port's own submodule.",
    )


def preflight_n64_framework(root: Path) -> CmdResult | None:
    """Reasons an n64lle port cannot configure, found before cmake runs."""
    root = Path(root).expanduser().resolve()
    fw = _n64_paths.framework_root(root)
    if not (fw / _n64_paths.MARKER).is_file():
        return _n64_framework_missing(root, fw)
    # Before the artifact check, and even when the framework IS built: since
    # n64lle went Rust the PORT's own build runs cargo too (the host staticlib
    # and the drivers), so a built framework does not mean a buildable port.
    rust = _n64_paths.rust_toolchain_problem(fw)
    if rust is not None:
        return CmdResult(False, rust)
    if _n64_paths.framework_is_built(root):
        return None
    script = _n64_paths.framework_build_script(root)
    if script is None:
        return CmdResult(
            False,
            f"n64lle is not built ({_n64_paths.FRAMEWORK_BUILD_DIR}/ has no "
            "n64emit) and this port has no tools/build_framework.sh to build "
            "it. That script is scaffolded into every n64lle port and carries "
            "the flags the framework revision it is pinned to needs; without "
            "it, build n64lle out of tree by hand into "
            f"{_n64_paths.FRAMEWORK_BUILD_DIR}/.",
        )
    return CmdResult(
        False,
        f"n64lle is not built yet — missing {_missing_summary(root)}, so "
        "n64lle_runtime_resolve_framework() would fail inside cmake. "
        f"Run: bash {script.relative_to(root).as_posix()} Release",
    )


def _n64_rust_versions(fw: Path, *, log: LogFn | None = None) -> CmdResult:
    """``cargo --version`` and rustc's host triple, asked inside ``fw``."""
    cargo = _n64_paths.find_cargo() or "cargo"
    lines: list[str] = []
    for argv in ([cargo, "--version"], ["rustc", "-vV"]):
        try:
            proc = subprocess.run(argv, cwd=str(fw), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=1800)
        except (OSError, subprocess.SubprocessError) as exc:
            return CmdResult(False, f"could not run {argv[0]}: {exc}")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()
            return CmdResult(
                False,
                f"`{' '.join(argv)}` failed in {fw}, so the toolchain its "
                "rust-toolchain.toml pins is not usable here.",
                tail,
            )
        lines.append(proc.stdout.strip())
    host = next((ln.split(":", 1)[1].strip() for ln in lines[1].splitlines()
                 if ln.startswith("host:")), "?")
    channel = _n64_paths.rust_channel(fw)
    if log:
        log(f"rust: {lines[0]} (host {host}"
            + (f"; pinned channel {channel}" if channel else "") + ")")
        if os.name == "nt" and not host.endswith("-windows-gnu"):
            # n64lle's CMake runs cargo with no --target and links the result
            # as lib<name>.a by path, which is the GNU target's spelling. Not
            # verified on Windows from here -- said, not refused.
            log("note: n64lle links cargo's output as .a archives from a "
                "MinGW build; this rustc's host is " + host + ", not a "
                "*-windows-gnu target. Expect the link to need "
                "`rustup default stable-x86_64-pc-windows-gnu` (or the "
                "toolchain rust-toolchain.toml pins, for that target).")
    return CmdResult(True, lines[0])


def build_n64_framework(
    root: Path,
    *,
    config: str = "Release",
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Run the framework's ``n64lle/tools/build_framework.sh``, or the port's.

    A SCRIPT, never a Studio reimplementation, for the same reason Studio runs
    SNES's regen.sh rather than calling snesrecomp_cli.

    WHICH script changed on 2026-09-15. It used to be the port's own copy, on
    the reasoning that the copy carries the workarounds the framework revision
    THIS port is pinned to needs (GloverRecomp adds -frounding-math and -lm and
    documents why each belongs upstream). That reasoning holds for what a port
    ADDS and fails completely for what the framework later REQUIRES: a private
    copy cannot inherit a fix. -DN64LLE_RSP_CENSUS=1 reached one port's copy and
    no other, so seven of nine N64 ports harvested no RSP microcode and ran the
    RSP fully interpreted, with every build reporting success.

    So: prefer the framework's shared copy when the pinned n64lle has one, and
    fall back to the port's when it does not. The port's scaffolded copy is now
    a shim onto the same file, so on a current port the two are the same build
    either way -- the preference matters for a port whose private copy has
    drifted, which is exactly the case that went wrong.
    """
    root = Path(root).expanduser().resolve()
    script = _n64_paths.framework_owned_build_script(root)
    used_framework = script is not None
    if script is None:
        script = _n64_paths.framework_build_script(root)
    if script is None:
        return CmdResult(
            False,
            f"No build_framework.sh in {root} — neither n64lle/tools/ (the "
            "shared one) nor tools/ (the port's shim). This tree is missing it.",
        )
    if used_framework and _n64_paths.port_script_is_shim(root) is False:
        if log:
            log(
                "note: this port's tools/build_framework.sh is not a shim, so it "
                "carries build logic no other port can inherit. Running the "
                "shared n64lle/tools/build_framework.sh instead; fold anything "
                "that copy still needs upstream."
            )
    fw = _n64_paths.framework_root(root)
    if not (fw / _n64_paths.MARKER).is_file():
        return _n64_framework_missing(root, fw)
    rust = _n64_paths.rust_toolchain_problem(fw)
    if rust is not None:
        return CmdResult(False, rust)
    # NOT ``sh``: build_framework.sh is `#!/usr/bin/env bash`, and Windows has
    # no sh on PATH at all — the same resolution tools/regen.sh and
    # package_release.sh already go through.
    shell = find_bash()
    if shell is None:
        return CmdResult(
            False,
            "No POSIX shell found to run build_framework.sh. On Windows "
            "install Git for Windows (it ships bash.exe); on Linux/macOS put "
            "bash on PATH.",
        )
    cmd = [shell, str(script), config]
    # The shared script writes build-n64lle/ under the PORT, not under n64lle.
    env = dict(os.environ)
    env["N64LLE_PORT_ROOT"] = str(root)
    # And it builds where the port will LOOK: a tree configured with its own
    # N64LLE_BUILD (a worktree's framework, kept apart from the submodule's)
    # has to be rebuilt there, not into build-n64lle/. N64LLE_ROOT is exported
    # for the fallback case, the port's shim, which reads it; the canonical
    # script builds the tree it lives in, which framework_root() already chose.
    fw_build = _n64_paths.framework_build_dir(root)
    env["N64LLE_FRAMEWORK_BUILD_DIR"] = str(fw_build)
    env["N64LLE_ROOT"] = str(fw)
    if log and (fw != root / "n64lle" or fw_build != root / _n64_paths.FRAMEWORK_BUILD_DIR):
        log(f"n64lle: {fw} (from {_n64_paths.framework_root_source(root)}), "
            f"built into {fw_build}")
    if dry_run:
        msg = "dry-run: " + " ".join(cmd)
        if log:
            log(msg)
        return CmdResult(True, msg)
    if _n64_paths.framework_needs_cargo(fw):
        # Asked in the framework's directory, so rustup answers with the
        # toolchain rust-toolchain.toml pins (and installs it if it must)
        # before the build starts, rather than in the middle of a configure.
        v = _n64_rust_versions(fw, log=log)
        if not v.ok:
            return v
    r = _run_stream(cmd, root, log=log, env=env)
    if not r.ok:
        return r
    if not _n64_paths.framework_is_built(root):
        # Exit 0 without the artifacts is the case worth naming: it is what an
        # SDL3-absent or option-disabled configure looks like, and treating it
        # as success moves the failure to the next step.
        return CmdResult(
            False,
            f"tools/build_framework.sh reported success but "
            f"{_missing_summary(root)} is still missing — read its output "
            "before building the port.",
            r.detail,
        )
    return CmdResult(True, f"Built n64lle into {_n64_paths.FRAMEWORK_BUILD_DIR}/", r.detail)


def generate_n64_c(
    root: Path,
    *,
    rom: str = "",
    build_dir: str = DEFAULT_BUILD_DIR,
    build_type: str = DEFAULT_BUILD_TYPE,
    generator: str = "",
    ensure_framework: bool = True,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Harvest the ROM and emit C: ``cmake --build … --target <slug>-generate``.

    ``rom`` overrides the port's default dump by setting the ``<SLUG>_ROM``
    cache variable — the same knob the CMakeLists declares — rather than by
    copying a file into roms/. Studio never moves a user's dump to make a build
    work.
    """
    root = Path(root).expanduser().resolve()
    target = n64_generate_target(root)
    if not target:
        return CmdResult(
            False,
            f"No n64lle_add_runtime_target() in {root}/CMakeLists.txt, so the "
            "generate target's name cannot be read. Is this an n64lle port?",
        )
    if ensure_framework:
        pre = preflight_n64_framework(root)
        if pre is not None:
            if log:
                log(pre.message)
            return pre

    defines: list[str] = []
    if rom:
        rom_p = Path(rom).expanduser()
        if not rom_p.is_file():
            return CmdResult(False, f"ROM not found: {rom}")
        slug = n64_project_slug(root)
        defines.append(f"-D{slug.upper()}_ROM={rom_p.resolve()}")

    cfg = configure(
        root,
        build_dir=build_dir,
        build_type=build_type,
        generator=generator,
        extra_args=defines,
        ensure_bios=False,
        dry_run=dry_run,
        log=log,
    )
    if not cfg.ok:
        return cfg
    if dry_run:
        # build() refuses a missing build dir before it looks at dry_run, and
        # on a dry run the configure above did not create one. Reporting that
        # as "Configure first" would be a lie about a run that never happened.
        msg = f"dry-run: cmake --build {build_dir} --target {target}"
        if log:
            log(msg)
        return CmdResult(True, msg, cfg.detail)
    r = build(
        root,
        build_dir=build_dir,
        target=target,
        dry_run=dry_run,
        log=log,
    )
    if not r.ok:
        return r
    gen = root / "generated"
    n = len(list(gen.glob("*.c"))) if gen.is_dir() else 0
    return CmdResult(True, f"Generated {n} C file(s) into generated/", r.detail)


_MAX_PLAYERS_CMAKE_RE = re.compile(
    r"^\s*MAX_PLAYERS\s+(\d+)\s*$", re.MULTILINE | re.IGNORECASE
)
_MAX_PLAYERS_RANGE_RE = re.compile(
    r"MAX_PLAYERS must be in\s+(\d+)\.\.(\d+)", re.IGNORECASE
)


def project_max_players(root: Path) -> int | None:
    """``MAX_PLAYERS N`` from the game ``CMakeLists.txt``, if present."""
    cmake = root / "CMakeLists.txt"
    if not cmake.is_file():
        return None
    try:
        text = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _MAX_PLAYERS_CMAKE_RE.search(text)
    return int(m.group(1)) if m else None


def framework_max_players_range(fw: Path) -> tuple[int, int] | None:
    """``(lo, hi)`` from runtime.cmake's FATAL_ERROR range check, if found."""
    cmake = fw / "runtime" / "runtime.cmake"
    if not cmake.is_file():
        return None
    try:
        text = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _MAX_PLAYERS_RANGE_RE.search(text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def preflight_max_players(root: Path) -> CmdResult | None:
    """Fail fast when the game asks for MAX_PLAYERS the nested framework rejects.

    Single-player titles (Ape Escape, Tomba, …) use ``MAX_PLAYERS 1``. Older
    psxrecomp pins only allowed 2..5 and abort configure with a cryptic
    FATAL_ERROR. Detect that before running cmake.
    """
    players = project_max_players(root)
    if players is None:
        return None
    fw = resolve_framework_root(root)
    if fw is None:
        return None
    rng = framework_max_players_range(fw)
    if rng is None:
        return None
    lo, hi = rng
    if lo <= players <= hi:
        return None
    pins = root / "framework_pins.txt"
    pin_hint = ""
    if pins.is_file():
        pin_hint = f" Check {pins.name} vs the checked-out psxrecomp commit."
    return CmdResult(
        False,
        f"MAX_PLAYERS {players} is outside nested psxrecomp range {lo}..{hi}. "
        f"Update the psxrecomp submodule to a pin that allows 1..8 "
        f"(runtime.cmake after single-player / rewind support).{pin_hint}",
    )


def preflight_snes_mod_catalog(root: Path) -> CmdResult | None:
    """The mod-catalog guard, asked before a configure is spent discovering it.

    snesrecomp aborts configure with a FATAL_ERROR when a repo ships
    mods/preloaded/packages that no target declared, and Studio's own audit
    already knows: it is a REQUIRED failure carrying the fix op's id. Letting
    cmake find it first means the user reads a 25-line CMake recipe, and
    ``diagnose_configure_failure`` only gets to translate it afterwards —
    after a configure that could never have succeeded.

    The grading is not re-derived here. The audit weighs four different
    conditions (nothing declared, two staging blocks disagreeing, a host
    reading the wrong directory, the loader compiled out) and a second copy of
    that judgement in this file would drift from the row the Migrate tab
    shows. This asks the audit and relays its answer.
    """
    try:
        from .models import CheckStatus
        from .snesops import audit_project
    except ImportError:  # pragma: no cover - non-SNES toolkit build
        return None
    try:
        report = audit_project(root)
    except Exception:
        # An audit that cannot run is not grounds for refusing a build; the
        # post-hoc diagnosis still catches the guard if cmake trips it.
        return None
    row = next((c for c in report.checks if c.id == "mod_catalog"), None)
    if row is None or row.status is not CheckStatus.FAIL:
        return None
    fix = row.fix_op or "snes_declare_mod_catalog"
    fw = platforms.current().framework
    return CmdResult(
        False,
        f"Configure would abort on {fw}'s mod-catalog guard, so it was not "
        f"started. {row.detail}. Studio has the fix: Migrate → Audit + Plan, "
        f"tick \"{fix}\", Apply — or "
        f"`apply --root {root} --only {fix}` (add --dry-run first).",
    )


def diagnose_configure_failure(detail: str, root: Path) -> str | None:
    """Extra hint appended to a failed cmake configure message."""
    blob = detail or ""
    if "MAX_PLAYERS must be in" in blob and "got" in blob:
        pre = preflight_max_players(root)
        if pre is not None:
            return pre.message
        return (
            "MAX_PLAYERS rejected by nested psxrecomp. Single-player titles need "
            "a framework pin whose runtime.cmake allows 1..8 — update the "
            "psxrecomp submodule (and framework_pins.txt)."
        )
    if "generated" in blob.lower() and (
        "GEN_MARKER" in blob or "dispatch.c" in blob or "missing" in blob.lower()
    ):
        return (
            "Game generated C may be missing — run Generate (disc→C) before "
            "Configure, or ensure generated/<boot>_dispatch.c exists."
        )
    # snesrecomp's mod-catalog guard. The message it prints is a hand-edit
    # recipe, and hand-editing is what left four SNES ports each spelling the
    # staging differently. Point at the migration instead, which also moves
    # the host's mod_runtime root -- the half the guard cannot see.
    if "snesrecomp_target_mod_catalog" in blob and (
            "no target declared it" in blob or "would never be staged" in blob):
        return (
            "This repo predates the framework's mod catalog contract. Run the "
            "SNES migration to fix it: `migrate_project.py --platform snes "
            "apply --root <repo> --only snes_declare_mod_catalog` (add "
            "--dry-run first). It declares snesrecomp_target_mod_catalog() on "
            "the target, deletes the per-title staging block, and repoints "
            "the host's snes_mod_runtime_initialize_c() root at the layout "
            "the framework stages."
        )
    if "Does not match the generator used previously" in blob:
        return (
            "This build dir already uses a different CMake generator. "
            "Pick matching Generator (Ninja vs Unix Makefiles) on the Build tab, "
            "or remove CMakeCache.txt and CMakeFiles / use a new build dir."
        )
    return None


def bios_backend_present(fw: Path, stem: str) -> bool:
    """True when generated/<stem>_{full,dispatch}.c look linkable."""
    dispatch = fw / "generated" / f"{stem}_dispatch.c"
    full = fw / "generated" / f"{stem}_full.c"
    if not dispatch.is_file() or not full.is_file():
        return False
    try:
        text = dispatch.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return f"{stem}_psx_bios_backend" in text


def _allow_no_bios(extra_args: list[str] | None) -> bool:
    if not extra_args:
        return False
    blob = " ".join(extra_args)
    return (
        "PSXRECOMP_ALLOW_NO_BIOS=ON" in blob
        or "PSXRECOMP_ALLOW_NO_BIOS:BOOL=ON" in blob
        or "PSXRECOMP_ALLOW_NO_BIOS=1" in blob
    )


def _find_psxrecomp_bios(fw: Path, game_root: Path | None = None) -> Path | None:
    names = ("psxrecomp-bios.exe", "psxrecomp-bios")
    dirs: list[Path] = [
        fw / "recompiler" / "build",
        fw / "recompiler" / "build-t2",
    ]
    if game_root is not None:
        dirs.append(game_root / "build-recompiler")
    for d in dirs:
        if not d.is_dir():
            continue
        for name in names:
            p = d / name
            if p.is_file():
                return p
        # Nested generator layouts (e.g. Debug/Release on MSVC)
        for sub in d.iterdir():
            if not sub.is_dir():
                continue
            for name in names:
                p = sub / name
                if p.is_file():
                    return p
    return None


def _recompiler_build_usable(build_dir: Path) -> bool:
    cache = build_dir / "CMakeCache.txt"
    if not cache.is_file():
        return False
    gen = cached_cmake_generator(build_dir)
    if gen.startswith("Ninja"):
        return (build_dir / "build.ninja").is_file()
    if "Makefiles" in gen:
        return (build_dir / "Makefile").is_file()
    if gen.startswith("Visual Studio"):
        return any(build_dir.glob("*.sln"))
    # Unknown generator — trust the cache and let cmake --build diagnose.
    return True


def _iter_recompiler_build_dirs(fw: Path) -> list[Path]:
    src = fw / "recompiler"
    if not src.is_dir():
        return []
    dirs: list[Path] = []
    for name in ("build-t2", "build"):
        dirs.append(src / name)
    dirs.extend(sorted(src.glob("cmake-build*")))
    return dirs


def _any_usable_recompiler_build(fw: Path) -> bool:
    return any(_recompiler_build_usable(d) for d in _iter_recompiler_build_dirs(fw))


def ensure_bios_emitter(
    fw: Path,
    *,
    game_root: Path | None = None,
    build_type: str = DEFAULT_BUILD_TYPE,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Configure ``recompiler/build`` and build ``psxrecomp-bios`` if needed."""
    host = detect_host()
    if not host.cmake:
        return CmdResult(False, "cmake not found on PATH (needed to build psxrecomp-bios)")

    existing = _find_psxrecomp_bios(fw, game_root)
    if existing is not None and not dry_run:
        if log:
            log(f"BIOS emitter ready: {existing}")
        return CmdResult(True, f"BIOS emitter ready: {existing.name}")

    src = fw / "recompiler"
    if not (src / "CMakeLists.txt").is_file():
        return CmdResult(False, f"recompiler sources missing under {src}")

    # Prefer an already-finished tree (matches regen_bios.sh discovery order).
    build_dir = src / "build"
    for cand in _iter_recompiler_build_dirs(fw):
        if _recompiler_build_usable(cand):
            build_dir = cand
            break

    gen = default_generator(host)
    cfg = [host.cmake, "-S", str(src), "-B", str(build_dir)]
    if gen:
        cfg.extend(["-G", gen])
    cfg.append(f"-DCMAKE_BUILD_TYPE={build_type}")
    cfg.extend(toolchain_cache_repairs(build_dir, host=host))

    if dry_run:
        msg = "dry-run: " + " ".join(cfg)
        if log:
            log(msg)
            log(f"dry-run: {host.cmake} --build {build_dir} --target psxrecomp-bios")
        return CmdResult(True, msg)

    if not _recompiler_build_usable(build_dir):
        if log:
            log("Configuring recompiler for psxrecomp-bios…")
        build_dir.mkdir(parents=True, exist_ok=True)
        r = _run_stream(cfg, fw, log=log, env=merged_env(toolchain_env(host)))
        if not r.ok:
            return CmdResult(
                False,
                "Failed to configure recompiler (needed for OpenBIOS regen)",
                r.detail,
            )

    jobs = str(host.jobs)
    build_cmd = [
        host.cmake,
        "--build",
        str(build_dir),
        "--target",
        "psxrecomp-bios",
        "-j",
        jobs,
    ]
    if log:
        log("Building psxrecomp-bios…")
    r = _run_stream(build_cmd, fw, log=log)
    if not r.ok:
        return CmdResult(False, "Failed to build psxrecomp-bios", r.detail)

    bios = _find_psxrecomp_bios(fw, game_root)
    if bios is None:
        return CmdResult(
            False,
            f"psxrecomp-bios not found after build under {build_dir}",
        )
    return CmdResult(True, f"Built BIOS emitter: {bios.name}")


def _regen_bios_profile(
    fw: Path,
    profile_rel: str,
    *,
    game_root: Path | None = None,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Regen one BIOS profile into ``fw/generated/`` (canonical regen_bios.sh)."""
    profile = fw / profile_rel
    if not profile.is_file():
        return CmdResult(False, f"BIOS profile missing: {profile}")

    script = fw / "tools" / "regen_bios.sh"
    bash = find_bash()

    if script.is_file() and bash:
        cmd = [bash, str(script), "--config", profile_rel]
        if dry_run:
            msg = "dry-run: " + " ".join(cmd) + f"  (cwd={fw})"
            if log:
                log(msg)
            return CmdResult(True, msg)
        return _run_stream(cmd, fw, log=log)

    # Fallback without bash: invoke emitter + optional fingerprint helper.
    r = ensure_bios_emitter(fw, game_root=game_root, dry_run=dry_run, log=log)
    if not r.ok:
        return r
    if dry_run:
        return CmdResult(True, f"dry-run: psxrecomp-bios --config {profile_rel}")

    bios = _find_psxrecomp_bios(fw, game_root)
    if bios is None:
        return CmdResult(False, "psxrecomp-bios missing after ensure")
    (fw / "generated").mkdir(parents=True, exist_ok=True)
    r = _run_stream([str(bios), "--config", profile_rel], fw, log=log)
    if not r.ok:
        return CmdResult(False, f"psxrecomp-bios failed for {profile_rel}", r.detail)

    # Best-effort fingerprint (staleness WARN in runtime.cmake).
    fp = fw / "tools" / "bios_emitter_fingerprint.sh"
    if fp.is_file() and bash:
        stem = OPENBIOS_STEM if "OpenBIOS" in profile_rel else SCPH1001_STEM
        try:
            proc = subprocess.run(
                [bash, str(fp), profile_rel],
                cwd=str(fw),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                out = fw / "generated" / f"{stem}.emitter.sha"
                out.write_text(proc.stdout, encoding="utf-8")
                if log:
                    log(f"Wrote fingerprint {out.name}")
        except OSError:
            pass
    return CmdResult(True, f"Regenerated BIOS profile {profile_rel}")


def ensure_bios_backends(
    root: Path,
    *,
    force: bool = False,
    include_scph1001: bool = True,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Ensure linkable OpenBIOS (and optional SCPH1001) under ``psxrecomp/generated``.

    OpenBIOS is bundled and MIT-licensed. SCPH1001 is regenerated only when the
    retail dump ``bios/SCPH1001.BIN`` is already present beside the profile.
    """
    root = root.expanduser().resolve()
    fw = resolve_framework_root(root)
    if fw is None:
        return CmdResult(True, "No psxrecomp framework — skipping BIOS ensure")

    needed: list[tuple[str, str]] = []
    if force or not bios_backend_present(fw, OPENBIOS_STEM):
        needed.append((OPENBIOS_STEM, OPENBIOS_PROFILE))
    elif log:
        log(f"OpenBIOS backend already present under {fw / 'generated'}")

    scph_rom = fw / "bios" / "SCPH1001.BIN"
    if include_scph1001 and scph_rom.is_file():
        if force or not bios_backend_present(fw, SCPH1001_STEM):
            needed.append((SCPH1001_STEM, SCPH1001_PROFILE))
        elif log:
            log("SCPH1001 backend already present")

    if not needed:
        return CmdResult(True, "BIOS backends already generated")

    # Prefer regen_bios.sh (builds emitter + fingerprints). It does not configure
    # the recompiler — ensure a usable tree (or a found binary) first.
    script = fw / "tools" / "regen_bios.sh"
    bash = find_bash()
    need_emitter_setup = not (
        _any_usable_recompiler_build(fw) or _find_psxrecomp_bios(fw, root) is not None
    )
    if need_emitter_setup or not (script.is_file() and bash):
        r = ensure_bios_emitter(fw, game_root=root, dry_run=dry_run, log=log)
        if not r.ok:
            return r

    done: list[str] = []
    for stem, profile in needed:
        if log:
            log(f"Regenerating {stem} via {profile}…")
        r = _regen_bios_profile(
            fw, profile, game_root=root, dry_run=dry_run, log=log
        )
        if not r.ok:
            return CmdResult(
                False,
                f"Failed to regenerate {stem}: {r.message}",
                r.detail,
            )
        if not dry_run and not bios_backend_present(fw, stem):
            return CmdResult(
                False,
                f"Regen finished but {stem} backend still missing under {fw / 'generated'}",
            )
        done.append(stem)

    return CmdResult(True, "BIOS ready: " + ", ".join(done))


def configure(
    root: Path,
    *,
    build_dir: str = DEFAULT_BUILD_DIR,
    build_type: str = DEFAULT_BUILD_TYPE,
    generator: str | None = None,
    extra_args: list[str] | None = None,
    dry_run: bool = False,
    log: LogFn | None = None,
    ensure_bios: bool = True,
) -> CmdResult:
    root = root.expanduser().resolve()
    host = detect_host()
    if not host.cmake:
        return CmdResult(False, "cmake not found on PATH")
    if not (root / "CMakeLists.txt").is_file():
        return CmdResult(False, f"No CMakeLists.txt in {root}")

    profile = platforms.current()

    # The BIOS backend step is a PSX concept: a cartridge boots from its own
    # reset vector and there is nothing to stage. Asked as has_bios rather than
    # as "not snes", so a console added later does not inherit a BIOS hunt by
    # being spelled differently.
    if ensure_bios and profile.has_bios and not _allow_no_bios(extra_args):
        bios_r = ensure_bios_backends(root, dry_run=dry_run, log=log)
        if not bios_r.ok:
            return bios_r
        if log and bios_r.message:
            log(bios_r.message)

    # MAX_PLAYERS is psxrecomp's runtime.cmake range check; neither cartridge
    # scaffold declares it.
    pre = preflight_max_players(root) if profile.has_bios else None
    if pre is not None:
        if log:
            log(pre.message)
        return pre

    # A SNES port whose mod catalog no target declares cannot configure —
    # snesrecomp's guard is a FATAL_ERROR. The audit already grades that state
    # and names the op that fixes it, so say so instead of spending a configure
    # to be handed the framework's hand-edit recipe.
    if profile.key == "snes":
        pre = preflight_snes_mod_catalog(root)
        if pre is not None:
            if log:
                log(pre.message)
            return pre

    # n64lle is resolved as a PRE-BUILT tree, not add_subdirectory()'d, so a
    # port cannot configure until the framework has been built out of tree.
    if profile.key == "n64":
        pre = preflight_n64_framework(root)
        if pre is not None:
            if log:
                log(pre.message)
            return pre
        # The framework preflight just checked, not the submodule the
        # CMakeLists defaults to. Empty unless $N64LLE_ROOT /
        # $N64LLE_FRAMEWORK_BUILD_DIR override it; a caller's own -D wins.
        explicit = _explicit_cache_vars(extra_args)
        extra_args = [a for a in _n64_paths.framework_configure_args(root)
                      if a[2:].split("=", 1)[0] not in explicit] + list(extra_args or [])

    bdir = Path(build_dir)
    if not bdir.is_absolute():
        bdir = root / bdir
    gen = resolve_configure_generator(host, bdir, generator)
    cmd = [host.cmake, "-S", str(root), "-B", str(bdir)]
    if gen:
        cmd.extend(["-G", gen])
    cmd.append(f"-DCMAKE_BUILD_TYPE={build_type}")
    # Bundled deps before caller overrides: a caller's explicit -D wins.
    repairs = toolchain_cache_repairs(bdir, extra_args, host)
    cmd.extend(repairs)
    if extra_args:
        cmd.extend(extra_args)
    tc_env = toolchain_env(host)

    if dry_run:
        msg = "dry-run: " + " ".join(cmd)
        if log:
            log(msg)
        return CmdResult(True, msg)

    if log and repairs:
        log("Re-pointing cached host deps at the toolchain: " + " ".join(repairs))
    r = _run_stream(cmd, root, log=log, env=merged_env(tc_env))
    if r.ok:
        r = CmdResult(
            True,
            f"Configured {bdir.name} ({build_type}" + (f", {gen}" if gen else "") + ")",
            r.detail,
        )
        return r
    hint = diagnose_configure_failure(r.detail or "", root)
    if hint:
        msg = f"{r.message}\n{hint}"
        if log:
            log(hint)
        return CmdResult(False, msg, r.detail)
    return r


# A header a *found* package failed to provide. The pattern matters more than
# the library: it is what a dependency resolved outside the compiler's sysroot
# always looks like.
_MISSING_DEP_HEADER_RE = re.compile(
    r"fatal error: '((?:SDL[23]|zlib|png|freetype)[^']*)' file not found"
)


def diagnose_build_failure(detail: str, build_dir: Path) -> str | None:
    """Why a compile failed on a header its CMake package claimed to provide.

    The case this exists for: configure succeeds, links SDL3::SDL3, prints
    "SDL3 desktop backend" — and every translation unit then fails on
    ``'SDL3/SDL.h' file not found``. The toolchain pack's clang runs against
    its OWN sysroot and never searches /usr/include, while CMake omits an
    include directory it believes is implicit. So a build tree whose
    ``SDL3_DIR`` resolved to the host's /usr/lib/cmake/SDL3 configures cleanly
    and cannot compile a line.

    Stated from the cache rather than guessed: the message names the entry that
    is actually wrong and where it actually points.
    """
    m = _MISSING_DEP_HEADER_RE.search(detail or "")
    if m is None:
        return None
    header = m.group(1)
    pack = toolchain_root()
    lines = [
        f"'{header}' went missing even though CMake found the package that "
        "provides it — the dependency resolved outside the compiler's sysroot."
    ]
    if pack is None:
        lines.append(
            "No retcomm toolchain pack is driving this build, so Studio cannot "
            "re-point it. If you meant to build with the pack, run Studio under "
            "its python (the GUI does) and Configure again."
        )
        return " ".join(lines)
    for name, want in toolchain_env().items():
        if name == "CMAKE_PREFIX_PATH":
            continue
        have = cache_entry(build_dir, name)
        if have and Path(have) != Path(want):
            lines.append(
                f"{build_dir.name} has {name}={have}, outside the pack; the "
                f"pack ships its own at {want}."
            )
    lines.append(
        "Configure again — Studio re-points a stale entry automatically — or "
        "delete CMakeCache.txt and configure fresh."
    )
    return " ".join(lines)


def build(
    root: Path,
    *,
    build_dir: str = DEFAULT_BUILD_DIR,
    target: str = DEFAULT_TARGET,
    jobs: int | None = None,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    root = root.expanduser().resolve()
    host = detect_host()
    if not host.cmake:
        return CmdResult(False, "cmake not found on PATH")
    bdir = Path(build_dir)
    if not bdir.is_absolute():
        bdir = root / bdir
    if not bdir.is_dir():
        return CmdResult(False, f"Build dir missing — Configure first: {bdir}")

    j = jobs if jobs and jobs > 0 else host.jobs
    cmd = [host.cmake, "--build", str(bdir), "--target", target, "-j", str(j)]
    if dry_run:
        msg = "dry-run: " + " ".join(cmd)
        if log:
            log(msg)
        return CmdResult(True, msg)

    r = _run_stream(cmd, root, log=log)
    if r.ok:
        exe = find_runtime_exe(bdir, preferred=target)
        hint = f" → {exe.name}" if exe else ""
        return CmdResult(True, f"Built {target} in {bdir.name}{hint}", r.detail)
    hint = diagnose_build_failure(r.detail or "", bdir)
    if hint:
        if log:
            log(hint)
        return CmdResult(False, f"{r.message} — {hint}", r.detail)
    return r


def find_runtime_exe(build_dir: Path, preferred: str = "") -> Path | None:
    """Locate the game product binary under a CMake build tree.

    ``preferred`` is the product's CMake target name (``default_target``); an
    executable with exactly that stem wins outright.  Without it the finder
    falls back to name heuristics, and those must not be a coin toss: a full
    ``cmake --build`` drops the project's ctest binaries next to the product,
    and a tie broken alphabetically launched ``ppu_window_test`` instead of
    ``SuperMetroidSNESRecomp`` (2026-09-11).
    """
    build_dir = build_dir.expanduser().resolve()
    preferred = (preferred or "").strip()
    if preferred.lower().endswith(".exe"):
        preferred = preferred[:-4]
    if not build_dir.is_dir():
        return None

    host = detect_host()
    suffixes = {""}
    if host.label == "windows":
        suffixes = {".exe"}

    # Prefer names that look like Recompiled products / known targets.
    ranked: list[tuple[int, Path]] = []
    skip_dirs = {
        "CMakeFiles",
        "_deps",
        ".cmake",
        "Testing",
        "CMakeTmp",
        "assets",
        "bios",
        "fonts",
        "img",
        "mods",
    }

    def consider(p: Path) -> None:
        if not p.is_file():
            return
        name = p.name
        lower = name.lower()
        if host.label == "windows":
            if not lower.endswith(".exe"):
                return
            stem = name[:-4]
        else:
            if any(
                lower.endswith(ext)
                for ext in (".so", ".dll", ".dylib", ".a", ".lib", ".pdb", ".cmake", ".ninja")
            ):
                return
            stem = name
        if stem.lower() in ("cmake", "ninja", "cpack", "ctest"):
            return
        score = 0
        if preferred and stem == preferred:
            score += 1000
        if "recomp" in lower:  # *Recomp, *-recompiled, *SNESRecomp
            score += 100
        if stem in ("psx-runtime", "psx-runtime.exe") or stem == "psx-runtime":
            score += 50
        # Test / tooling binaries a full build leaves beside the product.
        if re.search(r"(^|_)test(s|_|$)|_capture$|_smoke$|_bench$", lower):
            score -= 200
        if p.parent == build_dir:
            score += 20
        if host.label != "windows" and not os.access(p, os.X_OK):
            return
        ranked.append((score, p))

    for p in build_dir.iterdir():
        if p.is_file():
            consider(p)

    for sub in build_dir.iterdir():
        if not sub.is_dir() or sub.name in skip_dirs or sub.name.startswith("."):
            continue
        if sub.name in ("Debug", "Release", "RelWithDebInfo", "MinSizeRel") or host.label == "windows":
            for p in sub.iterdir():
                if p.is_file():
                    consider(p)

    if not ranked:
        return None
    ranked.sort(key=lambda t: (-t[0], t[1].name.lower()))
    return ranked[0][1]


def resolve_build_dir(root: Path, build_dir: str) -> Path:
    root = root.expanduser().resolve()
    bdir = Path(build_dir)
    if not bdir.is_absolute():
        bdir = root / bdir
    return bdir


def launch_rom_for(root: Path | str, rom: str = "") -> tuple[str, str]:
    """Which ROM a SNES launch should hand the runner, and where it came from.

    Returns ``(path, provenance)``; both empty when there is nothing to pass.
    A cartridge runner takes the ROM as a positional and exits 1 without one,
    and the path is not the caller's to remember — it is whatever New Project
    or the Migrate tab recorded for this repo.
    """
    rom = (rom or "").strip()
    if rom:
        return rom, "explicit"
    if not platforms.current().is_cartridge:
        return "", ""
    from .repo_index import load_index

    entry = load_index().find(root)
    if entry is not None and entry.cue:
        return entry.cue, "the repo index"
    return "", ""


def launch(
    root: Path,
    *,
    build_dir: str = DEFAULT_BUILD_DIR,
    exe: Path | str | None = None,
    env_text: str = "",
    extra_args: list[str] | None = None,
    dry_run: bool = False,
    log: LogFn | None = None,
    wait: bool = True,
    target: str = "",
) -> CmdResult:
    """Start the local product build.

    ``target`` names the product executable (CMake target); when empty it is
    resolved the same way Compile resolves it, so Launch and Compile always
    agree on which binary is the game.

    By default ``wait=True``: stream stdout/stderr into ``log`` until the
    process exits (Studio Launch → activity log). Pass ``wait=False`` to
    detach immediately (legacy fire-and-forget).
    """
    global _active_launch
    root = root.expanduser().resolve()
    bdir = resolve_build_dir(root, build_dir)
    if exe:
        exe_path: Path | None = Path(exe)
    else:
        exe_path = find_runtime_exe(bdir, preferred=target or default_target(root))
    if exe_path is None:
        return CmdResult(False, f"No runtime executable found under {bdir}")
    if not exe_path.is_file():
        return CmdResult(False, f"Executable missing: {exe_path}")

    overlay = parse_env_text(env_text)
    env = os.environ.copy()
    env.update(overlay)
    # Run from game root so relative game.toml / disc / saves resolve.
    cmd = [str(exe_path), *(extra_args or [])]
    # Prefer line-buffered stdio so fprintf diagnostics show up live when piped.
    if sys.platform != "win32" and shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL", "-eL", *cmd]

    if dry_run:
        preview = " ".join(f"{k}={v}" for k, v in overlay.items())
        msg = "dry-run: " + (f"env {preview} " if preview else "") + " ".join(cmd)
        _flush_log(log, msg)
        return CmdResult(True, msg)

    with _launch_lock:
        if _active_launch and _active_launch.poll() is None:
            return CmdResult(
                False,
                f"Already running (pid {_active_launch.pid}) — Stop first",
            )
        stale = _read_launch_pid()
        if stale and _pid_alive(stale[0]):
            return CmdResult(
                False,
                f"Already running (pid {stale[0]}) — Stop first",
            )
        try:
            kwargs: dict = {
                "cwd": str(root),
                "env": env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
            }
            host = detect_host()
            if host.label == "windows":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                # New session → killpg works from a separate ``build stop`` process.
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            return CmdResult(False, "Launch failed", str(exc))
        _active_launch = LaunchHandle(
            proc=proc, exe=exe_path, cwd=root, env_overlay=overlay
        )
        if proc.pid:
            _write_launch_pid(proc.pid, exe=exe_path, root=root)

    env_note = ""
    if overlay:
        env_note = " env=[" + ", ".join(sorted(overlay)) + "]"
    msg = f"Launched {exe_path.name} (pid {proc.pid}){env_note}"
    _flush_log(log, msg)
    _flush_log(log, "--- game stdout/stderr (Stop to end) ---")

    if not wait:
        # Detach: drain pipes so the child never blocks on a full pipe.
        def _drain() -> None:
            global _active_launch
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        _flush_log(log, line.rstrip("\n\r"))
            except Exception:
                pass
            code = proc.wait()
            with _launch_lock:
                if _active_launch and _active_launch.proc is proc:
                    _active_launch = None
            _clear_launch_pid()
            _flush_log(log, f"--- game exited (code {code}) ---")

        threading.Thread(target=_drain, daemon=True).start()
        return CmdResult(True, msg)

    # Stream diagnostics until exit (Studio keeps the CLI job alive → activity log).
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            _flush_log(log, line.rstrip("\n\r"))
    except Exception as exc:
        _flush_log(log, f"[warn] log reader: {exc}")
    code = proc.wait()
    with _launch_lock:
        if _active_launch and _active_launch.proc is proc:
            _active_launch = None
    _clear_launch_pid()
    _flush_log(log, f"--- game exited (code {code}) ---")
    if code != 0:
        return CmdResult(False, f"{exe_path.name} exited {code}", msg)
    return CmdResult(True, f"{exe_path.name} exited 0")


def stop_launch() -> CmdResult:
    global _active_launch
    killed_pid: int | None = None
    with _launch_lock:
        h = _active_launch
        if h is not None and h.poll() is None:
            killed_pid = h.pid
            h.terminate()
            try:
                h.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                h.proc.kill()
            _active_launch = None
    # Cross-process Stop (Studio Launch holds ``build run`` in another Python).
    stale = _read_launch_pid()
    if stale and _pid_alive(stale[0]):
        _kill_launch_pid(stale[0])
        killed_pid = stale[0]
    _clear_launch_pid()
    if killed_pid is None:
        return CmdResult(False, "No running launch")
    return CmdResult(True, f"Stopped pid {killed_pid}")


def launch_status() -> str:
    with _launch_lock:
        h = _active_launch
        if h is not None:
            code = h.poll()
            if code is None:
                return f"running pid={h.pid} ({h.exe.name})"
            return f"exited code={code} ({h.exe.name})"
    stale = _read_launch_pid()
    if stale and _pid_alive(stale[0]):
        name = Path(stale[1]).name if stale[1] else "?"
        return f"running pid={stale[0]} ({name})"
    return "not running"


def find_psxrecomp_cli(root: Path) -> Path | None:
    """Locate ``psxrecomp_cli.py`` next to the game's framework checkout.

    ``RETCOMM_PSXRECOMP_CLI`` overrides the search so a framework change can be
    exercised against a game repo before its submodule pin moves.
    """
    root = root.expanduser().resolve()
    override = os.environ.get("RETCOMM_PSXRECOMP_CLI", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.resolve()
    fw = resolve_framework_root(root)
    candidates: list[Path] = []
    if fw is not None:
        candidates.append(fw / "psxrecomp_cli.py")
    candidates.append(root / "psxrecomp" / "psxrecomp_cli.py")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def generate_rom_and_bios(
    root: Path,
    *,
    disc: str = "",
    bios: str = "",
    force_bios: bool = True,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Run ``psxrecomp_cli.py generate`` (BIOS backends + disc prepare + game C).

    ``bios`` empty → OpenBIOS (bundled MIT). Non-empty path → stage as
    ``bios/SCPH1001.BIN`` and regenerate the retail backend, then game C.
    """
    root = root.expanduser().resolve()
    cli = find_psxrecomp_cli(root)
    if cli is None:
        return CmdResult(False, "psxrecomp_cli.py not found (is psxrecomp checked out?)")
    config = root / "game.toml"
    if not config.is_file():
        return CmdResult(False, f"Missing game.toml in {root}")

    py = sys.executable or shutil.which("python3") or shutil.which("python")
    if not py:
        return CmdResult(False, "No Python interpreter for generate")

    cmd = [
        str(py),
        str(cli),
        "generate",
        "--config",
        str(config),
        "--project-root",
        str(root),
    ]
    disc_s = (disc or "").strip()
    if disc_s:
        cmd.extend(["--disc", disc_s])
    bios_s = (bios or "").strip()
    if bios_s:
        bp = Path(bios_s).expanduser()
        if not bp.is_file():
            return CmdResult(False, f"BIOS dump not found: {bios_s}")
        cmd.extend(["--bios", str(bp.resolve())])
    if force_bios:
        cmd.append("--force-bios")

    if dry_run:
        return CmdResult(True, "dry-run: " + " ".join(cmd))

    if log:
        mode = f"SCPH1001 ({bios_s})" if bios_s else "OpenBIOS"
        log(f"Generate ROM + BIOS C ({mode})")
    return _run_stream(cmd, root, log=log)


def ensure_emitters(
    root: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Build ``psxrecomp-game`` + ``psxrecomp-bios`` via ``psxrecomp_cli ensure-emitters``."""
    root = root.expanduser().resolve()
    cli = find_psxrecomp_cli(root)
    if cli is None:
        return CmdResult(False, "psxrecomp_cli.py not found (is psxrecomp checked out?)")

    py = sys.executable or shutil.which("python3") or shutil.which("python")
    if not py:
        return CmdResult(False, "No Python interpreter for ensure-emitters")

    cmd = [
        str(py),
        str(cli),
        "ensure-emitters",
        "--project-root",
        str(root),
    ]
    if force:
        cmd.append("--force")

    if dry_run:
        return CmdResult(True, "dry-run: " + " ".join(cmd))

    if log:
        log("Generate emitters (psxrecomp-game + psxrecomp-bios)" + (" [force]" if force else ""))
    return _run_stream(cmd, root, log=log)


# --- local bundle + export -------------------------------------------------
#
# Studio's Build tab "Bundle + Export" for the regular (host) build: package
# what is already in the local build dir into dist/<prefix>-<ver>-<tag>.zip,
# then hand the path back so the GUI can open a native save dialog.


@dataclass
class PackageResult:
    ok: bool
    message: str
    detail: str = ""
    zip_path: Path | None = None


def host_artifact_tag(host: BuildHost | None = None) -> str:
    """`linux-x64`, `windows-x64`, `macos-arm64`, … — matches CI zip naming."""
    host = host or detect_host()
    machine = (platform.machine() or "").lower()
    if machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "x86"
    else:
        arch = re.sub(r"[^a-z0-9]+", "", machine) or "unknown"
    return f"{host.label}-{arch}"


def project_version(root: Path) -> str:
    """VERSION file → game.toml `version` → `0.0.0` (mirrors package_release.sh)."""
    vf = root / "VERSION"
    if vf.is_file():
        text = vf.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text.split()[0]
    toml = root / "game.toml"
    if toml.is_file():
        for line in toml.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"""\s*version\s*=\s*["']([^"']+)["']""", line)
            if m:
                return m.group(1).strip()
    return "0.0.0"


def _zip_prefix(root: Path) -> str:
    try:
        from fill_tokens import derive_zip_prefix
    except ImportError:
        derive_zip_prefix = None  # type: ignore[assignment]
    if derive_zip_prefix is not None:
        prefix = derive_zip_prefix(root.name)
        if prefix:
            return prefix
    return re.sub(r"[^a-z0-9._-]+", "", root.name.lower()) or "game"


def _newest_zip(dist: Path, tag: str) -> Path | None:
    if not dist.is_dir():
        return None
    zips = sorted(dist.glob(f"*-{tag}.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not zips:
        zips = sorted(dist.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0].resolve() if zips else None


def _stage_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copy2(src, dst)


def _write_zip(stage: Path, out: Path) -> None:
    """Zip `stage` preserving the executable bit (no `zip` binary needed)."""
    import zipfile

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    entries = sorted(p for p in stage.rglob("*"))
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in entries:
            rel = path.relative_to(stage).as_posix()
            if path.is_dir():
                info = zipfile.ZipInfo(rel + "/")
                info.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(info, b"")
                continue
            info = zipfile.ZipInfo.from_file(path, rel)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = path.stat().st_mode & 0o777
            info.external_attr = mode << 16
            with path.open("rb") as fh, zf.open(info, "w") as out_fh:
                shutil.copyfileobj(fh, out_fh)


def _stage_local_bundle(
    root: Path,
    exe: Path,
    stage: Path,
    *,
    log: LogFn | None = None,
) -> str:
    """Built-in stager used when the repo has no scripts/package_release.sh.

    Mirrors that script's payload: exe + assets/{fonts,img} + bundled OpenBIOS
    + game.toml / VERSION. Never stages a disc image or retail BIOS dump.
    """
    exe_dir = exe.parent
    _stage_copy(exe, stage / exe.name)

    # Runtime shared libs sitting next to the exe (dynamic builds).
    for pattern in ("*.dll", "*.DLL", "*.so", "*.so.*", "*.dylib"):
        for lib in sorted(exe_dir.glob(pattern)):
            if lib.is_file():
                _stage_copy(lib, stage / lib.name)

    staged_assets = False
    for sub in ("fonts", "img"):
        src = exe_dir / "assets" / sub
        if src.is_dir():
            _stage_copy(src, stage / "assets" / sub)
            staged_assets = True
    if not staged_assets:
        return f"assets/fonts + assets/img missing next to {exe.name} — rebuild first"

    bios_bin = ""
    for cand in (
        exe_dir / "bios" / "openbios.bin",
        root / "psxrecomp" / "bios" / "openbios.bin",
        root / "bios" / "openbios.bin",
    ):
        if cand.is_file():
            _stage_copy(cand, stage / "bios" / "openbios.bin")
            bios_bin = str(cand)
            break
    if not bios_bin:
        return "bios/openbios.bin not found — rebuild psx-runtime to stage it"
    for cand in (
        exe_dir / "bios" / "OpenBIOS.LICENSE",
        root / "psxrecomp" / "bios" / "OpenBIOS.LICENSE",
    ):
        if cand.is_file():
            _stage_copy(cand, stage / "bios" / "OpenBIOS.LICENSE")
            break

    for name in ("game.toml", "VERSION", "README-SETUP.txt", "keybinds.ini"):
        src = root / name
        if src.is_file():
            _stage_copy(src, stage / name)

    # Exe-relative runtime data the build staged next to the binary. The
    # runtime resolves these from the EXE's own directory (mods via
    # <exe_dir>/mods), so a zip without them ships a game whose Mods page is
    # empty and whose netplay lobbies can never agree on a mod plan.
    # mods/state.toml is the packaging machine's own enable/disable state —
    # preloaded catalogs ship default-disabled, so it must never travel.
    staged_extra = []
    for sub in ("mods", "bezels"):
        src = exe_dir / sub
        if src.is_dir():
            _stage_copy(src, stage / sub)
            staged_extra.append(sub)
    for leftover in ("state.toml", "state.toml.tmp"):
        stale = stage / "mods" / leftover
        if stale.is_file():
            stale.unlink()

    # Project-root data the runtime reads relative to the project (not the
    # exe): translations, and the mods/preloaded source a rebuild restages
    # the catalog from.
    for sub in ("translations",):
        src = root / sub
        if src.is_dir():
            _stage_copy(src, stage / sub)
            staged_extra.append(sub)

    _flush_log(
        log,
        f"    staged {exe.name} + assets + bios (OpenBIOS: {bios_bin})"
        + (f" + {', '.join(staged_extra)}" if staged_extra else ""),
    )
    return ""


def snes_build_is_setup_host(build_dir: Path) -> bool:
    """Was this build dir configured -DSNESRECOMP_SETUP_HOST=ON?

    The two SNES build shapes need different packaging and the cache is the
    only honest way to tell them apart: a SETUP HOST carries no recompiled
    code and ships as a source pack the player rebuilds, while an ordinary
    build links src/gen and is playable as it stands. An absent or unreadable
    cache reads as "not a setup host", which is the safe answer -- it routes
    to the local stager, which ships no source tree and claims nothing.
    """
    return cache_entry(build_dir, "SNESRECOMP_SETUP_HOST").upper() == "ON"


# Files a build dir accumulates that are the developer's machine, not the
# game: logs, the local LAN room registry, and config.ini (player name and
# controller GUIDs). Shipping them is a privacy leak and hands the player
# someone else's settings.
_SNES_LOCAL_SKIP = {
    "config.ini",
    "netplay_lan_lobby.txt",
    "snes-diag.log",
    "mesen.log",
}


def _stage_snes_local_bundle(
    root: Path,
    exe: Path,
    stage: Path,
    *,
    log: LogFn | None = None,
) -> str:
    """Stage a PLAYABLE SNES build: what the game loads, and nothing else.

    This is the local counterpart of the MinGW script's zip, for the build
    that is already sitting in the build dir -- not a release. A release is a
    setup pack (scripts/package_release.sh), built from a tree with no
    generated C; this binary has the recompiled code linked in, so the zip
    is for the machines its author chooses, and it carries no source tree and
    no README promising a rebuild.

    Never stages ROM bytes: no .sfc/.smc/.zip is copied from anywhere, and
    src/gen (recompiler OUTPUT, not input) is not part of the payload either
    -- only the compiled exe is.
    """
    exe_dir = exe.parent
    _stage_copy(exe, stage / exe.name)

    # Runtime shared libs sitting next to the exe (dynamic builds).
    for pattern in ("*.dll", "*.DLL", "*.so", "*.so.*", "*.dylib"):
        for lib in sorted(exe_dir.glob(pattern)):
            if lib.is_file():
                _stage_copy(lib, stage / lib.name)

    staged_assets = False
    for sub in ("fonts", "img"):
        src = exe_dir / "assets" / sub
        if src.is_dir():
            _stage_copy(src, stage / "assets" / sub)
            staged_assets = True
    if not staged_assets:
        return f"assets/fonts + assets/img missing next to {exe.name} — rebuild first"

    # Exe-relative runtime data, from beside the binary where the runtime
    # resolves it. mods/ carries the netplay mod plan; without it the Mods
    # page is empty and a lobby can never agree on one.
    staged_extra = []
    for sub in ("mods", "translations"):
        src = exe_dir / sub
        if not src.is_dir():
            src = root / sub
        if src.is_dir():
            _stage_copy(src, stage / sub)
            staged_extra.append(sub)
    # The packaging machine's own enable/disable state. Preloaded catalogs
    # ship default-disabled; travelling with this file would hand every
    # player whatever was toggled here when the zip was made.
    for leftover in ("state.toml", "state.toml.tmp"):
        stale = stage / "mods" / "preloaded" / leftover
        if stale.is_file():
            stale.unlink()
        stale = stage / "mods" / leftover
        if stale.is_file():
            stale.unlink()

    for name in ("VERSION", "keybinds.ini", "LICENSE"):
        for src in (exe_dir / name, root / name):
            if src.is_file():
                _stage_copy(src, stage / name)
                break

    for name in _SNES_LOCAL_SKIP:
        stale = stage / name
        if stale.is_file():
            stale.unlink()

    _flush_log(
        log,
        f"    staged {exe.name} + assets"
        + (f" + {', '.join(staged_extra)}" if staged_extra else "")
        + " (playable build; no ROM, no source tree)",
    )
    return ""


# Files an n64lle port keeps beside its executable that belong to the machine
# it was built on, never to a zip. settings.toml is written by the launcher and
# input.cfg by its Configure page; both carry absolute ROM paths, pad GUIDs and
# scancodes. The port's own .gitignore lists exactly these for the same reason.
_N64_LOCAL_SKIP = (
    "settings.toml",
    "settings.toml.bad",
    "input.cfg",
    "keybinds.ini",
)


def _stage_n64_local_bundle(
    root: Path,
    exe: Path,
    stage: Path,
    *,
    log: LogFn | None = None,
) -> str:
    """Stage a PLAYABLE n64lle build: what the game loads, and nothing else.

    An n64lle port has no scripts/package_release.sh to defer to — n64lle ships
    no release-workflow template yet — so this is the only packager on this
    console, and it is a local zip rather than a release.

    Never stages ROM bytes: roms/ holds the user's own dump (usually a SYMLINK
    to it, which a naive copytree would follow), and generated/ is ROM-derived
    C whose distribution posture n64lle has explicitly not settled
    (docs/DISTRIBUTION-POSTURE.md is a draft). Neither is part of the payload;
    only the compiled executable is.
    """
    exe_dir = exe.parent
    _stage_copy(exe, stage / exe.name)

    # Runtime shared libs sitting next to the exe (dynamic builds).
    for pattern in ("*.dll", "*.DLL", "*.so", "*.so.*", "*.dylib"):
        for lib in sorted(exe_dir.glob(pattern)):
            if lib.is_file():
                _stage_copy(lib, stage / lib.name)

    # recomp_ui.cmake stages the console's fonts and art beside the executable
    # (CONSOLE n64 selects the Nintendo 64 SystemProfile). Absent, the launcher
    # comes up with no fonts, so this is a rebuild, not a warning.
    staged_assets = False
    for sub in ("fonts", "img"):
        src = exe_dir / "assets" / sub
        if src.is_dir():
            _stage_copy(src, stage / "assets" / sub)
            staged_assets = True
    if not staged_assets:
        return f"assets/fonts + assets/img missing next to {exe.name} — rebuild first"

    # game.toml is the contract the host reads at startup (host_config.c), so a
    # zip without it is a build that cannot resolve its own title. It is the
    # one config file that IS source here.
    contract = root / "game.toml"
    if not contract.is_file():
        return f"game.toml missing in {root} — an n64lle host reads it at startup"
    _stage_copy(contract, stage / "game.toml")

    for name in ("VERSION", "LICENSE", "README.md"):
        for src in (exe_dir / name, root / name):
            if src.is_file():
                _stage_copy(src, stage / name)
                break

    for name in _N64_LOCAL_SKIP:
        stale = stage / name
        if stale.is_file():
            stale.unlink()

    _flush_log(
        log,
        f"    staged {exe.name} + assets + game.toml "
        "(playable build; no ROM, no generated C)",
    )
    return ""


def package_local(
    root: Path,
    *,
    build_dir: str = DEFAULT_BUILD_DIR,
    artifact_tag: str = "",
    exe: Path | None = None,
    use_repo_script: bool = True,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> PackageResult:
    """Zip the existing local build into ``<root>/dist``.

    Prefers the repo's own ``scripts/package_release.sh`` (CI-parity payload
    and zip name) when it exists and bash is available; otherwise stages an
    equivalent bundle in Python so this works on Windows too.
    """
    root = root.expanduser().resolve()
    bdir = resolve_build_dir(root, build_dir)
    if not bdir.is_dir():
        return PackageResult(False, f"Build dir missing — Build first: {bdir}")

    binary = exe.expanduser().resolve() if exe else find_runtime_exe(bdir)
    if binary is None or not binary.is_file():
        return PackageResult(False, f"No runtime executable under {bdir} — Build first")

    tag = artifact_tag.strip() or host_artifact_tag()
    version = project_version(root)
    dist = root / "dist"
    script = root / "scripts" / "package_release.sh"
    bash = find_bash()
    profile = platforms.current()
    snes = profile.key == "snes"
    n64 = profile.key == "n64"
    script_ok = use_repo_script and script.is_file() and bool(bash)
    # A SNES repo's package_release.sh packages SETUP HOSTS only: it refuses a
    # build dir without -DSNESRECOMP_SETUP_HOST=ON, and CMake in turn refuses
    # that option while src/gen holds generated C. So an ordinary playable
    # build could never be packaged at all -- the button failed with
    # "was not configured with -DSNESRECOMP_SETUP_HOST=ON" and no way forward.
    # Route on what the build dir actually is instead.
    setup_host = snes and snes_build_is_setup_host(bdir)
    # n64lle ships no release-workflow or packager template, so a
    # scripts/package_release.sh in an N64 port did not come from its
    # scaffolder. Running one here would hand a psxrecomp-shaped argv to an
    # unknown script; the built-in stager is the honest route.
    use_script = script_ok and not n64 and (not snes or setup_host)

    if snes and setup_host and not script_ok:
        return PackageResult(
            False,
            "This build dir is a SETUP HOST, which ships as a source pack — "
            "that needs scripts/package_release.sh (emit it from the Migrate "
            "tab, snes_emit_packager) and bash.",
        )

    if dry_run:
        how = f"{script.name} {bdir.name} {tag}" if use_script else f"built-in stager ({tag})"
        msg = f"dry-run: package {binary.name} via {how} → {dist}"
        _flush_log(log, msg)
        return PackageResult(True, msg)

    if use_script:
        _flush_log(log, f"==> package {tag} via scripts/package_release.sh")
        if platforms.current().key == "snes":
            # snesrecomp's packager is configured by environment, not argv:
            # positional arguments would be silently ignored and it would
            # package whatever is in ./build.
            env = os.environ.copy()
            env["BUILD_DIR"] = str(bdir)
            env["PLATFORM"] = tag
            r = _run_stream([bash, str(script)], root, log=log, env=env)
        else:
            r = _run_stream([bash, str(script), str(bdir), tag], root, log=log)
        if not r.ok:
            return PackageResult(False, f"package_release.sh failed for {tag}", r.detail)
        zip_path = _newest_zip(dist, tag)
        if zip_path is None:
            return PackageResult(False, f"package_release.sh produced no zip under {dist}", r.detail)
        return PackageResult(True, f"Packaged {zip_path.name}", r.detail, zip_path)

    if n64 and script_ok:
        _flush_log(
            log,
            f"==> package {tag} (built-in stager; {script.name} is not an "
            "n64lle scaffold artifact and is not run)",
        )
    elif snes and script_ok:
        _flush_log(
            log,
            f"==> package {tag} (playable build; {script.name} packages setup "
            "hosts only, and this build dir is not one)",
        )
    else:
        _flush_log(log, f"==> package {tag} (built-in stager)")
    stage = dist / f"stage-local-{tag}"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    try:
        if snes:
            err = _stage_snes_local_bundle(root, binary, stage, log=log)
        elif n64:
            err = _stage_n64_local_bundle(root, binary, stage, log=log)
        else:
            err = _stage_local_bundle(root, binary, stage, log=log)
        if err:
            return PackageResult(False, err)
        # "-local" so a playable build is never mistaken for a release: a
        # setup pack from the repo script is <zip-prefix>-<ver>-<tag>.zip.
        # Every N64 zip is local — there is no release packager on that console
        # yet — so the suffix is never dropped there.
        suffix = "-local" if (snes or n64) else ""
        zip_path = dist / f"{_zip_prefix(root)}-{version}-{tag}{suffix}.zip"
        _write_zip(stage, zip_path)
    except OSError as exc:
        return PackageResult(False, f"Packaging failed: {exc}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    size_mb = zip_path.stat().st_size / (1024.0 * 1024.0)
    _flush_log(log, f"Wrote {zip_path} ({size_mb:.1f} MiB)")
    return PackageResult(True, f"Packaged {zip_path.name}", "", zip_path.resolve())
