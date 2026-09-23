#!/usr/bin/env python3
"""Headless check of the platform split — the N64 half of Project Studio.

The GUI needs a GPU and a window; the decisions that make an N64 session an
N64 session do not. This drives them directly against a synthetic repo:

  * one platform per process, and the PSX/SNES defaults unchanged by its
    existence
  * the capability table, because adding a third console is exactly what makes
    `key == "snes"` and `else` two different claims — every capability an N64
    session answers differently from PSX is asserted here, so a fourth console
    cannot inherit PSX's answers by being spelled differently
  * a separate repo index per console, and repo recognition that refuses to put
    one port in two lists (an n64lle port carries a game.toml, and so does a
    PSX one)
  * the N64 audit / plan / apply ops, end to end, on a scaffold-shaped tree
  * refusal to emit a file whose ROM identity cannot be resolved — the one
    failure mode worth a test, because inventing those values writes a guess
    into a document whose whole value is that its rows are [MEASURED]
  * the three names an n64lle port needs, derived by the SCAFFOLDER's rules,
    because Studio always runs that wizard with --yes and a name Studio
    derived differently would silently disagree with the tree it creates

Run:  python3 tests/n64_platform_test.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "tools" / "new_project_layout"))

from project_studio import n64_paths, platforms  # noqa: E402
from project_studio.models import MigrateOptions  # noqa: E402

failures = 0


def check(cond: bool, what: str) -> None:
    global failures
    if cond:
        print(f"  ok    {what}")
    else:
        print(f"  FAIL  {what}")
        failures += 1


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


# The contract a scaffolded port carries. Only the rows the ops actually read.
GAME_TOML = """\
[game]
name       = "Zed"
cartid     = "NZDE"
region     = "E"
revision   = 0
entry_pc   = "0x80100000"
rom_size   = 8388608
byte_order = "z64"
cic        = "CIC-NUS-6102"
cic_seed   = "0x3f"
cic_challenge = "dummy"
crc1       = "11112222"
crc2       = "33334444"
sha256     = "%s"

[recompiler]
harvest_frames = 900
harvest_step_cap_millions = 3000

[runtime]
window_title = "Zed (n64lle)"
""" % ("ab" * 32)

CMAKE = """\
cmake_minimum_required(VERSION 3.20)
project(ZedRecomp C CXX)
include("${N64LLE_ROOT}/runtime/runtime.cmake")
n64lle_runtime_resolve_framework("${N64LLE_ROOT}" "${N64LLE_BUILD}")
set(ZED_ROM "${CMAKE_CURRENT_SOURCE_DIR}/roms/zed.z64" CACHE FILEPATH "dump")
n64lle_add_runtime_target(zed-runtime
  GAME_TOML   "${CMAKE_CURRENT_SOURCE_DIR}/game.toml"
  GEN_LIB     zed_gen
  OUTPUT_NAME zed
  RECOMP_UI   "${CMAKE_CURRENT_SOURCE_DIR}/recomp-ui")
