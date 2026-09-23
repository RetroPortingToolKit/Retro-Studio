"""Audit / plan / apply for a SNES title repo.

The PSX migration in ``ops.py`` is not reusable here and should not be forced
to be: it is almost entirely about ``game.toml``, disc probing, BIOS backends
and a CMake rewrite that has no SNES counterpart. What the two consoles share
is the *shape* — audit produces checks, checks name fix ops, plan orders them,
apply runs them — so this module reimplements the shape and nothing else.

Every op here is either idempotent or a no-op. Anything that cannot be derived
from the repository as it stands (ROM digests, above all) is reported as a
warning with no fix op rather than guessed at: a regen.sh carrying invented
digests would verify a ROM nobody owns and fail at the least useful moment.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from collections.abc import Callable
from pathlib import Path

from . import snes_paths
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

# Ordered for apply: submodules first (later ops read their contents), pins
# last (they record what everything above settled on).
OP_ORDER: tuple[str, ...] = (
    "snes_repair_framework_submodule",
    "snes_ensure_framework_submodule",
    "snes_ensure_recomp_ui_submodule",
    "snes_ensure_nested_modules",
    "snes_enable_netplay",
    "snes_disable_netplay",
    "snes_declare_mod_catalog",
    "snes_adopt_framework_regen",
    "snes_merge_gitignore",
    "snes_untrack_generated",
    "snes_ensure_src_gen",
    "snes_emit_version",
    "snes_probe_rom_refresh",
    "snes_emit_rom_identity",
    "snes_emit_codegen_setup",
    "snes_emit_regen",
    "snes_relocate_boxart",
    "snes_emit_boxart_stub",
    "snes_emit_packager",
    "snes_emit_ci_workflow",
    "snes_patch_readme_metrics",
    "snes_record_framework_pins",
)

ADOPT_REGEN_TITLE = "Hand tools/regen.sh back to the framework"

OP_TITLES: dict[str, str] = {
    "snes_ensure_framework_submodule": "Add snesrecomp submodule",
    "snes_ensure_recomp_ui_submodule": "Add recomp-ui submodule",
    "snes_ensure_nested_modules": "Init lib/recomp-net + lib/retcomm-rbengine",
    "snes_merge_gitignore": "Merge SNES .gitignore rules (src/gen, *.sfc)",
    "snes_untrack_generated": "Untrack committed generated C / funcs.h",
    "snes_ensure_src_gen": "Create src/gen/.gitkeep",
    "snes_emit_version": "Emit VERSION",
    "snes_emit_regen": "Emit tools/regen.sh",
    "snes_emit_packager": "Emit scripts/package_release.sh",
    "snes_emit_ci_workflow": "Emit .github/workflows/release.yml",
    "snes_record_framework_pins": "Write framework_pins.txt",
    "snes_repair_framework_submodule": "Repair broken snesrecomp/ git checkout",
    "snes_probe_rom_refresh": "Refresh ROM identity via probe_rom.py",
    "snes_emit_rom_identity": "Emit rom_identity.txt",
    "snes_emit_codegen_setup": "Emit src/codegen_setup.c / .h",
    "snes_relocate_boxart": "Relocate boxart → launcher_assets/img/",
    "snes_emit_boxart_stub": "Create launcher_assets/img stub dir",
    "snes_patch_readme_metrics":
        "Patch README badges, Retro Launcher, and R.A.I.D. footer",
    "snes_enable_netplay": "Wire netplay (snesrecomp_enable_recomp_net)",
    "snes_disable_netplay": "Unwire netplay (comment the call out)",
    "snes_declare_mod_catalog":
        "Hand mod staging to the framework (snesrecomp_target_mod_catalog)",
    "snes_adopt_framework_regen": ADOPT_REGEN_TITLE,
}

# Matches snesrecomp's tools/new_project/templates/gitignore.in. The launcher
# entries are not cosmetic: a port that wires the recomp-ui launcher gets
# rom.cfg / keybinds.ini / config.ini written beside the executable — and in
# the repo root whenever the game is run from there — which shows up as
# untracked files nobody meant to commit.
GITIGNORE_RULES: tuple[str, ...] = (
    "/src/gen/",
    "/recomp/funcs.h",
    "*.sfc",
    "*.smc",
    "*.srm",
    "/build/",
    "/build-*/",
    "/dist/",
    "/saves/",
    "/rom.cfg",
    "/keybinds.ini",
    "/config.ini",
    "/input.ini",
    # Capture bundles from tools/snes_analysis embed ROM-derived
    # VRAM/CGRAM dumps — evidence, not source.
    "/analysis/",
)

FRAMEWORK = "snesrecomp"
NESTED_PATHS = ("lib/recomp-net", "lib/retcomm-rbengine")

# ---------------------------------------------------------------------------
# ROM identity carriers
# ---------------------------------------------------------------------------
# Which file carries ROM identity is the framework's call, not Studio's, and it
# changed: snesrecomp now scaffolds a single ``rom_identity.txt`` that CMake
# turns into snesrecomp_rom_identity.h and that tools/regen.sh and the release
# workflow read directly, replacing the src/codegen_setup.c/.h pair older
# revisions emitted. Studio drives whichever wizard the port is *pinned* to
# (see snes_paths), so it has to serve both eras — and the templates present in
# that wizard are the only honest signal of which one this is. Hardcoding
# either set is what made a migration against a current checkout die with
# "Template not found: .../codegen_setup.c.in".
IDENTITY_FILE = "rom_identity.txt"

_IDENTITY_CARRIERS: dict[str, tuple[tuple[str, str], ...]] = {
    "file": (("rom_identity.txt.in", IDENTITY_FILE),),
    "codegen": (
        ("codegen_setup.c.in", "src/codegen_setup.c"),
        ("codegen_setup.h.in", "src/codegen_setup.h"),
    ),
}

_IDENTITY_OPS: dict[str, str] = {
    "file": "snes_emit_rom_identity",
    "codegen": "snes_emit_codegen_setup",
}


def identity_layout(game_root: Path | str | None = None) -> str:
    """``"file"``, ``"codegen"``, or ``""`` when the wizard offers neither."""
    tdir = snes_paths.templates_dir(game_root)
    if tdir is None:
        return ""
    if (tdir / "rom_identity.txt.in").is_file():
        return "file"
    if (tdir / "codegen_setup.c.in").is_file():
        return "codegen"
    return ""


def identity_carriers(game_root: Path | str | None = None) -> tuple[tuple[str, str], ...]:
    """``(template, repo-relative path)`` pairs this wizard scaffolds."""
    return _IDENTITY_CARRIERS.get(identity_layout(game_root), ())


def list_ops() -> list[str]:
    return list(OP_ORDER)


# ---------------------------------------------------------------------------
# Reading the repo
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str) -> tuple[int, str]:
    """``(returncode, output)`` — stdout on success, stderr on failure.

    git writes its diagnostics to stderr, and returning only stdout meant
    every failure here reported an empty reason: "git failed: " with nothing
    after it, for a `submodule update` that had said
    "fatal: No url found for submodule path 'lib/retcomm-rbengine' in
    .gitmodules". A gate that discards the one sentence explaining itself is
    worse than no gate, because the user has nothing to act on.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return 1, f"could not run git: {exc}"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        # Prefer stderr, but never return empty when either stream said
        # something — callers put this straight in front of the user.
        return proc.returncode, err or out
    return proc.returncode, out


def _is_git_repo(root: Path) -> bool:
    code, out = _git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out == "true"


_PROJECT_RE = re.compile(r"^\s*project\s*\(\s*([A-Za-z0-9_.+-]+)", re.MULTILINE)


