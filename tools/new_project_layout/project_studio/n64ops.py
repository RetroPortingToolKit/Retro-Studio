"""Audit / plan / apply for an N64 title repo.

The third implementation of the same shape, and a third one rather than a
parametrisation of either existing module for the same reason ``snesops``
gives: what the consoles share is the *shape* — audit produces checks, checks
name fix ops, plan orders them, apply runs them — and almost nothing else. The
PSX migration is about game.toml rewriting, disc probing and BIOS backends; the
SNES one is about ``tools/regen.sh`` and the framework CLI vocabulary it calls.
An n64lle port has neither: it owns no generation script, calls no framework
CLI, and its ``game.toml`` is a HAND-MAINTAINED contract that nothing in the
repo writes.

WHAT THIS MODULE WILL NOT WRITE, and why that is the design rather than a gap:

* ``game.toml`` — n64lle's scaffolder writes it once, from a probed ROM, and
  tags every row [MEASURED] / [DECLARED] / [UNKNOWN]. The file's own header
  says no program in the repo writes it, and that is exactly what makes those
  tags worth anything. A migration that regenerated it would be laundering
  Studio's guesses into a provenance record.
* ``CMakeLists.txt`` — the port's build graph, including the harvest window it
  mirrors out of game.toml. Rewriting it is how a port silently loses a
  workaround somebody measured.
* ``docs/STATUS.md`` — the honesty ledger. A freshly cut one asserts that
  nothing has been measured; dropping that over a port that HAS measured
  things would delete the findings and replace them with a claim of ignorance.
* ``README.md`` — the port's prose.

So the ops here are the mechanical ones: submodules, .gitignore, untracking
what must never be committed, and the small stub files the scaffolder writes
that carry no measurements. Anything that cannot be derived from the repository
as it stands is reported with no fix op rather than guessed at.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

from . import n64_paths, n64_toolchain
from .models import (
    ApplyResult,
    AuditReport,
    CheckResult,
    CheckStatus,
    LayoutClass,
    MigrateOptions,
    Plan,
    PlanStep,
    Severity,
)

FRAMEWORK = "n64lle"
FRAMEWORK_MARKER = "runtime/runtime.cmake"
PINS_FILE = "framework_pins.txt"

# Ordered for apply: submodules first (later ops read their contents), pins
# last (they record what everything above settled on).
OP_ORDER: tuple[str, ...] = (
    "n64_repair_framework_submodule",
    "n64_ensure_framework_submodule",
    "n64_ensure_recomp_ui_submodule",
    "n64_merge_gitignore",
    "n64_untrack_generated",
    "n64_untrack_roms",
    "n64_template_sync",
    "n64_template_take_cmakelists",
    "n64_emit_version",
    "n64_emit_build_framework",
    "n64_emit_cmakelists",
    "n64_emit_roms_readme",
    "n64_emit_generated_readme",
    "n64_emit_claude_md",
    "n64_record_framework_pins",
)

OP_TITLES: dict[str, str] = {
    "n64_repair_framework_submodule": "Repair broken n64lle/ git checkout",
    "n64_ensure_framework_submodule": "Add n64lle submodule",
    "n64_ensure_recomp_ui_submodule": "Add recomp-ui submodule",
    "n64_merge_gitignore": "Merge N64 .gitignore rules (roms, generated, settings)",
    "n64_untrack_generated": "Untrack committed generated C",
    "n64_untrack_roms": "Untrack committed ROM bytes",
    "n64_template_sync": "Sync template-owned files (n64lle port_drift.py)",
    "n64_template_take_cmakelists": "Take CMakeLists.txt from the pinned template (needs Force)",
    "n64_emit_version": "Emit VERSION",
    "n64_emit_build_framework": "Emit tools/build_framework.sh",
    "n64_emit_cmakelists": "Re-emit CMakeLists.txt from the pinned template",
    "n64_emit_roms_readme": "Emit roms/README.md",
    "n64_emit_generated_readme": "Emit generated/README.md",
    "n64_emit_claude_md": "Emit CLAUDE.md",
    "n64_record_framework_pins": f"Write {PINS_FILE}",
}

# Matches n64lle's tools/new_project/templates/gitignore.in. Two of these are
# load-bearing rather than tidiness:
#
#   roms/*        ROM bytes are NEVER committed. The scaffold SYMLINKS the
#                 user's dump in, so without this rule a `git add -A` commits
#                 either the link or (with core.symlinks off) the ROM itself.
#   generated/*   ROM-derived C, gitignored PENDING n64lle's own posture
#                 decision (docs/DISTRIBUTION-POSTURE.md is an explicit draft
#                 that disagrees with docs/05 §10). Until the owner rules,
#                 local is the reversible choice — so Studio enforces the
#                 reversible one and does not quietly pick a side.
GITIGNORE_RULES: tuple[str, ...] = (
    "roms/*",
    "!roms/README.md",
    "generated/*",
    "!generated/README.md",
    "settings.toml",
    "settings.toml.bad",
    "input.cfg",
    "keybinds.ini",
    "build*/",
    "captures/",
)

# Paths that must never be tracked, and the op that untracks each.
_GENERATED_PATHSPECS = ("generated/*.c", "generated/*.h", "generated/*.manifest.json")
_ROM_PATHSPECS = tuple(f"roms/*{ext}" for ext in (".z64", ".n64", ".v64"))


def list_ops() -> list[str]:
    return list(OP_ORDER)


# ---------------------------------------------------------------------------
# Reading the repo
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str) -> tuple[int, str]:
    # The Toolchain's git when the port records one (tools/toolchain.cmake),
    # so Migrate reads the repo with the same git its scaffolder used.
    git = n64_toolchain.for_root(root).get("git") or "git"
    try:
        proc = subprocess.run(
            [git, *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def _is_git_repo(root: Path) -> bool:
    code, out = _git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out == "true"


def _tracked(root: Path, pathspec: str) -> list[str]:
    code, out = _git(root, "ls-files", "--", pathspec)
    return [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []


_PROJECT_RE = re.compile(r"^\s*project\s*\(\s*([A-Za-z0-9_.+-]+)", re.MULTILINE)
_RUNTIME_TARGET_RE = re.compile(
    r"^\s*n64lle_add_runtime_target\s*\(\s*([A-Za-z0-9_.+-]+)", re.MULTILINE
)
_OUTPUT_NAME_RE = re.compile(r"OUTPUT_NAME\s+([A-Za-z0-9_.+-]+)")
_RESOLVE_RE = re.compile(r"n64lle_runtime_resolve_framework\s*\(")


def _cmake_text(root: Path) -> str:
    cml = root / "CMakeLists.txt"
    if not cml.is_file():
        return ""
    try:
        return cml.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def project_name(root: Path) -> str:
    m = _PROJECT_RE.search(_cmake_text(root))
    return m.group(1) if m else root.name


def runtime_target(root: Path) -> str:
    """``<slug>-runtime`` — read from the one call that defines it."""
    m = _RUNTIME_TARGET_RE.search(_cmake_text(root))
    return m.group(1) if m else ""


def project_slug(root: Path) -> str:
    tgt = runtime_target(root)
    return tgt[: -len("-runtime")] if tgt.endswith("-runtime") else ""


def executable_name(root: Path) -> str:
    text = _cmake_text(root)
    m = _RUNTIME_TARGET_RE.search(text)
    if m:
        tail = text[m.end() : m.end() + 600]
        om = _OUTPUT_NAME_RE.search(tail)
        if om:
            return om.group(1)
    return project_slug(root)


def default_branch(root: Path) -> str:
    code, out = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out if code == 0 and out else "main"


def read_contract(root: Path) -> dict:
    """``game.toml`` as parsed data, or ``{}``.

    Read, never written. It is the port's declared contract and the only place
    the ROM identity is recorded in-repo.
    """
    toml = Path(root) / "game.toml"
    if not toml.is_file():
        return {}
    try:
        with toml.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# The [game] rows n64lle's scaffolder tags [MEASURED]. A contract missing any
# of them cannot verify the dump it was cut against.
_IDENTITY_KEYS = (
    "cartid", "region", "revision", "entry_pc", "rom_size",
    "byte_order", "cic", "cic_seed", "crc1", "crc2", "sha256",
)


def contract_identity(root: Path) -> dict[str, str]:
    """The measured ROM identity rows present in ``game.toml``."""
    game = read_contract(root).get("game") or {}
    out: dict[str, str] = {}
    for key in _IDENTITY_KEYS:
        val = game.get(key)
        if val is not None and str(val).strip():
            out[key] = str(val).strip()
    return out


def _submodule_present(root: Path, path: str, marker: str = "") -> tuple[bool, bool]:
    """``(declared in .gitmodules, actually checked out)``."""
    declared = False
    mods = root / ".gitmodules"
    if mods.is_file():
        try:
            declared = f"path = {path}" in mods.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            declared = False
    sub = root / path
    live = sub.is_dir() and (not marker or (sub / marker).is_file())
    if not marker:
        live = sub.is_dir() and any(sub.iterdir())
    return declared, live


def diagnose_framework_checkout(root: Path) -> str | None:
    """Why an n64lle/ directory that exists is not a usable checkout."""
    sub = root / FRAMEWORK
    if not sub.is_dir():
        return None
    if not (sub / FRAMEWORK_MARKER).is_file():
        return (
            f"{FRAMEWORK}/ exists but has no {FRAMEWORK_MARKER} — a port "
            "includes that file and cannot configure without it."
        )
    if not (sub / ".git").exists() and not (root / ".git" / "modules" / FRAMEWORK).exists():
        return (
            f"{FRAMEWORK}/ is a plain directory, not a git checkout, so its "
            "revision cannot be pinned or advanced."
        )
    return None


# ---------------------------------------------------------------------------
# Template drift: n64lle's port_drift.py is the authority
# ---------------------------------------------------------------------------
# The checks below this section used to BE Studio's idea of the scaffold: a
# hand-kept list of .gitignore rules, a -D option diff for the build script,
# presence tests for the READMEs. Each was a second copy of the template, and
# the copies went stale in exactly the way that costs: the rule list never
# learned `overlays/*`, so a port that captured an overlay would commit ROM-
# derived code while this tab reported .gitignore as passing.
#
# n64lle now measures that itself (tools/new_project/port_drift.py): it renders
# the templates with the port's own values and reports what differs, by class
# -- owned files it may rewrite, CMakeLists.txt whose CODE it compares, and
# game.toml whose sections and keys it compares but NEVER writes. When the tool
# is reachable this tab reports its verdict and the hand-kept checks it
# replaces stand down; when it is not, they remain, and a row says why.
#
# Files whose verdict the drift tool owns once it has run against the pin.
_DRIFT_SUPERSEDES = frozenset({"build_framework", "gitignore", "roms_readme",
                               "generated_readme"})
_BUMP_HINT = ("Advance the n64lle pin first (Git tab), then re-audit: the "
              "templates must be the ones the port builds against.")


def measure_drift(root: Path, *extra: str) -> tuple[dict | None, str]:
    """port_drift.py's JSON for this port, and a reason when there is none.

    The returned dict carries keys of Studio's own: ``_checkout`` (which
    n64lle rendered the templates), ``_builds_here`` (it is the framework the
    port's build uses) and ``_own`` (True only when it is BOTH that framework
    and the port's pin -- its submodule, or a worktree at the gitlink -- the
    only case in which applying is safe)."""
    tried: list[str] = []
    for script, checkout, own in n64_paths.drift_tools(root):
        try:
            # port_drift.py is n64lle's, so it runs under the port's chosen
            # Python -- the one the port's own <slug>_template_drift ctest
            # runs it with (-DPython3_EXECUTABLE) -- not Studio's interpreter.
            proc = subprocess.run(
                [n64_toolchain.python_exe(n64_toolchain.for_root(root)),
                 str(script), str(root), "--json", *extra],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            tried.append(f"{checkout}: did not run ({exc})")
            continue
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
            tried.append(f"{checkout}: {tail[0]}")
            continue
        data["_checkout"] = str(checkout)
        # The port's own submodule is always its pin. Any other checkout is the
        # pin too when it is the framework the port BUILDS against and the tool
        # itself says the port's pin is that checkout's revision -- a framework
        # worktree at the gitlink, which is how the family works on framework
        # and port together. Anything else stays a preview.
        pin = str(data.get("pin_rev") or "?")
        builds_here = _same_dir(checkout, n64_paths.framework_root(root))
        data["_builds_here"] = builds_here
        data["_own"] = (bool(own) and builds_here) or (
            builds_here and pin != "?" and pin == str(data.get("templates_rev") or ""))
        data["_tried"] = tried
        return data, ""
    if tried:
        return None, "port_drift.py did not answer: " + "; ".join(tried) + ". " + _BUMP_HINT
    return None, ("No n64lle checkout carries tools/new_project/port_drift.py "
                  "(added 2026-09-23). " + _BUMP_HINT + " Or point N64LLE_ROOT "
                  "at a current n64lle to preview.")


def _same_dir(a: Path | str, b: Path | str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _drift_rows(drift: dict, add) -> None:
    """One audit row per drift item. Preview rows carry no fix op."""
    own = drift["_own"]
    src = f"n64lle {drift.get('templates_rev', '?')} ({drift['_checkout']})"
    if own:
        add("drift_source", "Template drift: measured", CheckStatus.PASS, Severity.INFO,
            f"Against {src}, the port's own pin -- the same verdict its "
            "<slug>_template_drift ctest gives.")
    elif drift.get("_builds_here"):
        add("drift_source", "Template drift: PREVIEW", CheckStatus.WARN, Severity.INFO,
            f"This port builds against {src} but pins n64lle "
            f"{drift.get('pin_rev', '?')}. These rows are that framework's "
            "templates -- what the build uses and the port's drift ctest "
            "reports -- but the committed pin is a different revision, so "
            "nothing below is applied. Advance the pin to the framework you "
            "build against (Git tab), then re-audit.")
    else:
        add("drift_source", "Template drift: PREVIEW", CheckStatus.WARN, Severity.INFO,
            f"The pinned n64lle ({drift.get('pin_rev', '?')}) has no port_drift.py, so "
            f"these rows are measured against {src}: what a bump would bring, not "
            "this port's state. Nothing below is applied in preview. " + _BUMP_HINT)
    for t in drift.get("_tried", []):
        add("drift_note", "Template drift: skipped a checkout", CheckStatus.PASS,
            Severity.INFO, t)
    for note in drift.get("notes", []):
        add("drift_note", "Template drift note", CheckStatus.PASS, Severity.INFO, note)

    prefix = "" if own else "After bump: "
    for it in drift.get("items", []):
        cls, path, st = it.get("class"), it.get("path"), it.get("state")
        if cls == "contract":
            where = f"[{it.get('section')}]" + (f" {it['key']}" if it.get("key") else "")
            add(f"drift:game.toml:{where}", f"{prefix}game.toml {where}",
                CheckStatus.WARN, Severity.RECOMMENDED,
                "The template has this and game.toml does not. Studio never writes "
                "game.toml (the hand-maintained contract), so paste it and decide "
                "the value for THIS title -- a template default is not a measurement:\n"
                + it.get("paste", ""))
            continue
        cid, title = f"drift:{path}", f"{prefix}{path}"
        if st in ("ok", "comments"):
            add(cid, title, CheckStatus.PASS, Severity.RECOMMENDED,
                "comments differ; code matches the template" if st == "comments" else "")
            continue
        if st == "unknown":
            add(cid, title, CheckStatus.WARN, Severity.RECOMMENDED,
                (it.get("detail") or "cannot render") + ". No fix op: a template "
                "rendered with a blank would read as drift.")
            continue
        size = ("missing" if st == "missing"
                else f"+{it.get('plus', 0)} -{it.get('minus', 0)} lines")
        if cls == "owned":
            # .gitignore is the one owned file whose drift can leak ROM-derived
            # bytes into git (captured overlays), so it is not a style warning.
            status = CheckStatus.FAIL if path == ".gitignore" else CheckStatus.WARN
            sev = Severity.REQUIRED if path == ".gitignore" else Severity.RECOMMENDED
            add(cid, title, status, sev,
                f"{size} against the template (template-owned: re-rendered whole).",
                "n64_template_sync" if own else None)
        else:
            add(cid, title, CheckStatus.WARN, Severity.RECOMMENDED,
                f"{size} of CODE against the template (comments are the port's "
                "and are not compared). Taking it overwrites the file -- read the "
                "diff first if this port carries its own build logic; tick Force.",
                "n64_template_take_cmakelists" if own else None)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def audit_project(root: Path, options: MigrateOptions | None = None) -> AuditReport:
    options = options or MigrateOptions()
    root = Path(root).expanduser().resolve()
    checks: list[CheckResult] = []
    notes: list[str] = []

    def add(
        cid: str,
        title: str,
        status: CheckStatus,
        severity: Severity,
        detail: str = "",
        fix_op: str | None = None,
    ) -> None:
        checks.append(
            CheckResult(
                id=cid, title=title, status=status, severity=severity,
                detail=detail, fix_op=fix_op,
            )
        )

    is_git = _is_git_repo(root)
    if not is_git:
        notes.append("Not a git repository — submodule and untrack ops cannot run.")

    tpl_drift, drift_why = measure_drift(root) if (root / "game.toml").is_file() \
        else (None, "No game.toml -- not a scaffolded port, nothing to measure.")
    superseded = _DRIFT_SUPERSEDES if (tpl_drift and tpl_drift["_own"]) else frozenset()

    # --- framework ----------------------------------------------------------
    declared, live = _submodule_present(root, FRAMEWORK, FRAMEWORK_MARKER)
    broken = diagnose_framework_checkout(root) if (root / FRAMEWORK).is_dir() else None
    # The build may use another checkout (see "n64lle used by the build"
    # below). When it does and that checkout is real, an uninitialised
    # submodule does not stop this port building -- it stops it building
    # WITHOUT the override, which is worth a warning, not a failure.
    fw_build = n64_paths.framework_root(root)
    elsewhere = (not _same_dir(fw_build, root / FRAMEWORK)
                 and (fw_build / FRAMEWORK_MARKER).is_file())
    if not live and declared and elsewhere:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.WARN, Severity.RECOMMENDED,
            f"Declared in .gitmodules but not initialised. The build uses {fw_build} "
            f"(from {n64_paths.framework_root_source(root)}), so it still "
            "configures; without that override it would not.",
            "n64_ensure_framework_submodule")
    elif live and broken:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.FAIL, Severity.REQUIRED,
            broken + " Repair re-clones it as a real submodule.",
            "n64_repair_framework_submodule")
    elif live:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.PASS, Severity.REQUIRED,
            str(root / FRAMEWORK))
    elif declared:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.FAIL, Severity.REQUIRED,
            f"Declared in .gitmodules but not initialised (or missing {FRAMEWORK_MARKER}).",
            "n64_ensure_framework_submodule")
    else:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.FAIL, Severity.REQUIRED,
            "No n64lle submodule — the project cannot generate or build.",
            "n64_ensure_framework_submodule")

    ui_declared, ui_live = _submodule_present(root, "recomp-ui")
    if ui_live:
        add("recomp_ui", "recomp-ui/ checkout", CheckStatus.PASS, Severity.RECOMMENDED, "")
    else:
        add("recomp_ui", "recomp-ui/ checkout", CheckStatus.WARN, Severity.RECOMMENDED,
            "Declared but not initialised." if ui_declared else "Not present (optional).",
            "n64_ensure_recomp_ui_submodule")

    # Which n64lle the BUILD uses. Usually the submodule above; a port can be
    # built against a framework worktree instead ($N64LLE_ROOT, or the
    # N64LLE_ROOT its build tree was configured with), and then the submodule
    # row is about the committed pin while this one is about the build.
    fw = n64_paths.framework_root(root)
    if not _same_dir(fw, root / FRAMEWORK):
        if (fw / FRAMEWORK_MARKER).is_file():
            add("framework_build", "n64lle used by the build", CheckStatus.PASS,
                Severity.INFO,
                f"{fw} (from {n64_paths.framework_root_source(root)}), not the "
                "submodule. Framework checks below read this checkout.")
        else:
            add("framework_build", "n64lle used by the build", CheckStatus.FAIL,
                Severity.REQUIRED,
                f"{fw} (from {n64_paths.framework_root_source(root)}) has no "
                f"{FRAMEWORK_MARKER}. Point N64LLE_ROOT at an n64lle checkout "
                "or reconfigure against the submodule.")

    # The Rust toolchain, when the framework is built with cargo (n64lle
    # rust-parity onwards). The PORT's own build runs cargo too -- the host
    # staticlib and the bench/cosim drivers -- so this is a port requirement,
    # not only a framework one.
    if (fw / FRAMEWORK_MARKER).is_file() and n64_paths.framework_needs_cargo(fw):
        chosen_cargo = n64_toolchain.for_root(root).get("cargo")
        rust = n64_paths.rust_toolchain_problem(fw, chosen_cargo)
        chan = n64_paths.rust_channel(fw)
        if rust is None:
            add("rust_toolchain", "Rust toolchain (cargo)", CheckStatus.PASS,
                Severity.REQUIRED,
                f"{chosen_cargo or n64_paths.find_cargo()}"
                + (f"; the pin asks for {chan}" if chan else ""))
        else:
            add("rust_toolchain", "Rust toolchain (cargo)", CheckStatus.FAIL,
                Severity.REQUIRED, rust + " No fix op: installing a toolchain "
                "is the machine's owner's call.")

    # The Build tab's Toolchain: every tool this port's n64lle commands will be
    # handed, checked the way the Build tab checks it (exists, runs, version).
    # WARN rather than FAIL: a missing gh or an unrecognised compiler banner
    # does not stop a migration, and the Build tab is where each is fixed.
    tc_bad = [f"{s.label}: {s.error}" for s in n64_toolchain.resolve(root)
              if s.error and s.key != "gh"]
    if tc_bad:
        add("toolchain", "Toolchain (Build tab)", CheckStatus.WARN, Severity.RECOMMENDED,
            "; ".join(tc_bad) + f". Fix them in Build > Toolchain; they are recorded "
            f"in {n64_toolchain.PROJECT_FILE.as_posix()}.")
    else:
        rec = n64_toolchain.read_project(root)
        add("toolchain", "Toolchain (Build tab)", CheckStatus.PASS, Severity.INFO,
            (f"{len(rec)} tool(s) recorded in {n64_toolchain.PROJECT_FILE.as_posix()}"
             if rec else "nothing recorded; n64lle discovers every tool on PATH"))

    # n64lle vendors ares and rabbitizer, but its own build initialises them
    # and Studio does not manage their pins. Recorded as a passing INFO row so
    # the absence of a "nested libs" line is not read as an oversight.
    add("nested", "Nested libs inside n64lle", CheckStatus.PASS, Severity.INFO,
        "n64lle carries no recomp-net / retcomm-rbengine; its own vendored "
        "submodules (ares, rabbitizer) are the framework build's to initialise.")

    # --- the framework build ------------------------------------------------
    # Not a style check: a port resolves n64lle as a PRE-BUILT tree, so without
    # this script there is no supported way to produce what CMake looks for.
    script = root / n64_paths.FRAMEWORK_BUILD_SCRIPT
    if "build_framework" in superseded:
        pass  # port_drift.py's row for tools/build_framework.sh is the verdict
    elif script.is_file():
        detail = str(n64_paths.FRAMEWORK_BUILD_SCRIPT)
        drift = build_framework_option_drift(root, options)
        if not script.stat().st_mode & 0o111:
            add("build_framework", "tools/build_framework.sh", CheckStatus.WARN,
                Severity.RECOMMENDED, detail + " is not executable.",
                "n64_emit_build_framework")
        elif drift:
            # The script is rendered ONCE, at scaffold time, and then owned by
            # the port — so a port cut last month builds this month's framework
            # with last month's switches, silently. That is not hypothetical:
            # -DN64LLE_RINGS_PROFILE=prod arrived after the first ports were
            # cut, and without it a player's build uses the framework's "dev"
            # ring capacities (an 8,388,608-record fntrace window, ~320 MiB
            # resident) instead of the ~2.5 MiB a port is supposed to ship.
            #
            # Reported as drift in the OPTIONS, not as "the file differs": a
            # port is allowed to own this script, and several legitimately add
            # flags of their own. What it may not do is silently LOSE one the
            # framework it is pinned to now expects.
            add("build_framework", "tools/build_framework.sh", CheckStatus.WARN,
                Severity.RECOMMENDED,
                detail + " predates the pinned framework's template: it never "
                "passes " + ", ".join(drift) + ". Re-emitting overwrites the "
                "port's copy, so read it first if this port customised it "
                "(--force / tick the op to apply).",
                "n64_emit_build_framework")
        else:
            add("build_framework", "tools/build_framework.sh", CheckStatus.PASS,
                Severity.REQUIRED, detail)
    else:
        add("build_framework", "tools/build_framework.sh", CheckStatus.FAIL,
            Severity.REQUIRED,
            "Missing. A port includes n64lle/runtime/runtime.cmake and resolves "
            "the framework from build-n64lle/, so it cannot configure until "
            "something builds n64lle out of tree.",
            "n64_emit_build_framework")

    # --- scaffold vs the framework it is pinned to ---------------------------
    gone = missing_framework_sources(root)
    unset = vanished_framework_variables(root)
    if gone or unset:
        parts = []
        if gone:
            parts.append(
                "Names " + ", ".join(f"n64lle/{g}" for g in gone) + ", which the "
                "pinned n64lle does not have. add_executable() on a missing "
                "source is a CMake generate error, so this port cannot "
                "configure at all.")
        if unset:
            parts.append(
                "Reads " + ", ".join("${" + v + "}" for v in unset) + ", which "
                "nothing in the pinned n64lle sets any more, so each expands to "
                "an empty string: a source path of \"/n64emit_support.c\", an "
                "include directory of \"\". (n64lle rust-parity removed "
                "N64LLE_SUPPORT and N64LLE_ISA_INC with the C headers: the "
                "emitters now write the headers beside the generated C, and "
                "the drivers come from n64lle_add_driver_target().)")
        add("cmake_framework_sources", "CMakeLists.txt framework sources",
            CheckStatus.FAIL, Severity.REQUIRED,
            " ".join(parts) + " The scaffolder's current template already "
            "handles it; this copy was rendered before that. Re-emit it "
            "(--force) rather than editing the game repo — a hand edit cannot "
            "inherit the next template fix.",
            "n64_template_take_cmakelists" if superseded else "n64_emit_cmakelists")
    else:
        add("cmake_framework_sources", "CMakeLists.txt framework sources",
            CheckStatus.PASS, Severity.REQUIRED, "")

    # --- the contract -------------------------------------------------------
    ident = contract_identity(root)
    if not (root / "game.toml").is_file():
        add("contract", "game.toml", CheckStatus.FAIL, Severity.REQUIRED,
            "Missing. The host reads it at startup and the build has no other "
            "source for this title's identity. It is written once by "
            "n64lle's setup_project.sh from a probed ROM and hand-maintained "
            "after that — Studio will not synthesise one.")
    elif not read_contract(root):
        add("contract", "game.toml", CheckStatus.FAIL, Severity.REQUIRED,
            "Present but not parseable as TOML — the host would fail "
            "--check-config. Fix it by hand; nothing here rewrites it.")
    else:
        missing = [k for k in _IDENTITY_KEYS if k not in ident]
        if missing:
            add("contract", "game.toml ROM identity", CheckStatus.WARN,
                Severity.REQUIRED,
                "[game] is missing " + ", ".join(missing) +
                " — nothing can verify the dump this port was cut against. "
                "Re-probe the ROM with the wizard's probe_rom.py and fill "
                "them in by hand; a guessed [MEASURED] row is worse than none.")
        else:
            add("contract", "game.toml ROM identity", CheckStatus.PASS,
                Severity.REQUIRED,
                f"{ident.get('cartid', '?')} / {ident.get('cic', '?')} / "
                f"sha256 {ident.get('sha256', '')[:12]}…")

    # --- the build graph ----------------------------------------------------
    text = _cmake_text(root)
    if not text:
        add("cmake", "CMakeLists.txt", CheckStatus.FAIL, Severity.REQUIRED,
            "Missing — this is not a buildable port.")
    else:
        tgt = runtime_target(root)
        calls = len(_RUNTIME_TARGET_RE.findall(text))
        if calls == 1:
            add("cmake", "n64lle_add_runtime_target()", CheckStatus.PASS,
                Severity.REQUIRED, f"{tgt} (executable: {executable_name(root)})")
        elif calls == 0:
            add("cmake", "n64lle_add_runtime_target()", CheckStatus.WARN,
                Severity.REQUIRED,
                "Not called. The host executable comes from exactly one such "
                "call; without it this repo builds gates and tools but no "
                "playable target. No fix op — wiring a build is not a "
                "mechanical edit.")
        else:
            add("cmake", "n64lle_add_runtime_target()", CheckStatus.WARN,
                Severity.REQUIRED,
                f"Called {calls} times. One port, one runtime target.")
        if not _RESOLVE_RE.search(text):
            add("cmake_resolve", "n64lle_runtime_resolve_framework()",
                CheckStatus.WARN, Severity.REQUIRED,
                "Not called. n64lle is included as a pre-built tree, not "
                "add_subdirectory()'d; without the resolve call N64LLE_LIBS "
                "and the emitter paths are unset.")

    # --- the host that should not be here -----------------------------------
    # n64lle's scaffolder states this as its headline invariant: the executable
    # is ONE n64lle_add_runtime_target() call, and the framework owns every
    # line of the host. The first N64 port carried ~1,500 lines of host C, of
    # which ~1,300 had nothing to do with that game — and a port that keeps its
    # own copy stops inheriting host fixes on a submodule bump.
    host = root / "host"
    if host.is_dir():
        srcs = sorted(p for p in host.glob("*.c"))
        add("host_dir", "host/ carries a private copy of the host",
            CheckStatus.WARN, Severity.RECOMMENDED,
            f"{len(srcs)} C file(s) under host/. The scaffolded layout has no "
            "host/ at all: the launcher, input, audio and run loop live in "
            "n64lle (crates/n64lle-host since the Rust migration, "
            "runtime/host before it) and reach every port on a submodule bump. "
            "No fix op — deleting a port's host is a decision with a "
            "measurement behind it, not a mechanical sweep.")

    # --- what must never be committed ---------------------------------------
    gen_tracked = [f for spec in _GENERATED_PATHSPECS for f in _tracked(root, spec)]
    if not is_git:
        add("generated", "Generated C not committed", CheckStatus.SKIP,
            Severity.REQUIRED, "Not a git repository.")
    elif gen_tracked:
        add("generated", "Generated C not committed", CheckStatus.FAIL,
            Severity.REQUIRED,
            f"{len(gen_tracked)} tracked file(s) derived from the ROM, while "
            "n64lle's distribution posture is still an open draft.",
            "n64_untrack_generated")
    else:
        add("generated", "Generated C not committed", CheckStatus.PASS,
            Severity.REQUIRED, "")

    rom_tracked = [f for spec in _ROM_PATHSPECS for f in _tracked(root, spec)]
    if not is_git:
        add("roms", "No ROM bytes committed", CheckStatus.SKIP, Severity.REQUIRED,
            "Not a git repository.")
    elif rom_tracked:
        add("roms", "No ROM bytes committed", CheckStatus.FAIL, Severity.REQUIRED,
            f"{len(rom_tracked)} tracked dump(s): " + ", ".join(rom_tracked[:3]),
            "n64_untrack_roms")
    else:
        add("roms", "No ROM bytes committed", CheckStatus.PASS, Severity.REQUIRED, "")

    # --- .gitignore ---------------------------------------------------------
    gi = root / ".gitignore"
    have = ""
    if gi.is_file():
        try:
            have = gi.read_text(encoding="utf-8", errors="replace")
        except OSError:
            have = ""
    existing = {ln.strip() for ln in have.splitlines()}
    missing_rules = [r for r in GITIGNORE_RULES if r not in existing]
    if "gitignore" in superseded:
        pass  # port_drift.py compares the whole file against the template
    elif not missing_rules:
        add("gitignore", ".gitignore rules", CheckStatus.PASS, Severity.REQUIRED, "")
    else:
        add("gitignore", ".gitignore rules", CheckStatus.FAIL, Severity.REQUIRED,
            "Missing: " + ", ".join(missing_rules), "n64_merge_gitignore")

    # --- scaffolded stubs ---------------------------------------------------
    for cid, rel, op, sev in (
        ("version", "VERSION", "n64_emit_version", Severity.RECOMMENDED),
        ("roms_readme", "roms/README.md", "n64_emit_roms_readme", Severity.RECOMMENDED),
        ("generated_readme", "generated/README.md", "n64_emit_generated_readme",
         Severity.RECOMMENDED),
        ("claude_md", "CLAUDE.md", "n64_emit_claude_md", Severity.OPTIONAL),
    ):
        if cid in superseded:
            continue
        if (root / rel).is_file():
            add(cid, rel, CheckStatus.PASS, sev, "")
        else:
            add(cid, rel, CheckStatus.WARN, sev, "Missing from the scaffold.", op)

    # docs/STATUS.md deliberately has NO fix op. A scaffolded one asserts that
    # nothing has been measured yet; writing that over a port that has measured
    # things would replace findings with a claim of ignorance, and writing it
    # into a port that has not is prose only its author can supply.
    if (root / "docs" / "STATUS.md").is_file():
        add("status_doc", "docs/STATUS.md", CheckStatus.PASS, Severity.RECOMMENDED, "")
    else:
        add("status_doc", "docs/STATUS.md", CheckStatus.WARN, Severity.RECOMMENDED,
            "Missing. The scaffold's honesty ledger — what has and has not "
            "been established for this title. Studio will not emit one: a "
            "fresh ledger asserts that nothing is measured, which is a claim "
            "about this port that only its author can make.")

    # --- CI -----------------------------------------------------------------
    add("ci", "Release workflow", CheckStatus.SKIP, Severity.INFO,
        "n64lle ships no release-workflow or packager template, so there is "
        "nothing to emit. Package a local build from the Build tab; file the "
        "missing template against n64lle rather than hand-writing one here.")

    # --- template drift (n64lle's own measurement) --------------------------
    if tpl_drift is None:
        add("drift_source", "Template drift", CheckStatus.SKIP, Severity.RECOMMENDED,
            drift_why + " The hand-kept checks above stand in until it can run.")
    else:
        _drift_rows(tpl_drift, add)

    # --- pins ---------------------------------------------------------------
    pins = root / PINS_FILE
    if not pins.is_file():
        add("pins", PINS_FILE, CheckStatus.WARN, Severity.OPTIONAL,
            "Not recorded.", "n64_record_framework_pins")
    else:
        stale = _stale_pins(root, pins)
        if stale:
            add("pins", PINS_FILE, CheckStatus.WARN, Severity.OPTIONAL,
                "Stale: " + ", ".join(stale), "n64_record_framework_pins")
        else:
            add("pins", PINS_FILE, CheckStatus.PASS, Severity.OPTIONAL, "")

    layout = _classify(checks, live or elsewhere)
    return AuditReport(
        root=str(root),
        layout=layout,
        project_name=project_name(root),
        boot_exe=executable_name(root) or None,
        checks=checks,
        notes=notes,
    )


def _classify(checks: list[CheckResult], have_framework: bool) -> LayoutClass:
    if not have_framework:
        return LayoutClass.UNKNOWN
    required_fail = any(
        c.status == CheckStatus.FAIL and c.severity == Severity.REQUIRED
        for c in checks
    )
    return LayoutClass.LEGACY_PACKAGING if required_fail else LayoutClass.SCAFFOLD_COMPLETE


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------
def _current_pins(root: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for path in (FRAMEWORK, "recomp-ui"):
        sub = root / path
        if sub.is_dir():
            code, out = _git(sub, "rev-parse", "HEAD")
            if code == 0 and out:
                pins[path] = out
    return pins


def _stale_pins(root: Path, pins_file: Path) -> list[str]:
    try:
        text = pins_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["unreadable"]
    recorded: dict[str, str] = {}
    for line in text.splitlines():
        key, _, val = line.partition("=")
        if key.strip() and val.strip():
            recorded[key.strip()] = val.strip()
    return [k for k, sha in _current_pins(root).items() if recorded.get(k) != sha]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def build_plan(
    root: Path,
    options: MigrateOptions | None = None,
    report: AuditReport | None = None,
) -> Plan:
    root = Path(root).expanduser().resolve()
    options = options or MigrateOptions()
    report = report or audit_project(root, options)

    wanted = set(report.failing_ops())
    if options.record_pins:
        wanted.add("n64_record_framework_pins")
    else:
        wanted.discard("n64_record_framework_pins")
    if not options.merge_gitignore:
        wanted.discard("n64_merge_gitignore")
    if not options.enable_recomp_ui:
        wanted.discard("n64_ensure_recomp_ui_submodule")

    if options.only:
        wanted = {o for o in wanted if o in options.only} | set(options.only)
    if options.skip:
        wanted -= set(options.skip)

    ordered = [op for op in OP_ORDER if op in wanted]
    ordered.extend(sorted(op for op in wanted if op not in ordered))

    steps = [
        PlanStep(
            op_id=op,
            title=OP_TITLES.get(op, op),
            detail=next((c.detail for c in report.checks if c.fix_op == op), ""),
            selected=True,
        )
        for op in ordered
    ]
    return Plan(root=str(root), layout=report.layout, steps=steps, options=options)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def apply_plan(plan: Plan) -> list[ApplyResult]:
    root = Path(plan.root).expanduser().resolve()
    opts = plan.options
    results: list[ApplyResult] = []
    for step in plan.steps:
        if not step.selected:
            continue
        fn = _OPS.get(step.op_id)
        if fn is None:
            results.append(ApplyResult(step.op_id, False, "Unknown op"))
            continue
        try:
            results.append(fn(root, opts))
        except Exception as exc:  # noqa: BLE001 - one bad op must not kill the run
            results.append(ApplyResult(step.op_id, False, f"{type(exc).__name__}: {exc}"))
    return results


def _dry(opts: MigrateOptions) -> bool:
    return bool(opts.dry_run)


# --- submodules ------------------------------------------------------------
_SUBMODULE_URLS = {
    FRAMEWORK: ("https://github.com/RetroPortingToolKit/n64lle.git", "main"),
    "recomp-ui": ("https://github.com/mstan/recomp-ui.git", "master"),
}


def _ensure_submodule(root: Path, opts: MigrateOptions, path: str) -> ApplyResult:
    op = f"n64_ensure_{'framework' if path == FRAMEWORK else 'recomp_ui'}_submodule"
    url, branch = _SUBMODULE_URLS[path]
    if not _is_git_repo(root):
        return ApplyResult(op, False, "Not a git repository")
    declared, live = _submodule_present(
        root, path, FRAMEWORK_MARKER if path == FRAMEWORK else ""
    )
    if live:
        return ApplyResult(op, True, f"{path} already present")
    if _dry(opts):
        verb = "init" if declared else "add"
        return ApplyResult(op, True, f"[dry-run] would {verb} submodule {path}")
    if declared:
        code, out = _git(root, "submodule", "update", "--init", "--recursive", path)
    else:
        code, out = _git(root, "submodule", "add", "-b", branch, url, path)
        if code == 0:
            _git(root, "submodule", "update", "--init", "--recursive", path)
    if code != 0:
        return ApplyResult(op, False, f"{path}: {out or 'git failed'}")
    return ApplyResult(op, True, f"{path} ready", [path])


def _op_ensure_framework(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _ensure_submodule(root, opts, FRAMEWORK)


def _op_ensure_recomp_ui(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _ensure_submodule(root, opts, "recomp-ui")


def _op_repair_framework(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "n64_repair_framework_submodule"
    sub = root / FRAMEWORK
    why = diagnose_framework_checkout(root)
    if why is None:
        return ApplyResult(op, True, f"{FRAMEWORK}/ is a healthy checkout")
    if not _is_git_repo(root):
        return ApplyResult(op, False, "Not a git repository")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would re-add {FRAMEWORK} as a submodule")
    # Deliberately not `rm -rf`: an unrecognised n64lle/ may hold work nobody
    # else has. Moving it aside keeps the repair reversible.
    if sub.exists():
        aside = root / f"{FRAMEWORK}.broken"
        n = 1
        while aside.exists():
            n += 1
            aside = root / f"{FRAMEWORK}.broken.{n}"
        try:
            sub.rename(aside)
        except OSError as exc:
            return ApplyResult(op, False, f"Could not move {FRAMEWORK}/ aside: {exc}")
        moved = aside.name
    else:
        moved = ""
    _git(root, "rm", "-r", "--cached", "-q", FRAMEWORK)
    url, branch = _SUBMODULE_URLS[FRAMEWORK]
    code, out = _git(root, "submodule", "add", "--force", "-b", branch, url, FRAMEWORK)
    if code != 0:
        return ApplyResult(op, False, f"{FRAMEWORK}: {out or 'git failed'}")
    _git(root, "submodule", "update", "--init", "--recursive", FRAMEWORK)
    msg = f"Re-added {FRAMEWORK} as a submodule"
    if moved:
        msg += f" (previous tree kept at {moved}/)"
    return ApplyResult(op, True, msg, [FRAMEWORK])


# --- untracking ------------------------------------------------------------
def _untrack(root: Path, opts: MigrateOptions, op: str, specs: tuple[str, ...],
             label: str) -> ApplyResult:
    if not _is_git_repo(root):
        return ApplyResult(op, False, "Not a git repository")
    files = [f for spec in specs for f in _tracked(root, spec)]
    if not files:
        return ApplyResult(op, True, f"No tracked {label}")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would untrack {len(files)} {label}")
    # --cached: the working tree keeps the files. Deleting a user's generated C
    # (or their ROM) to fix a tracking mistake would be a far worse bug.
    code, out = _git(root, "rm", "-r", "--cached", "-q", "--", *files)
    if code != 0:
        return ApplyResult(op, False, out or "git rm --cached failed")
    return ApplyResult(op, True, f"Untracked {len(files)} {label} (kept on disk)", files)


def _op_untrack_generated(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _untrack(root, opts, "n64_untrack_generated", _GENERATED_PATHSPECS,
                    "generated file(s)")


def _op_untrack_roms(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _untrack(root, opts, "n64_untrack_roms", _ROM_PATHSPECS, "ROM file(s)")


# --- .gitignore ------------------------------------------------------------
def _op_merge_gitignore(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "n64_merge_gitignore"
    gi = root / ".gitignore"
    have = ""
    if gi.is_file():
        try:
            have = gi.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ApplyResult(op, False, f"Could not read .gitignore: {exc}")
    existing = {ln.strip() for ln in have.splitlines()}
    missing = [r for r in GITIGNORE_RULES if r not in existing]
    if not missing:
        return ApplyResult(op, True, ".gitignore already carries the N64 rules")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would append {len(missing)} rule(s)")
    block = "\n# --- n64lle port rules (Retro Studio) ---\n" + "\n".join(missing) + "\n"
    text = have
    if text and not text.endswith("\n"):
        text += "\n"
    try:
        gi.write_text(text + block, encoding="utf-8", newline="\n")
    except OSError as exc:
        return ApplyResult(op, False, f"Could not write .gitignore: {exc}")
    return ApplyResult(op, True, f"Appended {len(missing)} rule(s)", [".gitignore"])


# --- templates -------------------------------------------------------------
_TOKEN_RE = re.compile(r"@([A-Z0-9_]+)@")


# Longest extension first, and a boundary after it. Ordered c|cpp|cmake, the
# alternation matches "c" inside "runtime.cmake" and reports a file called
# runtime.c that no one ever named.
_FRAMEWORK_SRC_RE = re.compile(
    r"\$\{N64LLE_ROOT\}/([A-Za-z0-9_./-]+\.(?:cmake|cpp|hpp|cc|c|h))(?![A-Za-z0-9_])"
)


def missing_framework_sources(root: Path) -> list[str]:
    """Framework files the port's CMakeLists names that the pinned n64lle lacks.

    This is a CONFIGURE-BLOCKING class, not a warning: add_executable() on a
    source that does not exist is a CMake *generate* error, so the port never
    reaches a compiler. It happens because a port's CMakeLists is rendered once
    from the template and then frozen while the framework moves under it — the
    worked example is bench/frame_probe.c, removed from n64lle after the first
    ports were cut, with the template growing an if(EXISTS) guard that only new
    ports got.

    Only ${N64LLE_ROOT}-relative source paths are checked, because those are
    the ones the port has no control over. A path that resolves through a
    variable this reader cannot expand is skipped rather than guessed at.
    """
    fw = n64_paths.framework_root(root)
    if not (fw / n64_paths.MARKER).is_file():
        return []  # no checkout to check against; the submodule check says so.
    text = _cmake_text(root)
    out: list[str] = []
    for rel in _FRAMEWORK_SRC_RE.findall(text):
        if (fw / rel).is_file() or rel in out:
            continue
        # A port that already tests for the file handles its absence itself --
        # that IS the template's fix, and flagging it would report every
        # up-to-date port as broken. Only an unguarded reference blocks
        # configure.
        if f'if(EXISTS "${{N64LLE_ROOT}}/{rel}")' in text:
            continue
        out.append(rel)
    return out


_N64LLE_VAR_REF_RE = re.compile(r"\$\{(N64LLE_[A-Z0-9_]+)\}")
_N64LLE_VAR_DEF_RE = re.compile(
    r"\b(?:set|option|find_program|find_file|find_path|find_library)\s*\(\s*"
    r"(N64LLE_[A-Z0-9_]+)\b")
_FRAMEWORK_INCLUDE_RE = re.compile(
    r'include\s*\(\s*"?\$\{N64LLE_ROOT\}/([A-Za-z0-9_./-]+\.cmake)"?')


def vanished_framework_variables(root: Path) -> list[str]:
    """``${N64LLE_*}`` the port reads and nothing defines any more.

    The sibling of missing_framework_sources for the class that check cannot
    see: a port's CMakeLists names framework paths through variables the
    framework sets, and when the framework stops setting one the reference
    does not fail -- it expands to "". n64lle rust-parity (2026-09-23) removed
    N64LLE_SUPPORT and N64LLE_ISA_INC from n64lle_runtime_resolve_framework(),
    so a port cut before it compiles "${N64LLE_SUPPORT}/n64emit_support.c" as
    "/n64emit_support.c".

    "Defined" is read from the pin, never listed here: every set()/option()/
    find_*() of an N64LLE_ variable in the pinned runtime.cmake and in each
    framework .cmake file the port include()s, plus the port's own. Empty when
    there is no checkout to read.
    """
    fw = n64_paths.framework_root(root)
    if not (fw / n64_paths.MARKER).is_file():
        return []
    text = _cmake_text(root)
    if not text:
        return []
    defined = set(_N64LLE_VAR_DEF_RE.findall(text))
    for rel in {n64_paths.MARKER.as_posix(), *_FRAMEWORK_INCLUDE_RE.findall(text)}:
        try:
            src = (fw / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        defined |= set(_N64LLE_VAR_DEF_RE.findall(src))
    out: list[str] = []
    for name in _N64LLE_VAR_REF_RE.findall(text):
        if name not in defined and name not in out:
            out.append(name)
    return out


_CMAKE_DEFINE_RE = re.compile(r"-D([A-Za-z_][A-Za-z0-9_]*)\s*=")


def build_framework_option_drift(root: Path, opts: MigrateOptions) -> list[str]:
    """cmake -D options the pinned framework's template has and the port lacks.

    The port owns tools/build_framework.sh — it is rendered once at scaffold
    time and then never touched again, which means a port cut before a switch
    existed keeps building without it forever and nothing says so.

    Compared by OPTION rather than by file content on purpose. A port is
    allowed to add its own flags (GloverRecomp adds -lm and documents why it
    belongs upstream), and a whole-file diff would report every such port as
    broken. The direction that actually costs something is the other one: an
    option the framework now expects that the script never passes.

    Returns the option NAMES, in template order. Empty when there is nothing to
    compare against — no template on disk, or a template whose tokens this repo
    cannot resolve. "I could not tell" is never reported as drift.
    """
    script = root / n64_paths.FRAMEWORK_BUILD_SCRIPT
    if not script.is_file():
        return []

    # A shim delegates to the framework's shared script, so it passes no -D
    # options of its own and has nothing to drift. That is the GOOD state, not
    # an empty one -- report it as no drift rather than as every option missing.
    if n64_paths.port_script_is_shim(root):
        return []

    # Compare against the FRAMEWORK'S script when the pinned n64lle has one,
    # and only fall back to the scaffold template otherwise. The template is a
    # snapshot of what a NEW port gets; the framework's copy is what the build
    # actually requires today, and the gap between those two is precisely how
    # -DN64LLE_RSP_CENSUS=1 went missing from seven ports without this check
    # ever having anything to compare against.
    canon = n64_paths.framework_owned_build_script(root)
    tdir = n64_paths.templates_dir(root)
    src = canon if canon is not None else (
        tdir / "build_framework.sh.in" if tdir is not None else None)
    if src is None or not Path(src).is_file():
        return []
    try:
        have_text = script.read_text(encoding="utf-8", errors="replace")
        raw = Path(src).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if canon is not None:
        # The framework's copy is already concrete -- it has no @TOKEN@s to
        # render, so compare it directly.
        have = set(_CMAKE_DEFINE_RE.findall(have_text))
        out: list[str] = []
        for name in _CMAKE_DEFINE_RE.findall(raw):
            if name not in have and name not in out:
                out.append(name)
        return out
    # Rendered, not raw: a token could in principle appear inside an option
    # name, and comparing a rendered file against a rendered file is the only
    # comparison that cannot produce a phantom.
    rendered, missing = _render(raw, _template_values(root, opts))
    if missing:
        return []
    have = set(_CMAKE_DEFINE_RE.findall(have_text))
    out: list[str] = []
    for name in _CMAKE_DEFINE_RE.findall(rendered):
        if name not in have and name not in out:
            out.append(name)
    return out


def _render(text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """@TOKEN@ substitution, matching the wizard's fill_tokens.py contract.

    Reimplemented rather than imported for the same reason snesops does it:
    the toolkit ships its own top-level ``fill_tokens.py`` for PSX, and putting
    a wizard directory on sys.path makes which one you get depend on import
    order. Unknown tokens are RETURNED, never silently blanked — the wizard's
    own rule, and the values most likely to be missing here are ROM digests.
    """
    missing: list[str] = []

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return values[key]

    return _TOKEN_RE.sub(replace, text), missing


def _default_build_dir() -> str:
    """Studio's build tree name, imported lazily.

    buildops imports n64_paths and snes_paths at module scope; importing it at
    the top of this one would close a cycle through models. Read at call time
    instead, so the two layers cannot drift apart by a stale copy.
    """
    from .buildops import DEFAULT_BUILD_DIR

    return DEFAULT_BUILD_DIR


def _human_size(n: int) -> str:
    mib = n / 1048576.0
    return f"{mib:.0f} MiB" if n % 1048576 == 0 else f"{mib:.2f} MiB"


_REGION_LABELS = {
    "E": "USA", "A": "USA/Australia", "J": "Japan", "P": "Europe",
    "D": "Germany", "F": "France", "I": "Italy", "S": "Spain",
    "U": "Australia", "X": "Europe", "Y": "Europe",
}


def _template_values(root: Path, opts: MigrateOptions) -> dict[str, str]:
    """Token values for the scaffold stubs, read out of the repo itself.

    Everything ROM-shaped comes from ``game.toml`` — the port's own committed
    contract, whose rows the scaffolder tagged [MEASURED]. Nothing here probes
    a ROM or infers a digest: a value that is not already recorded stays
    absent, and _render then refuses the write rather than emitting a blank.
    """
    slug = project_slug(root)
    contract = read_contract(root)
    game = contract.get("game") or {}
    recomp = contract.get("recompiler") or {}
    runtime = contract.get("runtime") or {}

    values: dict[str, str] = {
        "PROJECT": opts.project_name or project_name(root),
        "SLUG": slug,
        "SLUG_UPPER": slug.upper(),
        "EXE": opts.boot_exe or executable_name(root),
        "DATE": date.today().isoformat(),
        "DEFAULT_BRANCH": default_branch(root),
        # The port's build tree. n64lle's wizard writes this token into
        # build_framework.sh, README.md and STATUS.md so a scaffold's docs
        # cannot drift from where it actually builds; Studio has to answer it
        # too, and the authority on this side is the one value every build
        # subcommand already defaults to.
        "BUILD_DIR": _default_build_dir(),
    }
    if game.get("name"):
        values["NAME"] = str(game["name"])
    if runtime.get("window_title"):
        values["WINDOW_TITLE"] = str(runtime["window_title"])
    for token, key in (
        ("CARTID", "cartid"), ("REGION", "region"), ("REVISION", "revision"),
        ("ENTRY_PC", "entry_pc"), ("ROM_SIZE", "rom_size"),
        ("BYTE_ORDER", "byte_order"), ("CIC", "cic"), ("CIC_SEED", "cic_seed"),
        ("CIC_CHALLENGE", "cic_challenge"), ("CRC1", "crc1"),
        ("CRC2", "crc2"), ("SHA256", "sha256"),
    ):
        if game.get(key) is not None and str(game[key]).strip():
            values[token] = str(game[key]).strip()
    for token, key in (
        ("HARVEST_FRAMES", "harvest_frames"),
        ("HARVEST_STEP_CAP_M", "harvest_step_cap_millions"),
    ):
        if recomp.get(key) is not None:
            values[token] = str(recomp[key])
    if "REGION" in values:
        values["REGION_LABEL"] = _REGION_LABELS.get(values["REGION"], values["REGION"])
    if "ROM_SIZE" in values:
        try:
            values["ROM_SIZE_H"] = _human_size(int(values["ROM_SIZE"]))
        except ValueError:
            pass
    # The dump's in-repo path, as the scaffolder writes it and CMake reads it.
    # Preferred from the CMakeLists cache line so a port that renamed it is
    # described accurately rather than by the naming rule.
    rom_rel = ""
    m = re.search(
        r"set\(\s*" + re.escape(values["SLUG_UPPER"]) +
        r"_ROM\s+\"\$\{CMAKE_CURRENT_SOURCE_DIR\}/([^\"]+)\"",
        _cmake_text(root),
    )
    if m:
        rom_rel = m.group(1)
    elif slug and values.get("BYTE_ORDER"):
        rom_rel = f"roms/{slug}.{values['BYTE_ORDER']}"
    if rom_rel:
        values["ROM_FILE"] = rom_rel
        values["ROM_BASENAME"] = Path(rom_rel).name
    return values


def _fill_template(
    root: Path, opts: MigrateOptions, op: str, template: str, rel: str
) -> ApplyResult:
    tdir = n64_paths.templates_dir(root)
    if tdir is None:
        return ApplyResult(op, False, n64_paths.MISSING_CHECKOUT)
    src = tdir / template
    if not src.is_file():
        return ApplyResult(op, False, f"Template not found: {src}")
    dst = root / rel
    if dst.is_file() and not opts.force:
        return ApplyResult(op, True, f"{rel} already present (use --force to overwrite)")
    try:
        raw = src.read_text(encoding="utf-8")
    except OSError as exc:
        return ApplyResult(op, False, f"Could not read {template}: {exc}")

    rendered, missing = _render(raw, _template_values(root, opts))
    if missing:
        # Refuse rather than emit a file with @TOKEN@ or a blank in it. The
        # commonest missing token is a ROM digest, and that is exactly the
        # value nobody should be inventing.
        return ApplyResult(
            op, False,
            f"{rel} not written — unresolved tokens: "
            f"{', '.join(sorted(set(missing)))}. They come from game.toml; "
            "fill that in first rather than letting a blank through.",
        )
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would write {rel} from {template}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(rendered, encoding="utf-8", newline="\n")
        if rel.endswith(".sh"):
            dst.chmod(dst.stat().st_mode | 0o111)
    except OSError as exc:
        return ApplyResult(op, False, f"Could not write {rel}: {exc}")
    return ApplyResult(op, True, f"Wrote {rel} ({n64_paths.wizard_source(root)})", [rel])


def _op_emit_version(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_template(root, opts, "n64_emit_version", "VERSION.in", "VERSION")


def _op_emit_build_framework(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "n64_emit_build_framework"
    dst = root / n64_paths.FRAMEWORK_BUILD_SCRIPT
    # Present but not executable is its own fix, and rewriting the file would
    # discard workarounds this port measured against its pinned framework.
    if dst.is_file() and not opts.force:
        if not dst.stat().st_mode & 0o111:
            if _dry(opts):
                return ApplyResult(op, True, f"[dry-run] would chmod +x {dst.name}")
            dst.chmod(dst.stat().st_mode | 0o111)
            return ApplyResult(op, True, f"Made {n64_paths.FRAMEWORK_BUILD_SCRIPT} executable",
                               [str(n64_paths.FRAMEWORK_BUILD_SCRIPT)])
        return ApplyResult(op, True, "tools/build_framework.sh already present")
    return _fill_template(root, opts, op, "build_framework.sh.in",
                          str(n64_paths.FRAMEWORK_BUILD_SCRIPT))


def _op_emit_cmakelists(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Re-render CMakeLists.txt from the PINNED framework's template.

    Overwriting a port's build file is not something to do casually, and this
    op requires --force for exactly that reason. It exists because a port's
    CMakeLists is scaffold-owned rather than hand-written — n64lle's contract
    is that a port makes ONE n64lle_add_runtime_target() call and carries no
    host source (docs/05 §10) — so when the framework moves, the correct fix
    flows from the template, not from editing the game repo.

    The failure that made it necessary: the template used to declare
    <slug>-frame-probe from ${N64LLE_ROOT}/bench/frame_probe.c unguarded.
    n64lle later removed that file ("bench: do not carry frame_probe onto the
    rewrite") and the template grew an if(EXISTS) around it — but every port
    already cut kept the unguarded copy, and add_executable on a missing source
    is a CMake GENERATE error. Those ports cannot configure at all, and nothing
    in the game repo is the right place to fix it.
    """
    return _fill_template(root, opts, "n64_emit_cmakelists",
                          "CMakeLists.txt.in", "CMakeLists.txt")


