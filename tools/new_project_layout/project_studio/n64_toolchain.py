"""Explicit tool paths for an n64lle port: which Python, C/C++ compiler, CMake,
Ninja, Cargo, git and gh every n64lle command Studio runs is handed.

WHY THIS EXISTS. A machine with several compilers and several Pythons (the
owner's, on Linux and on Windows) could only steer an n64lle build with
environment variables set before Studio started, and the choice was invisible:
nothing on screen said which cc a configure had used, and a terminal build of
the same port could pick a different one. n64lle's scripts now take each tool
explicitly and RECORD the choice in the project (see n64lle
docs/PROJECT-SETUP.md for what each tool is for and how the scripts read the
recorded file). This module is Studio's half of that contract:

* it reads and writes THE SAME recorded file n64lle's scripts read -- not a
  Studio-private copy, so a terminal build and a Studio build of one port agree;
* it resolves each tool to a path and says where the path came from, runs it
  for a version, and reports what is wrong with it per field;
* it builds the flags / -D entries / environment every n64lle command Studio
  runs is given, so the choice is explicit on every command line in the log.

THE CONTRACT IS n64lle's tools/toolchain.sh (feat/explicit-toolchain): its
flag names, environment variables, recorded-file keys and file format are
mirrored in TOOLS below and nowhere else. The file is machine-local (the port
gitignores it) -- it records what THIS machine builds with.

PRECEDENCE, as Studio resolves it:  project file > Studio default > environment
> PATH. The project file is what the user picked for THIS port on the Build tab;
the Studio default is what they picked for every port on this machine; the
environment and PATH are what a shell would have found. n64lle's own scripts
rank the environment ABOVE the project file (flag > env > file > discovery), so
when an environment variable disagrees with the project file the row carries a
warning saying a terminal build would use the environment. Studio passes the
resolved value as an explicit flag, which outranks both on n64lle's side.

Everything that shapes a command line is a pure function of strings, so the
Windows spellings (forward slashes for bash, no ``\\\\?\\`` prefix, spaces kept
inside one argv element) are tested on Linux.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    key: str             # Studio's id, the CLI's --tool value, the JSON key
    label: str           # what the Build tab shows
    flag: str            # n64lle script flag (setup_project.sh / build_framework.sh)
    env: str             # the environment variable n64lle reads for it
    file_var: str        # the key in the project's recorded toolchain file
    cache_var: str       # the port-configure -D that carries it ("" = none)
    names: tuple[str, ...]           # PATH discovery, first hit wins
    scan: tuple[str, ...] = ()       # regexes for Detect's candidate list
    used_by: tuple[str, ...] = ()    # which n64lle steps take it
    is_path: bool = True             # False: a name (the CMake generator)


# One row per n64lle tools/toolchain.sh TC_TABLE row, same flag / environment
# variable / recorded key. `--git-path` / `--gh-path`, not --git / --gh: those
# two are setup_project.sh's "make a git repo" / "create a GitHub repo"
# switches. build_framework.sh takes neither (it runs no git), which is what
# used_by says.
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("python", "Python", "--python", "N64LLE_PYTHON", "Python3_EXECUTABLE",
             "Python3_EXECUTABLE", ("python3", "python"),
             (r"python3(\.\d+)?(\.exe)?", r"python(\.exe)?"),
             ("setup", "framework", "configure", "migrate")),
    ToolSpec("cc", "C compiler", "--cc", "CC", "CMAKE_C_COMPILER",
             "CMAKE_C_COMPILER", ("cc", "gcc", "clang"),
             (r"(x86_64-w64-mingw32-)?gcc(-\d+)?(\.exe)?", r"clang(-\d+)?(\.exe)?",
              r"cc(\.exe)?", r"cl\.exe", r"clang-cl(\.exe)?"),
             ("setup", "framework", "configure")),
    ToolSpec("cxx", "C++ compiler", "--cxx", "CXX", "CMAKE_CXX_COMPILER",
             "CMAKE_CXX_COMPILER", ("c++", "g++", "clang++"),
             (r"(x86_64-w64-mingw32-)?g\+\+(-\d+)?(\.exe)?", r"clang\+\+(-\d+)?(\.exe)?",
              r"c\+\+(\.exe)?", r"cl\.exe", r"clang-cl(\.exe)?"),
             ("setup", "framework", "configure")),
    ToolSpec("cmake", "CMake", "--cmake", "N64LLE_CMAKE", "N64LLE_CMAKE", "",
             ("cmake",), (r"cmake(\.exe)?",),
             ("setup", "framework", "configure")),
    ToolSpec("generator", "Generator", "--generator", "N64LLE_GENERATOR",
             "N64LLE_GENERATOR", "", (), (),
             ("setup", "framework", "configure"), is_path=False),
    ToolSpec("ninja", "Ninja", "--ninja", "N64LLE_NINJA", "N64LLE_MAKE_PROGRAM",
             "CMAKE_MAKE_PROGRAM", ("ninja", "ninja-build"), (r"ninja(-build)?(\.exe)?",),
             ("setup", "framework", "configure")),
    ToolSpec("cargo", "Cargo", "--cargo", "N64LLE_CARGO", "N64LLE_CARGO",
             "N64LLE_CARGO", ("cargo",), (r"cargo(\.exe)?",),
             ("setup", "framework", "configure")),
    ToolSpec("git", "Git", "--git-path", "N64LLE_GIT", "N64LLE_GIT", "",
             ("git",), (r"git(\.exe)?",), ("setup", "migrate")),
    ToolSpec("gh", "GitHub CLI", "--gh-path", "N64LLE_GH", "N64LLE_GH", "",
             ("gh",), (r"gh(\.exe)?",), ("setup",)),
)

# What Detect offers for the generator, which is a name and not a program.
GENERATORS = ("Ninja", "Unix Makefiles", "MinGW Makefiles", "MSYS Makefiles",
              "Visual Studio 17 2022")

BY_KEY: dict[str, ToolSpec] = {t.key: t for t in TOOLS}

# Minimums that are n64lle's, read here only to say "wrong version" next to the
# field instead of three minutes into a configure. Python: the port template's
# find_package(Python3 3.11); CMake: cmake_minimum_required(VERSION 3.20) in
# both the framework and the port template.
MIN_VERSIONS = {"python": (3, 11), "cmake": (3, 20)}

# ---------------------------------------------------------------------------
# The project's recorded file -- THE ONE n64lle's scripts read
# ---------------------------------------------------------------------------
# n64lle's format, fixed in tools/toolchain.sh so other programs can read and
# write it: a CMake initial-cache script, one line per tool,
#
#   set(<KEY> "<value>" CACHE <FILEPATH|STRING> "<doc>")
#
# forward slashes only, and no `"`, `\`, `$` or `;` in a value -- so it needs
# no escaping and one regex reads it. `cmake -C` preloads it, the port's
# CMakeLists include()s it before project(), and every line is a non-FORCE
# cache set, so a -D on a command line still wins. Lines Studio does not own
# are kept verbatim on a rewrite.
PROJECT_FILE = Path("tools") / "toolchain.cmake"

_SET_RE = re.compile(
    r'^\s*set\(\s*([A-Za-z0-9_]+)\s+"([^"]*)"\s+CACHE\s+(?:FILEPATH|PATH|STRING)\b.*\)\s*$'
)
_FORBIDDEN = re.compile(r'["\\$;]')

_FILE_HEADER = """\
# n64lle recorded toolchain -- THIS MACHINE ONLY (gitignored).
#
# Written by Retro Studio (Build tab > Toolchain). One non-FORCE CMake cache
# set per tool: tools/build_framework.sh preloads it with `cmake -C`, and the
# port's CMakeLists include()s it before project(), so the framework, the port
# and every cargo run under them use the same compiler, Python and cargo.
# Change a tool with the matching flag (tools/build_framework.sh --cc <path>)
# or edit a line; n64lle docs/PROJECT-SETUP.md, "Choosing tools".
"""


def project_file(root: Path | str) -> Path:
    return Path(str(root)) / PROJECT_FILE


def parse_project_file(text: str) -> dict[str, str]:
    """``{tool key: path}`` from the recorded file's text."""
    by_var = {t.file_var: t.key for t in TOOLS}
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _SET_RE.match(line)
        if m and m.group(1) in by_var:
            val = m.group(2).strip()
            if val:
                out[by_var[m.group(1)]] = val
    return out