def project_name(root: Path) -> str:
    cml = root / "CMakeLists.txt"
    if cml.is_file():
        try:
            m = _PROJECT_RE.search(cml.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            m = None
        if m:
            return m.group(1)
    return root.name


def display_name(root: Path) -> str:
    """Title from README's first heading, else the CMake project name."""
    readme = root / "README.md"
    if readme.is_file():
        try:
            for line in readme.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("# "):
                    return line[2:].strip() or project_name(root)
        except OSError:
            pass
    return project_name(root)


def _zip_prefix(root: Path) -> str:
    """Reuse the existing packager's prefix so a re-emit keeps asset names."""
    pkg = root / "scripts" / "package_release.sh"
    if pkg.is_file():
        try:
            m = re.search(
                r'^ZIP_PREFIX="?\$\{ZIP_PREFIX:-([^}"]+)\}"?',
                pkg.read_text(encoding="utf-8", errors="replace"),
                re.MULTILINE,
            )
        except OSError:
            m = None
        if m:
            return m.group(1).strip()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", project_name(root)).strip("-")
    return slug or "game"


def default_branch(root: Path) -> str:
    code, out = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 0 and out and out != "HEAD":
        return out
    return "main"


_DIGEST_PATTERNS = (
    # tools/regen.sh, written by the same wizard we are re-emitting from.
    (re.compile(r'EXPECTED_CRC32="?\$\{SNESRECOMP_EXPECTED_CRC32:-([0-9a-fA-Fx]+)'), "crc32"),
    (re.compile(r'EXPECTED_SHA256="?\$\{SNESRECOMP_EXPECTED_SHA256:-([0-9a-fA-F]+)'), "sha256"),
)


def parse_identity_file(path: Path) -> dict[str, str]:
    """``key = value`` lines, ``#`` comments, optionally quoted values.

    Deliberately mirrors the sed in regen.sh's ``identity_get`` rather than
    being stricter: only a line that *starts* with ``#`` is a comment, the
    first occurrence of a key wins, trailing whitespace goes, surrounding
    double quotes come off. Studio agreeing with the file's actual consumer
    matters more than agreeing with a tidier grammar. Values still carrying an
    unfilled ``@TOKEN@`` are dropped, not recovered.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        if key and val and "@" not in val:
            out.setdefault(key, val)
    return out


def _carrier_label(game_root: Path | str | None = None) -> str:
    """What to call the identity carrier in a message, for this wizard."""
    carriers = identity_carriers(game_root)
    return " / ".join(rel for _, rel in carriers) or "the identity carrier"


def _identity_source(root: Path) -> str:
    """The file a recovered digest actually came from, for the audit detail."""
    for rel in (IDENTITY_FILE, "src/codegen_setup.c"):
        if (root / rel).is_file():
            return rel
    return "tools/regen.sh"


def rom_identity(root: Path, rom: str | None = None) -> dict[str, str]:
    """ROM tokens for the templates: probed if a ROM is given, else recovered.

    Recovery reads what a previous scaffold already baked in. That is the whole
    reason it is safe to re-emit these files: the digests are not re-derived,
    they are carried across unchanged.
    """
    out: dict[str, str] = {}
    if rom:
        probe = snes_paths.probe_rom_script(root)
        rom_path = Path(rom).expanduser()
        if probe is not None and probe.is_file() and rom_path.is_file():
            code, text = _run_probe(probe, rom_path)
            if code == 0 and text:
                import json

                try:
                    data = json.loads(text)
                except ValueError:
                    data = {}
                for key, token in (
                    ("crc32", "crc32"),
                    ("md5", "md5"),
                    ("sha1", "sha1"),
                    ("rom_size", "rom_size"),
                    ("sha256", "sha256"),
                    ("display_name", "display_name"),
                    ("mapping", "mapping"),
                    ("region", "region"),
                ):
                    val = str(data.get(key) or "").strip()
                    if val:
                        out[token] = val
                out["rom_file"] = rom_path.name
        if "rom_file" not in out:
            out["rom_file"] = rom_path.name
        return out

    # rom_identity.txt first: it is the current carrier, it holds mapping and
    # region alongside the digests, and it is the very file the build and
    # regen.sh read — so what Studio recovers is what the port actually uses.
    # (Under this layout regen.sh no longer bakes digests in at all, it calls
    # identity_get, so _DIGEST_PATTERNS below has nothing to fall back on.)
    ident_file = root / IDENTITY_FILE
    if ident_file.is_file():
        data = parse_identity_file(ident_file)
        for field, key in (
            ("display_name", "display_name"),
            ("rom_file", "rom_file"),
            ("expected_crc32", "crc32"),
            ("expected_md5", "md5"),
            ("expected_sha1", "sha1"),
            ("expected_sha256", "sha256"),
            ("rom_size", "rom_size"),
            ("mapping", "mapping"),
            ("region", "region"),
            ("game_id", "game_id"),
        ):
            if data.get(field):
                out.setdefault(key, data[field])

    # codegen_setup.c is the same thing for a port pinned to an older
    # framework: mapping and region alongside the digests, which regen.sh
    # does not carry.
    cg = root / "src" / "codegen_setup.c"
    if cg.is_file():
        try:
            text = cg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for field, key in (
            ("display_name", "display_name"),
            ("rom_file", "rom_file"),
            ("expected_crc32", "crc32"),
            ("expected_sha256", "sha256"),
            ("mapping", "mapping"),
            ("region", "region"),
        ):
            m = re.search(r"\." + field + r"\s*=\s*\"([^\"]*)\"", text)
            if m and m.group(1) and "@" not in m.group(1):
                out.setdefault(key, m.group(1))

    regen = root / "tools" / "regen.sh"
    if regen.is_file():
        try:
            text = regen.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for pattern, key in _DIGEST_PATTERNS:
            m = pattern.search(text)
            if m:
                out.setdefault(key, m.group(1))
        m = re.search(r'for cand in "([^"]+\.s[fm]c)"', text)
        if m:
            out.setdefault("rom_file", m.group(1))
    return out


def _run_probe(probe: Path, rom: Path) -> tuple[int, str]:
    """Run probe_rom.py and return its JSON.

    The wizard's probe writes JSON to a file (``--json-out``) and prints a
    human summary to stdout — there is no ``--json`` that puts it on stdout, so
    reading stdout would parse the summary and silently find no digests.
    """
    # sys.executable before the PATH lookups, as analyzeops/buildops already
    # do. On Windows "python3"/"python" resolve to the WindowsApps App
    # Execution Alias, which is a stub that exits 9009 with a "install from
    # the Store" notice and writes no --json-out -- so the probe reported no
    # CRC32/SHA256 and the ROM looked unreadable. The interpreter already
    # running this toolkit is by definition a working one.
    python = (
        os.environ.get("PYTHON")
        or sys.executable
        or shutil.which("python3")
        or shutil.which("python")
    )
    if not python:
        return 1, ""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "probe.json"
        try:
            proc = subprocess.run(
                [python, str(probe), str(rom), "--json-out", str(out), "--quiet"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return 1, ""
        if proc.returncode != 0 or not out.is_file():
            return proc.returncode or 1, ""
        try:
            return 0, out.read_text(encoding="utf-8")
        except OSError:
            return 1, ""


def _tracked(root: Path, pathspec: str) -> list[str]:
    code, out = _git(root, "ls-files", "--", pathspec)
    if code != 0 or not out:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def _submodule_present(root: Path, path: str, marker: str = "") -> tuple[bool, bool]:
    """(declared in .gitmodules, initialised on disk)."""
    declared = False
    gm = root / ".gitmodules"
    if gm.is_file():
        try:
            declared = f"path = {path}" in gm.read_text(encoding="utf-8", errors="replace")
        except OSError:
            declared = False
    sub = root / path
    live = sub.is_dir() and any(sub.iterdir())
    if live and marker:
        live = (sub / marker).is_file()
    return declared, live


def diagnose_framework_checkout(root: Path) -> str | None:
    """A human reason when snesrecomp/ has the marker but git cannot use it.

    Same failure family the PSX audit repairs: a .git file pointing at a
    deleted gitdir (renamed submodule), or a tree absorbed into the parent.
    Returns None when the checkout is healthy or simply absent.
    """
    dest = root / FRAMEWORK
    if not dest.is_dir() or not (dest / "runner" / "runner.cmake").is_file():
        return None
    code, out = _git(dest, "rev-parse", "--is-inside-work-tree")
    if code == 0 and out.strip() == "true":
        return None
    git_file = dest / ".git"
    if git_file.is_file():
        try:
            text = git_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        if text.lower().startswith("gitdir:"):
            rel = text.split(":", 1)[1].strip()
            target = (dest / rel).resolve() if rel else None
            if target is not None and not target.exists():
                return (f"{FRAMEWORK}/.git points at missing gitdir ({rel}) — "
                        "broken submodule metadata.")
            return f"{FRAMEWORK}/.git gitdir is unusable ({rel or text})."
    if git_file.is_dir():
        return f"{FRAMEWORK}/.git exists but git rev-parse fails."
    return (f"{FRAMEWORK}/ has runner/runner.cmake but is not a git checkout "
            "(absorbed into the parent tree or missing .git).")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def _audit_readme(root: Path, add: Callable[..., None]) -> None:
    """The README / GitHub About row.

    Split out so the switch that gates it reads as a switch. One row and one op
    cover both because ``snes_patch_readme_metrics`` writes both: the badges,
    boxart, launcher and R.A.I.D. blocks in README.md, and the repository's
    About blurb over the GitHub API.
    """
    from .readme_metrics import (
        boxart_png_present,
        readme_has_boxart,
        readme_has_launcher,
        readme_has_metrics,
        readme_has_raid,
    )
    readme_path = root / "README.md"
    try:
        readme_text = readme_path.read_text(encoding="utf-8", errors="replace") \
            if readme_path.is_file() else ""
    except OSError:
        readme_text = ""
    missing_readme: list[str] = []
    if not readme_path.is_file():
        missing_readme.append("README.md")
    else:
        if not readme_has_metrics(readme_text):
            missing_readme.append("download badges")
        if not readme_has_boxart(readme_text):
            missing_readme.append("libretro boxart")
        if not readme_has_launcher(readme_text):
            missing_readme.append("Retro Launcher section")
        if not readme_has_raid(readme_text):
            missing_readme.append("R.A.I.D. Discord footer")
    if not (root / ".github" / "raid-discord.png").is_file():
        missing_readme.append(".github/raid-discord.png")
    if not boxart_png_present(root):
        missing_readme.append("launcher_assets/img/boxart.png")
    if missing_readme:
        add("readme_metrics", "README download metrics / launcher / RAID / boxart",
            CheckStatus.WARN, Severity.RECOMMENDED,
            "Missing: " + ", ".join(missing_readme), "snes_patch_readme_metrics")
    else:
        add("readme_metrics", "README download metrics / launcher / RAID / boxart",
            CheckStatus.PASS, Severity.RECOMMENDED,
            "Badges, boxart, Retro Launcher, and R.A.I.D. footer present.")


def _audit_mod_catalog(
    root: Path,
    cml_text: str,
    have_framework: bool,
    add: Callable[..., None],
) -> None:
    """The mod catalog row: who stages it, and does the host read that place.

    Split out for the same reason ``_audit_readme`` is: it answers several
    questions against one file and one framework pin, and inlining it would
    bury the ``audit_project`` sequence it sits in.

    Severity is graded rather than uniform. A catalog the framework aborts on,
    two staging paths disagreeing, or a host reading the wrong directory are
    all shipped-product failures. A per-title block that still stages the
    right bytes to the right place is only a spelling the framework has taken
    over -- worth migrating, not worth failing.
    """
    title = "Mod catalog staging"
    op = "snes_declare_mod_catalog"
    if not have_framework or not cml_text:
        add("mod_catalog", title, CheckStatus.SKIP, Severity.OPTIONAL,
            "Needs the framework checkout and a CMakeLists.txt.")
        return
    if not framework_has_mod_catalog(root):
        # Not a defect: on this pin the per-title block is load-bearing, and
        # the fix op refuses to run rather than call a function that does not
        # exist. Advance the submodule and the row becomes actionable.
        add("mod_catalog", title, CheckStatus.SKIP, Severity.OPTIONAL,
            f"The checked-out {FRAMEWORK} pin predates {MOD_CATALOG_CALL}(); "
            "update the submodule to migrate.")
        return

    pkgs = catalog_package_ids(root)
    declared = _mod_catalog_declared(cml_text)
    legacy = _legacy_mod_staging(cml_text)
    dest = framework_catalog_dest(root)
    want = host_mod_root(dest)
    bad_hosts = [(src, got) for src, got in host_mod_roots(root) if got != want]

    broken: list[str] = []     # ships wrong, or does not configure
    cleanup: list[str] = []    # works, but the framework owns this now
    unreadable: list[str] = [] # the op will not touch these

    if pkgs and declared is None:
        broken.append(
            f"{len(pkgs)} package(s) ({', '.join(pkgs[:3])}"
            + (", …" if len(pkgs) > 3 else "")
            + f") but no {MOD_CATALOG_CALL}() — configure aborts on the "
              "framework's guard")
    if legacy:
        labels = "; ".join(label for _s, _e, label, _t in legacy)
        if declared is not None:
            broken.append(f"per-title staging alongside the declaration "
                          f"({labels}) — two blocks writing two layouts")
        else:
            cleanup.append(f"per-title staging ({labels}) — the framework "
                           "owns the destination now")
    if pkgs and not _mods_enabled(cml_text):
        broken.append("SNESRECOMP_ENABLE_MODS is not forced ON before "
                      "runner.cmake, so the loader is not compiled")
    for src, got in bad_hosts:
        if got == "?":
            unreadable.append(
                f"{src} initializes mod_runtime at a root this audit cannot "
                "read — check it by hand")
        else:
            broken.append(
                f'{src} initializes mod_runtime at "{got}" while the build '
                f"stages {dest} — the Mods page would list nothing")

    if broken:
        add("mod_catalog", title, CheckStatus.FAIL, Severity.REQUIRED,
            "; ".join(broken + cleanup + unreadable), op)
    elif cleanup:
        add("mod_catalog", title, CheckStatus.WARN, Severity.RECOMMENDED,
            "; ".join(cleanup + unreadable), op)
    elif unreadable:
        # No fix op: rewriting an argument nobody can read is how a migration
        # breaks a host that was working.
        add("mod_catalog", title, CheckStatus.WARN, Severity.RECOMMENDED,
            "; ".join(unreadable))
    else:
        add("mod_catalog", title, CheckStatus.PASS, Severity.REQUIRED,
            f"{len(pkgs)} package(s) staged by {MOD_CATALOG_CALL}()."
            if pkgs else "No catalog to stage.")


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
                id=cid, title=title, status=status, severity=severity, detail=detail,
                fix_op=fix_op,
            )
        )

    is_git = _is_git_repo(root)
    if not is_git:
        notes.append("Not a git repository — submodule and untrack ops cannot run.")

    # --- framework -----------------------------------------------------------
    declared, live = _submodule_present(root, FRAMEWORK, "runner/runner.cmake")
    broken = diagnose_framework_checkout(root) if live else None
    if live and broken:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.FAIL, Severity.REQUIRED,
            broken + " Repair re-clones it as a real submodule.",
            "snes_repair_framework_submodule")
    elif live:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.PASS, Severity.REQUIRED,
            str(root / FRAMEWORK))
    elif declared:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.FAIL, Severity.REQUIRED,
            "Declared in .gitmodules but not initialised (or missing runner/runner.cmake).",
            "snes_ensure_framework_submodule")
    else:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.FAIL, Severity.REQUIRED,
            "No snesrecomp submodule — the project cannot generate or build.",
            "snes_ensure_framework_submodule")

    ui_declared, ui_live = _submodule_present(root, "recomp-ui")
    if ui_live:
        add("recomp_ui", "recomp-ui/ checkout", CheckStatus.PASS, Severity.RECOMMENDED, "")
    else:
        add("recomp_ui", "recomp-ui/ checkout",
            CheckStatus.WARN, Severity.RECOMMENDED,
            "Declared but not initialised." if ui_declared else "Not present (optional).",
            "snes_ensure_recomp_ui_submodule")

    fw = root / FRAMEWORK
    missing_nested = [
        p for p in NESTED_PATHS
        if (fw / p).is_dir() and not any((fw / p).iterdir())
    ]
    if not live:
        add("nested", "Nested libs inside snesrecomp", CheckStatus.SKIP, Severity.OPTIONAL,
            "Needs the framework checkout first.")
    elif missing_nested:
        add("nested", "Nested libs inside snesrecomp", CheckStatus.WARN, Severity.OPTIONAL,
            "Uninitialised: " + ", ".join(missing_nested), "snes_ensure_nested_modules")
    else:
        add("nested", "Nested libs inside snesrecomp", CheckStatus.PASS, Severity.OPTIONAL, "")

    # --- analysis config -----------------------------------------------------
    recomp = root / "recomp"
    cfgs = sorted(recomp.glob("bank*.cfg")) if recomp.is_dir() else []
    if cfgs and (recomp / "symbols.toml").is_file():
        add("analysis", "recomp/ analysis config", CheckStatus.PASS, Severity.REQUIRED,
            f"{len(cfgs)} bank cfg(s) + symbols.toml")
    else:
        # No fix op: seeding these needs the ROM, and a blank seed would look
        # like analysis input while proving nothing.
        add("analysis", "recomp/ analysis config", CheckStatus.FAIL, Severity.REQUIRED,
            "Missing bank*.cfg / symbols.toml. Seed them with the wizard's "
            "probe_rom.py --write-seed-cfg --write-symbols against your ROM.")

    # --- generated output ----------------------------------------------------
    gen_tracked = _tracked(root, "src/gen") + _tracked(root, "recomp/funcs.h")
    if not is_git:
        add("generated", "Generated C not committed", CheckStatus.SKIP, Severity.REQUIRED,
            "Not a git repository.")
    elif gen_tracked:
        add("generated", "Generated C not committed", CheckStatus.FAIL, Severity.REQUIRED,
            f"{len(gen_tracked)} tracked file(s) derived from the ROM.",
            "snes_untrack_generated")
    else:
        add("generated", "Generated C not committed", CheckStatus.PASS, Severity.REQUIRED, "")

    if (root / "src" / "gen").is_dir():
        add("src_gen", "src/gen/ present", CheckStatus.PASS, Severity.OPTIONAL, "")
    else:
        add("src_gen", "src/gen/ present", CheckStatus.WARN, Severity.OPTIONAL,
            "Generator output directory missing.", "snes_ensure_src_gen")

    # --- .gitignore ----------------------------------------------------------
    gi = root / ".gitignore"
    text = ""
    if gi.is_file():
        try:
            text = gi.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    existing = {ln.strip() for ln in text.splitlines()}
    missing_rules = [r for r in GITIGNORE_RULES if r not in existing]
    if not missing_rules:
        add("gitignore", ".gitignore covers ROM + generated C", CheckStatus.PASS,
            Severity.REQUIRED, "")
    else:
        add("gitignore", ".gitignore covers ROM + generated C", CheckStatus.FAIL,
            Severity.REQUIRED, "Missing: " + ", ".join(missing_rules), "snes_merge_gitignore")

    # --- scaffold files ------------------------------------------------------
    for cid, rel, op, sev in (
        ("version", "VERSION", "snes_emit_version", Severity.REQUIRED),
        ("regen", "tools/regen.sh", "snes_emit_regen", Severity.REQUIRED),
        ("packager", "scripts/package_release.sh", "snes_emit_packager", Severity.RECOMMENDED),
        ("ci", ".github/workflows/release.yml", "snes_emit_ci_workflow", Severity.RECOMMENDED),
    ):
        if (root / rel).is_file():
            add(cid, rel, CheckStatus.PASS, sev, "")
            continue
        blocked = op == "snes_emit_regen" and not rom_identity(root)
        add(
            cid,
            rel,
            CheckStatus.FAIL,
            sev,
            "Missing — ROM digests unknown, so it cannot be emitted without a "
            "--disc ROM path." if blocked else "Missing.",
            None if blocked else op,
        )

    # --- regen.sh vs the framework this port is pinned to ---------------------
    # A fork carries whatever gitlink its parent recorded, which may be far
    # older than the wizard Studio is driving. Say so on the Migrate tab rather
    # than letting it surface as an argparse error from the Build tab.
    regen_path = root / "tools" / "regen.sh"
    if regen_path.is_file():
        try:
            regen_text = regen_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            regen_text = ""
        gap = snes_paths.regen_framework_gap(root, regen_text) if regen_text else None
        if gap is not None:
            missing, have = gap
            # No fix op: moving a framework pin is a decision about what this
            # port is measured against, and that belongs to a human.
            add("regen_framework", "tools/regen.sh vs pinned snesrecomp",
                CheckStatus.FAIL, Severity.REQUIRED,
                f"regen.sh calls {', '.join(missing)}, which this port's "
                f"snesrecomp does not have (it offers {', '.join(sorted(have))}). "
                "Generate cannot work until the submodule moves to a revision "
                "that has them.")
        elif regen_text:
            add("regen_framework", "tools/regen.sh vs pinned snesrecomp",
                CheckStatus.PASS, Severity.REQUIRED,
                "The pinned snesrecomp offers everything regen.sh calls.")

    # --- which wizard is being driven ----------------------------------------
    # Provenance, because every emitted file inherits it. Silent until now, and
    # a fallback to another checkout is precisely what lets Studio write files
    # the port's own framework has never heard of.
    own_wizard = root / FRAMEWORK / "tools" / "new_project" / "setup_project.sh"
    if snes_paths.wizard_dir(root) is None:
        add("wizard_source", "Scaffold templates in use", CheckStatus.WARN,
            Severity.INFO, snes_paths.MISSING_CHECKOUT)
    elif not own_wizard.is_file():
        add("wizard_source", "Scaffold templates in use", CheckStatus.WARN,
            Severity.INFO,
            f"{FRAMEWORK}/tools/new_project is absent, so Studio is driving "
            f"{snes_paths.wizard_dir(root)} instead — a different revision from "
            "the one this port builds against.")
    else:
        add("wizard_source", "Scaffold templates in use", CheckStatus.PASS,
            Severity.INFO, f"{snes_paths.wizard_source(root)}.")

    # --- ROM / catalog identity ---------------------------------------------
    ident = rom_identity(root)
    if ident.get("sha256") and ident.get("crc32"):
        add("rom_identity", "ROM identity (digests)", CheckStatus.PASS,
            Severity.RECOMMENDED,
            f"crc32 {ident['crc32']} recovered from {_identity_source(root)}.")
    else:
        # Refresh needs the ROM (--disc) — the plan gates the op on it.
        add("rom_identity", "ROM identity (digests)", CheckStatus.WARN,
            Severity.RECOMMENDED,
            "No ROM digests recoverable — probe with a ROM path to seed "
            f"regen.sh / {_carrier_label(root)}.", "snes_probe_rom_refresh")

    # --- ROM identity carrier -------------------------------------------------
    # Audit whichever carrier the wizard this port is pinned to actually
    # scaffolds, rather than assuming either era (see IDENTITY_FILE above).
    layout = identity_layout(root)
    carriers = identity_carriers(root)
    blocked_id = not (ident.get("sha256") and ident.get("crc32"))
    if not carriers:
        # A wizard that offers neither template is a broken tool, and saying so
        # beats emitting a FAIL whose fix op would die on a missing template.
        add("identity_carrier", "ROM identity carrier", CheckStatus.WARN,
            Severity.REQUIRED,
            f"{snes_paths.templates_label(root)} has neither rom_identity.txt.in "
            "nor codegen_setup.c.in — cannot tell which carrier this framework "
            "revision wants.")
    else:
        title = " / ".join(rel for _, rel in carriers)
        emit_op = _IDENTITY_OPS[layout]
        present = [root / rel for _, rel in carriers]
        primary = present[0]
        if all(pth.is_file() for pth in present):
            try:
                text = primary.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if layout == "codegen" and "kGameCodegenIdentity" not in text:
                add("identity_carrier", title, CheckStatus.WARN, Severity.REQUIRED,
                    "codegen_setup.c missing kGameCodegenIdentity.", emit_op)
            elif re.search(r"@[A-Z0-9_]+@", text):
                add("identity_carrier", title, CheckStatus.WARN, Severity.REQUIRED,
                    f"{primary.name} still has unfilled @TOKEN@ placeholders.",
                    None if blocked_id else emit_op)
            elif layout == "file" and not all(
                parse_identity_file(primary).get(k)
                for k in ("expected_crc32", "expected_sha256")
            ):
                # An empty digest is not a formatting nit: the build says so and
                # then cannot verify the ROM it is handed.
                add("identity_carrier", title, CheckStatus.WARN, Severity.REQUIRED,
                    "rom_identity.txt carries no digests, so the build cannot "
                    "verify a ROM.", None if blocked_id else emit_op)
            else:
                add("identity_carrier", title, CheckStatus.PASS,
                    Severity.REQUIRED, "Identity present with digests.")
        else:
            add("identity_carrier", title, CheckStatus.FAIL, Severity.REQUIRED,
                "Missing — ROM digests unknown, so it cannot be emitted without "
                "a --disc ROM path." if blocked_id
                else "Missing identity carrier.",
                None if blocked_id else emit_op)

    # A port that predates the move keeps dead codegen_setup sources: this
    # framework's CMakeLists no longer compiles them, so their digests are no
    # longer the ones the build reads — two identities, one of them a lie.
    # Named, not auto-fixed: deleting a tracked source is a human's call.
    if layout == "file" and (root / "src" / "codegen_setup.c").is_file():
        add("identity_stale", "src/codegen_setup.c (superseded)", CheckStatus.WARN,
            Severity.OPTIONAL,
            "This framework revision reads rom_identity.txt and no longer "
            "compiles codegen_setup.c — remove it once rom_identity.txt is in "
            "place.")

    # --- boxart ---------------------------------------------------------------
    modern_box = root / "launcher_assets" / "img"
    modern_hit = next(
        (p for p in (modern_box / "boxart.tga", modern_box / "boxart.png")
         if p.is_file()), None)
    legacy_candidates = [
        root / "assets" / "boxart.tga", root / "assets" / "boxart.png",
        root / "boxart.tga", root / "boxart.png",
    ]
    legacy_box = next((p for p in legacy_candidates if p.is_file()), None)
    if modern_hit is not None:
        add("boxart", "launcher_assets boxart", CheckStatus.PASS, Severity.OPTIONAL,
            str(modern_hit.relative_to(root)))
    elif legacy_box is not None:
        add("boxart", "launcher_assets boxart", CheckStatus.WARN, Severity.OPTIONAL,
            f"Boxart at {legacy_box.relative_to(root)} — relocate to "
            "launcher_assets/img/.", "snes_relocate_boxart")
    else:
        add("boxart", "launcher_assets boxart", CheckStatus.WARN, Severity.OPTIONAL,
            "No boxart found (optional; README patch can fetch libretro art).",
            "snes_emit_boxart_stub")

    # --- README metrics / launcher / RAID ------------------------------------
    # Reported as skipped rather than dropped: a row that silently disappears
    # reads as "nothing to do here", which is the opposite of what the switch
    # means. SKIP carries no fix op, so it never reaches failing_ops() and the
    # layout classification below stops counting it as a recommended warning.
    if not options.patch_readme:
        add("readme_metrics", "README download metrics / launcher / RAID / boxart",
            CheckStatus.SKIP, Severity.INFO,
            "Skipped — README & About is off for this repo.")
    else:
        _audit_readme(root, add)

    # --- netplay wiring -------------------------------------------------------
    # Report-only: netplay is opt-in, so the plan adds the enable/disable op
    # from --enable-netplay / --disable-netplay, never from a failing check.
    cml = root / "CMakeLists.txt"
    cml_text = ""
    if cml.is_file():
        try:
            cml_text = cml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            cml_text = ""
    if _netplay_wired(cml_text):
        add("netplay", "Netplay (recomp-net) wiring", CheckStatus.PASS,
            Severity.INFO,
            "snesrecomp_enable_recomp_net() wired in CMakeLists.txt.")
    elif cml_text:
        add("netplay", "Netplay (recomp-net) wiring", CheckStatus.WARN,
            Severity.INFO,
            "Not wired — apply with --enable-netplay to add the "
            "snesrecomp_enable_recomp_net() call (host code must also wire "
            "the launcher; see MetalWarriorsSNESRecomp src/main.c).")

    # --- mod catalog ----------------------------------------------------------
    # Two failures live here and only one of them is loud. runner.cmake aborts
    # configure when packages exist that no target declared; nothing at all
    # complains when the host reads a different directory than the build
    # stages into, and that one ships an empty Mods page in a release zip.
    _audit_mod_catalog(root, cml_text, live, add)
    _audit_regen_ownership(root, options, add)

    # --- lobby pin stamp vs VERSION ------------------------------------------
    # Fires only when a build tree carries a version stamp; drift between the
    # stamp and VERSION splits netplay lobbies onto different pins.
    ver_path = root / "VERSION"
    ver_text = ""
    if ver_path.is_file():
        try:
            ver_text = ver_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            ver_text = ""
    stamp_hits: list[tuple[str, str]] = []
    for build in sorted(root.glob("build*")):
        if not build.is_dir():
            continue
        for stamp in (build / "snes_game_version.txt",
                      build / "Release" / "snes_game_version.txt"):
            if stamp.is_file():
                try:
                    stamp_hits.append(
                        (str(stamp.relative_to(root)).replace("\\", "/"),
                         stamp.read_text(encoding="utf-8",
                                         errors="replace").strip()))
                except OSError:
                    pass
    if ver_text and stamp_hits:
        bad = [(pth, st) for pth, st in stamp_hits
               if st and st.lstrip("vV") != ver_text.lstrip("vV")]
        if bad:
            detail = "; ".join(f"{pth}={st} (VERSION={ver_text})"
                               for pth, st in bad[:3])
            add("version_stamp_match", "Lobby pin stamp", CheckStatus.FAIL,
                Severity.REQUIRED,
                "snes_game_version.txt disagrees with VERSION — rebuild with "
                "-DSNES_GAME_VERSION matching VERSION before releasing. "
                + detail)
        else:
            add("version_stamp_match", "Lobby pin stamp", CheckStatus.PASS,
                Severity.RECOMMENDED, "Build stamp matches VERSION.")

    pins = root / "framework_pins.txt"
    if pins.is_file():
        stale = _stale_pins(root, pins)
        if stale:
            add("pins", "framework_pins.txt matches gitlinks", CheckStatus.WARN,
                Severity.RECOMMENDED, "Stale: " + ", ".join(stale),
                "snes_record_framework_pins")
        else:
            add("pins", "framework_pins.txt matches gitlinks", CheckStatus.PASS,
                Severity.RECOMMENDED, "")
    else:
        add("pins", "framework_pins.txt matches gitlinks", CheckStatus.WARN,
            Severity.RECOMMENDED, "Missing.", "snes_record_framework_pins")

    layout = _classify(checks, live)
    return AuditReport(
        root=str(root),
        layout=layout,
        project_name=project_name(root),
        boot_exe=None,  # SNES boots from the cartridge vector, not a named EXE
        checks=checks,
        notes=notes,
    )


def _classify(checks: list[CheckResult], have_framework: bool) -> LayoutClass:
    fails = sum(1 for c in checks if c.status == CheckStatus.FAIL)
    if not have_framework:
        return LayoutClass.UNKNOWN
    # Committed ROM-derived C is the pre-scaffold layout: the repo was built
    # by checking generator output in rather than regenerating from the ROM.
    if any(c.id == "generated" and c.status == CheckStatus.FAIL for c in checks):
        return LayoutClass.LEGACY_PACKAGING
    if fails == 0:
        return LayoutClass.SCAFFOLD_COMPLETE
    return LayoutClass.SETUP_HOST_PARTIAL


def _current_pins(root: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for path in (FRAMEWORK, "recomp-ui"):
        sub = root / path
        if sub.is_dir():
            code, out = _git(sub, "rev-parse", "HEAD")
            if code == 0 and out:
                pins[path] = out
    fw = root / FRAMEWORK
    for nested in NESTED_PATHS:
        sub = fw / nested
        if sub.is_dir():
            code, out = _git(sub, "rev-parse", "HEAD")
            if code == 0 and out:
                pins[Path(nested).name] = out
    return pins


def _stale_pins(root: Path, pins_file: Path) -> list[str]:
    try:
        text = pins_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["unreadable"]
    recorded = {}
    for line in text.splitlines():
        key, _, val = line.partition("=")
        if key.strip() and val.strip():
            recorded[key.strip()] = val.strip()
    stale: list[str] = []
    for key, sha in _current_pins(root).items():
        if recorded.get(key) != sha:
            stale.append(key)
    return stale


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
        wanted.add("snes_record_framework_pins")
    else:
        wanted.discard("snes_record_framework_pins")
    if not options.enable_ci:
        wanted.discard("snes_emit_ci_workflow")
    if not options.patch_readme:
        wanted.discard("snes_patch_readme_metrics")
    if not options.merge_gitignore:
        wanted.discard("snes_merge_gitignore")
    if not options.enable_recomp_ui:
        wanted.discard("snes_ensure_recomp_ui_submodule")
    # The probe rewrites identity files from a real ROM: never planned without
    # one, always planned when the user asked for it and supplied one.
    if options.probe_disc and options.disc:
        wanted.add("snes_probe_rom_refresh")
    else:
        wanted.discard("snes_probe_rom_refresh")
    # Netplay is opt-in both ways: only an explicit flag plans a flip, and the
    # audit row never does (severity INFO, no auto-planned fix op).
    if options.enable_netplay and options.players >= 2:
        wanted.add("snes_enable_netplay")
    if options.disable_netplay:
        wanted.discard("snes_enable_netplay")
        wanted.add("snes_disable_netplay")

    if options.only:
        wanted = {o for o in wanted if o in options.only} | set(options.only)
    if options.skip:
        wanted -= set(options.skip)

    ordered = [op for op in OP_ORDER if op in wanted]
    ordered.extend(sorted(op for op in wanted if op not in ordered))

    # Everything is ticked by default except a step that would knowingly drop
    # capability: adopting a regen.sh whose port had grown past the wizard's is
    # a choice, so it is shown, explained, and left for the user to tick.
    #
    # `--only` overrides that, and must: it IS the user ticking the box (the
    # GUI's Apply sends the ticked ops as --only). Without this, naming the op
    # explicitly put it in the plan and then apply_plan skipped it for being
    # unselected — printing no line at all, so the op appeared to do nothing.
    # A silent no-op is worse than either running or refusing.
    explicit = set(options.only or ())
    lossy_adoption = bool(regen_adoption_report(root, options).lost)
    steps = [
        PlanStep(
            op_id=op,
            title=OP_TITLES.get(op, op),
            detail=next((c.detail for c in report.checks if c.fix_op == op), ""),
            selected=(
                op in explicit
                or not (op == "snes_adopt_framework_regen" and lossy_adoption)
            ),
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
    requested = set(opts.only or ())
    for step in plan.steps:
        if not step.selected:
            # Never drop a step the caller asked for without a word. The
            # selection default exists to stop a lossy op running unasked, not
            # to swallow an explicit request.
            if step.op_id in requested:
                results.append(ApplyResult(
                    step.op_id, False,
                    "Requested but not selected — this is a Studio bug; "
                    "please report it with the op name",
                ))
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


def _op_ensure_framework(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _ensure_submodule(root, opts, FRAMEWORK)


def _op_ensure_recomp_ui(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _ensure_submodule(root, opts, "recomp-ui")


def _ensure_submodule(root: Path, opts: MigrateOptions, path: str) -> ApplyResult:
    from .gitops import DEFAULT_RECOMP_UI_URL, ensure_submodule, framework_branch, framework_url

    op = "snes_ensure_framework_submodule" if path == FRAMEWORK \
        else "snes_ensure_recomp_ui_submodule"
    if not _is_git_repo(root):
        return ApplyResult(op, False, f"{root} is not a git repository")
    if path == FRAMEWORK:
        url, branch = framework_url(), framework_branch()
    else:
        url, branch = DEFAULT_RECOMP_UI_URL, "master"
    r = ensure_submodule(root, path, url=url, branch=branch, dry_run=_dry(opts))
    return ApplyResult(op, r.ok, r.message, [path] if r.ok else [])


def _op_ensure_nested(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_ensure_nested_modules"
    fw = root / FRAMEWORK
    if not fw.is_dir():
        return ApplyResult(op, False, "No snesrecomp checkout")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] submodule update --init in {fw}")
    # Delegate rather than re-run `submodule update` here. gitops already
    # knows how to heal a .gitmodules section with no url — the exact state
    # several engine pins are in for lib/retcomm-rbengine — and a second,
    # weaker implementation in this file cannot inherit fixes to that one.
    from .gitops import ensure_nested_modules

    heal = ensure_nested_modules(root)
    failed = [r for r in heal if not r.ok]
    code, out = _git(fw, "submodule", "update", "--init", "--recursive", *NESTED_PATHS)
    ok = code == 0
    if not ok and failed:
        out = f"{out} (also: " + "; ".join(r.message for r in failed) + ")"
    return ApplyResult(op, ok, "Initialised nested libs" if ok else f"git failed: {out}",
                       [f"{FRAMEWORK}/{p}" for p in NESTED_PATHS] if ok else [])


def _op_merge_gitignore(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_merge_gitignore"
    gi = root / ".gitignore"
    text = ""
    if gi.is_file():
        text = gi.read_text(encoding="utf-8", errors="replace")
    existing = {ln.strip() for ln in text.splitlines()}
    missing = [r for r in GITIGNORE_RULES if r not in existing]
    if not missing:
        return ApplyResult(op, True, ".gitignore already complete")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would append {len(missing)} rule(s)")
    block = "\n# --- SNES recomp (Studio) ---\n" + "\n".join(missing) + "\n"
    if text and not text.endswith("\n"):
        block = "\n" + block
    gi.write_text(text + block, encoding="utf-8", newline="\n")
    return ApplyResult(op, True, f"Appended {len(missing)} rule(s)", [".gitignore"])


_GENERATED_PATHSPECS = ("src/gen", "recomp/funcs.h")


def _op_untrack_generated(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_untrack_generated"
    # Per pathspec, not one combined call: `git rm` fails the whole command on
    # any pathspec that matches nothing, and the usual case is exactly that —
    # src/gen committed, recomp/funcs.h not.
    hits = {spec: _tracked(root, spec) for spec in _GENERATED_PATHSPECS}
    live = [spec for spec, files in hits.items() if files]
    tracked = [f for files in hits.values() for f in files]
    if not tracked:
        return ApplyResult(op, True, "Nothing tracked")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would untrack {len(tracked)} file(s)")
    # --cached only: the working tree keeps the files, so a developer mid-build
    # does not lose the C they just generated.
    for spec in live:
        code, out = _git(root, "rm", "-r", "--cached", "-q", "--", spec)
        if code != 0:
            return ApplyResult(op, False, f"git rm --cached {spec} failed: {out}")
    return ApplyResult(op, True, f"Untracked {len(tracked)} file(s) (working tree kept)",
                       tracked[:20])


def _op_ensure_src_gen(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_ensure_src_gen"
    keep = root / "src" / "gen" / ".gitkeep"
    if keep.is_file():
        return ApplyResult(op, True, "src/gen/.gitkeep already present")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would create {keep}")
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("", encoding="utf-8")
    return ApplyResult(op, True, "Created src/gen/.gitkeep", ["src/gen/.gitkeep"])


def _op_emit_version(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_emit_version"
    dst = root / "VERSION"
    if dst.is_file() and not opts.force:
        return ApplyResult(op, True, "VERSION already present")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would write {dst}")
    dst.write_text("0.1.0\n", encoding="utf-8", newline="\n")
    return ApplyResult(op, True, "Wrote VERSION (0.1.0)", ["VERSION"])


# A mod package's [[target]] names the title it applies to as
# `game_id = "..."`, and the runtime matches it with ==  against the id
# compiled in from rom_identity.txt (snesrecomp runner/src/mod_runtime.cpp,
# target_matches). So game_id is not a cosmetic slug: get it wrong and every
# mod the port ships stops applying, with the player seeing only "This feature
# does not support the selected stock ROM."
_MOD_MANIFEST_GLOB = "mods/*/packages/*/*/manifest.toml"
_GAME_ID_RE = re.compile(r'^\s*game_id\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def recorded_game_ids(root: Path) -> dict[str, list[str]]:
    """Every game_id this port's own mod manifests name → which files name it."""
    found: dict[str, list[str]] = {}
    for manifest in sorted(root.glob(_MOD_MANIFEST_GLOB)):
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for gid in _GAME_ID_RE.findall(text):
            rel = str(manifest.relative_to(root)).replace("\\", "/")
            found.setdefault(gid.strip(), []).append(rel)
    return found


def resolve_game_id(root: Path, opts: MigrateOptions, ident: dict[str, str]) -> tuple[str, str]:
    """``(game_id, how)`` — read from what the port already commits to.

    Deliberately NOT derived first. The framework's scaffolder derives it once,
    at `setup_project.sh` time, from a project name Studio never sees
    (`safe_slug(NAME).lower()` + region), and rom_identity.txt then records the
    answer because it has to stay stable across revisions. Re-deriving it here
    reproduces that string only by luck: this port's mods name
    `zelda-alttp-us`, while the same formula on its recorded region ("USA")
    and display name yields `zeldaalttp-usa`. Writing the derived one would
    silently orphan every mod package in the repo — the kind of wrong value
    that looks like a successful run.

    So the order is read, read, ask — and the caller refuses to write the file
    at all rather than invent one.
    """
    explicit = (getattr(opts, "game_id", "") or "").strip()
    if explicit:
        return explicit, "--game-id"
    recorded = (ident.get("game_id") or "").strip()
    if recorded:
        return recorded, f"recorded in {IDENTITY_FILE}"
    ids = recorded_game_ids(root)
    if len(ids) == 1:
        gid, files = next(iter(ids.items()))
        return gid, f"named by {len(files)} mod manifest(s), e.g. {files[0]}"
    if ids:
        # Several, disagreeing. Picking one is a coin toss that breaks the
        # packages naming the other.
        return "", ""
    # No manifests at all — and that changes the answer. The whole reason not
    # to derive is that a derived id can silently match no [[target]] and
    # orphan the port's own mods. With no mod package in the repo there is
    # nothing to orphan: game_id has no consumer yet, and it becomes the
    # string future manifests must name. Refusing here blocked the identity
    # carrier the current framework *requires* over a field nothing reads,
    # which is a worse failure than deriving the scaffolder's own answer and
    # saying so out loud.
    derived, how = _scaffolder_game_id(root, ident)
    if derived:
        return derived, how
    return "", ""


def _scaffolder_game_id(root: Path, ident: dict[str, str]) -> tuple[str, str]:
    """``<slug>-<region>``, by the scaffolder's own rule and its own slugger.

    ``safe_slug`` is imported from the wizard rather than reimplemented: it is
    the function ``setup_project.sh`` line 343 uses, and a second copy here
    would drift from it — the exact way a port ends up with an id the
    framework would never have generated.
    """
    region = (ident.get("region") or "").strip().lower()
    if not region:
        return "", ""
    display = (ident.get("display_name") or "").strip() or display_name(root)
    if not display:
        return "", ""
    import sys as _sys

    wizard_path = snes_paths.wizard_dir(root)
    if wizard_path is None:
        return "", ""
    wizard = str(wizard_path)
    added = wizard not in _sys.path
    if added:
        _sys.path.insert(0, wizard)
    try:
        from probe_rom import safe_slug  # type: ignore
    except ImportError:
        return "", ""
    finally:
        if added and wizard in _sys.path:
            _sys.path.remove(wizard)
    slug = safe_slug(display).lower()
    if not slug:
        return "", ""
    return f"{slug}-{region}", (
        f"derived as <slug>-<region> from {display!r} + {region.upper()}, the "
        "scaffolder's own rule — this repo ships no mod package to read it "
        "from, so nothing can be orphaned; future manifests must name this"
    )


def _template_values(root: Path, opts: MigrateOptions) -> dict[str, str]:
    name = opts.project_name or project_name(root)
    shown = display_name(root)
    values = {
        "PROJECT_NAME": name,
        "DISPLAY_NAME": shown,
        "ZIP_PREFIX": opts.zip_prefix or _zip_prefix(root),
        "DEFAULT_BRANCH": default_branch(root),
    }
    ident = rom_identity(root, opts.disc)
    if ident.get("display_name"):
        values["DISPLAY_NAME"] = ident["display_name"]
    # ROM_MD5 / ROM_SHA1 / ROM_SIZE: the identity file carries every digest
    # the catalog matches on since snesrecomp 7c5fdc5, so the template needs
    # them; a project that predates them gets "" (the wizard's probe fills
    # them on the next probe refresh).
    for token, key in (("ROM_CRC32", "crc32"), ("ROM_SHA256", "sha256"),
                       ("ROM_MD5", "md5"), ("ROM_SHA1", "sha1"), ("ROM_SIZE", "rom_size"),
                       ("ROM_FILE", "rom_file"), ("ROM_MAPPING", "mapping"),
                       ("REGION", "region")):
        if ident.get(key):
            values[str(token)] = str(ident[key])
    for token in ("ROM_MD5", "ROM_SHA1", "ROM_SIZE"):
        values.setdefault(token, "")
    if values.get("ROM_FILE"):
        values["ROM_SLUG"] = Path(values["ROM_FILE"]).stem
    gid, _how = resolve_game_id(root, opts, ident)
    if gid:
        values["GAME_ID"] = gid
    return values


_TOKEN_RE = re.compile(r"@([A-Z0-9_]+)@")


def _render(text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """@TOKEN@ substitution, matching the wizard's fill_tokens.py contract.

    Reimplemented rather than imported: the Studio toolkit ships its own
    top-level ``fill_tokens.py`` for PSX, and putting the SNES wizard on
    sys.path makes which one you get depend on import order. Six lines of
    regex is a smaller liability than that. Unknown tokens are returned, never
    silently blanked — a blank in a CI workflow surfaces much later and much
    worse.
    """
    missing: list[str] = []

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return values[key]

    return _TOKEN_RE.sub(replace, text), missing


def _fill_template(
    root: Path, opts: MigrateOptions, op: str, template: str, rel: str
) -> ApplyResult:
    tdir = snes_paths.templates_dir(root)
    if tdir is None:
        return ApplyResult(op, False, snes_paths.MISSING_CHECKOUT)
    src = tdir / template
    if not src.is_file():
        return ApplyResult(op, False, f"Template not found: {src}")
    dst = root / rel
    if dst.is_file() and not opts.force:
        return ApplyResult(op, True, f"{rel} already present (use --force to overwrite)")

    rendered, missing = _render(src.read_text(encoding="utf-8"), _template_values(root, opts))
    if missing:
        # Refuse rather than emit a file with @TOKEN@ or a blank in it. The
        # commonest missing token is a ROM digest, and that is exactly the value
        # nobody should be inventing.
        return ApplyResult(
            op, False,
            f"{rel} not written — unresolved tokens: "
            f"{', '.join(sorted(set(missing)))}"
            + _token_help(root, sorted(set(missing))),
        )
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would write {rel} from {template}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered, encoding="utf-8", newline="\n")
    if rel.endswith(".sh"):
        dst.chmod(dst.stat().st_mode | 0o111)
    return ApplyResult(op, True, f"Wrote {rel} ({snes_paths.wizard_source(root)})", [rel])


def _token_help(root: Path, missing: list[str]) -> str:
    """What to do about the tokens that did not resolve.

    "unresolved tokens: GAME_ID" names a template variable and no action, and
    the action is not the same for every token: a missing ROM digest means
    probe the ROM, while a missing GAME_ID means nothing in the repo records
    the id its own mod packages match against — a value that must be supplied,
    never guessed.
    """
    if "GAME_ID" not in missing:
        return ""
    ids = recorded_game_ids(root)
    if len(ids) > 1:
        listed = "; ".join(
            f"{gid} ({', '.join(files)})" for gid, files in sorted(ids.items())
        )
        return (
            f". This port's mod manifests disagree about game_id — {listed} — "
            "so there is no single id to record. Fix the manifests, or pass "
            "--game-id to say which one this build is."
        )
    return (
        f". Nothing in this repo records a game_id: no {IDENTITY_FILE} to read "
        "it from and no mod manifest naming one. It is the id a mod package's "
        "[[target]] matches with == (snesrecomp docs/MOD_PACKAGES.md), so it "
        "cannot be derived from the ROM without risking one that silently "
        "matches nothing — pass --game-id (the scaffolder's own form is "
        "<slug>-<region>, e.g. zelda-alttp-us)."
    )


def _regen_gap(root: Path, rendered: str) -> str | None:
    """The reason this regen.sh would not run here, or None.

    Studio drives whichever wizard it can find; the port runs whichever
    snesrecomp its gitlink records. On a fork those are routinely different
    revisions, and writing the newer wizard's regen.sh into the older
    framework's repo is exactly how a port ends up with a script that dies on
    `invalid choice: 'verify-rom'` the first time anyone presses Generate.
    Refusing to write it is the fix: the file Studio was about to create is the
    defect, and creating it anyway only moves the failure later.
    """
    from .buildops import framework_gap_message

    gap = snes_paths.regen_framework_gap(root, rendered)
    if gap is None:
        return None
    missing, have = gap
    cli = snes_paths.regen_framework_root(root) / "snesrecomp_cli.py"
    return framework_gap_message(cli, missing, have)


# ---------------------------------------------------------------------------
# Handing tools/regen.sh back to the framework
#
# A port carrying its own regen.sh is carrying a fork of engine tooling: fixes
# to the wizard's script never reach it, and Studio and CI cannot drive it
# (MegaManX's answers `--rom` with "unknown argument"). The end state is one
# script, owned upstream, emitted per port.
#
# What this must never do is mistake a fork for a duplicate. A hand-written
# driver may have grown capability the wizard's script has no way to express,
# and replacing it then looks like a successful cleanup while quietly deleting
# the only way to build half the project.
# ---------------------------------------------------------------------------
REGEN_REL = "tools/regen.sh"


def _rendered_regen_template(root: Path, opts: MigrateOptions) -> str:
    """The wizard's regen.sh as it would be written for THIS port, or "".

    Rendered, not raw: the comparison is against the file that would actually
    replace the port's, and against the template generation this port's own
    framework checkout carries — another checkout is a different vintage and
    diffing against it invents losses that are not there.
    """
    tdir = snes_paths.templates_dir(root)
    if tdir is None:
        return ""
    src = tdir / "regen.sh.in"
    if not src.is_file():
        return ""
    try:
        rendered, _missing = _render(
            src.read_text(encoding="utf-8"), _template_values(root, opts))
    except OSError:
        return ""
    return rendered


@dataclass
class RegenAdoption:
    """Whether tools/regen.sh can be handed back to the framework, and at what cost."""

    state: str = "absent"          # absent | framework | port
    lost: list[str] = field(default_factory=list)   # capability adoption would drop
    notes: list[str] = field(default_factory=list)  # interface differences, not capability
    blocker: str = ""              # adoption impossible, regardless of consent


def regen_adoption_report(
    root: Path, opts: MigrateOptions | None = None
) -> RegenAdoption:
    """Read this port's tools/regen.sh and say what adopting would mean.

    ``blocker`` is a reason adoption cannot happen at all — a pinned framework
    that could not run the replacement, which is the same rule
    :func:`_fill_regen` already applies. ``lost`` is capability; ``notes`` are
    interface differences that a caller should hear about but that do not
    justify refusing.
    """
    opts = opts or MigrateOptions()
    root = Path(root).expanduser().resolve()
    script = root / REGEN_REL
    if not script.is_file():
        return RegenAdoption("absent")
    try:
        existing = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return RegenAdoption("absent")
    if not snes_paths.regen_is_port_authored(existing):
        return RegenAdoption("framework")
    rendered = _rendered_regen_template(root, opts)
    if not rendered:
        return RegenAdoption("port", blocker=(
            f"no regen.sh.in in {snes_paths.templates_label(root)} — there is no "
            "framework script to adopt"
        ))
    lost, notes = snes_paths.regen_capability_delta(existing, rendered)
    return RegenAdoption("port", lost, notes, _regen_gap(root, rendered) or "")


def regen_ownership_guidance(root: Path, opts: MigrateOptions | None = None) -> str:
    """What to actually do about a port-owned tools/regen.sh, in this repo.

    The three adoption states need three different instructions, and picking
    the wrong one is worse than saying nothing. Generate used to tell everyone
    to tick "snes_adopt_framework_regen" in Migrate — including a port where
    the audit deliberately offers no such step, because the pinned framework
    cannot run the replacement. Super Metroid is that port, so the message sent
    the user hunting for a checkbox that was never rendered. Both sentences now
    come from :func:`regen_adoption_report`, so they cannot disagree.
    """
    rep = regen_adoption_report(root, opts or MigrateOptions())
    if rep.state != "port":
        return ""
    if rep.blocker:
        return (
            "Migrate offers no step for this and should not: the framework's "
            f"script would not run here — {rep.blocker} Until that pin moves, "
            "this port's own script is the working path: run "
            "`bash tools/regen.sh` in the repo (its --help lists the arguments, "
            "and it says which ROM it expects where)."
        )
    if rep.lost:
        return (
            f'Migrate → Audit + Plan lists "{ADOPT_REGEN_TITLE}" UNTICKED, '
            "because adopting would drop: " + "; ".join(rep.lost)
            + ". Tick it and tick Force to accept that, or keep this port's "
              "script and run `bash tools/regen.sh` by hand."
        )
    return (
        f'Migrate → Audit + Plan, tick "{ADOPT_REGEN_TITLE}", Apply — it '
        "replaces the script with the wizard's, which Studio and CI both know "
        "how to drive."
    )


def _audit_regen_ownership(
    root: Path,
    options: MigrateOptions,
    add: Callable[..., None],
) -> None:
    """Who owns tools/regen.sh — and whether handing it back is mechanical."""
    title = "regen.sh ownership"
    op = "snes_adopt_framework_regen"
    rep = regen_adoption_report(root, options)
    state, lost, blocker = rep.state, rep.lost, rep.blocker
    if state == "absent":
        add("regen_ownership", title, CheckStatus.SKIP, Severity.OPTIONAL,
            "No tools/regen.sh yet.")
        return
    if state == "framework":
        add("regen_ownership", title, CheckStatus.PASS, Severity.RECOMMENDED,
            "tools/regen.sh is the framework's — engine fixes reach this port "
            "on a submodule bump.")
        return
    detail = (
        "tools/regen.sh is this port's own, not the framework's: engine fixes "
        "never reach it and Studio cannot drive it (it takes no --rom)"
    )
    if blocker:
        # No fix op: the replacement would not run here, and writing it anyway
        # is the defect this refuses to create.
        add("regen_ownership", title, CheckStatus.WARN, Severity.RECOMMENDED,
            f"{detail}. Cannot adopt the framework's yet — {blocker}")
        return
    if lost:
        # The fix op is offered, but build_plan leaves this one UNTICKED and the
        # op still refuses without --force. Withholding the op entirely was the
        # first shape and it was worse: the only route left was the CLI, so the
        # GUI showed a defect with no way to act on it. Visible and opt-in beats
        # invisible, and neither auto-applies.
        add("regen_ownership", title, CheckStatus.WARN, Severity.RECOMMENDED,
            f"{detail}. Adopting the framework's script would drop: "
            + "; ".join(lost)
            + ". Teach the framework's regen.sh.in to express these, then "
              "adopt — or tick Force to accept the loss.", op)
        return
    add("regen_ownership", title, CheckStatus.WARN, Severity.RECOMMENDED,
        f"{detail}. Nothing it does is missing from the framework's script, "
        "so adopting is mechanical."
        + ("; " + "; ".join(rep.notes) if rep.notes else ""), op)


def _port_tools_inventory(root: Path) -> tuple[list[str], list[str]]:
    """``(engine_named, port_specific)`` for everything else in tools/.

    Reported, never deleted. "Remove the tools folder" is right for a port
    whose tools/ held nothing but the wizard's script, and wrong for one that
    keeps its own research tooling there — MegaManX has 48 other files
    (eye_*.py, oracle_*.py, sprite_cap.py …) and not one of them shares a name
    with anything the framework ships. Deleting a folder because of its name
    is how a port loses work that no engine fix will ever bring back.
    """
    tools = Path(root) / "tools"
    if not tools.is_dir():
        return [], []
    fw = snes_paths.snesrecomp_root(root)
    engine_names: set[str] = set()
    if fw is not None:
        for d in (fw / "tools", fw / "tools" / "new_project"):
            if d.is_dir():
                engine_names.update(x.name for x in d.iterdir())
    engine_named: list[str] = []
    port_specific: list[str] = []
    for entry in sorted(tools.iterdir()):
        if entry.name == "regen.sh":
            continue
        (engine_named if entry.name in engine_names else port_specific).append(entry.name)
    return engine_named, port_specific


def _op_adopt_framework_regen(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Replace a port-owned tools/regen.sh with the framework's."""
    op = "snes_adopt_framework_regen"
    root = Path(root).expanduser().resolve()
    rep = regen_adoption_report(root, opts)
    state, lost, blocker = rep.state, rep.lost, rep.blocker
    if state == "absent":
        return ApplyResult(op, True, "No tools/regen.sh to adopt")
    if state == "framework":
        return ApplyResult(op, True, "tools/regen.sh is already the framework's")
    if blocker:
        return ApplyResult(
            op, False,
            "Not adopted — the framework's regen.sh would not run against "
            f"this port's own snesrecomp. {blocker}",
        )
    if lost and not opts.force:
        return ApplyResult(
            op, False,
            "Not adopted — this port's regen.sh does things the framework's "
            "cannot express, so replacing it would delete capability, not "
            "duplication: " + "; ".join(lost)
            + ". Teach snesrecomp's tools/new_project/templates/regen.sh.in to "
              "express these and adopt afterwards, or re-run with --force to "
              "accept the loss.",
        )
    script = root / REGEN_REL
    engine_named, port_specific = _port_tools_inventory(root)
    notes: list[str] = list(rep.notes)
    if lost:
        notes.append("--force accepted the loss of: " + "; ".join(lost))
    if _dry(opts):
        msg = (
            f"[dry-run] would replace {REGEN_REL} with the framework's "
            f"({snes_paths.wizard_source(root)})"
        )
        return ApplyResult(op, True, "; ".join([msg, *notes]))
    try:
        script.unlink()
    except OSError as exc:
        return ApplyResult(op, False, f"Could not remove {REGEN_REL}: {exc}")
    # force=False on purpose: the old script is gone, so there is nothing to
    # overwrite, and _fill_regen's guard against silently clobbering a
    # port-owned script stays armed for every other caller.
    import dataclasses as _dc

    res = _fill_regen(root, _dc.replace(opts, force=False), op)
    if not res.ok:
        return res
    # tools/ itself deliberately stays. The framework's regen.sh is emitted
    # INTO tools/regen.sh — that is where the wizard puts it and where CI and
    # Studio look — so "remove the tools folder" cannot be part of adopting
    # it: the folder is where the adopted script lives. What ends here is the
    # fork, not the directory. Removing the port-local script entirely would
    # mean teaching Studio and every port's CI to call snesrecomp_cli directly,
    # which is a framework decision, not a per-port migration.
    if engine_named:
        notes.append(
            "tools/ also has files the framework ships by the same name, worth "
            "a look: " + ", ".join(engine_named))
    if port_specific:
        notes.append(
            f"{len(port_specific)} other file(s) in tools/ are this port's own "
            "and were left alone (nothing the framework ships shares their "
            "names)")
    msg = f"tools/regen.sh is now the framework's ({snes_paths.wizard_source(root)})"
    return ApplyResult(op, True, "; ".join([msg, *notes]), [REGEN_REL])


def _fill_regen(root: Path, opts: MigrateOptions, op: str) -> ApplyResult:
    """tools/regen.sh, but never a version the pinned framework cannot run.

    And never over one the port wrote itself. "Never edit generated output"
    has a mirror: hand-authored output is not Studio's to regenerate. Probe
    ROM refreshes the identity carriers with ``force=True`` — it has to, that
    is the op — and that force reached regen.sh too, so on a port whose
    regen.sh is its own the only thing standing between Studio and replacing
    it was an unrelated framework-version check. MegaManXSNESRecomp's is a
    130-line multi-variant driver (a second regional variant, per-variant
    profile manifests, a strict-idempotency pass, a native-analyzer build);
    the wizard's single-variant script would have silently replaced all of it
    the moment that port advanced its submodule pin.
    """
    dst = root / "tools" / "regen.sh"
    if dst.is_file() and opts.force:
        try:
            existing = dst.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        if existing and snes_paths.regen_is_port_authored(existing):
            return ApplyResult(
                op, True,
                "tools/regen.sh left alone — this port wrote its own (it does "
                "not drive the framework CLI the wizard's does). Overwriting "
                "it would drop whatever it adds; re-emit deliberately with "
                "Emit tools/regen.sh if that is really what you want.",
            )
    tdir = snes_paths.templates_dir(root)
    src = tdir / "regen.sh.in" if tdir is not None else None
    if src is not None and src.is_file():
        try:
            rendered, _ = _render(src.read_text(encoding="utf-8"),
                                  _template_values(root, opts))
        except OSError:
            rendered = ""
        why = _regen_gap(root, rendered) if rendered else None
        if why:
            return ApplyResult(
                op, False,
                "tools/regen.sh not written — it would not run against this "
                f"port's own snesrecomp. {why}")
    return _fill_template(root, opts, op, "regen.sh.in", "tools/regen.sh")


def _op_emit_regen(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_regen(root, opts, "snes_emit_regen")


def _op_emit_packager(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_template(root, opts, "snes_emit_packager", "package_release.sh.in",
                          "scripts/package_release.sh")


def _op_emit_ci(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_template(root, opts, "snes_emit_ci_workflow", "release.yml.in",
                          ".github/workflows/release.yml")


def _op_record_pins(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_record_framework_pins"
    pins = _current_pins(root)
    if not pins:
        return ApplyResult(op, False, "No initialised submodules to pin")
    text = "".join(f"{k}={v}\n" for k, v in pins.items())
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would write {len(pins)} pin(s)")
    (root / "framework_pins.txt").write_text(text, encoding="utf-8", newline="\n")
    return ApplyResult(op, True, f"Wrote {len(pins)} pin(s)", ["framework_pins.txt"])


_NETPLAY_CALL_RE = re.compile(
    r"^([ \t]*)snesrecomp_enable_recomp_net\(([^)]*)\)", re.M)
_NETPLAY_DISABLED_RE = re.compile(
    r"^([ \t]*)#[ \t]*snesrecomp_enable_recomp_net\(([^)]*)\)", re.M)


def _netplay_wired(cml_text: str) -> bool:
    return bool(_NETPLAY_CALL_RE.search(cml_text))


def _cml_target_name(cml_text: str) -> str | None:
    m = re.search(r"add_executable\(\s*([A-Za-z0-9_.-]+)", cml_text)
    return m.group(1) if m else None


def _op_enable_netplay(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Wire snesrecomp_enable_recomp_net(<target>) into CMakeLists.txt.

    Build-side flip only: it links the delay-sync engine and lobby client and
    defines SNES_HAS_LOBBY_CLIENT. The launcher's netplay button additionally
    needs host wiring in src/main.c (gi.netplay_supported + the barrier loop);
    scaffolds from the current wizard template carry it, older hosts follow
    MetalWarriorsSNESRecomp's src/main.c.
    """
    op = "snes_enable_netplay"
    cml = root / "CMakeLists.txt"
    if not cml.is_file():
        return ApplyResult(op, False, "No CMakeLists.txt")
    text = cml.read_text(encoding="utf-8", errors="replace")
    if _netplay_wired(text):
        return ApplyResult(op, True, "Netplay already wired")
    changed_note = ""
    m = _NETPLAY_DISABLED_RE.search(text)
    if m:
        new_text = text[: m.start()] + m.group(1) \
            + f"snesrecomp_enable_recomp_net({m.group(2)})" + text[m.end():]
        changed_note = "Uncommented the existing call"
    else:
        target = _cml_target_name(text)
        if not target:
            return ApplyResult(op, False,
                               "Could not find add_executable() to name the target")
        block = (
            "\n# Delay-sync netplay + MotK lobby client (defines "
            "SNES_HAS_LOBBY_CLIENT; the\n"
            "# launcher's netplay button needs the host wiring in src/main.c "
            "as well —\n"
            "# see MetalWarriorsSNESRecomp src/main.c for the reference).\n"
            f"snesrecomp_enable_recomp_net({target})\n"
        )
        anchor = re.search(r"^[ \t]*recomp_target_launcher_ui\([^)]*\)",
                           text, re.M | re.S)
        if anchor:
            end = anchor.end()
            new_text = text[:end] + block + text[end:]
        else:
            new_text = text.rstrip() + "\n" + block
        changed_note = f"Added snesrecomp_enable_recomp_net({target})"
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] {changed_note}")
    cml.write_text(new_text, encoding="utf-8", newline="\n")
    # The engine needs its nested submodules; reuse the existing op.
    nested = _op_ensure_nested(root, opts)
    msg = changed_note + ("; " + nested.message if nested.message else "")
    return ApplyResult(op, True, msg, ["CMakeLists.txt"] + nested.changed_paths)


def _op_disable_netplay(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Comment the snesrecomp_enable_recomp_net() call out (reversible)."""
    op = "snes_disable_netplay"
    cml = root / "CMakeLists.txt"
    if not cml.is_file():
        return ApplyResult(op, False, "No CMakeLists.txt")
    text = cml.read_text(encoding="utf-8", errors="replace")
    m = _NETPLAY_CALL_RE.search(text)
    if not m:
        return ApplyResult(op, True, "Netplay already not wired")
    new_text = text[: m.start()] + m.group(1) \
        + f"# snesrecomp_enable_recomp_net({m.group(2)})" + text[m.end():]
    if _dry(opts):
        return ApplyResult(op, True, "[dry-run] would comment the call out")
    cml.write_text(new_text, encoding="utf-8", newline="\n")
    return ApplyResult(op, True,
                       "Commented snesrecomp_enable_recomp_net() out "
                       "(host code compiles the netplay blocks away)",
                       ["CMakeLists.txt"])


# ---------------------------------------------------------------------------
# Mod catalog
# ---------------------------------------------------------------------------
#
# snesrecomp used to leave mod staging to each title, and every title spelled
# it differently: a copy_directory POST_BUILD block here, a
# snesrecomp_target_stage_dir(... mods) there, each choosing its own
# destination beside the executable. runner.cmake now owns both the call and
# the destination, and fails configure when a repo ships packages that no
# target declared -- which is the error that brings people here.
#
# The CMake half is only half. mod_runtime scans <root>/packages where <root>
# is what the host passes to snes_mod_runtime_initialize_c(); the framework
# stages into mods/preloaded/packages. A port whose host still says "mods"
# therefore configures cleanly, builds cleanly, and shows an empty Mods page,
# which is a worse failure than the one the guard produces. So this op moves
# the host root too, and the audit reports the mismatch on its own.

MOD_CATALOG_CALL = "snesrecomp_target_mod_catalog"
# Repo-side catalog root: the directory holding packages/.
MOD_CATALOG_SRC = "mods/preloaded"
# Fallback only. The live value is read from runner.cmake below, because the
# framework is where that layout is decided and a copy here would rot.
MOD_CATALOG_DEST_DEFAULT = "mods/preloaded/packages"

_MOD_CATALOG_DEST_RE = re.compile(
    r"""set\s*\(\s*SNESRECOMP_MOD_CATALOG_DEST\s+"?([^"\s)]+)"?""")


def framework_catalog_dest(root: Path) -> str:
    """Staged layout beside the executable, as the framework declares it."""
    runner = root / FRAMEWORK / "runner" / "runner.cmake"
    try:
        text = runner.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return MOD_CATALOG_DEST_DEFAULT
    m = _MOD_CATALOG_DEST_RE.search(text)
    return m.group(1) if m else MOD_CATALOG_DEST_DEFAULT


def framework_has_mod_catalog(root: Path) -> bool:
    """True when the checked-out pin defines snesrecomp_target_mod_catalog().

    Older pins do not, and on those the per-title copy block is still the only
    thing that stages anything. Migrating a repo onto a function its framework
    has never heard of would turn a working build into a configure error.
    """
    runner = root / FRAMEWORK / "runner" / "runner.cmake"
    try:
        text = runner.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(re.search(r"function\s*\(\s*" + MOD_CATALOG_CALL + r"\b", text))


def host_mod_root(dest: str) -> str:
    """What the host must pass as mod_runtime's root: the parent of packages/."""
    parts = [p for p in dest.replace("\\", "/").split("/") if p]
    if parts and parts[-1] == "packages":
        parts = parts[:-1]
    return "/".join(parts) or "mods"


def catalog_package_ids(root: Path) -> list[str]:
    """Package ids under mods/preloaded/packages, the way the guard counts."""
    pkgs = root / MOD_CATALOG_SRC / "packages"
    if not pkgs.is_dir():
        return []
    try:
        return sorted(p.name for p in pkgs.iterdir() if p.is_dir())
    except OSError:
        return []


# Read and write without touching line endings. A migration that also
# normalizes CRLF rewrites every line of a host's main.c, and a diff where the
# one real change is buried in 1200 whitespace hunks is a diff nobody reviews.
def _read_verbatim(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def _write_verbatim(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _newline_of(text: str) -> str:
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf * 2 >= text.count("\n") else "\n"


def _as_newline(block: str, nl: str) -> str:
    return block if nl == "\n" else block.replace("\n", nl)


# --- a CMake reader that does not truncate ---------------------------------
#
# A POST_BUILD block is full of generator expressions and nested parentheses,
# so the `\(([^)]*)\)` shape used for the netplay one-liner above stops in the
# middle of one. A span that stops in the middle is a wrong edit, and a wrong
# edit to a CMakeLists is worse than the error we came to fix -- hence a real
# paren-balanced scan that knows about comments and quoted strings.

_CMAKE_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _skip_quoted(text: str, i: int) -> int:
    i += 1
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return n


def _match_paren(text: str, i: int) -> int | None:
    """Index just past the ')' matching the '(' at `i`, or None if unbalanced."""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i = _skip_quoted(text, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _cmake_calls(text: str) -> list[tuple[str, int, int, str]]:
    """``(name, start, end, args)`` for every command invocation in `text`.

    Descends into argument lists as well, so a command inside a function()
    body is found -- Mega Man X stages its shader presets from one, and the
    filter below has to be able to see it in order to leave it alone.
    """
    calls: list[tuple[str, int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i = _skip_quoted(text, i)
            continue
        m = _CMAKE_IDENT_RE.match(text, i)
        if not m:
            i += 1
            continue
        j = m.end()
        k = j
        while k < n and text[k] in " \t":
            k += 1
        if k < n and text[k] == "(":
            end = _match_paren(text, k)
            if end is None:
                break
            calls.append((m.group(0), i, end, text[k + 1:end - 1]))
            i = k + 1
            continue
        i = j
    return calls


# "mods" as a path segment, or the _MODS tail of a MMX_PRELOADED_MODS-style
# variable. Deliberately not a bare substring test: MMX_SHADER_ASSETS is
# staged by a copy_directory block that must survive this migration.
_MODS_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9_])mods(?![A-Za-z0-9_])|_MODS(?![A-Za-z0-9])", re.I)

_CUSTOM_CMD_TARGET_RE = re.compile(
    r"(?<![A-Za-z0-9_])TARGET\s+([A-Za-z0-9_.$={}-]+)")


def _legacy_mod_staging(text: str) -> list[tuple[int, int, str, str]]:
    """``(start, end, label, target)`` for each per-title mod staging command."""
    hits: list[tuple[int, int, str, str]] = []
    for name, start, end, args in _cmake_calls(text):
        if not _MODS_WORD_RE.search(args):
            continue
        if name == "snesrecomp_target_stage_dir":
            tok = args.split()
            hits.append((start, end, "snesrecomp_target_stage_dir(... mods)",
                         tok[0] if tok else ""))
        elif name == "add_custom_command" and (
                "copy_directory" in args or "remove_directory" in args):
            m = _CUSTOM_CMD_TARGET_RE.search(args)
            hits.append((start, end,
                         "add_custom_command(... copy_directory ... mods)",
                         m.group(1) if m else ""))
    # A hit fully inside another is the same block seen twice; keep the outer.
    outer: list[tuple[int, int, str, str]] = []
    for h in sorted(hits, key=lambda x: (x[0], -x[1])):
        if any(o[0] <= h[0] and h[1] <= o[1] for o in outer):
            continue
        outer.append(h)
    return outer


def _mod_catalog_declared(text: str) -> str | None:
    """The target the catalog is declared on, or None."""
    for name, _s, _e, args in _cmake_calls(text):
        if name == MOD_CATALOG_CALL:
            tok = args.split()
            return tok[0] if tok else ""
    return None


def _mods_enabled(text: str) -> bool:
    """SNESRECOMP_ENABLE_MODS forced ON before runner.cmake is included.

    The framework refuses a catalog without it: the loader is not compiled, so
    the packages would be staged beside the executable and read by nothing.
    """
    for name, start, _e, args in _cmake_calls(text):
        if name != "set":
            continue
        if "SNESRECOMP_ENABLE_MODS" in args and re.search(
                r"(?<![A-Za-z0-9_])ON(?![A-Za-z0-9_])", args):
            return start < _runner_include_pos(text)
    return False


def _runner_include_pos(text: str) -> int:
    for name, start, _e, args in _cmake_calls(text):
        if name == "include" and "runner.cmake" in args:
            return start
    return len(text)


# --- the host half ---------------------------------------------------------

_HOST_INIT_RE = re.compile(
    r"snes_mod_runtime_initialize_c\s*\(\s*([^,]+),", re.S)
def _exe_dir_path_for(text: str, ident: str) -> str:
    """The literal an exe_dir_path() wrote into `ident`, or ``"?"``.

    Following the actual out-parameter rather than "the only exe_dir_path in
    the file": a host stages several exe-relative directories (translations,
    assets), and picking whichever one happened to be first would report a
    mismatch that is not there.
    """
    m = re.search(r'snesrecomp_exe_dir_path\s*\(\s*"([^"]*)"\s*,\s*'
                  + re.escape(ident) + r'(?![A-Za-z0-9_])', text)
    return m.group(1) if m else "?"


def _host_sources(root: Path) -> list[Path]:
    out: list[Path] = []
    src = root / "src"
    if not src.is_dir():
        return out
    for ext in ("*.c", "*.cpp", "*.cc", "*.inc"):
        out.extend(sorted(src.rglob(ext)))
    return out


def host_mod_roots(root: Path) -> list[tuple[str, str]]:
    """``(repo-relative source, root it passes)`` for every mod_runtime init.

    ``"?"`` when the argument is neither a literal nor an exe_dir_path() the
    same file resolves -- reported rather than guessed at, because rewriting
    an argument we cannot read is how a migration breaks a working host.
    """
    found: list[tuple[str, str]] = []
    for path in _host_sources(root):
        try:
            text = _read_verbatim(path)
        except OSError:
            continue
        if "snes_mod_runtime_initialize_c" not in text:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        for m in _HOST_INIT_RE.finditer(text):
            arg = m.group(1).strip()
            lit = re.fullmatch(r'"([^"]*)"', arg)
            if lit:
                found.append((rel, lit.group(1)))
                continue
            ident = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", arg)
            found.append((rel, _exe_dir_path_for(text, arg)
                          if ident else "?"))
    return found


# --- rewriting CMakeLists.txt ----------------------------------------------

def _line_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Grow a span to whole lines, trailing newline included."""
    s = text.rfind("\n", 0, start) + 1
    e = text.find("\n", max(end - 1, start))
    return s, (len(text) if e < 0 else e + 1)


def _prev_line_span(text: str, start: int) -> tuple[int, int] | None:
    """The line above `start`, or None at the top of the file."""
    if start <= 0:
        return None
    e = start
    s = text.rfind("\n", 0, e - 1) + 1
    return s, e


def _expand_region(text: str, start: int, end: int) -> tuple[int, int, list[str]]:
    """Grow a staging command to everything that existed only to serve it.

    Three layers, each one observed in a real port: the ``if(EXISTS ...)`` /
    ``endif()`` Zelda and Super Mario World wrap the block in, the
    ``set(<X>_PRELOADED_MODS ...)`` line that feeds it, and the comment
    paragraph above explaining a staging rule that is now the framework's.
    Leaving any of them behind leaves a reader believing the repo still
    decides where the catalog goes.
    """
    notes: list[str] = []
    s, e = _line_span(text, start, end)

    # if()/endif() that brackets the block exactly: the previous non-blank,
    # non-comment line opens it and the next one closes it, so the block is
    # the whole body and both lines go with it.
    def _prev_code(i: int) -> tuple[int, int, str] | None:
        while True:
            sp = _prev_line_span(text, i)
            if sp is None:
                return None
            line = text[sp[0]:sp[1]]
            if line.strip() and not line.lstrip().startswith("#"):
                return sp[0], sp[1], line
            if not line.strip():
                return None  # a blank line ends the association
            i = sp[0]

    def _next_code(i: int) -> tuple[int, int, str] | None:
        while i < len(text):
            e2 = text.find("\n", i)
            e2 = len(text) if e2 < 0 else e2 + 1
            line = text[i:e2]
            if line.strip():
                return i, e2, line
            i = e2
        return None

    prev = _prev_code(s)
    nxt = _next_code(e)
    if (prev and nxt
            and re.match(r"\s*if\s*\(", prev[2])
            and re.match(r"\s*endif\s*\(", nxt[2])):
        s, e = prev[0], nxt[1]
        notes.append("its if()/endif() guard")

    # set(<X>_PRELOADED_MODS ...) directly above, if nothing else reads it.
    prev = _prev_code(s)
    if prev and re.match(r"\s*set\s*\(", prev[2]) and _MODS_WORD_RE.search(prev[2]):
        var = re.match(r"\s*set\s*\(\s*([A-Za-z0-9_]+)", prev[2])
        rest = text[:prev[0]] + text[e:]
        if var and f"${{{var.group(1)}}}" not in rest:
            s = prev[0]
            notes.append(f"the {var.group(1)} variable")

    # The comment paragraph immediately above (no blank line between).
    while True:
        sp = _prev_line_span(text, s)
        if sp is None or not text[sp[0]:sp[1]].lstrip().startswith("#"):
            break
        s = sp[0]
    return s, e, notes


def _mod_catalog_block(target: str, dest: str) -> str:
    return (
        "# The mod catalog. The framework owns the staged layout\n"
        f"# ({dest} beside the executable) and verifies it after\n"
        "# every build, so a rename touches snesrecomp/runner/runner.cmake and\n"
        "# nothing here. Pass NONE instead of a directory if this title ships\n"
        "# no catalog.\n"
        f"{MOD_CATALOG_CALL}({target}\n"
        f'    "${{CMAKE_SOURCE_DIR}}/{MOD_CATALOG_SRC}")\n'
    )


def _catalog_target(text: str, legacy: list) -> tuple[str, str]:
    """``(target, how)``. Empty target when the repo does not say which one."""
    for _s, _e, _label, tgt in legacy:
        if tgt and not tgt.startswith("$"):
            return tgt, "named by the staging block it replaces"
    declared = _mod_catalog_declared(text)
    if declared:
        return declared, "already declared"
    exes = [args.split()[0] for name, _s, _e, args in _cmake_calls(text)
            if name == "add_executable" and args.split()]
    literal = [e for e in exes if not e.startswith("$")]
    if len(literal) == 1:
        return literal[0], "the only add_executable() target"
    if len(literal) > 1:
        return "", ("several executables (" + ", ".join(literal[:4])
                    + ") and no staging block naming one — "
                    "declare the catalog by hand on the one that ships it")
    return "", "no literal add_executable() target to declare it on"


def _insert_after_pos(text: str) -> int:
    """Where a fresh declaration goes when no legacy block marks the spot."""
    best = -1
    for name, _s, end, _args in _cmake_calls(text):
        if name.startswith("snesrecomp_target_") or name in (
                "recomp_target_launcher_ui", "target_link_libraries"):
            best = max(best, end)
    if best < 0:
        return len(text)
    nl = text.find("\n", best)
    return len(text) if nl < 0 else nl + 1


def _op_declare_mod_catalog(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Hand mod staging to the framework, on both sides of the boundary.

    CMake: one snesrecomp_target_mod_catalog() call, every per-title staging
    block deleted, SNESRECOMP_ENABLE_MODS forced on before runner.cmake.
    Host: the mod_runtime root moved to wherever the framework now stages, so
    the Mods page lists what the build actually shipped.
    """
    op = "snes_declare_mod_catalog"
    cml = root / "CMakeLists.txt"
    if not cml.is_file():
        return ApplyResult(op, False, "No CMakeLists.txt")
    if not framework_has_mod_catalog(root):
        return ApplyResult(
            op, False,
            f"The checked-out {FRAMEWORK} pin has no {MOD_CATALOG_CALL}() — "
            "update the submodule first; on this pin the per-title staging "
            "block is still the only thing that stages the catalog.")
    try:
        text = _read_verbatim(cml)
    except OSError as exc:
        return ApplyResult(op, False, f"Cannot read CMakeLists.txt: {exc}")
    nl = _newline_of(text)

    dest = framework_catalog_dest(root)
    legacy = _legacy_mod_staging(text)
    target, how = _catalog_target(text, legacy)
    if not target:
        return ApplyResult(op, False, f"Cannot name the target: {how}")

    changed: list[str] = []
    done: list[str] = []
    new = text

    # 1. Delete the per-title staging, innermost-last so earlier spans keep
    #    their offsets.
    anchor: int | None = None
    for start, end, label, _tgt in sorted(legacy, reverse=True):
        s, e, notes = _expand_region(new, start, end)
        new = new[:s] + new[e:]
        anchor = s
        done.append("removed " + label
                    + (" with " + " and ".join(notes) if notes else ""))

    # 2. Declare it -- in the hole the old block left, so the catalog stays
    #    where a reader of this file already expects to find it.
    if _mod_catalog_declared(new) is None:
        at = anchor if anchor is not None else _insert_after_pos(new)
        block = _as_newline(_mod_catalog_block(target, dest), nl)
        if at > 0 and not new[:at].endswith(nl + nl):
            block = nl + block
        new = new[:at] + block + new[at:]
        done.append(f"declared {MOD_CATALOG_CALL}({target})")

    # 3. The loader has to be compiled or the catalog is read by nothing.
    inc = _runner_include_pos(new)
    if catalog_package_ids(root) and not _mods_enabled(new) and inc < len(new):
        at = new.rfind("\n", 0, inc) + 1
        new = new[:at] + _as_newline(
            "# Forces the package loader and the launcher's Mods view to be\n"
            "# compiled. Must precede runner.cmake: the framework refuses a\n"
            "# catalog it cannot read.\n"
            'set(SNESRECOMP_ENABLE_MODS ON CACHE BOOL\n'
            '    "Enable this title\'s mod catalog" FORCE)\n\n', nl) + new[at:]
        done.append("forced SNESRECOMP_ENABLE_MODS ON")

    if new != text:
        changed.append("CMakeLists.txt")

    # 4. The host half. mod_runtime scans <root>/packages; the framework now
    #    stages into `dest`, so anything else silently lists nothing.
    want = host_mod_root(dest)
    host_changes, host_notes = _rewrite_host_mod_root(root, want, opts)
    done.extend(host_notes)

    if not done:
        return ApplyResult(op, True, "Mod catalog already framework-owned")
    if _dry(opts):
        return ApplyResult(op, True, "[dry-run] " + "; ".join(done),
                           changed + host_changes)
    if "CMakeLists.txt" in changed:
        _write_verbatim(cml, new)
    return ApplyResult(op, True, "; ".join(done), changed + host_changes)


def _rewrite_host_mod_root(
    root: Path, want: str, opts: MigrateOptions
) -> tuple[list[str], list[str]]:
    """Point snes_mod_runtime_initialize_c() at the framework's staged root.

    Edits only the literal that call actually reads -- its own first argument,
    or the exe_dir_path() that filled its out-parameter. A host stages other
    exe-relative directories from the same file, and a blanket substitution
    would move one of those instead.
    """
    changed: list[str] = []
    notes: list[str] = []
    for path in _host_sources(root):
        try:
            text = _read_verbatim(path)
        except OSError:
            continue
        if "snes_mod_runtime_initialize_c" not in text:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        edits: list[tuple[int, int, str]] = []   # (start, end, replacement)
        for m in _HOST_INIT_RE.finditer(text):
            arg = m.group(1).strip()
            lit = re.fullmatch(r'"([^"]*)"', arg)
            if lit:
                if lit.group(1) != want:
                    s = m.start(1) + (len(m.group(1)) - len(m.group(1).lstrip()))
                    edits.append((s, s + len(arg), f'"{want}"'))
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", arg):
                notes.append(f"{rel}: mod_runtime root is an expression this "
                             "migration will not rewrite — check it by hand")
                continue
            d = re.search(r'snesrecomp_exe_dir_path\s*\(\s*("[^"]*")\s*,\s*'
                          + re.escape(arg) + r"(?![A-Za-z0-9_])", text)
            if d is None:
                notes.append(f"{rel}: cannot find where {arg} is filled — "
                             "check the mod_runtime root by hand")
            elif d.group(1) != f'"{want}"':
                edits.append((d.start(1), d.end(1), f'"{want}"'))
        if not edits:
            continue
        new = text
        for s, e, repl in sorted(edits, reverse=True):
            new = new[:s] + repl + new[e:]
        changed.append(rel)
        notes.append(f'{rel}: mod_runtime root → "{want}"')
        if not _dry(opts):
            _write_verbatim(path, new)
    return changed, notes


def _op_repair_framework(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_repair_framework_submodule"
    broken = diagnose_framework_checkout(root)
    if broken is None:
        return ApplyResult(op, True, "snesrecomp/ checkout is healthy")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would move {FRAMEWORK}/ aside and re-clone")
    import time as _time

    aside = root / f"{FRAMEWORK}.broken-{_time.strftime('%Y%m%d-%H%M%S')}"
    try:
        (root / FRAMEWORK).rename(aside)
    except OSError as exc:
        return ApplyResult(op, False, f"Could not move broken tree aside: {exc}")
    # Drop the stale index entry so ensure_submodule re-adds cleanly.
    _git(root, "rm", "-r", "--cached", "-q", "--", FRAMEWORK)
    res = _ensure_submodule(root, opts, FRAMEWORK)
    msg = (f"Moved broken tree to {aside.name}; " + res.message)
    return ApplyResult(op, res.ok, msg,
                       ([FRAMEWORK, aside.name] if res.ok else [aside.name]))


def _emit_identity(root: Path, opts: MigrateOptions, op: str) -> ApplyResult:
    """Write whichever identity carrier the pinned wizard scaffolds.

    Both op ids route here on purpose. A saved plan, a stale audit or an
    explicit ``--only snes_emit_codegen_setup`` should still produce the file
    the framework in front of it actually builds against; the result message
    names what was written, so nothing is written behind the caller's back.
    """
    carriers = identity_carriers(root)
    if not carriers:
        return ApplyResult(
            op, False,
            f"No identity template in {snes_paths.templates_label(root)} — "
            "neither rom_identity.txt.in nor codegen_setup.c.in")
    results = [_fill_template(root, opts, op, tpl, rel) for tpl, rel in carriers]
    changed = [pth for r in results for pth in r.changed_paths]
    failed = "; ".join(r.message for r in results if not r.ok)
    return ApplyResult(op, not failed,
                       failed or "; ".join(r.message for r in results), changed)


def _op_emit_codegen_setup(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _emit_identity(root, opts, "snes_emit_codegen_setup")


def _op_emit_rom_identity(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _emit_identity(root, opts, "snes_emit_rom_identity")


def _op_probe_rom_refresh(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Probe the ROM and re-emit the identity carriers with fresh digests."""
    op = "snes_probe_rom_refresh"
    if not opts.disc:
        return ApplyResult(op, False, "No ROM path (--disc) supplied")
    ident = rom_identity(root, opts.disc)
    if not (ident.get("sha256") and ident.get("crc32")):
        return ApplyResult(op, False, f"probe_rom.py produced no digests for {opts.disc}")
    # Re-emit with force: refreshing identity is the whole point of this op,
    # and _fill_template re-reads rom_identity(root, opts.disc) itself.
    import dataclasses as _dc

    forced = _dc.replace(opts, force=True)
    carriers = identity_carriers(root)
    if not carriers:
        return ApplyResult(
            op, False,
            f"No identity template in {snes_paths.templates_label(root)} — "
            "neither rom_identity.txt.in nor codegen_setup.c.in")
    results = [_fill_template(root, forced, op, tpl, rel) for tpl, rel in carriers]
    results.append(_fill_regen(root, forced, op))
    changed = [pth for r in results for pth in r.changed_paths]
    ok = all(r.ok for r in results)
    detail = f"crc32 {ident['crc32']}, sha256 {ident['sha256'][:12]}…"
    # Say where game_id came from. Reading it off the port's mod manifests is
    # still an inference, and it is being written into the file the build
    # compiles in — so it gets stated rather than assumed to be noticed.
    gid, how = resolve_game_id(root, forced, ident)
    if gid:
        detail += f", game_id {gid} ({how})"
    msgs = "; ".join(r.message for r in results if not r.ok) or detail
    return ApplyResult(op, ok, ("Refreshed identity: " + detail) if ok else msgs, changed)


def _op_relocate_boxart(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_relocate_boxart"
    dst_dir = root / "launcher_assets" / "img"
    for name in ("boxart.tga", "boxart.png"):
        if (dst_dir / name).is_file():
            return ApplyResult(op, True, "Modern boxart already present")
    candidates = [
        root / "assets" / "boxart.tga", root / "assets" / "boxart.png",
        root / "boxart.tga", root / "boxart.png",
    ]
    src = next((c for c in candidates if c.is_file()), None)
    if src is None:
        return ApplyResult(op, False, "No legacy boxart found")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would move {src.name} → launcher_assets/img/")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    rel = str(dst.relative_to(root)).replace("\\", "/")
    return ApplyResult(op, True, f"Moved {src.name} → {rel}", [rel])


def _op_emit_boxart_stub(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_emit_boxart_stub"
    img = root / "launcher_assets" / "img"
    if img.is_dir():
        return ApplyResult(op, True, "launcher_assets/img already exists")
    if _dry(opts):
        return ApplyResult(op, True, "[dry-run] mkdir launcher_assets/img")
    img.mkdir(parents=True, exist_ok=True)
    keep = img / ".gitkeep"
    keep.write_text("", encoding="utf-8")
    return ApplyResult(op, True, "Created launcher_assets/img/",
                       ["launcher_assets/img/.gitkeep"])


_SNES_LIBRETRO_SYSTEM = "Nintendo - Super Nintendo Entertainment System"


def _ensure_boxart_png(root: Path, opts: MigrateOptions) -> list[str]:
    """Fetch libretro SNES boxart when the README PNG is missing. Never raises."""
    import sys as _sys

    png = root / "launcher_assets" / "img" / "boxart.png"
    tga = root / "launcher_assets" / "img" / "boxart.tga"
    if png.is_file():
        return []
    if _dry(opts):
        return ["launcher_assets/img/boxart.png"]
    ident = rom_identity(root, opts.disc)
    rom_stem = Path(ident["rom_file"]).stem if ident.get("rom_file") else ""
    display = ident.get("display_name") or display_name(root)
    if not rom_stem and not display:
        return []
    try:
        from .paths import toolkit_dir

        _sys.path.insert(0, str(toolkit_dir()))
        from fetch_boxart import fetch_to_paths  # type: ignore
    except ImportError:
        return []
    try:
        fetch_to_paths(tga, cue_stem=rom_stem, display_name=display,
                       system=_SNES_LIBRETRO_SYSTEM)
    except Exception:
        return []
    changed: list[str] = []
    for rel in ("boxart.png", "boxart.tga", "BOXART_SOURCE.txt"):
        if (tga.parent / rel).is_file():
            changed.append(f"launcher_assets/img/{rel}")
    return changed


def _op_patch_readme_metrics(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Upsert download badges, boxart, Retro Launcher, and R.A.I.D. footer."""
    op = "snes_patch_readme_metrics"
    from .paths import templates_dir as _toolkit_templates
    from .readme_metrics import (
        apply_github_about,
        boxart_png_present,
        render_boxart_block,
        render_launcher_block,
        render_metrics_block,
        render_raid_block,
        resolve_github_slug,
        upsert_readme_blocks,
    )

    owner, repo = resolve_github_slug(root, opts.github_owner, opts.github_repo)
    zp = opts.zip_prefix or _zip_prefix(root)
    fetched = _ensure_boxart_png(root, opts)
    display = opts.window_title or display_name(root)
    boxart_block = None
    if boxart_png_present(root) or (
        opts.dry_run and "launcher_assets/img/boxart.png" in fetched
    ):
        boxart_block = render_boxart_block(display)

    path = root / "README.md"
    if path.is_file():
        old_text = path.read_text(encoding="utf-8", errors="replace")
    else:
        old_text = f"# {display}\n"
    new_text = upsert_readme_blocks(
        old_text,
        render_metrics_block(owner, repo, zp),
        render_launcher_block(),
        render_raid_block(),
        boxart=boxart_block,
    )
    changed: list[str] = list(fetched)
    if new_text != old_text:
        if not _dry(opts):
            path.write_text(new_text, encoding="utf-8", newline="\n")
        if "README.md" not in changed:
            changed.append("README.md")

    src_img = _toolkit_templates() / "raid-discord.png"
    dst_img = root / ".github" / "raid-discord.png"
    if src_img.is_file() and not dst_img.is_file():
        if not _dry(opts):
            dst_img.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_img, dst_img)
        changed.append(".github/raid-discord.png")

    about_ok, about_msg = apply_github_about(owner, repo, dry_run=_dry(opts))
    parts: list[str] = []
    if changed:
        parts.append(f"Patched README blocks ({owner}/{repo})")
    else:
        parts.append("README already complete")
    if about_msg:
        parts.append(about_msg if about_ok else f"[about] {about_msg}")
    return ApplyResult(op, True, "; ".join(parts), changed)


# Public aliases for the two ops gitops.install_and_push_release_ci reaches
# for directly — it installs CI outside a full plan.
op_emit_packager = _op_emit_packager
op_emit_ci_workflow = _op_emit_ci


_OPS = {
    "snes_ensure_framework_submodule": _op_ensure_framework,
    "snes_ensure_recomp_ui_submodule": _op_ensure_recomp_ui,
    "snes_ensure_nested_modules": _op_ensure_nested,
    "snes_merge_gitignore": _op_merge_gitignore,
    "snes_untrack_generated": _op_untrack_generated,
    "snes_ensure_src_gen": _op_ensure_src_gen,
    "snes_emit_version": _op_emit_version,
    "snes_emit_regen": _op_emit_regen,
    "snes_emit_packager": _op_emit_packager,
    "snes_emit_ci_workflow": _op_emit_ci,
    "snes_record_framework_pins": _op_record_pins,
    "snes_repair_framework_submodule": _op_repair_framework,
    "snes_probe_rom_refresh": _op_probe_rom_refresh,
    "snes_emit_codegen_setup": _op_emit_codegen_setup,
    "snes_emit_rom_identity": _op_emit_rom_identity,
    "snes_relocate_boxart": _op_relocate_boxart,
    "snes_emit_boxart_stub": _op_emit_boxart_stub,
    "snes_patch_readme_metrics": _op_patch_readme_metrics,
    "snes_enable_netplay": _op_enable_netplay,
    "snes_disable_netplay": _op_disable_netplay,
    "snes_declare_mod_catalog": _op_declare_mod_catalog,
    "snes_adopt_framework_regen": _op_adopt_framework_regen,
}
