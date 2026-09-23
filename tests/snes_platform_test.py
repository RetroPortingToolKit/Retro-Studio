#!/usr/bin/env python3
"""Headless check of the platform split — the SNES half of Project Studio.

The GUI needs a GPU and a window; the decisions that make a SNES session a
SNES session do not. This drives them directly against a synthetic repo:

  * one platform per process, and the PSX defaults unchanged by its existence
  * a separate repo index per console, so a SNES add never rewrites the PSX list
  * the SNES audit / plan / apply ops, end to end, on a scaffold-shaped tree
  * refusal to emit a file whose ROM digests cannot be resolved — the one
    failure mode worth a test, because inventing them produces a regen.sh that
    verifies a ROM nobody owns

Run:  python3 tests/snes_platform_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "tools" / "new_project_layout"))

from project_studio import platforms  # noqa: E402
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


def make_repo(root: Path) -> None:
    """A SNES port mid-migration: framework present, scaffold half-missing."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(ZedSNESRecomp C)\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# Zed\n", encoding="utf-8")
    (root / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (root / "recomp").mkdir()
    (root / "recomp" / "bank00.cfg").write_text("# seed\n", encoding="utf-8")
    (root / "recomp" / "symbols.toml").write_text("[symbols]\n", encoding="utf-8")
    # A framework checkout, complete with the marker that proves it is one.
    fw = root / "snesrecomp" / "runner"
    fw.mkdir(parents=True)
    (fw / "runner.cmake").write_text("# marker\n", encoding="utf-8")
    # Generated C, wrongly committed — the thing the audit must catch.
    (root / "src" / "gen").mkdir(parents=True)
    (root / "src" / "gen" / "bank00.c").write_text("/* generated */\n", encoding="utf-8")

    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Studio Test")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")


def test_platform_defaults() -> None:
    print("platform profiles")
    check(platforms.current().key == "psx", "a fresh process defaults to psx")
    check(platforms.get("snes").framework == "snesrecomp", "snes framework is snesrecomp")
    check(platforms.get("psx").framework == "psxrecomp", "psx framework is unchanged")
    check(platforms.normalize("SNES") == "snes", "platform keys are case-insensitive")
    check(platforms.normalize("gameboy") == "psx", "an unknown console falls back to psx")


def test_index_separation() -> None:
    print("repo index")
    from project_studio import repo_index

    platforms.set_current("psx")
    psx_path = repo_index.default_index_path()
    platforms.set_current("snes")
    snes_path = repo_index.default_index_path()
    check(psx_path != snes_path, "each console resolves a different index file")
    check(snes_path.name.endswith("_snes.json"), "the SNES index is named for its console")


def test_repo_recognition(root: Path) -> None:
    print("repo recognition")
    from project_studio import repo_index

    platforms.set_current("snes")
    check(repo_index.looks_like_game_repo(root), "a SNES port is recognised under snes")
    platforms.set_current("psx")
    check(
        not repo_index.looks_like_game_repo(root),
        "the same tree is NOT a PSX port (no game.toml, no psxrecomp/)",
    )
    platforms.set_current("snes")


def test_audit(root: Path) -> None:
    print("audit")
    from project_studio import snesops

    report = snesops.audit_project(root)
    by_id = {c.id: c for c in report.checks}
    check(report.project_name == "ZedSNESRecomp", "project name read from project()")
    check(by_id["framework"].status.value == "pass", "framework checkout found")
    check(by_id["analysis"].status.value == "pass", "recomp/ analysis config found")
    check(by_id["generated"].status.value == "fail", "committed generated C is a failure")
    check(
        by_id["generated"].fix_op == "snes_untrack_generated",
        "committed generated C names its fix op",
    )
    check(by_id["gitignore"].status.value == "fail", "missing .gitignore rules are a failure")
    check(by_id["version"].status.value == "pass", "VERSION present")
    check(
        by_id["regen"].fix_op is None,
        "regen.sh is NOT offered as a fix when ROM digests are unknown",
    )


def test_plan_and_apply(root: Path) -> None:
    print("plan + apply")
    from project_studio import snesops

    plan = snesops.build_plan(root, MigrateOptions(dry_run=True))
    ops = [s.op_id for s in plan.steps]
    check("snes_merge_gitignore" in ops, "gitignore merge planned")
    check("snes_untrack_generated" in ops, "untrack planned")
    check("snes_emit_regen" not in ops, "regen.sh not planned without digests")
    check(
        ops.index("snes_merge_gitignore") < ops.index("snes_record_framework_pins"),
        "pins are recorded after the ops that change what is pinned",
    )

    dry = snesops.apply_plan(plan)
    check(all(r.ok for r in dry), "every planned op succeeds as a dry run")
    check(
        not (root / ".gitignore").is_file(),
        "a dry run writes nothing",
    )

    real = snesops.apply_plan(snesops.build_plan(root, MigrateOptions()))
    check(all(r.ok for r in real), "every planned op succeeds for real")
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    check("/src/gen/" in gi and "*.sfc" in gi, ".gitignore now blocks generated C and ROMs")
    out = subprocess.run(
        ["git", "ls-files", "--", "src/gen"], cwd=str(root), capture_output=True, text=True
    ).stdout
    check(out.strip() == "", "generated C is no longer tracked")
    check(
        (root / "src" / "gen" / "bank00.c").is_file(),
        "untracking kept the working-tree file (--cached only)",
    )
    check((root / "framework_pins.txt").is_file(), "framework_pins.txt written")
    pkg = root / "scripts" / "package_release.sh"
    check(pkg.is_file(), "packager emitted from the wizard template")
    check("@ZIP_PREFIX@" not in pkg.read_text(encoding="utf-8"), "no @TOKEN@ survives the fill")
    # NTFS has no execute bit, so chmod(+x) is a documented no-op on Windows
    # and this would fail for a reason that says nothing about the emitter.
    # Nothing depends on the bit either: the packager is always invoked as
    # "bash <script>", never executed directly.
    if os.name != "nt":
        check(pkg.stat().st_mode & 0o111 != 0, "the emitted packager is executable")
    check(
        (root / ".github" / "workflows" / "release.yml").is_file(),
        "CI workflow emitted from the wizard template",
    )

    after = snesops.audit_project(root)
    ids = {c.id: c.status.value for c in after.checks}
    check(ids["gitignore"] == "pass", "re-audit: gitignore now passes")
    check(ids["generated"] == "pass", "re-audit: generated C now passes")


def test_parity_checks(root: Path) -> None:
    """The PSX-parity additions: identity, codegen_setup, boxart, README,
    legacy classification, checkout repair diagnosis, and probe gating."""
    print("parity checks")
    from project_studio import snesops

    # make_repo committed generated C → the legacy layout class.
    before = snesops.audit_project(root)
    check(before.layout.value == "legacy-packaging",
          "committed generated C classifies as legacy-packaging")

    by_id = {c.id: c for c in before.checks}
    check(by_id["rom_identity"].status.value == "warn",
          "no recoverable digests → ROM identity warns")
    check(by_id["rom_identity"].fix_op == "snes_probe_rom_refresh",
          "identity warn names the probe op")
    check(by_id["identity_carrier"].status.value == "fail",
          "a missing ROM identity carrier fails")
    check(by_id["identity_carrier"].fix_op is None,
          "the carrier is NOT offered as a fix without digests")
    check(by_id["boxart"].status.value == "warn", "no boxart warns (optional)")
    check(by_id["readme_metrics"].status.value == "warn",
          "bare README warns with the metrics op")

    # Digest recovery unlocks codegen emission.
    (root / "src" / "codegen_setup.c").write_text(
        '#include "codegen_setup.h"\n'
        "const GameCodegenIdentity kGameCodegenIdentity = {\n"
        '    .display_name   = "Zed",\n'
        '    .rom_file       = "Zed (World).sfc",\n'
        '    .expected_crc32 = "12345678",\n'
        '    .expected_sha256= "aa" ,\n'
        '    .mapping        = "lorom",\n'
        '    .region         = "NTSC",\n'
        "};\n",
        encoding="utf-8",
    )
    ident = snesops.rom_identity(root)
    check(ident.get("crc32") == "12345678", "digests recover from codegen_setup.c")
    check(ident.get("mapping") == "lorom", "mapping recovers from codegen_setup.c")
    mid = {c.id: c for c in snesops.audit_project(root).checks}
    check(mid["rom_identity"].status.value == "pass",
          "identity passes once recoverable")
    # Which carrier is incomplete depends on the wizard this port is pinned to
    # — codegen_setup.c without its header, or a rom_identity.txt that does not
    # exist yet — but either way it fails and names an op that can be applied.
    layout = snesops.identity_layout(root)
    check(layout in ("file", "codegen"),
          f"the wizard declares an identity layout ({layout or 'neither'})")
    expected_op = {"file": "snes_emit_rom_identity",
                   "codegen": "snes_emit_codegen_setup"}[layout]
    check(mid["identity_carrier"].status.value == "fail",
          "an incomplete carrier still fails (the build needs all of it)")
    check(mid["identity_carrier"].fix_op == expected_op,
          f"and names the emit op this wizard can honour ({expected_op})")

    # rom_identity.txt is the current carrier: digests recover from it, and it
    # outranks a codegen_setup.c the older layout left behind.
    (root / "rom_identity.txt").write_text(
        "# ROM identity\n"
        "display_name    = Zed\n"
        'rom_file        = "Zed (World).sfc"\n'
        "expected_crc32  = deadbeef\n"
        "expected_sha256 = bb\n"
        "mapping         = hirom\n"
        "region          = PAL\n",
        encoding="utf-8",
    )
    fid = snesops.rom_identity(root)
    check(fid.get("crc32") == "deadbeef", "digests recover from rom_identity.txt")
    check(fid.get("rom_file") == "Zed (World).sfc",
          "a quoted value is unquoted, as regen.sh identity_get does")
    check(fid.get("mapping") == "hirom",
          "rom_identity.txt outranks a stale codegen_setup.c")
    if layout == "file":
        fchecks = {c.id: c for c in snesops.audit_project(root).checks}
        check(fchecks["identity_carrier"].status.value == "pass",
              "a filled rom_identity.txt passes")
        check(fchecks["identity_stale"].status.value == "warn",
              "and a superseded codegen_setup.c is named, not silently kept")
    (root / "rom_identity.txt").unlink()

    # Probe gating: never planned without a ROM, planned with one.
    plan = snesops.build_plan(root, MigrateOptions(dry_run=True, probe_disc=True))
    check("snes_probe_rom_refresh" not in [st.op_id for st in plan.steps],
          "probe never planned without a ROM path")
    (root / "src" / "codegen_setup.c").unlink()

    # Boxart relocation: legacy file moves to launcher_assets/img/.
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets" / "boxart.png").write_bytes(b"\x89PNG fake")
    box = {c.id: c for c in snesops.audit_project(root).checks}["boxart"]
    check(box.fix_op == "snes_relocate_boxart", "legacy boxart names relocate")
    res = snesops._op_relocate_boxart(root, MigrateOptions())
    check(res.ok and (root / "launcher_assets" / "img" / "boxart.png").is_file(),
          "relocate moves the file into launcher_assets/img/")
    check(not (root / "assets" / "boxart.png").is_file(),
          "and removes the legacy copy")

    # Broken-checkout diagnosis: a .git file pointing nowhere is named.
    (root / "snesrecomp" / ".git").write_text(
        "gitdir: ../.git/modules/gone\n", encoding="utf-8")
    reason = snesops.diagnose_framework_checkout(root)
    check(reason is not None and "missing gitdir" in reason,
          "stale gitdir pointer is diagnosed")
    fw_check = {c.id: c for c in snesops.audit_project(root).checks}["framework"]
    check(fw_check.fix_op == "snes_repair_framework_submodule",
          "broken checkout names the repair op")
    (root / "snesrecomp" / ".git").unlink()


def test_netplay_flip(root: Path) -> None:
    print("netplay flip")
    from project_studio import snesops

    cml = root / "CMakeLists.txt"
    base = cml.read_text(encoding="utf-8")
    # The scaffold fixture has no launcher call; the op appends after
    # add_executable-derived target discovery... it needs add_executable.
    cml.write_text(base + "add_executable(ZedSNESRecomp src/main.c)\n",
                   encoding="utf-8")

    plan = snesops.build_plan(root, MigrateOptions(dry_run=True))
    check(not any("netplay" in st.op_id for st in plan.steps),
          "no netplay op planned without the flag")
    plan = snesops.build_plan(root, MigrateOptions(enable_netplay=True))
    check("snes_enable_netplay" in [st.op_id for st in plan.steps],
          "--enable-netplay plans the enable op")
    plan = snesops.build_plan(root,
                              MigrateOptions(enable_netplay=True, players=1))
    check("snes_enable_netplay" not in [st.op_id for st in plan.steps],
          "1-player titles cannot opt in")

    res = snesops._op_enable_netplay(root, MigrateOptions())
    text = cml.read_text(encoding="utf-8")
    check(res.ok and "snesrecomp_enable_recomp_net(ZedSNESRecomp)" in text,
          "enable wires the call with the discovered target")
    audit = {c.id: c for c in snesops.audit_project(root).checks}
    check(audit["netplay"].status.value == "pass",
          "audit reports netplay wired")

    res = snesops._op_disable_netplay(root, MigrateOptions())
    text = cml.read_text(encoding="utf-8")
    check(res.ok and "# snesrecomp_enable_recomp_net(" in text,
          "disable comments the call out")
    res = snesops._op_enable_netplay(root, MigrateOptions())
    text = cml.read_text(encoding="utf-8")
    check(res.ok and "\nsnesrecomp_enable_recomp_net(" in text.replace(
              "# snesrecomp", "XX"),
          "re-enable uncomments rather than duplicating")
    check(text.count("snesrecomp_enable_recomp_net(") == 1,
          "exactly one call after the round trip")
    cml.write_text(base, encoding="utf-8")


def test_version_stamp(root: Path) -> None:
    print("lobby pin stamp")
    from project_studio import snesops

    build = root / "build-release"
    build.mkdir(exist_ok=True)
    (build / "snes_game_version.txt").write_text("0.9.9\n", encoding="utf-8")
    stamp = {c.id: c for c in snesops.audit_project(root).checks}.get(
        "version_stamp_match")
    check(stamp is not None and stamp.status.value == "fail",
          "stamp drift against VERSION fails")
    (build / "snes_game_version.txt").write_text("0.2.0\n", encoding="utf-8")
    stamp = {c.id: c for c in snesops.audit_project(root).checks}.get(
        "version_stamp_match")
    check(stamp is not None and stamp.status.value == "pass",
          "matching stamp passes")
    (build / "snes_game_version.txt").unlink()