class UnrecordableValue(ValueError):
    """A value n64lle's format cannot carry (a quote, backslash, $ or ;)."""


def check_recordable(key: str, value: str) -> str:
    """The value as it will be recorded, or UnrecordableValue.

    Backslashes become forward slashes first (a Windows path is fine); what
    is left is refused, the same refusal n64lle's tc_write_file makes, so
    Studio never writes a line the scripts would then misread.
    """
    v = cmake_path(value.strip()) if BY_KEY[key].is_path else value.strip()
    if _FORBIDDEN.search(v):
        raise UnrecordableValue(
            f"{BY_KEY[key].label}: {value!r} has a quote, backslash, $ or ; in it, "
            "which n64lle's tools/toolchain.cmake cannot record")
    return v


def render_project_file(values: dict[str, str], previous: str = "") -> str:
    """The file's text for ``values``, keeping every line it does not own."""
    ours = {t.file_var for t in TOOLS}
    kept: list[str] = []
    for line in previous.splitlines():
        m = _SET_RE.match(line)
        if m and m.group(1) in ours:
            continue
        if line.startswith("#"):
            continue  # a header -- ours or n64lle's -- is re-emitted below
        kept.append(line)
    while kept and not kept[0].strip():
        kept.pop(0)
    body = [_FILE_HEADER.rstrip("\n")]
    for t in TOOLS:
        v = (values.get(t.key) or "").strip()
        if not v:
            continue
        v = check_recordable(t.key, v)
        typ = "FILEPATH" if t.is_path else "STRING"
        body.append(f'set({t.file_var:<19} "{v}" CACHE {typ} "{t.label} ({t.flag})")')
    if kept:
        body.append("")
        body.extend(kept)
    return "\n".join(body).rstrip("\n") + "\n"


