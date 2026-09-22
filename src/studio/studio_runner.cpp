#include "studio/studio_runner.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <mutex>
#include <thread>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <poll.h>
// kill() and SIGTERM, used by process_alive / process_stop. glibc hands them
// over transitively through <sys/wait.h>, so Linux built without this line for
// as long as it has existed; Apple's libc does not, and the macOS build failed
// on "no member named 'kill' in the global namespace". Declared where it is
// used rather than relied on from a neighbour.
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

namespace retcomm::studio {

std::string tool_error(const RunResult& r) {
    std::string text = r.stderr_text.empty() ? r.stdout_text : r.stderr_text;
    std::string last;
    size_t pos = 0;
    while (pos <= text.size()) {
        const size_t nl = text.find('\n', pos);
        std::string line = text.substr(pos, nl == std::string::npos
                                                ? std::string::npos
                                                : nl - pos);
        while (!line.empty() && (line.back() == '\r' || line.back() == ' '))
            line.pop_back();
        if (!line.empty()) last = line;
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }
    if (last.empty()) last = "exit code " + std::to_string(r.exit_code);
    if (last.size() > 240) last = last.substr(0, 240) + " …";
    return last;
}
namespace {

struct PendingDone {
    DoneFn fn;
    RunResult result;
};

// One captured output stream of a child process.
//
// Both of a child's pipes have to be drained CONCURRENTLY. Reading stdout to
// EOF and only then reading stderr -- what this file used to do -- fails two
// ways. It deadlocks outright once the child writes more than one pipe buffer
// (64 KiB on Linux) to stderr: the child blocks in write(2), the parent blocks
// in read(1), and neither moves again. And even below that threshold it holds
// back every stderr line until the child exits, so a cross-build that reports
// progress on stderr shows an empty log for its whole run and is
// indistinguishable from a hang.
//
// Declared out here, above the platform fork, so the line splitting cannot
// drift between the two implementations.
struct CapturedStream {
    std::string* dest = nullptr;   // full text, handed back in RunResult
    bool to_log = false;           // also stream to the Studio log, line by line
    std::string acc;               // bytes not yet terminated by a newline
    bool open = true;
};

// Append freshly read bytes, emitting each completed line to the log as it
// arrives. At end of stream, `final_flush` releases a trailing partial line.
void stream_take(CapturedStream& s, const char* buf, size_t n, StudioModel* log_model) {
    s.dest->append(buf, buf + n);
    if (!s.to_log || !log_model) return;
    s.acc.append(buf, buf + n);
    size_t pos;
    while ((pos = s.acc.find('\n')) != std::string::npos) {
        std::string line = s.acc.substr(0, pos);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        log_model->append_log(std::move(line));
        s.acc.erase(0, pos + 1);
    }
}

void stream_finish(CapturedStream& s, StudioModel* log_model) {
    if (s.to_log && log_model && !s.acc.empty()) log_model->append_log(s.acc);
    s.acc.clear();
    s.open = false;
}

std::mutex g_done_mu;
std::vector<PendingDone> g_done_queue;

fs::path weakly_canonical_path(const fs::path& p) {
    std::error_code ec;
    fs::path c = fs::weakly_canonical(p, ec);
    return (!ec && !c.empty()) ? c : p;
}

bool looks_like_toolkit(const fs::path& dir) {
    std::error_code ec;
    return fs::is_directory(dir / "project_studio", ec) &&
           fs::is_regular_file(dir / "project_studio" / "__main__.py", ec);
}

fs::path find_toolkit_near(const fs::path& start) {
    fs::path walk = start;
    for (int i = 0; i < 8 && !walk.empty(); ++i) {
        const fs::path candidates[] = {
            walk / "toolkit",
            walk / "share" / "retcomm-studio" / "toolkit",
            walk / ".." / "share" / "retcomm-studio" / "toolkit",
            walk / "tools" / "new_project_layout",
            walk,
        };
        for (const auto& c : candidates) {
            if (looks_like_toolkit(c)) return weakly_canonical_path(c);
        }
        walk = walk.parent_path();
    }
    return {};
}

// Declared early so find_system_python() can probe an interpreter by RUNNING
// it. Defined below in the platform fork.
RunResult run_process(const std::string& exe, const std::vector<std::string>& args,
                      StudioModel* log_model, bool log_stdout);

std::string find_system_python() {
#if defined(_WIN32)
    // py.exe first: it is the real python.org launcher and is never a stub.
    // The bare names come after it because a default Windows 11 install ships
    // App Execution Aliases for python.exe / python3.exe under WindowsApps
    // that only open the Microsoft Store.
    const char* candidates[] = {"py.exe", "python.exe", "python3.exe"};
#else
    const char* candidates[] = {"python3", "python"};
#endif
    for (const char* c : candidates) {
        // Presence is NOT the test. `where python.exe` happily resolves the
        // Store alias, which then exits 9009 with "Python was not found" on
        // every call -- so Studio would pick an interpreter that cannot run a
        // single toolkit command. Execute a trivial program instead: only a
        // real interpreter exits 0. run_process() is used rather than
        // std::system() so this costs no console flash in a GUI build.
        RunResult r = run_process(c, {"-c", "import sys"}, nullptr, false);
        if (r.ok()) return c;
    }
#if defined(_WIN32)
    return "python";
#else
    return "python3";
#endif
}

fs::path retcomm_data_dir() {
    if (const char* env = std::getenv("RETCOMM_DATA_DIR")) {
        if (*env) return fs::path(env);
    }
#if defined(_WIN32)
    if (const char* local = std::getenv("LOCALAPPDATA")) {
        if (*local) return fs::path(local) / "retcomm";
    }
    if (const char* home = std::getenv("USERPROFILE")) {
        if (*home) return fs::path(home) / "AppData" / "Local" / "retcomm";
    }
#else
    if (const char* xdg = std::getenv("XDG_DATA_HOME")) {
        if (*xdg) return fs::path(xdg) / "retcomm";
    }
    if (const char* home = std::getenv("HOME")) {
        if (*home) return fs::path(home) / ".local" / "share" / "retcomm";
    }
#endif
    return {};
}

fs::path resolve_toolchain_root() {
    if (const char* env = std::getenv("RETCOMM_TOOLCHAIN")) {
        fs::path p(env);
        std::error_code ec;
        if (fs::is_directory(p, ec)) return weakly_canonical_path(p);
    }
    const fs::path data = retcomm_data_dir();
    if (data.empty()) return {};
    const fs::path base = data / "toolchains" / "cmake-clang-v1";
    std::error_code ec;
    const fs::path latest = base / "latest";
    if (fs::exists(latest, ec)) {
        fs::path resolved = weakly_canonical_path(latest);
        if (fs::is_directory(resolved, ec)) return resolved;
    }
    const fs::path path_file = base / "latest.path";
    if (fs::is_regular_file(path_file, ec)) {
        std::FILE* f = std::fopen(path_file.string().c_str(), "r");
        if (f) {
            char buf[4096] = {};
            if (std::fgets(buf, sizeof(buf), f)) {
                std::string line(buf);
                while (!line.empty() && (line.back() == '\n' || line.back() == '\r'))
                    line.pop_back();
                if (!line.empty()) {
                    fs::path p(line);
                    if (fs::is_directory(p, ec)) {
                        std::fclose(f);
                        return weakly_canonical_path(p);
                    }
                }
            }
            std::fclose(f);
        }
    }
    return {};
}

std::string find_toolchain_python() {
    if (const char* env = std::getenv("RETCOMM_PYTHON")) {
        if (*env) {
            std::error_code ec;
            if (fs::is_regular_file(env, ec)) return env;
        }
    }
    const fs::path root = resolve_toolchain_root();
    if (root.empty()) return {};
    const fs::path candidates[] = {
#if defined(_WIN32)
        root / "python" / "python.exe",
        root / "python" / "bin" / "python.exe",
#else
        root / "python" / "bin" / "python3",
        root / "python" / "bin" / "python",
#endif
    };
    std::error_code ec;
    for (const auto& c : candidates) {
        if (fs::is_regular_file(c, ec)) return weakly_canonical_path(c).string();
    }
    return {};
}

std::string find_python() {
    const std::string tc = find_toolchain_python();
    if (!tc.empty()) return tc;
    return find_system_python();
}

void prepend_env_path(const char* key, const fs::path& value) {
#if defined(_WIN32)
    std::string cur;
    char buf[32768];
    DWORD n = GetEnvironmentVariableA(key, buf, sizeof(buf));
    if (n > 0 && n < sizeof(buf)) cur = buf;
    std::string next = value.string();
    if (!cur.empty()) next += ";" + cur;
    SetEnvironmentVariableA(key, next.c_str());
#else
    const char* cur = std::getenv(key);
    std::string next = value.string();
    if (cur && *cur) {
        next += ":";
        next += cur;
    }
    setenv(key, next.c_str(), 1);
#endif
}

// Every Python this process spawns has to be able to PRINT what it computes.
// The toolkit and the analysis tools are full of "A -> B" written with U+2192;
// the Windows console default is cp1252, which cannot encode it, so one such
// line aborts the tool with UnicodeEncodeError. Set once at startup rather
// than per-command: the analysis tools go out through run_python_script_async,
// which does not pass through run_project_studio, so setting it there only
// covered them when a toolkit command happened to run first.
void set_python_utf8_env() {
#if defined(_WIN32)
    SetEnvironmentVariableA("PYTHONUTF8", "1");
    SetEnvironmentVariableA("PYTHONIOENCODING", "utf-8");
#else
    setenv("PYTHONUTF8", "1", 1);
    setenv("PYTHONIOENCODING", "utf-8", 1);
#endif
}

#if defined(_WIN32)

RunResult run_process(const std::string& exe, const std::vector<std::string>& args,
                      StudioModel* log_model, bool log_stdout) {
    RunResult out;
    std::string cmdline = "\"" + exe + "\"";
    for (const auto& a : args) {
        cmdline += " \"";
        for (char ch : a) {
            if (ch == '"') cmdline += "\\\"";
            else cmdline += ch;
        }
        cmdline += "\"";
    }

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE out_r = nullptr, out_w = nullptr;
    HANDLE err_r = nullptr, err_w = nullptr;
    if (!CreatePipe(&out_r, &out_w, &sa, 0) || !CreatePipe(&err_r, &err_w, &sa, 0)) {
        out.stderr_text = "CreatePipe failed";
        return out;
    }
    SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOA si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = out_w;
    si.hStdError = err_w;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);

