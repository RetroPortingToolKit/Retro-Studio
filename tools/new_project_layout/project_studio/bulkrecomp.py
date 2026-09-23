"""Bulk Recomp — scaffold, generate and build many game images in one run.

A testing tool, not a way to make ports. It answers "which of these dumps make
it through the pipeline today?" by running, for every image, the SAME CLI
subcommands the New Project and Build tabs run for one:

    psx   probe → scaffold → generate → configure → compile
    snes  probe → scaffold → generate → configure → compile
    n64   probe → scaffold → framework → generate → compile

Each stage is a child ``python -m project_studio --platform <key> …`` rather
than an in-process call. That is the point, not overhead: a bulk run that took
a different path from the buttons would be testing something nobody clicks,
and a stage that wedges or crashes takes one project down instead of the batch.

Output lives under one folder the caller chooses:

    <out>/<ProjectFolder>/                      each scaffolded port
    <out>/Log Output/<NN-image>/<n>-<stage>.log  every stage's full output
    <out>/Log Output/bulk_status.json            live state, read by the GUI
    <out>/Log Output/summary.txt                 PASSED / FAILED at the end
    <out>/Log Output/.cancel                     touch it to stop the run

The stage logs replace the Activity log for this tool: twenty interleaved
builds in one scrolling pane is not something anybody can read.

GitHub repo creation, CI and boxart are OFF by default — every one of them
reaches outside the output folder, and a test batch that created forty private
repos would be a cleanup job, not a test.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import platforms

LOG_DIR_NAME = "Log Output"
STATUS_FILE = "bulk_status.json"
SUMMARY_FILE = "summary.txt"
CANCEL_FILE = ".cancel"

# Ordered stage lists per console. `framework` is N64-only for the reason
# cmd_build_framework gives: n64lle is resolved as a pre-built tree, not
# add_subdirectory()'d, and its generate step configures the port itself.
STAGES: dict[str, tuple[str, ...]] = {
    "psx": ("probe", "scaffold", "generate", "configure", "compile"),
    "snes": ("probe", "scaffold", "generate", "configure", "compile"),
    "n64": ("probe", "scaffold", "framework", "generate", "compile"),
}

# Progress inside a build stage, read from the build tool's own output.
# Ninja prints "[123/456] …", Make prints "[ 45%] …".
_NINJA_RE = re.compile(r"^\[\s*(\d+)\s*/\s*(\d+)\s*\]")
_MAKE_RE = re.compile(r"^\[\s*(\d+)%\]")


@dataclass
class BulkRecompOptions:
    images: list[str]
    out_dir: str
    parallel: int = 1
    build_jobs: int = 0          # 0 = let cmake / the toolchain decide
    build_type: str = "Release"
    create_github: bool = False
    enable_ci: bool = False
    fetch_boxart: bool = False
    reuse_existing: bool = False
    add_to_index: bool = False


@dataclass
class Item:
    index: int
    image: str
    label: str                    # log folder name: NN-<image stem>
    name: str = ""
    root: str = ""
    state: str = "queued"         # queued | running | passed | failed | cancelled
    stage: str = ""
    stage_progress: float = 0.0
    stages: dict[str, str] = field(default_factory=dict)
    message: str = ""
    started: float = 0.0
    finished: float = 0.0
    log: str = ""                 # the stage log currently / last written


def _safe_label(index: int, image: str) -> str:
    stem = Path(image).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "image"
    return f"{index + 1:02d}-{stem[:60]}"


def _strip_tags(stem: str) -> str:
    """'Game (USA) (Rev 1) [!]' → 'Game' — the same rule n64's probe uses."""
    return re.sub(r"\s*\([^)]*\)|\s*\[[^\]]*\]", "", stem).strip()


