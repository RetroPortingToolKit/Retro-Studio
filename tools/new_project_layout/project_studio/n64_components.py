"""The three binaries an n64lle game package runs on, and their dev builds.

A game-package port (n64lle, 2026-09-26 on) builds ONE thing, the package
``<slug>_game.so``. Playing it takes three more, none of them the port's:

    core    n64lle_core.<ext>   n64lle's generic core, one build for every game
    runner  retro-core-runner   Retro-Runtime; loads the core, in a child process
    hub     retro-hub           Retro-Launcher; the window the player sees

    retro-hub --run-core <core> --package <slug>_game.so --rom <dump> ...
      -> retro-core-runner (child) -> n64lle_core -> <slug>_game.so

Which ones a port uses by DEFAULT is its ``.n64lle/local.env`` (written by
n64lle's setup_project.sh: fetched releases, or whatever it was told), resolved
at configure into ``<build>/run_game.env``. A DEV build is one made from a
source checkout on this machine by that checkout's OWN local-build script --
never a Studio reimplementation of it:

    core    <n64lle>/tools/build_core.sh           last line N64LLE_CORE_LIB=<path>
    runner  <Retro-Runtime>/scripts/build-local.sh last line RETRO_CORE_RUNNER=<path>
    hub     <Retro-Launcher>/scripts/build-local.sh last line RETRO_HUB=<path>

Each writes into ``<checkout>/out/local/<platform>/`` by default, which is how
a dev build is found again later without Studio keeping a record of its own
that could disagree with the disk.
"""

from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import n64_paths
from .gitops import CmdResult
from .paths import find_bash, toolkit_dir

LogFn = Callable[[str], None]


@dataclass(frozen=True)
class Component:
    key: str          # core | runner | hub
    label: str
    env_key: str      # the local.env / run_game.env / -D name
    checkout_env: str # names a source checkout explicitly
    repo: str         # for messages


COMPONENTS: dict[str, Component] = {
    "core": Component("core", "n64lle core", "N64LLE_CORE_LIB", "N64LLE_ROOT", "n64lle"),
    "runner": Component("runner", "retro-core-runner", "RETRO_CORE_RUNNER",
                        "RETRO_RUNTIME_ROOT", "Retro-Runtime"),
    "hub": Component("hub", "retro-hub", "RETRO_HUB", "RETRO_LAUNCHER_ROOT",
                     "Retro-Launcher"),
}


def _is_windows() -> bool:
    return os.name == "nt"


def host_platform() -> str:
    """``<os>-<arch>`` as the three build-local scripts name out/local/<it>/."""
    s = _platform.system().lower()
    os_name = {"darwin": "macos"}.get(s, s)
    m = _platform.machine().lower()
    arch = "arm64" if m in ("aarch64", "arm64") else "x86_64"
    return f"{os_name}-{arch}"


def binary_name(which: str) -> str:
    if which == "core":
        s = _platform.system()
        return "n64lle_core" + (".dll" if s == "Windows" else ".dylib" if s == "Darwin" else ".so")
    exe = ".exe" if _is_windows() else ""
    return ("retro-core-runner" if which == "runner" else "retro-hub") + exe


# ---------------------------------------------------------------------------
# Source checkouts
# ---------------------------------------------------------------------------

def _is_runtime_checkout(d: Path) -> bool:
    return (d / "scripts" / "build-local.sh").is_file() and (d / "runner").is_dir() \
        and (d / "corelink").is_dir()


def _is_launcher_checkout(d: Path) -> bool:
    # The same test n64lle's setup_project.sh applies (find_launcher_checkout):
    # a checkout that can build a hub AND carries the title-app kit.
    return (d / "scripts" / "build-local.sh").is_file() and \
        (d / "packaging" / "title" / "build-title-app.sh").is_file()