    PROCESS_INFORMATION pi{};
    std::vector<char> cmd_mutable(cmdline.begin(), cmdline.end());
    cmd_mutable.push_back('\0');
    BOOL ok = CreateProcessA(nullptr, cmd_mutable.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW,
                             nullptr, nullptr, &si, &pi);
    CloseHandle(out_w);
    CloseHandle(err_w);
    if (!ok) {
        out.stderr_text = "CreateProcess failed";
        CloseHandle(out_r);
        CloseHandle(err_r);
        return out;
    }

    // Poll both handles so neither pipe can fill and stall the child -- see
    // CapturedStream. ReadFile on an anonymous pipe blocks, so ask
    // PeekNamedPipe what is available and only ever read that much.
    HANDLE handles[2] = {out_r, err_r};
    CapturedStream streams[2];
    streams[0].dest = &out.stdout_text;
    streams[0].to_log = log_stdout;
    streams[1].dest = &out.stderr_text;
    streams[1].to_log = true;

    while (streams[0].open || streams[1].open) {
        bool progressed = false;
        for (int i = 0; i < 2; ++i) {
            CapturedStream& st = streams[i];
            if (!st.open) continue;
            DWORD avail = 0;
            if (!PeekNamedPipe(handles[i], nullptr, 0, nullptr, &avail, nullptr)) {
                stream_finish(st, log_model);  // write end closed: child is done with it
                continue;
            }
            if (avail == 0) continue;
            char buf[4096];
            DWORD want = avail < sizeof(buf) ? avail : static_cast<DWORD>(sizeof(buf));
            DWORD n = 0;
            if (!ReadFile(handles[i], buf, want, &n, nullptr) || n == 0) {
                stream_finish(st, log_model);
                continue;
            }
            stream_take(st, buf, static_cast<size_t>(n), log_model);
            progressed = true;
        }
        if (!progressed && (streams[0].open || streams[1].open)) Sleep(5);
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    out.exit_code = static_cast<int>(code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    CloseHandle(out_r);
    CloseHandle(err_r);
    return out;
}

#else

RunResult run_process(const std::string& exe, const std::vector<std::string>& args,
                      StudioModel* log_model, bool log_stdout) {
    RunResult out;
    int out_pipe[2];
    int err_pipe[2];
    if (pipe(out_pipe) != 0 || pipe(err_pipe) != 0) {
        out.stderr_text = "pipe failed";
        return out;
    }
    pid_t pid = fork();
    if (pid < 0) {
        out.stderr_text = "fork failed";
        return out;
    }
    if (pid == 0) {
        dup2(out_pipe[1], STDOUT_FILENO);
        dup2(err_pipe[1], STDERR_FILENO);
        close(out_pipe[0]);
        close(out_pipe[1]);
        close(err_pipe[0]);
        close(err_pipe[1]);
        std::vector<char*> argv;
        argv.push_back(const_cast<char*>(exe.c_str()));
        for (const auto& a : args) argv.push_back(const_cast<char*>(a.c_str()));
        argv.push_back(nullptr);
        execvp(exe.c_str(), argv.data());
        std::fprintf(stderr, "execvp failed: %s\n", exe.c_str());
        _exit(127);
    }
    close(out_pipe[1]);
    close(err_pipe[1]);

    // poll() both pipes so whichever has bytes is served -- see CapturedStream.
    const int fds[2] = {out_pipe[0], err_pipe[0]};
    CapturedStream streams[2];
    streams[0].dest = &out.stdout_text;
    streams[0].to_log = log_stdout;
    streams[1].dest = &out.stderr_text;
    streams[1].to_log = true;

    while (streams[0].open || streams[1].open) {
        struct pollfd pfd[2];
        int idx[2] = {-1, -1};
        nfds_t nfd = 0;
        for (int i = 0; i < 2; ++i) {
            if (!streams[i].open) continue;
            pfd[nfd].fd = fds[i];
            pfd[nfd].events = POLLIN;
            pfd[nfd].revents = 0;
            idx[nfd] = i;
            ++nfd;
        }
        if (nfd == 0) break;
        if (poll(pfd, nfd, -1) < 0) {
            if (errno == EINTR) continue;
            break;  // the reads below would spin; treat as end of capture
        }
        for (nfds_t k = 0; k < nfd; ++k) {
            if (pfd[k].revents == 0) continue;
            CapturedStream& st = streams[idx[k]];
            char buf[4096];
            ssize_t n = read(pfd[k].fd, buf, sizeof(buf));
            if (n > 0) {
                stream_take(st, buf, static_cast<size_t>(n), log_model);
            } else if (n == 0 || (errno != EINTR && errno != EAGAIN)) {
                stream_finish(st, log_model);
            }
        }
    }
    close(out_pipe[0]);
    close(err_pipe[0]);
    int status = 0;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status)) out.exit_code = WEXITSTATUS(status);
    else out.exit_code = 1;
    return out;
}

#endif

} // namespace