def read_project(root: Path | str) -> dict[str, str]:
    try:
        return parse_project_file(project_file(root).read_text(encoding="utf-8"))
    except OSError:
        return {}


def write_project(root: Path | str, values: dict[str, str]) -> Path:
    """Record ``values`` (tool key -> path; "" clears) in the port's file.

    An all-empty result removes the file rather than leaving a header that
    records nothing -- "no file" and "nothing recorded" mean the same thing
    to n64lle. Raises UnrecordableValue before touching the file.
    """
    p = project_file(root)
    try:
        prev = p.read_text(encoding="utf-8")
    except OSError:
        prev = ""
    merged = {**parse_project_file(prev), **values}
    merged = {k: v for k, v in merged.items() if (v or "").strip()}
    text = render_project_file(merged, prev)
    if not merged and not [ln for ln in text.splitlines()
                           if ln.strip() and not ln.startswith("#")]:
        if p.is_file():
            p.unlink()
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The Studio-wide default (for new projects, and ports with nothing recorded)
# ---------------------------------------------------------------------------


def _studio_config() -> Path:
    from .retcomm_paths import default_paths

    return default_paths().studio_config_path


def read_studio_defaults(path: Path | None = None) -> dict[str, str]:
    p = path or _studio_config()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    tc = data.get("n64_toolchain") if isinstance(data, dict) else None
    if not isinstance(tc, dict):
        return {}
    return {k: str(v) for k, v in tc.items() if k in BY_KEY and str(v).strip()}


def write_studio_defaults(values: dict[str, str], path: Path | None = None) -> Path:
    """Merge ``values`` into studio.json's ``n64_toolchain`` ("" clears a key)."""
    p = path or _studio_config()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    tc = dict(data.get("n64_toolchain") or {})
    for k, v in values.items():
        if k not in BY_KEY:
            raise KeyError(k)
        if (v or "").strip():
            tc[k] = check_recordable(k, v)
        else:
            tc.pop(k, None)
    data["n64_toolchain"] = tc
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Path spellings -- pure string logic, tested for both hosts on Linux
# ---------------------------------------------------------------------------


