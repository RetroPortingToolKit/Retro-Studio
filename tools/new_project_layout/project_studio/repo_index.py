"""Persistent indexed list of local game repos for Project Studio."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import platforms

if TYPE_CHECKING:
    from collections.abc import Sequence

_TOOLKIT = Path(__file__).resolve().parent.parent
# Kept for callers that predate the platform split; new code asks
# default_index_path() so a SNES session never writes the PSX index.
DEFAULT_INDEX_PATH = _TOOLKIT / platforms.PSX.repo_index_file


def default_index_path(platform: str | None = None) -> Path:
    """Index file for `platform` (default: this process's platform).

    One file per console rather than one file with a platform column: the two
    lists are edited by different sessions, and a shared file would have every
    SNES add rewrite the PSX list it never read.
    """
    profile = platforms.get(platform) if platform else platforms.current()
    return _TOOLKIT / profile.repo_index_file


@dataclass
class RepoEntry:
    path: str
    name: str = ""
    cue: str = ""  # absolute path to Redump .cue when known
    # Per-project Build tab state (exe / dir / launch) — keyed by repo path so
    # switching projects never leaks another title's binary.
    build: dict | None = None

    def resolved(self) -> Path:
        return Path(self.path).expanduser().resolve()

    def display(self) -> str:
        name = (self.name or "").strip() or Path(self.path).name
        return name

    def label(self) -> str:
        """Unique-ish label for dropdowns (name · basename if needed)."""
        p = Path(self.path)
        name = self.display()
        if name != p.name:
            return f"{name}  ({p.name})"
        return name

    def cue_path(self) -> Path | None:
        raw = (self.cue or "").strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.resolved() / p
        try:
            p = p.resolve()
        except OSError:
            return p
        return p if p.is_file() else p

    def build_settings(self) -> dict:
        return dict(self.build) if isinstance(self.build, dict) else {}


@dataclass
class RepoIndex:
    repos: list[RepoEntry]
    last: str = ""
    path: Path = field(default_factory=default_index_path)
    log_height: int = 160  # Studio activity-log pane height (px)
    catalog_only: bool = False  # Filter Game-repo dropdown to catalog titles
    bulk_jobs: int = 2  # Parallel workers for Bulk tab (1–4)

    def to_dict(self) -> dict:
        repos_out: list[dict] = []
        for r in self.repos:
            d = asdict(r)
            if not d.get("build"):
                d.pop("build", None)
            if not d.get("cue"):
                # Keep empty cue for compatibility with existing index files.
                pass
            repos_out.append(d)
        h = int(self.log_height) if self.log_height else 160
        if h < 100:
            h = 100
        if h > 800:
            h = 800
        jobs = int(self.bulk_jobs) if self.bulk_jobs else 2
        if jobs < 1:
            jobs = 1
        if jobs > 4:
            jobs = 4
        return {
            "last": self.last,
            "log_height": h,
            "catalog_only": bool(self.catalog_only),
            "bulk_jobs": jobs,
            "repos": repos_out,
        }

    def find(self, root: Path | str) -> RepoEntry | None:
        try:
            key = str(Path(str(root)).expanduser().resolve())
        except OSError:
            key = str(root).strip()
        for entry in self.repos:
            if entry.path == key:
                return entry
        return None


def _toml_game_field(root: Path, field: str) -> str:
    gt = root / "game.toml"
    if not gt.is_file():
        return ""
    try:
        text = gt.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    in_game = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_game = s == "[game]"
            continue
        if not in_game or "=" not in s or s.startswith("#"):
            continue
        key, _, val = s.partition("=")
        if key.strip() != field:
            continue
        return val.strip().strip('"').strip("'")
    return ""


def _game_toml_name(root: Path) -> str:
    return _toml_game_field(root, "name") or root.name


def discover_cue(root: Path) -> str:
    """Best-effort .cue path from game.toml or common disc/ layouts."""
    root = root.expanduser().resolve()
    rel = _toml_game_field(root, "disc")
    if rel:
        cand = Path(rel)
        if not cand.is_absolute():
            cand = root / cand
        try:
            cand = cand.resolve()
        except OSError:
            pass
        if cand.is_file():
            return str(cand)
    # Fallbacks: disc/*.cue (prefer name matching game.toml cue_name)
    cue_name = ""
    gt = root / "game.toml"
    if gt.is_file():
        try:
            text = gt.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        in_prep = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                in_prep = s == "[prepare_disc]"
                continue
            if not in_prep or "=" not in s or s.startswith("#"):
                continue
            key, _, val = s.partition("=")
            if key.strip() == "cue_name":
                cue_name = val.strip().strip('"').strip("'")
                break
    disc_dir = root / "disc"
    if disc_dir.is_dir():
        if cue_name:
            named = disc_dir / cue_name
            if named.is_file():
                return str(named.resolve())
        cues = sorted(disc_dir.glob("*.cue"))
        if len(cues) == 1:
            return str(cues[0].resolve())
        if cues and cue_name:
            for c in cues:
                if c.name == cue_name:
                    return str(c.resolve())
    return ""


# SNES's answer, kept as a module constant because callers outside this file
# import it. New code asks rom_globs(), which reads the current profile.
ROM_EXTS: tuple[str, ...] = ("*.sfc", "*.smc")
ROM_DIRS_ENV = "RETCOMM_SNES_ROM_DIRS"
# Per-console override for where dumps are kept, so an N64 library folder is
# not searched for SNES cartridges and vice versa. The SNES name is the one
# that already existed and keeps working.
_ROM_DIRS_ENV_BY_KEY = {
    "snes": ROM_DIRS_ENV,
    "n64": "RETCOMM_N64_ROM_DIRS",
}
# The env var naming a single dump directly, per console. Each is the same
# variable that console's own tooling honours: regen.sh reads SNESRECOMP_ROM,
# and n64lle's runtime/tools read N64LLE_ROM.
_ROM_ENV_BY_KEY = {
    "snes": "SNESRECOMP_ROM",
    "n64": "N64LLE_ROM",
}
# A headered SNES dump tops out well under this. N64 carts reach 64 MiB
# (Resident Evil 2), so the cap is per-console: it exists only so a stray
# same-named file cannot cost a multi-gigabyte read to reject, and setting it
# below a legitimate dump would silently refuse to match the real ROM.
_MAX_ROM_BYTES = 16 * 1024 * 1024
_MAX_ROM_BYTES_BY_KEY = {
    "snes": _MAX_ROM_BYTES,
    "n64": 64 * 1024 * 1024,
}


def rom_globs(profile=None) -> tuple[str, ...]:
    """Glob patterns for this console's dumps, from its image extensions."""
    profile = profile or platforms.current()
    return tuple(f"*{ext}" for ext in profile.image_exts)


def rom_dirs_env(profile=None) -> str:
    profile = profile or platforms.current()
    return _ROM_DIRS_ENV_BY_KEY.get(profile.key, ROM_DIRS_ENV)


def rom_env(profile=None) -> str:
    profile = profile or platforms.current()
    return _ROM_ENV_BY_KEY.get(profile.key, "")


def max_rom_bytes(profile=None) -> int:
    profile = profile or platforms.current()
    return _MAX_ROM_BYTES_BY_KEY.get(profile.key, _MAX_ROM_BYTES)

# `for cand in "A.sfc" "B.sfc"; do` — the only place a scaffolded repo records
# what its ROM is *called*. The wizard writes it; nothing writes where it lives.
_REGEN_CANDS_RE = re.compile(r"^\s*for\s+cand\s+in\s+(.+?);\s*do\s*$", re.MULTILINE)
_REGEN_CRC32_RE = re.compile(
    r'EXPECTED_CRC32="?\$\{SNESRECOMP_EXPECTED_CRC32:-([0-9a-fA-F]+)'
)


def _regen_text(root: Path) -> str:
    regen = root / "tools" / "regen.sh"
    if not regen.is_file():
        return ""
    try:
        return regen.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def regen_rom_names(root: Path) -> list[str]:
    """ROM filenames ``tools/regen.sh`` will accept, in its own order."""
    m = _REGEN_CANDS_RE.search(_regen_text(root))
    if not m:
        return []
    try:
        names = shlex.split(m.group(1))
    except ValueError:
        return []
    out: list[str] = []
    for n in names:
        n = n.strip()
        # Only literals: an unexpanded $VAR names no file, and guessing at one
        # is exactly the habit this module refuses.
        if n and "$" not in n and n not in out:
            out.append(n)
    return out


def regen_expected_crc32(root: Path) -> str:
    m = _REGEN_CRC32_RE.search(_regen_text(root))
    return m.group(1).lower() if m else ""


def _crc32_of(path: Path) -> str:
    try:
        if path.stat().st_size > max_rom_bytes():
            return ""
        crc = 0
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                crc = zlib.crc32(chunk, crc)
    except OSError:
        return ""
    return f"{crc & 0xFFFFFFFF:08x}"


def rom_library_dirs() -> list[Path]:
    """Directories the user keeps dumps in (``RETCOMM_<CONSOLE>_ROM_DIRS``)."""
    raw = os.environ.get(rom_dirs_env()) or ""
    out: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            d = Path(part).expanduser().resolve()
        except OSError:
            continue
        if d.is_dir() and d not in out:
            out.append(d)
    return out


# `sha256 = "…"` in an n64lle port's game.toml [game] section. The scaffolder
# writes it from the dump it probed and tags it [MEASURED], so it is the one
# digest in that file that is known to describe the ROM this port was cut
# against. Anchored to the key so the CIC's own hex values cannot match.
_GAME_TOML_SHA256_RE = re.compile(
    r'^\s*sha256\s*=\s*["\']([0-9a-fA-F]{64})["\']', re.MULTILINE
)


def game_toml_sha256(root: Path) -> str:
    """The ROM digest an n64lle port's ``game.toml`` records, or ""."""
    toml = Path(root).expanduser().resolve() / "game.toml"
    if not toml.is_file():
        return ""
    try:
        text = toml.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = _GAME_TOML_SHA256_RE.search(text)
    return m.group(1).lower() if m else ""


def _sha256_of(path: Path) -> str:
    try:
        if path.stat().st_size > max_rom_bytes():
            return ""
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def discover_rom(root: Path, extra_dirs: "Sequence[Path | str]" = ()) -> str:
    """Best-effort cartridge ROM path for a title repo.

    The ROM is never committed — both cartridge scaffolds gitignore it — and
    neither wizard records where the copy it was run against lives, so the repo
    alone is usually not enough. Three sources, in falling order of how
    directly they were stated:

      1. The console's own ROM variable (``SNESRECOMP_ROM`` / ``N64LLE_ROM``) —
         the same one that console's tooling honours.
      2. A dump parked in the working tree (root, ``rom/``, ``roms/``). On N64
         this is usually a SYMLINK: setup_project.sh links the dump into
         ``roms/<slug>.z64`` rather than copying it, which is why the search
         resolves what it finds instead of reporting the link path.
      3. A file in a known ROM directory whose DIGEST matches the one this port
         records — ``tools/regen.sh``'s CRC32 on SNES, ``game.toml``'s sha256
         on N64.

    Source 3 is never accepted on name alone. A matching name in a library
    folder is a guess; a matching digest is the ROM, and a wrong ROM
    regenerates wrong C. Finding nothing stays a normal outcome, not a failure.
    """
    root = root.expanduser().resolve()
    profile = platforms.current()

    var = rom_env(profile)
    env = (os.environ.get(var) or "").strip() if var else ""
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            try:
                return str(p.resolve())
            except OSError:
                return str(p)

    globs = rom_globs(profile)
    for sub in (root, root / "rom", root / "roms"):
        if not sub.is_dir():
            continue
        hits = sorted(
            [p for g in globs for p in sub.glob(g)],
            key=lambda p: p.name.lower(),
        )
        for hit in hits:
            try:
                resolved = hit.resolve()
            except OSError:
                continue
            # A dangling symlink is the ordinary state of a cloned n64lle port
            # on a second machine: the link is committed-looking but its target
            # never existed here. Skipping it falls through to the digest
            # search, which can actually find the dump.
            if resolved.is_file():
                return str(resolved)

    seen: set[Path] = set()
    dirs: list[Path] = []
    for d in list(extra_dirs) + list(rom_library_dirs()):
        try:
            dp = Path(str(d)).expanduser().resolve()
        except OSError:
            continue
        if dp in seen or not dp.is_dir():
            continue
        seen.add(dp)
        dirs.append(dp)

    if profile.key == "n64":
        # No filename to go on: n64lle's scaffold renames the dump to
        # <slug>.<ext> in roms/ and records nothing about the original. The
        # digest is the whole identity, so every candidate of the right
        # extension is hashed rather than pre-filtered by name.
        want = game_toml_sha256(root)
        if not want:
            return ""
        for d in dirs:
            for g in globs:
                for cand in sorted(d.glob(g), key=lambda p: p.name.lower()):
                    if cand.is_file() and _sha256_of(cand) == want:
                        return str(cand.resolve())
        return ""

    names = regen_rom_names(root)
    if not names:
        return ""
    want = regen_expected_crc32(root)
    for d in dirs:
        for name in names:
            cand = d / name
            if not cand.is_file():
                continue
            # No pinned digest means nothing here can prove the match, and a
            # wrong ROM regenerates wrong C. Name alone is not enough.
            if not want or _crc32_of(cand) != want:
                continue
            return str(cand)
    return ""


def discover_image(root: Path, extra_dirs: "Sequence[Path | str]" = ()) -> str:
    """Platform-appropriate game image: .cue for PSX, ROM for SNES."""
    if platforms.current().image_kind == "rom":
        return discover_rom(root, extra_dirs)
    return discover_cue(root)


def looks_like_game_repo(root: Path) -> bool:
    """Does `root` look like a port for this process's platform?

    Deliberately platform-scoped: adding a PSX repo to the SNES index would
    hand every later Bulk/Build op a tree whose framework it cannot find, and
    the failure would surface as a confusing git error rather than "wrong
    list".
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        return False
    profile = platforms.current()
    if profile.key == "snes":
        if not (root / "CMakeLists.txt").is_file():
            return False
        # A SNES port has no game.toml. Its identity is the framework checkout
        # plus the per-title analysis config the recompiler consumes.
        if (root / profile.framework).exists():
            return True
        recomp = root / "recomp"
        if recomp.is_dir() and (
            (recomp / "symbols.toml").is_file() or any(recomp.glob("bank*.cfg"))
        ):
            return True
        return False
    if profile.key == "n64":
        # game.toml is NOT sufficient here: a PSX port has one too, and the two
        # files are different contracts under the same name. What separates
        # them is the framework — an n64lle port names n64lle, in its submodule
        # or (on a not-yet-initialised clone) in .gitmodules.
        if not (root / "CMakeLists.txt").is_file():
            return False
        if (root / profile.framework).exists():
            return True
        # A game-package port (2026-09-26 on) has no submodule at all: its
        # .n64lle/local.env, or the game-shim call in its CMakeLists, says so.
        from . import n64_paths

        if n64_paths.is_package_port(root):
            return True
        mods = root / ".gitmodules"
        if mods.is_file():
            try:
                if profile.framework in mods.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    return True
            except OSError:
                pass
        return False
    # PSX. game.toml is its contract too, so before accepting one, rule out a
    # port built on somebody else's framework. Without this an n64lle port —
    # which carries a game.toml of its own — lands in the PSX index, and every
    # later Bulk/Build op there looks for a psxrecomp that is not present.
    if _names_foreign_framework(root, profile):
        return False
    if (root / "game.toml").is_file():
        return True
    if (root / "CMakeLists.txt").is_file() and (
        (root / profile.framework).exists() or (root / "runtime").exists()
    ):
        return True
    return False


def _names_foreign_framework(root: Path, profile) -> bool:
    """Does this tree carry another console's framework rather than ours?"""
    if profile.key != "n64":
        # An n64lle game-package port carries no n64lle/ to be spotted by, and
        # a game.toml like a PSX port's.
        from . import n64_paths

        if n64_paths.is_package_port(root):
            return True
    for other in platforms.PROFILES.values():
        if other.key == profile.key:
            continue
        if (root / other.framework).is_dir() and not (root / profile.framework).exists():
            return True
    return False


def load_index(path: Path | None = None) -> RepoIndex:
    path = path or default_index_path()
    if not path.is_file():
        return RepoIndex(
            repos=[], last="", path=path, log_height=160, catalog_only=False, bulk_jobs=2
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RepoIndex(repos=[], last="", path=path, log_height=160)
    repos: list[RepoEntry] = []
    seen: set[str] = set()
    for raw in data.get("repos") or []:
        if isinstance(raw, str):
            p = raw
            name = ""
            cue = ""
            build = None
        elif isinstance(raw, dict):
            p = str(raw.get("path") or "").strip()
            name = str(raw.get("name") or "").strip()
            cue = str(raw.get("cue") or raw.get("disc") or "").strip()
            raw_build = raw.get("build")
            build = dict(raw_build) if isinstance(raw_build, dict) else None
        else:
            continue
        if not p:
            continue
        try:
            key = str(Path(p).expanduser().resolve())
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
        root_p = Path(key)
        if not name:
            try:
                name = _game_toml_name(root_p) if root_p.is_dir() else Path(p).name
            except OSError:
                name = Path(p).name
        if cue:
            try:
                cue_p = Path(cue).expanduser()
                if not cue_p.is_absolute() and root_p.is_dir():
                    cue_p = root_p / cue_p
                cue = str(cue_p.resolve())
            except OSError:
                pass
        repos.append(RepoEntry(path=key, name=name, cue=cue, build=build))
    # Discovery runs in a second pass so every image the index already states
    # can serve as a search directory for the entries that state none — one
    # title pointed at your ROM folder is enough to find the rest.
    known_dirs: list[Path] = []
    for entry in repos:
        if not entry.cue:
            continue
        d = Path(entry.cue).parent
        if d not in known_dirs:
            known_dirs.append(d)
    for entry in repos:
        if entry.cue:
            continue
        root_p = Path(entry.path)
        if root_p.is_dir():
            entry.cue = discover_image(root_p, known_dirs)
    last = str(data.get("last") or "").strip()
    if last:
        try:
            last = str(Path(last).expanduser().resolve())
        except OSError:
            pass
    log_height = 160
    try:
        log_height = int(data.get("log_height") or 160)
    except (TypeError, ValueError):
        log_height = 160
    if log_height < 100:
        log_height = 100
    if log_height > 800:
        log_height = 800
    catalog_only = bool(data.get("catalog_only"))
    bulk_jobs = 2
    try:
        bulk_jobs = int(data.get("bulk_jobs") or 2)
    except (TypeError, ValueError):
        bulk_jobs = 2
    if bulk_jobs < 1:
        bulk_jobs = 1
    if bulk_jobs > 4:
        bulk_jobs = 4
    return RepoIndex(
        repos=repos,
        last=last,
        path=path,
        log_height=log_height,
        catalog_only=catalog_only,
        bulk_jobs=bulk_jobs,
    )


def save_index(index: RepoIndex) -> None:
    path = index.path or default_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(index.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def add_repo(
    index: RepoIndex,
    root: Path,
    *,
    name: str = "",
    cue: str = "",
) -> RepoEntry:
    root = root.expanduser().resolve()
    key = str(root)
    cue_s = ""
    if cue:
        try:
            cue_s = str(Path(cue).expanduser().resolve())
        except OSError:
            cue_s = str(cue).strip()
    else:
        # discover_image, not discover_cue: a SNES add that fell through to the
        # PSX-only probe recorded nothing and looked like "no ROM exists".
        cue_s = discover_image(
            root, [Path(e.cue).parent for e in index.repos if e.cue]
        )
    for existing in index.repos:
        if existing.path == key:
            if name and name != existing.name:
                existing.name = name
            if cue_s and not existing.cue:
                existing.cue = cue_s
            elif cue and cue_s:
                existing.cue = cue_s
            index.last = key
            save_index(index)
            return existing
    entry = RepoEntry(
        path=key,
        name=(name or _game_toml_name(root)),
        cue=cue_s,
    )
    index.repos.append(entry)
    index.repos.sort(key=lambda e: e.display().lower())
    index.last = key
    save_index(index)
    return entry


def set_repo_cue(index: RepoIndex, root: Path | str, cue: Path | str) -> RepoEntry | None:
    """Store / update the .cue path for an indexed repo."""
    entry = index.find(root)
    if entry is None:
        return None
    cue_p = Path(str(cue)).expanduser()
    try:
        cue_p = cue_p.resolve()
    except OSError:
        pass
    entry.cue = str(cue_p)
    save_index(index)
    return entry


def clear_repo_cue(index: RepoIndex, root: Path | str) -> bool:
    entry = index.find(root)
    if entry is None:
        return False
    if not entry.cue:
        return False
    entry.cue = ""
    save_index(index)
    return True


def set_repo_build(
    index: RepoIndex,
    root: Path | str,
    settings: dict | None,
) -> RepoEntry | None:
    """Persist Build-tab settings for one indexed repo (or clear with None/{})."""
    entry = index.find(root)
    if entry is None:
        return None
    if not settings:
        entry.build = None
    else:
        clean: dict = {}
        for key, val in settings.items():
            if val is None:
                continue
            if isinstance(val, str) and not val.strip():
                continue
            clean[str(key)] = val.strip() if isinstance(val, str) else val
        entry.build = clean or None
    save_index(index)
    return entry


def get_repo_build(index: RepoIndex, root: Path | str) -> dict:
    entry = index.find(root)
    if entry is None:
        return {}
    return entry.build_settings()


def remove_repo(index: RepoIndex, root: Path | str) -> bool:
    try:
        key = str(Path(str(root)).expanduser().resolve())
    except OSError:
        key = str(root).strip()
    before = len(index.repos)
    index.repos = [r for r in index.repos if r.path != key]
    if index.last == key:
        index.last = index.repos[0].path if index.repos else ""
    if len(index.repos) != before:
        save_index(index)
        return True
    return False


def set_last(index: RepoIndex, root: Path | str) -> None:
    try:
        key = str(Path(str(root)).expanduser().resolve())
    except OSError:
        key = str(root).strip()
    if any(r.path == key for r in index.repos):
        index.last = key
        save_index(index)


def labels_for_repos(repos: list[RepoEntry]) -> list[str]:
    """Build unique dropdown labels; disambiguate duplicate names with parent."""
    counts: dict[str, int] = {}
    for r in repos:
        counts[r.display()] = counts.get(r.display(), 0) + 1
    labels: list[str] = []
    for r in repos:
        base = r.display()
        if counts[base] > 1:
            parent = Path(r.path).parent.name
            labels.append(f"{base}  [{parent}]")
        else:
            labels.append(base)
    seen: dict[str, int] = {}
    out: list[str] = []
    for lab in labels:
        if lab not in seen:
            seen[lab] = 0
            out.append(lab)
            continue
        seen[lab] += 1
        out.append(f"{lab}  #{seen[lab]+1}")
    return out


def labels_for(index: RepoIndex) -> list[str]:
    return labels_for_repos(index.repos)


def path_for_label(
    index: RepoIndex,
    label: str,
    *,
    repos: list[RepoEntry] | None = None,
) -> str | None:
    entries = list(repos) if repos is not None else index.repos
    labs = labels_for_repos(entries)
    for lab, entry in zip(labs, entries):
        if lab == label:
            return entry.path
    for entry in entries:
        if entry.display() == label or entry.path == label:
            return entry.path
    return None


def entry_for_label(index: RepoIndex, label: str) -> RepoEntry | None:
    path = path_for_label(index, label)
    if not path:
        return None
    return index.find(path)