"""


def make_repo(root: Path) -> None:
    """An N64 port mid-migration: framework present, scaffold half-missing."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CMakeLists.txt").write_text(CMAKE, encoding="utf-8")
    (root / "game.toml").write_text(GAME_TOML, encoding="utf-8")
    # A framework checkout, complete with the marker that proves it is one AND
    # its own .git — a submodule checkout has both, and the audit distinguishes
    # them: a plain directory with the right name cannot be pinned or advanced.
    fw = root / "n64lle"
    (fw / "runtime").mkdir(parents=True)
    (fw / "runtime" / "runtime.cmake").write_text("# marker\n", encoding="utf-8")
    git(fw, "init", "-q")
    git(fw, "config", "user.email", "t@example.invalid")
    git(fw, "config", "user.name", "t")
    git(fw, "add", "-A")
    git(fw, "-c", "user.useConfigOnly=false", "commit", "-qm", "fw")
    ui = root / "recomp-ui"
    ui.mkdir()
    (ui / "recomp_ui.cmake").write_text("# marker\n", encoding="utf-8")
    git(ui, "init", "-q")
    git(ui, "config", "user.email", "t@example.invalid")
    git(ui, "config", "user.name", "t")
    git(ui, "add", "-A")
    git(ui, "-c", "user.useConfigOnly=false", "commit", "-qm", "ui")
    # The two things that must never be committed, committed.
    (root / "roms").mkdir()
    (root / "roms" / "zed.z64").write_bytes(b"\x80\x37\x12\x40" + b"\0" * 64)
    (root / "generated").mkdir()
    (root / "generated" / "zed_generated.c").write_text("int x;\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "-c", "user.useConfigOnly=false", "commit", "-qm", "init")


# ---------------------------------------------------------------------------
def test_profile() -> None:
    print("profile")
    n64 = platforms.get("n64")
    check(n64.key == "n64", "n64 resolves")
    check(n64.framework == "n64lle", "framework is n64lle")
    check(n64.framework_branch == "main", "tracks main")
    check(n64.image_exts == (".z64", ".n64", ".v64"), "accepts .z64/.n64/.v64")
    check(n64.is_cartridge, "is a cartridge")
    check(n64.framework_marker == "runtime/runtime.cmake", "marker is runtime.cmake")
    for alias in ("N64", "nintendo64", "nintendo-64", "n64lle"):
        check(platforms.normalize(alias) == "n64", f"{alias!r} normalises to n64")
    # The other two are untouched by the third existing.
    check(platforms.get("psx").framework == "psxrecomp", "psx unchanged")
    check(platforms.get("snes").framework == "snesrecomp", "snes unchanged")
    check(platforms.DEFAULT_KEY == "psx", "default is still psx")


def test_capabilities() -> None:
    """Every question an N64 session answers differently from PSX.

    This is the test that earns its keep. With two consoles every branch could
    be written `== "snes"` with `else` meaning PSX; the moment a third exists,
    those are different claims and the `else` silently hands N64 psxrecomp's
    BIOS hunt, Redump lookup and MinGW script. Asserting the answers here means
    a fourth console gets them wrong loudly, at one table.
    """
    print("capabilities")
    psx, snes, n64 = (platforms.get(k) for k in ("psx", "snes", "n64"))

    check(not n64.has_bios, "no BIOS to stage")
    check(psx.has_bios and not snes.has_bios, "...and PSX still has one, SNES not")
    check(not n64.has_disc_meta, "no Redump/libretro identity")
    check(n64.max_images == 1, "one image, no disc set")
    check(n64.region_default == "", "no region default — the header decides")
    check(psx.region_default == "USA", "...and PSX still defaults USA")
    check(n64.has_image_probe, "has a ROM probe")
    check(not n64.has_netplay, "no netplay")
    check(n64.nested_paths == (), "no nested modules Studio manages")
    check(snes.nested_paths == ("lib/recomp-net", "lib/retcomm-rbengine"),
          "...and SNES still has both")
    check(n64.generate_kind == "cmake-target", "generation is a cmake target")
    check(n64.analysis_kind == "" and not n64.has_analysis, "no analysis pass")
    check(psx.has_analysis and snes.has_analysis, "...and both others still have one")
    check(n64.mingw_script == "", "no MinGW cross-build script")
    check(n64.migrate_module == "n64ops", "migrates through n64ops")


def test_repo_recognition(root: Path) -> None:
    """One port, one list.

    An n64lle port carries a game.toml and so does a PSX one, so before this
    check the PSX arm accepted GloverRecomp into the PSX index — where every
    later Bulk/Build op would look for a psxrecomp that is not there.
    """
    print("repo recognition")
    from project_studio import repo_index

    platforms.set_current("n64")
    check(repo_index.looks_like_game_repo(root), "an n64lle port is an N64 repo")
    check(repo_index.default_index_path().name == "project_studio_repos_n64.json",
          "and lands in the N64 index")
    platforms.set_current("psx")
    check(not repo_index.looks_like_game_repo(root),
          "the same port is NOT a PSX repo (game.toml alone is not enough)")
    platforms.set_current("snes")
    check(not repo_index.looks_like_game_repo(root), "nor a SNES one")
    platforms.set_current("n64")


def test_rom_discovery(root: Path, tmp: Path) -> None:
    """Found by DIGEST, never by name.

    n64lle's scaffold renames the dump to <slug>.z64 in roms/ and records
    nothing about where it came from, so the game.toml sha256 is the whole
    identity. A name match in a library folder is a guess.
    """
    print("ROM discovery")
    from project_studio import repo_index

    platforms.set_current("n64")
    check(repo_index.game_toml_sha256(root) == "ab" * 32, "reads the contract digest")
    # A dump in the tree wins, resolved through a symlink (the scaffold's own
    # default is a link, not a copy).
    found = repo_index.discover_rom(root)
    check(found.endswith("zed.z64"), "finds the dump staged in roms/")

    lib = tmp / "library"
    lib.mkdir()
    (lib / "Wrong Game (USA).z64").write_bytes(b"\0" * 128)
    bare = tmp / "bare"
    bare.mkdir()
    (bare / "game.toml").write_text(GAME_TOML, encoding="utf-8")
    os.environ["RETCOMM_N64_ROM_DIRS"] = str(lib)
    try:
        check(repo_index.discover_rom(bare) == "",
              "a same-extension file whose digest differs is NOT accepted")
    finally:
        os.environ.pop("RETCOMM_N64_ROM_DIRS", None)


def test_audit_plan_apply(root: Path) -> None:
    print("audit / plan / apply")
    from project_studio import n64ops

    platforms.set_current("n64")
    rep = n64ops.audit_project(root)
    ids = {c.id: c for c in rep.checks}
    check(rep.project_name == "ZedRecomp", "reads project() from CMakeLists")
    check(rep.boot_exe == "zed", "reads OUTPUT_NAME as the executable")
    check(n64ops.runtime_target(root) == "zed-runtime", "reads the runtime target")
    check(ids["framework"].status.value == "pass", "framework checkout found")
    # And the case the marker alone would have passed: a plain directory of the
    # right name with the right file in it, which cannot be pinned or advanced.
    plain = root.parent / "PlainFw"
    (plain / "n64lle" / "runtime").mkdir(parents=True, exist_ok=True)
    (plain / "n64lle" / "runtime" / "runtime.cmake").write_text("#\n", encoding="utf-8")
    why = n64ops.diagnose_framework_checkout(plain)
    check(why is not None and "not a git checkout" in why,
          "a plain n64lle/ directory is diagnosed, not accepted")
    check(ids["contract"].status.value == "pass", "contract identity complete")
    check(ids["generated"].status.value == "fail", "committed generated C is a failure")
    check(ids["roms"].status.value == "fail", "committed ROM bytes are a failure")
    check(ids["ci"].status.value == "skip", "release workflow is skipped, not failed")
    check(ids["status_doc"].fix_op is None,
          "docs/STATUS.md has NO fix op — a fresh ledger would assert ignorance")

    plan = n64ops.build_plan(root, MigrateOptions())
    steps = [s.op_id for s in plan.steps]
    check("n64_untrack_generated" in steps, "plans untracking generated C")
    check("n64_untrack_roms" in steps, "plans untracking the ROM")
    check("n64_merge_gitignore" in steps, "plans the .gitignore merge")
    check(all(op in n64ops.OP_ORDER for op in steps), "every planned op is ordered")

    results = n64ops.apply_plan(plan)
    for r in results:
        check(r.ok, f"apply {r.op_id}: {r.message[:60]}")

    # The ROM and the generated C stay ON DISK: untracking is --cached.
    check((root / "roms" / "zed.z64").is_file(), "the dump is not deleted")
    check((root / "generated" / "zed_generated.c").is_file(), "generated C is not deleted")

    again = n64ops.audit_project(root)
    ids2 = {c.id: c for c in again.checks}
    check(ids2["generated"].status.value == "pass", "generated C is no longer tracked")
    check(ids2["roms"].status.value == "pass", "the ROM is no longer tracked")
    check(ids2["gitignore"].status.value == "pass", "the rules are in .gitignore")
    # Idempotent: a second apply of the same plan changes nothing and fails
    # nothing.
    for r in n64ops.apply_plan(n64ops.build_plan(root, MigrateOptions())):
        check(r.ok, f"re-apply {r.op_id}: {r.message[:60]}")


def test_refuses_unresolved_tokens(tmp: Path) -> None:
    """A blank is worse than a refusal.

    Every identity token in roms/README.md comes from game.toml. With a
    contract that carries none, emitting the file would produce a document
    telling a user to check their dump against nothing.
    """
    print("token refusal")
    from project_studio import n64ops

    platforms.set_current("n64")
    bare = tmp / "no_identity"
    bare.mkdir()
    (bare / "game.toml").write_text('[game]\nname = "Nothing"\n', encoding="utf-8")
    r = n64ops._op_emit_roms_readme(bare, MigrateOptions())
    check(not r.ok, "refuses to emit roms/README.md without the identity")
    check("SHA256" in r.message, "and names the missing tokens")
    check(not (bare / "roms" / "README.md").exists(), "nothing was written")


def test_new_project_command(tmp: Path) -> None:
    """The three names, derived by the SCAFFOLDER's rules.

    Studio runs setup_project.sh with --yes, which takes every default
    silently. If Studio's idea of the slug differed from the script's, the
    project would be created under a name Studio then reports as missing.
    """
    print("new project")
    from project_studio import newproject as npj

    platforms.set_current("n64")
    rom = tmp / "Zed Adventure (USA).z64"
    rom.write_bytes(b"\x80\x37\x12\x40" + b"\0" * 64)
    opts = npj.NewProjectOptions(
        platform="n64", name="Zed Adventure", disc=str(rom),
        parent_dir=str(tmp), players=4, do_generate=False, do_build=False,
    )
    check(npj.validate_options(opts) == [], "valid options pass validation")
    check(npj.n64_project_name(opts) == "ZedAdventureRecomp", "project = <Pascal>Recomp")
    check(npj.n64_slug(opts) == "zedadventure", "slug = lowercased, stripped")
    check(npj.n64_exe(opts) == "zedadventure", "exe defaults to the slug")
    check(npj.project_root_for(opts).name == "ZedAdventureRecomp",
          "the folder created is $DIR/$PROJECT, not an install_dir slug")

    cmd, _ = npj.build_command(opts)
    joined = " ".join(cmd)
    for flag in ("--yes", "--rom", "--project", "--slug", "--exe", "--players", "--dir"):
        check(flag in cmd, f"sends {flag}")
    # argv[0] is a RESOLVED shell, not the word "sh". Windows has no sh on
    # PATH, so a literal one never reached the wizard at all; and n64lle's
    # scaffolder is `#!/usr/bin/env bash`, so running it under a /bin/sh that
    # is dash breaks it on Debian and Ubuntu too.
    check(cmd[0] != "sh", "argv[0] is not the bare word sh")
    check(Path(cmd[0]).is_absolute() and "bash" in Path(cmd[0]).name,
          "argv[0] is the bash the host actually has")
    check(cmd[1] == str(n64_paths.setup_script(None)),
          "argv[1] is the scaffolder the shell is handed")
    # Flags this scaffolder does not have. An unknown option is `exit 2` there,
    # so a habitual PSX flag would kill the run before it probed the ROM.
    for flag in ("--description", "--publisher", "--year", "--region", "--zip-prefix",
                 "--enable-ci", "--no-ci", "--fetch-boxart", "--enable-build",
                 "--netplay", "--recomp-ui"):
        check(flag not in joined, f"never sends {flag} (n64lle has no such flag)")
    # --n64lle-ref / --recomp-ui-ref exist on wizards from 2026-09-12 on, and
    # are sent ONLY to a copy whose parser declares them: Studio drives
    # whichever setup_project.sh it found, and that copy can be older than
    # Studio. A blank ref is never sent at all — blank means "keep the
    # scaffolder's own pin", which is not the same as naming main.
    check("--n64lle-ref" not in joined, "a blank n64lle ref sends no flag")
    script = n64_paths.setup_script(None)
    opts.n64lle_ref = "fix/some-branch"
    ref_cmd = " ".join(npj.build_command(opts)[0])
    if npj.script_supports(script, "--n64lle-ref"):
        check("--n64lle-ref fix/some-branch" in ref_cmd,
              "forwards --n64lle-ref to a wizard that declares it")
    else:
        check("--n64lle-ref" not in ref_cmd,
              "withholds --n64lle-ref from a wizard that would exit 2 on it")
    opts.n64lle_ref = ""

    # Five seats is a PSX/SNES notion; the N64 has four ports and the script
    # rejects anything else outright.
    opts.players = 5
    errs = npj.validate_options(opts)
    check(any("1–4" in e for e in errs), "refuses more than four controller ports")
    opts.players = 4

    # A slug the script would reject must not reach it.
    opts.n64_slug = "Zed Adventure"
    check(any("lowercase" in e for e in npj.validate_options(opts)),
          "refuses a slug the scaffolder would reject")
    opts.n64_slug = ""

    # Multi-disc is a PSX notion.
    opts.extra_discs = [str(rom)]
    check(any("one ROM" in e for e in npj.validate_options(opts)),
          "refuses extra discs on a cartridge")


def test_build_target(root: Path) -> None:
    """<slug>-runtime appears in neither project() nor OUTPUT_NAME."""
    print("build target")
    from project_studio import buildops

    platforms.set_current("n64")
    check(buildops.default_target(root) == "zed-runtime", "target read from the macro call")
    check(buildops.n64_project_slug(root) == "zed", "slug derived from it")
    check(buildops.n64_generate_target(root) == "zed-generate", "generate target derived")
    platforms.set_current("psx")
    check(buildops.default_target(root) == "psx-runtime", "PSX still uses its constant")
    platforms.set_current("n64")


def test_framework_preflight(root: Path) -> None:
    """A port resolves n64lle as a PRE-BUILT tree, not add_subdirectory()'d."""
    print("framework preflight")
    from project_studio import buildops

    platforms.set_current("n64")
    r = buildops.preflight_n64_framework(root)
    check(r is not None and not r.ok, "an unbuilt framework blocks configure")
    check(r is not None and "build_framework" in r.message,
          "and the message names the step that was skipped")

    # And the script is run through a resolved shell. build_framework.sh is
    # `#!/usr/bin/env bash`; `sh` was neither that nor anything Windows has on
    # PATH, so the N64 Build tab's first button could not start at all there.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        port = Path(td) / "ZedRecomp"
        (port / "tools").mkdir(parents=True)
        (port / "n64lle" / "runtime").mkdir(parents=True)
        (port / "n64lle" / "runtime" / "runtime.cmake").write_text("")
        (port / "tools" / "build_framework.sh").write_text("#!/usr/bin/env bash\n")
        d = buildops.build_n64_framework(port, config="Release", dry_run=True)
        argv = d.message.removeprefix("dry-run: ").split()
        check(d.ok and argv[0] != "sh", "the framework build is not run by `sh`")
        check("bash" in Path(argv[0]).name,
              "it is run by the bash its shebang asks for")
        check(argv[-1] == "Release", "the build config is still the last argument")


def test_failed_scaffold_is_discarded(tmp: Path) -> None:
    """A scaffold that does not finish leaves nothing behind.

    The failure that prompted this was an n64lle compile error partway through
    `--generate`: the wizard exited 1 having already laid out the repo and
    cloned submodules, and MarioPartyRecomp/ stayed on disk looking like a
    project. The next attempt then refused it as "Destination already exists".
    """
    print("failed scaffold cleanup")
    from project_studio import newproject as npj

    platforms.set_current("n64")
    parent = tmp / "src"
    parent.mkdir(exist_ok=True)
    # The user's own dump, OUTSIDE the project. The scaffolder symlinks it into
    # roms/, so cleanup that followed links would eat the library it came from.
    rom = tmp / "library" / "Zed Adventure (USA).z64"
    rom.parent.mkdir(exist_ok=True)
    rom.write_bytes(b"\x80\x37\x12\x40" + b"\0" * 64)

    opts = npj.NewProjectOptions(
        platform="n64", name="Zed Adventure", disc=str(rom),
        parent_dir=str(parent), players=2,
    )
    dest = npj.project_root_for(opts)

    real_build_command = npj.build_command

    def half_finished_wizard(_opts):
        """Stand in for a wizard that lays the repo out and then dies."""
        script = (
            f"import os,sys;"
            f"os.makedirs(r'{dest}/roms', exist_ok=True);"
            f"os.symlink(r'{rom}', r'{dest}/roms/{rom.name}');"
            f"open(r'{dest}/game.toml','w').write('partial');"
            f"sys.exit(1)"
        )
        return [sys.executable, "-c", script], dict(os.environ)

    npj.build_command = half_finished_wizard
    try:
        r = npj.run_new_project(opts)
    finally:
        npj.build_command = real_build_command

    check(not r.ok, "a wizard that exits non-zero is a failure")
    check(not dest.exists(), "and the half-written project is gone")
    check("removed" in (r.detail or ""), "the cleanup is reported, not silent")
    check(rom.is_file() and rom.stat().st_size == 68,
          "the dump the symlink pointed at is untouched")

    # The escape hatch, for anyone debugging the scaffolder itself.
    os.environ["RETCOMM_KEEP_FAILED_SCAFFOLD"] = "1"
    npj.build_command = half_finished_wizard
    try:
        r = npj.run_new_project(opts)
    finally:
        npj.build_command = real_build_command
        del os.environ["RETCOMM_KEEP_FAILED_SCAFFOLD"]
    check(dest.is_dir(), f"{npj.KEEP_FAILED_ENV} keeps the tree for inspection")
    shutil.rmtree(dest)


def test_refusals() -> None:
    """Dead ends refuse at the question, with a reason."""
    print("CLI refusals")
    toolkit = _REPO / "tools" / "new_project_layout"
    root = str(_REPO)  # any path; these refuse before touching it
    for args, needle in (
        (["build", "ensure-bios"], "psxrecomp-only"),
        (["build", "ensure-emitters"], "psxrecomp-only"),
        (["analyze", "status"], "no analysis bundle"),
        (["analyze", "run"], "no analysis pass"),
        (["analyze", "symbols"], "no analysis bundle"),
        (["build", "mingw"], "no MinGW cross-build script"),
    ):
        proc = subprocess.run(
            [sys.executable, "project_studio/cli.py", "--platform", "n64", *args,
             "--root", root],
            cwd=str(toolkit), capture_output=True, text=True,
        )
        blob = (proc.stdout or "") + (proc.stderr or "")
        check(proc.returncode != 0 and needle in blob,
              f"`{' '.join(args)}` refuses with a reason ({needle})")


def main() -> int:
    import tempfile

    # The migration and New Project checks render n64lle's own wizard
    # templates. Without a checkout they fail one by one, far from the cause;
    # say it once.
    from project_studio import n64_paths
    if n64_paths.wizard_dir(None) is None:
        print(f"error: {n64_paths.MISSING_CHECKOUT}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = tmp / "ZedRecomp"
        make_repo(root)
        test_profile()
        test_capabilities()
        test_repo_recognition(root)
        test_rom_discovery(root, tmp)
        test_build_target(root)
        test_framework_preflight(root)
        test_audit_plan_apply(root)
        test_refuses_unresolved_tokens(tmp)
        test_new_project_command(tmp)
        test_failed_scaffold_is_discarded(tmp)
        test_refusals()
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