def strip_extended(p: str) -> str:
    r"""Drop a Windows extended-length prefix: ``\\?\C:\x`` -> ``C:\x``,
    ``\\?\UNC\srv\share`` -> ``\\srv\share``. Path.resolve() on Windows hands
    these back for long paths, and neither bash nor CMake understands them."""
    for pre in ("\\\\?\\UNC\\", "//?/UNC/"):
        if p.startswith(pre):
            return "\\\\" + p[len(pre):]
    for pre in ("\\\\?\\", "//?/", "\\??\\"):
        if p.startswith(pre):
            return p[len(pre):]
    return p


def cmake_path(p: str) -> str:
    """Forward slashes, no extended prefix. CMake and Git-for-Windows bash both
    accept ``C:/Program Files/LLVM/bin/clang.exe``; a backslash in a -D value
    or a bash argument is an escape, not a separator."""
    return strip_extended(p).replace("\\", "/")


bash_path = cmake_path  # the same spelling; two names for the two consumers


def is_windows_spelling(p: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", p)) or p.startswith("\\\\")


def shell_quote(arg: str) -> str:
    """For the LOG ONLY -- commands are exec'd as argv lists, never via a shell,
    so a space in ``C:/Program Files/...`` is one argument either way. The log
    line has to be pasteable into bash, so it is quoted the way bash reads it."""
    if arg and re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def format_command(argv: list[str], env: dict[str, str] | None = None) -> str:
    pre = " ".join(f"{k}={shell_quote(v)}" for k, v in sorted((env or {}).items()))
    cmd = " ".join(shell_quote(a) for a in argv)
    return f"{pre} {cmd}".strip()


# ---------------------------------------------------------------------------
# Resolution and validation
# ---------------------------------------------------------------------------


@dataclass
class ToolState:
    key: str
    label: str
    path: str = ""          # the resolved path ("" = none found)
    source: str = "none"    # project | studio | env | path | none
    project: str = ""       # what the project file records
    studio: str = ""        # the Studio default
    env: str = ""           # the environment variable's value
    discovered: str = ""    # first PATH hit
    version: str = ""
    error: str = ""
    warning: str = ""
    used_by: list[str] = field(default_factory=list)


SOURCE_LABELS = {
    "project": "project file",
    "studio": "Studio default",
    "env": "environment",
    "path": "PATH",
    "none": "not found",
}


def _which(names: tuple[str, ...], env: dict[str, str]) -> str:
    path = env.get("PATH", os.environ.get("PATH", ""))
    for n in names:
        hit = shutil.which(n, path=path)
        if hit:
            return hit
    return ""


def discover(spec: ToolSpec, env: dict[str, str] | None = None) -> str:
    env = dict(os.environ if env is None else env)
    hit = _which(spec.names, env)
    if not hit and spec.key == "cargo":
        from .n64_paths import find_cargo

        hit = find_cargo() or ""
    return hit


def candidates(spec: ToolSpec, env: dict[str, str] | None = None,
               limit: int = 40) -> list[str]:
    """Every executable on PATH that could be this tool, for Detect's list.

    More than which(): a machine with gcc-13, gcc-14, clang-18 and a MinGW gcc
    wants to SEE them, and which() answers only the first name that resolves.
    """
    if not spec.is_path:
        return list(GENERATORS)
    env = dict(os.environ if env is None else env)
    pats = [re.compile(p + r"$", re.IGNORECASE) for p in spec.scan]
    out: list[str] = []
    seen: set[str] = set()
    dirs = [d for d in env.get("PATH", "").split(os.pathsep) if d]
    if spec.key == "cargo":
        dirs.append(str(Path(env.get("CARGO_HOME") or (Path.home() / ".cargo")) / "bin"))
    for d in dirs:
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for name in entries:
            if not any(p.fullmatch(name) for p in pats):
                continue
            full = os.path.join(d, name)
            if not (os.path.isfile(full) and _executable(full)):
                continue
            try:
                real = os.path.realpath(full)
            except OSError:
                real = full
            if real in seen:
                continue
            seen.add(real)
            out.append(full)
            if len(out) >= limit:
                return out
    return out


def _executable(p: str) -> bool:
    if os.name == "nt":
        return p.lower().endswith((".exe", ".bat", ".cmd", ".com"))
    return os.access(p, os.X_OK)


def _run_version(path: str, key: str, timeout: float = 20.0) -> tuple[str, str]:
    """(first line of `<tool> --version`, error)."""
    argv = [path, "--version"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "", f"`{path} --version` did not finish in {int(timeout)}s"
    except OSError as exc:
        return "", f"could not run: {exc.strerror or exc}"
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if proc.returncode != 0:
        return first, f"`--version` exited {proc.returncode}" + (f": {first}" if first else "")
    return first, ""


def parse_version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return ()
    return tuple(int(g) for g in m.groups() if g is not None)


def compiler_family(path: str, version: str) -> str:
    """gcc | clang | msvc | ""  -- n64lle's _tc_family: clang-cl counts as msvc
    (it speaks cl's command line and the MSVC ABI)."""
    v = (version or "").lower()
    name = os.path.basename(cmake_path(path)).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in ("cl",) or name.startswith("clang-cl"):
        return "msvc"
    if "clang" in v or name.startswith("clang"):
        return "clang"
    if "free software foundation" in v or "gcc" in v or "g++" in v or \
            re.search(r"(^|-)(gcc|g\+\+)", name):
        return "gcc"
    if "microsoft" in v:
        return "msvc"
    return ""


def compiler_abi(family: str, dumpmachine: str) -> str:
    """The Windows C runtime ABI: gnu | msvc | "" (n64lle's TC_CC_ABI)."""
    t = (dumpmachine or "").lower()
    if family == "msvc" or "msvc" in t:
        return "msvc"
    if "mingw" in t or "windows-gnu" in t or "cygwin" in t:
        return "gnu"
    return ""


def rust_abi_mismatch(cc_abi: str, rust_host: str, cc_path: str = "") -> str:
    """n64lle's tc_check_rust_pair, as a message ("" = fine).

    rustc's *-pc-windows-msvc objects do not link into a MinGW link, nor -gnu
    ones into an MSVC one. Either pairing is supported; a MIX is refused.
    Off Windows there is one ABI and nothing to check.
    """
    host = (rust_host or "").strip()
    if not cc_abi or not host:
        return ""
    if host.endswith("-windows-msvc"):
        want = "msvc"
    elif host.endswith("-windows-gnu") or host.endswith("-windows-gnullvm"):
        want = "gnu"
    else:
        return ""
    if want == cc_abi:
        return ""
    arch = host.split("-", 1)[0]
    cc = cc_path or "the C compiler"
    if cc_abi == "gnu":
        return (f"Rust host {host} does not match the C compiler ({cc}: MinGW). "
                f"Run `rustup set default-host {arch}-pc-windows-gnu` and reinstall "
                "the pinned toolchain, or choose an MSVC compiler (cl.exe from a VS "
                "developer shell).")
    return (f"Rust host {host} does not match the C compiler ({cc}: MSVC). "
            f"Run `rustup set default-host {arch}-pc-windows-msvc` and reinstall the "
            "pinned toolchain, or choose a MinGW compiler (e.g. "
            "C:/msys64/mingw64/bin/gcc.exe).")


def cxx_beside(cc: str) -> str:
    """The C++ compiler that belongs to an explicitly chosen C compiler.

    n64lle's _tc_cxx_for: the sibling in the same directory with the same
    prefix and suffix -- --cc /opt/gcc-15/bin/x86_64-w64-mingw32-gcc-15 means
    g++ from the same place, not the first g++ on PATH. Returned as a path
    string whether or not it exists; the caller checks.
    """
    p = cmake_path(cc)
    d, _, base = p.rpartition("/")
    ext = ""
    if base.lower().endswith(".exe"):
        base, ext = base[:-4], base[-4:]
    if base in ("cl", "clang-cl"):
        return p
    if "clang" in base:
        cand = base.replace("clang", "clang++", 1)
    elif "gcc" in base:
        cand = base.replace("gcc", "g++", 1)
    elif base == "cc" or base.endswith("-cc"):
        cand = base[:-2] + "c++"
    else:
        return ""
    return (d + "/" if d else "") + cand + ext


def rust_host(cargo: str, cwd: Path | None = None) -> str:
    """``cargo -vV``'s host triple, asked from ``cwd`` (the n64lle tree, so
    rustup applies rust-toolchain.toml -- the triple the build will use)."""
    if not cargo:
        return ""
    try:
        proc = subprocess.run([strip_extended(cargo), "-vV"], capture_output=True,
                              text=True, timeout=300, cwd=str(cwd) if cwd else None,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    for ln in (proc.stdout or "").splitlines():
        if ln.startswith("host:"):
            return ln.split(":", 1)[1].strip()
    return ""


def _dumpmachine(cc: str) -> str:
    try:
        proc = subprocess.run([strip_extended(cc), "-dumpmachine"], capture_output=True,
                              text=True, timeout=20, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def resolve(
    root: Path | str | None,
    *,
    env: dict[str, str] | None = None,
    studio: dict[str, str] | None = None,
    project: dict[str, str] | None = None,
    versions: bool = True,
    windows: bool | None = None,
    rust_cwd: Path | None = None,
) -> list[ToolState]:
    """Every tool, resolved, with its version and what is wrong with it.

    The layers are n64lle's except for the order of the first two -- see the
    module docstring -- plus two rules copied from tools/toolchain.sh so the
    Build tab shows what the script will do: a C compiler chosen without a
    C++ compiler brings its SIBLING, and the generator is Ninja when a ninja
    is chosen or on PATH, with ninja itself unused under any other generator.
    """
    env = dict(os.environ if env is None else env)
    studio = read_studio_defaults() if studio is None else studio
    project = (read_project(root) if root else {}) if project is None else project
    windows = (os.name == "nt") if windows is None else windows
    by: dict[str, ToolState] = {}
    for t in TOOLS:
        st = ToolState(t.key, t.label, used_by=list(t.used_by))
        st.project = (project.get(t.key) or "").strip()
        st.studio = (studio.get(t.key) or "").strip()
        st.env = (env.get(t.env) or "").strip()
        st.discovered = discover(t, env) if t.is_path else ""
        for src, val in (("project", st.project), ("studio", st.studio), ("env", st.env)):
            if val:
                st.path, st.source = val, src
                break
        by[t.key] = st

    # C++ beside an explicitly chosen C compiler (before discovery fills it).
    cc, cxx = by["cc"], by["cxx"]
    if not cxx.path and cc.path:
        sib = cxx_beside(cc.path)
        if sib and os.path.isfile(strip_extended(sib)):
            cxx.path, cxx.source = sib, "beside"

    # Generator: a name, implied by ninja when not chosen.
    gen, ninja = by["generator"], by["ninja"]
    if not gen.path:
        if ninja.path:
            gen.path, gen.source = "Ninja", "implied"
        elif ninja.discovered:
            gen.path, gen.source = "Ninja", "path"
        else:
            gen.source = "default"

    for t in TOOLS:
        st = by[t.key]
        if not st.path and st.discovered:
            st.path, st.source = st.discovered, "path"
        if t.is_path and st.path and st.source in ("project", "studio", "env"):
            # A bare name ("clang-19") is a PATH lookup, recorded as such.
            if not any(sep in st.path for sep in ("/", "\\")):
                hit = shutil.which(st.path, path=env.get("PATH"))
                if hit:
                    st.path = hit
        if st.env and st.project and st.env != st.project and \
                (not t.is_path or cmake_path(st.env) != cmake_path(st.project)):
            st.warning = (f"${t.env}={st.env} in the environment: a terminal "
                          "build would use that (n64lle ranks the environment "
                          "above the project file). Studio passes the project "
                          "file's value explicitly.")

    if gen.path and gen.path != "Ninja" and ninja.source in ("path", "none", ""):
        ninja.warning = f"not used by the {gen.path} generator"
    for t in TOOLS:
        _validate(t, by[t.key], versions)
    if gen.path and gen.path != "Ninja" and by["ninja"].error and ninja.source == "none":
        ninja.error = ""  # a missing ninja is not an error when nothing uses it

    # Compiler pairing: a gcc beside a clang++ links, then trips over two C++
    # runtimes at the first exception. Said, not refused.
    if cc.path and cxx.path and not cc.error and not cxx.error and versions:
        fc, fx = compiler_family(cc.path, cc.version), compiler_family(cxx.path, cxx.version)
        if fc and fx and fc != fx:
            _warn(cxx, f"C compiler is {fc}, C++ compiler is {fx}: pick one family.")
        vc, vx = parse_version(cc.version), parse_version(cxx.version)
        if fc == fx and vc[:1] and vx[:1] and vc[:1] != vx[:1]:
            _warn(cxx, f"C compiler is {fc} {vc[0]}, C++ compiler is {fx} {vx[0]}.")
    carg = by["cargo"]
    if windows and versions and cc.path and not cc.error and carg.path and not carg.error:
        fam = compiler_family(cc.path, cc.version)
        abi = compiler_abi(fam, "" if fam == "msvc" else _dumpmachine(cc.path))
        msg = rust_abi_mismatch(abi, rust_host(carg.path, rust_cwd), cc.path)
        if msg:
            carg.error = msg  # where n64lle files it: TC_ERR[CARGO]
    return [by[t.key] for t in TOOLS]


def _warn(st: ToolState, msg: str) -> None:
    st.warning = f"{st.warning} {msg}".strip()


def _validate(t: ToolSpec, st: ToolState, versions: bool) -> None:
    if not t.is_path:
        if st.path and _FORBIDDEN.search(st.path):
            st.error = "a generator name cannot contain a quote, backslash, $ or ;"
        return
    if not st.path:
        st.source = "none"
        if t.key == "gh":
            st.warning = "not found -- only needed to create a GitHub repo"
        elif t.key == "ninja":
            st.warning = "not found -- CMake's default generator will be used"
        else:
            st.error = f"not found on PATH (looked for: {' '.join(t.names)})"
        return
    p = strip_extended(st.path)
    if not os.path.exists(p):
        st.error = f"does not exist: {st.path}"
        return
    if os.path.isdir(p):
        st.error = f"is a directory, not a program: {st.path}"
        return
    if not _executable(p):
        st.error = f"not executable: {st.path}"
        return
    if not versions:
        return
    st.version, err = _run_version(p, t.key)
    if err:
        st.error = err
        return
    want = MIN_VERSIONS.get(t.key)
    have = parse_version(st.version)
    if t.key == "python" and not st.version.lower().startswith("python"):
        st.error = f"is not a Python interpreter (printed: {st.version})"
    elif t.key == "cmake" and not st.version.lower().startswith("cmake version"):
        st.error = f"is not CMake (printed: {st.version})"
    elif want and have and have[: len(want)] < want:
        st.error = (f"is {st.version}; n64lle needs "
                    f"{'.'.join(map(str, want))} or newer")
    if t.key in ("cc", "cxx") and st.version and not st.error \
            and not compiler_family(p, st.version):
        _warn(st, "not recognised as gcc, clang or MSVC from its --version banner")


def chosen(states: list[ToolState]) -> dict[str, str]:
    """``{key: value}`` for every tool that resolved, whatever the source."""
    return {s.key: s.path for s in states if s.path}


def explicit(states: list[ToolState]) -> dict[str, str]:
    """Only the tools somebody CHOSE (project file / Studio default / env), plus
    the C++ compiler derived beside a chosen C compiler.

    What Studio passes on a command line. A PATH discovery is left to the
    script's own discovery: passing it would be Studio re-deciding what the
    script decides identically (n64lle then records its own answer in the
    project file, and the Build tab shows it as recorded from then on).
    """
    return {s.key: s.path for s in states
            if s.path and s.source in ("project", "studio", "env", "beside")}


# ---------------------------------------------------------------------------
# Command construction -- pure
# ---------------------------------------------------------------------------
_CASE_ARM_RE = re.compile(r"^\s*((?:--[a-z][a-z0-9-]*)(?:\s*\|\s*--[a-z][a-z0-9-]*)*)\s*\)",
                          re.MULTILINE)


def script_flags(script_text: str) -> set[str]:
    """Every ``--flag`` a bash script's ``case`` arms accept -- including
    each alternative of ``--python|--cc|--cxx)``, the arm n64lle uses."""
    out: set[str] = set()
    for arm in _CASE_ARM_RE.findall(script_text):
        out.update(a.strip() for a in arm.split("|"))
    return out


def script_args(tools: dict[str, str], step: str, supported: set[str] | None,
                ) -> tuple[list[str], dict[str, str], list[str]]:
    """(argv tail, env overlay, dropped) for a bash script of n64lle's.

    A tool whose flag the script ON DISK declares goes as ``--flag <value>``
    (bash spelling: forward slashes, no extended-length prefix; exec'd as one argv
    element, so a space needs no quoting). One it does not declare goes in
    the environment n64lle reads for it -- and an older pin that knows
    neither is named in ``dropped``, so the log can say which choice could
    not be passed rather than claim it was. CC / CXX are CMake's own
    variables, which even a pre-toolchain script's fresh configure honours.
    ``supported=None`` means "assume every flag" (tests, docs).
    """
    argv: list[str] = []
    env: dict[str, str] = {}
    dropped: list[str] = []
    for t in TOOLS:
        v = (tools.get(t.key) or "").strip()
        if not v or step not in t.used_by:
            continue
        spelled = bash_path(v) if t.is_path else v
        if supported is None or t.flag in supported:
            argv += [t.flag, spelled]
        else:
            env[t.env] = spelled
            if t.key not in ("cc", "cxx"):
                dropped.append(t.key)
    return argv, env, dropped


def configure_defines(tools: dict[str, str], *, generator: str = "",
                      project_file_path: str = "") -> list[str]:
    """The port-configure ``-C`` / ``-D`` entries for the chosen tools.

    ``-C <recorded file>`` first: what a terminal configure of a port cut
    before the port template include()d the file needs, and a no-op beside
    that include on a newer one. Then each tool as a -D, the entries n64lle's
    tc_cmake_defs emits, because an initial-cache file does not override a
    value an existing CMakeCache.txt already holds, and the log should name
    the tool either way. CMAKE_MAKE_PROGRAM only under Ninja: it is make's
    path under "Unix Makefiles", and ninja there breaks the build. The
    generator itself is -G, which the caller owns.
    """
    out: list[str] = []
    if project_file_path:
        out += ["-C", cmake_path(project_file_path)]
    for t in TOOLS:
        v = (tools.get(t.key) or "").strip()
        if not v or not t.cache_var or "configure" not in t.used_by:
            continue
        if t.key == "ninja" and not (generator or "").lower().startswith("ninja"):
            continue
        out.append(f"-D{t.cache_var}={cmake_path(v)}")
    return out


def cmake_exe(tools: dict[str, str], fallback: str | None) -> str | None:
    v = (tools.get("cmake") or "").strip()
    return strip_extended(v) if v else fallback


def python_exe(tools: dict[str, str]) -> str:
    """The Python n64lle's own .py helpers (port_drift.py, probe_rom.py) run
    under. Studio's OWN process stays on its interpreter -- it needs Studio's
    packages -- but the framework's scripts get the port's choice."""
    v = (tools.get("python") or "").strip()
    return strip_extended(v) if v else sys.executable


# ---------------------------------------------------------------------------
# One call for the build paths
# ---------------------------------------------------------------------------


def for_root(root: Path | str | None, *, versions: bool = False) -> dict[str, str]:
    """The explicitly chosen tools for a port (no version runs: fast)."""
    return explicit(resolve(root, versions=versions))


SOURCE_LABELS.update({"beside": "beside the C compiler", "implied": "implied by Ninja",
                      "default": "CMake's default"})


def states_json(states: list[ToolState], root: Path | str | None) -> dict:
    return {
        "project_file": str(project_file(root)) if root else "",
        "project_file_exists": bool(root) and project_file(root).is_file(),
        "studio_config": str(_studio_config()),
        "generators": list(GENERATORS),
        "tools": [
            {**asdict(s), "source_label": SOURCE_LABELS.get(s.source, s.source),
             "flag": BY_KEY[s.key].flag, "env_var": BY_KEY[s.key].env,
             "is_path": BY_KEY[s.key].is_path}
            for s in states
        ],
    }
