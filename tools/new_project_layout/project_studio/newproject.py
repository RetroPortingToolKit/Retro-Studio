"""New-project wizard: drive setup_project.sh / .ps1 non-interactively."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import n64_paths, n64_toolchain, platforms, snes_paths
from .gitops import CmdResult, switch_modules
from .paths import find_bash, toolkit_dir

# Upper bound on a PSX disc set. Four covers every PS1 release we know of; the
# setup scripts have no limit of their own, so this is the one place that
# decides how many rows the UI offers. The cartridge consoles have no
# equivalent — a cartridge is one image — which is what PlatformProfile
# .max_images says, and this constant is now just PSX's answer to it.
MAX_DISCS = 4


@dataclass
class NewProjectOptions:
    """Mirrors setup_project.sh / setup_project.ps1 CLI flags."""

    name: str = ""
    disc: str = ""
    # PSX only: discs 2..N of a multi-disc set, in disc order. `disc` stays the
    # boot disc (and the ROM path on SNES), so every single-image caller is
    # unaffected. setup_project verifies the set before creating anything and
    # refuses one that needs N programs.
    extra_discs: list[str] = field(default_factory=list)
    parent_dir: str = ""
    bios: str = ""
    boot_exe: str = ""
    players: int = 2
    zip_prefix: str = ""
    github_owner: str = ""
    github_repo: str = ""
    description: str = ""
    publisher: str = ""
    year: str = ""
    # PSX default. The SNES path leaves this blank so the cartridge header
    # decides — see build_snes_command.
    region: str = "USA"

    enable_recomp_ui: bool = True
    enable_wizard: bool = True
    # GUI defaults: all on except GitHub; netplay follows players (≥2).
    enable_netplay: bool = False
    lobby_url: str = "netplay.retcomm.net"
    enable_ci: bool = True
    fetch_boxart: bool = True
    stage_disc: bool = True
    do_generate: bool = True
    do_build: bool = True
    create_github: bool = False
    github_visibility: str = "private"  # public | private | internal

    psxrecomp_ref: str = "master"
    recomp_ui_ref: str = "master"
    recomp_net_ref: str = ""  # empty = keep the framework's pin
    rbengine_ref: str = ""  # post-setup switch only (psx script has no flag)

    # --- SNES only ---------------------------------------------------------
    # `disc` carries the ROM path on SNES; there is no second field, because a
    # project has exactly one game image and two would let them disagree.
    platform: str = "psx"
    snesrecomp_ref: str = "main"
    multitap: str = ""  # port1 | port2 | both | off; "" = derive from players
    enable_rollback: bool = False

    # --- N64 only ----------------------------------------------------------
    # n64lle's scaffolder asks for four names where the other two ask for one,
    # because an n64lle port has no single one that serves: the CMake project
    # (GloverRecomp), the target prefix every target is built from
    # (glover-runtime, glover-cosim, glover-generate), and the executable the
    # player launches (glover) are three different strings. `name` and
    # `github_repo` carry the first; these carry the rest. Blank means "let the
    # scaffolder derive it", which is what a terminal run would have offered.
    n64_slug: str = ""  # target prefix; lowercase [a-z0-9_]
    n64_exe: str = ""   # executable name; defaults to the slug
    # The framework revision to cut the port against. Blank = the scaffolder's
    # own default, which is branch `main` pinned at the HEAD of the n64lle
    # checkout it was run from. Sent only when the script on disk declares the
    # flag; see build_n64_command.
    n64lle_ref: str = ""
    # The execution-derived discovery window. n64lle harvests what actually ran
    # rather than following seeds, so these two ARE the coverage decision, and
    # the scaffolder writes them into game.toml [recompiler] and mirrors them
    # into CMake. 0 = take the scaffolder's default.
    harvest_frames: int = 0
    harvest_step_cap_m: int = 0
    # Explicit tool paths (n64_toolchain.TOOLS keys -> path). Passed to the
    # scaffolder as --python/--cc/... and recorded in the new port's
    # tools/toolchain.cmake, so its first build and every later one -- Studio
    # or terminal -- use the same tools. Empty = the Studio default, else
    # the scaffolder's own discovery.
    n64_tools: dict = field(default_factory=dict)

    dry_run: bool = False


def all_discs(opts: NewProjectOptions) -> list[str]:
    """Boot disc first, then discs 2..N. Blank rows are dropped."""
    out = []
    for d in [opts.disc, *(opts.extra_discs or [])]:
        d = (d or "").strip()
        if d:
            out.append(d)
    return out


def resolved_discs(opts: NewProjectOptions) -> list[str]:
    """all_discs() as absolute paths, ready to hand to the setup script."""
    return [str(Path(d).expanduser().resolve()) for d in all_discs(opts)]


def setup_script_paths() -> tuple[Path, Path]:
    """Return (setup_project.sh, setup_project.ps1) under the toolkit."""
    base = toolkit_dir()
    return base / "setup_project.sh", base / "setup_project.ps1"


def wizard_shell_argv(script: Path, *, framework: str) -> list[str]:
    """``[shell, script]`` for a cartridge wizard's setup_project.sh.

    NOT a literal ``sh``, and for two reasons that both bite.

    Windows has no ``sh`` on PATH, so ``["sh", …]`` is a FileNotFoundError
    before the wizard is reached. The console's own setup_project.ps1 is not a
    second scaffolder to drive instead -- read its header: it finds the bash
    that Git for Windows ships and runs THIS script with the same arguments.
    Resolving that bash here is the same run without the extra hop, and it is
    already what the toolkit does for every other .sh it drives (regen.sh,
    package_release.sh, build_framework.sh) via paths.find_bash.

    And off Windows, n64lle's wizard is ``#!/usr/bin/env bash``: ``sh`` runs it
    under whatever /bin/sh is, which on a Debian or Ubuntu host is dash. The
    shebang is the script's own statement of what it needs, so honour it.
    """
    shell = find_bash()
    if shell is None:
        raise FileNotFoundError(
            f"No POSIX shell found to run {framework}'s setup_project.sh. "
            "On Windows install Git for Windows (it ships bash.exe); on "
            "Linux/macOS put bash on PATH."
        )
    return [shell, str(script)]


def is_windows() -> bool:
    return platform.system().lower().startswith("win") or os.name == "nt"


def opts_platform(opts: "NewProjectOptions") -> str:
    """The console this scaffold is for; falls back to the session's."""
    return platforms.normalize(opts.platform or platforms.current().key)