def _branch(d: Path) -> str:
    try:
        p = subprocess.run(["git", "-C", str(d), "branch", "--show-current"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (p.stdout or "").strip()


def _search_dirs(port_root: Path | str | None) -> list[Path]:
    """Where sibling checkouts are looked for: beside the port's n64lle, beside
    the port, and beside retcomm-studio -- in that order, deduplicated."""
    out: list[Path] = []
    if port_root:
        fw = n64_paths.port_framework(port_root)
        if fw is not None:
            out.append(fw[0].parent)
        out.append(Path(str(port_root)).expanduser().resolve().parent)
    fw_any = n64_paths.n64lle_root(port_root)
    if fw_any is not None:
        out.append(fw_any.parent)
    out.append(toolkit_dir().parent.parent.parent)
    seen: set[Path] = set()
    uniq: list[Path] = []
    for d in out:
        try:
            d = d.resolve()
        except OSError:
            continue
        if d not in seen and d.is_dir():
            seen.add(d)
            uniq.append(d)
    return uniq


def _find_sibling(port_root: Path | str | None, test) -> Path | None:
    """The first matching checkout on branch ``main``, else the first found."""
    best: Path | None = None
    for parent in _search_dirs(port_root):
        try:
            kids = sorted(p for p in parent.iterdir() if p.is_dir())
        except OSError:
            continue
        for d in kids:
            if not test(d):
                continue
            if _branch(d) == "main":
                return d
            if best is None:
                best = d
    return best


def checkout(which: str, port_root: Path | str | None) -> Path | None:
    """The source checkout a dev ``which`` is built from, or None.

    core: the n64lle checkout the PORT builds against (a dev core from any
    other checkout could carry a different module ABI than the package was
    emitted for). runner / hub: $RETRO_RUNTIME_ROOT / $RETRO_LAUNCHER_ROOT,
    else a sibling checkout, the one on ``main`` first.
    """
    comp = COMPONENTS[which]
    if which == "core":
        fw = n64_paths.port_framework(port_root) if port_root else None
        if fw is not None:
            return fw[0]
        return n64_paths.n64lle_root(port_root)
    test = _is_runtime_checkout if which == "runner" else _is_launcher_checkout
    raw = (os.environ.get(comp.checkout_env) or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p.resolve() if test(p) else None
    return _find_sibling(port_root, test)


def dev_output(which: str, src: Path | None) -> Path | None:
    """Where that checkout's local-build script puts ``which`` by default."""
    if src is None:
        return None
    return src / "out" / "local" / host_platform() / binary_name(which)


def dev_path(which: str, port_root: Path | str | None) -> Path | None:
    """The dev build of ``which``, when one exists on disk."""
    p = dev_output(which, checkout(which, port_root))
    return p if p is not None and p.is_file() else None


# ---------------------------------------------------------------------------
# Building one
# ---------------------------------------------------------------------------

def host_env() -> dict[str, str]:
    """This process's environment WITHOUT the retcomm toolchain pack.

    Studio puts the pack's bin/ first on PATH (studio_runner.cpp) and overlays
    its deps (buildops.toolchain_env) so PORTS build hermetically. These three
    scripts are not ports: each is "build for THIS machine" and picks its own
    compiler, exactly as it does from a terminal. Under the pack it gets the
    pack's clang, whose sysroot has no libgcc_eh -- and Retro-Runtime's
    build-local.sh links -static-libgcc on Linux, so its first try_compile
    failed (measured 2026-09-28). The trees they share are host-configured too:
    n64lle/build-n64lle and Retro-Launcher's build-local cache /usr/bin/cc.
    """
    from .buildops import toolchain_root

    env = os.environ.copy()
    pack = toolchain_root()
    if pack is None:
        return env
    pack = pack.resolve()

    def in_pack(raw: str) -> bool:
        try:
            p = Path(raw).expanduser().resolve()
        except (OSError, ValueError):
            return False
        return p == pack or pack in p.parents

    for key in ("PATH", "CMAKE_PREFIX_PATH"):
        if key in env:
            kept = [p for p in env[key].split(os.pathsep) if p and not in_pack(p)]
            if kept:
                env[key] = os.pathsep.join(kept)
            else:
                env.pop(key)
    for key in ("SDL3_DIR", "ZLIB_ROOT", "CC", "CXX", "RETCOMM_TOOLCHAIN_DIR"):
        if key in env and (key == "RETCOMM_TOOLCHAIN_DIR" or in_pack(env[key])):
            env.pop(key)
    return env


# Studio's own build tree for the runner and hub, beside the scripts' default
# build-local/. A terminal build and a Studio build then never share a CMake
# cache -- CMake keeps a tree's first compiler for good, so one tree configured
# under the wrong toolchain used to poison the other (2026-09-28: a Studio run
# left Retro-Runtime/build-local on the pack's clang). Both repos gitignore
# build*/. The output still goes to the scripts' default out/local/<platform>/,
# which is where dev_output() and Launch look.
#
# NOT the core: build_core.sh builds in the framework tree the port's game
# package LINKS (<checkout>/build-n64lle). A second tree would be a second full
# framework build, and a core built beside a package it was not linked with.
STUDIO_BUILD_DIR = "build-studio"


def build_tree(which: str, src: Path, *, debug: bool = False) -> Path:
    """The CMake tree Studio builds ``which`` in."""
    if which == "core":
        fw = n64_paths.FRAMEWORK_BUILD_DIR + ("-debug" if debug else "")
        return src / fw
    return src / (STUDIO_BUILD_DIR + ("-debug" if debug else ""))


def _stale_pack_cache(tree: Path) -> bool:
    """Is ``tree`` configured with the toolchain pack's compiler?

    CMake keeps a tree's compiler for good, so such a tree fails the same way
    even under the host's environment. Said by name instead.
    """
    from .buildops import toolchain_root

    pack = toolchain_root()
    if pack is None:
        return False
    try:
        text = (tree / "CMakeCache.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.startswith(("CMAKE_C_COMPILER:", "CMAKE_CXX_COMPILER:"))
               and str(pack.resolve()) in line for line in text.splitlines())


def _script_argv(which: str, src: Path) -> list[str] | None:
    if which == "core":
        sh, ps1 = src / "tools" / "build_core.sh", src / "tools" / "build_core.ps1"
    else:
        sh, ps1 = src / "scripts" / "build-local.sh", src / "scripts" / "build-local.ps1"
    if _is_windows() and ps1.is_file():
        ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)]
    if not sh.is_file():
        return None
    bash = find_bash()
    return [bash, str(sh)] if bash else None


def build_component(
    which: str,
    port_root: Path | str | None,
    *,
    debug: bool = False,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Run ``which``'s own local-build script; the message names the result.

    The script's last line is ``KEY=<absolute path>`` and that is the answer:
    each script checks its product (``--check-core``, ``game_package 1``,
    ``title_app 1``) before it prints the line, so a path means a usable build.
    """
    from .buildops import _run_stream  # the one streaming runner

    comp = COMPONENTS[which]
    src = checkout(which, port_root)
    if src is None:
        if which == "core":
            return CmdResult(False, "No n64lle checkout to build the core from — "
                                    + n64_paths.MISSING_PORT_CHECKOUT)
        return CmdResult(
            False,
            f"No {comp.repo} checkout found to build {comp.label} from. Clone it "
            f"beside n64lle, or set {comp.checkout_env} to one.",
        )
    argv = _script_argv(which, src)
    if argv is None:
        return CmdResult(False, f"{src} has no local-build script for {comp.label} "
                                "(or no bash to run it).")
    ps1 = argv[-1].endswith(".ps1")
    if debug:
        argv.append("-Debug" if ps1 else "--debug")
    tree = build_tree(which, src, debug=debug)
    if which != "core":
        argv += ["-Build" if ps1 else "--build", str(tree)]
    if dry_run:
        msg = "dry-run: " + " ".join(argv)
        if log:
            log(msg)
        return CmdResult(True, msg)
    env = host_env()
    if _stale_pack_cache(tree):
        return CmdResult(
            False,
            f"{tree} was configured with Studio's toolchain pack compiler, and CMake "
            "keeps a tree's compiler. Delete that folder -- a gitignored build tree -- "
            f"and press Build {which} again.")
    if log:
        log(f"--- Build {comp.label} (dev) from {src}, host toolchain ---")
    r = _run_stream(argv, src, log=log, env=env)
    if not r.ok:
        return CmdResult(False, f"Building {comp.label} failed — {r.message}", r.detail)
    prefix = comp.env_key + "="
    for line in reversed((r.detail or "").splitlines()):
        line = line.strip()
        if line.startswith(prefix):
            return CmdResult(True, f"Built {comp.label}: {line[len(prefix):]}", r.detail)
    return CmdResult(False, f"{comp.label} built, but the script printed no {prefix}<path> "
                            "line — read its output above.", r.detail)


# ---------------------------------------------------------------------------
# What a launch would use
# ---------------------------------------------------------------------------

def hub_direct_flags(hub: Path | str) -> set[str]:
    """The Direct-mode flags a retro-hub lists in ``--version``; empty if unknown."""
    try:
        proc = subprocess.run([str(hub), "--version"], capture_output=True, text=True,
                              timeout=15)
    except (OSError, subprocess.SubprocessError):
        return set()
    for line in (proc.stdout or "").splitlines():
        if line.startswith("direct_mode_flags "):
            return set(line.split()[1:])
    return set()


def read_run_game_env(build_dir: Path) -> dict[str, str]:
    """``<build>/run_game.env``: what the port's configure resolved."""
    p = build_dir / "run_game.env"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = n64_paths._LOCAL_LINE_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


_FROM_KEYS = {"core": "N64LLE_CORE_FROM", "runner": "RETRO_CORE_RUNNER_FROM",
              "hub": "RETRO_HUB_FROM"}


def default_for(which: str, port_root: Path, build_dir: Path) -> tuple[str, str]:
    """``(path, provenance)`` of the binary a plain launch uses.

    The configured build's run_game.env first -- it is what tools/run_game.sh
    will actually run, -D overrides included -- then local.env. Provenance is
    local.env's ``*_FROM`` (release / generate / manual / dev / build / none).
    """
    comp = COMPONENTS[which]
    local = n64_paths.read_local_env(port_root)
    frm = local.get(_FROM_KEYS[which], "")
    rge = read_run_game_env(build_dir)
    path = rge.get(comp.env_key, "")
    if path and path != local.get(comp.env_key, ""):
        frm = "build cache"
    if not path:
        path = local.get(comp.env_key, "")
    return path, frm


def status(port_root: Path, build_dir: Path) -> dict:
    """Every component: its checkout, dev build and default, for the Build tab."""
    port_root = Path(port_root).expanduser().resolve()
    out: dict = {"platform": host_platform(), "package_port": n64_paths.is_package_port(port_root)}
    for key, comp in COMPONENTS.items():
        src = checkout(key, port_root)
        dev = dev_output(key, src)
        dflt, frm = default_for(key, port_root, build_dir)
        out[key] = {
            "label": comp.label,
            "checkout": str(src) if src else "",
            "dev": str(dev) if dev else "",
            "dev_built": bool(dev and dev.is_file()),
            "default": dflt,
            "default_from": frm,
            "default_exists": bool(dflt) and Path(dflt).exists(),
        }
    rge = read_run_game_env(build_dir)
    out["game"] = {
        "package": rge.get("GAME_PACKAGE", ""),
        "built": bool(rge.get("GAME_PACKAGE")) and Path(rge["GAME_PACKAGE"]).is_file(),
        "configured": bool(rge),
        "rom": rge.get("ROM", ""),
    }
    return out
