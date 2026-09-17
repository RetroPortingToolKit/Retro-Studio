"""Git / GitHub operations for a game repository.

Uses ``git`` and (optionally) ``gh``. No force-push, amend, or hook skips.
"""

from __future__ import annotations

import configparser
import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import platforms

DEFAULT_PSXRECOMP_URL = "https://github.com/mstan/psxrecomp.git"
DEFAULT_RECOMP_UI_URL = "https://github.com/RetroPortingToolKit/recomp-ui.git"
DEFAULT_RECOMP_NET_URL = "https://github.com/RetroPortingToolKit/recomp-net.git"
DEFAULT_RBENGINE_URL = "https://github.com/RetroPortingToolKit/rbengine.git"
DEFAULT_BRANCH = "master"
DEFAULT_NESTED_BRANCH = "main"
# Modules whose repo changed owner or name. A .gitmodules entry naming one of
# these is a dead pointer that only still resolves because GitHub redirects the
# old slug — not a deliberate fork choice — so Studio says so on the row, and
# Reset lands on the live URL instead of faithfully restoring the stale one.
# Keyed by lowercased "owner/repo": a genuine fork lives under some other
# owner, is absent from this map, and is left alone.
MOVED_REPOS: dict[str, str] = {
    "technicallycomputers/recomp-net": DEFAULT_RECOMP_NET_URL,
    "technicallycomputers/retcomm-rbengine": DEFAULT_RBENGINE_URL,
    "mstan/n64lle": "https://github.com/RetroPortingToolKit/n64lle.git",
    "mstan/recomp-ui": DEFAULT_RECOMP_UI_URL,
}
_GITHUB_SLUG_RE = re.compile(
    r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/*$", re.I
)


def github_slug(url: str) -> str:
    """Lowercased ``owner/repo`` from any GitHub remote spelling, else ""."""
    m = _GITHUB_SLUG_RE.search((url or "").strip())
    return f"{m.group(1).lower()}/{m.group(2).lower()}" if m else ""


def moved_url(url: str) -> str:
    """Where a repo that has since moved now lives, or "" if ``url`` is current."""
    return MOVED_REPOS.get(github_slug(url), "")
# Nested under the framework checkout (game/<framework> or the engine repo
# itself). Both consoles vendor the same two libraries at the same paths.
KNOWN_NESTED_SUBMODULES: tuple[tuple[str, str, str], ...] = (
    ("lib/recomp-net", DEFAULT_RECOMP_NET_URL, DEFAULT_NESTED_BRANCH),
    ("lib/retcomm-rbengine", DEFAULT_RBENGINE_URL, DEFAULT_NESTED_BRANCH),
)
# Every nested module Studio knows how to manage, across all consoles. Which
# of them a given session actually has is PlatformProfile.nested_paths — n64lle
# vendors neither, so an N64 session's tuple is empty and the --modules ops
# report "none" instead of hunting for a lib/recomp-net that was never there.
NESTED_PATHS = tuple(p for p, _, _ in KNOWN_NESTED_SUBMODULES)


def nested_paths() -> tuple[str, ...]:
    """Nested modules THIS session's framework actually carries."""
    return platforms.current().nested_paths


# ---------------------------------------------------------------------------
# Which framework this session's repos are built on.
#
# Everything below used to say "psxrecomp" literally. These four helpers are
# the only place that decision is now made, so a SNES session gets snesrecomp
# in the submodule path, the default URL, the default branch and every error
# message without a single call site passing a flag.
# ---------------------------------------------------------------------------
def framework_name() -> str:
    return platforms.current().framework


def framework_url() -> str:
    return platforms.current().framework_url


def framework_branch() -> str:
    return platforms.current().framework_branch


def framework_prefix() -> str:
    return framework_name() + "/"


def known_submodules() -> tuple[str, ...]:
    return (framework_name(), "recomp-ui")


# There is deliberately no KNOWN_SUBMODULES constant. One existed as a
# back-compat alias, PSX-shaped by definition and carrying a comment telling
# callers not to use it — and default_module_paths() used it anyway. Under
# --platform snes every --modules op then targeted a psxrecomp that is not
# there ("psxrecomp: checkout missing") while never touching snesrecomp, so a
# framework commit was silently never committed, pulled, or PUSHED and CI hit
# a submodule pin the remote had never seen. A constant that must not be used
# is a rule in prose; deleting it is the same rule in the artifact.


@dataclass
class CmdResult:
    ok: bool
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubmoduleInfo:
    name: str
    path: str
    url: str = ""
    branch: str = ""
    sha: str = ""
    present: bool = False
    initialized: bool = False
    checkout_branch: str = ""  # actual HEAD branch; empty if detached / missing

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepoStatus:
    root: str
    is_git: bool
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    remote_url: str = ""
    gh_available: bool = False
    gh_repo: str = ""
    short_status: str = ""
    submodules: list[SubmoduleInfo] = field(default_factory=list)
    nested_submodules: list[SubmoduleInfo] = field(default_factory=list)
    # Same value under two names: `framework_root` is what new code reads,
    # `psxrecomp_root` stays so existing JSON consumers keep working.
    framework_root: str = ""
    psxrecomp_root: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["submodules"] = [s.to_dict() for s in self.submodules]
        d["nested_submodules"] = [s.to_dict() for s in self.nested_submodules]
        return d


def _run(
    cmd: list[str],
    cwd: Path,
    *,
    dry_run: bool = False,
    check: bool = False,
) -> tuple[int, str, str]:
    if dry_run:
        return 0, "dry-run: " + " ".join(cmd), ""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        if check:
            raise
        return 127, "", str(exc)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


# Git verbs / patterns that talk to a remote (DNS / TLS / HTTP).
_GIT_NETWORK_VERBS = frozenset(
    {"fetch", "pull", "push", "clone", "ls-remote"}
)

# Transient reachability failures (DNS blips, TLS resets, GitHub 5xx/429, etc.).
_TRANSIENT_GIT_NETWORK_MARKERS = (
    "could not resolve host",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "failed to connect",
    "failed to connect to",
    "connection timed out",
    "connection refused",
    "connection reset by peer",
    "network is unreachable",
    "no route to host",
    # Note: do not match bare "unable to access" — that also covers 401/403 auth.
    "the remote end hung up unexpectedly",
    "ssl_error_syscall",
    "ssl connection timeout",
    "tls handshake timeout",
    "gnutls_handshake",
    "openssl ssl_read",
    "openssl ssl_connect",
    "empty reply from server",
    "operation timed out",
    "transfer closed with outstanding read data remaining",
    "rpc failed",
    "recv failure",
    "early eof",
    "http/2 stream",
    "http 429",
    "http 502",
    "http 503",
    "http 504",
    "error: 429",
    "error: 502",
    "error: 503",
    "error: 504",
    "server aborted the request",
)

# Default: 4 attempts with 1s / 2s / 4s backoff (covers bulk-pull DNS flakes).
_GIT_NETWORK_ATTEMPTS = 4
_GIT_NETWORK_BASE_DELAY_S = 1.0


def _git_args_need_network(args: tuple[str, ...]) -> bool:
    for i, a in enumerate(args):
        if a in _GIT_NETWORK_VERBS:
            return True
        if a == "submodule" and i + 1 < len(args) and args[i + 1] in (
            "update",
            "add",
        ):
            return True
        if a == "remote" and i + 1 < len(args) and args[i + 1] == "update":
            return True
    return False


def _is_transient_git_network_error(stdout: str, stderr: str) -> bool:
    blob = f"{stdout}\n{stderr}".lower()
    return any(m in blob for m in _TRANSIENT_GIT_NETWORK_MARKERS)


def _git(
    cwd: Path,
    *args: str,
    dry_run: bool = False,
    network_attempts: int | None = None,
) -> tuple[int, str, str]:
    """Run git. Network-touching verbs retry on transient DNS/connect errors."""
    if dry_run or not _git_args_need_network(args):
        return _run(["git", *args], cwd, dry_run=dry_run)

    attempts = (
        _GIT_NETWORK_ATTEMPTS if network_attempts is None else max(1, int(network_attempts))
    )
    code, out, err = 1, "", ""
    for attempt in range(1, attempts + 1):
        code, out, err = _run(["git", *args], cwd, dry_run=False)
        if code == 0:
            if attempt > 1:
                note = f"(git network: succeeded on attempt {attempt}/{attempts})"
                out = f"{out}\n{note}".strip() if out else note
            return code, out, err
        if attempt < attempts and _is_transient_git_network_error(out, err):
            time.sleep(_GIT_NETWORK_BASE_DELAY_S * (2 ** (attempt - 1)))
            continue
        if attempt > 1 and _is_transient_git_network_error(out, err):
            note = f"(git network: failed after {attempts} attempts)"
            err = f"{err}\n{note}".strip() if err else note
        return code, out, err
    return code, out, err


def current_branch(root: Path) -> str | None:
    """Return the checked-out branch name, or None if detached / unknown."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        return None
    code, out, _ = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return None
    name = out.strip()
    if not name or name == "HEAD":
        return None
    return name


def _which_gh() -> str | None:
    return shutil.which("gh")


def _is_git_repo(root: Path) -> bool:
    code, out, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def _is_repo_root(path: Path) -> bool:
    """True only if `path` is the TOPLEVEL of its own repository.

    Module resolution must use this, never _is_git_repo(): that one answers
    "inside a work tree", which is true for ANY subdirectory of a repo —
    including an *uninitialised* submodule, which is just an empty directory.

    The consequence was not cosmetic. `psxrecomp/lib/retcomm-rbengine` is empty
    in several game repos, so every `git -C` on it silently resolved to
    psxrecomp itself: the rbengine branch dropdown listed psxrecomp's branches
    (feat/rbengine, master, …, which looked plausible), and worse, "switch
    lib/retcomm-rbengine to master" actually moved PSXRECOMP to master —
    immediately undoing the psxrecomp switch performed one step earlier in the
    same run.
    """
    path = path.expanduser().resolve()
    code, out, _ = _git(path, "rev-parse", "--show-toplevel")
    if code != 0:
        return False
    try:
        return Path(out.strip()).resolve() == path
    except OSError:
        return False


def _module_checked_out(owner: Path, path: str) -> bool:
    """True only when ``owner/path`` is a cloned module, not a placeholder.

    ``SubmoduleInfo.present`` answers "the directory exists", which is also
    true of the empty directory ``git clone`` leaves behind when it is not
    given ``--recurse-submodules``. Every ``--modules`` op needs this stronger
    question instead, or it reports "checkout missing" against a repo whose
    .gitmodules is perfectly correct.
    """
    sub = owner / _normalize_module_path(path, nested=False)
    return sub.is_dir() and _is_repo_root(sub)


def missing_module_reason(root: Path, path: str, *, nested: bool = False) -> str:
    """Why a module checkout did not resolve, ending in the op that fixes it.

    "checkout missing" on its own named a symptom and no cure, and the three
    states behind it want different sentences: never cloned (the common one,
    from a clone without --recurse-submodules), a stray non-repo directory in
    the way, or — for a nested module — a framework that is not checked out
    either. Ending every one of them with "Ensure submodules first" is the
    rule living in the message rather than in someone's memory.
    """
    root = root.expanduser().resolve()
    path = _normalize_module_path(path, nested=nested)
    if nested:
        owner = resolve_framework_dir(root)
        if owner is None:
            fw = framework_name()
            return (
                f"{path}: checkout missing — {fw} itself is not checked out; "
                "run Ensure submodules first, then Ensure nested"
            )
    else:
        owner = root
    sub = owner / path
    if not sub.exists():
        return (
            f"{path}: checkout missing — nothing at {sub}; "
            "run Ensure submodules first"
        )
    if sub.is_dir() and not any(sub.iterdir()):
        return (
            f"{path}: checkout missing — {path}/ is the empty placeholder a "
            "clone without --recurse-submodules leaves behind (git submodule "
            "status prefixes it with '-'); run Ensure submodules first"
        )
    return (
        f"{path}: checkout missing — {sub} has files but is not a git "
        "checkout; move it aside, then run Ensure submodules first"
    )


def _init_one_module(owner: Path, path: str, *, dry_run: bool = False) -> CmdResult:
    """``git submodule update --init --recursive`` for one registered module.

    The missing half of Ensure submodules. Being registered in .gitmodules and
    being cloned on disk are two states, and a plain ``git clone`` produces the
    first without the second.
    """
    path = _normalize_module_path(path, nested=False)
    sub = owner / path
    if sub.is_dir() and not _is_repo_root(sub) and any(sub.iterdir()):
        return CmdResult(
            False,
            f"{path} has files but is not a git checkout ({sub}) — move it "
            "aside, then Ensure submodules again",
        )
    if dry_run:
        return CmdResult(
            True, f"[dry-run] would clone {path} (submodule update --init --recursive)"
        )
    # sync first: a local `submodule.<path>.url` override left by an earlier
    # fork experiment otherwise decides where this clone comes from.
    _git(owner, "submodule", "sync", "--", path)
    code, out, err = _git(
        owner, "submodule", "update", "--init", "--recursive", "--", path
    )
    if code != 0:
        return CmdResult(False, f"Failed to clone {path}", err or out)
    if not _is_repo_root(sub):
        # Tool Skepticism: git exited 0, so check the thing it claimed to do.
        return CmdResult(
            False,
            f"git reported success but {sub} is still not a checkout",
            out or err,
        )
    branch = current_branch(sub)
    _, head, _ = _git(sub, "rev-parse", "--short=12", "HEAD")
    where = branch or f"detached at {head or 'HEAD'}"
    return CmdResult(True, f"Cloned {path} ({where})", out)


def init_module_checkouts(
    root: Path,
    *,
    paths: list[str] | None = None,
    nested: bool = False,
    dry_run: bool = False,
) -> list[CmdResult]:
    """Clone the module checkouts a clone without --recurse-submodules skipped."""
    root = root.expanduser().resolve()
    if not _is_git_repo(root):
        return [CmdResult(False, "Not a git repository")]
    owner = resolve_framework_dir(root) if nested else root
    if owner is None:
        fw = framework_name()
        return [
            CmdResult(
                False,
                f"No {fw} checkout found — run Ensure submodules first",
            )
        ]
    want = paths or list(default_module_paths(nested=nested))
    cp = _read_gitmodules(owner)
    results: list[CmdResult] = []
    for path in want:
        path = _normalize_module_path(path, nested=nested)
        if _module_checked_out(owner, path):
            results.append(CmdResult(True, f"{path}: already checked out"))
            continue
        # Registered is this op's whole input. A module the repo does not
        # vendor at all is not a failure to clone it — plenty of ports carry
        # no recomp-ui — and reporting it as one would put a red FAIL next to
        # a healthy repo. Adding it is Ensure submodules' job, and it says so.
        if _section_for_path(cp, path) is None:
            results.append(
                CmdResult(
                    True,
                    f"{path}: not in .gitmodules — nothing to clone "
                    "(Ensure submodules adds it)",
                )
            )
            continue
        r = _init_one_module(owner, path, dry_run=dry_run)
        results.append(CmdResult(r.ok, f"{path}: {r.message}", r.detail))
    return results


def _read_gitmodules(root: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    gm = root / ".gitmodules"
    if gm.is_file():
        cp.read(gm, encoding="utf-8")
    return cp


def _write_gitmodules(root: Path, cp: configparser.ConfigParser, *, dry_run: bool) -> None:
    if dry_run:
        return
    buf: list[str] = []
    for section in cp.sections():
        buf.append(f"[{section}]")
        for key, value in cp.items(section):
            buf.append(f"\t{key} = {value}")
        buf.append("")
    text = "\n".join(buf).rstrip() + "\n"
    (root / ".gitmodules").write_text(text, encoding="utf-8")


def _section_for_path(cp: configparser.ConfigParser, path: str) -> str | None:
    for section in cp.sections():
        if cp.get(section, "path", fallback="") == path:
            return section
    # Common form: [submodule "psxrecomp"]
    want = f'submodule "{path}"'
    for section in cp.sections():
        if section == want or section.endswith(f'"{path}"'):
            return section
    return None


def _submodule_sha(root: Path, path: str) -> str:
    code, out, _ = _git(root, "rev-parse", f"HEAD:{path}")
    if code == 0 and re.fullmatch(r"[0-9a-f]{40}", out.strip()):
        return out.strip()[:12]
    # Fallback: ls-tree
    code, out, _ = _git(root, "ls-tree", "HEAD", path)
    if code == 0:
        parts = out.split()
        if len(parts) >= 3 and parts[0] == "160000":
            return parts[2][:12]
    # Working tree HEAD inside submodule
    sub = root / path
    if sub.is_dir():
        code, out, _ = _git(sub, "rev-parse", "HEAD")
        if code == 0:
            return out.strip()[:12]
    return ""


def _submodule_remote_url(root: Path, path: str) -> str:
    """Best-effort origin URL from a submodule working tree."""
    sub = root / path
    if not sub.is_dir():
        return ""
    code, out, _ = _git(sub, "remote", "get-url", "origin")
    if code == 0 and out.strip():
        return out.strip()
    return ""


def _default_url_for_path(path: str) -> str:
    for p, url, _ in KNOWN_NESTED_SUBMODULES:
        if p == path:
            return url
    if path == framework_name():
        return framework_url()
    if path == "recomp-ui":
        return DEFAULT_RECOMP_UI_URL
    # A tree cut against the other console still resolves, so a mixed
    # workspace reports real URLs instead of blanks.
    for profile in platforms.PROFILES.values():
        if path == profile.framework:
            return profile.framework_url
    return ""


def _list_submodules(
    root: Path, *, known: tuple[str, ...] | None = None
) -> list[SubmoduleInfo]:
    if known is None:
        known = known_submodules()
    cp = _read_gitmodules(root)
    found: dict[str, SubmoduleInfo] = {}
    for section in cp.sections():
        path = cp.get(section, "path", fallback="")
        if not path:
            continue
        name = path
        m = re.search(r'"([^"]+)"', section)
        if m:
            name = m.group(1)
        url = cp.get(section, "url", fallback="").strip()
        if not url:
            url = _submodule_remote_url(root, path) or _default_url_for_path(path)
        found[path] = SubmoduleInfo(
            name=name,
            path=path,
            url=url,
            branch=cp.get(section, "branch", fallback=""),
            sha=_submodule_sha(root, path),
            present=(root / path).exists(),
            # `initialized` is the strong question, and it has to be: an empty
            # placeholder directory satisfies every weaker test, and
            # current_branch() inside one resolves to the SUPERPROJECT's branch
            # (git walks up), so an uninitialised snesrecomp used to report the
            # game repo's branch as its own.
            initialized=_module_checked_out(root, path),
            checkout_branch=(
                current_branch(root / path) or ""
                if _module_checked_out(root, path)
                else ""
            ),
        )
    # Ensure known slots show up even if missing from .gitmodules
    for path in known:
        if path not in found:
            url = _submodule_remote_url(root, path) or _default_url_for_path(path)
            present = (root / path).exists()
            checked_out = _module_checked_out(root, path)
            found[path] = SubmoduleInfo(
                name=path,
                path=path,
                url=url,
                present=present,
                initialized=checked_out,
                sha=_submodule_sha(root, path) if present else "",
                checkout_branch=(
                    (current_branch(root / path) or "") if checked_out else ""
                ),
            )
    # Stable order: known first, then others
    ordered: list[SubmoduleInfo] = []
    for path in known:
        if path in found:
            ordered.append(found.pop(path))
    ordered.extend(sorted(found.values(), key=lambda s: s.path))
    return ordered


def resolve_framework_dir(root: Path) -> Path | None:
    """The framework checkout: ``root`` itself, or ``root/<framework>``.

    Studio is routinely pointed at the engine repo as well as at a port, so
    both spellings have to resolve. The marker file (runtime.cmake /
    runner.cmake) is what distinguishes a real checkout from an uninitialised
    submodule directory of the same name.
    """
    root = root.expanduser().resolve()
    profile = platforms.current()
    marker = profile.framework_marker
    if marker and (root / marker).is_file():
        return root
    nested = root / profile.framework
    if marker and (nested / marker).is_file():
        return nested
    if nested.is_dir() and (nested / ".git").exists():
        return nested
    return None


# Historical name kept so nothing outside this module has to change at once.
resolve_psxrecomp_dir = resolve_framework_dir


def list_nested_modules(root: Path) -> list[SubmoduleInfo]:
    """Nested modules inside the framework that THIS session manages.

    Empty when the framework carries none. n64lle's own .gitmodules does list
    two (ares, rabbitizer), but those are its vendored build dependencies —
    its build initialises them and Studio has no business advancing their pins.
    Listing them here would put an Advance-pins button in front of something
    every --modules op then declines to touch.
    """
    if not nested_paths():
        return []
    psx = resolve_psxrecomp_dir(root)
    if psx is None:
        return []
    return _list_submodules(psx, known=nested_paths())


def repo_status(root: Path) -> RepoStatus:
    root = root.expanduser().resolve()
    st = RepoStatus(root=str(root), is_git=_is_git_repo(root), gh_available=bool(_which_gh()))
    if not st.is_git:
        st.notes.append("Not a git repository.")
        return st

    code, branch, _ = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    st.branch = branch if code == 0 else ""

    code, upstream, _ = _git(root, "rev-parse", "--abbrev-ref", "@{upstream}")
    if code == 0:
        st.upstream = upstream
        code2, ab, _ = _git(root, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
        if code2 == 0:
            parts = ab.split()
            if len(parts) == 2:
                st.behind = int(parts[0])
                st.ahead = int(parts[1])

    code, porcelain, _ = _git(root, "status", "--porcelain")
    if code == 0:
        lines = [ln for ln in porcelain.splitlines() if ln.strip()]
        st.short_status = "\n".join(lines[:40])
        for ln in lines:
            xy = ln[:2]
            if ln.startswith("??"):
                st.untracked += 1
            else:
                if xy[0] not in (" ", "?"):
                    st.staged += 1
                if xy[1] not in (" ", "?"):
                    st.unstaged += 1
        st.dirty = bool(lines)

    code, url, _ = _git(root, "remote", "get-url", "origin")
    if code == 0:
        st.remote_url = url

    if st.gh_available:
        code, out, err = _run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            root,
        )
        if code == 0 and out:
            st.gh_repo = out.strip()
        elif err:
            st.notes.append("gh present but not authenticated / no GitHub remote.")

    st.submodules = _list_submodules(root)
    psx = resolve_psxrecomp_dir(root)
    if psx is not None:
        st.framework_root = str(psx)
        st.psxrecomp_root = str(psx)
        if psx == root:
            # Engine checkout: top-level known slots are the nested libs.
            st.submodules = _list_submodules(root, known=nested_paths())
            st.nested_submodules = list(st.submodules)
            st.notes.append(
                f"Root is a {framework_name()} checkout (nested modules are direct)."
            )
        else:
            st.nested_submodules = list_nested_modules(root)
    else:
        fw = framework_name()
        st.notes.append(
            f"{fw} is registered but not checked out — every --modules and "
            "--nested op will report 'checkout missing' until you run Ensure "
            "submodules (git submodule update --init --recursive)."
        )
    stale = [
        x.path
        for x in (*st.submodules, *st.nested_submodules)
        if x.present and not x.initialized
    ]
    if stale:
        st.notes.append(
            "Registered but never cloned (empty placeholder directories): "
            + ", ".join(stale)
            + " — run Ensure submodules. This is what a `git clone` without "
            "--recurse-submodules leaves behind."
        )
    return st


def set_submodule_url(
    root: Path,
    path: str,
    url: str,
    *,
    dry_run: bool = False,
) -> CmdResult:
    """Write ``url`` into ``.gitmodules`` for ``path`` (create section if needed)."""
    root = root.expanduser().resolve()
    url = url.strip()
    if not url:
        return CmdResult(False, "URL required")
    path = path.strip().replace("\\", "/")
    cp = _read_gitmodules(root)
    section = _section_for_path(cp, path)
    if section is None:
        if not (root / path).exists():
            return CmdResult(False, f"No .gitmodules entry for {path}")
        section = f'submodule "{path}"'
        cp.add_section(section)
        cp.set(section, "path", path)
    prev = cp.get(section, "url", fallback="").strip()
    if prev == url:
        return CmdResult(True, f"{path} url already set")
    cp.set(section, "url", url)
    _write_gitmodules(root, cp, dry_run=dry_run)
    if not dry_run:
        _git(root, "config", "-f", ".gitmodules", f"submodule.{path}.url", url)
        _git(root, "submodule", "sync", "--", path)
    return CmdResult(True, f"Set {path} url → {url}")


# ---------------------------------------------------------------------------
# Where each module is fetched from and pushed to
# ---------------------------------------------------------------------------
# Three settings answer "which repo is this", and they are not the same one:
#
#   .gitmodules  submodule.<p>.url   tracked; what everyone who clones gets
#   .git/config  submodule.<p>.url   this clone only; what `submodule update` uses
#   <sub>/remote origin              this clone only; what push and pull use
#
# A contributor working from a fork wants the last two pointed at their fork
# and the first left alone — changing .gitmodules would commit their fork into
# the port for everybody. Someone re-homing a project wants all three. Both are
# legitimate, so the scope is the caller's to state rather than ours to guess.
URL_SCOPES = ("local", "gitmodules")


@dataclass
class ModuleUrl:
    path: str
    nested: bool = False
    owner: str = ""            # the repo holding this gitlink
    gitmodules_url: str = ""   # tracked
    local_url: str = ""        # .git/config override, this clone only
    origin_url: str = ""       # the checkout's own origin
    present: bool = False

    @property
    def effective_url(self) -> str:
        """What git will actually reach for, in the order git resolves it."""
        return self.origin_url or self.local_url or self.gitmodules_url

    @property
    def moved_to(self) -> str:
        """Live URL when the URL git will actually use points at a moved repo.

        Judged on the effective URL alone. A stale ``.gitmodules`` behind a
        reset override is already reported by the row's ".gitmodules says …"
        line, and flagging it here too left the row still saying "moved" after
        a Reset had already moved it.
        """
        return moved_url(self.effective_url)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["effective_url"] = self.effective_url
        d["moved_to"] = self.moved_to
        return d


def _local_url(owner: Path, path: str) -> str:
    code, out, _ = _git(owner, "config", "--local", "--get", f"submodule.{path}.url")
    return out.strip() if code == 0 else ""


def _gitmodules_url(owner: Path, path: str) -> str:
    cp = _read_gitmodules(owner)
    section = _section_for_path(cp, path)
    if section is None:
        return ""
    return cp.get(section, "url", fallback="").strip()


def _url_owner(root: Path, *, nested: bool) -> Path | None:
    return resolve_framework_dir(root) if nested else root.expanduser().resolve()


def module_urls(root: Path) -> list[ModuleUrl]:
    """Every module this port pins, and where each one currently points."""
    root = root.expanduser().resolve()
    rows: list[ModuleUrl] = []
    for nested in (False, True):
        owner = _url_owner(root, nested=nested)
        if owner is None or not _is_git_repo(owner):
            continue
        for path in default_module_paths(nested=nested):
            rows.append(ModuleUrl(
                path=path,
                nested=nested,
                owner=str(owner),
                gitmodules_url=_gitmodules_url(owner, path) or _default_url_for_path(path),
                local_url=_local_url(owner, path),
                origin_url=_submodule_remote_url(owner, path),
                # Same strong question the --modules ops ask: an empty
                # placeholder directory is not a checkout, and reporting it as
                # present is what let "(not checked out)" go missing from the
                # one listing that would have explained the whole problem.
                present=_module_checked_out(owner, path),
            ))
    return rows


# Not validation of whether the repo exists — only of whether this is the shape
# of a git remote at all, so a stray path or a half-pasted line is caught before
# it is written somewhere that will fail confusingly later.
_URL_SHAPE_RE = re.compile(
    r"^(https?://|ssh://|git://|file://|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:|/|\.{1,2}/)"
)


def valid_remote_url(url: str) -> bool:
    return bool(_URL_SHAPE_RE.match((url or "").strip()))


def set_module_url(
    root: Path,
    path: str,
    url: str,
    *,
    nested: bool = False,
    scope: str = "local",
    dry_run: bool = False,
) -> CmdResult:
    """Point one module at a different remote.

    ``scope="local"`` touches nothing tracked: the ``.git/config`` override and
    the checkout's ``origin``, which together are what a fork workflow needs.
    ``scope="gitmodules"`` additionally rewrites the tracked ``.gitmodules``,
    which changes the project for everyone who clones it and has to be
    committed — so it is never the default.
    """
    if scope not in URL_SCOPES:
        return CmdResult(False, f"Unknown scope {scope!r} (expected {', '.join(URL_SCOPES)})")
    url = (url or "").strip()
    if not url:
        return CmdResult(False, "URL required")
    if not valid_remote_url(url):
        return CmdResult(
            False,
            f"{url!r} does not look like a git remote — expected https://…, "
            "ssh://…, git@host:owner/repo.git, or an absolute path")
    root = root.expanduser().resolve()
    path = _normalize_module_path(path, nested=nested)
    owner = _url_owner(root, nested=nested)
    if owner is None:
        return CmdResult(False, f"No {framework_name()} checkout found")

    if scope == "gitmodules":
        # set_submodule_url syncs, which propagates into .git/config and origin.
        r = set_submodule_url(owner, path, url, dry_run=dry_run)
        if not r.ok:
            return r
        return CmdResult(True, f"{path} → {url} (.gitmodules; commit to share it)")

    if dry_run:
        return CmdResult(True, f"[dry-run] {path} → {url} (this clone only)")
    code, out, err = _git(owner, "config", "--local", f"submodule.{path}.url", url)
    if code != 0:
        return CmdResult(False, f"{path}: could not set the local override", err or out)
    # Deliberately not `git submodule sync`: sync copies .gitmodules back over
    # the override we just wrote, which would silently undo this.
    sub_dir = owner / path
    if (sub_dir / ".git").exists():
        code, out, err = _git(sub_dir, "remote", "set-url", "origin", url)
        if code != 0:
            return CmdResult(False, f"{path}: could not set origin", err or out)
        return CmdResult(True, f"{path} → {url} (this clone only; push/pull use it now)")
    return CmdResult(
        True,
        f"{path} → {url} (this clone only; applies when the submodule is checked out)")


def reset_module_url(
    root: Path,
    path: str,
    *,
    nested: bool = False,
    dry_run: bool = False,
) -> CmdResult:
    """Drop the local override and go back to what ``.gitmodules`` says.

    Except where ``.gitmodules`` names a repo that has since moved: restoring a
    dead pointer is not a useful reset, and ``git submodule sync`` would write
    that stale URL straight back over both ``.git/config`` and the submodule's
    origin. There the live URL goes in as a local override instead. Either way
    nothing tracked changes — rewriting ``.gitmodules`` is Save's job, under the
    scope the user picked.
    """
    root = root.expanduser().resolve()
    path = _normalize_module_path(path, nested=nested)
    owner = _url_owner(root, nested=nested)
    if owner is None:
        return CmdResult(False, f"No {framework_name()} checkout found")
    tracked = _gitmodules_url(owner, path) or _default_url_for_path(path)
    live = moved_url(tracked)
    if dry_run:
        if live:
            return CmdResult(
                True,
                f"[dry-run] {path}: would drop the override and use {live} "
                f"({tracked} moved)")
        return CmdResult(True, f"[dry-run] {path}: would drop the local override")
    _git(owner, "config", "--local", "--unset", f"submodule.{path}.url")
    _git(owner, "submodule", "sync", "--", path)
    if not live:
        return CmdResult(True, f"{path} → {tracked} (back to .gitmodules)")
    _git(owner, "config", "--local", f"submodule.{path}.url", live)
    sub_dir = owner / path
    if (sub_dir / ".git").exists():
        _git(sub_dir, "remote", "set-url", "origin", live)
    return CmdResult(
        True,
        f"{path} → {live} (this clone only; .gitmodules still says {tracked}, "
        "which moved — Save with the .gitmodules scope to fix it for everyone)")


def ensure_submodule(
    root: Path,
    path: str,
    *,
    url: str,
    branch: str = DEFAULT_BRANCH,
    dry_run: bool = False,
) -> CmdResult:
    root = root.expanduser().resolve()
    if not _is_git_repo(root):
        return CmdResult(False, "Not a git repository")
    dest = root / path
    cp = _read_gitmodules(root)
    already_registered = _section_for_path(cp, path) is not None
    looks_present = dest.exists() and (
        (dest / ".git").exists()
        or (dest / "CMakeLists.txt").is_file()
        or (dest / "runtime" / "runtime.cmake").is_file()
        or any(dest.iterdir())
    )
    if already_registered or looks_present:
        notes: list[str] = []
        # Heal missing .gitmodules url (common when a submodule was added by hand).
        section = _section_for_path(cp, path)
        have_url = ""
        if section:
            have_url = cp.get(section, "url", fallback="").strip()
        if not have_url and url:
            url_r = set_submodule_url(root, path, url, dry_run=dry_run)
            notes.append(url_r.message)
        set_r = set_submodule_branch(root, path, branch, dry_run=dry_run)
        notes.append(set_r.message)
        # Registered is not cloned, and this used to stop here. A repo cloned
        # without --recurse-submodules has the .gitmodules entry and an empty
        # directory, so Ensure submodules reported "already present" while
        # every later --modules op reported "checkout missing" — the two
        # answers disagreeing about the same repo, with neither naming the
        # cure. Ensure now finishes the job it claims to have done.
        if not _module_checked_out(root, path):
            init_r = _init_one_module(root, path, dry_run=dry_run)
            notes.append(init_r.detail)
            return CmdResult(
                init_r.ok,
                f"{path} registered but not cloned → {init_r.message}",
                "; ".join(n for n in notes if n),
            )
        return CmdResult(
            True,
            f"{path} already present",
            "; ".join(n for n in notes if n),
        )

    code, out, err = _git(
        root,
        "submodule",
        "add",
        "-b",
        branch,
        url,
        path,
        dry_run=dry_run,
    )
    if code != 0:
        return CmdResult(False, f"Failed to add {path}", err or out)
    _git(root, "submodule", "update", "--init", "--recursive", path, dry_run=dry_run)
    return CmdResult(True, f"Added submodule {path} (branch {branch})", out)


def ensure_known_submodules(
    root: Path,
    *,
    framework_branch_name: str = "",
    recomp_ui_branch: str = DEFAULT_BRANCH,
    dry_run: bool = False,
) -> list[CmdResult]:
    """Add / init the framework + recomp-ui submodules for this platform."""
    return [
        ensure_submodule(
            root,
            framework_name(),
            url=framework_url(),
            branch=framework_branch_name or framework_branch(),
            dry_run=dry_run,
        ),
        ensure_submodule(
            root,
            "recomp-ui",
            url=DEFAULT_RECOMP_UI_URL,
            branch=recomp_ui_branch or DEFAULT_BRANCH,
            dry_run=dry_run,
        ),
    ]


def ensure_nested_modules(
    root: Path,
    *,
    recomp_net_branch: str = DEFAULT_NESTED_BRANCH,
    rbengine_branch: str = DEFAULT_NESTED_BRANCH,
    dry_run: bool = False,
) -> list[CmdResult]:
    """Ensure ``lib/recomp-net`` + ``lib/retcomm-rbengine`` inside the framework."""
    psx = resolve_psxrecomp_dir(root)
    if psx is None:
        fw = framework_name()
        return [
            CmdResult(
                False,
                f"No {fw} checkout found (need root or root/{fw}) — the nested "
                "modules live inside it, so run Ensure submodules first",
            )
        ]
    if not _is_git_repo(psx):
        return [CmdResult(False, f"{framework_name()} is not a git repo: {psx}")]

    branch_by_path = {
        "lib/recomp-net": recomp_net_branch or DEFAULT_NESTED_BRANCH,
        "lib/retcomm-rbengine": rbengine_branch or DEFAULT_NESTED_BRANCH,
    }
    want = set(nested_paths())
    if not want:
        return [
            CmdResult(
                True,
                f"{framework_name()} carries no nested modules — nothing to ensure",
            )
        ]
    results: list[CmdResult] = []
    for path, url, default_branch in KNOWN_NESTED_SUBMODULES:
        if path not in want:
            continue
        results.append(
            ensure_submodule(
                psx,
                path,
                url=url,
                branch=branch_by_path.get(path, default_branch),
                dry_run=dry_run,
            )
        )
    return results


def update_nested_modules(
    root: Path,
    *,
    paths: list[str] | None = None,
    remote: bool = False,
    stage: bool = True,
    dry_run: bool = False,
) -> CmdResult:
    """Update nested modules inside the framework; optionally stage gitlinks there."""
    psx = resolve_psxrecomp_dir(root)
    if psx is None:
        return CmdResult(False, f"No {framework_name()} checkout found")
    want = paths or list(nested_paths())
    if not want:
        return CmdResult(
            True, f"{framework_name()} carries no nested modules — nothing to update"
        )
    # Allow callers to pass game-relative paths
    normalized: list[str] = []
    for p in want:
        p = p.strip().replace("\\", "/")
        if p.startswith(framework_prefix()):
            p = p[len(framework_prefix()) :]
        normalized.append(p)

    r = update_submodules(psx, paths=normalized, remote=remote, dry_run=dry_run)
    if not r.ok:
        return r
    if stage and not dry_run:
        code, out, err = _git(psx, "add", "--", *normalized)
        if code != 0:
            return CmdResult(
                False,
                f"Nested update ok but failed to stage gitlinks in {framework_name()}",
                err or out,
            )
        detail = (r.detail + "\n" if r.detail else "") + f"staged in {framework_name()}: " + ", ".join(
            normalized
        )
        return CmdResult(
            True,
            r.message + f" (staged in {framework_name()})",
            detail.strip(),
        )
    if stage and dry_run:
        return CmdResult(
            True,
            r.message + f" (would stage in {framework_name()})",
            r.detail,
        )
    return r


def commit_nested(
    root: Path,
    message: str,
    *,
    dry_run: bool = False,
) -> CmdResult:
    """Commit inside the framework checkout (nested gitlink bumps)."""
    psx = resolve_psxrecomp_dir(root)
    if psx is None:
        return CmdResult(False, f"No {framework_name()} checkout found")
    r = commit_all(psx, message, dry_run=dry_run)
    if r.ok:
        r = CmdResult(r.ok, f"{framework_name()}: {r.message}", r.detail)
    return r


def set_nested_branch(
    root: Path,
    path: str,
    branch: str,
    *,
    dry_run: bool = False,
) -> CmdResult:
    psx = resolve_psxrecomp_dir(root)
    if psx is None:
        return CmdResult(False, f"No {framework_name()} checkout found")
    path = path.strip().replace("\\", "/")
    if path.startswith(framework_prefix()):
        path = path[len(framework_prefix()) :]
    return set_submodule_branch(psx, path, branch, dry_run=dry_run)


def set_submodule_branch(
    root: Path,
    path: str,
    branch: str,
    *,
    dry_run: bool = False,
) -> CmdResult:
    root = root.expanduser().resolve()
    branch = branch.strip()
    if not branch:
        return CmdResult(False, "Branch name required")
    cp = _read_gitmodules(root)
    section = _section_for_path(cp, path)
    if section is None:
        # Create section if submodule dir exists
        if not (root / path).exists():
            return CmdResult(False, f"No .gitmodules entry for {path}")
        section = f'submodule "{path}"'
        cp.add_section(section)
        cp.set(section, "path", path)
        # Try to keep existing url from git config
        code, url, _ = _git(root, "config", "-f", ".gitmodules", f"submodule.{path}.url")
        if code == 0 and url:
            cp.set(section, "url", url)
    cp.set(section, "branch", branch)
    _write_gitmodules(root, cp, dry_run=dry_run)
    if not dry_run:
        _git(root, "config", "-f", ".gitmodules", f"submodule.{path}.branch", branch)
        # Sync into local git config
        _git(root, "submodule", "sync", "--", path)
    return CmdResult(True, f"Set {path} tracking branch → {branch}")


def resolve_default_branch(root: Path, *, remote: str = "origin") -> str | None:
    """Best-effort default branch for a checkout (``origin/HEAD``, else main/master)."""
    root = root.expanduser().resolve()
    if not _is_git_repo(root):
        return None
    code, out, _err = _git(
        root,
        "symbolic-ref",
        "--quiet",
        "--short",
        f"refs/remotes/{remote}/HEAD",
    )
    if code == 0:
        ref = (out or "").strip()
        if ref:
            # origin/main → main
            if "/" in ref:
                return ref.split("/", 1)[-1]
            return ref
    for name in ("main", "master"):
        code, _o, _e = _git(
            root, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{name}"
        )
        if code == 0:
            return name
        code, _o, _e = _git(
            root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"
        )
        if code == 0:
            return name
    code, out, _err = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 0:
        head = (out or "").strip()
        if head and head != "HEAD":
            return head
    return None


def is_default_branch_token(branch: str | None) -> bool:
    """True for empty / ``(default)`` / similar UI sentinels."""
    s = (branch or "").strip().lower()
    if not s:
        return True
    if s.startswith("(") and "default" in s:
        return True
    return s in ("default", "auto")


def _git_switch(
    cwd: Path,
    *args: str,
    dry_run: bool = False,
) -> tuple[int, str, str]:
    """Run ``git switch``; fall back to ``git checkout`` on older Git."""
    code, out, err = _git(cwd, "switch", *args, dry_run=dry_run)
    if dry_run or code == 0:
        return code, out, err
    err_l = f"{err}\n{out}".lower()
    if "not a git command" not in err_l and "unknown command" not in err_l:
        return code, out, err
    checkout_args: list[str] = []
    i = 0
    a = list(args)
    while i < len(a):
        if a[i] in ("--guess", "--no-guess"):
            i += 1
            continue
        if a[i] == "-c":
            checkout_args.append("-b")
            i += 1
            continue
        if a[i] == "-C":
            checkout_args.append("-B")
            i += 1
            continue
        if a[i] == "--track" and i + 1 < len(a):
            checkout_args.extend(["--track", a[i + 1]])
            i += 2
            continue
        checkout_args.append(a[i])
        i += 1
    return _git(cwd, "checkout", *checkout_args, dry_run=False)


def switch_branch(
    root: Path,
    branch: str,
    *,
    create: bool = False,
    dry_run: bool = False,
    prefer_default: bool = True,
) -> CmdResult:
    """``git switch`` onto ``branch`` (guess remote-tracking; optional ``-c``).

    Empty / ``(default)`` resolves each repo's default branch (``origin/HEAD``,
    else main/master). When ``prefer_default`` is set and a named branch is
    missing, falls back to that default instead of failing.
    """
    root = root.expanduser().resolve()
    if not _is_git_repo(root):
        return CmdResult(False, "Not a git repository")

    requested = (branch or "").strip()
    used_default_token = is_default_branch_token(requested)
    if used_default_token:
        resolved = resolve_default_branch(root)
        if not resolved:
            return CmdResult(
                False,
                "Could not resolve default branch (fetch remotes / set origin/HEAD)",
            )
        target = resolved
        note = "default"
    else:
        target = requested
        note = ""

    current = current_branch(root)
    if current == target:
        suffix = f" ({note})" if note else ""
        return CmdResult(True, f"Already on {target}{suffix}")

    verb = "Would switch" if dry_run else "Switched"
    if dry_run:
        # Validate the ref exists so dry-run matches real switch + default fallback.
        code_v, _, _ = _git(
            root, "rev-parse", "--verify", "--quiet", f"refs/heads/{target}"
        )
        if code_v != 0:
            code_v, _, _ = _git(
                root,
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/{target}",
            )
        if code_v == 0:
            suffix = f" ({note})" if note else ""
            return CmdResult(True, f"{verb} to {target}{suffix}")
        # fall through to create / prefer_default handling below
        code, out, err = 1, "", f"fatal: invalid reference: {target}"
    else:
        code, out, err = _git_switch(root, "--guess", target, dry_run=False)
        if code == 0:
            suffix = f" ({note})" if note else ""
            return CmdResult(True, f"{verb} to {target}{suffix}", out)

    if create and not used_default_token:
        code2, out2, err2 = _git_switch(root, "-c", target, dry_run=dry_run)
        if code2 == 0:
            return CmdResult(True, f"{verb} to new branch {target}", out2)
        return CmdResult(
            False,
            f"Could not create/switch to {target}",
            err2 or out2 or err or out,
        )

    # Named branch missing → prefer this checkout's default branch.
    if prefer_default and not used_default_token and not create:
        fallback = resolve_default_branch(root)
        if fallback and fallback != target:
            if current == fallback:
                return CmdResult(
                    True,
                    f"Already on {fallback} (default; {target} missing)",
                )
            if dry_run:
                return CmdResult(
                    True,
                    f"{verb} to {fallback} (default; {target} missing)",
                )
            code_f, out_f, err_f = _git_switch(
                root, "--guess", fallback, dry_run=False
            )
            if code_f == 0:
                return CmdResult(
                    True,
                    f"{verb} to {fallback} (default; {target} missing)",
                    out_f or err or out,
                )
            err = err_f or out_f or err

    hint = " (enable Create / --create to start a new branch)"
    return CmdResult(False, f"Could not switch to {target}{hint}", err or out)


def set_repo_branch(
    root: Path,
    branch: str,
    *,
    create: bool = False,
    dry_run: bool = False,
) -> CmdResult:
    """Switch the repo working tree (``git switch``). Kept for CLI compatibility."""
    return switch_branch(root, branch, create=create, dry_run=dry_run)


def list_branches(
    repo: Path,
    *,
    remotes: bool = True,
    fetch: bool = False,
) -> list[str]:
    """Return sorted local (+ remote-tracking) branch short names for a repo."""
    repo = repo.expanduser().resolve()
    if not repo.is_dir() or not _is_git_repo(repo):
        return []
    if fetch:
        _git(repo, "fetch", "--prune", "--quiet")
    names: set[str] = set()
    code, out, _ = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    if code == 0:
        for line in out.splitlines():
            n = line.strip()
            if n and n != "HEAD":
                names.add(n)
    if remotes:
        code, out, _ = _git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes"
        )
        if code == 0:
            for line in out.splitlines():
                n = line.strip()
                if not n or n.endswith("/HEAD") or "->" in n:
                    continue
                # origin/main → main
                if "/" in n:
                    n = n.split("/", 1)[1]
                if n and n != "HEAD":
                    names.add(n)
    return _sort_branch_names(names)


def list_remote_head_branches(url: str) -> list[str]:
    """``git ls-remote --heads`` when a local checkout is unavailable."""
    url = (url or "").strip()
    if not url:
        return []
    code, out, _ = _git(Path.cwd(), "ls-remote", "--heads", url)
    if code != 0:
        return []
    names: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if ref.startswith("refs/heads/"):
            names.add(ref[len("refs/heads/") :])
    return _sort_branch_names(names)


def list_module_branches(
    root: Path,
    path: str,
    *,
    nested: bool = False,
    remotes: bool = True,
    fetch: bool = False,
    url_fallback: str = "",
) -> list[str]:
    """Branches for a game submodule or a nested module inside the framework."""
    root = root.expanduser().resolve()
    path = path.strip().replace("\\", "/")
    if nested:
        psx = resolve_psxrecomp_dir(root)
        if psx is None:
            return list_remote_head_branches(url_fallback) if url_fallback else []
        if path.startswith(framework_prefix()):
            path = path[len(framework_prefix()) :]
        owner = psx
    else:
        owner = root
    sub = owner / path
    if sub.is_dir() and _is_repo_root(sub):
        return list_branches(sub, remotes=remotes, fetch=fetch)
    # Fall back to URL from .gitmodules or caller
    url = url_fallback
    if not url:
        cp = _read_gitmodules(owner)
        section = _section_for_path(cp, path)
        if section:
            url = cp.get(section, "url", fallback="")
    return list_remote_head_branches(url)


def _sort_branch_names(names: set[str] | list[str]) -> list[str]:
    preferred = ("main", "master", "develop", "development")

    def key(s: str) -> tuple:
        s_l = s.lower()
        try:
            rank = preferred.index(s_l)
        except ValueError:
            rank = len(preferred)
        return (rank, s_l)

    return sorted(set(names), key=key)


def update_submodules(
    root: Path,
    *,
    paths: list[str] | None = None,
    remote: bool = False,
    dry_run: bool = False,
) -> CmdResult:
    root = root.expanduser().resolve()
    if not _is_git_repo(root):
        return CmdResult(False, "Not a git repository")
    cmd = ["submodule", "update", "--init", "--recursive"]
    if remote:
        cmd.append("--remote")
    if paths:
        cmd.append("--")
        cmd.extend(paths)
    code, out, err = _git(root, *cmd, dry_run=dry_run)
    if code != 0:
        return CmdResult(False, "Submodule update failed", err or out)
    mode = "remote tracking tip" if remote else "pinned gitlink"
    return CmdResult(True, f"Updated submodules ({mode})", out)


def advance_submodule_pins(
    root: Path,
    *,
    paths: list[str] | None = None,
    nested: bool = False,
    ref: str = "",
    stage: bool = True,
    dry_run: bool = False,
) -> list[CmdResult]:
    """Move submodule gitlinks forward, and stage the move.

    Not the same operation as :func:`update_submodules`, and the difference is
    the whole point. ``git submodule update`` checks out the gitlink the
    superproject *already records* — so on a fork carrying an old pin it puts
    the old revision back, which is exactly what makes "I tried updating the
    modules" leave the pin where it was. Advancing fetches, moves the checkout
    to the tracked branch tip (or ``ref``), and stages the new gitlink so the
    superproject records it.

    The commit is deliberately left to the caller. Which revision a port is
    pinned to decides what that port is measured against, so it should be
    reviewable — ``git diff --cached`` — before it becomes history.

    ``nested=True`` does the same thing one level down, for the modules inside
    the framework checkout (``lib/recomp-net``, ``lib/retcomm-rbengine``) —
    which is where a PSX port keeps most of what it pins. The gitlinks are
    staged *inside the framework*, so that repo needs its own commit before the
    game repo's framework pin is worth advancing; the caller is told so rather
    than left to discover it from a confusing diff.
    """
    root = root.expanduser().resolve()
    if not _is_git_repo(root):
        return [CmdResult(False, "Not a git repository")]
    # Whose gitlinks are being moved: the game repo, or the framework checkout
    # that owns the nested modules.
    owner = resolve_framework_dir(root) if nested else root
    if owner is None:
        return [
            CmdResult(
                False,
                f"No {framework_name()} checkout found — run Ensure submodules first",
            )
        ]
    want = paths or list(default_module_paths(nested=nested))
    results: list[CmdResult] = []
    for path in want:
        path = _normalize_module_path(path, nested=nested)
        sub_dir = resolve_module_dir(root, path, nested=nested)
        if sub_dir is None or not _is_git_repo(sub_dir):
            results.append(
                CmdResult(False, missing_module_reason(root, path, nested=nested))
            )
            continue
        code, old, _ = _git(sub_dir, "rev-parse", "HEAD")
        old = old.strip() if code == 0 else ""

        target = (ref or "").strip()
        tracked = _tracking_branch_for(owner, path)
        if target:
            # An explicit revision still needs fetching: the point of moving a
            # stale fork's pin is usually a commit it has never seen.
            code, out, err = _git(sub_dir, "fetch", "--tags", "origin", dry_run=dry_run)
            if code != 0 and not dry_run:
                results.append(CmdResult(False, f"{path}: fetch failed", err or out))
                continue
            code, out, err = _git(sub_dir, "checkout", "--detach", target, dry_run=dry_run)
            if code != 0 and not dry_run:
                results.append(CmdResult(
                    False, f"{path}: cannot check out {target}", err or out))
                continue
            moved_to = target
        else:
            # Let git resolve .gitmodules `branch =` and do the fetch, rather
            # than reimplementing that resolution here and drifting from it.
            #
            # Deliberately NOT --recursive: `--remote --recursive` also walks
            # the nested modules to *their* tips, which moves checkouts this
            # call neither reports nor stages. Nested pins are advanced by an
            # explicit nested=True run, so every move that happens is a move
            # somebody asked for and can see.
            code, out, err = _git(
                owner, "submodule", "update", "--init", "--remote",
                "--", path, dry_run=dry_run)
            if code != 0 and not dry_run:
                results.append(CmdResult(False, f"{path}: update --remote failed", err or out))
                continue
            moved_to = tracked or "(default branch)"

        if dry_run:
            results.append(CmdResult(
                True, f"[dry-run] {path}: would advance to {moved_to} and stage the gitlink"))
            continue

        code, new, _ = _git(sub_dir, "rev-parse", "HEAD")
        new = new.strip() if code == 0 else ""
        if new and new == old:
            results.append(CmdResult(True, f"{path}: already at {moved_to} ({old[:9]})"))
            continue
        msg = f"{path}: {old[:9] or '?'} → {new[:9] or '?'} ({moved_to})"
        if not stage:
            results.append(CmdResult(True, msg + "; not staged"))
            continue
        code, out, err = _git(owner, "add", "--", path)
        if code != 0:
            results.append(CmdResult(False, msg + "; staging the gitlink failed", err or out))
            continue
        where = f" in {framework_name()}" if nested else ""
        results.append(CmdResult(True, msg + f"; gitlink staged{where}"))
    return results


# pull() strategies — used by CLI/GUI for game root, submodules, and nested libs.
PULL_MODES = ("ff-only", "rebase", "merge", "reset")
PULL_DIRTY = ("fail", "stash", "discard")


def _upstream_ref(root: Path) -> str | None:
    """Return @{upstream} rev-parse, or origin/<branch> if branch tracks nothing yet."""
    code, out, _ = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if code == 0:
        ref = out.strip()
        if ref:
            return ref
    branch = current_branch(root)
    if branch:
        return f"origin/{branch}"
    return None


def _working_tree_dirty(root: Path) -> bool:
    code, out, _ = _git(root, "status", "--porcelain")
    return code == 0 and bool(out.strip())


def pull(
    root: Path,
    *,
    mode: str = "ff-only",
    dirty: str = "fail",
    dry_run: bool = False,
) -> CmdResult:
    """Pull (or reset-to-upstream) for a git checkout.

    ``mode``:
      - ``ff-only`` (default): ``git pull --ff-only``
      - ``rebase``: ``git pull --rebase``
      - ``merge``: ``git pull --no-rebase``
      - ``reset``: ``git fetch`` + ``git reset --hard <upstream>`` (match GH tip)

    ``dirty`` (ignored for ``reset``, which always hard-resets):
      - ``fail`` (default): leave tree alone; git aborts if it would overwrite
      - ``stash``: ``stash push -u`` before pull, ``stash pop`` after
      - ``discard``: ``git reset --hard HEAD`` before pull (drops local edits)
    """
    root = root.expanduser().resolve()
    mode = (mode or "ff-only").strip().lower().replace("_", "-")
    dirty = (dirty or "fail").strip().lower()
    if mode == "no-rebase":
        mode = "merge"
    if mode not in PULL_MODES:
        return CmdResult(
            False,
            f"Unknown pull mode {mode!r} (want: {', '.join(PULL_MODES)})",
        )
    if dirty not in PULL_DIRTY:
        return CmdResult(
            False,
            f"Unknown dirty policy {dirty!r} (want: {', '.join(PULL_DIRTY)})",
        )
    if not _is_git_repo(root):
        return CmdResult(False, "Not a git repository")

    # --- reset: make local HEAD match upstream tip (discards local commits+edits) ---
    if mode == "reset":
        code, out, err = _git(root, "fetch", "origin", dry_run=dry_run)
        if code != 0 and not dry_run:
            return CmdResult(False, "Fetch failed", err or out)
        upstream = _upstream_ref(root)
        if not upstream:
            return CmdResult(
                False,
                "No upstream branch (set upstream or checkout a branch tracking origin)",
            )
        code, out, err = _git(root, "reset", "--hard", upstream, dry_run=dry_run)
        if code != 0:
            return CmdResult(False, f"reset --hard {upstream} failed", err or out)
        return CmdResult(True, f"Reset to {upstream}", out)

    # --- dirty working tree handling before pull ---
    stashed = False
    if dirty == "discard" and (_working_tree_dirty(root) or dry_run):
        code, out, err = _git(root, "reset", "--hard", "HEAD", dry_run=dry_run)
        if code != 0:
            return CmdResult(False, "discard (reset --hard HEAD) failed", err or out)
    elif dirty == "stash" and (_working_tree_dirty(root) or dry_run):
        code, out, err = _git(
            root, "stash", "push", "-u", "-m", "project-studio pull", dry_run=dry_run
        )
        if code != 0:
            return CmdResult(False, "stash before pull failed", err or out)
        stashed = True
    elif dirty == "fail" and _working_tree_dirty(root) and not dry_run:
        return CmdResult(
            False,
            "Pull blocked: working tree dirty "
            "(commit, or pass dirty=stash|discard, or mode=reset)",
        )

    if mode == "ff-only":
        pull_args = ("pull", "--ff-only")
        label = "ff-only"
    elif mode == "rebase":
        pull_args = ("pull", "--rebase")
        label = "rebase"
    else:  # merge
        pull_args = ("pull", "--no-rebase")
        label = "merge"

    code, out, err = _git(root, *pull_args, dry_run=dry_run)
    if code != 0:
        if stashed and not dry_run:
            _git(root, "stash", "pop")
        return CmdResult(False, f"Pull failed ({label})", err or out)

    detail = out
    if stashed and not dry_run:
        sc, so, se = _git(root, "stash", "pop")
        if sc != 0:
            return CmdResult(
                False,
                f"Pulled ({label}) but stash pop failed",
                (detail + "\n" + (se or so)).strip(),
            )
        detail = (detail + "\n" + so).strip()
    return CmdResult(True, f"Pulled ({label})", detail)


def commit_all(
    root: Path,
    message: str,
    *,
    dry_run: bool = False,
) -> CmdResult:
    root = root.expanduser().resolve()
    message = message.strip()
    if not message:
        return CmdResult(False, "Commit message required")
    code, porcelain, _ = _git(root, "status", "--porcelain")
    if code != 0:
        return CmdResult(False, "git status failed", porcelain)
    if not porcelain.strip() and not dry_run:
        return CmdResult(False, "Nothing to commit")
    code, out, err = _git(root, "add", "-A", dry_run=dry_run)
    if code != 0:
        return CmdResult(False, "git add failed", err or out)
    code, out, err = _git(root, "commit", "-m", message, dry_run=dry_run)
    if code != 0:
        return CmdResult(False, "git commit failed", err or out)
    return CmdResult(True, "Committed", out)


def push(
    root: Path,
    *,
    branch: str = "",
    dry_run: bool = False,
) -> CmdResult:
    """Push current HEAD to origin.

    If HEAD is detached, ``branch`` is required and we push
    ``HEAD:refs/heads/<branch>`` so GitHub gets a real branch ref.
    """
    root = root.expanduser().resolve()
    code, _, err = _git(root, "remote", "get-url", "origin")
    if code != 0 and not dry_run:
        return CmdResult(False, "No origin remote", err)

    local = current_branch(root)
    target = (branch or "").strip()
    if target.startswith("("):
        target = ""

    if local:
        # On a branch: normal upstream push
        code, out, err = _git(root, "push", "-u", "origin", "HEAD", dry_run=dry_run)
        if code != 0:
            return CmdResult(False, "Push failed", err or out)
        return CmdResult(True, f"Pushed {local} → origin", out)

    # Detached HEAD
    if not target:
        return CmdResult(
            False,
            "Detached HEAD — select/checkout a branch before push "
            "(or Studio will push HEAD:refs/heads/<branch> if one is selected)",
        )
    refspec = f"HEAD:refs/heads/{target}"
    code, out, err = _git(root, "push", "-u", "origin", refspec, dry_run=dry_run)
    if code != 0:
        return CmdResult(False, f"Push failed (detached → {target})", err or out)
    # Best-effort: attach local checkout to that branch so later pushes are normal
    if not dry_run:
        _git_switch(root, "-C", target)
        return CmdResult(
            True,
            f"Pushed detached HEAD → origin/{target} (switched to {target})",
            out,
        )
    return CmdResult(
        True,
        f"Would push detached HEAD → origin/{target} and switch to {target}",
        out,
    )


def _normalize_module_path(path: str, *, nested: bool) -> str:
    p = path.strip().replace("\\", "/")
    if nested and p.startswith(framework_prefix()):
        p = p[len(framework_prefix()) :]
    return p


def resolve_module_dir(
    root: Path,
    path: str,
    *,
    nested: bool = False,
) -> Path | None:
    """Resolve a game submodule or a nested module checkout under the framework."""
    root = root.expanduser().resolve()
    path = _normalize_module_path(path, nested=nested)
    if not path:
        return None
    if nested:
        psx = resolve_psxrecomp_dir(root)
        if psx is None:
            return None
        # Engine repo itself: nested paths are direct.
        owner = psx
    else:
        owner = root
    sub = owner / path
    if sub.is_dir() and _is_repo_root(sub):
        return sub
    return None


def default_module_paths(*, nested: bool = False) -> tuple[str, ...]:
    """Module paths a --modules op covers, for THIS session's platform."""
    return nested_paths() if nested else known_submodules()


def _tracking_branch_for(owner: Path, path: str) -> str:
    cp = _read_gitmodules(owner)
    section = _section_for_path(cp, path)
    if not section:
        return ""
    return cp.get(section, "branch", fallback="").strip()


def switch_modules(
    root: Path,
    *,
    paths: list[str] | None = None,
    nested: bool = False,
    branch_by_path: dict[str, str] | None = None,
    create: bool = False,
    set_tracking: bool = True,
    dry_run: bool = False,
) -> list[CmdResult]:
    """``git switch`` inside each module checkout.

    ``branch_by_path`` overrides .gitmodules tracking. When a path has no
    explicit branch, tracking is used, then the checkout's default branch.
    ``(default)`` always means each module's own default. ``set_tracking``
    also writes ``branch =`` in .gitmodules so ``update --remote`` stays aligned.
    """
    want = paths or list(default_module_paths(nested=nested))
    branches = {
        _normalize_module_path(k, nested=nested): (v or "").strip()
        for k, v in (branch_by_path or {}).items()
    }
    results: list[CmdResult] = []
    owner: Path | None = None
    if nested:
        owner = resolve_psxrecomp_dir(root)
    else:
        owner = root.expanduser().resolve()
    for path in want:
        path = _normalize_module_path(path, nested=nested)
        branch = (branches.get(path) or "").strip()
        if is_default_branch_token(branch):
            # Blank override → prefer .gitmodules tracking, else default.
            if not branch and owner is not None:
                tracked = _tracking_branch_for(owner, path)
                if tracked and not is_default_branch_token(tracked):
                    branch = tracked
                else:
                    branch = "(default)"
            else:
                branch = "(default)"
        if not branch:
            results.append(CmdResult(False, f"{path}: no branch to switch to"))
            continue
        sub = resolve_module_dir(root, path, nested=nested)
        if sub is None:
            results.append(
                CmdResult(False, missing_module_reason(root, path, nested=nested))
            )
            continue
        r = switch_branch(sub, branch, create=create, dry_run=dry_run)
        msg = f"{path}: {r.message}"
        if r.ok and set_tracking:
            # Track the branch we actually landed on (may be default fallback).
            actual = ""
            if not dry_run:
                actual = current_branch(sub) or ""
            if not actual:
                # dry-run: derive from switch message when possible
                for token in ("Switched to ", "Would switch to ", "Already on "):
                    if token in (r.message or ""):
                        rest = (r.message or "").split(token, 1)[1]
                        actual = rest.split()[0] if rest else ""
                        break
            if not actual:
                actual = (
                    resolve_default_branch(sub)
                    if is_default_branch_token(branch)
                    else branch
                )
            if nested:
                tr = set_nested_branch(root, path, actual, dry_run=dry_run)
            else:
                tr = set_submodule_branch(root, path, actual, dry_run=dry_run)
            detail = "\n".join(x for x in (r.detail, tr.message) if x)
            results.append(CmdResult(tr.ok, f"{msg}; {tr.message}", detail))
        else:
            results.append(CmdResult(r.ok, msg, r.detail))
    return results


def switch_framework(
    root: Path,
    branch: str,
    *,
    create: bool = False,
    dry_run: bool = False,
) -> CmdResult:
    psx = resolve_psxrecomp_dir(root)
    if psx is None:
        return CmdResult(False, f"No {framework_name()} checkout found")
    r = switch_branch(psx, branch, create=create, dry_run=dry_run)
    return CmdResult(r.ok, f"{framework_name()}: {r.message}", r.detail)


def pull_modules(
    root: Path,
    *,
    paths: list[str] | None = None,
    nested: bool = False,
    mode: str = "ff-only",
    dirty: str = "fail",
    dry_run: bool = False,
) -> list[CmdResult]:
    """Pull inside each module checkout (see ``pull`` for mode/dirty)."""
    want = paths or list(default_module_paths(nested=nested))
    results: list[CmdResult] = []
    for path in want:
        path = _normalize_module_path(path, nested=nested)
        sub = resolve_module_dir(root, path, nested=nested)
        if sub is None:
            results.append(
                CmdResult(False, missing_module_reason(root, path, nested=nested))
            )
            continue
        r = pull(sub, mode=mode, dirty=dirty, dry_run=dry_run)
        results.append(CmdResult(r.ok, f"{path}: {r.message}", r.detail))
    return results


def push_modules(
    root: Path,
    *,
    paths: list[str] | None = None,
    nested: bool = False,
    branch_by_path: dict[str, str] | None = None,
    dry_run: bool = False,
) -> list[CmdResult]:
    """Push each module checkout to origin (handles detached HEAD via branch map)."""
    want = paths or list(default_module_paths(nested=nested))
    branches = branch_by_path or {}
    results: list[CmdResult] = []
    for path in want:
        path = _normalize_module_path(path, nested=nested)
        sub = resolve_module_dir(root, path, nested=nested)
        if sub is None:
            results.append(
                CmdResult(False, missing_module_reason(root, path, nested=nested))
            )
            continue
        r = push(sub, branch=branches.get(path, ""), dry_run=dry_run)
        results.append(CmdResult(r.ok, f"{path}: {r.message}", r.detail))
    return results


def commit_modules(
    root: Path,
    message: str,
    *,
    paths: list[str] | None = None,
    nested: bool = False,
    dry_run: bool = False,
) -> list[CmdResult]:
    """``git add -A && git commit`` inside each module checkout."""
    message = message.strip()
    if not message:
        return [CmdResult(False, "Commit message required")]
    want = paths or list(default_module_paths(nested=nested))
    results: list[CmdResult] = []
    for path in want:
        path = _normalize_module_path(path, nested=nested)
        sub = resolve_module_dir(root, path, nested=nested)
        if sub is None:
            results.append(
                CmdResult(False, missing_module_reason(root, path, nested=nested))
            )
            continue
        r = commit_all(sub, message, dry_run=dry_run)
        results.append(CmdResult(r.ok, f"{path}: {r.message}", r.detail))
    return results


def pull_framework(
    root: Path,
    *,
    mode: str = "ff-only",
    dirty: str = "fail",
    dry_run: bool = False,
) -> CmdResult:
    psx = resolve_psxrecomp_dir(root)
    if psx is None:
        return CmdResult(False, f"No {framework_name()} checkout found")
    r = pull(psx, mode=mode, dirty=dirty, dry_run=dry_run)
    return CmdResult(r.ok, f"{framework_name()}: {r.message}", r.detail)


def push_framework(
    root: Path,
    *,
    branch: str = "",
    dry_run: bool = False,
) -> CmdResult:
    psx = resolve_psxrecomp_dir(root)
    if psx is None:
        return CmdResult(False, f"No {framework_name()} checkout found")
    r = push(psx, branch=branch, dry_run=dry_run)
    return CmdResult(r.ok, f"{framework_name()}: {r.message}", r.detail)


def install_and_push_release_ci(
    root: Path,
    *,
    zip_prefix: str = "",
    force: bool = False,
    push_remote: bool = True,
    dry_run: bool = False,
) -> CmdResult:
    """Write psxrecomp setup-release.yml (+ packager), commit, and push.

    Uses the same template as ``setup_project --enable-ci`` /
    ``op_emit_ci_workflow``. After push, best-effort checks that Actions has
    registered ``release.yml`` (nudge commit if needed).
    """
    from .models import MigrateOptions

    profile = platforms.current()
    if profile.key == "n64":
        # n64lle ships no release-workflow and no packager template, so there
        # is nothing to emit and nothing this could write that its scaffolder
        # would recognise. Refusing here beats writing a psxrecomp workflow
        # into an N64 port, which would fail in Actions on someone else's
        # machine days later.
        return CmdResult(
            False,
            "n64lle ships no release workflow or packager template, so there "
            "is no CI to set up for an N64 port. Package a local build from "
            "the Build tab instead (Package), and file the missing template "
            "against n64lle rather than working around it here.",
        )
    if profile.key == "snes":
        from .snesops import op_emit_ci_workflow, op_emit_packager
    else:
        from .ops import op_emit_ci_workflow, op_emit_packager

    root = root.expanduser().resolve()
    if not _is_git_repo(root):
        return CmdResult(False, "Not a git repository")

    opts = MigrateOptions(
        zip_prefix=zip_prefix.strip() or None,
        enable_ci=True,
        force=force,
        dry_run=dry_run,
    )
    pack = op_emit_packager(root, opts)
    if not pack.ok:
        return CmdResult(False, f"Packager: {pack.message}")
    ci = op_emit_ci_workflow(root, opts)
    if not ci.ok:
        return CmdResult(False, f"CI workflow: {ci.message}")

    paths = sorted({*pack.changed_paths, *ci.changed_paths})
    if not paths and not dry_run:
        # Already present — still allow push of any uncommitted workflow edits.
        wf = root / ".github" / "workflows" / "release.yml"
        pkg = root / "scripts" / "package_setup_release.sh"
        for p in (wf, pkg):
            if p.is_file():
                rel = str(p.relative_to(root)).replace("\\", "/")
                if rel not in paths:
                    paths.append(rel)

    if dry_run:
        return CmdResult(
            True,
            "dry-run: would install CI + commit + push\n"
            + "\n".join(f"  {p}" for p in paths),
        )

    if not paths:
        return CmdResult(False, "No CI files to commit")

    code, out, err = _git(root, "add", "--", *paths)
    if code != 0:
        return CmdResult(False, "git add failed", err or out)

    code, porcelain, _ = _git(root, "diff", "--cached", "--name-only")
    staged = [ln.strip() for ln in porcelain.splitlines() if ln.strip()]
    if not staged:
        # Nothing new staged — still try push so remote gets existing commits.
        detail = f"{pack.op_id}: {pack.message}; {ci.op_id}: {ci.message}"
        if not push_remote:
            return CmdResult(True, "CI already up to date (nothing to commit)", detail)
        push_r = push(root, dry_run=False)
        if not push_r.ok:
            return CmdResult(
                True,
                "CI already up to date locally; push skipped/failed",
                (detail + "\n" + push_r.message).strip(),
            )
        nudge = ensure_actions_registers_release_yml(root)
        msg = "CI already up to date; pushed existing commits"
        if nudge.message:
            msg += f"\n{nudge.message}"
        return CmdResult(True, msg, detail)

    code, out, err = _git(
        root,
        "commit",
        "-m",
        f"ci: add release.yml ({framework_name()} template)",
    )
    if code != 0:
        return CmdResult(False, "git commit failed", err or out)

    if not push_remote:
        return CmdResult(True, "Installed + committed release CI (not pushed)", out)

    push_r = push(root, dry_run=False)
    if not push_r.ok:
        return CmdResult(False, f"Committed CI but push failed: {push_r.message}", push_r.detail)

    nudge = ensure_actions_registers_release_yml(root)
    msg = f"Installed + pushed release CI\n{push_r.message}"
    if nudge.message:
        msg += f"\n{nudge.message}"
    return CmdResult(True, msg, out)


def ensure_actions_registers_release_yml(root: Path) -> CmdResult:
    """Poll Actions for release.yml; nudge with an empty commit if missing.

    Mirrors setup_project.sh ensure_actions_registers_release_yml.
    """
    root = root.expanduser().resolve()
    if not _which_gh():
        return CmdResult(False, "gh CLI not found — skip Actions registration check")

    wf_path = ".github/workflows/release.yml"
    if not (root / wf_path).is_file():
        return CmdResult(False, "release.yml missing locally")

    import time

    for _ in range(6):
        name = release_workflow_name(root)
        if name:
            return CmdResult(
                True,
                f"Actions workflow registered: {name}\n"
                "(runs on workflow_dispatch or push of v* tags)",
            )
        time.sleep(2)

    # Nudge: empty commit touching the workflow mtime via amend-free empty commit
    code, out, err = _git(
        root,
        "commit",
        "--allow-empty",
        "-m",
        "ci: nudge Actions to register release.yml",
    )
    if code != 0:
        return CmdResult(
            False,
            "Actions has not listed release.yml yet; nudge commit failed",
            err or out,
        )
    push_r = push(root, dry_run=False)
    if not push_r.ok:
        return CmdResult(
            False,
            "Nudge committed but push failed",
            push_r.detail or push_r.message,
        )

    for _ in range(8):
        time.sleep(2)
        name = release_workflow_name(root)
        if name:
            return CmdResult(
                True,
                f"Actions workflow registered after nudge: {name}\n"
                "(runs on workflow_dispatch or push of v* tags)",
            )
    return CmdResult(
        False,
        "release.yml nudged but Actions still has not listed it; "
        "open the repo Actions tab or re-run Install & push CI later",
    )


def release_workflow_name(root: Path) -> str | None:
    """Return registered Actions workflow name for release.yml, if any."""
    if not _which_gh():
        return None
    code, out, _ = _run(
        [
            "gh",
            "api",
            "repos/{owner}/{repo}/actions/workflows",
            "--jq",
            '.workflows[] | select(.path|endswith("release.yml")) | .name',
        ],
        root,
    )
    if code != 0:
        return None
    for line in out.splitlines():
        name = line.strip()
        if name:
            return name
    return None


def declared_dispatch_inputs(workflow: Path) -> set[str]:
    """Input names a workflow's ``workflow_dispatch:`` accepts.

    ``gh workflow run`` rejects the whole dispatch if handed an ``-f`` the
    workflow does not declare, and the two consoles' release workflows differ:
    psxrecomp's takes version / bump / publish / reuse_cached_emitters,
    snesrecomp's takes none and releases off a tag. Sending PSX's four at a
    SNES repo fails with "unexpected inputs" — a confusing way to learn that
    the release was never dispatched.

    Deliberately a scanner, not a YAML parse: the toolkit CLI is stdlib-only,
    and the shape being read here (two nested keys at known indents) does not
    justify a dependency. Over-reporting an input is harmless; the only cost of
    getting it wrong is the error we already had.
    """
    try:
        text = workflow.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    lines = text.splitlines()
    names: set[str] = set()
    for i, line in enumerate(lines):
        if line.strip().rstrip(":") != "workflow_dispatch":
            continue
        wd_indent = len(line) - len(line.lstrip())
        # Walk the block under workflow_dispatch: looking for `inputs:`.
        j = i + 1
        inputs_indent = None
        while j < len(lines):
            cur = lines[j]
            if not cur.strip() or cur.lstrip().startswith("#"):
                j += 1
                continue
            indent = len(cur) - len(cur.lstrip())
            if indent <= wd_indent:
                break
            if inputs_indent is None:
                if cur.strip().rstrip(":") == "inputs":
                    inputs_indent = indent
                j += 1
                continue
            if indent <= inputs_indent:
                break
            # The first key level under inputs: is an input name; anything
            # deeper is that input's description / type / default.
            if indent == inputs_indent + 2:
                key = cur.strip()
                if key.endswith(":"):
                    names.add(key[:-1].strip().strip("'\""))
            j += 1
    return names


def run_release_workflow(
    root: Path,
    *,
    version: str = "",
    bump: str = "patch",
    publish: bool = True,
    reuse_cached_emitters: bool = True,
    dry_run: bool = False,
) -> CmdResult:
    root = root.expanduser().resolve()
    if not _which_gh():
        return CmdResult(False, "gh CLI not found — install GitHub CLI and auth login")
    if bump not in ("patch", "minor", "major"):
        return CmdResult(False, f"Invalid bump: {bump}")

    wf = root / ".github" / "workflows" / "release.yml"
    if not wf.is_file() and not dry_run:
        return CmdResult(False, "Missing .github/workflows/release.yml")

    # Prefer workflow file path; gh accepts it.
    cmd = ["gh", "workflow", "run", "release.yml"]
    accepted = declared_dispatch_inputs(wf) if wf.is_file() else set()
    wanted = {
        "version": version,
        "bump": bump,
        "publish": "true" if publish else "false",
        "reuse_cached_emitters": "true" if reuse_cached_emitters else "false",
    }
    skipped = []
    for key, val in wanted.items():
        if key in accepted:
            cmd.extend(["-f", f"{key}={val}"])
        else:
            skipped.append(key)
    if dry_run:
        return CmdResult(True, "dry-run: " + " ".join(cmd))

    code, out, err = _run(cmd, root)
    if code != 0:
        return CmdResult(False, "Failed to dispatch release workflow", err or out)
    note = ""
    if skipped and accepted:
        note = " (workflow declares no " + ", ".join(skipped) + " input)"
    elif skipped and not accepted:
        note = " (workflow takes no dispatch inputs; version/bump ignored)"

    # Best-effort: fetch latest run URL
    run_url = ""
    code2, runs_json, _ = _run(
        [
            "gh",
            "run",
            "list",
            "--workflow",
            "release.yml",
            "--limit",
            "1",
            "--json",
            "url,databaseId,status",
        ],
        root,
    )
    if code2 == 0 and runs_json:
        try:
            runs = json.loads(runs_json)
            if runs:
                run_url = runs[0].get("url") or ""
        except json.JSONDecodeError:
            pass

    # Prefer gh stdout URL when present (newer gh prints it); else our run-list lookup.
    # Do not also return ``out`` as detail — CLI prints message + detail and would
    # duplicate the same actions/runs link.
    gh_url = (out or "").strip()
    url = run_url or gh_url
    if gh_url and run_url and gh_url != run_url:
        url = run_url
    msg = "Dispatched Release builds workflow" + note
    if url:
        msg += f"\n{url}"
    return CmdResult(True, msg)