bool resolve_runtime(StudioModel& model, std::string* err) {
    if (const char* env = std::getenv("RETCOMM_STUDIO_TOOLKIT")) {
        fs::path p(env);
        if (looks_like_toolkit(p)) {
            model.toolkit_dir = weakly_canonical_path(p);
        }
    }
    if (model.toolkit_dir.empty() && !model.exe_dir.empty()) {
        model.toolkit_dir = find_toolkit_near(model.exe_dir);
    }
    if (model.toolkit_dir.empty()) {
        model.toolkit_dir = find_toolkit_near(fs::current_path());
    }
    if (model.toolkit_dir.empty() || !looks_like_toolkit(model.toolkit_dir)) {
        if (err) *err = "Could not find Project Studio toolkit (tools/new_project_layout).";
        return false;
    }
    set_python_utf8_env();
    model.toolchain_root = resolve_toolchain_root();
    model.python_exe = find_python();
    model.toolchain_ready = !find_toolchain_python().empty();
    // Prefer toolchain bin on PATH for cmake/ninja/ccache when present.
    if (!model.toolchain_root.empty()) {
        prepend_env_path("PATH", model.toolchain_root / "bin");
        prepend_env_path("PATH", model.toolchain_root / "python" / "bin");
    }
    return true;
}

RunResult run_project_studio(StudioModel& model, const std::vector<std::string>& args,
                             bool log_stdout) {
    if (model.toolkit_dir.empty() || model.python_exe.empty()) {
        std::string e;
        if (!resolve_runtime(model, &e)) {
            RunResult r;
            r.stderr_text = e;
            model.append_log("[FAIL] " + e);
            return r;
        }
    }
    prepend_env_path("PYTHONPATH", model.toolkit_dir);
    set_python_utf8_env();
#if defined(_WIN32)
    SetEnvironmentVariableA("RETCOMM_STUDIO_TOOLKIT", model.toolkit_dir.string().c_str());
#else
    setenv("RETCOMM_STUDIO_TOOLKIT", model.toolkit_dir.string().c_str(), 1);
#endif

    // --platform is global and goes ahead of the subcommand. Injecting it here
    // rather than at each call site is deliberate: it is a property of the
    // session, and ~90 call sites each remembering to pass it is ~90 chances
    // for a SNES session to quietly run a PSX command.
    std::vector<std::string> full = {"-m", "project_studio"};
    if (model.platform != Platform::None) {
        full.push_back("--platform");
        full.push_back(platform_key(model.platform));
    }
    full.insert(full.end(), args.begin(), args.end());

    {
        std::string shown = model.python_exe + " -m project_studio";
        for (auto it = full.begin() + 2; it != full.end(); ++it) {
            shown += " ";
            shown += *it;
        }
        model.append_log("$ " + shown);
    }

    return run_process(model.python_exe, full, &model, log_stdout);
}

