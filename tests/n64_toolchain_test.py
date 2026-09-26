#!/usr/bin/env python3
"""Headless check of the n64lle Toolchain (Build tab > Toolchain).

What is asserted, and why each is worth a test:

  * the recorded file round-trips in n64lle's OWN format -- a file exactly as
    n64lle's tools/toolchain.sh tc_write_file prints it parses, and what Studio
    writes parses back with the same keys -- because the whole point of the
    file is that a terminal build and a Studio build read the same thing
  * values n64lle's format cannot carry (a quote, $ or ;) are refused before
    the file is touched, and a Windows backslash path is recorded with
    forward slashes rather than refused
  * the Studio-wide default round-trips through studio.json without eating the
    file's other keys
  * resolution precedence (project file > Studio default > environment > PATH),
    the C++ compiler derived beside a chosen C compiler, the generator implied
    by ninja, and per-field errors -- against fake tools in a temp PATH
  * command construction as pure strings, for Linux AND Windows spellings:
    forward slashes where bash consumes them, never a \\\\?\\ prefix, a path with
    spaces kept as ONE argv element; the flags a script declares (including
    every alternative of `--python|--cc|--cxx)`), and the environment fallback
    for a pin that predates them
  * the three Studio call sites end to end as dry runs on a synthetic port:
    `build framework`, `build configure`, and `new-project`

Run:  python3 tests/n64_toolchain_test.py
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "tools" / "new_project_layout"))

from project_studio import n64_toolchain as tc  # noqa: E402
from project_studio import platforms  # noqa: E402

failures = 0


def check(cond: bool, what: str) -> None:
    global failures
    if cond:
        print(f"  ok    {what}")
    else:
        print(f"  FAIL  {what}")
        failures += 1


# Exactly what n64lle's tc_write_file prints (feat/explicit-toolchain 1d04ea3e).
N64LLE_WRITTEN = """\
# n64lle recorded toolchain -- THIS MACHINE ONLY (gitignored).
#
# Written by n64lle tools/build_framework.sh on 2026-09-25. One non-FORCE CMake cache set per tool:
set(CMAKE_C_COMPILER    "/usr/bin/cc" CACHE FILEPATH "C compiler (--cc)")
set(CMAKE_CXX_COMPILER  "/usr/bin/c++" CACHE FILEPATH "C++ compiler (--cxx)")
set(Python3_EXECUTABLE  "/usr/bin/python3" CACHE FILEPATH "Python (--python)")
set(N64LLE_CMAKE        "/usr/bin/cmake" CACHE FILEPATH "CMake (--cmake)")
set(N64LLE_GENERATOR    "Ninja" CACHE STRING "generator (--generator)")
set(N64LLE_MAKE_PROGRAM "/usr/bin/ninja" CACHE FILEPATH "ninja (--ninja)")
set(N64LLE_CARGO        "/home/u/.cargo/bin/cargo" CACHE FILEPATH "cargo (--cargo)")
set(N64LLE_GIT          "/usr/bin/git" CACHE FILEPATH "git (--git-path)")
"""


def fake_tool(d: Path, name: str, version_line: str, *, exe: bool = True) -> Path:
    p = d / name
    p.write_text(f"#!/bin/sh\necho '{version_line}'\n")
    if exe:
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_file_format(tmp: Path) -> None:
    print("recorded file (n64lle's format)")
    got = tc.parse_project_file(N64LLE_WRITTEN)
    check(got == {"cc": "/usr/bin/cc", "cxx": "/usr/bin/c++", "python": "/usr/bin/python3",
                  "cmake": "/usr/bin/cmake", "generator": "Ninja", "ninja": "/usr/bin/ninja",
                  "cargo": "/home/u/.cargo/bin/cargo", "git": "/usr/bin/git"},
          "a file n64lle wrote parses to every key")

    port = tmp / "port"
    (port / "tools").mkdir(parents=True)
    tc.project_file(port).write_text(N64LLE_WRITTEN + "set(SOMETHING_ELSE \"x\" CACHE STRING \"\")\n")
    tc.write_project(port, {"cc": "/usr/bin/clang", "cxx": "", "gh": "/usr/bin/gh"})
    text = tc.project_file(port).read_text()
    back = tc.read_project(port)
    check(back.get("cc") == "/usr/bin/clang", "set: the changed key is recorded")
    check("cxx" not in back, "set: an empty value clears the key")
    check(back.get("python") == "/usr/bin/python3" and back.get("cargo", "").endswith("cargo"),
          "set: untouched keys survive")
    check('set(SOMETHING_ELSE "x" CACHE STRING "")' in text, "set: a line Studio does not own is kept")
    check('set(CMAKE_C_COMPILER    "/usr/bin/clang" CACHE FILEPATH "C compiler (--cc)")' in text,
          "written line is n64lle's exact shape")
    check('"Ninja" CACHE STRING' in text, "the generator is a STRING, not a FILEPATH")
    check(text.count("THIS MACHINE ONLY") == 1, "one header, not one per rewrite")
    for line in text.splitlines():
        if line.startswith("set("):
            val = line.split('"')[1]
            if any(c in val for c in '"\\$;'):
                check(False, f"no forbidden character in a written value: {line}")

    tc.write_project(port, {"cc": r"C:\Program Files\LLVM\bin\clang.exe"})
    check(tc.read_project(port)["cc"] == "C:/Program Files/LLVM/bin/clang.exe",
          "a Windows path is recorded with forward slashes")
    try:
        tc.write_project(port, {"cc": "/opt/$weird/cc"})
        check(False, "a $ in a value is refused")
    except tc.UnrecordableValue:
        check(tc.read_project(port)["cc"] == "C:/Program Files/LLVM/bin/clang.exe",
              "a $ in a value is refused, and the file is untouched")

    empty = tmp / "empty"
    (empty / "tools").mkdir(parents=True)
    tc.write_project(empty, {"cc": "/usr/bin/cc"})
    tc.write_project(empty, {"cc": ""})
    check(not tc.project_file(empty).exists(), "clearing the last key removes the file")


def test_studio_defaults(tmp: Path) -> None:
    print("Studio-wide default (studio.json)")
    cfg = tmp / "studio.json"
    cfg.write_text(json.dumps({"other": {"keep": 1}}))
    tc.write_studio_defaults({"cc": r"C:\msys64\ucrt64\bin\gcc.exe", "python": "/usr/bin/python3"},
                             cfg)
    tc.write_studio_defaults({"python": ""}, cfg)
    data = json.loads(cfg.read_text())
    check(data.get("other") == {"keep": 1}, "other studio.json keys survive")
    check(tc.read_studio_defaults(cfg) == {"cc": "C:/msys64/ucrt64/bin/gcc.exe"},
          "round-trip: set, clear, forward slashes")


def test_paths() -> None:
    print("path spellings (pure strings, both hosts)")
    cases = {
        r"\\?\C:\Program Files\LLVM\bin\clang.exe": "C:/Program Files/LLVM/bin/clang.exe",
        r"\\?\UNC\build\share\gcc.exe": "//build/share/gcc.exe",
        r"C:\msys64\ucrt64\bin\gcc.exe": "C:/msys64/ucrt64/bin/gcc.exe",
        "C:/already/fine.exe": "C:/already/fine.exe",
        "/usr/bin/gcc-15": "/usr/bin/gcc-15",
        "//?/C:/x/y.exe": "C:/x/y.exe",
    }
    for raw, want in cases.items():
        got = tc.cmake_path(raw)
        check(got == want and "?" not in got, f"{raw!r} -> {want!r} (got {got!r})")
    check(tc.shell_quote("C:/Program Files/x.exe") == "'C:/Program Files/x.exe'",
          "log spelling quotes a path with a space")
    check(tc.cxx_beside("C:/msys64/mingw64/bin/x86_64-w64-mingw32-gcc-15.exe")
          == "C:/msys64/mingw64/bin/x86_64-w64-mingw32-g++-15.exe", "C++ beside a MinGW gcc")
    check(tc.cxx_beside("/usr/bin/clang-22") == "/usr/bin/clang++-22", "C++ beside clang-22")
    check(tc.cxx_beside(r"C:\VS\bin\cl.exe") == "C:/VS/bin/cl.exe", "cl is its own C++ compiler")


def test_windows_abi() -> None:
    print("Windows compiler <-> Rust target (pure)")
    check(tc.compiler_abi("gcc", "x86_64-w64-mingw32") == "gnu", "MinGW gcc is gnu")
    check(tc.compiler_abi("msvc", "") == "msvc", "cl is msvc")
    check(tc.compiler_family(r"C:\LLVM\bin\clang-cl.exe", "clang version 19") == "msvc",
          "clang-cl counts as msvc")
    check(tc.compiler_abi("gcc", "x86_64-pc-linux-gnu") == "", "Linux has no ABI to pair")
    check(tc.rust_abi_mismatch("gnu", "x86_64-pc-windows-gnu") == "", "gnu + -gnu is fine")
    check(tc.rust_abi_mismatch("msvc", "x86_64-pc-windows-msvc") == "", "msvc + -msvc is fine")
    m = tc.rust_abi_mismatch("gnu", "x86_64-pc-windows-msvc", "C:/msys64/mingw64/bin/gcc.exe")
    check("rustup set default-host x86_64-pc-windows-gnu" in m, "MinGW + -msvc names the rustup fix")
    m = tc.rust_abi_mismatch("msvc", "x86_64-pc-windows-gnu")
    check("x86_64-pc-windows-msvc" in m, "MSVC + -gnu names the other fix")
    check(tc.rust_abi_mismatch("", "x86_64-unknown-linux-gnu") == "", "nothing to check off Windows")


def test_resolve(tmp: Path) -> None:
    print("resolution against fake tools")
    bind = tmp / "bin"
    alt = tmp / "alt"
    bind.mkdir()
    alt.mkdir()
    fake_tool(bind, "python3", "Python 3.12.4")
    fake_tool(bind, "cc", "cc (GCC) 15.1.0")
    fake_tool(bind, "c++", "c++ (GCC) 15.1.0")
    fake_tool(bind, "cmake", "cmake version 3.31.2")
    fake_tool(bind, "ninja", "1.12.1")
    fake_tool(bind, "cargo", "cargo 1.96.0")
    fake_tool(bind, "git", "git version 2.50.0")
    fake_tool(alt, "clang-19", "clang version 19.1.0")
    fake_tool(alt, "clang++-19", "clang version 19.1.0")
    fake_tool(alt, "python3.10", "Python 3.10.14")
    fake_tool(alt, "cmake-old", "cmake version 3.16.3")
    noexec = fake_tool(alt, "notexec", "x", exe=False)
    env = {"PATH": str(bind), "HOME": str(tmp)}

    st = {s.key: s for s in tc.resolve(None, env=env, studio={}, project={}, windows=False)}
    check(st["cc"].source == "path" and st["cc"].path == str(bind / "cc"), "PATH discovery")
    check(st["cc"].version == "cc (GCC) 15.1.0" and not st["cc"].error, "version is run and read")
    check(st["generator"].path == "Ninja", "Ninja on PATH implies the Ninja generator")
    check(st["gh"].source == "none" and not st["gh"].error and st["gh"].warning,
          "a missing gh is a warning, not an error")

    env2 = dict(env, CC=str(bind / "cc"), N64LLE_PYTHON=str(alt / "python3.10"))
    st = {s.key: s for s in tc.resolve(
        None, env=env2, windows=False,
        studio={"cc": str(alt / "clang-19")},
        project={"python": str(bind / "python3"), "cmake": str(alt / "cmake-old"),
                 "git": str(noexec), "cargo": str(alt / "nope")})}
    check(st["cc"].source == "studio" and st["cc"].path.endswith("clang-19"),
          "Studio default beats the environment")
    check(st["cxx"].source == "beside" and st["cxx"].path.endswith("clang++-19"),
          "C++ compiler derived beside the chosen C compiler")
    check(st["python"].source == "project" and not st["python"].error,
          "project file beats the environment")
    check("terminal build would use that" in st["python"].warning,
          "an environment value that disagrees with the project file is flagged")
    check("n64lle needs 3.20" in st["cmake"].error, "a too-old CMake is a per-field error")
    check(st["git"].error.startswith("not executable"), "a non-executable file is refused")
    check(st["cargo"].error.startswith("does not exist"), "a missing path is refused")

    # cargo's version: the one the build gets (asked outside the tree, as
    # n64lle's resolver asks), plus a warning when the pin inside differs.
    tree = tmp / "n64lle-tree"
    tree.mkdir()
    pwdcargo = alt / "cargo-pwd"   # a rustup proxy: its toolchain depends on $PWD
    pwdcargo.write_text('#!/bin/sh\n[ "$(basename "$PWD")" = n64lle-tree ] '
                        '&& echo "cargo 1.96.0 (pin)" || echo "cargo 1.93.1 (default)"\n')
    pwdcargo.chmod(0o755)
    st = {s.key: s for s in tc.resolve(None, env=env, studio={}, windows=False,
                                        project={"cargo": str(pwdcargo)}, rust_cwd=tree)}
    check(st["cargo"].version == "cargo 1.93.1 (default)",
          "cargo's version is the one the BUILD gets (asked outside the n64lle tree)")
    check("rust-toolchain.toml pin (cargo 1.96.0 (pin))" in st["cargo"].warning
          and "rustup default 1.96.0" in st["cargo"].warning,
          "a pin that differs from the build's toolchain is named, with the fix")
    st = {s.key: s for s in tc.resolve(None, env=env, studio={}, windows=False,
                                        project={"cargo": str(bind / "cargo")}, rust_cwd=tree)}
    check(not st["cargo"].warning, "no warning when the pin and the build agree")

    st = {s.key: s for s in tc.resolve(None, env=env, studio={}, windows=False,
                                        project={"python": str(alt / "python3.10")})}
    check("n64lle needs 3.11" in st["python"].error, "Python older than 3.11 is refused")

    st = tc.resolve(None, env=env, studio={"cc": str(alt / "clang-19")},
                    project={"generator": "Unix Makefiles"}, windows=False, versions=False)
    ex = tc.explicit(st)
    check(ex.get("cc", "").endswith("clang-19") and ex.get("cxx", "").endswith("clang++-19")
          and ex.get("generator") == "Unix Makefiles" and "python" not in ex,
          "explicit(): chosen + derived, never a PATH discovery")
    ninja = next(s for s in st if s.key == "ninja")
    check("not used by the Unix Makefiles generator" in ninja.warning,
          "ninja is flagged unused under another generator")


def test_commands() -> None:
    print("command construction (pure)")
    new_arm = """
    case "$a" in
      --test)    RUN_TESTS="--test"; shift ;;
      --toolchain-file)
        TOOLFILE="$2"; shift 2 ;;
      --python|--cc|--cxx|--cmake|--generator|--ninja|--cargo)
        tc_set_flag "$(tc_flag_key "$a")" "$2"; shift 2 ;;
    """
    flags = tc.script_flags(new_arm)
    check({"--python", "--cc", "--cxx", "--cmake", "--generator", "--ninja", "--cargo",
           "--test", "--toolchain-file"} <= flags, "every alternative of a case arm is read")
    old_flags = tc.script_flags('case "$a" in\n  --test) x ;;\n  --profile) y ;;\n  --core) z ;;\n')

    win = {"cc": r"\\?\C:\Program Files\LLVM\bin\clang.exe",
           "cxx": r"C:\Program Files\LLVM\bin\clang++.exe",
           "python": r"C:\Python313\python.exe", "generator": "Ninja",
           "cargo": r"C:\Users\A B\.cargo\bin\cargo.exe", "git": r"C:\Git\cmd\git.exe"}
    argv, envo, dropped = tc.script_args(win, "framework", flags)
    check(argv[argv.index("--cc") + 1] == "C:/Program Files/LLVM/bin/clang.exe",
          "Windows: --cc is forward-slashed, no \\\\?\\ prefix, one argv element")
    check(argv[argv.index("--cargo") + 1] == "C:/Users/A B/.cargo/bin/cargo.exe",
          "Windows: a space stays inside one argument")
    check("--git-path" not in argv and "--git" not in argv,
          "the framework build is not handed git (it runs none)")
    check(not envo and not dropped, "a current script takes every tool as a flag")
    check(all("\\" not in a for a in argv), "no backslash anywhere on the bash command line")

    argv, envo, dropped = tc.script_args({"git": "/usr/bin/git", "gh": "/usr/bin/gh",
                                          "cc": "/usr/bin/clang"}, "setup",
                                         {"--git-path", "--gh-path", "--cc", "--git", "--gh"})
    check(argv == ["--cc", "/usr/bin/clang", "--git-path", "/usr/bin/git", "--gh-path",
                   "/usr/bin/gh"], "setup: git/gh go as --git-path/--gh-path, never --git/--gh")

    argv, envo, dropped = tc.script_args({"cc": "/usr/bin/clang", "python": "/usr/bin/python3"},
                                         "framework", old_flags)
    check(argv == [] and envo == {"CC": "/usr/bin/clang", "N64LLE_PYTHON": "/usr/bin/python3"},
          "an older pin gets the environment instead of a flag it would exit 2 on")
    check(dropped == ["python"], "...and the choice it cannot read is named, CC is not")

    defs = tc.configure_defines(win, generator="Ninja",
                                project_file_path=r"C:\ports\Glover Recomp\tools\toolchain.cmake")
    check(defs[:2] == ["-C", "C:/ports/Glover Recomp/tools/toolchain.cmake"],
          "configure: -C <recorded file> first, forward-slashed")
    check("-DCMAKE_C_COMPILER=C:/Program Files/LLVM/bin/clang.exe" in defs,
          "configure: -DCMAKE_C_COMPILER, Windows spelling")
    check("-DPython3_EXECUTABLE=C:/Python313/python.exe" in defs, "configure: -DPython3_EXECUTABLE")
    check("-DN64LLE_CARGO=C:/Users/A B/.cargo/bin/cargo.exe" in defs, "configure: -DN64LLE_CARGO")
    lin = {"ninja": "/usr/bin/ninja", "cc": "/usr/bin/gcc-15"}
    check("-DCMAKE_MAKE_PROGRAM=/usr/bin/ninja" in tc.configure_defines(lin, generator="Ninja"),
          "configure: CMAKE_MAKE_PROGRAM under Ninja")
    check(not any("MAKE_PROGRAM" in d for d in tc.configure_defines(lin, generator="Unix Makefiles")),
          "configure: no CMAKE_MAKE_PROGRAM under Makefiles")


def _synthetic_port(tmp: Path) -> tuple[Path, Path]:
    """A port + n64lle shaped enough for the dry runs: the marker, a
    build_framework.sh with n64lle's new case arm, a setup_project.sh with
    the scaffolder's flags, an already-'built' framework tree."""
    fw = tmp / "n64lle"
    (fw / "runtime").mkdir(parents=True)
    (fw / "runtime" / "runtime.cmake").write_text("# no resolve function: fallback path\n")
    (fw / "tools" / "new_project").mkdir(parents=True)
    (fw / "tools" / "build_framework.sh").write_text(
        "#!/usr/bin/env bash\nwhile [ $# -gt 0 ]; do\n  case \"$1\" in\n"
        "    --test) shift ;;\n"
        "    --python|--cc|--cxx|--cmake|--generator|--ninja|--cargo) shift 2 ;;\n"
        "  esac\ndone\n")
    (fw / "tools" / "new_project" / "setup_project.sh").write_text(
        "#!/usr/bin/env bash\ncase \"$1\" in\n  --rom) ;;\n  --n64lle-ref) ;;\n"
        "  --python|--cc|--cxx|--cmake|--generator|--ninja|--cargo|--git-path|--gh-path) ;;\n"
        "  --git) ;;\n  --gh) ;;\nesac\n")
    port = tmp / "Zed Recomp"
    (port / "tools").mkdir(parents=True)
    (port / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(ZedRecomp C CXX)\n"
        "n64lle_add_runtime_target(zed-runtime)\n")
    (port / "build-n64lle").mkdir()
    (port / "build-n64lle" / "n64emit").write_text("")
    return port, fw