def is_snes(opts: "NewProjectOptions") -> bool:
    return opts_platform(opts) == "snes"


def is_n64(opts: "NewProjectOptions") -> bool:
    return opts_platform(opts) == "n64"


def script_supports(script: Path, flag: str) -> bool:
    """Does this wizard's argument parser have an arm for ``flag``?

    Studio drives whichever copy of a scaffolder it found on disk, and that
    copy can be OLDER than Studio — a port's pinned submodule, a sibling
    checkout. setup_project.sh answers an unknown
    option with `exit 2` before it probes anything, so a flag sent hopefully is
    a dead scaffold rather than a degraded one.

    The parsing is snes_paths.regen_options', not a second copy of it: both
    wizards dispatch through the same `case "$1" in --flag) …` shape, and a
    reimplementation here could not inherit a fix to it. Reading the case arms
    rather than the usage text is deliberate for the same reason it is there —
    a script whose help still lists a flag its parser has dropped must read as
    not supporting it.
    """
    try:
        text = Path(script).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return flag in snes_paths.regen_options(text)


def _pascal(text: str) -> str:
    """setup_project.sh's own pascal(): non-alnum to word breaks, then join.

    Reimplemented rather than shelled out to because Studio has to know the
    destination directory BEFORE the script runs — validate_options refuses a
    destination that already exists, and it cannot ask a script it has not
    started. Two implementations of one rule is a drift risk, so this is the
    only place it is written, and n64_project_name() is the only reader.
    """
    parts = re.split(r"[^A-Za-z0-9]+", text or "")
    return "".join(w[:1].upper() + w[1:] for w in parts if w)


def _slugify(text: str) -> str:
    """setup_project.sh's slugify(): lowercase, then drop everything else."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def n64_project_name(opts: "NewProjectOptions") -> str:
    """The repo / CMake project name — and the directory the scaffold creates.

    n64lle's setup_project.sh creates ``$DIR/$PROJECT``, so on N64 the repo
    name IS the folder name; there is no separate install_dir slug to sanitise.
    """
    repo = (opts.github_repo or "").strip()
    if repo:
        return repo
    return f"{_pascal(opts.name)}Recomp"


def n64_slug(opts: "NewProjectOptions") -> str:
    return (opts.n64_slug or "").strip() or _slugify(opts.name)


def n64_exe(opts: "NewProjectOptions") -> str:
    return (opts.n64_exe or "").strip() or n64_slug(opts)


def project_folder_name(opts: NewProjectOptions) -> str:
    """Checkout folder = GitHub/catalog install_dir slug (not display name with spaces)."""
    # N64 is the exception, and it is the script's rule rather than a
    # preference: n64lle's setup_project.sh creates "$DIR/$PROJECT" verbatim.
    # Running install_dir_name() over it here would have Studio watch for a
    # directory the scaffolder never creates, so the run would "succeed" and
    # then be reported as a missing project root.
    if is_n64(opts):
        return n64_project_name(opts)
    if is_snes(opts):
        # Also the script's rule: snesrecomp's setup_project.sh creates
        # "$DIR/$PROJECT_NAME" with PROJECT_NAME = probe_rom.project_name(name)
        # (<Title>SNESRecomp), whatever --github-repo says. Asked of the
        # wizard that will run, so the two cannot drift; a scaffold Studio
        # then "could not find" was this rule being guessed as a slug.
        return snes_project_name(opts)
    from fill_tokens import install_dir_name, sanitize_github_name

    repo = (opts.github_repo or "").strip()
    if repo:
        return sanitize_github_name(repo)
    return install_dir_name(opts.name or "")


def snes_project_name(opts: "NewProjectOptions") -> str:
    """<Title>SNESRecomp, from the wizard's own probe_rom.project_name()."""
    import importlib.util

    probe = snes_paths.probe_rom_script(None)
    try:
        if probe is None:
            raise FileNotFoundError(snes_paths.MISSING_CHECKOUT)
        spec = importlib.util.spec_from_file_location("snes_probe_rom", probe)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        return str(mod.project_name(opts.name or ""))
    except Exception:
        # Same rule, spelled locally, for a toolkit without the wizard.
        return _pascal(opts.name) + "SNESRecomp"


def project_root_for(opts: NewProjectOptions) -> Path:
    parent = Path(opts.parent_dir or ".").expanduser()
    if not parent.is_absolute():
        parent = parent.resolve()
    else:
        parent = parent.resolve()
    folder = project_folder_name(opts)
    if not folder:
        folder = (opts.name or "").strip() or "repo"
    return (parent / folder).resolve()


