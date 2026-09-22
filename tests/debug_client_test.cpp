// debug_client_test.cpp — DebugClient against a stub debug server.
//
// The real server needs a running game and a GPU; the protocol does not. This
// stands up a socket that speaks the same newline-framed JSON and checks the
// parts that are easy to get wrong: cursor advance over the fn_entry ring,
// per-function tallies, caller capture, and clean shutdown.

#include "studio/studio_debug.hpp"

// Same platform shim as studio_debug.cpp: Winsock is close enough to BSD
// sockets that only the header, the close name and the length types differ.
#ifdef _WIN32
// winsock2.h drags in windows.h, which defines min/max as MACROS unless this
// is set first -- and then `std::min(lo, e.func)` expands to `std::(...)`,
// which MSVC reports as "illegal token on right side of '::'" several lines
// from anything that looks wrong. Set before the include, not after: the
// macros are defined by the header being pulled in here.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
using socklen_t = int;
#define CLOSESOCK closesocket
#define SHUT_RDWR SD_BOTH
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#define CLOSESOCK ::close
#endif

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <thread>
#include <vector>

using namespace retcomm::studio;

namespace {

// Windows resets a connection that is closed while data it has accepted is
// still undelivered, so the peer sees ECONNRESET partway through instead of
// the rest of the reply. gpu_frame_dump below is 3 MB -- comfortably more than
// the socket buffer -- and without this it arrived truncated about a third of
// the time. SO_LINGER makes closesocket() wait for delivery instead of
// aborting. It must be set BEFORE the send, and then a plain close is enough:
// a shutdown()/drain dance before an aborting close does not help.
// Harmless on POSIX, where the default already behaves this way.
void deliver_before_close(int fd) {
    struct linger lg {};
    lg.l_onoff = 1;
    lg.l_linger = 30;
    ::setsockopt(fd, SOL_SOCKET, SO_LINGER, reinterpret_cast<const char*>(&lg), sizeof(lg));
}


int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %s  %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++failures;
}

// Three functions, called 3 / 2 / 1 times, from two distinct return addresses.
struct Entry { uint32_t func, ra; };
const Entry kEntries[] = {
    {0x80010000, 0x80020000}, {0x80010000, 0x80020000}, {0x80010000, 0x80030000},
    {0x80011000, 0x80020000}, {0x80011000, 0x80020000},
    {0x80012000, 0x80040000},
};
constexpr int kTotal = sizeof(kEntries) / sizeof(kEntries[0]);

std::atomic<bool> g_stop{false};
std::atomic<int>  g_armed{0};
std::atomic<int>  g_port{0};

std::string field(const std::string& j, const char* key) {
    const std::string k = std::string("\"") + key + "\":";
    auto p = j.find(k);
    if (p == std::string::npos) return "";
    p += k.size();
    if (p < j.size() && j[p] == '"') {
        auto e = j.find('"', p + 1);
        return j.substr(p + 1, e - p - 1);
    }
    auto e = j.find_first_of(",}", p);
    return j.substr(p, e - p);
}

void serve(int listen_fd) {
    while (!g_stop.load()) {
        sockaddr_in ca{};
        socklen_t cl = sizeof(ca);
        int fd = ::accept(listen_fd, reinterpret_cast<sockaddr*>(&ca), &cl);
        if (fd < 0) { if (g_stop.load()) break; continue; }
        deliver_before_close(fd);
        std::string rx;
        while (!g_stop.load()) {
            char buf[4096];
            auto n = ::recv(fd, buf, static_cast<int>(sizeof(buf)), 0);
            if (n <= 0) break;
            rx.append(buf, static_cast<size_t>(n));
            size_t nl;
            while ((nl = rx.find('\n')) != std::string::npos) {
                const std::string line = rx.substr(0, nl);
                rx.erase(0, nl + 1);
                const std::string id = field(line, "id");
                const std::string cmd = field(line, "cmd");
                std::string out;
                if (cmd == "ping") {
                    out = "{\"id\":" + id + ",\"ok\":true,\"frame\":4242}";
                } else if (cmd == "fn_stats") {
                    out = "{\"id\":" + id + ",\"ok\":true,\"entry_total\":" +
                          std::to_string(kTotal) + "}";
                } else if (cmd == "fn_entry_dump") {
                    const unsigned long lo = std::strtoul(field(line, "seq_lo").c_str(),
                                                          nullptr, 0);
                    out = "{\"id\":" + id + ",\"ok\":true,\"entries\":[";
                    int emitted = 0;
                    for (unsigned long s = lo; s < (unsigned long)kTotal; ++s) {
                        char e[256];
                        std::snprintf(e, sizeof(e),
                                      "%s{\"seq\":%lu,\"func\":\"0x%08X\",\"ra\":\"0x%08X\"}",
                                      emitted ? "," : "", s, kEntries[s].func,
                                      kEntries[s].ra);
                        out += e;
                        ++emitted;
                    }
                    out += "],\"emitted\":" + std::to_string(emitted) + "}";
                } else if (cmd == "fntrace_arm") {
                    g_armed.fetch_add(1);
                    out = "{\"id\":" + id + ",\"ok\":true,\"armed\":1}";
                } else {
                    out = "{\"id\":" + id + ",\"ok\":true}";
                }
                out += "\n";
                ::send(fd, out.data(), static_cast<int>(out.size()), 0);
            }
        }
        CLOSESOCK(fd);
    }
}