def test_digest_recovery(root: Path) -> None:
    print("template tokens")
    from project_studio import snesops

    check(snesops.rom_identity(root) == {}, "no digests recoverable from a repo without regen.sh")
    (root / "tools").mkdir(exist_ok=True)
    (root / "tools" / "regen.sh").write_text(
        'EXPECTED_CRC32="${SNESRECOMP_EXPECTED_CRC32:-deadbeef}"\n'
        'EXPECTED_SHA256="${SNESRECOMP_EXPECTED_SHA256:-abc123}"\n'
        '  for cand in "Zed (USA).sfc" ; do :; done\n',
        encoding="utf-8",
    )
    ident = snesops.rom_identity(root)
    check(ident.get("crc32") == "deadbeef", "CRC32 carried across from the existing regen.sh")
    check(ident.get("sha256") == "abc123", "SHA256 carried across")
    check(ident.get("rom_file") == "Zed (USA).sfc", "ROM filename carried across")

    values = snesops._template_values(root, MigrateOptions())
    check(values.get("ROM_CRC32") == "deadbeef", "recovered digests reach the template values")
    check(values.get("ROM_SLUG") == "Zed (USA)", "ROM_SLUG derived from the recovered filename")

    # With digests in hand a forced re-emit of regen.sh now resolves every
    # token — the same file, carried forward rather than re-derived.
    r = snesops._op_emit_regen(root, MigrateOptions(force=True))
    check(r.ok, f"regen.sh re-emits once digests are known ({r.message})")
    regen = (root / "tools" / "regen.sh").read_text(encoding="utf-8")
    # The current template does not embed the digest at all: regen.sh reads
    # rom_identity.txt through the framework's parser, so a revision bump is
    # one edit. Either spelling carries the identity forward.
    check("deadbeef" in regen or "rom_identity.txt" in regen,
          "the re-emitted regen.sh keeps the original CRC32 (or reads rom_identity.txt)")
    check("@ROM_SHA256@" not in regen, "no ROM token survives the fill")


def test_probe_rom(root: Path) -> None:
    """Probing a real ROM, not just recovering digests from an old regen.sh.

    The wizard's probe writes JSON to --json-out and prints a human summary to
    stdout. Reading stdout instead parses the summary, finds no digests, and
    fails as "cannot emit regen.sh" — a wrong answer that looks like the
    correct refusal, which is exactly why this is tested against a real file.
    """
    print("probe ROM")
    from project_studio import snes_paths, snesops

    probe = snes_paths.probe_rom_script(root)
    if probe is None or not probe.is_file():
        print("  skip  no probe_rom.py available")
        return
    # A minimal LoROM image the probe can read: 32 KiB with a header at $7FC0.
    rom = root / "Synthetic (USA).sfc"
    raw = bytearray(b"\x00" * 0x8000)
    title = b"SYNTHETIC TEST      "  # 21 bytes
    raw[0x7FC0:0x7FC0 + 21] = title[:21].ljust(21, b" ")
    raw[0x7FDB] = 0x00  # version
    raw[0x7FD9] = 0x01  # region: USA
    raw[0x7FFC:0x7FFE] = b"\x00\x80"  # reset vector
    rom.write_bytes(bytes(raw))

    ident = snesops.rom_identity(root, str(rom))
    check(len(ident.get("crc32", "")) == 8, f"CRC32 probed from the ROM ({ident.get('crc32')})")
    check(len(ident.get("sha256", "")) == 64, "SHA256 probed from the ROM")
    check(ident.get("rom_file") == rom.name, "ROM filename recorded")
    check(
        ident.get("crc32") != "deadbeef",
        "a probed ROM overrides the digests recovered from regen.sh",
    )

    # The op itself, not just the probe behind it. This is the one that died
    # with "Template not found: .../codegen_setup.c.in" once snesrecomp moved
    # identity into rom_identity.txt: it must write whatever carrier the wizard
    # in front of it scaffolds, and never name a template that wizard lacks.
    res = snesops._op_probe_rom_refresh(root, MigrateOptions(disc=str(rom), force=True))
    check(res.ok, f"probe refresh applies against the live wizard ({res.message})")
    for _tpl, rel in snesops.identity_carriers(root):
        check((root / rel).is_file(), f"probe refresh wrote {rel}")
    regen_after = (root / "tools" / "regen.sh").read_text(encoding="utf-8")
    check(ident["crc32"] in regen_after
          or "identity_get expected_crc32" in regen_after
          or "--get expected_crc32" in regen_after,
          "regen.sh either carries the fresh CRC32 or reads it from the file")

_CODEGEN_SETUP_C = """const GameCodegenIdentity kGameCodegenIdentity = {
    .display_name   = "@DISPLAY_NAME@",
    .rom_file       = "@ROM_FILE@",
    .expected_crc32 = "@ROM_CRC32@",
    .expected_sha256= "@ROM_SHA256@",
    .mapping        = "@ROM_MAPPING@",
    .region         = "@REGION@",
};
"""

_ROM_IDENTITY_TXT = """# ROM identity for @DISPLAY_NAME@.
display_name    = @DISPLAY_NAME@
rom_file        = @ROM_FILE@
expected_crc32  = @ROM_CRC32@
expected_sha256 = @ROM_SHA256@
mapping         = @ROM_MAPPING@
region          = @REGION@
"""

_REGEN_SH = 'EXPECTED_CRC32="${SNESRECOMP_EXPECTED_CRC32:-@ROM_CRC32@}"\n'


def _fake_wizard(base: Path, templates: dict[str, str]) -> Path:
    """The smallest thing snes_paths will accept as a snesrecomp checkout."""
    (base / "runner").mkdir(parents=True, exist_ok=True)
    (base / "runner" / "runner.cmake").write_text("", encoding="utf-8")
    wiz = base / "tools" / "new_project"
    (wiz / "templates").mkdir(parents=True, exist_ok=True)
    (wiz / "setup_project.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    for name, body in templates.items():
        (wiz / "templates" / name).write_text(body, encoding="utf-8")
    return base


def _seed_port(root: Path) -> None:
    """A port already carrying identity, in the shape the old wizard left."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "codegen_setup.c").write_text(
        'const GameCodegenIdentity kGameCodegenIdentity = {\n'
        '    .display_name   = "Zed",\n'
        '    .rom_file       = "Zed (USA).sfc",\n'
        '    .expected_crc32 = "deadbeef",\n'
        '    .expected_sha256= "abc123",\n'
        '    .mapping        = "lorom",\n'
        '    .region         = "USA",\n'
        "};\n",
        encoding="utf-8",
    )


def test_identity_layouts() -> None:
    """Studio follows the wizard's identity carrier, across the move.

    snesrecomp replaced src/codegen_setup.c/.h with a single rom_identity.txt
    that CMake, regen.sh and the release workflow all read. Studio drives
    whichever wizard a port is *pinned* to, so both eras run through the same
    code — and neither may name a template the other lacks. Hardcoding the old
    pair is what made a migration against a current checkout die with
    "Template not found: .../codegen_setup.c.in".

    Both wizards are synthetic on purpose: this must keep testing the skew long
    after every checkout on this machine has moved past it.
    """
    print("identity layouts")
    from project_studio import snesops

    cases = (
        ("file",
         {"rom_identity.txt.in": _ROM_IDENTITY_TXT, "regen.sh.in": _REGEN_SH},
         ["rom_identity.txt"],
         "snes_emit_rom_identity"),
        ("codegen",
         {"codegen_setup.c.in": _CODEGEN_SETUP_C,
          "codegen_setup.h.in": "/* @DISPLAY_NAME@ */\n",
          "regen.sh.in": _REGEN_SH},
         ["src/codegen_setup.c", "src/codegen_setup.h"],
         "snes_emit_codegen_setup"),
    )
    prev = os.environ.get("SNESRECOMP_ROOT")
    try:
        for layout, templates, rels, emit_op in cases:
            with tempfile.TemporaryDirectory() as td:
                os.environ["SNESRECOMP_ROOT"] = str(
                    _fake_wizard(Path(td) / "snesrecomp", templates))
                root = Path(td) / "Port"
                _seed_port(root)

                check(snesops.identity_layout(root) == layout,
                      f"{layout}: the wizard's templates decide the layout")
                check([r for _, r in snesops.identity_carriers(root)] == rels,
                      f"{layout}: carriers are {', '.join(rels)}")

                by_id = {c.id: c for c in snesops.audit_project(root).checks}
                check(by_id["identity_carrier"].fix_op == emit_op,
                      f"{layout}: the audit names {emit_op}")

                res = snesops._OPS[emit_op](root, MigrateOptions(force=True))
                check(res.ok, f"{layout}: {emit_op} applies ({res.message})")
                for rel in rels:
                    body = (root / rel).read_text(encoding="utf-8")
                    check("@" not in body, f"{layout}: no @TOKEN@ survives in {rel}")
                check("deadbeef" in (root / rels[0]).read_text(encoding="utf-8"),
                      f"{layout}: the recovered CRC32 is carried across, not re-derived")

                # The legacy op id routes to whatever this wizard scaffolds; it
                # must never reach for a template the wizard dropped.
                legacy = snesops._op_emit_codegen_setup(root, MigrateOptions(force=True))
                check(legacy.ok,
                      f"{layout}: the legacy op id still applies ({legacy.message})")

                after = {c.id: c for c in snesops.audit_project(root).checks}
                check(after["identity_carrier"].status.value == "pass",
                      f"{layout}: the carrier passes once written")
                check(("identity_stale" in after) == (layout == "file"),
                      f"{layout}: a superseded codegen_setup.c is named only "
                      "when the framework has moved past it")

        # A wizard offering neither template is a broken tool, and the ops say
        # so by name instead of surfacing a bare missing-file path.
        with tempfile.TemporaryDirectory() as td:
            os.environ["SNESRECOMP_ROOT"] = str(
                _fake_wizard(Path(td) / "snesrecomp", {"regen.sh.in": _REGEN_SH}))
            root = Path(td) / "Port"
            _seed_port(root)
            check(snesops.identity_layout(root) == "",
                  "neither template present → no layout claimed")
            res = snesops._op_emit_rom_identity(root, MigrateOptions(force=True))
            check(not res.ok and "neither rom_identity.txt.in" in res.message,
                  f"and the op refuses by name ({res.message})")
            stuck = {c.id: c for c in snesops.audit_project(root).checks}
            check(stuck["identity_carrier"].fix_op is None,
                  "the audit offers no fix op it could not honour")
    finally:
        if prev is None:
            os.environ.pop("SNESRECOMP_ROOT", None)
        else:
            os.environ["SNESRECOMP_ROOT"] = prev

def test_readme_toggle() -> None:
    """The README & About switch gates the audit row, not just the op.

    One switch for both because one op writes both: README.md's badge, boxart,
    launcher and R.A.I.D. blocks *and* the repository's GitHub About blurb. A
    port that hand-writes its README should stop being told about it on every
    run, so the row goes to SKIP rather than disappearing — a row that vanishes
    reads as "nothing to do here", which is the opposite of what was asked for.

    Both consoles, because the two migrations are independent implementations
    and a switch wired into only one of them is the bug this guards against.
    """
    print("README & About toggle")
    from project_studio import detect, plan as psx_plan, snesops

    backends = (
        ("snes", snesops.audit_project, snesops.build_plan, "snes_patch_readme_metrics"),
        ("psx", detect.audit_project, psx_plan.build_plan, "patch_readme_metrics"),
    )
    for name, audit, build, op in backends:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Port"
            root.mkdir()
            # No README at all: the loudest thing the check can say, so a SKIP
            # here cannot be mistaken for the repo simply being clean.
            on = {c.id: c for c in audit(root).checks}["readme_metrics"]
            check(on.status.value == "warn", f"{name}: on → the row warns")
            check(on.fix_op == op, f"{name}: on → the row names {op}")

            off_opts = MigrateOptions(patch_readme=False)
            off = {c.id: c for c in audit(root, off_opts).checks}["readme_metrics"]
            check(off.status.value == "skip", f"{name}: off → the row is SKIP, not absent")
            check(off.fix_op is None, f"{name}: off → the row names no op")
            check(off.severity.value == "info",
                  f"{name}: off → INFO, so it stops counting toward the layout class")

            steps_on = [st.op_id for st in build(root, MigrateOptions()).steps]
            check(op in steps_on, f"{name}: on → {op} is planned")
            steps_off = [st.op_id for st in build(root, off_opts).steps]
            check(op not in steps_off, f"{name}: off → {op} is not planned")
            # --only is the explicit escape hatch every other switch honours;
            # asking for the op by name still gets it.
            forced = [st.op_id for st in build(root, MigrateOptions(
                patch_readme=False, only=[op])).steps]
            check(op in forced, f"{name}: off → --only {op} still plans it")

_FAKE_CLI = """#!/usr/bin/env python3
import argparse
ap = argparse.ArgumentParser(prog="snesrecomp")
sub = ap.add_subparsers(dest="command", required=True)
%s
ap.parse_args()
"""


def _fake_framework(base: Path, commands: tuple[str, ...]) -> Path:
    """A snesrecomp checkout whose CLI offers exactly `commands`."""
    base.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f'sub.add_parser({c!r})' for c in commands)
    (base / "snesrecomp_cli.py").write_text(_FAKE_CLI % body, encoding="utf-8")
    return base


# Stands for a regen.sh emitted by a current wizard, so it has to carry the
# interface one really has: the option arms as well as the $CLI call sites.
# Studio now refuses to hand a flag to a script whose parser does not accept
# it (MegaManXSNESRecomp's hand-written driver answers --rom with "unknown
# argument"), and a fixture that modelled only the call sites would exercise
# that gate against a script no wizard ever emitted.
_REGEN_SH_MODERN = """#!/usr/bin/env bash
IDENTITY="$ROOT/rom_identity.txt"
while [ $# -gt 0 ]; do
  case "$1" in
    --rom) ROM=$2; shift 2 ;;
    --no-verify) VERIFY=0; shift ;;
    --cfg-roots) CFG_ROOTS=1; shift ;;
    -h|--help) exit 0 ;;
    *) echo "unknown: $1" >&2; exit 2 ;;
  esac
