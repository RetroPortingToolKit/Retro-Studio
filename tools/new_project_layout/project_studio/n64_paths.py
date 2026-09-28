"""Locate the n64lle wizard (templates + probe) Studio should drive.

The N64 counterpart to :mod:`snes_paths`, and the search order is the same for
the same reason: a migration is measured against the framework revision the
port is actually pinned to, so the project's own ``n64lle/`` submodule outranks
a checkout found anywhere else.

UNLIKE snes_paths, THERE IS NO VENDORED FALLBACK. Studio used to ship a copy of
n64lle's ``tools/new_project/`` for a packaged install with no checkout on
disk, and the copy went stale in the way that matters: n64lle made the HLE
graphics tier the default (``hle_tier = true``) while the copy still scaffolded
``false``, so a project cut from it started on the LLE path with nothing saying
so. A scaffold that cannot inherit a fix is the failure this repo keeps paying
for (see framework_build_script below), so with no checkout the answer is None
and every caller says so -- see MISSING_CHECKOUT.

WHAT IS DELIBERATELY NOT HERE. snes_paths carries a second half — "can the
snesrecomp this port is pinned to run the tools/regen.sh that wizard emits" —
because a SNES port owns a regen script that calls the framework CLI by name.
An n64lle port owns no such script: generation is a target in its own CMake
graph (``<slug>-generate``), driven by the framework libraries the pinned
submodule builds. There is no CLI subcommand vocabulary to skew, so there is
no gap to compute, and inventing one would be a check that cannot fail.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .paths import toolkit_dir

MARKER = Path("runtime") / "runtime.cmake"
_WIZARD_REL = Path("tools") / "new_project"


def _is_framework(root: Path) -> bool:
    return (root / MARKER).is_file()


def _env_root() -> Path | None:
    raw = (os.environ.get("N64LLE_ROOT") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if _is_framework(p) else None


def _candidates(game_root: Path | str | None):
    """Every n64lle checkout Studio may use, in precedence order."""
    env = _env_root()
    if env is not None:
        yield env
    if game_root:
        root = Path(str(game_root)).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        for cand in (root / "n64lle", root):
            if _is_framework(cand):
                yield cand
        # A game-package port has no submodule: its checkout is the one its
        # .n64lle/local.env names.
        raw = read_local_env(root).get("N64LLE_ROOT", "")
        if raw and _is_framework(Path(raw).expanduser()):
            yield Path(raw).expanduser()
    # …/retcomm-studio/tools/new_project_layout → …/GitHub/n64lle
    base = toolkit_dir()
    for parent in (base.parent.parent, base.parent.parent.parent):
        cand = parent / "n64lle"
        if _is_framework(cand):
            yield cand.resolve()


def n64lle_root(game_root: Path | str | None = None) -> Path | None:
    """A real n64lle checkout, or None."""
    return next(_candidates(game_root), None)


MISSING_CHECKOUT = (
    "no n64lle checkout found -- Studio drives n64lle's own tools/new_project/, "
    "and ships no copy of it. Set N64LLE_ROOT to an n64lle checkout, or clone "
    "n64lle beside retcomm-studio."
)


def wizard_dir(game_root: Path | str | None = None) -> Path | None:
    """n64lle's ``tools/new_project/`` (setup_project.sh / probe_rom.py /
    templates/), or None when there is no checkout to take it from."""
    # The first checkout that HAS one. A port pinned to an n64lle older than
    # tools/new_project/ is still a framework, but it has no wizard to drive,
    # so the next checkout down answers rather than nothing.
    for root in _candidates(game_root):
        live = root / _WIZARD_REL
        if (live / "setup_project.sh").is_file():
            return live
    return None


def wizard_source(game_root: Path | str | None = None) -> str:
    """Human-readable provenance for the log — which checkout is being driven."""
    d = wizard_dir(game_root)
    return f"checkout {d}" if d is not None else "none (no n64lle checkout)"


def _in_wizard(game_root: Path | str | None, rel: str) -> Path | None:
    d = wizard_dir(game_root)
    return d / rel if d is not None else None


# n64lle's own template-drift tool (tools/new_project/port_drift.py, 2026-09-23).
# The port's scaffold is rendered once and nothing brings it forward; this is
# what measures how far it has moved, with the port's own values, so Studio
# does not carry a second copy of "what the scaffold should look like".
DRIFT_TOOL_REL = _WIZARD_REL / "port_drift.py"


def drift_tools(game_root: Path | str):
    """Every ``(script, checkout, is_the_ports_own_pin)``, best first.

    A DIFFERENT precedence from wizard_dir, on purpose. The port's OWN n64lle
    comes first: its ``n64lle/`` submodule (even over $N64LLE_ROOT), or, on a
    game-package port, the checkout it builds against (port_framework: the
    environment, then .n64lle/local.env -- the same one its CMakeLists uses).
    Measured against that, the answer is the one the port's own
    ``<slug>_template_drift`` ctest gives, and applying it is safe. Any other
    checkout only PREVIEWS what a bump would bring -- its templates can name
    framework files the pinned n64lle does not have (the build shim execs
    n64lle/tools/build_framework.sh, which ports pinned before 2026-09-15 lack).

    All of them, not the first: a pinned copy can be too old to speak --json,
    and the caller then falls through to a preview instead of to nothing.
    """
    root = Path(str(game_root)).expanduser()
    try:
        root = root.resolve()
    except OSError:
        pass
    pf = port_framework(root)
    own = pf[0] if pf is not None else root / "n64lle"
    seen: set[Path] = set()
    order = ([own] if _is_framework(own) else []) + list(_candidates(root))
    for cand in order:
        try:
            key = cand.resolve()
        except OSError:
            key = cand
        if key in seen or not (cand / DRIFT_TOOL_REL).is_file():
            continue
        seen.add(key)
        yield cand / DRIFT_TOOL_REL, cand, key == own.resolve()


def drift_tool(game_root: Path | str) -> tuple[Path, Path, bool] | None:
    """The best of drift_tools(), or None."""
    return next(drift_tools(game_root), None)


def templates_dir(game_root: Path | str | None = None) -> Path | None:
    return _in_wizard(game_root, "templates")


def setup_script(game_root: Path | str | None = None) -> Path | None:
    return _in_wizard(game_root, "setup_project.sh")


def probe_rom_script(game_root: Path | str | None = None) -> Path | None:
    return _in_wizard(game_root, "probe_rom.py")


# ---------------------------------------------------------------------------
# Which n64lle a port builds against
# ---------------------------------------------------------------------------
# Two layouts exist, and a port says which it is by what it carries.
#
#   game package (2026-09-26 on) -- NO submodule. The port's gitignored
#     .n64lle/local.env names this machine's n64lle checkout (N64LLE_ROOT),
#     its framework build (N64LLE_BUILD, default <checkout>/build-n64lle), and
#     the core / runner / hub the port runs on. The port's CMakeLists reads it,
#     with a -D or an environment variable of the same name winning.
#   submodule (before) -- n64lle/ inside the port, built into
#     <port>/build-n64lle/.
#
# Everything below that used to assume the second asks port_framework() now.
LOCAL_ENV_REL = Path(".n64lle") / "local.env"
_LOCAL_LINE_RE = re.compile(r"^([A-Z0-9_]+)='([^']*)'\s*$")


def read_local_env(game_root: Path | str) -> dict[str, str]:
    """``.n64lle/local.env`` as a dict; empty when the port has none.

    Parsed with the port CMakeLists' own rule (``^[A-Z0-9_]+='[^']*'$``) rather
    than sourced, so a line CMake would ignore is ignored here too.
    """
    p = Path(str(game_root)).expanduser() / LOCAL_ENV_REL
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _LOCAL_LINE_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def port_setting(game_root: Path | str, var: str) -> tuple[str, str]:
    """``(value, where)`` for one port setting: environment, then local.env.

    The port's CMakeLists puts a -D cache value first; Studio has no cache to
    read before configure, so it starts at the environment. ``where`` is
    ``"environment"``, ``".n64lle/local.env"`` or ``""`` when unset.
    """
    env = (os.environ.get(var) or "").strip()
    if env:
        return env, "environment"
    val = read_local_env(game_root).get(var, "")
    return (val, ".n64lle/local.env") if val else ("", "")


def is_package_port(game_root: Path | str) -> bool:
    """A game-package port: local.env, or the template's game-shim call."""
    root = Path(str(game_root)).expanduser()
    if (root / LOCAL_ENV_REL).is_file():
        return True
    try:
        text = (root / "CMakeLists.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "n64lle_add_game_shim(" in text


def port_framework(game_root: Path | str) -> tuple[Path, Path] | None:
    """``(n64lle checkout, its framework build)`` this port builds against.

    None when the port names no checkout that exists -- a fresh clone of a
    game-package port before ``setup_project.sh --attach``.
    """
    root = Path(str(game_root)).expanduser()
    try:
        root = root.resolve()
    except OSError:
        pass
    sub = root / "n64lle"
    if not is_package_port(root) and _is_framework(sub):
        return sub, root / FRAMEWORK_BUILD_DIR
    fw_raw, _ = port_setting(root, "N64LLE_ROOT")
    if not fw_raw:
        return None
    fw = Path(fw_raw).expanduser()
    if not _is_framework(fw):
        return None
    build_raw, _ = port_setting(root, "N64LLE_BUILD")
    build = Path(build_raw).expanduser() if build_raw else fw / FRAMEWORK_BUILD_DIR
    return fw, build


MISSING_PORT_CHECKOUT = (
    "this port names no n64lle checkout (.n64lle/local.env is missing, or its "
    "N64LLE_ROOT is not a checkout). From your n64lle checkout run "
    "`sh tools/new_project/setup_project.sh --attach <port>`, or set N64LLE_ROOT."
)


# ---------------------------------------------------------------------------
# The out-of-tree framework build every n64lle port needs before it configures
# ---------------------------------------------------------------------------
# n64lle is NOT add_subdirectory()'d: a port's CMakeLists includes
# runtime/runtime.cmake and calls n64lle_runtime_resolve_framework(<root>,
# <build>), which looks for already-built libraries and tools under
# build-n64lle/. So "configure the project" has a prerequisite that "cmake -S ."
# does not state, and a port that skips it fails inside a resolve function
# rather than at the missing step. Naming it here is what lets buildops check
# for it first.
FRAMEWORK_BUILD_DIR = "build-n64lle"
FRAMEWORK_BUILD_SCRIPT = Path("tools") / "build_framework.sh"


def framework_build_dir(game_root: Path | str) -> Path:
    fw = port_framework(game_root)
    if fw is not None:
        return fw[1]
    return Path(str(game_root)).expanduser().resolve() / FRAMEWORK_BUILD_DIR


FRAMEWORK_OWNED_BUILD_SCRIPT = Path("n64lle") / "tools" / "build_framework.sh"


def framework_build_script(game_root: Path | str) -> Path | None:
    """The port's own ``tools/build_framework.sh``, or None.

    Scaffolded into every n64lle port (templates/build_framework.sh.in). It
    USED to be owned by the port, the way SNES's tools/regen.sh is, on the
    reasoning that it carries the workarounds that port needs for the framework
    revision it is pinned to.

    That reasoning was wrong in one direction and it was expensive. A private
    copy per port cannot inherit a fix: -DN64LLE_RSP_CENSUS=1 reached Mario
    Kart 64's copy on 2026-09-12 and none of the others, so seven of nine N64
    ports harvested no RSP microcode, compiled their generated-RSP tier to
    nothing, and ran the RSP fully interpreted with no build step saying a word.
    The template is now a SHIM onto the framework's copy, and this function is
    the fallback rather than the primary. See framework_owned_build_script.
    """
    p = Path(str(game_root)).expanduser().resolve() / FRAMEWORK_BUILD_SCRIPT
    return p if p.is_file() else None


def framework_owned_build_script(game_root: Path | str) -> Path | None:
    """The framework's own ``n64lle/tools/build_framework.sh``, or None.

    Present only on ports pinned to an n64lle from 2026-09-15 or later. When it
    is there it is the one to run: it is shared, so a fix lands once. On a
    game-package port it is the checkout's, found through port_framework().
    """
    fw = port_framework(game_root)
    if fw is None:
        return None
    p = fw[0] / FRAMEWORK_BUILD_SCRIPT
    return p if p.is_file() else None


def port_script_is_shim(game_root: Path | str) -> bool | None:
    """Does the port's copy simply delegate to the framework's?

    None when there is no port copy to judge. A shim is the expected state; a
    port copy that is NOT a shim is carrying build logic that no other port can
    inherit, which is the condition worth reporting.
    """
    p = framework_build_script(game_root)
    if p is None:
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Submodule ports exec n64lle/tools/build_framework.sh; game-package ports
    # exec "$N64LLE_ROOT/tools/build_framework.sh" (the checkout local.env names).
    return "exec" in text and (
        "n64lle/tools/build_framework.sh" in text
        or "$N64LLE_ROOT/tools/build_framework.sh" in text
    )


# What "built" means is not Studio's to define: n64lle_runtime_resolve_framework()
# enumerates the artifacts it will FATAL_ERROR on, and that list is the contract.
# It is read out of the port's own pinned runtime.cmake rather than copied here,
# because it grows — libn64lle-runtime-cosim.a and -xxhash.a are both newer than
# the first port — and a second copy would go stale in the direction that hurts:
# reporting a tree as built that cmake then rejects.
_RESOLVE_ARTIFACT_RE = re.compile(r'"\$\{BUILD\}/([^"\n]+)"')
_EXE_SUFFIX_RE = re.compile(r"\$\{CMAKE_EXECUTABLE_SUFFIX\}")


def framework_runtime_cmake(game_root: Path | str) -> Path | None:
    """The ``runtime/runtime.cmake`` of the n64lle this port builds against."""
    fw = port_framework(game_root)
    if fw is None:
        return None
    p = fw[0] / MARKER
    return p if p.is_file() else None


def framework_required_artifacts(game_root: Path | str) -> list[str]:
    """Paths under build-n64lle/ that resolve_framework() insists exist.

    Returned relative to the build dir, in the order runtime.cmake checks them,
    so the first missing one is the one cmake would name. Empty when the file
    cannot be read or its shape changed — callers fall back rather than treat
    "I could not tell" as "nothing is required".
    """
    cmake = framework_runtime_cmake(game_root)
    if cmake is None:
        return []
    try:
        text = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Only what the FATAL_ERROR loop actually checks. Everything ${BUILD}-
    # relative that comes AFTER it is output — N64LLE_ISA_INC is an include
    # DIRECTORY, and demanding it as a file reported a complete tree as
    # unbuilt. So the scan stops at the foreach that does the checking.
    body = text.split("function(n64lle_runtime_resolve_framework", 1)
    if len(body) < 2:
        return []
    head = body[1].split("foreach(", 1)[0]
    out: list[str] = []
    for raw in _RESOLVE_ARTIFACT_RE.findall(head):
        # ${CMAKE_EXECUTABLE_SUFFIX} is empty off Windows; the caller tries the
        # .exe spelling too, so drop the token rather than guess here.
        rel = _EXE_SUFFIX_RE.sub("", raw).strip()
        if rel and rel not in out:
            out.append(rel)
    return out


def _artifact_present(build: Path, rel: str) -> bool:
    p = build / rel
    return p.is_file() or p.with_suffix(p.suffix + ".exe").is_file()


def framework_missing_artifacts(game_root: Path | str) -> list[str]:
    """Which of those are absent, in runtime.cmake's own order."""
    build = framework_build_dir(game_root)
    if not build.is_dir():
        return framework_required_artifacts(game_root)
    return [
        rel
        for rel in framework_required_artifacts(game_root)
        if not _artifact_present(build, rel)
    ]


def framework_is_built(game_root: Path | str) -> bool:
    """Has build_framework.sh actually produced what resolve expects?

    Checked by artifact, not by "the directory exists": an aborted build leaves
    build-n64lle/ behind with a CMakeCache and nothing else, and treating that
    as built moves the failure into n64lle_runtime_resolve_framework().

    It checks the WHOLE list, and that is the fix for a real failure rather
    than tidiness. This used to look for n64emit alone, on the reasoning that
    the emitter is "the last thing the framework build produces". It is not:
    ninja builds the emitter and the runtime libraries in parallel, so a build
    that dies compiling runtime/src/rsp/rsp.c still leaves n64emit behind. The
    preflight then passed, configure ran, and cmake failed several files away
    naming libn64lle-runtime-devices.a — with Studio having just reported the
    prerequisite as satisfied.
    """
    build = framework_build_dir(game_root)
    if not build.is_dir():
        return False
    required = framework_required_artifacts(game_root)
    if required:
        return all(_artifact_present(build, rel) for rel in required)
    # runtime.cmake unreadable (no submodule checkout, or its shape changed).
    # Fall back to the emitter rather than claim either answer confidently.
    for name in ("n64emit", "n64emit.exe"):
        if list(build.rglob(name)):
            return True
    return False