// The REAL runtime server is one request per connection: io_thread_main() in
// runtime/src/debug_server.c accepts, reads one line, replies, and closes. A
// client that assumes a persistent socket works fine against the stub above and
// then goes silent against an actual game, so the protocol is worth pinning.
// This server also answers with a multi-megabyte payload, because a frame dump
// is one and the status-line path truncates at 4000 bytes.
std::atomic<bool> g_os_stop{false};
constexpr size_t kBigPayload = 3u << 20;   // 3 MB

void serve_one_shot(int listen_fd) {
    while (!g_os_stop.load()) {
        sockaddr_in ca{};
        socklen_t cl = sizeof(ca);
        int fd = ::accept(listen_fd, reinterpret_cast<sockaddr*>(&ca), &cl);
        if (fd < 0) { if (g_os_stop.load()) break; continue; }
        deliver_before_close(fd);
        std::string rx;
        while (rx.find('\n') == std::string::npos) {
            char buf[4096];
            auto n = ::recv(fd, buf, static_cast<int>(sizeof(buf)), 0);
            if (n <= 0) break;
            rx.append(buf, static_cast<size_t>(n));
        }
        const std::string line = rx.substr(0, rx.find('\n'));
        const std::string id = field(line, "id");
        const std::string cmd = field(line, "cmd");
        std::string out;
        if (cmd == "ping") {
            out = "{\"id\":" + id + ",\"ok\":true,\"frame\":77}";
        } else if (cmd == "gpu_frame_dump") {
            out = "{\"id\":" + id + ",\"ok\":true,\"frame\":77,\"pad\":\"" +
                  std::string(kBigPayload, 'x') + "\"}";
        } else {
            out = "{\"id\":" + id + ",\"ok\":true}";
        }
        out += "\n";
        size_t sent = 0;
        while (sent < out.size()) {
            auto n = ::send(fd, out.data() + sent, static_cast<int>(out.size() - sent), 0);
            if (n <= 0) break;
            sent += static_cast<size_t>(n);
        }
        CLOSESOCK(fd);   // <- the contract
    }
}

bool wait_for(const std::function<bool()>& pred, int ms = 5000) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(ms);
    while (std::chrono::steady_clock::now() < deadline) {
        if (pred()) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    return false;
}

} // namespace