done
"$PYTHON" "$CLI" verify-rom --rom "$ROM"
"$PYTHON" "$CLI" generate --rom "$ROM"
"""


def test_generate_preflight() -> None:
    """Generate refuses, by name, when the pinned framework cannot run regen.sh.

    A port forked from GitHub carries a regen.sh emitted by whatever wizard was
    current, and a snesrecomp submodule pinned to whatever that fork pointed at.
    When those disagree the raw failure is an argparse "invalid choice:
    'verify-rom'" with exit 2, attributed to Studio's Generate button. The
    preflight has to name the skew instead — and it must ask the CLI what it
    supports rather than assume, because assuming is how this happened.
    """
    print("generate preflight")
    from project_studio import buildops

    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Port"
            (root / "tools").mkdir(parents=True)
            (root / "tools" / "regen.sh").write_text(_REGEN_SH_MODERN, encoding="utf-8")

            # 1. No framework checked out at all.
            r = buildops.preflight_snes_generate(root)
            check(r is not None and "git submodule update" in r.message,
                  f"an absent snesrecomp names the submodule command ({r.message[:48]}…)")

            # 2. The reported case: a framework older than its own regen.sh.
            fw = _fake_framework(root / "snesrecomp", ("build",))
            check(buildops.snes_cli_commands(fw / "snesrecomp_cli.py") == {"build"},
                  "the CLI is asked what it supports, not assumed")
            r = buildops.preflight_snes_generate(root)
            check(r is not None and "older than its own" in r.message,
                  "an old framework is named as skew, not as an argparse error")
            check(r is not None and "'verify-rom'" in r.message and "'generate'" in r.message,
                  "and both missing subcommands are named")

            # 3. --no-verify does not need verify-rom, but still needs generate.
            r = buildops.preflight_snes_generate(root, verify=False)
            check(r is not None and "'verify-rom'" not in r.message,
                  "no-verify stops asking for verify-rom")
            check(r is not None and "'generate'" in r.message,
                  "but generate is still required")

            # 4. A current framework, but no digests for --verify to check.
            _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))
            r = buildops.preflight_snes_generate(root)
            check(r is not None and "rom_identity.txt" in r.message and "is missing" in r.message,
                  "an absent rom_identity.txt is caught before regen.sh verifies nothing")
            (root / "rom_identity.txt").write_text(
                "expected_crc32  =\nexpected_sha256 =\n", encoding="utf-8")
            r = buildops.preflight_snes_generate(root)
            check(r is not None and "carries no digests" in r.message,
                  "and so is one that carries empty digests")

            # 5. Everything in place — the preflight gets out of the way.
            (root / "rom_identity.txt").write_text(
                "expected_crc32  = deadbeef\nexpected_sha256 = abc123\n", encoding="utf-8")
            check(buildops.preflight_snes_generate(root) is None,
                  "a pinned framework that can run regen.sh is not blocked")
            check(buildops.preflight_snes_generate(root, verify=False) is None,
                  "and neither is the no-verify path")

            # 6. Whatever the preflight cannot foresee is still read, not
            #    passed through as somebody else's stack trace.
            hint = buildops.diagnose_generate_failure(
                "snesrecomp: error: argument command: invalid choice: 'verify-rom' "
                "(choose from build)", root)
            check(hint is not None and "predates" in hint,
                  "the raw argparse error is translated after the fact too")
    finally:
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev

# Stands in for snesrecomp's tools/check_runner_paths.py. Deliberately a
# stand-in and not the real thing: the real tool is owned by the framework and
# has its own gate there (tests/v2/test_runner_paths.py). What THIS repo owns
# is the wiring — which halves get audited, how a pin too old to answer is
# reported, and whether a flag is sent to a copy that has never heard of it —
# and a fixture is the only way to exercise the last of those, because the
# whole point is a tool older than the flag.
_FAKE_PATH_TOOL = """#!/usr/bin/env python3
import argparse, pathlib, sys
ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=None)
ap.add_argument("--fix", action="store_true")
ap.add_argument("--include-docs", action="store_true")
ap.add_argument("--quiet", action="store_true")
%s
args = ap.parse_args()
repo = pathlib.Path(args.repo or ".").resolve()
# One rule, enough to be a real audit: a line naming runner/src/<name>.c is
# broken unless runner/src/moved/<name>.c exists, and --fix rewrites it.
broken = 0
for p in sorted(repo.rglob("*.txt")) + sorted(repo.rglob("*.cmake")):
    if ".git" in p.parts:
        continue
    text = p.read_text(encoding="utf-8", errors="replace")
    if "runner/src/stale.c" not in text:
        continue
    if args.fix:
        p.write_text(text.replace("runner/src/stale.c", "runner/src/moved/stale.c"),
                     encoding="utf-8")
        print("  fixed  %%s" %% p.name)
    else:
        broken += 1
        print("  BROKEN %%s" %% p.name)