void run_project_studio_async(StudioModel& model, std::vector<std::string> args, DoneFn on_done,
                              bool log_stdout, bool allow_when_busy) {
    const bool is_stop =
        args.size() >= 2 && args[0] == "build" && args[1] == "stop";
    const bool bypass_busy = allow_when_busy || is_stop;
    if (!bypass_busy) {
        if (model.busy.exchange(true)) {
            model.append_log("[FAIL] Another job is already running.");
            if (on_done) {
                RunResult r;
                r.exit_code = 1;
                r.stderr_text = "busy";
                on_done(std::move(r));
            }
            return;
        }
    } else if (!is_stop) {
        // Non-stop allow_when_busy still claims busy if free.
        model.busy.store(true);
    }
    std::thread([&model, args = std::move(args), on_done = std::move(on_done), log_stdout,
                 is_stop]() mutable {
        RunResult r = run_project_studio(model, args, log_stdout);
        // Stop never owns busy (so it can run while Launch streams diagnostics).
        if (!is_stop) model.busy.store(false);
        if (on_done) {
            std::lock_guard<std::mutex> lock(g_done_mu);
            g_done_queue.push_back(PendingDone{std::move(on_done), std::move(r)});
        }
    }).detach();
}

#if defined(_WIN32)

long spawn_detached_logged(const std::string& exe, const std::vector<std::string>& args,
                           const std::string& cwd, const std::string& logfile,
                           const std::vector<std::pair<std::string, std::string>>& env,
                           std::string* err) {
    for (const auto& kv : env) SetEnvironmentVariableA(kv.first.c_str(), kv.second.c_str());
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE log = CreateFileA(logfile.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             &sa, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (log == INVALID_HANDLE_VALUE) {
        if (err) *err = "cannot open " + logfile;
        return 0;
    }
    std::string cmdline = "\"" + exe + "\"";
    for (const auto& a : args) cmdline += " \"" + a + "\"";
    STARTUPINFOA si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = log;
    si.hStdError = log;
    PROCESS_INFORMATION pi{};
    const BOOL ok = CreateProcessA(nullptr, cmdline.data(), nullptr, nullptr, TRUE,
                                   CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS, nullptr,
                                   cwd.empty() ? nullptr : cwd.c_str(), &si, &pi);
    CloseHandle(log);
    if (!ok) {
        if (err) *err = "CreateProcess failed";
        return 0;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return static_cast<long>(pi.dwProcessId);
}

bool process_alive(long pid) {
    if (pid <= 0) return false;
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, static_cast<DWORD>(pid));
    if (!h) return false;
    DWORD code = 0;
    const bool alive = GetExitCodeProcess(h, &code) && code == STILL_ACTIVE;
    CloseHandle(h);
    return alive;
}

bool process_stop(long pid) {
    if (pid <= 0) return false;
    HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, static_cast<DWORD>(pid));
    if (!h) return false;
    const bool ok = TerminateProcess(h, 0) != 0;
    CloseHandle(h);
    return ok;
}

