#!/usr/bin/env python3
"""Build and drive n64ref — the Ares N64 reference oracle — for Studio.

The N64 sibling of ``tools/snes_analysis/mesen_oracle.py`` and
``tools/psx_analysis/duckstation_oracle.py``, and deliberately a THIRD kind of
oracle from either. Read "Not the same kind of oracle" below before assuming
anything downstream.

Layout (mirrors the other two so Studio can find any of them)
-------------------------------------------------------------
    <root>/oracle.json   manifest: pinned Ares SHA, patch list, build stamp
    <root>/oracle.log    stdout/stderr of runs started by this tool
    <root>/oracle.pid    pid of a run started by this tool
    <root>/oracle.port   the ephemeral TCP port that run bound

where ``<root>`` is ``<retcomm-data>/oracle/n64ref`` (override with
``RETCOMM_N64_ORACLE_DIR``). There is deliberately no ``<root>/app``: the
binary is built inside the n64lle checkout, not installed here (see below).

Not the same kind of oracle
---------------------------
    PSX   DuckStation, a third-party emulator PATCHED to speak the runtime's
          own TCP debug protocol.
    SNES  Mesen2, a third-party emulator taken UNPATCHED as a pinned release
          and consulted through Lua-written artifacts.
    N64   n64ref, which is FIRST-PARTY. It is n64lle's own frontend around a
          pinned Ares core, it lives in the framework checkout, and it already
          serves the same JSON-over-TCP protocol the runtime does — by design,
          so that "every debug and co-simulation tool works against either
          backend by switching ports" (n64ref/README.md).

So there is nothing to download and nothing to patch into speaking our
protocol; there is a submodule to initialise at a governed SHA and a build to
run. The manifest records ``speaks_runtime_protocol: true``, which is the
opposite of the SNES one and the reason the Gates pane can point the same
coordinators at either side.

What "pinned" means here
------------------------
Not a release asset with a sha256, but ``n64lle/n64ref/ORACLE-PIN.md`` — the
project's own record of which Ares tree is the accuracy reference, governed by
``n64lle/docs/ORACLE-UPGRADE.md``. Changing it is an owner-signed event with a
fixed procedure, never a side effect of a build, so this tool VERIFIES the pin
and refuses on a mismatch rather than building whatever is checked out.

Licence note: Ares is ISC; paraLLEl-RDP, vendored inside it, is MIT. Both are
permissive and both are attributed in n64lle's THIRD-PARTY.md. (This is a
correction of an easy mistake — Ares is not MIT.)

Usage
-----
    python3 n64_oracle.py doctor                 # host prerequisites
    python3 n64_oracle.py setup                  # submodule + build Ares + n64ref
    python3 n64_oracle.py status [--json]
    python3 n64_oracle.py path --binary
    python3 n64_oracle.py start --rom game.z64
    python3 n64_oracle.py stop
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

MANIFEST_KIND = "retcomm-oracle-n64ref"
LISTEN_RE = re.compile(r"n64ref: listening on 127\.0\.0\.1:(\d+)")


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def data_root() -> Path:
    """The Retro shared data root.

    Precedence must match mesen_oracle.py / duckstation_oracle.py data_root()
    and studio_runner.cpp's retcomm_data_dir(); if they disagree, Studio
    reports an oracle somewhere this tool did not install it.
    """
    env = os.environ.get("RETCOMM_DATA_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "retcomm"
    return Path.home() / ".local" / "share" / "retcomm"


def oracle_root() -> Path:
    env = os.environ.get("RETCOMM_N64_ORACLE_DIR")
    if env:
        return Path(env).expanduser()
    return data_root() / "oracle" / "n64ref"


def find_n64lle(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate a live n64lle checkout.

    Same precedence Studio's New Project uses for the scaffolder: an explicit
    path, then $N64LLE_ROOT, then a sibling checkout beside the toolkit. There
    is no vendored fallback here on purpose -- the oracle must be built from
    the SAME framework tree the project links, or the differentials grade a
    different engine than the one under test.
    """
    cands = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    env = os.environ.get("N64LLE_ROOT")
    if env:
        cands.append(Path(env).expanduser())
    here = Path(__file__).resolve()
    workspace = here.parents[3] if len(here.parents) > 3 else here.parent
    cands.append(workspace / "n64lle")
    for c in cands:
        if (c / "n64ref" / "ORACLE-PIN.md").is_file():
            return c.resolve()
    return None