sys.exit(1 if broken else 0)
"""


def _fake_path_tool(framework: Path, *, aimable: bool) -> None:
    """Install the stand-in auditor; `aimable` = this copy has --runner-src."""
    tools = framework / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    arm = 'ap.add_argument("--runner-src", default=None)' if aimable else ""
    (tools / "check_runner_paths.py").write_text(
        _FAKE_PATH_TOOL % arm, encoding="utf-8"
    )


def test_check_runner_paths() -> None:
    """The Build tab's runner-path audit: both halves, and the pin skew.

    snesrecomp's runner/src is organised into layer folders, so moving a file
    between them is a rename with no content change — nothing objects until a
    port configures and cmake says "Cannot find source file", one file per
    target, in the GAME's repo, for a defect that lives in the framework. The
    op therefore audits the framework FIRST and the port second, and reports a
    pin it cannot question as a failure rather than as a pass.
    """
    print("runner/src path audit")
    from project_studio import buildops

    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Zed"
            fw = root / "snesrecomp"
            (fw / "runner" / "src" / "moved").mkdir(parents=True)
            (root / "CMakeLists.txt").write_text("# clean\n", encoding="utf-8")

            # 1. A pin with no tool is not a clean bill of health.
            r = buildops.check_snes_runner_paths(root)
            check(not r.ok, "a pin without the tool FAILS rather than passing")
            check("check_runner_paths.py" in r.message,
                  "...and names the tool the pin is missing")

            # 2. Clean framework + clean port.
            _fake_path_tool(fw, aimable=True)
            r = buildops.check_snes_runner_paths(root)
            check(r.ok, "a clean framework and port pass")

            # 3. A stale reference in the PORT's own CMakeLists is caught —
            #    the half a framework-only check would miss.
            (root / "CMakeLists.txt").write_text(
                "add_executable(t runner/src/stale.c)\n", encoding="utf-8")
            r = buildops.check_snes_runner_paths(root)
            check(not r.ok, "a stale reference in the port is a failure")
            check("port" in r.message, "...and the message says which half")

            # 4. --fix repairs it, and the next check is clean.
            (fw / "runner" / "src" / "moved" / "stale.c").write_text("", encoding="utf-8")
            r = buildops.check_snes_runner_paths(root, fix=True)
            check(r.ok, "--fix repairs the port's reference")
            check("runner/src/moved/stale.c" in
                  (root / "CMakeLists.txt").read_text(encoding="utf-8"),
                  "...by rewriting the file, not by reporting success")

            # 5. A stale FRAMEWORK is reported as the framework's, even when
            #    the port is spotless — that is the case that broke three ports.
            (fw / "runner.cmake").write_text(
                "set(S runner/src/stale.c)\n", encoding="utf-8")
            r = buildops.check_snes_runner_paths(root)
            check(not r.ok and "framework" in r.message,
                  "a stale framework is named as the framework's defect")

        # 6. Pin skew: the port builds against a framework somewhere else, and
        #    the pinned tool has no --runner-src to aim at it. Studio must say
        #    the port half could not be asked, not report it clean.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Zed"
            root.mkdir(parents=True)
            fw = Path(td) / "elsewhere" / "snesrecomp"
            (fw / "runner" / "src" / "moved").mkdir(parents=True)
            _fake_path_tool(fw, aimable=False)
            os.environ["SNESRECOMP_ROOT"] = str(fw)
            r = buildops.check_snes_runner_paths(root)
            check(not r.ok, "an unaimable tool does not pass the port half")
            check("--runner-src" in r.message,
                  "...and names the flag the pinned tool is missing")
            os.environ.pop("SNESRECOMP_ROOT", None)

            # The same layout with an aimable tool is audited normally.
            _fake_path_tool(fw, aimable=True)
            os.environ["SNESRECOMP_ROOT"] = str(fw)
            r = buildops.check_snes_runner_paths(root)
            check(r.ok, "an aimable tool audits a port built elsewhere")
    finally:
        os.environ.pop("SNESRECOMP_ROOT", None)
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev


def test_regen_framework_skew() -> None:
    """Studio must not write a regen.sh the port's own snesrecomp cannot run.

    This is the defect behind the Generate failure, not just its symptom. A
    fork carries whatever snesrecomp gitlink its parent recorded; Studio drives
    whichever wizard it can find, which on such a fork is a sibling checkout.
    Emitting that wizard's regen.sh into the fork bakes in calls
    the pinned CLI has never heard of, and nothing notices until somebody
    presses Generate. Refusing to write the file is the fix — writing it anyway
    only moves the failure somewhere less legible.
    """
    print("regen.sh vs pinned framework")
    from project_studio import buildops, snes_paths, snesops

    # regen.sh honours $SNESRECOMP_ROOT, so it has to be unset for the fork's
    # own pinned framework to be the one measured. But the WIZARD still has to
    # come from somewhere else -- that is the scenario -- and on CI the only
    # checkout is the one that variable names. So the wizard lookup keeps
    # seeing it while regen.sh's rule does not.
    prev = os.environ.get("SNESRECOMP_ROOT")
    wizard_env = snes_paths._env_root()
    real_env_root = snes_paths._env_root
    snes_paths._env_root = lambda: wizard_env
    os.environ.pop("SNESRECOMP_ROOT", None)
    try:
        # Read out of the script's own call sites: prose naming a command is
        # not a call, which is what `echo "regen.sh: $CLI missing"` looks like.
        text = (
            'echo "regen.sh: $CLI missing — run: git submodule update" >&2\n'
            '"$PYTHON" "$CLI" verify-rom --rom "$ROM"\n'
            '"$PYTHON" "$CLI" generate --rom "$ROM"\n'
        )
        check(snes_paths.regen_cli_commands(text) == ["verify-rom", "generate"],
              "regen.sh's calls are read from its call sites, not from prose")
        check("missing" not in snes_paths.regen_cli_commands(text),
              "an echo mentioning $CLI is not mistaken for a subcommand")
        check(snes_paths.regen_cli_commands(text, verify=False) == ["generate"],
              "--no-verify drops the one call regen.sh gates on verification")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Fork"
            (root / "tools").mkdir(parents=True)
            (root / "tools" / "regen.sh").write_text(text, encoding="utf-8")
            _fake_framework(root / "snesrecomp", ("build",))

            gap = snes_paths.regen_framework_gap(root, text)
            check(gap is not None and gap[0] == ["verify-rom", "generate"],
                  "the gap names what the pinned CLI is missing")
            check(gap is not None and gap[1] == {"build"},
                  "and what it does offer")

            # The audit says so on the Migrate tab, before Generate is pressed.
            by_id = {c.id: c for c in snesops.audit_project(root).checks}
            check(by_id["regen_framework"].status.value == "fail",
                  "the audit fails on the skew")
            check(by_id["regen_framework"].fix_op is None,
                  "and offers no fix op — moving a framework pin is a human's call")
            check(by_id["wizard_source"].status.value == "warn",
                  "and names the wizard being driven, which is where this came from")

            # The emit ops refuse rather than overwrite with a broken script.
            before = (root / "tools" / "regen.sh").read_text(encoding="utf-8")
            r = snesops._op_emit_regen(root, MigrateOptions(force=True))
            check(not r.ok and "would not run against" in r.message,
                  f"emit regen.sh refuses ({r.message[:56]}…)")
            check((root / "tools" / "regen.sh").read_text(encoding="utf-8") == before,
                  "and leaves the existing script untouched")

            # One wording, wherever the skew is met.
            cli = snes_paths.regen_framework_root(root) / "snesrecomp_cli.py"
            shared = buildops.framework_gap_message(cli, ["generate"], {"build"})
            check("that has it" in shared, "a single missing command reads as 'it'")
            check("that has them" in buildops.framework_gap_message(
                cli, ["generate", "verify-rom"], {"build"}),
                  "and two or more as 'them'")

            # A framework that can run it is not blocked.
            _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))
            check(snes_paths.regen_framework_gap(root, text) is None,
                  "a current framework reports no gap")
            ok_ids = {c.id: c for c in snesops.audit_project(root).checks}
            check(ok_ids["regen_framework"].status.value == "pass",
                  "and the audit row passes")
    finally:
        snes_paths._env_root = real_env_root
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev

_CLI_WITH = """import argparse
ap = argparse.ArgumentParser(prog="snesrecomp")
s = ap.add_subparsers(dest="command", required=True)
%s
ap.parse_args()
"""

# A local `file://` submodule is refused by default (git's CVE-2022-39253
# mitigation). Injected through the environment rather than written into the
# user's global config, and only for the fixture.
_FILE_PROTOCOL_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "protocol.file.allow",
    "GIT_CONFIG_VALUE_0": "always",
}


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return (r.stdout or "").strip()


def _stale_fork(base: Path) -> tuple[Path, str, str]:
    """A port pinned to a framework revision older than its own regen.sh.

    Real git objects, not a mock: the thing under test is whether a *gitlink*
    moves and gets staged, and a fake directory cannot answer that.
    """
    env = {**os.environ, **_FILE_PROTOCOL_ENV,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def run(cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)

    fw = base / "fw"
    fw.mkdir(parents=True)
    run(fw, "init", "-q", "-b", "main")
    (fw / "runner").mkdir()
    (fw / "runner" / "runner.cmake").write_text("", encoding="utf-8")
    (fw / "snesrecomp_cli.py").write_text(
        _CLI_WITH % 's.add_parser("build")', encoding="utf-8")
    # A module one level down, where a PSX port keeps most of what it pins.
    lib = base / "netlib"
    lib.mkdir()
    run(lib, "init", "-q", "-b", "main")
    (lib / "v.txt").write_text("1\n", encoding="utf-8")
    run(lib, "add", "-A")
    run(lib, "commit", "-qm", "lib old")
    run(fw, "submodule", "add", "-q", "-b", "main", str(lib), "lib/recomp-net")
    run(fw, "add", "-A")
    run(fw, "commit", "-qm", "old")
    old = _git(fw, "rev-parse", "HEAD")
    (lib / "v.txt").write_text("2\n", encoding="utf-8")
    run(lib, "add", "-A")
    run(lib, "commit", "-qm", "lib new")
    lib_new = _git(lib, "rev-parse", "HEAD")
    (fw / "snesrecomp_cli.py").write_text(
        _CLI_WITH % ('s.add_parser("build")\ns.add_parser("generate")\n'
                     's.add_parser("verify-rom")'), encoding="utf-8")
    run(fw, "add", "-A")
    run(fw, "commit", "-qm", "new")
    new = _git(fw, "rev-parse", "HEAD")

    port = base / "port"
    port.mkdir()
    run(port, "init", "-q", "-b", "main")
    run(port, "submodule", "add", "-q", "-b", "main", "../fw", "snesrecomp")
    run(port / "snesrecomp", "checkout", "-q", old)
    (port / "tools").mkdir()
    (port / "tools" / "regen.sh").write_text(
        '"$PYTHON" "$CLI" verify-rom --rom "$ROM"\n'
        '"$PYTHON" "$CLI" generate --rom "$ROM"\n', encoding="utf-8")
    run(port, "add", "-A")
    run(port, "commit", "-qm", "init")
    return port, old, new, lib_new


def test_advance_pins() -> None:
    """Moving a stale fork's framework pin, which `submodule update` cannot do.

    `git submodule update` checks out the gitlink the superproject *already
    records*, so on a fork carrying an old pin it puts the old revision back —
    which is why reaching for it leaves the pin exactly where it was. Advancing
    is a different operation, and the half that is easy to forget is staging
    the new gitlink: without it nothing about the superproject has changed.
    """
    print("advance framework pins")
    from project_studio import gitops, snesops

    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    saved = {k: os.environ.get(k) for k in _FILE_PROTOCOL_ENV}
    os.environ.update(_FILE_PROTOCOL_ENV)
    try:
        with tempfile.TemporaryDirectory() as td:
            port, old, new, lib_new = _stale_fork(Path(td))
            check(old != new and len(old) == 40, "the fixture has two real revisions")
            check(_git(port / "snesrecomp", "rev-parse", "HEAD") == old,
                  "the port starts pinned to the old one")

            before = {c.id: c for c in snesops.audit_project(port).checks}
            check(before["regen_framework"].status.value == "fail",
                  "and the audit fails on the skew")

            # The operation people reach for first, and what it actually does.
            gitops.update_submodules(port, paths=["snesrecomp"])
            check(_git(port / "snesrecomp", "rev-parse", "HEAD") == old,
                  "`submodule update` puts the recorded pin back — it cannot advance")

            dry = gitops.advance_submodule_pins(port, paths=["snesrecomp"], dry_run=True)
            check(all(r.ok for r in dry) and "would advance" in dry[0].message,
                  "dry-run says what it would do")
            check(_git(port / "snesrecomp", "rev-parse", "HEAD") == old,
                  "and moves nothing")

            res = gitops.advance_submodule_pins(port, paths=["snesrecomp"])
            check(all(r.ok for r in res), f"advance succeeds ({res[0].message})")
            check(_git(port / "snesrecomp", "rev-parse", "HEAD") == new,
                  "the checkout is at the tracked branch tip")
            check(old[:9] in res[0].message and new[:9] in res[0].message,
                  "and the move is reported as from → to, not just 'done'")
            staged = _git(port, "diff", "--cached", "--name-only")
            check("snesrecomp" in staged,
                  "the new gitlink is staged, so the superproject records the move")
            check(_git(port, "log", "--oneline", "-1", "--format=%s") == "init",
                  "but nothing is committed — the pin change stays reviewable")

            after = {c.id: c for c in snesops.audit_project(port).checks}
            check(after["regen_framework"].status.value == "pass",
                  "and the audit that flagged the skew now passes")

            again = gitops.advance_submodule_pins(port, paths=["snesrecomp"])
            check(all(r.ok for r in again) and "already at" in again[0].message,
                  "a second run is a no-op that says so")

            gone = gitops.advance_submodule_pins(port, paths=["recomp-ui"])
            check(not gone[0].ok and "Ensure submodules first" in gone[0].message,
                  "a module that is not checked out names the op that fixes it")

            # Nested: the PSX shape, where what a port pins lives inside the
            # framework rather than beside it. Same op, one level down.
            fw_dir = port / "snesrecomp"
            nested = gitops.advance_submodule_pins(
                port, paths=["lib/recomp-net"], nested=True)
            check(all(r.ok for r in nested), f"nested advance succeeds ({nested[0].message})")
            check(_git(fw_dir / "lib" / "recomp-net", "rev-parse", "HEAD") == lib_new,
                  "the nested checkout moves to its tracked tip")
            check("staged in snesrecomp" in nested[0].message,
                  "and the message says which repo the gitlink was staged in")
            check("lib/recomp-net" in _git(fw_dir, "diff", "--cached", "--name-only"),
                  "the gitlink is staged inside the framework, not the game repo")
            check("lib/recomp-net" not in _git(port, "diff", "--cached", "--name-only"),
                  "the game repo records nothing until the framework is committed")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev

def test_uncloned_submodules() -> None:
    """A clone without --recurse-submodules, and the one op that heals it.

    This is the state every peer's repo lands in: `git clone` writes the
    .gitmodules entry and an empty directory, `git submodule status` prefixes
    the module with '-', and every --modules op reports "checkout missing"
    against a repo whose configuration is perfectly correct. The two answers
    that used to disagree about that same repo were `git status` ("[OK]
    snesrecomp") and `git switch --modules` ("checkout missing"), with Ensure
    submodules claiming "already present" and changing nothing.
    """
    print("uncloned submodule checkouts")
    from project_studio import gitops

    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    saved = {k: os.environ.get(k) for k in _FILE_PROTOCOL_ENV}
    os.environ.update(_FILE_PROTOCOL_ENV)
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    try:
        platforms.set_current("snes")
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            origin, _old, _new, _lib = _stale_fork(base)
            # What a peer does: clone the port, no --recurse-submodules.
            clone = base / "clone"
            subprocess.run(
                ["git", "clone", "-q", str(origin), str(clone)],
                env=env, capture_output=True, text=True,
            )
            sub = clone / "snesrecomp"
            check(sub.is_dir() and not any(sub.iterdir()),
                  "the clone leaves snesrecomp/ as an empty placeholder")
            check((clone / ".gitmodules").is_file(),
                  "while .gitmodules records it perfectly well")

            # Status must not call that OK, and must not borrow the
            # superproject's branch for it (git walks up out of an empty dir).
            st = gitops.repo_status(clone)
            rows = {x.path: x for x in st.submodules}
            check(rows["snesrecomp"].present and not rows["snesrecomp"].initialized,
                  "status reports it present but UNINITIALISED, not OK")
            check(rows["snesrecomp"].checkout_branch == "",
                  "and claims no checkout branch — the parent's is not its own")
            check(any("never cloned" in n for n in st.notes),
                  f"status names the state in a note ({st.notes})")

            # The diagnosis every --modules op gives now carries the cure.
            sw = gitops.switch_modules(
                clone, paths=["snesrecomp"], branch_by_path={"snesrecomp": "main"})
            check(not sw[0].ok, "switch still refuses to touch what is not there")
            check("Ensure submodules first" in sw[0].message,
                  f"and names the op that fixes it ({sw[0].message})")
            check("--recurse-submodules" in sw[0].message,
                  "and says why the directory is empty")

            # Ensure submodules used to stop at .gitmodules. It finishes now.
            res = gitops.ensure_known_submodules(clone)
            fw_row = next(r for r in res if "snesrecomp" in r.message)
            check(fw_row.ok, f"ensure-submodules succeeds ({fw_row.message})")
            check(gitops._is_repo_root(sub),
                  "snesrecomp is a real checkout afterwards, not a placeholder")
            check("not cloned" in fw_row.message,
                  "and says it cloned rather than claiming 'already present'")

            # --recursive, so the nested module inside the framework came too.
            check(gitops._is_repo_root(sub / "lib" / "recomp-net"),
                  "the nested module inside it is cloned as well")

            # And the op that failed a moment ago now works.
            again = gitops.switch_modules(
                clone, paths=["snesrecomp"], branch_by_path={"snesrecomp": "main"})
            check(again[0].ok, f"switch --modules now succeeds ({again[0].message})")

            # The symptom-named cure, on a second port, standing alone.
            clone2 = base / "clone2"
            subprocess.run(
                ["git", "clone", "-q", str(origin), str(clone2)],
                env=env, capture_output=True, text=True,
            )
            dry = gitops.init_module_checkouts(clone2, dry_run=True)
            check(any("would clone" in r.message for r in dry),
                  "init-modules dry-run says what it would do")
            check(not gitops._is_repo_root(clone2 / "snesrecomp"),
                  "and clones nothing")
            init = gitops.init_module_checkouts(clone2)
            check(all(r.ok for r in init), f"init-modules succeeds ({init[0].message})")
            check(gitops._is_repo_root(clone2 / "snesrecomp"),
                  "the framework checkout exists after it")
            twice = gitops.init_module_checkouts(clone2)
            check(all(r.ok for r in twice)
                  and any("already checked out" in r.message for r in twice),
                  "a second run is a no-op that says so")

            # A stray non-repo directory in the way is a different failure and
            # must not be silently deleted to make the clone succeed.
            clone3 = base / "clone3"
            subprocess.run(
                ["git", "clone", "-q", str(origin), str(clone3)],
                env=env, capture_output=True, text=True,
            )
            (clone3 / "snesrecomp" / "notes.txt").write_text("mine\n", encoding="utf-8")
            blocked = gitops.init_module_checkouts(clone3, paths=["snesrecomp"])
            check(not blocked[0].ok, "a non-empty non-repo directory is refused")
            check("move it aside" in blocked[0].message,
                  f"and says what to do about it ({blocked[0].message})")
            check((clone3 / "snesrecomp" / "notes.txt").is_file(),
                  "and the file that was in the way is still there")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev


def test_game_id_is_read_not_derived() -> None:
    """rom_identity.txt's game_id, and why Studio must never invent one.

    The runtime matches a mod package's `[[target]] game_id` against the id
    compiled in from rom_identity.txt with == (snesrecomp
    runner/src/mod_runtime.cpp, target_matches). The framework derives that id
    once, in setup_project.sh, from a project name Studio never sees —
    `safe_slug(NAME).lower()` + region — and records the answer because it has
    to stay stable across revisions. Re-deriving it from the ROM reproduces
    that string only by luck, and a near-miss is the worst outcome available:
    the build succeeds and every mod the port ships stops applying, with the
    player told only "This feature does not support the selected stock ROM."
    """
    print("game_id is read, not derived")
    from project_studio import snesops
    from project_studio.models import MigrateOptions

    platforms.set_current("snes")

    def manifest(root: Path, pkg: str, gid: str) -> None:
        d = root / "mods" / "preloaded" / "packages" / pkg / "1.0.0"
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.toml").write_text(
            f'format_version = 1\nid = "{pkg}"\n\n[[target]]\n'
            f'game_id = "{gid}"\nrom_sha256 = "{"0" * 64}"\n',
            encoding="utf-8",
        )

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "port"
        root.mkdir()
        opts = MigrateOptions()

        # Nothing records one. Refuse — and say what to pass, not just which
        # template variable failed to expand.
        gid, how = snesops.resolve_game_id(root, opts, {})
        check(gid == "" and how == "", "with nothing to read, no id is produced")
        help_text = snesops._token_help(root, ["GAME_ID"])
        check("--game-id" in help_text,
              f"the refusal names the override ({help_text[:60]}…)")
        check("matches" in help_text,
              "and says what the value is for, so it is not guessed at")

        # The port's own mod packages name one. That is a recorded commitment
        # in tracked files, and it is what would break.
        manifest(root, "zelda-alttp.enhancement.widescreen", "zelda-alttp-us")
        manifest(root, "zelda-alttp.enhancement.msu1", "zelda-alttp-us")
        gid, how = snesops.resolve_game_id(root, opts, {})
        check(gid == "zelda-alttp-us", f"the id the mods name is used ({gid})")
        check("manifest" in how, f"and its provenance is reported ({how})")

        # The formula would NOT have produced it: safe_slug strips hyphens and
        # the recorded region is "USA", so derivation yields zeldaalttp-usa.
        # This is the whole reason the order is read-first.
        derived = "".join(ch for ch in "Zelda Alttp" if ch.isalnum()).lower() + "-usa"
        check(derived != gid,
              f"deriving would have produced a different id ({derived} != {gid})")

        # rom_identity.txt outranks the manifests: it is the file the build
        # compiles in and regen.sh reads.
        gid, how = snesops.resolve_game_id(root, opts, {"game_id": "recorded-us"})
        check(gid == "recorded-us" and "rom_identity" in how,
              f"a recorded id wins over the manifests ({gid}, {how})")

        # And the human override outranks everything.
        gid, how = snesops.resolve_game_id(
            root, MigrateOptions(game_id="told-us"), {"game_id": "recorded-us"})
        check(gid == "told-us" and "--game-id" in how,
              f"--game-id wins over both ({gid}, {how})")

        # Manifests that disagree are not a majority vote.
        manifest(root, "third.pkg", "something-else")
        gid, how = snesops.resolve_game_id(root, opts, {})
        check(gid == "", "manifests that disagree produce no id rather than a guess")
        clash = snesops._token_help(root, ["GAME_ID"])
        check("disagree" in clash and "something-else" in clash and "zelda-alttp-us" in clash,
              "and the message lists the ids in conflict and where they came from")

        # Not asked for, not explained.
        check(snesops._token_help(root, ["ROM_SHA256"]) == "",
              "a missing digest gets no game_id lecture")


def test_configure_preflights_mod_catalog() -> None:
    """Configure refuses on the mod-catalog guard instead of spending a cmake.

    snesrecomp's guard is a FATAL_ERROR, and the audit already grades that
    state as a REQUIRED failure carrying the id of the op that fixes it. What
    used to happen is that Studio ran cmake anyway, the user read the
    framework's 25-line hand-edit recipe, and the translation only arrived
    afterwards from diagnose_configure_failure — after a configure that could
    never have succeeded. The preflight has to reuse the audit's row rather
    than re-derive the rule: the grading weighs four separate conditions and a
    second copy of it in buildops would drift from the Migrate tab's.
    """
    print("configure preflights the mod catalog")
    from project_studio import buildops

    platforms.set_current("snes")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "port"
        (root / "mods" / "preloaded" / "packages" / "a.pkg" / "1.0.0").mkdir(parents=True)
        (root / "mods" / "preloaded" / "packages" / "a.pkg" / "1.0.0"
         / "manifest.toml").write_text(
            'format_version = 1\nid = "a.pkg"\n\n[[target]]\n'
            f'game_id = "a-us"\nrom_sha256 = "{"0" * 64}"\n', encoding="utf-8")

        # No framework checkout: the audit skips the row, so the preflight must
        # not block. A port on an older pin has a load-bearing per-title block.
        (root / "CMakeLists.txt").write_text("project(Zed C)\n", encoding="utf-8")
        check(buildops.preflight_snes_mod_catalog(root) is None,
              "with no framework checkout to judge against, nothing is blocked")

        # A framework that defines the call, and a CMakeLists that never makes
        # it — the state that aborts configure.
        fw = root / "snesrecomp" / "runner"
        fw.mkdir(parents=True)
        (fw / "runner.cmake").write_text(
            'set(SNESRECOMP_MOD_CATALOG_DEST "mods/preloaded/packages")\n'
            "function(snesrecomp_target_mod_catalog target preloaded_dir)\n"
            "endfunction()\n", encoding="utf-8")
        (root / "CMakeLists.txt").write_text(
            "project(Zed C)\n"
            'set(SNESRECOMP_ENABLE_MODS ON CACHE BOOL "" FORCE)\n'
            "add_custom_command(TARGET Zed POST_BUILD COMMAND ${CMAKE_COMMAND}\n"
            '  -E copy_directory "${CMAKE_SOURCE_DIR}/mods/preloaded"\n'
            '  "$<TARGET_FILE_DIR:Zed>/mods")\n', encoding="utf-8")
        pre = buildops.preflight_snes_mod_catalog(root)
        check(pre is not None and not pre.ok, "an undeclared catalog is refused")
        check("snes_declare_mod_catalog" in pre.message,
              f"and the refusal names the op that fixes it ({pre.message[:70]}…)")
        check("not started" in pre.message,
              "and says no configure was spent on it")

        # Declared properly: the row passes and the preflight gets out of the way.
        (root / "CMakeLists.txt").write_text(
            "project(Zed C)\n"
            'set(SNESRECOMP_ENABLE_MODS ON CACHE BOOL "" FORCE)\n'
            'snesrecomp_target_mod_catalog(Zed "${CMAKE_SOURCE_DIR}/mods/preloaded")\n',
            encoding="utf-8")
        check(buildops.preflight_snes_mod_catalog(root) is None,
              "a declared catalog blocks nothing")

        # And the post-hoc translation stays, for what a preflight cannot see.
        hint = buildops.diagnose_configure_failure(
            "CMake Error: ... snesrecomp_target_mod_catalog(<target>\n"
            "but no target declared it, so those packages ...", root)
        check(hint is not None and "snes_declare_mod_catalog" in hint,
              "cmake's own guard text still translates to the same op")


# MegaManXSNESRecomp's real shape, reduced: a regional variant chosen
# positionally, each variant's ROM required at a fixed staged path, and the
# framework's internal tools driven directly rather than through its CLI.
_REGEN_SH_PORT_OWNED = """#!/usr/bin/env bash
VARIANT="usa"
for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    --strict-idempotent) STRICT=1 ;;
    -h|--help) exit 0 ;;
    usa|jp|all) VARIANT="$arg" ;;
    *) echo "regen.sh: unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done