int main() {
#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif
    int lfd = ::socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    ::setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&one), sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;   // ephemeral
    if (::bind(lfd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        std::printf("bind failed\n");
        return 1;
    }
    ::listen(lfd, 4);
    socklen_t al = sizeof(addr);
    ::getsockname(lfd, reinterpret_cast<sockaddr*>(&addr), &al);
    g_port.store(ntohs(addr.sin_port));
    std::thread server([&] { serve(lfd); });

    DebugClient client;
    client.start("127.0.0.1", g_port.load());

    check(wait_for([&] { return client.snapshot().state == DebugClient::State::Connected; }),
          "connects to the debug server");
    check(wait_for([&] { return client.snapshot().frame == 4242; }),
          "ping reports the current frame");

    client.set_live_trace(true, 0x80010000, 0x80013000);
    check(wait_for([&] { return client.snapshot().consumed >= (uint64_t)kTotal; }),
          "drains the fn_entry ring exactly once");

    auto s = client.snapshot();
    check(s.calls[0x80010000] == 3, "tallies 3 calls for func A");
    check(s.calls[0x80011000] == 2, "tallies 2 calls for func B");
    check(s.calls[0x80012000] == 1, "tallies 1 call for func C");
    check(s.consumed == (uint64_t)kTotal, "does not double-count on repeated polls");
    check(s.callers[0x80010000].size() == 2, "captures both distinct callers of func A");

    client.send("\"cmd\":\"fntrace_arm\",\"target\":\"0x80010000\"", "fntrace_arm");
    check(wait_for([&] { return g_armed.load() == 1; }), "queued command reaches the server");

    // Idempotence: another poll cycle must not inflate the counts.
    std::this_thread::sleep_for(std::chrono::milliseconds(600));
    check(client.snapshot().calls[0x80010000] == 3, "counts stay stable while idle");

    // Reproduce the Functions tab's exact sequence: connect first, tick "Live
    // trace" later, with lo/hi derived from the loaded rows the way the tab
    // derives them. A screenshot run showed "0 entries observed" here and the
    // headless compositor was too flaky to tell a UI bug from a harness one.
    client.reset_counts();
    client.set_live_trace(false, 0, 0);
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    check(client.snapshot().consumed == 0, "counts reset to zero");

    uint32_t lo = 0xFFFFFFFFu, hi = 0;
    for (const auto& e : kEntries) {           // stand-in for model.fn_rows
        lo = std::min(lo, e.func);
        hi = std::max(hi, e.func + 0x100u);
    }
    client.set_live_trace(true, lo, hi);
    check(wait_for([&] { return client.snapshot().consumed >= (uint64_t)kTotal; }),
          "re-arming after a reset drains the ring again");
    check(client.snapshot().calls[0x80010000] == 3,
          "tab-shaped enable produces the same tallies");

    client.stop();
    check(client.snapshot().state == DebugClient::State::Disconnected,
          "stops cleanly");

    g_stop.store(true);
    ::shutdown(lfd, SHUT_RDWR);
    CLOSESOCK(lfd);
    server.detach();

    // ---- phase 2: the real server's one-request-per-connection contract ----
    std::printf("  -- one-shot server (real protocol) --\n");
    int ofd = ::socket(AF_INET, SOCK_STREAM, 0);
    ::setsockopt(ofd, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&one), sizeof(one));
    sockaddr_in oaddr{};
    oaddr.sin_family = AF_INET;
    oaddr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    oaddr.sin_port = 0;
    if (::bind(ofd, reinterpret_cast<sockaddr*>(&oaddr), sizeof(oaddr)) != 0) {
        std::printf("bind failed\n");
        return 1;
    }
    ::listen(ofd, 4);
    socklen_t ol = sizeof(oaddr);
    ::getsockname(ofd, reinterpret_cast<sockaddr*>(&oaddr), &ol);
    const int oport = ntohs(oaddr.sin_port);
    std::thread oserver([&] { serve_one_shot(ofd); });

    DebugClient one_shot;
    one_shot.start("127.0.0.1", oport);
    check(wait_for([&] { return one_shot.snapshot().frame == 77; }),
          "survives a server that closes after every reply");
    // A second poll over a *new* connection: a client holding the old socket
    // would stall here instead of reporting the frame again.
    std::this_thread::sleep_for(std::chrono::milliseconds(700));
    check(one_shot.snapshot().state == DebugClient::State::Connected,
          "stays connected across repeated one-shot exchanges");

    one_shot.request("\"cmd\":\"gpu_frame_dump\",\"frame\":77", "dump");
    check(one_shot.pending("dump"), "a queued request reports as pending");
    one_shot.request("\"cmd\":\"gpu_frame_dump\",\"frame\":77", "dump");
    std::string payload;
    check(wait_for([&] { return one_shot.take_reply("dump", payload); }),
          "take_reply returns the retained payload");
    check(payload.size() > kBigPayload,
          "a multi-megabyte reply arrives whole, not truncated");
    check(!one_shot.pending("dump"), "the label clears once taken");
    check(!one_shot.take_reply("dump", payload), "a taken reply is not served twice");

    one_shot.stop();

    // A request against a server that is gone must resolve as an error, not
    // leave the caller polling take_reply() forever.
    g_os_stop.store(true);
    ::shutdown(ofd, SHUT_RDWR);
    CLOSESOCK(ofd);
    oserver.detach();

    DebugClient dead;
    dead.start("127.0.0.1", oport);
    dead.request("\"cmd\":\"gpu_frame_dump\",\"frame\":1", "gone");
    std::string err;
    const bool resolved = wait_for([&] { return dead.take_reply("gone", err); }, 8000);
    check(resolved && err.find("\"ok\":false") != std::string::npos,
          "a request to a dead server resolves as an error");
    dead.stop();

    std::printf("%s\n", failures ? "FAILED" : "PASSED");
    return failures ? 1 : 0;
}