def test_call_sites(tmp: Path) -> None:
    print("Studio call sites (dry runs on a synthetic port)")
    from project_studio import buildops, newproject

    port, fw = _synthetic_port(tmp)
    bind = tmp / "bin"
    clang = str(tmp / "alt" / "clang-19")
    tc.write_project(port, {"cc": clang, "python": str(bind / "python3")})
    old_env = dict(os.environ)
    os.environ["N64LLE_ROOT"] = str(fw)
    os.environ["RETCOMM_CONFIG_DIR"] = str(tmp / "cfg")  # an empty Studio default
    platforms.set_current("n64")
    try:
        logs: list[str] = []
        r = buildops.build_n64_framework(port, dry_run=True, log=logs.append)
        check(r.ok and f"--cc {clang}" in r.message and f"--cxx {clang.replace('clang', 'clang++')}"
              in r.message, "build framework: --cc and its sibling --cxx on the command line")
        check(f"--python {bind / 'python3'}" in r.message, "build framework: --python")
        check(any(l.startswith("toolchain: ") for l in logs), "build framework: the choice is logged")

        logs = []
        r = buildops.configure(port, dry_run=True, log=logs.append)
        check(r.ok, f"configure dry run ok ({r.message[:80]})")
        check(f"-C '{tc.cmake_path(str(tc.project_file(port)))}'" in r.message,
              "configure: -C the port's tools/toolchain.cmake (quoted: the path has a space)")
        check(f"-DCMAKE_C_COMPILER={clang}" in r.message, "configure: -DCMAKE_C_COMPILER")
        check(f"-DPython3_EXECUTABLE={bind / 'python3'}" in r.message,
              "configure: -DPython3_EXECUTABLE")

        opts = newproject.NewProjectOptions(
            name="Zed", disc=str(port / "CMakeLists.txt"), parent_dir=str(tmp / "new"),
            platform="n64", players=1,
            n64_tools={"cc": r"C:\Program Files\LLVM\bin\clang.exe", "git": "/usr/bin/git"})
        cmd, envo = newproject.build_n64_command(opts)
        check(cmd[cmd.index("--cc") + 1] == "C:/Program Files/LLVM/bin/clang.exe",
              "new-project: --cc, forward-slashed, one argv element")
        check(cmd[cmd.index("--git-path") + 1] == "/usr/bin/git" and "--git" in cmd,
              "new-project: --git-path carries the tool, --git still means 'make a repo'")
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="n64tc-") as d:
        tmp = Path(d)
        test_file_format(tmp)
        test_studio_defaults(tmp)
        test_paths()
        test_windows_abi()
        test_resolve(tmp)
        test_commands()
        test_call_sites(tmp)
    print()
    print("FAILED: %d" % failures if failures else "all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