def _op_emit_roms_readme(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_template(root, opts, "n64_emit_roms_readme",
                          "roms_README.md.in", "roms/README.md")


def _op_emit_generated_readme(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_template(root, opts, "n64_emit_generated_readme",
                          "generated_README.md.in", "generated/README.md")


def _op_emit_claude_md(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_template(root, opts, "n64_emit_claude_md", "CLAUDE.md.in", "CLAUDE.md")


# --- template drift -----------------------------------------------------------
def _drift_apply(root: Path, opts: MigrateOptions, op: str, extra: tuple[str, ...],
                 cls: str) -> ApplyResult:
    drift, why = measure_drift(root)
    if drift is None:
        return ApplyResult(op, False, why)
    if not drift["_own"]:
        return ApplyResult(op, False, "Preview only -- the templates are not from this "
                           "port's pinned n64lle. " + _BUMP_HINT)
    todo = [it["path"] for it in drift.get("items", [])
            if it.get("class") == cls and it.get("state") in ("missing", "drift")]
    if not todo:
        return ApplyResult(op, True, "Nothing to take: already matches the template")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would write {', '.join(todo)} "
                           f"from n64lle {drift.get('templates_rev', '?')}")
    after, why = measure_drift(root, "--apply", *extra,
                               *[a for p in todo for a in ("--only", p)])
    if after is None:
        return ApplyResult(op, False, why)
    wrote = after.get("wrote", [])
    skipped = [f"{s['path']} ({s['why']})" for s in after.get("skipped", [])]
    msg = f"Wrote {len(wrote)} file(s) from n64lle {after.get('templates_rev', '?')}"
    if skipped:
        msg += "; NOT written: " + ", ".join(skipped)
    msg += ". Review with git diff, build and run the gates before committing."
    return ApplyResult(op, not skipped, msg, wrote)


def _op_template_sync(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _drift_apply(root, opts, "n64_template_sync", (), "owned")


def _op_template_take_cmakelists(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "n64_template_take_cmakelists"
    # The same bar n64_emit_cmakelists sets: the build graph is overwritten
    # only when asked for by name.
    if not opts.force:
        return ApplyResult(op, False, "CMakeLists.txt is overwritten only with Force "
                           "ticked -- read the audit row's diff size first.")
    return _drift_apply(root, opts, op, ("--take", "CMakeLists.txt"), "review")


# --- pins ------------------------------------------------------------------
def _op_record_pins(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "n64_record_framework_pins"
    pins = _current_pins(root)
    if not pins:
        return ApplyResult(op, False, "No submodule checkouts to record")
    body = "".join(f"{k}={v}\n" for k, v in sorted(pins.items()))
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would write {PINS_FILE} ({len(pins)} pin(s))")
    try:
        (root / PINS_FILE).write_text(body, encoding="utf-8", newline="\n")
    except OSError as exc:
        return ApplyResult(op, False, f"Could not write {PINS_FILE}: {exc}")
    return ApplyResult(op, True, f"Recorded {len(pins)} pin(s)", [PINS_FILE])


_OPS = {
    "n64_repair_framework_submodule": _op_repair_framework,
    "n64_ensure_framework_submodule": _op_ensure_framework,
    "n64_ensure_recomp_ui_submodule": _op_ensure_recomp_ui,
    "n64_merge_gitignore": _op_merge_gitignore,
    "n64_untrack_generated": _op_untrack_generated,
    "n64_untrack_roms": _op_untrack_roms,
    "n64_template_sync": _op_template_sync,
    "n64_template_take_cmakelists": _op_template_take_cmakelists,
    "n64_emit_version": _op_emit_version,
    "n64_emit_build_framework": _op_emit_build_framework,
    "n64_emit_cmakelists": _op_emit_cmakelists,
    "n64_emit_roms_readme": _op_emit_roms_readme,
    "n64_emit_generated_readme": _op_emit_generated_readme,
    "n64_emit_claude_md": _op_emit_claude_md,
    "n64_record_framework_pins": _op_record_pins,
}