class Layout:
    def __init__(self, root: Optional[Path] = None, n64lle: Optional[Path] = None):
        self.root = root or oracle_root()
        self.manifest = self.root / "oracle.json"
        self.pidfile = self.root / "oracle.pid"
        self.logfile = self.root / "oracle.log"
        self.portfile = self.root / "oracle.port"
        self.n64lle = n64lle

    @property
    def build_dir(self) -> Optional[Path]:
        return self.n64lle / "build-oracle" if self.n64lle else None

    @property
    def binary(self) -> Optional[Path]:
        b = self.build_dir
        if not b:
            return None
        exe = b / "n64ref" / ("n64ref.exe" if os.name == "nt" else "n64ref")
        return exe

    def resolved_binary(self) -> Optional[Path]:
        b = self.binary
        return b if b and b.is_file() else None


# --------------------------------------------------------------------------
# pin
# --------------------------------------------------------------------------

def pinned_sha(n64lle: Path) -> Optional[str]:
    """The Ares SHA ORACLE-PIN.md records. The record is the root of trust."""
    pin = n64lle / "n64ref" / "ORACLE-PIN.md"
    try:
        text = pin.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"\b([0-9a-f]{40})\b", text)
    return m.group(1) if m else None


def submodule_sha(n64lle: Path) -> Optional[str]:
    ares = n64lle / "n64ref" / "third_party" / "ares"
    if not (ares / "ares" / "ares" / "ares.hpp").is_file():
        return None
    try:
        out = subprocess.run(["git", "-C", str(ares), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


_CARGO_FIND_RE = re.compile(r"find_program\s*\(\s*N64LLE_CARGO")


def needs_cargo(n64lle: Path) -> bool:
    """Does configuring this checkout require cargo? Read from its CMake."""
    for rel in ("runtime/CMakeLists.txt", "recompiler/CMakeLists.txt",
                "n64ref/CMakeLists.txt"):
        try:
            if _CARGO_FIND_RE.search((n64lle / rel).read_text(
                    encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


def patch_list(n64lle: Path) -> list:
    d = n64lle / "n64ref" / "patches"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.patch"))


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    n64lle = find_n64lle(args.n64lle)
    ok = True

    if not n64lle:
        log("FAIL  n64lle checkout not found "
            "(pass --n64lle, or set N64LLE_ROOT)")
        return 1
    log(f"ok    n64lle           {n64lle}")

    for tool in ("git", "cmake", "ninja"):
        p = shutil.which(tool)
        log(f"{'ok  ' if p else 'FAIL'}  {tool:<15} {p or 'not found'}")
        ok = ok and bool(p)
    cxx = shutil.which(os.environ.get("CXX", "") or "g++")
    log(f"{'ok  ' if cxx else 'FAIL'}  {'c++':<15} {cxx or 'not found'}")
    ok = ok and bool(cxx)

    # `setup` configures the whole n64lle tree, and that tree's CMake finds
    # cargo with find_program(... REQUIRED) -- n64ref's own Rust drivers did
    # before 2026-09-23, and since the Rust migration the engine and the
    # recompiler tools do too. Asked of the checkout, not assumed.
    if needs_cargo(n64lle):
        cargo = shutil.which("cargo")
        log(f"{'ok  ' if cargo else 'FAIL'}  {'cargo':<15} "
            f"{cargo or 'not found — this n64lle configures with cargo; install rustup'}")
        ok = ok and bool(cargo)

    # paraLLEl-RDP renders through Vulkan. Ares' non-pixel surfaces do not need
    # it, so this is a WARN: an oracle without a Vulkan device still answers
    # regs/memory/command queries, it just cannot grade a pixel.
    vk = shutil.which("vulkaninfo")
    if vk:
        log(f"ok    {'vulkan':<15} {vk}")
    else:
        log(f"warn  {'vulkan':<15} vulkaninfo not found — non-pixel queries "
            "still work; the pixel differentials need a Vulkan device")

    want = pinned_sha(n64lle)
    have = submodule_sha(n64lle)
    log(f"      {'pinned Ares':<15} {want or 'ORACLE-PIN.md unreadable'}")
    if have is None:
        log(f"warn  {'ares submodule':<15} not initialised — `setup` will do it")
    elif want and have != want:
        log(f"FAIL  {'ares submodule':<15} {have} != pin")
        log("      A pin change is an owner-signed event "
            "(n64lle docs/ORACLE-UPGRADE.md §2-4).")
        ok = False
    else:
        log(f"ok    {'ares submodule':<15} {have}")

    pl = patch_list(n64lle)
    log(f"      {'patches':<15} {', '.join(pl) if pl else 'none'}")

    lay = Layout(Path(args.root) if args.root else None, n64lle)
    b = lay.resolved_binary()
    log(f"{'ok  ' if b else 'warn'}  {'n64ref':<15} "
        f"{b or 'not built — run `setup`'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def run(cmd: list, cwd: Optional[Path] = None, timeout: int = 7200) -> int:
    log("+ " + " ".join(str(c) for c in cmd))
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                              timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        log(f"timed out after {timeout}s")
        return 1
    except OSError as exc:
        log(f"failed to run: {exc}")
        return 1


def cmd_setup(args: argparse.Namespace) -> int:
    n64lle = find_n64lle(args.n64lle)
    if not n64lle:
        log("n64lle checkout not found (pass --n64lle, or set N64LLE_ROOT)")
        return 1
    lay = Layout(Path(args.root) if args.root else None, n64lle)
    lay.root.mkdir(parents=True, exist_ok=True)

    want = pinned_sha(n64lle)
    if not want:
        log("cannot read the pinned SHA from n64ref/ORACLE-PIN.md — refusing")
        return 1

    if submodule_sha(n64lle) is None:
        log("ares submodule absent; initialising at the pinned SHA")
        if run(["git", "submodule", "update", "--init",
                "n64ref/third_party/ares"], cwd=n64lle) != 0:
            return 1

    have = submodule_sha(n64lle)
    if have != want:
        log(f"ares HEAD {have} is not the pinned {want} — refusing.")
        log("A pin change is an owner-signed event "
            "(n64lle docs/ORACLE-UPGRADE.md §2-4), never a build side effect.")
        return 1
    log(f"pin verified: {have}")

    # The framework owns its own build recipe; this tool never re-implements
    # the configure flags. build_ares.sh applies the committed patches to a
    # pristine submodule and refuses a pin mismatch itself, so the check above
    # is belt-and-braces rather than the only guard.
    script = n64lle / "tools" / ("build_ares.ps1" if os.name == "nt"
                                 else "build_ares.sh")
    if not script.is_file():
        log(f"missing {script} — this n64lle predates the POSIX oracle build")
        return 1
    cmd = ([str(script)] if os.name != "nt"
           else ["powershell", "-File", str(script)])
    if run(cmd, cwd=n64lle) != 0:
        log("Ares pre-build failed")
        return 1

    if needs_cargo(n64lle) and not shutil.which("cargo"):
        log("this n64lle configures with cargo (find_program REQUIRED) and none "
            "is on PATH — install rustup; the checkout's rust-toolchain.toml "
            "names the toolchain")
        return 1

    bd = lay.build_dir
    if run(["cmake", "-S", str(n64lle), "-B", str(bd), "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release"], cwd=n64lle) != 0:
        return 1
    if run(["cmake", "--build", str(bd), "--target", "n64ref"], cwd=n64lle) != 0:
        return 1

    write_manifest(lay, "install", pin=want, patches=patch_list(n64lle))
    b = lay.resolved_binary()
    log(f"n64ref built: {b}" if b else "build reported success but no binary")
    return 0 if b else 1


# --------------------------------------------------------------------------
# manifest / status
# --------------------------------------------------------------------------

def write_manifest(lay: Layout, stage: str, pin: Optional[str] = None,
                   patches: Optional[list] = None, **extra: Any) -> None:
    doc: Dict[str, Any] = {}
    if lay.manifest.is_file():
        try:
            doc = json.loads(lay.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            doc = {}
    doc.update({
        "kind": MANIFEST_KIND,
        "version": 1,
        "root": str(lay.root),
        "n64lle": str(lay.n64lle) if lay.n64lle else "",
        "upstream_url": "https://github.com/ares-emulator/ares",
        "upstream_tag": "v148",
        # TRUE, unlike the SNES oracle: n64ref serves the same JSON-over-TCP
        # protocol the runtime does, so a coordinator can point at either by
        # switching ports. Anything planning a command-for-command diff keys
        # off this field.
        "speaks_runtime_protocol": True,
        "protocol": "json-over-tcp",
        "provider": "built-from-pinned-submodule",
        "licence": "ISC (ares); paraLLEl-RDP sub-tree MIT — developer tool, "
                   "never linked into a port",
    })
    if pin:
        doc["ares_sha"] = pin
    if patches is not None:
        doc["patches"] = patches
    doc.update(extra)
    doc[f"{stage}_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    lay.root.mkdir(parents=True, exist_ok=True)
    lay.manifest.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


def running_pid(lay: Layout) -> Optional[int]:
    try:
        pid = int(lay.pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def running_port(lay: Layout) -> Optional[int]:
    try:
        return int(lay.portfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def cmd_status(args: argparse.Namespace) -> int:
    n64lle = find_n64lle(args.n64lle)
    lay = Layout(Path(args.root) if args.root else None, n64lle)
    doc = {}
    if lay.manifest.is_file():
        try:
            doc = json.loads(lay.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            doc = {}
    resolved = lay.resolved_binary()
    pid = running_pid(lay)
    want = pinned_sha(n64lle) if n64lle else None
    have = submodule_sha(n64lle) if n64lle else None

    if args.json:
        print(json.dumps({
            "root": str(lay.root),
            "n64lle": str(n64lle) if n64lle else "",
            "installed": resolved is not None,
            "binary": str(resolved) if resolved else "",
            "build_dir": str(lay.build_dir) if lay.build_dir else "",
            "ares_pinned": want or "",
            "ares_present": have or "",
            # The single field the pane should gate "safe to grade with" on.
            "pin_ok": bool(want and have and want == have),
            "patches": patch_list(n64lle) if n64lle else [],
            "running_pid": pid or 0,
            "running_port": running_port(lay) or 0,
            "logfile": str(lay.logfile),
            "manifest": doc,
        }, indent=1))
        return 0

    print(f"root: {lay.root}")
    print(f"  {'n64lle':<24} {n64lle or 'not found'}")
    print(f"  {'ares pinned':<24} {want or '?'}")
    print(f"  {'ares present':<24} {have or 'not initialised'}")
    print(f"  {'pin ok':<24} {bool(want and have and want == have)}")
    print(f"  {'patches':<24} {', '.join(patch_list(n64lle)) if n64lle else ''}")
    print(f"  {'binary':<24} {resolved or 'not built'}")
    print(f"  {'running':<24} {f'{pid} on port {running_port(lay)}' if pid else 'no'}")
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root) if args.root else None,
                 find_n64lle(args.n64lle))
    b = lay.resolved_binary()
    if args.binary:
        print(str(b) if b else "")
        return 0 if b else 1
    print(str(lay.root))
    return 0


# --------------------------------------------------------------------------
# start / stop
# --------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    n64lle = find_n64lle(args.n64lle)
    lay = Layout(Path(args.root) if args.root else None, n64lle)
    b = lay.resolved_binary()
    if not b:
        log("n64ref is not built — run `setup`")
        return 1
    rom = Path(args.rom).expanduser()
    if not rom.is_file():
        log(f"ROM not found: {rom}")
        return 1
    if running_pid(lay):
        log(f"already running (pid {running_pid(lay)}) — `stop` first")
        return 1

    lay.root.mkdir(parents=True, exist_ok=True)
    # Port 0: the OS assigns a free ephemeral port, announced on stderr. Fixed
    # localhost ports are unsafe on this machine -- sibling recompiler projects
    # squat 4380/4370, and one of them once answered an n64lle gate's `status`
    # with its own protocol (n64lle KNOWN-ISSUES KI-3). Never hard-code one.
    logf = open(lay.logfile, "wb")
    try:
        proc = subprocess.Popen([str(b), str(rom), str(args.port)],
                                stdout=logf, stderr=subprocess.STDOUT,
                                cwd=str(n64lle) if n64lle else None,
                                start_new_session=True)
    except OSError as exc:
        log(f"failed to start: {exc}")
        return 1
    lay.pidfile.write_text(str(proc.pid) + "\n", encoding="utf-8")

    # Wait for the announce rather than assuming a port. A boot that dies takes
    # the log with it, so report that instead of a bare timeout.
    port = 0
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            log(f"n64ref exited immediately (code {proc.returncode}); log:")
            log(lay.logfile.read_text(encoding="utf-8", errors="replace")[-2000:])
            lay.pidfile.unlink(missing_ok=True)
            return 1
        try:
            m = LISTEN_RE.search(lay.logfile.read_text(encoding="utf-8",
                                                       errors="replace"))
        except OSError:
            m = None
        if m:
            port = int(m.group(1))
            break
        time.sleep(0.2)
    if not port:
        log(f"n64ref did not announce a port within {args.timeout}s")
        return 1

    lay.portfile.write_text(str(port) + "\n", encoding="utf-8")
    write_manifest(lay, "start", rom=str(rom), port=port)
    log(f"n64ref listening on 127.0.0.1:{port} (pid {proc.pid})")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root) if args.root else None,
                 find_n64lle(args.n64lle))
    pid = running_pid(lay)
    if not pid:
        log("not running")
        lay.pidfile.unlink(missing_ok=True)
        lay.portfile.unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log(f"could not signal {pid}: {exc}")
        return 1
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    lay.pidfile.unlink(missing_ok=True)
    lay.portfile.unlink(missing_ok=True)
    log(f"stopped {pid}")
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="oracle root (default: <retcomm-data>/oracle/n64ref)")
    ap.add_argument("--n64lle", help="n64lle checkout (default: $N64LLE_ROOT or a sibling)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("path")
    p.add_argument("--binary", action="store_true")
    p.set_defaults(fn=cmd_path)

    p = sub.add_parser("start")
    p.add_argument("--rom", required=True)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--timeout", type=float, default=90.0)
    p.set_defaults(fn=cmd_start)

    sub.add_parser("stop").set_defaults(fn=cmd_stop)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