"$PYTHON" "$SNESRECOMP_ROOT/tools/v2_emit.py" --rom "$rom" --cfg-dir recomp
"""


def test_port_owned_regen_script() -> None:
    """A tools/regen.sh the PORT wrote: not driveable by flag, not ours to replace.

    regen.sh belongs to the project, and Studio had two assumptions about it
    that a hand-written one breaks. It passed ``--rom <path>`` unconditionally,
    so a driver that selects a regional variant positionally answered
    "unknown argument: --rom" and the refusal arrived as a Studio Generate
    failure. And Probe ROM re-emits the identity carriers with force=True,
    which reached regen.sh too — so the only thing between Studio and
    overwriting a 130-line multi-variant driver was an unrelated
    framework-version check that would stop applying the moment that port
    advanced its submodule pin.
    """
    print("port-owned regen.sh")
    from project_studio import buildops, snes_paths, snesops
    from project_studio.models import MigrateOptions

    platforms.set_current("snes")

    # Classification first, because both fixes hang off it. The discriminator
    # cannot be "looks like the current template": an OLDER wizard script also
    # fails that, and re-emitting those is what Emit tools/regen.sh is for.
    check(snes_paths.regen_is_port_authored(_REGEN_SH_PORT_OWNED),
          "an own option vocabulary with none of the wizard's flags reads as port-authored")
    check(not snes_paths.regen_is_port_authored(_REGEN_SH_MODERN),
          "a wizard script is not")
    check(not snes_paths.regen_is_port_authored(_REGEN_SH),
          "and neither is an early wizard script with no parser — it is ours to replace")
    check(not snes_paths.regen_is_port_authored("#!/bin/sh\n-h|--help) exit 0 ;;\n"),
          "--help alone is not a vocabulary")
    opts = snes_paths.regen_options(_REGEN_SH_PORT_OWNED)
    check(opts == {"--no-tests", "--strict-idempotent", "-h", "--help"},
          f"the option surface is read exactly, arrays and prose excluded ({sorted(opts)})")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Port"
        (root / "tools").mkdir(parents=True)
        script = root / "tools" / "regen.sh"
        script.write_text(_REGEN_SH_PORT_OWNED, encoding="utf-8")
        _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))

        # Generate must name the interface mismatch, before running anything.
        r = buildops.preflight_snes_generate(root, rom="/roms/mmx.sfc")
        check(r is not None and not r.ok, "passing --rom to it is refused")
        check("does not accept --rom" in r.message,
              f"and the refusal says which flag ({r.message[:60]}…)")
        check("--no-tests" in r.message and "--strict-idempotent" in r.message,
              "and lists what the script does accept, so it can be driven by hand")

        # Silently dropping the flag would be worse than refusing: generation
        # would run against whatever ROM the script finds, not the one picked.
        check("--help" in r.message or "directly" in r.message,
              "and points at running it directly rather than guessing")

        # With no flags to pass there is nothing to mismatch, so this gate
        # must not fire — the framework checks behind it still run.
        r2 = buildops.preflight_snes_generate(root)
        check(r2 is None or "does not accept" not in r2.message,
              "asking for no flags is not an interface mismatch")

        # And a forced identity refresh leaves the script alone.
        before = script.read_text(encoding="utf-8")
        res = snesops._fill_regen(root, MigrateOptions(force=True), "op")
        check(res.ok, f"the refresh does not fail over it ({res.message[:50]}…)")
        check("left alone" in res.message,
              "it reports declining to touch it rather than staying silent")
        check(script.read_text(encoding="utf-8") == before,
              "and the port's script is byte-identical afterwards")
        check(not res.changed_paths,
              "with nothing claimed as changed")


# A port-owned driver that adds nothing the wizard's script lacks: same $CLI
# calls, same flags, just hand-rolled argument parsing. Adopting the
# framework's is pure cleanup here.
_REGEN_SH_FORKED_PLAIN = """#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    --skip) SKIP=1 ;;
    -h|--help) exit 0 ;;
    *) echo "unknown: $arg" >&2; exit 2 ;;
  esac
done
"$PYTHON" "$CLI" verify-rom --rom "$ROM"
"$PYTHON" "$CLI" generate --rom "$ROM"
"""

# MegaManX's shape, reduced: capability the wizard's single-target script has
# no way to express.
_REGEN_SH_FORKED_RICH = """#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    -h|--help) exit 0 ;;
    usa|jp) VARIANT="$arg" ;;
    *) echo "unknown: $arg" >&2; exit 2 ;;
  esac