def validate_options(opts: NewProjectOptions) -> list[str]:
    errs: list[str] = []
    snes = is_snes(opts)
    n64 = is_n64(opts)
    profile = platforms.get(opts_platform(opts))
    cart = profile.is_cartridge
    name = (opts.name or "").strip()
    disc = (opts.disc or "").strip()
    if not name:
        errs.append("Project name is required")
    if not disc:
        errs.append(f"{profile.image_label} path is required")
    else:
        p = Path(disc).expanduser()
        if not p.is_file():
            errs.append(f"{'ROM' if cart else 'Disc'} not found: {disc}")
        elif cart and p.suffix.lower() not in profile.image_exts:
            exts = " / ".join(profile.image_exts)
            errs.append(
                f"Not a {profile.display} ROM (expected {exts}): {p.name}"
            )

    # Multi-disc is a PSX-only notion: a cartridge is one image.
    extras = [d for d in (opts.extra_discs or [])]
    if extras and profile.max_images <= 1:
        errs.append(
            f"A {profile.display} project has one ROM — extra discs do not apply"
        )
    elif extras:
        if len(extras) + 1 > profile.max_images:
            errs.append(f"At most {profile.max_images} discs are supported")
        for i, raw in enumerate(extras, start=2):
            d = (raw or "").strip()
            if not d:
                errs.append(
                    f"Disc {i} .cue path is required (or reduce the disc count)"
                )
                continue
            if not Path(d).expanduser().is_file():
                errs.append(f"Disc {i} not found: {d}")
        seen: dict[str, int] = {}
        for i, raw in enumerate([disc, *extras], start=1):
            d = (raw or "").strip()
            if not d:
                continue
            try:
                key = str(Path(d).expanduser().resolve())
            except OSError:
                key = d
            if key in seen:
                errs.append(f"Disc {i} is the same file as disc {seen[key]}: {d}")
            else:
                seen[key] = i
    if opts.bios and profile.has_bios:
        bp = Path(opts.bios).expanduser()
        if not bp.is_file():
            errs.append(f"BIOS not found: {opts.bios}")
    if opts.players < 1 or opts.players > 8:
        errs.append("Players must be 1–8")
    if n64:
        # The N64 has four controller ports and the scaffolder rejects anything
        # else outright ("players must be 1-4"). Catching it here means the
        # message names the console instead of arriving as a dead script.
        if opts.players < 1 or opts.players > 4:
            errs.append("The Nintendo 64 has four controller ports — players must be 1–4")
        slug = n64_slug(opts)
        if not slug or not re.fullmatch(r"[a-z0-9_]+", slug):
            errs.append(
                f"Target prefix must be lowercase letters/digits (got {slug!r})"
            )
        for label, value in (
            ("Harvest frames", opts.harvest_frames),
            ("Harvest step cap", opts.harvest_step_cap_m),
        ):
            if value < 0:
                errs.append(f"{label} cannot be negative")
        wiz = n64_paths.wizard_dir(None)
        if wiz is None:
            errs.append(n64_paths.MISSING_CHECKOUT)
        elif opts.do_generate or opts.do_build:
            # --generate builds the framework the wizard lives in, and since
            # n64lle went Rust that build runs cargo. Without it the wizard
            # fails minutes in, inside cmake, and rolls the scaffold back; say
            # it before anything is laid out.
            rust = n64_paths.rust_toolchain_problem(
                wiz.parent.parent, n64_tools_for(opts).get("cargo"))
            if rust:
                errs.append(rust)
        for key, path in n64_tools_for(opts).items():
            if key not in n64_toolchain.BY_KEY:
                errs.append(f"unknown tool '{key}'")
            elif not Path(n64_toolchain.strip_extended(path)).is_file():
                errs.append(f"{n64_toolchain.BY_KEY[key].label}: not found: {path}")
    if snes:
        tap = (opts.multitap or "").strip().lower()
        if tap and tap not in ("port1", "port2", "both", "off"):
            errs.append("Multitap must be port1 / port2 / both / off")
        if snes_paths.setup_script(None) is None:
            errs.append(snes_paths.MISSING_CHECKOUT)
    if opts.do_build and not opts.do_generate and not n64:
        errs.append("Build requires Generate")
    # Wizard/netplay without UI (and 1P netplay) are auto-corrected at run time.
    vis = (opts.github_visibility or "private").strip().lower()
    if vis not in ("public", "private", "internal"):
        errs.append("GitHub visibility must be public/private/internal")
    dest = project_root_for(opts)
    if dest.exists():
        if dest.is_file() or (dest.is_dir() and any(dest.iterdir())):
            errs.append(f"Destination already exists: {dest}")
    return errs


