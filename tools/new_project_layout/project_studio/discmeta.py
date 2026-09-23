"""Disc metadata autofill from digests (Redump / libretro-database / catalog).

Lookup order for a Redump ``.cue``:
1. Local ``retcomm-catalog`` titles (sibling of studio / GitHub) by CRC/MD5/SHA1
2. libretro-database Redump DAT (CRC/MD5/SHA1 → name, region, serial)
3. libretro-database developer DAT (serial → description, publisher, year, players)

Romhacks and non-Redump dumps usually miss digest matches; serial-only
enrichment is attempted when the disc probe can still read a serial.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import toolkit_dir

LIBRETRO_RAW = (
    "https://raw.githubusercontent.com/libretro/libretro-database/master/"
)
REDUMP_DAT = "metadat/redump/Sony - PlayStation.dat"
DEVELOPER_DAT = "metadat/developer/Sony - PlayStation.dat"
USER_AGENT = "retcomm-studio-discmeta/1.0"
CACHE_MAX_AGE_SEC = 7 * 24 * 3600


@dataclass
class DiscMetaHit:
    """Marketing / identity fields resolved for a disc."""

    source: str = ""  # catalog | redump | serial | none
    sources: list[str] = field(default_factory=list)
    name: str = ""
    description: str = ""
    publisher: str = ""
    year: str = ""
    region: str = ""
    players: int | None = None
    serial: str = ""
    crc32: str = ""
    md5: str = ""
    sha1: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def cache_dir() -> Path:
    """Persistent DAT cache under the toolkit (shared for all Studio users)."""
    d = toolkit_dir() / ".cache" / "libretro-database"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _http_get(url: str, *, timeout: float = 90.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _cached_text(rel_path: str, *, force: bool = False) -> str:
    """Download ``rel_path`` from libretro-database into the local cache."""
    # Keep subdirectory in the cache name so redump/developer don't collide.
    safe = rel_path.replace("\\", "/").replace("/", "__")
    dest = cache_dir() / safe
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and not force:
        age = time.time() - dest.stat().st_mtime
        if age < CACHE_MAX_AGE_SEC and dest.stat().st_size > 1024:
            return dest.read_text(encoding="utf-8", errors="replace")
    url = LIBRETRO_RAW + urllib.parse.quote(rel_path)
    data = _http_get(url)
    dest.write_bytes(data)
    return data.decode("utf-8", errors="replace")


def _balanced_block(text: str, open_idx: int) -> tuple[str, int]:
    """Return (inner, end_idx) for ``(... )`` starting at ``open_idx`` (``(``)."""
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != "(":
        raise ValueError("expected '('")
    depth = 0
    i = open_idx
    in_str = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


_KV_RE = re.compile(
    r'(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s+"((?:\\.|[^"\\])*)"',
)
_KV_BARE_RE = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<val>[0-9A-Fa-f]+|\d+)\b",
)


def _parse_kv(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _KV_RE.finditer(block):
        key = m.group("key").lower()
        val = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
        # First wins for name/serial; rom fields collected separately.
        if key not in out:
            out[key] = val
    for m in _KV_BARE_RE.finditer(block):
        key = m.group("key").lower()
        if key not in out:
            out[key] = m.group("val")
    return out


def _iter_game_blocks(text: str):
    needle = "game ("
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            return
        open_paren = idx + len("game ")
        try:
            inner, end = _balanced_block(text, open_paren)
        except ValueError:
            start = idx + len(needle)
            continue
        yield inner
        start = end


def _norm_hex(s: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", (s or "")).lower()


def _norm_serial(s: str) -> str:
    s = (s or "").strip().upper().replace(" ", "")
    # SLUS_005.62 / SLUS-005.62 → SLUS-00562
    m = re.match(r"^([A-Z]{4})[-_]?(\d{3})\.(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}{m.group(3)}"
    m = re.match(r"^([A-Z]{4})[-_]?(\d{5})$", s.replace(".", ""))
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return s.replace("_", "-")


def _parse_users(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    # "1", "2", rarely "1-4"
    m = re.match(r"^(\d+)(?:\s*-\s*(\d+))?$", raw)
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    n = max(lo, hi)
    if 1 <= n <= 8:
        return n
    return None


@dataclass
class _RedumpIndexes:
    by_crc: dict[str, dict[str, str]] = field(default_factory=dict)
    by_md5: dict[str, dict[str, str]] = field(default_factory=dict)
    by_sha1: dict[str, dict[str, str]] = field(default_factory=dict)
    by_serial: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class _DevIndexes:
    by_serial: dict[str, dict[str, str]] = field(default_factory=dict)
    by_name: dict[str, dict[str, str]] = field(default_factory=dict)


_redump_cache: _RedumpIndexes | None = None
_dev_cache: _DevIndexes | None = None


def _load_redump(*, force: bool = False) -> _RedumpIndexes:
    global _redump_cache
    if _redump_cache is not None and not force:
        return _redump_cache
    text = _cached_text(REDUMP_DAT, force=force)
    idx = _RedumpIndexes()
    for block in _iter_game_blocks(text):
        top = _parse_kv(block)
        # Prefer digests from the first rom ( sub-block )
        rom_kv: dict[str, str] = {}
        ridx = block.find("rom (")
        if ridx >= 0:
            try:
                rom_inner, _ = _balanced_block(block, ridx + len("rom "))
                rom_kv = _parse_kv(rom_inner)
            except ValueError:
                rom_kv = {}
        entry = {
            "name": top.get("name") or rom_kv.get("name") or "",
            "region": top.get("region") or "",
            "serial": _norm_serial(top.get("serial") or rom_kv.get("serial") or ""),
            "crc": _norm_hex(rom_kv.get("crc") or ""),
            "md5": _norm_hex(rom_kv.get("md5") or ""),
            "sha1": _norm_hex(rom_kv.get("sha1") or ""),
            "size": rom_kv.get("size") or "",
        }
        if entry["crc"] and entry["crc"] not in idx.by_crc:
            idx.by_crc[entry["crc"]] = entry
        if entry["md5"] and entry["md5"] not in idx.by_md5:
            idx.by_md5[entry["md5"]] = entry
        if entry["sha1"] and entry["sha1"] not in idx.by_sha1:
            idx.by_sha1[entry["sha1"]] = entry
        if entry["serial"] and entry["serial"] not in idx.by_serial:
            idx.by_serial[entry["serial"]] = entry
    _redump_cache = idx
    return idx


def _load_developer(*, force: bool = False) -> _DevIndexes:
    global _dev_cache
    if _dev_cache is not None and not force:
        return _dev_cache
    text = _cached_text(DEVELOPER_DAT, force=force)
    idx = _DevIndexes()
    for block in _iter_game_blocks(text):
        top = _parse_kv(block)
        serial = _norm_serial(top.get("serial") or "")
        if not serial:
            ridx = block.find("rom (")
            if ridx >= 0:
                try:
                    rom_inner, _ = _balanced_block(block, ridx + len("rom "))
                    serial = _norm_serial(_parse_kv(rom_inner).get("serial") or "")
                except ValueError:
                    serial = ""
        entry = {
            "name": top.get("name") or "",
            "description": top.get("description") or "",
            "publisher": top.get("publisher") or "",
            "developer": top.get("developer") or "",
            "year": top.get("releaseyear") or top.get("year") or "",
            "users": top.get("users") or "",
            "serial": serial,
        }
        if serial and serial not in idx.by_serial:
            idx.by_serial[serial] = entry
        name = (entry["name"] or "").strip().lower()
        if name and name not in idx.by_name:
            idx.by_name[name] = entry
    _dev_cache = idx
    return idx


def find_catalog_roots() -> list[Path]:
    """Likely local checkouts of retcomm-catalog + shared Retro cache."""
    roots: list[Path] = []
    try:
        from project_studio.catalog_sync import catalog_cache_valid
        from project_studio.retcomm_paths import default_paths

        paths = default_paths()
        if catalog_cache_valid(paths):
            roots.append(paths.catalog_dir.resolve())
    except Exception:
        pass
    toolkit = toolkit_dir()
    for base in (
        toolkit.parent.parent,  # …/retcomm-studio
        toolkit.parent.parent.parent,  # …/GitHub
    ):
        for name in ("retcomm-catalog", "Retro-catalog"):
            p = base / name
            if (p / "titles").is_dir() or (p / "index.json").is_file():
                roots.append(p.resolve())
    # De-dupe
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _catalog_hit(
    *,
    crc32: str,
    md5: str,
    sha1: str,
) -> dict[str, Any] | None:
    crc32, md5, sha1 = _norm_hex(crc32), _norm_hex(md5), _norm_hex(sha1)
    for root in find_catalog_roots():
        titles = root / "titles"
        if not titles.is_dir():
            continue
        for path in titles.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            ident = data.get("rom_identity") or {}
            digests = {
                "crc32": [_norm_hex(x) for x in (ident.get("crc32") or [])],
                "md5": [_norm_hex(x) for x in (ident.get("md5") or [])],
                "sha1": [_norm_hex(x) for x in (ident.get("sha1") or [])],
            }
            # Also accept nested data_track from probe catalog_identity
            dt = ident.get("data_track") or {}
            if isinstance(dt, dict):
                if dt.get("crc32"):
                    digests["crc32"].append(_norm_hex(str(dt["crc32"])))
                if dt.get("md5"):
                    digests["md5"].append(_norm_hex(str(dt["md5"])))
                if dt.get("sha1"):
                    digests["sha1"].append(_norm_hex(str(dt["sha1"])))
            match = False
            if crc32 and crc32 in digests["crc32"]:
                match = True
            if md5 and md5 in digests["md5"]:
                match = True
            if sha1 and sha1 in digests["sha1"]:
                match = True
            if not match:
                continue
            marketing = data.get("marketing") or {}
            serials = ident.get("disc_serials") or []
            serial = ""
            if isinstance(serials, list) and serials:
                serial = _norm_serial(str(serials[0]))
            players = marketing.get("players")
            try:
                players_i = int(players) if players is not None else None
            except (TypeError, ValueError):
                players_i = None
            return {
                "name": (data.get("name") or "").strip(),
                "description": (
                    marketing.get("description") or data.get("description") or ""
                ).strip(),
                "publisher": (marketing.get("publisher") or "").strip(),
                "year": str(marketing.get("year") or "").strip(),
                "region": (marketing.get("region") or "").strip(),
                "players": players_i,
                "serial": serial,
                "path": str(path),
            }
    return None


def lookup_digests(
    *,
    crc32: str = "",
    md5: str = "",
    sha1: str = "",
    serial: str = "",
    force_refresh: bool = False,
) -> DiscMetaHit:
    """Resolve metadata from digests and/or disc serial."""
    hit = DiscMetaHit(
        crc32=_norm_hex(crc32),
        md5=_norm_hex(md5),
        sha1=_norm_hex(sha1),
        serial=_norm_serial(serial),
    )
    cat = _catalog_hit(crc32=hit.crc32, md5=hit.md5, sha1=hit.sha1)
    if cat:
        hit.source = "catalog"
        hit.sources.append("catalog")
        hit.name = cat.get("name") or hit.name
        hit.description = cat.get("description") or hit.description
        hit.publisher = cat.get("publisher") or hit.publisher
        hit.year = cat.get("year") or hit.year
        hit.region = cat.get("region") or hit.region
        if cat.get("players") is not None:
            hit.players = int(cat["players"])
        if cat.get("serial"):
            hit.serial = cat["serial"]
        hit.notes.append(f"Matched retcomm-catalog: {cat.get('path')}")

    redump_entry: dict[str, str] | None = None
    try:
        redump = _load_redump(force=force_refresh)
        if hit.sha1 and hit.sha1 in redump.by_sha1:
            redump_entry = redump.by_sha1[hit.sha1]
        elif hit.md5 and hit.md5 in redump.by_md5:
            redump_entry = redump.by_md5[hit.md5]
        elif hit.crc32 and hit.crc32 in redump.by_crc:
            redump_entry = redump.by_crc[hit.crc32]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        hit.notes.append(f"Redump DAT fetch failed: {exc}")

    if redump_entry:
        hit.sources.append("redump")
        if not hit.source:
            hit.source = "redump"
        hit.name = hit.name or redump_entry.get("name") or ""
        hit.region = hit.region or redump_entry.get("region") or ""
        if redump_entry.get("serial"):
            hit.serial = hit.serial or redump_entry["serial"]
        hit.notes.append(
            f"Matched libretro Redump DAT ({redump_entry.get('name', '?')})"
        )
    elif hit.crc32 or hit.md5 or hit.sha1:
        hit.notes.append(
            "No Redump digest match (romhack / bad dump / non-Redump cue?)"
        )

    # Developer / marketing by serial (or name)
    try:
        dev = _load_developer(force=force_refresh)
        entry = None
        if hit.serial and hit.serial in dev.by_serial:
            entry = dev.by_serial[hit.serial]
        elif hit.name:
            entry = dev.by_name.get(hit.name.strip().lower())
        if entry:
            hit.sources.append("libretro-developer")
            if not hit.source or hit.source == "none":
                hit.source = "serial"
            hit.description = hit.description or entry.get("description") or ""
            hit.publisher = hit.publisher or entry.get("publisher") or ""
            hit.year = hit.year or entry.get("year") or ""
            if hit.players is None:
                hit.players = _parse_users(entry.get("users") or "")
            hit.name = hit.name or entry.get("name") or ""
            hit.notes.append(
                f"Enriched from libretro developer DAT ({entry.get('name', '?')})"
            )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        hit.notes.append(f"Developer DAT fetch failed: {exc}")

    if not hit.sources:
        hit.source = "none"
        hit.notes.append("No metadata sources matched")
    return hit


def lookup_cue(
    cue_path: str | Path,
    *,
    force_refresh: bool = False,
) -> DiscMetaHit:
    """Probe a ``.cue`` then look up public metadata."""
    import importlib.util
    import sys

    cue = Path(cue_path).expanduser().resolve()
    if not cue.is_file():
        hit = DiscMetaHit(source="none")
        hit.notes.append(f"Cue not found: {cue}")
        return hit

    probe_py = toolkit_dir() / "probe_disc.py"
    spec = importlib.util.spec_from_file_location("probe_disc_toolkit", probe_py)
    if spec is None or spec.loader is None:
        hit = DiscMetaHit(source="none")
        hit.notes.append("probe_disc.py missing")
        return hit
    mod = importlib.util.module_from_spec(spec)
    # Avoid clobbering a previously imported module name
    sys.modules["probe_disc_toolkit"] = mod
    spec.loader.exec_module(mod)
    probe = mod.probe(cue)

    hit = lookup_digests(
        crc32=getattr(probe, "data_track_crc32", "") or "",
        md5=probe.data_track_md5,
        sha1=probe.data_track_sha1,
        serial=probe.serial or "",
        force_refresh=force_refresh,
    )
    if not hit.name and probe.display_name:
        hit.name = probe.display_name
        hit.notes.append("Name from disc volume / probe display_name")
    if not hit.serial and probe.serial:
        hit.serial = _norm_serial(probe.serial)
    hit.crc32 = hit.crc32 or getattr(probe, "data_track_crc32", "") or ""
    hit.md5 = hit.md5 or probe.data_track_md5
    hit.sha1 = hit.sha1 or probe.data_track_sha1
    return hit


def lookup_rom(
    rom_path: str | Path,
    *,
    force_refresh: bool = False,
) -> DiscMetaHit:
    """Cartridge counterpart of lookup_cue: digest the ROM, then look it up.

    Publisher, developer and year come from libretro-database's per-system
    metadata DATs, which are keyed by the ROM's CRC32 -- through the SNES
    wizard's own fetch_metadata.py, so Studio and a terminal scaffold agree.
    A marketing description comes only from the local catalog, when it knows
    the title; libretro has none, which is why the wizard asks for one after.
    """
    import hashlib
    import importlib.util
    import zlib

    from . import snes_paths

    rom = Path(rom_path).expanduser().resolve()
    hit = DiscMetaHit(source="none")
    if not rom.is_file():
        hit.notes.append(f"ROM not found: {rom}")
        return hit
    raw = rom.read_bytes()
    if len(raw) % 1024 == 512:
        raw = raw[512:]   # copier header: not part of the image's identity
        hit.notes.append("512-byte copier header skipped for the digests")
    crc = "%08x" % (zlib.crc32(raw) & 0xFFFFFFFF)
    md5 = hashlib.md5(raw).hexdigest()
    sha1 = hashlib.sha1(raw).hexdigest()
    hit.crc32, hit.md5, hit.sha1 = crc, md5, sha1
    # The catalog, by digest -- not lookup_digests(), whose other source is
    # the Redump PlayStation DAT and whose notes talk about discs.
    cat = _catalog_hit(crc32=crc, md5=md5, sha1=sha1)
    if cat:
        hit.source = "catalog"
        hit.sources.append("catalog")
        hit.name = cat.get("name") or ""
        hit.description = cat.get("description") or ""
        hit.publisher = cat.get("publisher") or ""
        hit.year = cat.get("year") or ""
        hit.region = cat.get("region") or ""
        hit.notes.append("catalog: matched by ROM digest")

    wizard = snes_paths.wizard_dir(None)
    fetcher = wizard / "fetch_metadata.py" if wizard is not None else None
    if fetcher is not None and fetcher.is_file():
        try:
            spec = importlib.util.spec_from_file_location("snes_fetch_metadata", fetcher)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            assert spec and spec.loader
            spec.loader.exec_module(mod)
            meta = mod.lookup(crc, force=force_refresh)
        except Exception as exc:  # network, parse: say so, do not fail the scaffold
            meta = {}
            hit.notes.append(f"libretro-database lookup failed: {exc}")
        if meta:
            hit.sources.append("libretro-database")
            if hit.source == "none":
                hit.source = "libretro-database"
            hit.name = hit.name or meta.get("name", "")
            hit.publisher = hit.publisher or meta.get("publisher", "")
            hit.year = hit.year or str(meta.get("year", ""))
            hit.notes.append(
                f"libretro-database: publisher={meta.get('publisher') or '-'} "
                f"developer={meta.get('developer') or '-'} year={meta.get('year') or '-'}")
            # The catalog's own description wins; Wikipedia's summary (found
            # by the No-Intro title the CRC32 resolved to) fills the gap.
            if not hit.description and meta.get("description"):
                hit.description = meta["description"]
                hit.notes.append(f"description: Wikipedia, {meta.get('description_source', '')}")
        else:
            hit.notes.append(f"no libretro-database entry for crc32 {crc}")
    else:
        hit.notes.append("fetch_metadata.py not found in the SNES wizard; libretro lookup skipped")
    if not hit.name:
        hit.name = re.sub(r"\s*\([^)]*\)\s*", " ", rom.stem).strip()
    return hit


def suggest_project_name(display: str) -> str:
    """Rough folder name from a Redump / catalog title."""
    stem = re.sub(r"\s*\([^)]*\)\s*", " ", display or "")
    stem = "".join(c if c.isalnum() else "" for c in stem).strip()
    if not stem:
        return ""
    return stem[:48] + "Recomp"


def apply_hit_to_options(opts: Any, hit: DiscMetaHit, *, only_empty: bool = True) -> list[str]:
    """Copy hit fields onto a ``NewProjectOptions``-like object. Returns filled keys."""
    filled: list[str] = []

    def empty(attr: str) -> bool:
        cur = getattr(opts, attr, None)
        if attr == "players":
            # Wizard default is 2; treat as unset for autofill.
            return cur in (None, 0, 2) if only_empty else True
        if only_empty:
            return cur in (None, "")
        return True

    def take(attr: str, value: Any) -> None:
        if value is None or value == "":
            return
        if only_empty and not empty(attr):
            return
        setattr(opts, attr, value)
        filled.append(attr)

    if hit.players is not None:
        take("players", int(hit.players))
    take("description", hit.description)
    take("publisher", hit.publisher)
    take("year", str(hit.year) if hit.year else "")
    take("region", hit.region)
    return filled