done
"$PYTHON" "$SNESRECOMP_ROOT/tools/v2_emit.py" --rom "$rom" --profile-manifest x.json
"""


def test_adopt_framework_regen() -> None:
    """Handing a port-owned tools/regen.sh back to the framework.

    A port carrying its own regen.sh carries a fork of engine tooling: wizard
    fixes never reach it and Studio cannot drive it. The cleanup is worth
    automating — but only where it is cleanup. A hand-written driver may have
    grown capability the wizard's script cannot express, and replacing that one
    reads as a successful tidy-up while deleting the only way to build half the
    project, so the gate is a derived capability diff rather than a guess.
    """
    print("adopt the framework's regen.sh")
    from project_studio import snesops
    from project_studio.models import MigrateOptions

    platforms.set_current("snes")
    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    try:
        # --- 1. A fork that adds nothing: mechanical, and tools/ goes with it.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Plain"
            (root / "tools").mkdir(parents=True)
            (root / "tools" / "regen.sh").write_text(
                _REGEN_SH_FORKED_PLAIN, encoding="utf-8")
            _fake_wizard(root / "snesrecomp", {"regen.sh.in": _REGEN_SH_MODERN})
            _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))
            (root / "rom_identity.txt").write_text(
                "expected_crc32  = deadbeef\nexpected_sha256 = abc123\n",
                encoding="utf-8")

            rep = snesops.regen_adoption_report(root)
            check(rep.state == "port", "the fork is recognised as port-owned")
            check(rep.lost == [], f"and no capability would be lost ({rep.lost})")
            check(any("--skip" in n for n in rep.notes),
                  f"though its own flag is reported as an interface change ({rep.notes})")
            check(rep.blocker == "", f"and the pinned framework can run it ({rep.blocker})")

            dry = snesops._op_adopt_framework_regen(
                root, MigrateOptions(dry_run=True))
            check(dry.ok and "would replace" in dry.message, "dry-run says what it would do")
            check("remove tools/" not in dry.message,
                  "and does not promise to remove tools/ — the adopted script "
                  "lives in it")
            check(_REGEN_SH_FORKED_PLAIN in (root / "tools" / "regen.sh").read_text(
                      encoding="utf-8"),
                  "and changes nothing")

            res = snesops._op_adopt_framework_regen(root, MigrateOptions())
            check(res.ok, f"adoption succeeds ({res.message[:60]}…)")
            check((root / "tools" / "regen.sh").is_file(),
                  "tools/ stays: it is where the wizard emits the adopted script")
            check(snesops.regen_adoption_report(root).state == "framework",
                  "and the port no longer owns it — the framework does")

        # --- 2. Same fork, but the port keeps its own research tooling there.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "WithTools"
            (root / "tools").mkdir(parents=True)
            (root / "tools" / "regen.sh").write_text(
                _REGEN_SH_FORKED_PLAIN, encoding="utf-8")
            (root / "tools" / "eye_scan.py").write_text("# mine\n", encoding="utf-8")
            _fake_wizard(root / "snesrecomp", {"regen.sh.in": _REGEN_SH_MODERN})
            _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))
            (root / "rom_identity.txt").write_text(
                "expected_crc32  = deadbeef\nexpected_sha256 = abc123\n",
                encoding="utf-8")

            res = snesops._op_adopt_framework_regen(root, MigrateOptions())
            check(res.ok, "adoption still succeeds")
            check((root / "tools" / "eye_scan.py").is_file(),
                  "a port's own tool in tools/ survives — the folder is not the unit")
            check("left alone" in res.message,
                  f"and the op says it left it ({res.message[-70:]})")
            after = (root / "tools" / "regen.sh").read_text(encoding="utf-8")
            check(after != _REGEN_SH_FORKED_PLAIN and "$CLI" in after,
                  "while regen.sh was actually replaced with the framework's")

        # --- 3. A fork with real capability: refused, and itemised.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Rich"
            (root / "tools").mkdir(parents=True)
            (root / "tools" / "regen.sh").write_text(
                _REGEN_SH_FORKED_RICH, encoding="utf-8")
            _fake_wizard(root / "snesrecomp", {"regen.sh.in": _REGEN_SH_MODERN})
            _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))
            (root / "rom_identity.txt").write_text(
                "expected_crc32  = deadbeef\nexpected_sha256 = abc123\n",
                encoding="utf-8")

            lost = snesops.regen_adoption_report(root).lost
            check(any("positionally" in x for x in lost),
                  f"the variant selector is named as a loss ({lost})")
            check(any("v2_emit" in x for x in lost),
                  "so is driving a framework tool the wizard's script never calls")
            check(any("--profile-manifest" in x for x in lost),
                  "and so is a generator flag it passes")

            res = snesops._op_adopt_framework_regen(root, MigrateOptions())
            check(not res.ok, "adoption is refused rather than done quietly")
            check("delete capability, not duplication" in res.message,
                  "and says why in those terms")
            check("regen.sh.in" in res.message,
                  "pointing the fix upstream, at the template")
            before = (root / "tools" / "regen.sh").read_text(encoding="utf-8")
            check(before == _REGEN_SH_FORKED_RICH, "the port's script is untouched")

            # Visible in the GUI, but never ticked for you. Withholding the
            # fix op entirely left the GUI showing a defect with no way to act
            # on it; auto-ticking it would apply a known loss unasked.
            report = snesops.audit_project(root)
            row = next(c for c in report.checks if c.id == "regen_ownership")
            check(row.fix_op == "snes_adopt_framework_regen",
                  "the audit does name the op, so the GUI has a route to it")
            step = next(x for x in snesops.build_plan(root, MigrateOptions(), report).steps
                        if x.op_id == "snes_adopt_framework_regen")
            check(step.selected is False,
                  "but the plan leaves it unticked — the loss is opt-in")
            check("would drop" in step.detail and "positionally" in step.detail,
                  "with the loss itemised on the step itself")

            # An explicit --force is the human saying yes.
            forced = snesops._op_adopt_framework_regen(
                root, MigrateOptions(force=True))
            check(forced.ok, f"--force adopts anyway ({forced.message[:50]}…)")
            check("accepted the loss" in forced.message,
                  "and records what was given up")

        # --- 4. A port already on the framework's script is left alone.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Already"
            (root / "tools").mkdir(parents=True)
            (root / "tools" / "regen.sh").write_text(_REGEN_SH_MODERN, encoding="utf-8")
            _fake_wizard(root / "snesrecomp", {"regen.sh.in": _REGEN_SH_MODERN})
            _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))
            check(snesops.regen_adoption_report(root).state == "framework",
                  "a wizard-emitted script is recognised as the framework's")
            res = snesops._op_adopt_framework_regen(root, MigrateOptions())
            check(res.ok and "already" in res.message, "and the op is a no-op")
            row = next(c for c in snesops.audit_project(root).checks
                       if c.id == "regen_ownership")
            check(row.status.value == "pass", "with a passing audit row")
    finally:
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev


def test_regen_advice_matches_the_plan() -> None:
    """Generate's advice may never name a Migrate step the plan does not offer.

    The reported symptom: Generate on SuperMetroidRecomp said to tick
    "snes_adopt_framework_regen" in Migrate, the user went to the Migrate tab,
    and no such step was there. Both statements were mine and both were
    "right" in isolation — the audit withholds the fix op when the pinned
    framework cannot run the replacement, and the refusal named it
    unconditionally. This is the invariant that makes that combination
    impossible, checked across all three adoption states rather than the one
    that happened to be reported.
    """
    print("regen advice matches the plan")
    from project_studio import snesops
    from project_studio.models import MigrateOptions

    platforms.set_current("snes")
    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    try:
        cases = (
            ("mechanical", _REGEN_SH_FORKED_PLAIN, ("build", "generate", "verify-rom")),
            ("lossy", _REGEN_SH_FORKED_RICH, ("build", "generate", "verify-rom")),
            # The Super Metroid shape: the pinned CLI predates the script.
            ("blocked", _REGEN_SH_FORKED_PLAIN, ("build",)),
        )
        for label, script_body, commands in cases:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / label
                (root / "tools").mkdir(parents=True)
                (root / "tools" / "regen.sh").write_text(script_body, encoding="utf-8")
                _fake_wizard(root / "snesrecomp", {"regen.sh.in": _REGEN_SH_MODERN})
                _fake_framework(root / "snesrecomp", commands)
                (root / "rom_identity.txt").write_text(
                    "expected_crc32  = deadbeef\nexpected_sha256 = abc123\n",
                    encoding="utf-8")

                advice = snesops.regen_ownership_guidance(root)
                check(bool(advice), f"{label}: a port-owned script gets advice")

                report = snesops.audit_project(root)
                plan_ops = {st.op_id for st in snesops.build_plan(
                    root, MigrateOptions(), report).steps}
                names_step = snesops.ADOPT_REGEN_TITLE in advice
                check(names_step == ("snes_adopt_framework_regen" in plan_ops),
                      f"{label}: advice names the Migrate step iff the plan has it "
                      f"(names={names_step}, planned="
                      f"{'snes_adopt_framework_regen' in plan_ops})")

                if label == "blocked":
                    check("offers no step" in advice,
                          f"blocked: the advice says so outright ({advice[:60]}…)")
                    check("bash tools/regen.sh" in advice,
                          "and points at the path that does work")
                    check("pin" in advice or "submodule" in advice,
                          "naming the pin as what has to move")
                if label == "lossy":
                    check("UNTICKED" in advice,
                          "lossy: the advice warns the step is not pre-ticked")
                    check("Force" in advice, "and that Force is required")
                if label == "mechanical":
                    check("Apply" in advice, "mechanical: just tick and Apply")
    finally:
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev


def test_only_overrides_cautious_default() -> None:
    """An op named in --only runs, even when the plan would leave it unticked.

    The reported symptom: the user ticked "Hand tools/regen.sh back to the
    framework" in Migrate, pressed Apply, and the op printed NOTHING — no OK,
    no FAIL. --only forces the op into the plan, but build_plan then set
    selected=False for a lossy adoption and apply_plan skipped unselected
    steps in silence. --only IS the tick (the GUI's Apply sends the ticked ops
    as --only), so it has to win; and a skipped-but-requested step must never
    again be droppable without a word.
    """
    print("--only overrides the unticked default")
    from project_studio import snesops
    from project_studio.models import MigrateOptions, Plan, PlanStep

    platforms.set_current("snes")
    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Lossy"
            (root / "tools").mkdir(parents=True)
            (root / "tools" / "regen.sh").write_text(
                _REGEN_SH_FORKED_RICH, encoding="utf-8")
            _fake_wizard(root / "snesrecomp", {"regen.sh.in": _REGEN_SH_MODERN})
            _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))
            (root / "rom_identity.txt").write_text(
                "expected_crc32  = deadbeef\nexpected_sha256 = abc123\n",
                encoding="utf-8")

            # Reviewing the plan: still unticked, so Apply-everything is safe.
            plain = snesops.build_plan(root, MigrateOptions())
            step = next(x for x in plain.steps
                        if x.op_id == "snes_adopt_framework_regen")
            check(step.selected is False, "unticked when nobody asked for it")

            # Asking for it by name: ticked, and it actually runs.
            asked = MigrateOptions(only=["snes_adopt_framework_regen"])
            plan = snesops.build_plan(root, asked)
            step = next(x for x in plan.steps
                        if x.op_id == "snes_adopt_framework_regen")
            check(step.selected is True, "--only ticks it — that is the user saying yes")
            results = snesops.apply_plan(plan)
            ids = [r.op_id for r in results]
            check("snes_adopt_framework_regen" in ids,
                  f"and it produces a result line rather than silence ({ids})")
            res = next(r for r in results
                       if r.op_id == "snes_adopt_framework_regen")
            check(not res.ok and "delete capability" in res.message,
                  "still refusing without --force, but out loud")

            # The backstop: an unselected step that WAS requested is reported.
            forced_plan = Plan(
                root=str(root), layout=plain.layout,
                steps=[PlanStep(op_id="snes_adopt_framework_regen", title="t",
                                selected=False)],
                options=MigrateOptions(only=["snes_adopt_framework_regen"]))
            out = snesops.apply_plan(forced_plan)
            check(len(out) == 1 and not out[0].ok,
                  "a requested-but-unselected step is reported, never swallowed")
            check("Studio bug" in out[0].message,
                  "and is labelled as the bug it would be")
    finally:
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev


def test_game_id_derived_only_without_manifests() -> None:
    """With no mod package in the repo, game_id is derived rather than refused.

    A correction to my own earlier gate. Refusing to derive protects a port's
    mods from being orphaned by an id that matches no [[target]] — real, and
    why a recorded id always wins. But with NO manifest in the repo there is
    nothing to orphan, and refusing blocked rom_identity.txt, which the current
    framework *requires* as its identity carrier. Blocking a build over a field
    nothing reads is the worse failure.
    """
    print("game_id derived only when nothing can be orphaned")
    from project_studio import snesops
    from project_studio.models import MigrateOptions

    platforms.set_current("snes")
    ident = {"region": "JPN", "display_name": "Super Metroid"}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Port"
        root.mkdir()
        gid, how = snesops.resolve_game_id(root, MigrateOptions(), ident)
        check(gid == "supermetroid-jpn", f"derived as <slug>-<region> ({gid})")
        check("scaffolder's own rule" in how,
              "and says it was derived, not read")
        check("orphaned" in how,
              "stating why deriving is safe here rather than just doing it")

        # A manifest changes the answer back: recorded wins, always.
        d = root / "mods" / "preloaded" / "packages" / "p.kg" / "1.0.0"
        d.mkdir(parents=True)
        (d / "manifest.toml").write_text(
            'format_version = 1\nid = "p.kg"\n\n[[target]]\n'
            f'game_id = "chosen-by-hand"\nrom_sha256 = "{"0" * 64}"\n',
            encoding="utf-8")
        gid, how = snesops.resolve_game_id(root, MigrateOptions(), ident)
        check(gid == "chosen-by-hand",
              f"a manifest's id beats the formula ({gid})")
        check("manifest" in how, "and is reported as read")

        # Disagreeing manifests still refuse — deriving would break one.
        d2 = root / "mods" / "preloaded" / "packages" / "q.kg" / "1.0.0"
        d2.mkdir(parents=True)
        (d2 / "manifest.toml").write_text(
            'format_version = 1\nid = "q.kg"\n\n[[target]]\n'
            f'game_id = "something-else"\nrom_sha256 = "{"0" * 64}"\n',
            encoding="utf-8")
        gid, _how = snesops.resolve_game_id(root, MigrateOptions(), ident)
        check(gid == "", "manifests in conflict are still not resolved by formula")


def test_git_errors_are_not_swallowed() -> None:
    """snesops._git must report git's own reason, which git writes to stderr.

    The reported symptom was a migrate step that said exactly
    "snes_ensure_nested_modules: git failed: " — nothing after the colon. git
    had said "fatal: No url found for submodule path 'lib/retcomm-rbengine' in
    .gitmodules", on stderr, and the helper returned only stdout. A gate that
    discards the one sentence explaining itself leaves nothing to act on.
    """
    print("git errors carry git's reason")
    from project_studio import snesops

    platforms.set_current("snes")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        code, out = snesops._git(root, "rev-parse", "--is-inside-work-tree")
        check(code != 0, "a failing git command reports a non-zero code")
        check(out.strip() != "",
              f"and a non-empty reason rather than silence ({out!r})")

        git(root, "init", "-q", "-b", "main")
        code, out = snesops._git(root, "rev-parse", "--is-inside-work-tree")
        check(code == 0 and out == "true", "success still returns stdout")

        # The exact shape from the log: a real gitlink in the index whose
        # .gitmodules section has a branch but no url. A plain directory does
        # not reproduce it — git rejects the pathspec before ever reading
        # .gitmodules, which is a different error and would let a regression
        # through.
        (root / ".gitmodules").write_text(
            '[submodule "sub"]\n\tpath = sub\n\tbranch = main\n', encoding="utf-8")
        git(root, "update-index", "--add", "--cacheinfo",
            f"160000,{'0' * 39}1,sub")
        code, out = snesops._git(root, "submodule", "update", "--init", "sub")
        check(code != 0, "the real failure still fails")
        check("url" in out.lower(),
              f"and names the missing url rather than nothing ({out[:70]}…)")


def test_build_failure_diagnosis() -> None:
    """A header the found package did not provide, explained from the cache.

    Configure succeeds, links SDL3::SDL3, prints "SDL3 desktop backend" — and
    every file then fails on 'SDL3/SDL.h' file not found. The toolchain pack's
    clang searches only its own sysroot, never /usr/include, while CMake omits
    an include dir it thinks is implicit. So a tree whose SDL3_DIR resolved to
    the host's /usr/lib/cmake/SDL3 configures cleanly and compiles nothing.
    """
    print("build failure diagnosis")
    from project_studio import buildops

    platforms.set_current("snes")
    err = "fatal error: 'SDL3/SDL.h' file not found"
    with tempfile.TemporaryDirectory() as td:
        bdir = Path(td) / "build-release"
        bdir.mkdir()
        pack = buildops.toolchain_root()

        hint = buildops.diagnose_build_failure(err, bdir)
        check(hint is not None, "the missing header is recognised")
        check("outside the compiler's sysroot" in hint,
              "and the mechanism is named, not just the symptom")

        if pack is not None:
            # A poisoned cache: the entry must be quoted back with its value.
            want = buildops.toolchain_env().get("SDL3_DIR", "")
            if want:
                (bdir / "CMakeCache.txt").write_text(
                    "SDL3_DIR:PATH=/usr/lib/cmake/SDL3\n", encoding="utf-8")
                hint = buildops.diagnose_build_failure(err, bdir)
                check("/usr/lib/cmake/SDL3" in hint,
                      f"the stale entry is quoted back ({hint[:70]}…)")
                check(want in hint, "alongside where the pack ships its own")
                check("Configure again" in hint, "and the cure is named")
        else:
            check("cannot re-point" in hint,
                  "without a pack, it says so instead of inventing a path")

        check(buildops.diagnose_build_failure("undefined reference to `foo'", bdir)
              is None,
              "an unrelated build failure gets no SDL lecture")
        check(buildops.diagnose_build_failure("", bdir) is None,
              "and neither does empty output")


def test_module_urls() -> None:
    """Repointing a module at a fork, without committing it for everyone.

    Three settings answer "which repo is this" and they are not the same one:
    the tracked `.gitmodules` URL, this clone's `.git/config` override, and the
    checkout's own `origin`. A contributor working from a fork needs the last
    two moved and the first left alone — committing their fork into the port
    would repoint it for everybody who clones it. The scope is therefore the
    caller's to state, and the default is the one that cannot surprise anyone.
    """
    print("module remote URLs")
    from project_studio import gitops

    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    saved = {k: os.environ.get(k) for k in _FILE_PROTOCOL_ENV}
    os.environ.update(_FILE_PROTOCOL_ENV)
    FORK = "https://github.com/alex/snesrecomp.git"
    try:
        with tempfile.TemporaryDirectory() as td:
            port, _old, _new, _lib = _stale_fork(Path(td))
            rows = {r.path: r for r in gitops.module_urls(port)}
            check("snesrecomp" in rows and "lib/recomp-net" in rows,
                  "both levels are listed — submodules and nested modules")
            check(rows["lib/recomp-net"].nested,
                  "and the nested one is marked as such")
            check(rows["recomp-ui"].present is False,
                  "a module that is not checked out still gets a row to edit")
            tracked_before = _git(port, "show", "HEAD:.gitmodules")

            bad = gitops.set_module_url(port, "snesrecomp", "my fork")
            check(not bad.ok and "does not look like a git remote" in bad.message,
                  "a URL that is not a URL is refused before anything is written")

            scoped = gitops.set_module_url(port, "snesrecomp", FORK, scope="nonsense")
            check(not scoped.ok and "Unknown scope" in scoped.message,
                  "and so is an unknown scope")

            r = gitops.set_module_url(port, "snesrecomp", FORK)
            check(r.ok, f"local scope applies ({r.message})")
            after = {x.path: x for x in gitops.module_urls(port)}
            check(after["snesrecomp"].origin_url == FORK,
                  "origin moves, so push and pull go to the fork")
            check(after["snesrecomp"].local_url == FORK,
                  "the .git/config override moves, so submodule update follows")
            check(after["snesrecomp"].gitmodules_url != FORK,
                  "but .gitmodules is untouched")
            check(_git(port, "diff", "--name-only", "--", ".gitmodules") == "",
                  "and nothing tracked is modified — the fork stays private")
            check(after["snesrecomp"].effective_url == FORK,
                  "the effective URL is what git will actually reach for")

            back = gitops.reset_module_url(port, "snesrecomp")
            check(back.ok, f"reset applies ({back.message})")
            check(gitops.module_urls(port)[0].origin_url != FORK,
                  "and origin goes back to what .gitmodules says")

            shared = gitops.set_module_url(port, "snesrecomp", FORK, scope="gitmodules")
            check(shared.ok and "commit to share it" in shared.message,
                  "the tracked scope says it has to be committed")
            check(_git(port, "diff", "--name-only", "--", ".gitmodules") == ".gitmodules",
                  "because it modifies a tracked file")
            check(_git(port, "show", "HEAD:.gitmodules") == tracked_before,
                  "and still commits nothing itself")

            # Nested modules live in the framework's .gitmodules, not the port's.
            # Init it first: with no checkout only the config override can move,
            # which is a different (also correct) path.
            subprocess.run(["git", "submodule", "update", "--init", "--", "lib/recomp-net"],
                           cwd=str(port / "snesrecomp"), capture_output=True, text=True)
            nested = gitops.set_module_url(
                port, "lib/recomp-net", "https://github.com/alex/recomp-net.git",
                nested=True)
            check(nested.ok, f"a nested module can be repointed too ({nested.message})")
            fw_rows = {x.path: x for x in gitops.module_urls(port)}
            check(fw_rows["lib/recomp-net"].origin_url
                  == "https://github.com/alex/recomp-net.git",
                  "and its origin moves")
            check(fw_rows["lib/recomp-net"].owner.endswith("snesrecomp"),
                  "with the framework named as the repo that owns the setting")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev


def test_region_default() -> None:
    """--region defaults to USA on PSX and to nothing on SNES.

    The parser cannot carry one default for both: "USA" is a PSX habit, and
    sending it at a cartridge relabels a Japanese ROM as North American in the
    README and the packaged zip name.
    """
    print("region default")
    from project_studio import cli

    ap = cli.build_parser()
    args = ap.parse_args(["new-project", "--name", "X", "--disc", "/tmp/x.cue"])
    check(args.region == "", "the parser itself carries no region default")

    def resolved(platform: str) -> str:
        platforms.set_current(platform)
        raw = (getattr(args, "region", None) or "").strip()
        return raw if platforms.current().key == "snes" else (raw or "USA")

    check(resolved("psx") == "USA", "PSX still resolves to USA when unset")
    check(resolved("snes") == "", "SNES resolves to blank — the header decides")
    platforms.set_current("snes")


def test_probe_rom_cli() -> None:
    """`probe-rom` must emit JSON on stdout, since the GUI parses it."""
    print("probe-rom CLI")
    import json as _json

    from project_studio import snes_paths

    probe = snes_paths.probe_rom_script(None)
    if probe is None or not probe.is_file():
        print("  skip  no probe_rom.py available")
        return
    with tempfile.TemporaryDirectory() as td:
        rom = Path(td) / "Probe Me (USA).sfc"
        raw = bytearray(b"\x00" * 0x8000)
        raw[0x7FC0:0x7FC0 + 21] = b"PROBE ME".ljust(21, b" ")
        raw[0x7FD9] = 0x01
        rom.write_bytes(bytes(raw))
        proc = subprocess.run(
            [sys.executable, "-m", "project_studio", "--platform", "snes",
             "probe-rom", "--rom", str(rom)],
            cwd=str(_REPO / "tools" / "new_project_layout"),
            capture_output=True, text=True,
        )
        check(proc.returncode == 0, f"probe-rom exits 0 ({proc.stderr.strip()[:80]})")
        if proc.returncode != 0:
            return
        data = _json.loads(proc.stdout)
        check(data.get("display_name") == "Probe Me", "display_name comes from the filename")
        check(data.get("region") == "USA", "region decoded from the header byte")
        check(data.get("zip_prefix") == "probe-me", "zip_prefix slugged for packaging")
        check(bool(data.get("project_name")), "project_name offered for the GitHub repo field")


def test_dispatch_inputs() -> None:
    """`gh workflow run` rejects an --f the workflow does not declare.

    psxrecomp's release.yml takes four dispatch inputs. snesrecomp's used to
    take none and release off a tag; since the 2026-09 re-vendor it declares
    its own set (publish_release, version, bump, embed_toolchain,
    toolchain_tag) -- overlapping PSX's on version/bump, and spelling the
    publish switch differently. Sending PSX's names at a SNES repo would fail
    the whole dispatch, so the flags are filtered to what the file declares;
    this pins what each file declares so a drift shows up here first.
    """
    print("release dispatch inputs")
    from project_studio import snes_paths
    from project_studio.gitops import declared_dispatch_inputs

    toolkit = _REPO / "tools" / "new_project_layout"
    psx_wf = toolkit / "ci_templates" / "setup-release.yml"
    snes_tdir = snes_paths.templates_dir(None)
    snes_wf = snes_tdir / "release.yml.in" if snes_tdir is not None else None
    if psx_wf.is_file():
        psx_inputs = declared_dispatch_inputs(psx_wf)
        check("version" in psx_inputs and "bump" in psx_inputs,
              f"PSX workflow declares version/bump ({sorted(psx_inputs)})")
        check("publish" in psx_inputs, "PSX workflow declares publish")
    if snes_wf is not None and snes_wf.is_file():
        snes_inputs = declared_dispatch_inputs(snes_wf)
        check("version" in snes_inputs and "bump" in snes_inputs,
              f"SNES workflow declares version/bump ({sorted(snes_inputs)})")
        check("publish_release" in snes_inputs and "publish" not in snes_inputs,
              "SNES workflow spells its publish switch publish_release")


def test_rom_discovery() -> None:
    """A ROM that lives outside the repo is still findable — by digest.

    The wizard bakes the dump's filename into tools/regen.sh and its directory
    nowhere, so a port scaffolded from ~/roms indexes with no image at all and
    both the Migrate field and "Regenerate C from ROM" come up empty. Recovery
    is allowed to search a known ROM folder, but only to *accept* a file whose
    CRC32 is the one this port was pinned to.
    """
    print("rom discovery")
    import zlib

    from project_studio import repo_index

    platforms.set_current("snes")
    # A tree of its own: the shared fixture has a probe ROM parked in it, and
    # the point here is the repo that contains no ROM at all.
    with tempfile.TemporaryDirectory() as lib_s, tempfile.TemporaryDirectory() as repo_s:
        lib = Path(lib_s)
        root = Path(repo_s)
        blob = bytes(range(256)) * 64
        crc = f"{zlib.crc32(blob) & 0xFFFFFFFF:08x}"
        (lib / "Zed (Japan).sfc").write_bytes(blob)
        (lib / "Wrong Game (USA).sfc").write_bytes(b"\x00" * 4096)

        regen = root / "tools" / "regen.sh"
        regen.parent.mkdir(parents=True, exist_ok=True)
        regen.write_text(
            "#!/usr/bin/env bash\n"
            f'EXPECTED_CRC32="${{SNESRECOMP_EXPECTED_CRC32:-{crc}}}"\n'
            '  for cand in "Zed (Japan).sfc" "zed.smc"; do\n'
            "    if [ -f \"$cand\" ]; then ROM=\"$cand\"; break; fi\n"
            "  done\n",
            encoding="utf-8",
        )

        check(
            repo_index.regen_rom_names(root) == ["Zed (Japan).sfc", "zed.smc"],
            "the filenames regen.sh accepts are recovered in its own order",
        )
        check(repo_index.regen_expected_crc32(root) == crc, "the pinned CRC32 is recovered")
        check(
            repo_index.discover_rom(root) == "",
            "no ROM in the tree and no library dir finds nothing",
        )
        check(
            repo_index.discover_rom(root, [lib]) == str(lib / "Zed (Japan).sfc"),
            "a named ROM in a known folder is found",
        )
        check(
            repo_index.discover_image(root, [lib]) == str(lib / "Zed (Japan).sfc"),
            "discover_image routes SNES to the ROM probe",
        )

        # Same name, wrong dump: the digest is what decides, not the filename.
        (lib / "Zed (Japan).sfc").write_bytes(b"\xff" * 4096)
        check(
            repo_index.discover_rom(root, [lib]) == "",
            "a name match whose CRC32 disagrees is refused, not returned",
        )

        # No pinned digest at all — nothing can prove a match, so nothing is claimed.
        regen.write_text('  for cand in "Zed (Japan).sfc"; do\n  done\n', encoding="utf-8")
        check(
            repo_index.discover_rom(root, [lib]) == "",
            "with no digest to check against, a library hit is not guessed at",
        )


def test_module_targets() -> None:
    """A --modules op covers THIS platform's framework, not always psxrecomp.

    The failure was silent in the worst direction: under --platform snes every
    module op reported "psxrecomp: checkout missing" and never touched
    snesrecomp, so a framework commit was never committed, pulled, or pushed —
    and CI then met a submodule pin the remote had never seen.
    """
    print("module targets")
    from project_studio import gitops

    platforms.set_current("snes")
    snes = gitops.default_module_paths()
    check("snesrecomp" in snes, f"a SNES session targets snesrecomp ({snes})")
    check("psxrecomp" not in snes, "and never psxrecomp")
    check("recomp-ui" in snes, "recomp-ui is shared by both consoles")

    platforms.set_current("psx")
    psx = gitops.default_module_paths()
    check("psxrecomp" in psx, f"a PSX session is unchanged ({psx})")
    check("snesrecomp" not in psx, "and never snesrecomp")

    # Nested modules live inside whichever framework is checked out, so their
    # paths are the same on both consoles.
    platforms.set_current("snes")
    nested = gitops.default_module_paths(nested=True)
    check(
        nested == ("lib/recomp-net", "lib/retcomm-rbengine"),
        f"nested module paths are console-independent ({nested})",
    )
    check(
        not hasattr(gitops, "KNOWN_SUBMODULES"),
        "the PSX-shaped constant is gone, so the bug cannot be re-imported",
    )


def test_launch_picks_product_binary() -> None:
    """Launch runs the game, not a ctest binary that shares its build dir.

    A full ``cmake --build`` leaves the project's test executables next to the
    product.  With every root executable scoring the same, an alphabetical
    tie-break launched ``ppu_window_test`` for SuperMetroidRecomp, which exits
    0 instantly and reads as "the game doesn't even launch".
    """
    print("launch picks the product binary")
    from project_studio import buildops

    platforms.set_current("snes")
    with tempfile.TemporaryDirectory() as td:
        bdir = Path(td) / "build-release"
        bdir.mkdir()
        for name in ("ppu_window_test", "sm_render_capture", "sm_video_test",
                     "SuperMetroidSNESRecomp"):
            f = bdir / name
            f.write_bytes(b"#!/bin/sh\n")
            f.chmod(0o755)
        got = buildops.find_runtime_exe(bdir, preferred="SuperMetroidSNESRecomp")
        check(got is not None and got.name == "SuperMetroidSNESRecomp",
              f"the CMake target's own executable wins outright ({got})")
        got = buildops.find_runtime_exe(bdir)
        check(got is not None and got.name == "SuperMetroidSNESRecomp",
              f"without a target hint, test binaries still lose to *Recomp ({got})")
        # A project whose product name carries no hint at all: the test
        # binaries are still demoted below it.
        (bdir / "SuperMetroidSNESRecomp").rename(bdir / "zed")
        got = buildops.find_runtime_exe(bdir)
        check(got is not None and got.name == "zed",
              f"an unhinted product still beats test/capture binaries ({got})")


def test_launch_rom() -> None:
    """The recorded ROM reaches the runner's argv.

    A SNES runner takes the ROM as a positional and exits 1 with its usage
    without one, so a Launch that forgets to pass it looks exactly like a
    broken build. The path is already known — Migrate wrote it to the index —
    which is why nothing should have to restate it at launch time.
    """
    print("launch ROM")
    from project_studio import buildops, repo_index

    platforms.set_current("snes")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "ZedSNESRecomp"
        root.mkdir()
        rom = Path(td) / "zed.sfc"
        rom.write_bytes(b"\x00" * 64)
        idx = repo_index.RepoIndex(
            repos=[repo_index.RepoEntry(path=str(root), name="Zed", cue=str(rom))],
            path=Path(td) / "index.json",
        )
        real = repo_index.load_index
        repo_index.load_index = lambda path=None: idx  # type: ignore[assignment]
        try:
            got, whence = buildops.launch_rom_for(root)
            check(got == str(rom), f"the indexed ROM is what a launch runs ({got})")
            check(whence == "the repo index", "and the log says where it came from")

            got, whence = buildops.launch_rom_for(root, "/elsewhere/other.sfc")
            check(got == "/elsewhere/other.sfc", "an explicit --rom wins over the index")
            check(whence == "explicit", "an explicit ROM is not announced as recovered")

            idx.repos[0].cue = ""
            check(
                buildops.launch_rom_for(root) == ("", ""),
                "a repo with no recorded ROM adds no positional",
            )

            idx.repos[0].cue = str(rom)
            platforms.set_current("psx")
            check(
                buildops.launch_rom_for(root) == ("", ""),
                "PSX launches are untouched — a disc is not a runner argument",
            )
        finally:
            repo_index.load_index = real  # type: ignore[assignment]
            platforms.set_current("snes")


def test_new_project_command() -> None:
    print("new-project command")
    from project_studio import newproject as np

    platforms.set_current("snes")
    with tempfile.NamedTemporaryFile(suffix=".sfc", delete=False) as fh:
        fh.write(b"\0" * 1024)
        rom = fh.name
    try:
        opts = np.NewProjectOptions(
            platform="snes",
            name="Zed",
            disc=rom,
            parent_dir=tempfile.gettempdir(),
            players=4,
            enable_rollback=True,
            bios="/nonexistent/bios.bin",
            description="A mech brawler.",
            publisher="Konami",
            year="1995",
            region="",
            dry_run=True,
        )
        errs = np.validate_options(opts)
        check(errs == [], f"a SNES scaffold with a ROM validates ({errs})")
        cmd, env = np.build_command(opts)
        check("--rom" in cmd, "the ROM is passed as --rom, not --disc")
        check("--rollback" in cmd and "--netplay" in cmd, "rollback implies netplay in the argv")
        check("--bios" not in cmd, "the PSX BIOS field never reaches the SNES scaffolder")
        check(env.get("SNESRECOMP_SETUP_YES") == "1", "non-interactive env is set")
        # argv[0] is a RESOLVED shell, not the word "sh": Windows has no sh on
        # PATH, and setup_project.ps1 is only a launcher that finds Git for
        # Windows' bash and runs this same script.
        check(cmd[0] != "sh", "argv[0] is not the bare word sh")
        check("bash" in Path(cmd[0]).name, "argv[0] is the bash the host actually has")
        check("BIOS" in np.snes_ignored_fields(opts), "ignored PSX fields are reported, not dropped")

        # The prompts the wizard grew. Studio always runs it with --yes, which
        # takes every default, so a field that does not become a flag is lost
        # in silence — the failure this pair of checks exists to catch.
        pairs = list(zip(cmd, cmd[1:]))
        check(("--description", "A mech brawler.") in pairs, "description reaches the argv")
        check(("--publisher", "Konami") in pairs, "publisher reaches the argv")
        check(("--year", "1995") in pairs, "year reaches the argv")
        check(
            "--region" not in cmd,
            "a blank region is omitted so the cartridge header decides",
        )
        for label in ("Description", "Publisher", "Year"):
            check(
                label not in np.snes_ignored_fields(opts),
                f"{label} is no longer reported as ignored",
            )

        opts.region = "JPN"
        argv = np.build_command(opts)[0]
        check(
            ("--region", "JPN") in list(zip(argv, argv[1:])),
            "an explicit region overrides the header",
        )

        opts.disc = ""
        check(
            any("ROM" in e for e in np.validate_options(opts)),
            "a missing image is reported in the platform's own words",
        )
    finally:
        os.unlink(rom)


def test_snes_functions() -> None:
    """The Functions tab's data layer: read the manifest, edit only symbols.toml.

    The write path is a line scanner, not a TOML round-trip, because the
    comments in symbols.toml are where the reasons live — "held at emit = false
    because ..." is the most valuable content in the file, and every TOML
    writer reflows it away. So the thing worth testing is that an add followed
    by a remove leaves the file byte-identical.
    """
    print("snes functions")
    from project_studio import snes_analyzeops as sa

    platforms.set_current("snes")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "recomp").mkdir()
        (root / "src" / "gen").mkdir(parents=True)
        original = (
            "# Progressive symbol map.\n"
            "# Held at emit = false on purpose — see the note.\n"
            "\n"
            "[[func]]\n"
            'name = "I_RESET"\n'
            'addr = "8000"\n'
            "bank = 0\n"
            "emit = false\n"
            'note = "Emulation RESET vector"\n'
        )
        syms = root / "recomp" / "symbols.toml"
        syms.write_text(original, encoding="utf-8")
        (root / "src" / "gen" / "program_manifest.json").write_text(
            json.dumps({
                "format_version": 3,
                "roots": [{"pc24": 0x8000, "m": 1, "x": 1}],
                "nodes": {
                    "008000:M1X1": {
                        "key": "008000:M1X1", "min_pc24": 0x8000, "max_pc24": 0x8075,
                        "disposition": "lle_only", "instruction_count": 51,
                        "reasons": ["unproven_callee_exit"],
                        "demands": [{"kind": "direct_call", "resolution": "aot_exact"}],
                    },
                    "00828A:M0X0": {
                        "key": "00828A:M0X0", "min_pc24": 0x828A, "max_pc24": 0x8295,
                        "disposition": "aot_eligible", "instruction_count": 11,
                        "reasons": [], "demands": [],
                    },
                },
            }),
            encoding="utf-8",
        )

        man = sa.read_manifest(root)
        check(man["present"], "the manifest is read")
        check(len(man["nodes"]) == 2, f"both nodes surface ({len(man['nodes'])})")
        check(man["counts"] == {"lle_only": 1, "aot_eligible": 1},
              f"dispositions are counted ({man['counts']})")
        check(len(man["unproven"]) == 1, "the unproven worklist holds the one with reasons")
        named = next(n for n in man["nodes"] if n["pc"] == "008000")
        check(named["name"] == "I_RESET", "a node is joined to its symbols.toml name")
        check(named["emit"] is False, "and to its emit state")
        unnamed = next(n for n in man["nodes"] if n["pc"] == "00828A")
        check(unnamed["emit"] is None, "a node with no symbol reports emit as unset")

        # Promote in place: the entry changes, the comments do not.
        r = sa.set_symbol(root, "8000", emit=True)
        check(r.ok, f"emit can be flipped ({r.message})")
        body = syms.read_text(encoding="utf-8")
        check("emit = true" in body, "the flag is written")
        check("# Held at emit = false on purpose" in body, "comments survive the edit")
        check('note = "Emulation RESET vector"' in body, "so does the note")
        check(body.count("[[func]]") == 1, "no duplicate entry was appended")

        # Bare hex, 0x-prefixed and bank:offset all name the same function.
        for form in ("0x8000", "00:8000", "8000"):
            check(sa._norm_pc(form) == "8000", f"{form} normalises to 8000")

        sa.set_symbol(root, "8000", emit=False)
        r = sa.set_symbol(root, "828A", name="sub_828A", emit=True)
        check(r.ok, "a new function can be added")
        check(len(sa.read_symbols(root)) == 2, "both entries are present")
        r = sa.clear_symbol(root, "828a")
        check(r.ok, "and removed again, case-insensitively")
        check(
            syms.read_text(encoding="utf-8") == original,
            "add + remove leaves the file byte-identical",
        )


def test_github_about_names_the_console() -> None:
    """The About line must name the console the port is actually built on.

    Both migrations call apply_github_about(), and it sent one hardcoded PSX
    string — so migrating a SNES port advertised it as "Made with PSXrecomp, a
    Sony PlayStation game static recompiler ecosystem" on a Super Nintendo
    repository. The README was right the whole time; only the About was wrong,
    which is the field nobody re-reads after it is set once.
    """
    print("github About")
    from project_studio.readme_metrics import apply_github_about

    def about(kind: str) -> str:
        platforms.set_current(kind)
        ok, msg = apply_github_about("TechnicallyComputers", "Example", dry_run=True)
        check(ok, f"{kind}: dry-run builds a command")
        return msg

    snes = about("snes")
    check("SNESrecomp" in snes, "a SNES session says SNESrecomp")
    check("Super Nintendo" in snes, "...and Super Nintendo")
    check("PSXrecomp" not in snes and "PlayStation" not in snes,
          "and never mentions the PlayStation")

    psx = about("psx")
    # Byte-identical to the string shipped before this was made per-console, so
    # re-migrating a PSX port does not rewrite its About.
    check(
        "Made with PSXrecomp, a Sony PlayStation game static recompiler "
        "ecosystem · Part of the R.A.I.D. community" in psx,
        "a PSX session is unchanged, word for word",
    )
    platforms.set_current("snes")


def test_moved_repo_urls() -> None:
    """A .gitmodules URL naming a repo that moved is a dead pointer, not a fork.

    recomp-net and rbengine changed owner. GitHub redirects the old slugs, so
    nothing looks broken — and the stale URL survives every Reset, because
    `git submodule sync` writes .gitmodules straight back over the override.
    Studio therefore knows the old names, says so on the row, and resets to the
    live URL. A fork under any other owner is absent from the map and is left
    alone, which is the whole point of the dialog.
    """
    print("moved repo URLs")
    from project_studio import gitops

    check(
        gitops.github_slug("git@github.com:TechnicallyComputers/recomp-net.git")
        == "technicallycomputers/recomp-net",
        "the ssh spelling of a remote parses to the same slug as https")
    check(
        gitops.moved_url("https://github.com/TechnicallyComputers/recomp-net.git")
        == gitops.DEFAULT_RECOMP_NET_URL,
        "the old recomp-net slug resolves to its new home")
    check(
        gitops.moved_url("https://github.com/mstan/n64lle")
        == "https://github.com/RetroPortingToolKit/n64lle.git",
        "a URL with no .git suffix still matches")
    check(
        gitops.moved_url("https://github.com/somefork/recomp-net.git") == "",
        "somebody else's fork is not mistaken for a repo that moved")
    check(
        gitops.moved_url(gitops.DEFAULT_RECOMP_NET_URL) == "",
        "and the current URL does not report itself as moved")

    old = "https://github.com/TechnicallyComputers/recomp-net.git"
    stale = gitops.ModuleUrl(path="lib/recomp-net", nested=True, gitmodules_url=old)
    check(stale.moved_to == gitops.DEFAULT_RECOMP_NET_URL,
          "a row still using the old URL is flagged as moved")
    fixed = gitops.ModuleUrl(
        path="lib/recomp-net", nested=True, gitmodules_url=old,
        local_url=gitops.DEFAULT_RECOMP_NET_URL,
        origin_url=gitops.DEFAULT_RECOMP_NET_URL)
    check(fixed.moved_to == "",
          "and stops being flagged once the override points at the live repo")
    check(fixed.to_dict().get("moved_to") == "",
          "the flag reaches the dialog over json")


def test_mod_catalog(tmp: Path) -> None:
    """The migration off per-title mod staging, on the shapes that exist.

    Four SNES ports each spelled the staging differently and the framework
    took the job over; the guard in runner.cmake makes the CMake half loud,
    and nothing at all makes the host half loud -- so both halves are checked
    here, along with the two blocks that must survive: an unrelated
    copy_directory, and a port that already migrated.
    """
    print("mod catalog migration")
    from project_studio import snesops

    def port(name: str, cmake: str, main_c: str, packages=("a.mod",),
             framework: bool = True) -> Path:
        root = tmp / name
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "CMakeLists.txt").write_text(cmake, encoding="utf-8")
        (root / "src" / "main.c").write_text(main_c, encoding="utf-8")
        for pkg in packages:
            (root / "mods" / "preloaded" / "packages" / pkg / "1.0.0").mkdir(
                parents=True, exist_ok=True)
        if framework:
            runner = root / "snesrecomp" / "runner"
            runner.mkdir(parents=True, exist_ok=True)
            (runner / "runner.cmake").write_text(
                'set(SNESRECOMP_MOD_CATALOG_DEST "mods/preloaded/packages")\n'
                "function(snesrecomp_target_mod_catalog target dir)\n"
                "endfunction()\n", encoding="utf-8")
        return root

    HOST = ('int boot(void) {\n'
            '  return snes_mod_runtime_initialize_c(\n'
            '      "mods", "zed-us", "ff");\n'
            '}\n')

    # 1. The shape the guard fires on: a copy_directory POST_BUILD block,
    #    inside an if(), fed by a variable, under a comment -- plus a second
    #    copy_directory for shader presets that has nothing to do with mods.
    legacy = port("legacy", '''cmake_minimum_required(VERSION 3.16)
project(Zed C)
set(SNESRECOMP_ENABLE_MODS ON CACHE BOOL "" FORCE)
include(${CMAKE_SOURCE_DIR}/snesrecomp/runner/runner.cmake)
add_executable(ZedSNESRecomp src/main.c)

# Release-owned mod catalog staged next to the exe.
set(ZED_PRELOADED_MODS "${CMAKE_CURRENT_SOURCE_DIR}/mods/preloaded")
if(EXISTS "${ZED_PRELOADED_MODS}/packages")
    add_custom_command(TARGET ZedSNESRecomp POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_directory
            "${ZED_PRELOADED_MODS}"
            "$<TARGET_FILE_DIR:ZedSNESRecomp>/mods")
endif()

set(ZED_SHADERS "${CMAKE_CURRENT_SOURCE_DIR}/assets/shaders")
add_custom_command(TARGET ZedSNESRecomp POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${ZED_SHADERS}" "$<TARGET_FILE_DIR:ZedSNESRecomp>/assets/shaders")
''', HOST)

    audit = {c.id: c for c in snesops.audit_project(legacy).checks}
    row = audit["mod_catalog"]
    check(row.status.value == "fail" and row.fix_op == "snes_declare_mod_catalog",
          "un-declared catalog fails the audit and names the op")
    check("configure aborts" in row.detail,
          "the detail says configure will abort, which is what brought them here")
    check("the Mods page would list nothing" in row.detail,
          "and reports the silent host-root half as well")
    check("snes_declare_mod_catalog" in
          [s.op_id for s in snesops.build_plan(legacy).steps],
          "the plan picks the op up from the failing check")

    res = snesops._op_declare_mod_catalog(legacy, MigrateOptions(dry_run=True))
    check(res.ok and (legacy / "CMakeLists.txt").read_text(
              encoding="utf-8").count("copy_directory") == 2,
          "--dry-run reports without writing")

    res = snesops._op_declare_mod_catalog(legacy, MigrateOptions())
    cml = (legacy / "CMakeLists.txt").read_text(encoding="utf-8")
    check(res.ok and "snesrecomp_target_mod_catalog(ZedSNESRecomp" in cml,
          "declares the catalog on the target the old block named")
    check("ZED_PRELOADED_MODS" not in cml and "if(EXISTS" not in cml,
          "and removes the block, its guard, and the variable feeding it")
    check('"${ZED_SHADERS}"' in cml and cml.count("copy_directory") == 1,
          "the unrelated shader copy_directory survives")
    check('"mods/preloaded"' in (legacy / "src" / "main.c").read_text(
              encoding="utf-8"),
          "the host reads the directory the framework stages into")
    check(snesops._op_declare_mod_catalog(legacy, MigrateOptions()).message
          == "Mod catalog already framework-owned", "re-running is a no-op")
    after = {c.id: c for c in snesops.audit_project(legacy).checks}
    check(after["mod_catalog"].status.value == "pass", "and the audit clears")

    # 2. snesrecomp_target_stage_dir(... mods) -- the other spelling, on a
    #    port with no packages yet. Nothing is mis-staged and the guard has
    #    nothing to fire on, so this is a cleanup rather than a failure, and
    #    the host it already agreed with must not be touched. (With packages
    #    present it is case 1: no declaration means configure aborts.)
    staged = port("staged", '''cmake_minimum_required(VERSION 3.16)
project(Zed C)
set(SNESRECOMP_ENABLE_MODS ON CACHE BOOL "" FORCE)
include(${CMAKE_SOURCE_DIR}/snesrecomp/runner/runner.cmake)
add_executable(ZedSNESRecomp src/main.c)
snesrecomp_target_stage_dir(ZedSNESRecomp ${CMAKE_SOURCE_DIR}/mods mods)
snesrecomp_target_stage_dir(ZedSNESRecomp ${CMAKE_SOURCE_DIR}/translations translations)
''', 'char d[64];\n'
     'int boot(void) {\n'
     '  snesrecomp_exe_dir_path("translations", t, sizeof(t));\n'
     '  snesrecomp_exe_dir_path("mods/preloaded", d, sizeof(d));\n'
     '  return snes_mod_runtime_initialize_c(d, "zed-us", "ff");\n'
     '}\n', packages=())
    row = {c.id: c for c in snesops.audit_project(staged).checks}["mod_catalog"]
    check(row.status.value == "warn",
          "staging that already lands correctly is a cleanup, not a failure")
    before_main = (staged / "src" / "main.c").read_text(encoding="utf-8")
    snesops._op_declare_mod_catalog(staged, MigrateOptions())
    cml = (staged / "CMakeLists.txt").read_text(encoding="utf-8")
    check("snesrecomp_target_mod_catalog(ZedSNESRecomp" in cml
          and "mods mods)" not in cml, "stage_dir(... mods) is replaced")
    check("translations translations)" in cml,
          "the translations stage_dir is left alone")
    check((staged / "src" / "main.c").read_text(encoding="utf-8") == before_main,
          "a host that already agreed with the framework is not rewritten")

    # 3. Declared, but the host still reads the pre-migration root. Silent in
    #    every build and every release zip -- the reason the op has a host half.
    half = port("half", '''cmake_minimum_required(VERSION 3.16)
project(Zed C)
set(SNESRECOMP_ENABLE_MODS ON CACHE BOOL "" FORCE)
include(${CMAKE_SOURCE_DIR}/snesrecomp/runner/runner.cmake)
add_executable(ZedSNESRecomp src/main.c)
snesrecomp_target_mod_catalog(ZedSNESRecomp "${CMAKE_SOURCE_DIR}/mods/preloaded")
''', HOST)
    row = {c.id: c for c in snesops.audit_project(half).checks}["mod_catalog"]
    check(row.status.value == "fail" and "Mods page" in row.detail,
          "a half-migrated port is caught even though it configures cleanly")
    snesops._op_declare_mod_catalog(half, MigrateOptions())
    check('"mods/preloaded"' in (half / "src" / "main.c").read_text(
              encoding="utf-8"), "and the host half alone is fixed")

    # 4. An old framework pin has no such function. Migrating onto it would
    #    turn a working build into a configure error.
    old = port("old", '''cmake_minimum_required(VERSION 3.16)
project(Zed C)
include(${CMAKE_SOURCE_DIR}/snesrecomp/runner/runner.cmake)
add_executable(ZedSNESRecomp src/main.c)
snesrecomp_target_stage_dir(ZedSNESRecomp ${CMAKE_SOURCE_DIR}/mods mods)
''', HOST)
    (old / "snesrecomp" / "runner" / "runner.cmake").write_text(
        "# a pin from before the catalog contract\n", encoding="utf-8")
    row = {c.id: c for c in snesops.audit_project(old).checks}["mod_catalog"]
    check(row.status.value == "skip" and row.fix_op is None,
          "an older framework pin is skipped, not failed")
    res = snesops._op_declare_mod_catalog(old, MigrateOptions())
    check(not res.ok and "update the submodule" in res.message,
          "and the op refuses rather than calling a function that is absent")
    check("stage_dir(ZedSNESRecomp" in (old / "CMakeLists.txt").read_text(
              encoding="utf-8"), "leaving the only staging it has intact")

    # 5. Several executables and nothing saying which ships the catalog. A
    #    guess here declares the catalog on the wrong binary.
    many = port("many", '''cmake_minimum_required(VERSION 3.16)
project(Zed C)
set(SNESRECOMP_ENABLE_MODS ON CACHE BOOL "" FORCE)
include(${CMAKE_SOURCE_DIR}/snesrecomp/runner/runner.cmake)
add_executable(ZedSNESRecomp src/main.c)
add_executable(ZedJPSNESRecomp src/main.c)
''', HOST)
    res = snesops._op_declare_mod_catalog(many, MigrateOptions())
    check(not res.ok and "several executables" in res.message,
          "refuses to guess which of several targets ships the catalog")

    # 6. The framework owns the destination: a renamed layout moves the host
    #    with it, rather than this module carrying a second copy of the path.
    renamed = port("renamed", '''cmake_minimum_required(VERSION 3.16)
project(Zed C)
set(SNESRECOMP_ENABLE_MODS ON CACHE BOOL "" FORCE)
include(${CMAKE_SOURCE_DIR}/snesrecomp/runner/runner.cmake)
add_executable(ZedSNESRecomp src/main.c)
''', HOST)
    (renamed / "snesrecomp" / "runner" / "runner.cmake").write_text(
        'set(SNESRECOMP_MOD_CATALOG_DEST "catalog/packages")\n'
        "function(snesrecomp_target_mod_catalog target dir)\nendfunction()\n",
        encoding="utf-8")
    snesops._op_declare_mod_catalog(renamed, MigrateOptions())
    check('"catalog"' in (renamed / "src" / "main.c").read_text(encoding="utf-8"),
          "the host root follows SNESRECOMP_MOD_CATALOG_DEST, not a copy of it")


def main() -> int:
    if not subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
        print("git not available — skipping")
        return 0
    # Most of this suite renders snesrecomp's own wizard templates. Without a
    # checkout those checks fail one by one, far from the cause; say it once.
    from project_studio import snes_paths
    if snes_paths.wizard_dir(None) is None:
        print(f"error: {snes_paths.MISSING_CHECKOUT}", file=sys.stderr)
        return 1
    test_platform_defaults()
    test_index_separation()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "ZedSNESRecomp"
        make_repo(root)
        test_repo_recognition(root)
        test_audit(root)
        test_parity_checks(root)
        test_netplay_flip(root)
        test_version_stamp(root)
        test_plan_and_apply(root)
        test_digest_recovery(root)
        test_probe_rom(root)
    test_rom_discovery()
    test_identity_layouts()
    test_readme_toggle()
    test_generate_preflight()
    test_check_runner_paths()
    test_regen_framework_skew()
    test_advance_pins()
    test_uncloned_submodules()
    test_game_id_is_read_not_derived()
    test_configure_preflights_mod_catalog()
    test_port_owned_regen_script()
    test_adopt_framework_regen()
    test_regen_advice_matches_the_plan()
    test_only_overrides_cautious_default()
    test_game_id_derived_only_without_manifests()
    test_git_errors_are_not_swallowed()
    test_build_failure_diagnosis()
    test_module_urls()
    test_moved_repo_urls()
    test_region_default()
    test_probe_rom_cli()
    test_dispatch_inputs()
    test_new_project_command()
    test_launch_rom()
    test_launch_picks_product_binary()
    test_module_targets()
    test_snes_functions()
    test_github_about_names_the_console()
    with tempfile.TemporaryDirectory() as td:
        test_mod_catalog(Path(td))
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