def build_snes_command(opts: NewProjectOptions) -> tuple[list[str], dict[str, str]]:
    """argv + env for snesrecomp's ``tools/new_project/setup_project.sh``.

    Only the flags that script actually has. The PSX form carries disc
    metadata (publisher / year / region / boxart / lobby) that the SNES
    scaffold has no slot for; passing them anyway would abort the run on an
    unknown flag, so they are dropped here and the caller is told which ones.
    """
    script = snes_paths.setup_script(None)
    if script is None:
        raise FileNotFoundError(snes_paths.MISSING_CHECKOUT)

    env = os.environ.copy()
    env["SNESRECOMP_SETUP_YES"] = "1"
    # The toolchain overlay that this scaffold needs (SDL3) is applied for
    # every console in build_command(); it used to be here, alone.

    cmd: list[str] = [
        *wizard_shell_argv(script, framework="snesrecomp"),
        "--yes",
        "--rom",
        str(Path(opts.disc).expanduser().resolve()),
        "--name",
        opts.name.strip(),
        "--players",
        str(int(opts.players)),
        "--dir",
        str(Path((opts.parent_dir or ".").strip() or ".").expanduser().resolve()),
        "--snesrecomp-ref",
        (opts.snesrecomp_ref or "main").strip(),
        "--github-visibility",
        (opts.github_visibility or "private").strip().lower(),
    ]
    if (opts.multitap or "").strip():
        cmd.extend(["--multitap", opts.multitap.strip().lower()])
    if opts.zip_prefix:
        cmd.extend(["--zip-prefix", opts.zip_prefix.strip()])
    if opts.github_owner:
        cmd.extend(["--github-owner", opts.github_owner.strip()])
    if opts.github_repo:
        cmd.extend(["--github-repo", opts.github_repo.strip()])
    # README metadata the wizard prompts for on a terminal. Studio always
    # passes --yes, which takes every default, so anything the user typed has
    # to arrive as a flag or it is silently lost.
    if opts.description:
        cmd.extend(["--description", opts.description.strip()])
    if opts.publisher:
        cmd.extend(["--publisher", opts.publisher.strip()])
    if opts.year:
        cmd.extend(["--year", opts.year.strip()])
    # Region only when set. Blank means "use the cartridge header", which is a
    # better answer than any default Studio could carry — sending a habitual
    # "USA" would relabel a Japanese cartridge.
    if (opts.region or "").strip():
        cmd.extend(["--region", opts.region.strip()])
    if (opts.recomp_net_ref or "").strip():
        cmd.extend(["--recomp-net-ref", opts.recomp_net_ref.strip()])
    if (opts.rbengine_ref or "").strip():
        cmd.extend(["--rbengine-ref", opts.rbengine_ref.strip()])
    if opts.enable_recomp_ui:
        cmd.extend(["--recomp-ui", "--recomp-ui-ref", (opts.recomp_ui_ref or "master").strip()])
    else:
        cmd.append("--no-recomp-ui")

    def flag(yes: bool, on: str, off: str) -> None:
        cmd.append(on if yes else off)

    # Rollback (retcomm-rbengine) is no longer a scaffold choice: the
    # framework's desktop host links it for every port. enable_rollback is
    # accepted from older callers and ignored.
    # with that rather than relying on order.
    flag(opts.enable_netplay or opts.enable_rollback, "--netplay", "--no-netplay")
    if opts.enable_rollback:
        cmd.append("--rollback")   # the wizard reads it as netplay too
    flag(opts.enable_ci, "--ci", "--no-ci")
    flag(opts.fetch_boxart, "--fetch-boxart", "--no-fetch-boxart")
    flag(opts.do_generate or opts.do_build, "--generate", "--no-generate")
    # The wizard's own build goes to a plain `build/` tree Studio never looks
    # at; Studio configures and builds its own tree after the scaffold
    # instead (see run()), so a new project shows up built on the Build tab.
    flag(False, "--build", "--no-build")
    flag(opts.create_github, "--create-github", "--no-github")
    return cmd, env


def n64_tools_for(opts: NewProjectOptions) -> dict[str, str]:
    """The tools a new N64 scaffold is cut with: the form's, over the Studio
    default. Blank rows fall through to the scaffolder's own discovery."""
    merged = {**n64_toolchain.read_studio_defaults(),
              **{k: v for k, v in (opts.n64_tools or {}).items() if (v or "").strip()}}
    return {k: v.strip() for k, v in merged.items() if (v or "").strip()}


