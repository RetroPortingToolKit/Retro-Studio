#!/usr/bin/env python3
"""Headless check of Bulk Recomp's orchestration.

The stages themselves are the New Project / Build subcommands, which have their
own tests and need a framework checkout. What is Bulk Recomp's own is the
bookkeeping around them, so this swaps the child CLI for a fake and checks:

  * every image runs every stage in order, and a passing one is PASSED
  * a stage that exits non-zero FAILS that project at that stage — and only
    that project
  * two images that probe to one project folder do not both scaffold into it
  * each stage's output lands in Log Output/<NN-image>/<n>-<stage>.log, and a
    summary.txt names each verdict
  * build-tool progress lines move the per-stage progress
  * parallel mode runs projects at the same time
  * .cancel stops a run, including a stage that is silent

Run:  python3 tests/bulk_recomp_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "tools" / "new_project_layout"))

from project_studio import bulkrecomp, platforms  # noqa: E402

failures = 0


def check(cond: bool, what: str) -> None:
    global failures
    print(f"  {'ok  ' if cond else 'FAIL'}  {what}")
    if not cond:
        failures += 1


# A stand-in for `python -m project_studio`. Invoked as
#   fake -m project_studio --platform snes <subcommand> …
# ROM stems steer it: "bad" fails compile, "slow" sleeps silently in generate,
# "twin" probes to the same name as every other "twin".
FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
a = sys.argv[1:]
assert a[:2] == ["-m", "project_studio"], a
a = a[2:]
assert a[0] == "--platform"
a = a[2:]
def opt(name):
    return a[a.index(name) + 1] if name in a else ""
cmd = a[0]
if cmd == "probe-rom":
    stem = Path(opt("--rom")).stem
    name = "Twin Game" if stem.startswith("twin") else stem.title()
    print(json.dumps({"display_name": name, "region": "USA", "zip_prefix": stem}))
    sys.exit(0)
if cmd == "new-project":
    from project_studio import platforms
    from project_studio.newproject import NewProjectOptions, project_root_for
    platforms.set_current("snes")
    assert "--no-index" in a and "--no-ci" in a and "--no-fetch-boxart" in a
    assert "--create-github" not in a
    root = project_root_for(NewProjectOptions(
        name=opt("--name"), disc=opt("--rom"), parent_dir=opt("--dir"), platform="snes"))
    root.mkdir(parents=True)
    (root / "CMakeLists.txt").write_text("# fake\n")
    print("scaffolded", root)
    sys.exit(0)
if cmd == "build":
    sub, root = a[1], opt("--root")
    assert Path(root).is_dir(), root
    stem = Path(opt("--rom")).stem if opt("--rom") else ""
    if sub == "generate" and "slow" in stem:
        time.sleep(60)          # silent: only the watcher can stop this
    if sub == "compile":
        for i in range(1, 5):
            print(f"[{i}/4] Building CXX object x{i}.o", flush=True)
            time.sleep(0.05)
        if "bad" in Path(root).name.lower():
            print("error: fake link failure", flush=True)
            sys.exit(3)
    print(f"[OK] {sub}")
    sys.exit(0)
print("unknown", a, file=sys.stderr)
sys.exit(9)
'''


def make_fake(tmp: Path) -> str:
    fake = tmp / "fake_cli.py"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return str(fake)


def roms(tmp: Path, *stems: str) -> list[str]:
    d = tmp / "roms"
    d.mkdir(exist_ok=True)
    out = []
    for s in stems:
        p = d / f"{s}.sfc"
        p.write_bytes(b"\0" * 512)
        out.append(str(p))
    return out


def status(out: Path) -> dict:
    return json.loads((out / "Log Output" / "bulk_status.json").read_text())


def test_batch(parallel: int) -> None:
    print(f"batch, parallel={parallel}")
    tmp = Path(tempfile.mkdtemp(prefix="bulkrecomp-"))
    try:
        images = roms(tmp, "alpha", "bad", "twin_usa", "twin_jpn", "gamma")
        out = tmp / "out"
        opts = bulkrecomp.BulkRecompOptions(images=images, out_dir=str(out), parallel=parallel)
        check(bulkrecomp.validate(opts) == [], "options validate")
        run = bulkrecomp.BulkRun(opts, python=make_fake(tmp))
        t0 = time.monotonic()
        ok = run.run()
        took = time.monotonic() - t0
        check(not ok, "batch with a failure reports not-ok")
        st = status(out)
        by = {Path(i["image"]).stem: i for i in st["items"]}
        check(st["state"] == "done", "status file says done")
        check(st["stages"] == ["probe", "scaffold", "generate", "configure", "compile"],
              "SNES stage list")
        for stem in ("alpha", "gamma"):
            it = by[stem]
            check(it["state"] == "passed" and it["progress"] == 1.0, f"{stem} PASSED")
            check(all(v == "passed" for v in it["stages"].values()), f"{stem} every stage passed")
        b = by["bad"]
        check(b["state"] == "failed" and b["stage"] == "compile", "bad FAILED at compile")
        check(b["stages"]["configure"] == "passed", "bad got through configure first")
        twins = [by["twin_usa"], by["twin_jpn"]]
        check(sorted(t["state"] for t in twins) == ["failed", "passed"],
              "one twin built, the other refused")
        loser = next(t for t in twins if t["state"] == "failed")
        check("same project folder" in loser["message"], "twin refusal says why")
        check(loser["stage"] == "scaffold", "twin refused AT scaffold, not reported at probe")
        logd = out / "Log Output"
        clog = logd / by["alpha"]["label"] / "5-compile.log"
        check(clog.is_file() and "[4/4]" in clog.read_text(), "compile output in its stage log")
        check("fake link failure" in (logd / b["label"] / "5-compile.log").read_text(),
              "failure text in the failing stage log")
        summ = (logd / "summary.txt").read_text()
        check("3/5 passed" in summ, "summary counts 3/5 (alpha, gamma, one twin)")
        check("FAILED" in summ and "PASSED" in summ, "summary names verdicts")
        if parallel > 1:
            check(took < 5 * 0.25 * 0.9 + 3, f"parallel run overlapped ({took:.1f}s)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reuse_and_exists() -> None:
    print("existing project folder")
    tmp = Path(tempfile.mkdtemp(prefix="bulkrecomp-"))
    try:
        images = roms(tmp, "alpha")
        out = tmp / "out"
        fake = make_fake(tmp)
        bulkrecomp.BulkRun(bulkrecomp.BulkRecompOptions(images, str(out)), python=fake).run()
        run = bulkrecomp.BulkRun(bulkrecomp.BulkRecompOptions(images, str(out)), python=fake)
        check(not run.run(), "second run into the same folder fails without reuse")
        check("already exists" in status(out)["items"][0]["message"], "and says so")
        run = bulkrecomp.BulkRun(
            bulkrecomp.BulkRecompOptions(images, str(out), reuse_existing=True), python=fake)
        check(run.run(), "reuse rebuilds the existing scaffold")
        check(status(out)["items"][0]["stages"]["scaffold"] == "skipped", "scaffold marked skipped")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cancel() -> None:
    print("cancel")
    tmp = Path(tempfile.mkdtemp(prefix="bulkrecomp-"))
    try:
        images = roms(tmp, "slow", "later")
        out = tmp / "out"
        run = bulkrecomp.BulkRun(bulkrecomp.BulkRecompOptions(images, str(out)),
                                 python=make_fake(tmp))
        th = threading.Thread(target=run.run)
        t0 = time.monotonic()
        th.start()
        cancel = out / "Log Output" / ".cancel"
        for _ in range(100):
            time.sleep(0.1)
            try:
                if status(out)["items"][0]["stage"] == "generate":
                    break
            except (OSError, ValueError):
                pass
        cancel.parent.mkdir(parents=True, exist_ok=True)
        cancel.touch()
        th.join(timeout=20)
        check(not th.is_alive(), "run stopped")
        check(time.monotonic() - t0 < 20, "a silent stage was killed, not waited out")
        st = status(out)
        check(st["state"] == "cancelled", "status says cancelled")
        check([i["state"] for i in st["items"]] == ["cancelled", "cancelled"],
              "running and queued projects both cancelled")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_progress_lines() -> None:
    print("build-tool progress")
    lp = bulkrecomp._line_progress
    check(lp("[12/48] Building CXX object a.o") == 0.25, "ninja [n/m]")
    check(lp("[ 50%] Built target x") == 0.5, "make [ n%]")
    check(lp("-- Configuring done") is None, "other lines move nothing")


def test_validate() -> None:
    print("validation")
    tmp = Path(tempfile.mkdtemp(prefix="bulkrecomp-"))
    try:
        (tmp / "x.cue").write_text("")
        errs = bulkrecomp.validate(bulkrecomp.BulkRecompOptions([str(tmp / "x.cue")], ""))
        check(any("output folder" in e for e in errs), "output folder required")
        check(any("not a Super Nintendo image" in e for e in errs), "wrong console's image refused")
        a = roms(tmp, "a")[0]
        errs = bulkrecomp.validate(bulkrecomp.BulkRecompOptions([a, a], str(tmp)))
        check(any("listed twice" in e for e in errs), "duplicate image refused")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    platforms.set_current("snes")
    os.environ["PYTHONPATH"] = str(_REPO / "tools" / "new_project_layout")
    test_validate()
    test_progress_lines()
    test_batch(parallel=1)
    test_batch(parallel=3)
    test_reuse_and_exists()
    test_cancel()
    print("PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