#else

long spawn_detached_logged(const std::string& exe, const std::vector<std::string>& args,
                           const std::string& cwd, const std::string& logfile,
                           const std::vector<std::pair<std::string, std::string>>& env,
                           std::string* err) {
    const pid_t pid = fork();
    if (pid < 0) {
        if (err) *err = "fork failed";
        return 0;
    }
    if (pid == 0) {
        setsid();   // survive Studio exiting
        const int fd = ::open(logfile.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
        if (fd >= 0) {
            dup2(fd, STDOUT_FILENO);
            dup2(fd, STDERR_FILENO);
            if (fd > STDERR_FILENO) ::close(fd);
        }
        ::close(STDIN_FILENO);
        if (!cwd.empty()) { if (chdir(cwd.c_str()) != 0) _exit(127); }
        for (const auto& kv : env) setenv(kv.first.c_str(), kv.second.c_str(), 1);
        std::vector<char*> argv;
        argv.push_back(const_cast<char*>(exe.c_str()));
        for (const auto& a : args) argv.push_back(const_cast<char*>(a.c_str()));
        argv.push_back(nullptr);
        execv(exe.c_str(), argv.data());
        _exit(127);
    }
    return static_cast<long>(pid);
}

bool process_alive(long pid) {
    if (pid <= 0) return false;
    // Reap first: a detached child we forked stays a zombie until waited on,
    // and a zombie answers kill(0) as alive.
    int status = 0;
    ::waitpid(static_cast<pid_t>(pid), &status, WNOHANG);
    return ::kill(static_cast<pid_t>(pid), 0) == 0;
}

bool process_stop(long pid) {
    if (pid <= 0) return false;
    return ::kill(static_cast<pid_t>(pid), SIGTERM) == 0;
}

#endif

std::string python_with_modules(StudioModel& model,
                                const std::vector<std::string>& modules) {
    if (modules.empty()) return model.python_exe;

    static std::mutex mu;
    static std::map<std::string, std::string> cache;
    std::string key;
    for (const auto& m : modules) key += m + ",";
    {
        std::lock_guard<std::mutex> lk(mu);
        const auto it = cache.find(key);
        if (it != cache.end()) return it->second;
    }

    std::string import_line = "import ";
    for (size_t i = 0; i < modules.size(); ++i)
        import_line += (i ? ", " : "") + modules[i];

    // Toolchain first (pinned, predictable), then whatever the host provides.
    std::vector<std::string> candidates;
    if (!model.python_exe.empty()) candidates.push_back(model.python_exe);
    candidates.emplace_back("python3");
    candidates.emplace_back("python");

    std::string chosen;
    for (const auto& cand : candidates) {
        RunResult r = run_process(cand, {"-c", import_line}, nullptr, false);
        if (r.ok()) {
            chosen = cand;
            break;
        }
    }
    std::lock_guard<std::mutex> lk(mu);
    cache[key] = chosen;
    return chosen;
}

void run_python_script_async(StudioModel& model, std::string script,
                             std::vector<std::string> args, DoneFn on_done,
                             bool log_stdout, JobSlot slot,
                             std::vector<std::string> requires_modules) {
    std::atomic<bool>& lock = (slot == JobSlot::Global)  ? model.busy_global
                              : (slot == JobSlot::Frames) ? model.busy_frames
                                                          : model.busy;
    if (model.python_exe.empty()) {
        std::string e;
        resolve_runtime(model, &e);  // best effort: we only need the interpreter
    }
    if (model.python_exe.empty()) {
        model.append_log("[FAIL] No python interpreter found.");
        if (on_done) {
            RunResult r;
            r.exit_code = 1;
            r.stderr_text = "no python";
            on_done(std::move(r));
        }
        return;
    }
    if (lock.exchange(true)) {
        model.append_log(slot == JobSlot::Project
                             ? "[FAIL] Another job is already running."
                             : "[FAIL] That background job is already running.");
        if (on_done) {
            RunResult r;
            r.exit_code = 1;
            r.stderr_text = "busy";
            on_done(std::move(r));
        }
        return;
    }
    std::string interpreter = model.python_exe;
    if (!requires_modules.empty()) {
        interpreter = python_with_modules(model, requires_modules);
        if (interpreter.empty()) {
            std::string names;
            for (size_t i = 0; i < requires_modules.size(); ++i)
                names += (i ? ", " : "") + requires_modules[i];
            const std::string msg =
                "[FAIL] No python found that can import: " + names +
                ".  Install them into the toolchain python:\n    " +
                model.python_exe + " -m pip install numpy pillow";
            model.append_log(msg);
            lock.store(false);
            if (on_done) {
                RunResult r;
                r.exit_code = 1;
                r.stderr_text = "missing python modules: " + names;
                std::lock_guard<std::mutex> qlock(g_done_mu);
                g_done_queue.push_back(PendingDone{std::move(on_done), std::move(r)});
            }
            return;
        }
        if (interpreter != model.python_exe)
            model.append_log("using " + interpreter + " (the toolchain python "
                             "cannot import " + requires_modules[0] + ")");
    }

    std::thread([&model, &lock, script = std::move(script), args = std::move(args),
                 on_done = std::move(on_done), log_stdout,
                 interpreter = std::move(interpreter)]() mutable {
        std::vector<std::string> full = {script};
        full.insert(full.end(), args.begin(), args.end());
        {
            std::string shown = interpreter + " " + script;
            for (const auto& a : args) {
                shown += " ";
                shown += a;
            }
            model.append_log("$ " + shown);
        }
        RunResult r = run_process(interpreter, full, &model, log_stdout);
        lock.store(false);
        if (on_done) {
            std::lock_guard<std::mutex> qlock(g_done_mu);
            g_done_queue.push_back(PendingDone{std::move(on_done), std::move(r)});
        }
    }).detach();
}

void pump_async_jobs(StudioModel& /*model*/) {
    std::vector<PendingDone> local;
    {
        std::lock_guard<std::mutex> lock(g_done_mu);
        local.swap(g_done_queue);
    }
    for (auto& p : local) {
        if (p.fn) p.fn(std::move(p.result));
    }
}

// nlohmann's value() falls back only when the KEY IS ABSENT. A key that is
// present and null throws type_error.302 — which is how a SNES audit, whose
// boot_exe is legitimately null because a cartridge boots from its reset
// vector rather than a named executable, failed to parse at all.
//
// Every optional field crossing this boundary comes from a Python dataclass
// with `str | None` somewhere in it, so this is the rule for all of them, not
// a patch for one.
std::string json_str(const nlohmann::json& j, const char* key, const char* fallback = "") {
    if (!j.contains(key)) return fallback;
    const auto& v = j.at(key);
    if (v.is_null()) return fallback;
    if (v.is_string()) return v.get<std::string>();
    return v.dump();  // a number/bool where a string was expected: show it, don't throw
}

bool load_repos_from_json(StudioModel& model, const std::string& json_text, std::string* err) {
    try {
        auto j = nlohmann::json::parse(json_text);
        std::lock_guard<std::mutex> lock(model.mu);
        model.repos.clear();
        model.catalog_only = j.value("catalog_only", false);
        model.bulk_jobs = j.value("bulk_jobs", 2);
        model.log_height = j.value("log_height", 160);
        model.log_h_pref = static_cast<float>(std::max(100, model.log_height));
        const std::string last = json_str(j, "last");
        for (const auto& r : j.at("repos")) {
            RepoEntry e;
            e.path = json_str(r, "path");
            e.name = json_str(r, "name");
            e.cue = json_str(r, "cue");
            e.label = json_str(r, "label");
            e.in_catalog = r.value("in_catalog", false);
            e.players = r.value("players", 2);
            if (e.players < 1) e.players = 1;
            if (e.players > 8) e.players = 8;
            if (e.label.empty()) {
                e.label = e.name.empty() ? fs::path(e.path).filename().string() : e.name;
            }
            if (!e.path.empty()) model.repos.push_back(std::move(e));
        }
        if (!last.empty()) model.select_repo_by_path(last);
        else if (!model.repos.empty() && model.selected_repo < 0) model.selected_repo = 0;
        // Seed bulk selection for new paths; drop orphans from removed repos.
        {
            std::map<std::string, bool> next;
            for (const auto& e : model.repos) {
                auto it = model.bulk_selected.find(e.path);
                next[e.path] = (it != model.bulk_selected.end()) ? it->second : true;
            }
            model.bulk_selected.swap(next);
        }
        model.coerce_catalog_only_selection();
        if (model.selected_repo >= 0 &&
            model.selected_repo < static_cast<int>(model.repos.size())) {
            const auto& sel = model.repos[static_cast<size_t>(model.selected_repo)];
            std::snprintf(model.disc_cue, sizeof(model.disc_cue), "%s", sel.cue.c_str());
            model.apply_selected_players();
        }
        return true;
    } catch (const std::exception& ex) {
        if (err) *err = ex.what();
        return false;
    }
}

bool load_module_urls_from_json(StudioModel& model, const std::string& json_text,
                                std::string* err) {
    try {
        auto j = nlohmann::json::parse(json_text);
        std::lock_guard<std::mutex> lock(model.mu);
        model.git_urls.clear();
        for (const auto& m : j.at("modules")) {
            StudioModel::ModuleUrlRow row;
            row.path = json_str(m, "path");
            row.nested = m.value("nested", false);
            row.gitmodules_url = json_str(m, "gitmodules_url");
            row.local_url = json_str(m, "local_url");
            row.origin_url = json_str(m, "origin_url");
            row.effective_url = json_str(m, "effective_url");
            row.moved_to = json_str(m, "moved_to");
            row.present = m.value("present", false);
            std::snprintf(row.edit, sizeof(row.edit), "%s", row.effective_url.c_str());
            if (!row.path.empty()) model.git_urls.push_back(std::move(row));
        }
        return true;
    } catch (const std::exception& ex) {
        if (err) *err = ex.what();
        return false;
    }
}

bool load_audit_from_json(StudioModel& model, const std::string& json_text, std::string* err) {
    try {
        auto j = nlohmann::json::parse(json_text);
        std::lock_guard<std::mutex> lock(model.mu);
        model.audit_checks.clear();
        model.audit_layout = json_str(j, "layout");
        model.audit_boot = json_str(j, "boot_exe");
        for (const auto& c : j.at("checks")) {
            AuditCheck a;
            a.id = json_str(c, "id");
            a.title = json_str(c, "title");
            a.status = json_str(c, "status");
            a.detail = json_str(c, "detail");
            a.fix_op = json_str(c, "fix_op");
            model.audit_checks.push_back(std::move(a));
        }
        return true;
    } catch (const std::exception& ex) {
        if (err) *err = ex.what();
        return false;
    }
}

bool load_plan_from_json(StudioModel& model, const std::string& json_text, std::string* err) {
    try {
        auto j = nlohmann::json::parse(json_text);
        std::lock_guard<std::mutex> lock(model.mu);
        model.plan_steps.clear();
        for (const auto& s : j.at("steps")) {
            PlanStep p;
            p.op_id = json_str(s, "op_id");
            p.title = json_str(s, "title");
            p.detail = json_str(s, "detail");
            p.selected = s.value("selected", true);
            model.plan_steps.push_back(std::move(p));
        }
        return true;
    } catch (const std::exception& ex) {
        if (err) *err = ex.what();
        return false;
    }
}

} // namespace retcomm::studio