def build_n64_command(opts: NewProjectOptions) -> tuple[list[str], dict[str, str]]:
    """argv + env for n64lle's ``tools/new_project/setup_project.sh``.

    Only the flags that script actually has, and its flag set is the smallest
    of the three: an unknown option is `exit 2` there, so a habitual
    --description or --enable-ci from the PSX form would kill the run before it
    probed the ROM. What it gains instead is the four names an n64lle port
    needs (project / slug / exe) and the harvest window, none of which the
    other two consoles have a concept of.

    --n64lle-ref / --recomp-ui-ref are sent only when the script ON DISK
    declares them. Studio drives whichever checkout it found — `$N64LLE_ROOT`,
    the port's own submodule, or a sibling checkout — and an unknown option is
    `exit 2` there, so an older wizard would die on the flag instead of
    scaffolding. Without them the script does what it always did:
    branch `main`, pinned at the HEAD of the checkout it was run from, "the SHA
    this scaffold was cut against".
    """
    script = n64_paths.setup_script(None)
    if script is None:
        raise FileNotFoundError(n64_paths.MISSING_CHECKOUT)

    env = os.environ.copy()

    cmd: list[str] = [
        *wizard_shell_argv(script, framework="n64lle"),
        "--yes",
        "--rom",
        str(Path(opts.disc).expanduser().resolve()),
        "--name",
        opts.name.strip(),
        "--project",
        n64_project_name(opts),
        "--slug",
        n64_slug(opts),
        "--exe",
        n64_exe(opts),
        "--players",
        str(int(opts.players)),
        "--dir",
        str(Path((opts.parent_dir or ".").strip() or ".").expanduser().resolve()),
    ]
    # The harvest window. Sent only when set: the scaffolder's own defaults
    # (900 frames / 3000M steps) are the ones its docs quote, and echoing them
    # back from here would be a second copy to drift.
    if opts.harvest_frames > 0:
        cmd.extend(["--frames", str(int(opts.harvest_frames))])
    if opts.harvest_step_cap_m > 0:
        cmd.extend(["--step-cap", str(int(opts.harvest_step_cap_m))])
    if opts.github_owner:
        cmd.extend(["--gh-owner", opts.github_owner.strip()])
    # The submodule revisions, when the wizard on disk can take them.
    n64lle_ref = (opts.n64lle_ref or "").strip()
    if n64lle_ref and script_supports(script, "--n64lle-ref"):
        cmd.extend(["--n64lle-ref", n64lle_ref])
    ui_ref = (opts.recomp_ui_ref or "").strip()
    # "master" is recomp-ui's own default in the script; sending it back would
    # be a second copy of the default to drift.
    if ui_ref and ui_ref != "master" and script_supports(script, "--recomp-ui-ref"):
        cmd.extend(["--recomp-ui-ref", ui_ref])

    # Stage image => --copy-rom. The scaffolder symlinks the dump into roms/ by
    # default, which is the better answer on this machine; --copy-rom is for a
    # dump on removable media. Either way no ROM bytes are committed: roms/* is
    # gitignored in the scaffold.
    if opts.stage_disc:
        cmd.append("--copy-rom")
    # --generate on this scaffolder is the WHOLE pipeline: framework build,
    # harvest, emit, compile, and ctest. There is no separate --build, so
    # either switch asking for work maps onto it.
    cmd.append("--generate" if (opts.do_generate or opts.do_build) else "--no-generate")
    cmd.append("--git")
    if opts.create_github:
        cmd.append("--gh")
        if (opts.github_visibility or "private").strip().lower() == "public":
            cmd.append("--public")
    # The chosen tools, as the flags this wizard declares; anything it does
    # not declare rides in the environment n64lle reads (run_new_project says
    # which, so a choice the wizard cannot take is not silently dropped).
    try:
        supported = n64_toolchain.script_flags(
            Path(script).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        supported = set()
    tc_argv, tc_env, _dropped = n64_toolchain.script_args(
        n64_tools_for(opts), "setup", supported)
    cmd.extend(tc_argv)
    env.update(tc_env)
    return cmd, env


def n64_ignored_fields(opts: NewProjectOptions) -> list[str]:
    """Inputs the N64 scaffolder has no flag for.

    Named rather than dropped: they are on screen when Studio runs with --yes,
    and a scaffold that ignored them without a word looks like it honoured
    them. Region is here and NOT defaulted — n64lle reads the region out of the
    cartridge header (`probe_rom.py` -> game.toml [game].region [MEASURED]) and
    has no flag to override it, which is the right answer and not a gap.
    """
    ignored: list[str] = []
    for label, value in (
        ("BIOS", opts.bios),
        ("Boot EXE", opts.boot_exe),
        ("Zip prefix", opts.zip_prefix),
        ("Description", opts.description),
        ("Publisher", opts.publisher),
        ("Year", opts.year),
        ("Region", opts.region),
        ("Lobby URL", opts.lobby_url if opts.enable_netplay else ""),
    ):
        if (value or "").strip():
            ignored.append(label)
    if opts.enable_netplay or opts.enable_rollback:
        ignored.append("Netplay")
    if not opts.enable_recomp_ui:
        # recomp-ui is not optional in this scaffold: setup_project.sh always
        # adds the submodule, and the CMake option that skips the launcher
        # (<SLUG>_BUILD_UI) is a build-time choice in the created repo.
        ignored.append("Disable recomp-ui")
    if opts.enable_ci:
        # n64lle ships no release workflow template, so there is nothing to
        # emit. Saying so beats a CI tick that quietly does nothing.
        ignored.append("CI workflow")
    if opts.fetch_boxart:
        ignored.append("Boxart")
    return ignored


def snes_ignored_fields(opts: NewProjectOptions) -> list[str]:
    """PSX-only inputs the SNES scaffolder has no flag for.

    Description / publisher / year / region are deliberately NOT here: the
    wizard grew prompts for them, and Studio passes them as flags.
    """
    ignored: list[str] = []
    for label, value in (
        ("BIOS", opts.bios),
        ("Boot EXE", opts.boot_exe),
        ("Lobby URL", opts.lobby_url if opts.enable_netplay else ""),
    ):
        if (value or "").strip():
            ignored.append(label)
    # Boxart is no longer ignored: the SNES wizard grew --fetch-boxart and
    # fetches SNES Named_Boxarts exactly the way the PSX wizard does.
    if opts.stage_disc:
        ignored.append("Stage image")
    return ignored


def build_command(opts: NewProjectOptions) -> tuple[list[str], dict[str, str]]:
    """Build argv + env for the OS-appropriate setup script.

    Always passes ``--yes`` / ``-Yes`` so the GUI/CLI supply every choice.

    THE TOOLCHAIN OVERLAY IS APPLIED HERE, ONCE, FOR EVERY CONSOLE. A wizard's
    own --generate/--build step runs a plain `cmake -S . -B build`: the
    toolchain's compiler is on PATH, but nothing points find_package() at the
    toolchain's own SDL3. The retcomm clang is hermetic and does not search
    /usr/include, so a configure that resolves the HOST's SDL3 succeeds and
    then every launcher TU fails with "SDL3/SDL.h file not found" -- while
    Studio's own `build configure`, which applies this overlay, builds the same
    tree fine.

    That was found and fixed on SNES, in build_snes_command, and nowhere else.
    N64 and PSX kept the bug for as long as they have existed; PokemonStadiumRecomp
    hit it on 2026-09-12 after the harvest and n64emit had already succeeded.
    Applying it at this seam rather than in each builder is the actual fix: the
    next console inherits it instead of re-discovering it.
    """
    cmd, env = _build_command_for_platform(opts)
    from .buildops import toolchain_env

    env.update(toolchain_env())
    return cmd, env


def _build_command_for_platform(
    opts: NewProjectOptions,
) -> tuple[list[str], dict[str, str]]:
    if is_snes(opts):
        return build_snes_command(opts)
    if is_n64(opts):
        return build_n64_command(opts)
    sh, ps1 = setup_script_paths()
    env = os.environ.copy()
    env["PSXRECOMP_SETUP_YES"] = "1"
    if (opts.recomp_net_ref or "").strip():
        env["RECOMP_NET_REF"] = opts.recomp_net_ref.strip()

    if is_windows():
        if not ps1.is_file():
            raise FileNotFoundError(f"Missing setup script: {ps1}")
        powershell = (
            shutil.which("pwsh")
            or shutil.which("powershell")
            or "powershell"
        )
        cmd: list[str] = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps1),
            # -Disc is [string[]], but an array cannot be passed reliably
            # through `powershell -File`: subprocess quotes any argument
            # containing a space, and PowerShell then binds the whole quoted
            # token as ONE element. Disc paths routinely contain spaces, so a
            # comma-joined list would silently become a single bogus path.
            # The script splits a lone argument on "|", which is one of the
            # characters Windows forbids in a path, so it cannot be ambiguous.
            "-Disc",
            "|".join(resolved_discs(opts)),
            "-Name",
            opts.name.strip(),
            "-Yes",
            "-Players",
            str(int(opts.players)),
            "-PsxrecompRef",
            (opts.psxrecomp_ref or "master").strip(),
            "-RecompUiRef",
            (opts.recomp_ui_ref or "master").strip(),
            "-GithubVisibility",
            (opts.github_visibility or "private").strip().lower(),
        ]
        parent = (opts.parent_dir or ".").strip() or "."
        cmd.extend(["-Dir", str(Path(parent).expanduser().resolve())])
        if opts.bios:
            cmd.extend(["-Bios", str(Path(opts.bios).expanduser().resolve())])
        if opts.boot_exe:
            cmd.extend(["-BootExe", opts.boot_exe.strip()])
        if opts.zip_prefix:
            cmd.extend(["-ZipPrefix", opts.zip_prefix.strip()])
        if opts.github_owner:
            cmd.extend(["-GithubOwner", opts.github_owner.strip()])
        if opts.github_repo:
            cmd.extend(["-GithubRepo", opts.github_repo.strip()])
        if opts.description:
            cmd.extend(["-Description", opts.description.strip()])
        if opts.publisher:
            cmd.extend(["-Publisher", opts.publisher.strip()])
        if opts.year:
            cmd.extend(["-Year", opts.year.strip()])
        if opts.region:
            cmd.extend(["-Region", opts.region.strip()])
        if opts.lobby_url:
            cmd.extend(["-LobbyUrl", opts.lobby_url.strip()])

        def sw(yes: bool, on: str, off: str) -> None:
            cmd.append(on if yes else off)

        sw(opts.enable_recomp_ui, "-EnableRecompUi", "-NoRecompUi")
        sw(opts.enable_wizard, "-EnableWizard", "-NoWizard")
        sw(opts.enable_netplay, "-EnableNetplay", "-NoNetplay")
        sw(opts.enable_ci, "-EnableCi", "-NoCi")
        sw(opts.fetch_boxart, "-FetchBoxart", "-NoFetchBoxart")
        sw(opts.do_generate, "-Generate", "-NoGenerate")
        sw(opts.do_build, "-EnableBuild", "-NoBuild")
        sw(opts.create_github, "-CreateGithub", "-NoGithub")
        if opts.stage_disc:
            cmd.append("-StageDisc")
        return cmd, env

    if not sh.is_file():
        raise FileNotFoundError(f"Missing setup script: {sh}")
    cmd = [
        "sh",
        str(sh),
        "--yes",
    ]
    for _d in resolved_discs(opts):
        cmd += ["--disc", _d]
    cmd += [
        "--name",
        opts.name.strip(),
        "--players",
        str(int(opts.players)),
        "--psxrecomp-ref",
        (opts.psxrecomp_ref or "master").strip(),
        "--recomp-ui-ref",
        (opts.recomp_ui_ref or "master").strip(),
        "--github-visibility",
        (opts.github_visibility or "private").strip().lower(),
    ]
    parent = (opts.parent_dir or ".").strip() or "."
    cmd.extend(["--dir", str(Path(parent).expanduser().resolve())])
    if opts.bios:
        cmd.extend(["--bios", str(Path(opts.bios).expanduser().resolve())])
    if opts.boot_exe:
        cmd.extend(["--boot-exe", opts.boot_exe.strip()])
    if opts.zip_prefix:
        cmd.extend(["--zip-prefix", opts.zip_prefix.strip()])
    if opts.github_owner:
        cmd.extend(["--github-owner", opts.github_owner.strip()])
    if opts.github_repo:
        cmd.extend(["--github-repo", opts.github_repo.strip()])
    if opts.description:
        cmd.extend(["--description", opts.description.strip()])
    if opts.publisher:
        cmd.extend(["--publisher", opts.publisher.strip()])
    if opts.year:
        cmd.extend(["--year", opts.year.strip()])
    if opts.region:
        cmd.extend(["--region", opts.region.strip()])
    if opts.lobby_url:
        cmd.extend(["--lobby-url", opts.lobby_url.strip()])
    if (opts.recomp_net_ref or "").strip():
        cmd.extend(["--recomp-net-ref", opts.recomp_net_ref.strip()])

    def flag(yes: bool, on: str, off: str) -> None:
        cmd.append(on if yes else off)

    flag(opts.enable_recomp_ui, "--enable-recomp-ui", "--no-recomp-ui")
    flag(opts.enable_wizard, "--enable-wizard", "--no-wizard")
    flag(opts.enable_netplay, "--enable-netplay", "--no-netplay")
    flag(opts.enable_ci, "--enable-ci", "--no-ci")
    flag(opts.fetch_boxart, "--fetch-boxart", "--no-fetch-boxart")
    flag(opts.do_generate, "--generate", "--no-generate")
    flag(opts.do_build, "--enable-build", "--no-build")
    flag(opts.create_github, "--create-github", "--no-github")
    cmd.append("--stage-disc" if opts.stage_disc else "--no-stage-disc")
    return cmd, env