class BulkRun:
    """One batch. Thread-safe: items are updated from the worker pool."""

    def __init__(self, opts: BulkRecompOptions, *, python: str | None = None,
                 on_event: Callable[[str], None] | None = None) -> None:
        self.opts = opts
        self.platform = platforms.current().key
        self.stages = STAGES[self.platform]
        self.python = python or sys.executable
        self.out = Path(opts.out_dir).expanduser().resolve()
        self.log_dir = self.out / LOG_DIR_NAME
        self.on_event = on_event
        self.items = [
            Item(index=i, image=str(Path(img).expanduser().resolve()),
                 label=_safe_label(i, img),
                 stages={s: "pending" for s in self.stages})
            for i, img in enumerate(opts.images)
        ]
        self._lock = threading.Lock()
        # Held across snapshot AND replace, so a slower writer can never land
        # an older snapshot over a newer one.
        self._write_lock = threading.Lock()
        self._claimed: dict[str, int] = {}   # project root → item index
        self._procs: set[subprocess.Popen] = set()
        self._last_write = 0.0
        self.started = 0.0
        self.finished = 0.0
        self.cancelled = False

    # ---- status file -----------------------------------------------------

    def _status_dict(self) -> dict:
        return {
            "version": 1,
            "platform": self.platform,
            "out_dir": str(self.out),
            "log_dir": str(self.log_dir),
            "parallel": self.opts.parallel,
            "stages": list(self.stages),
            "state": (
                "running" if not self.finished
                else "cancelled" if self.cancelled else "done"
            ),
            "started": self.started,
            "finished": self.finished,
            "items": [
                {
                    "index": it.index,
                    "image": it.image,
                    "label": it.label,
                    "name": it.name,
                    "root": it.root,
                    "state": it.state,
                    "stage": it.stage,
                    "stage_index": (
                        self.stages.index(it.stage) if it.stage in self.stages else -1
                    ),
                    "stage_progress": round(it.stage_progress, 4),
                    "progress": round(self._progress(it), 4),
                    "stages": dict(it.stages),
                    "message": it.message,
                    "log": it.log,
                    "log_dir": str(self.log_dir / it.label),
                    "started": it.started,
                    "finished": it.finished,
                }
                for it in self.items
            ],
        }

    def _progress(self, it: Item) -> float:
        n = len(self.stages)
        done = sum(1 for s in self.stages if it.stages.get(s) in ("passed", "skipped"))
        if it.state in ("passed",):
            return 1.0
        frac = it.stage_progress if it.stages.get(it.stage) == "running" else 0.0
        return min(1.0, (done + frac) / n)

    def write_status(self, *, force: bool = False) -> None:
        """Atomic replace, throttled — the GUI polls this file."""
        now = time.monotonic()
        if not force and now - self._last_write < 0.25:
            return
        with self._write_lock:
            with self._lock:
                self._last_write = now
                data = self._status_dict()
            path = self.log_dir / STATUS_FILE
            tmp = path.with_name(f".{STATUS_FILE}.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            for _ in range(20):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    # Windows: the reader may hold the file open for a moment.
                    time.sleep(0.05)
            tmp.unlink(missing_ok=True)

    def _event(self, msg: str) -> None:
        if not self.on_event:
            return
        try:
            self.on_event(msg)
        except (OSError, ValueError):
            # Studio closed and took our stdout with it. The batch is being
            # stopped through .cancel; a broken pipe must not be what ends it.
            self.on_event = None

    # ---- cancel ----------------------------------------------------------

    def cancel_requested(self) -> bool:
        if self.cancelled:
            return True
        if (self.log_dir / CANCEL_FILE).exists():
            self.cancelled = True
            self._kill_all()
        return self.cancelled

    def _kill_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            _kill_tree(p)

    # ---- stages ----------------------------------------------------------

    def _cli(self, *args: str) -> list[str]:
        return [self.python, "-m", "project_studio", "--platform", self.platform, *args]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        pp = env.get("PYTHONPATH", "")
        if pkg_parent not in pp.split(os.pathsep):
            env["PYTHONPATH"] = pkg_parent + (os.pathsep + pp if pp else "")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _stage_log(self, it: Item, stage: str) -> Path:
        d = self.log_dir / it.label
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.stages.index(stage) + 1}-{stage}.log"

    def _run_stage(self, it: Item, stage: str, argv: list[str],
                   *, capture_stdout: bool = False) -> tuple[int, str]:
        """Run one stage to its log. Returns (exit code, stdout if captured)."""
        log_path = self._stage_log(it, stage)
        with self._lock:
            it.stage = stage
            it.stage_progress = 0.0
            it.stages[stage] = "running"
            it.log = str(log_path)
        self.write_status(force=True)

        captured: list[str] = []
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            # Own process group, so Stop takes cmake / ninja / the compilers
            # with it instead of orphaning them to keep building.
            kwargs["start_new_session"] = True

        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            log.write(f"# {it.image}\n# stage: {stage}\n$ {_quote(argv)}\n\n")
            log.flush()
            try:
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE if capture_stdout else subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace",
                    env=self._env(), cwd=str(self.out), **kwargs,
                )
            except OSError as exc:
                log.write(f"failed to start: {exc}\n")
                return 127, ""
            with self._lock:
                self._procs.add(proc)

            # A silent stage (the N64 harvest, a long link) never yields a line
            # for the read loop to notice Stop on, so watch from the side.
            stop_watch = threading.Event()

            def watch() -> None:
                while not stop_watch.wait(0.5):
                    if self.cancel_requested():
                        _kill_tree(proc)
                        return

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()

            err_text: list[str] = []
            err_thread = None
            if capture_stdout and proc.stderr is not None:
                def drain_err() -> None:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        err_text.append(line)
                err_thread = threading.Thread(target=drain_err, daemon=True)
                err_thread.start()

            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                if capture_stdout:
                    captured.append(line)
                    continue
                frac = _line_progress(line)
                if frac is not None:
                    with self._lock:
                        it.stage_progress = frac
                    self.write_status()
            code = proc.wait()
            stop_watch.set()
            if err_thread is not None:
                err_thread.join()
                if err_text:
                    log.write("\n# stderr\n")
                    log.writelines(err_text)
            with self._lock:
                self._procs.discard(proc)
            log.write(f"\n# exit {code}\n")
        return code, "".join(captured)

    def _mark(self, it: Item, stage: str, state: str) -> None:
        with self._lock:
            it.stages[stage] = state
            if state == "passed":
                it.stage_progress = 1.0
        self.write_status(force=True)

    def _fail(self, it: Item, stage: str, message: str) -> None:
        with self._lock:
            if stage:
                it.stage = stage   # a refusal before the stage ran still names it
                it.stages[stage] = "failed"
            it.state = "cancelled" if self.cancelled else "failed"
            it.message = message
            it.finished = time.time()
        self.write_status(force=True)
        self._event(f"[{'CANCELLED' if self.cancelled else 'FAILED'}] {it.label}: {message}")

    # ---- one image -------------------------------------------------------

    def _probe(self, it: Item) -> dict | None:
        """Identity for the scaffold flags: what the New Project page fills in."""
        if platforms.current().is_cartridge:
            code, out = self._run_stage(
                it, "probe", self._cli("probe-rom", "--rom", it.image), capture_stdout=True)
            if code != 0:
                return None
        else:
            # A miss is not a failure: plenty of dumps have no Redump entry,
            # and the scaffold only needs a name, which the filename gives.
            code, out = self._run_stage(
                it, "probe",
                self._cli("lookup-disc-meta", "--disc", it.image, "--json"),
                capture_stdout=True)
        try:
            data = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            return None if platforms.current().is_cartridge else {}
        return data if isinstance(data, dict) else {}

    def _scaffold_args(self, it: Item, meta: dict) -> list[str]:
        key = self.platform
        args = ["new-project", "--name", it.name, "--dir", str(self.out), "--rom", it.image]
        if key == "n64":
            for flag, k in (("--github-repo", "project"), ("--n64-slug", "slug"),
                            ("--n64-exe", "exe")):
                if meta.get(k):
                    args += [flag, str(meta[k])]
        elif key == "snes":
            for flag, k in (("--github-repo", "project_name"), ("--region", "region"),
                            ("--zip-prefix", "zip_prefix")):
                if meta.get(k):
                    args += [flag, str(meta[k])]
        else:
            for flag, k in (("--description", "description"), ("--publisher", "publisher"),
                            ("--year", "year"), ("--region", "region")):
                if meta.get(k):
                    args += [flag, str(meta[k])]
            players = meta.get("players")
            if isinstance(players, int) and 1 <= players <= 8:
                args += ["--players", str(players)]
        if not self.opts.enable_ci:
            args.append("--no-ci")
        if not self.opts.fetch_boxart:
            args.append("--no-fetch-boxart")
        if self.opts.create_github:
            args.append("--create-github")
        if not self.opts.add_to_index:
            args.append("--no-index")
        return args

    def _project_root(self, it: Item, meta: dict) -> Path:
        from .newproject import NewProjectOptions, project_root_for

        repo = ""
        if self.platform == "n64":
            repo = str(meta.get("project") or "")
        elif self.platform == "snes":
            repo = str(meta.get("project_name") or "")
        opts = NewProjectOptions(
            name=it.name, disc=it.image, parent_dir=str(self.out),
            github_repo=repo, platform=self.platform,
            n64_slug=str(meta.get("slug") or "") if self.platform == "n64" else "",
            n64_exe=str(meta.get("exe") or "") if self.platform == "n64" else "",
        )
        return project_root_for(opts)

    def run_item(self, it: Item) -> None:
        if self.cancel_requested():
            self._fail(it, "", "cancelled before start")
            return
        with self._lock:
            it.state = "running"
            it.started = time.time()
        self.write_status(force=True)
        self._event(f"[START] {it.label}")

        meta = self._probe(it)
        if meta is None:
            self._fail(it, "probe", "probe failed — see the probe log")
            return
        self._mark(it, "probe", "passed")
        it.name = (
            str(meta.get("display_name") or meta.get("name") or "").strip()
            or _strip_tags(Path(it.image).stem) or Path(it.image).stem
        )

        # Claim the project folder before anything writes to it. Two dumps of
        # one game (two regions, two revisions) probe to the same name, and in
        # parallel mode two wizards writing one tree is corruption, not a
        # failure anybody could read.
        root = self._project_root(it, meta)
        with self._lock:
            it.root = str(root)
            owner = self._claimed.get(str(root))
            if owner is None:
                self._claimed[str(root)] = it.index
        if owner is not None:
            self._fail(it, "scaffold",
                       f"same project folder as #{owner + 1} ({root.name}) — "
                       "run it in a separate batch or output folder")
            return

        exists = root.is_dir() and any(root.iterdir())
        if exists and self.opts.reuse_existing:
            self._mark(it, "scaffold", "skipped")
        elif exists:
            self._fail(it, "scaffold",
                       f"{root} already exists — tick 'Reuse existing projects' "
                       "or pick an empty output folder")
            return
        else:
            code, _ = self._run_stage(it, "scaffold", self._cli(*self._scaffold_args(it, meta)))
            if code != 0:
                self._fail(it, "scaffold", f"new-project exit {code}")
                return
            self._mark(it, "scaffold", "passed")

        r = str(root)
        bt = self.opts.build_type or "Release"
        for stage in self.stages[2:]:
            if self.cancel_requested():
                self._fail(it, stage, "cancelled")
                return
            if stage == "framework":
                argv = self._cli("build", "framework", "--root", r, "--build-type", bt)
            elif stage == "generate":
                argv = self._cli("build", "generate", "--root", r, "--rom", it.image)
                if self.platform == "n64":
                    argv += ["--build-type", bt]
            elif stage == "configure":
                argv = self._cli("build", "configure", "--root", r, "--build-type", bt)
            else:  # compile
                argv = self._cli("build", "compile", "--root", r)
                if self.opts.build_jobs > 0:
                    argv += ["--jobs", str(self.opts.build_jobs)]
            code, _ = self._run_stage(it, stage, argv)
            if code != 0:
                self._fail(it, stage, f"{stage} exit {code}")
                return
            self._mark(it, stage, "passed")

        with self._lock:
            it.state = "passed"
            it.stage = ""
            it.message = "built"
            it.finished = time.time()
        self.write_status(force=True)
        self._event(f"[PASSED] {it.label}")

    # ---- the batch -------------------------------------------------------

    def run(self) -> bool:
        self.out.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / CANCEL_FILE).unlink(missing_ok=True)
        self.started = time.time()
        self.write_status(force=True)

        workers = max(1, min(int(self.opts.parallel or 1), len(self.items) or 1))
        if workers == 1:
            for it in self.items:
                self._run_item_safe(it)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(self._run_item_safe, self.items))
        for it in self.items:
            if it.state == "queued":
                self._fail(it, "", "cancelled before start")
        self.finished = time.time()
        self.write_status(force=True)
        self._write_summary()
        return all(it.state == "passed" for it in self.items)

    def _run_item_safe(self, it: Item) -> None:
        try:
            self.run_item(it)
        except Exception as exc:  # one project's bug must not stop the batch
            self._fail(it, it.stage, f"internal error: {exc!r}")

    def _write_summary(self) -> None:
        passed = sum(1 for it in self.items if it.state == "passed")
        lines = [
            f"Bulk Recomp — {platforms.current().display}",
            f"output: {self.out}",
            f"{passed}/{len(self.items)} passed"
            + (" (cancelled)" if self.cancelled else ""),
            f"elapsed: {self.finished - self.started:.0f}s",
            "",
        ]
        for it in self.items:
            verdict = it.state.upper()
            where = f" at {it.stage}" if it.state != "passed" and it.stage else ""
            lines.append(f"{verdict:<9} {it.label}  {it.name}{where}")
            if it.state != "passed":
                lines.append(f"          {it.message}")
                if it.log:
                    lines.append(f"          log: {it.log}")
        (self.log_dir / SUMMARY_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _line_progress(line: str) -> float | None:
    m = _NINJA_RE.match(line)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        return done / total if total > 0 else None
    m = _MAKE_RE.match(line)
    if m:
        return min(100, int(m.group(1))) / 100.0
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _quote(argv: list[str]) -> str:
    return " ".join(f'"{a}"' if (" " in a or not a) else a for a in argv)


def read_image_list(path: str) -> list[str]:
    """One image per line; blank lines and '#' comments ignored."""
    out: list[str] = []
    for raw in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def validate(opts: BulkRecompOptions) -> list[str]:
    errs: list[str] = []
    profile = platforms.current()
    if not opts.images:
        errs.append("no game images given")
    if not (opts.out_dir or "").strip():
        errs.append("an output folder is required")
    seen: set[str] = set()
    for img in opts.images:
        p = Path(img).expanduser()
        if not p.is_file():
            errs.append(f"not found: {img}")
        elif p.suffix.lower() not in profile.image_exts:
            errs.append(
                f"not a {profile.display} image ({' / '.join(profile.image_exts)}): {p.name}")
        key = str(p.resolve()) if p.exists() else img
        if key in seen:
            errs.append(f"listed twice: {img}")
        seen.add(key)
    if opts.parallel < 1:
        errs.append("parallel must be at least 1")
    return errs
