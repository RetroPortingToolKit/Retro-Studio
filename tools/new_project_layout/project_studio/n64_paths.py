"""Locate the n64lle wizard (templates + probe) Studio should drive.

The N64 counterpart to :mod:`snes_paths`, and the search order is the same for
the same reason: a migration is measured against the framework revision the
port is actually pinned to, so the project's own ``n64lle/`` submodule outranks
anything Studio ships. The vendored copy under the toolkit is a last resort
that only exists so a packaged install can scaffold a *new* project with no
checkout on disk.

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


def n64lle_root(game_root: Path | str | None = None) -> Path | None:
    """A real n64lle checkout, or None."""
    env = _env_root()
    if env is not None:
        return env
    if game_root:
        root = Path(str(game_root)).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        for cand in (root / "n64lle", root):
            if _is_framework(cand):
                return cand
    # …/retcomm-studio/tools/new_project_layout → …/GitHub/n64lle
    base = toolkit_dir()
    for parent in (base.parent.parent, base.parent.parent.parent):
        cand = parent / "n64lle"
        if _is_framework(cand):
            return cand.resolve()
    return None


def vendored_wizard_dir() -> Path:
    """The copy shipped with Studio (see n64/VENDOR.md)."""
    return toolkit_dir() / "n64"


def wizard_dir(game_root: Path | str | None = None) -> Path:
    """Directory holding setup_project.sh / probe_rom.py / templates/."""
    root = n64lle_root(game_root)
    if root is not None:
        live = root / _WIZARD_REL
        if (live / "setup_project.sh").is_file():
            return live
    return vendored_wizard_dir()


def wizard_source(game_root: Path | str | None = None) -> str:
    """Human-readable provenance for the log — which copy is being driven."""
    d = wizard_dir(game_root)
    return "vendored" if d == vendored_wizard_dir() else f"checkout {d}"


def templates_dir(game_root: Path | str | None = None) -> Path:
    return wizard_dir(game_root) / "templates"


def setup_script(game_root: Path | str | None = None) -> Path:
    return wizard_dir(game_root) / "setup_project.sh"


def probe_rom_script(game_root: Path | str | None = None) -> Path:
    return wizard_dir(game_root) / "probe_rom.py"


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
    is there it is the one to run: it is shared, so a fix lands once.
    """
    p = Path(str(game_root)).expanduser().resolve() / FRAMEWORK_OWNED_BUILD_SCRIPT
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
    return "n64lle/tools/build_framework.sh" in text and "exec" in text


# What "built" means is not Studio's to define: n64lle_runtime_resolve_framework()
# enumerates the artifacts it will FATAL_ERROR on, and that list is the contract.
# It is read out of the port's own pinned runtime.cmake rather than copied here,
# because it grows — libn64lle-runtime-cosim.a and -xxhash.a are both newer than
# the first port — and a second copy would go stale in the direction that hurts:
# reporting a tree as built that cmake then rejects.
_RESOLVE_ARTIFACT_RE = re.compile(r'"\$\{BUILD\}/([^"\n]+)"')
_EXE_SUFFIX_RE = re.compile(r"\$\{CMAKE_EXECUTABLE_SUFFIX\}")


def framework_runtime_cmake(game_root: Path | str) -> Path | None:
    """The port's pinned ``n64lle/runtime/runtime.cmake``, or None."""
    p = Path(str(game_root)).expanduser().resolve() / "n64lle" / MARKER
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