KEEP_FAILED_ENV = "RETCOMM_KEEP_FAILED_SCAFFOLD"


def discard_failed_scaffold(
    root: Path,
    opts: NewProjectOptions,
    *,
    existed_before: bool,
    reason: str,
    on_line: Callable[[str], None] | None = None,
) -> str:
    """Remove the half-written project a failed scaffold left behind.

    A scaffold that dies partway through leaves a directory that LOOKS like a
    project and is not one: submodules half-cloned, no game.toml, sometimes no
    commit. The next run then refuses it ("Destination already exists") and the
    user has to work out which of their repos is real before they can retry.
    Worse, an audit or a bulk sweep will happily pick it up.

    So the rule is the one the wizard itself implies: a run that does not
    finish produces nothing. Only ever the directory THIS run created, and only
    when validate_options had already established it was absent or empty -- a
    directory the user had already put something in is never ours to remove.

    shutil.rmtree does not follow directory symlinks, and roms/<dump> is
    usually a symlink to the user's own file. That is the whole reason this is
    rmtree and not a walk: the link is unlinked, the dump it points at is not
    touched. Checked, not assumed -- it is the difference between cleaning up
    and deleting somebody's ROM library.

    Returns a note for the caller's detail, or "" when nothing was removed.
    """
    if os.environ.get(KEEP_FAILED_ENV, "").strip() not in ("", "0"):
        note = f"kept {root} for inspection ({KEEP_FAILED_ENV} is set)"
        if on_line:
            on_line(f"note: {note}")
        return note
    try:
        target = root.expanduser().resolve()
    except OSError:
        return ""
    if not target.is_dir():
        return ""
    # Never above the parent the user chose, and never the parent itself.
    try:
        parent = Path(opts.parent_dir or ".").expanduser().resolve()
    except OSError:
        return ""
    if target == parent or parent not in target.parents:
        return f"refused to remove {target}: it is not inside {parent}"

    notes: list[str] = []
    try:
        if existed_before:
            # The directory was there and empty before this run, so the
            # contents are ours and the directory is not.
            for child in target.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            notes.append(f"emptied {target} ({reason})")
        else:
            shutil.rmtree(target, ignore_errors=True)
            notes.append(f"removed {target} ({reason})")
    except OSError as exc:
        return f"could not remove {target}: {exc}"

    if target.exists() and not existed_before:
        notes.append(f"note: {target} is still present — remove it by hand")
    if opts.create_github:
        # The local tree is gone; a repo the wizard may already have created
        # on GitHub is not, and deleting someone's remote is not a cleanup
        # step Studio gets to take on its own.
        notes.append(
            "a GitHub repository may already have been created for this "
            "project — the local tree was removed, the remote was not"
        )
    for n in notes:
        if on_line:
            on_line(f"cleanup: {n}")
    return "; ".join(notes)


def run_new_project(
    opts: NewProjectOptions,
    *,
    on_line: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> CmdResult:
    """Run setup_project end-to-end; stream lines via ``on_line``."""
    errs = validate_options(opts)
    if errs:
        return CmdResult(False, "Invalid new-project options", "\n".join(errs))

    # Match script policy: no UI ⇒ no wizard/netplay; 1P ⇒ no netplay.
    # On a console whose framework has no netplay at all, the switch is off
    # before any of that — see PlatformProfile.has_netplay.
    if not platforms.get(opts_platform(opts)).has_netplay:
        opts.enable_netplay = False
        opts.enable_rollback = False
    if not opts.enable_recomp_ui:
        opts.enable_wizard = False
        opts.enable_netplay = False
    if opts.players < 2:
        opts.enable_netplay = False
        opts.enable_rollback = False

    if is_n64(opts):
        if on_line:
            src = n64_paths.wizard_source(None)
            on_line(f"Using n64lle wizard: {src}")
            script = n64_paths.setup_script(None)
            can_ref = script is not None and script_supports(script, "--n64lle-ref")
            if (opts.n64lle_ref or "").strip() and not can_ref:
                # Said here rather than swallowed in build_n64_command: the ref
                # is on screen, and a scaffold that ignored it without a word
                # would look like it honoured it.
                on_line(
                    f"note: this wizard has no --n64lle-ref, so "
                    f"'{opts.n64lle_ref.strip()}' is NOT being used — the "
                    "project will be pinned the way that checkout pins. Update "
                    "the n64lle checkout Studio is reading."
                )
            ignored = n64_ignored_fields(opts)
            if ignored:
                on_line("note: not used by the N64 scaffolder — " + ", ".join(ignored))
            tools = n64_tools_for(opts)
            if tools:
                on_line("toolchain: " + ", ".join(f"{k}={v}" for k, v in tools.items()))
                try:
                    flags = n64_toolchain.script_flags(
                        script.read_text(encoding="utf-8", errors="replace")) if script else set()
                except OSError:
                    flags = set()
                _a, _e, dropped = n64_toolchain.script_args(tools, "setup", flags)
                for k in dropped:
                    spec = n64_toolchain.BY_KEY[k]
                    on_line(f"note: this wizard has no {spec.flag}; {spec.label} is "
                            f"passed as ${spec.env} and recorded in the new port's "
                            f"{n64_toolchain.PROJECT_FILE.as_posix()} afterwards")
    if is_snes(opts):
        if on_line:
            on_line(f"Using snesrecomp wizard: {snes_paths.wizard_source(None)}")
            ignored = snes_ignored_fields(opts)
            if ignored:
                # Say it rather than silently dropping: the fields are on
                # screen, and a scaffold that ignored them without a word looks
                # like it honoured them.
                on_line("note: not used by the SNES scaffolder — " + ", ".join(ignored))

    try:
        cmd, env = build_command(opts)
    except FileNotFoundError as exc:
        return CmdResult(False, str(exc))

    if opts.dry_run:
        preview = " ".join(cmd)
        if on_line:
            on_line(f"[dry-run] {preview}")
        return CmdResult(True, "dry-run: would run setup_project", preview)

    if on_line:
        on_line(f"$ {' '.join(cmd)}")

    # Recorded BEFORE the wizard runs, because afterwards there is no way to
    # tell a directory this run created from one that was already there.
    # validate_options has established it is absent or empty by this point.
    dest = project_root_for(opts)
    dest_existed = dest.is_dir()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(Path(opts.parent_dir or ".").expanduser().resolve()),
        )
    except OSError as exc:
        return CmdResult(False, f"Failed to start setup: {exc}")

    assert proc.stdout is not None
    for line in proc.stdout:
        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()
            proc.wait()
            note = discard_failed_scaffold(
                dest, opts, existed_before=dest_existed,
                reason="cancelled", on_line=on_line,
            )
            return CmdResult(False, "New project cancelled", note)
        text = line.rstrip("\n")
        if on_line:
            on_line(text)
    code = proc.wait()
    root = project_root_for(opts)
    if code != 0:
        # A scaffold that failed produces nothing. The failure itself is in the
        # Activity log above, which is where it is read from -- what is NOT
        # wanted is a directory that looks like a project and is not one, which
        # the next attempt then refuses as "Destination already exists".
        note = discard_failed_scaffold(
            root, opts, existed_before=dest_existed,
            reason=f"setup_project exit {code}", on_line=on_line,
        )
        detail = str(root)
        if note:
            detail += "\n" + note
        return CmdResult(False, f"setup_project failed (exit {code})", detail)

    # The N64 toolchain, recorded in the new port (machine-local; the port
    # gitignores it). A wizard with n64lle's explicit-toolchain change already
    # wrote the file from the flags above -- every tool it resolved, not only
    # the chosen ones -- and that answer is left alone. An older wizard wrote
    # nothing, so Studio records the chosen tools, and says so.
    if is_n64(opts) and root.is_dir():
        tools = n64_tools_for(opts)
        pf = n64_toolchain.project_file(root)
        if tools and not pf.is_file():
            try:
                n64_toolchain.write_project(root, tools)
                if on_line:
                    on_line(f"toolchain recorded in {pf} (this wizard does not "
                            "record one itself)")
            except n64_toolchain.UnrecordableValue as exc:
                if on_line:
                    on_line(f"toolchain NOT recorded: {exc}")

    # Optional nested rbengine (and net where the entry point has no ref flag)
    post_notes: list[str] = []
    if root.is_dir():
        nested_branches: dict[str, str] = {}
        net = (opts.recomp_net_ref or "").strip()
        rb = (opts.rbengine_ref or "").strip()
        # This compensates for ONE entry point, not for an OS: psxrecomp's
        # setup_project.ps1 declares no -RecompNetRef, so on Windows the PSX
        # scaffold has to be told afterwards. The cartridge wizards are driven
        # through their own setup_project.sh on every host (wizard_shell_argv),
        # so the flag they take is the flag that was sent, and re-switching a
        # module they may not even have would be a failed op for no reason.
        if net and is_windows() and not (is_snes(opts) or is_n64(opts)):
            nested_branches["lib/recomp-net"] = net
        if rb:
            nested_branches["lib/retcomm-rbengine"] = rb
        if nested_branches:
            if on_line:
                on_line("== Post-setup nested lib branch switch ==")
            for r in switch_modules(
                root,
                nested=True,
                branch_by_path=nested_branches,
                paths=list(nested_branches.keys()),
                set_tracking=True,
                dry_run=False,
            ):
                post_notes.append(r.message)
                if on_line:
                    on_line(f"  [{'OK' if r.ok else 'FAIL'}] {r.message}")

    # Configure and build STUDIO's tree (build-release, the toolchain's deps,
    # the same path the Build tab takes), not the wizard's `build/`: a
    # scaffold that "built" into a directory Studio never reads showed up
    # unconfigured, and the first thing a new project got was a manual
    # Configure + Build. Only after generate: an empty src/gen is the
    # framework's own fatal error, said in its own words.
    if opts.do_build and not is_n64(opts) and root.is_dir():
        from . import buildops

        if on_line:
            on_line("== Configure + build (Studio) ==")
        r = buildops.configure(root, log=on_line)
        if on_line:
            on_line(f"  [{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.ok:
            r = buildops.build(root, log=on_line)
            if on_line:
                on_line(f"  [{'OK' if r.ok else 'FAIL'}] {r.message}")
        if not r.ok:
            return CmdResult(
                False,
                f"Created project at {root}, but Studio's build failed: {r.message}",
                str(root),
            )
        post_notes.append(f"built: {buildops.DEFAULT_BUILD_DIR}")

    detail = str(root)
    if post_notes:
        detail += "\n" + "\n".join(post_notes)
    return CmdResult(True, f"Created project at {root}", detail)


def index_new_project(
    root: Path,
    *,
    name: str = "",
    cue: str = "",
) -> CmdResult:
    """Add/update the Studio repo index for a freshly created project."""
    from .bulkops import (
        catalog_title_id_for_root,
        upsert_studio_toml_title,
    )
    from .repo_index import add_repo, load_index

    if not root.is_dir():
        return CmdResult(False, f"Project root missing: {root}")
    # load_index() resolves the per-platform file, so a SNES scaffold lands in
    # the SNES list without this function knowing which one that is.
    idx = load_index()
    entry = add_repo(idx, root, name=name or root.name, cue=cue)
    notes: list[str] = [entry.path]
    tid = catalog_title_id_for_root(root)
    if tid:
        note = upsert_studio_toml_title(tid, root)
        if note:
            notes.append(note)
    return CmdResult(True, f"Indexed {entry.label()}", "\n".join(notes))
