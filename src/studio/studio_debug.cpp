// studio_debug.cpp — see studio_debug.hpp.

#include "studio/studio_debug.hpp"

#include <nlohmann/json.hpp>

#include <cerrno>
#include <chrono>
#include <algorithm>
#include <cstdio>
#include <cstring>

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
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>
#define CLOSESOCK ::close
#endif

namespace retcomm::studio {

namespace {

constexpr auto kIdlePoll = std::chrono::milliseconds(500);
constexpr auto kTracePoll = std::chrono::milliseconds(250);
constexpr auto kRetry = std::chrono::seconds(2);
// fn_dump_parse caps `count` at 2048 server-side; asking for more silently
// truncates, which would look like dropped calls rather than a capped request.
constexpr int kEntryBatch = 2048;

#ifdef _WIN32
struct WsaInit {
    WsaInit() { WSADATA d; WSAStartup(MAKEWORD(2, 2), &d); }
};
void ensure_wsa() { static WsaInit once; (void)once; }
// SO_RCVTIMEO expiring is NOT a broken socket -- it just means nothing arrived
// in the window. Winsock reports it out of band rather than through errno.
bool recv_timed_out() {
    const int e = WSAGetLastError();
    return e == WSAETIMEDOUT || e == WSAEWOULDBLOCK || e == WSAEINTR;
}
#else
void ensure_wsa() {}
bool recv_timed_out() {
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
}
#endif

} // namespace

DebugClient::~DebugClient() { stop(); }

void DebugClient::start(std::string host, int port) {
    stop();
    {
        std::lock_guard<std::mutex> lk(mu_);
        host_ = std::move(host);
        port_ = port;
        snap_ = Snapshot{};
        snap_.state = State::Connecting;
        outbox_.clear();
        replies_.clear();
        in_flight_.clear();
    }
    quit_.store(false);
    running_.store(true);
    thread_ = std::thread([this] { worker(); });
}

void DebugClient::stop() {
    if (!running_.load()) {
        if (thread_.joinable()) thread_.join();
        return;
    }
    quit_.store(true);
    cv_.notify_all();
    if (thread_.joinable()) thread_.join();
    running_.store(false);
    std::lock_guard<std::mutex> lk(mu_);
    snap_.state = State::Disconnected;
    snap_.frame = 0;
}

void DebugClient::send(std::string fields, std::string label) {
    std::lock_guard<std::mutex> lk(mu_);
    outbox_.push_back(Job{std::move(fields), std::move(label), false});
    cv_.notify_all();
}

void DebugClient::request(std::string fields, std::string label) {
    std::lock_guard<std::mutex> lk(mu_);
    // A second request under a live label would race the first reply into the
    // wrong slot; drop it and let the caller retry once the first lands.
    for (const auto& j : outbox_)
        if (j.label == label) return;
    for (const auto& l : in_flight_)
        if (l == label) return;
    replies_.erase(label);
    outbox_.push_back(Job{std::move(fields), label, true});
    in_flight_.push_back(std::move(label));
    cv_.notify_all();
}

bool DebugClient::take_reply(const std::string& label, std::string& out) {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = replies_.find(label);
    if (it == replies_.end()) return false;
    out = std::move(it->second);
    replies_.erase(it);
    return true;
}

bool DebugClient::pending(const std::string& label) const {
    std::lock_guard<std::mutex> lk(mu_);
    for (const auto& j : outbox_)
        if (j.label == label) return true;
    for (const auto& l : in_flight_)
        if (l == label) return true;
    return false;
}

void DebugClient::set_live_trace(bool on, uint32_t lo, uint32_t hi) {
    trace_lo_.store(lo);
    trace_hi_.store(hi);
    trace_on_.store(on);
    {
        std::lock_guard<std::mutex> lk(mu_);
        snap_.live_trace = on;
    }
    if (on) {
        char buf[128];
        std::snprintf(buf, sizeof(buf),
                      "\"cmd\":\"fn_filter\",\"lo\":\"0x%08X\",\"hi\":\"0x%08X\"", lo, hi);
        send(buf, "fn_filter");
    }
    cv_.notify_all();
}

void DebugClient::reset_counts() {
    std::lock_guard<std::mutex> lk(mu_);
    snap_.calls.clear();
    snap_.callers.clear();
    snap_.consumed = 0;
    cursor_ = 0;
}

DebugClient::Snapshot DebugClient::snapshot() {
    std::lock_guard<std::mutex> lk(mu_);
    return snap_;
}

void DebugClient::close_socket() {
    if (sock_ >= 0) {
        CLOSESOCK(sock_);
        sock_ = -1;
    }
    rx_.clear();
}

// The runtime's debug server is ONE REQUEST PER CONNECTION: io_thread_main()
// in runtime/src/debug_server.c accepts, reads a single line, replies, and
// closes. Holding the socket open for a second command gets silence then EOF,
// which is indistinguishable from a hung emulator -- so every exchange opens
// its own socket. tools/debug_client.py's query() states the same contract.
bool DebugClient::ensure_connected() {
    if (sock_ >= 0) return true;
    ensure_wsa();

    int s = static_cast<int>(::socket(AF_INET, SOCK_STREAM, 0));
    if (s < 0) return false;

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port_));
    if (::inet_pton(AF_INET, host_.c_str(), &addr.sin_addr) != 1) {
        CLOSESOCK(s);
        return false;
    }
    if (::connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        CLOSESOCK(s);
        std::lock_guard<std::mutex> lk(mu_);
        snap_.state = State::Connecting;
        snap_.error = "no runtime listening on " + host_ + ":" + std::to_string(port_);
        return false;
    }

    // A dead runtime must not wedge the worker. Every read is bounded.
#ifdef _WIN32
    DWORD tv = 3000;
#else
    timeval tv{};
    tv.tv_sec = 3;
#endif
    ::setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&tv), sizeof(tv));
    int one = 1;
    ::setsockopt(s, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&one), sizeof(one));

    sock_ = s;
    rx_.clear();
    std::lock_guard<std::mutex> lk(mu_);
    snap_.state = State::Connected;
    snap_.error.clear();
    return true;
}

bool DebugClient::exchange(const std::string& fields, std::string& response) {
    // Fresh socket per command -- see the note on ensure_connected().
    close_socket();
    if (!ensure_connected()) return false;
    const int id = next_id_++;
    std::string line = "{\"id\":" + std::to_string(id) + "," + fields + "}\n";

    size_t sent = 0;
    while (sent < line.size()) {
        const auto n = ::send(sock_, line.data() + sent,
                              static_cast<int>(line.size() - sent), 0);
        if (n <= 0) { close_socket(); return false; }
        sent += static_cast<size_t>(n);
    }

    // Newline-framed, then the server closes. Read until the line lands or the
    // peer hangs up; a reply with no newline but a clean EOF is still a reply.
    int stalls = 0;   // consecutive SO_RCVTIMEO windows with nothing arriving
    for (;;) {
        const size_t nl = rx_.find('\n');
        if (nl != std::string::npos) {
            response = rx_.substr(0, nl);
            close_socket();
            return true;
        }
        char buf[1 << 16];
        const auto n = ::recv(sock_, buf, static_cast<int>(sizeof(buf)), 0);
        if (n == 0) {
            const bool have = !rx_.empty();
            if (have) response = rx_;
            close_socket();
            return have;
        }
        if (n < 0) {
            // A timeout mid-reply used to be fatal, which threw away every byte
            // already buffered. gpu_frame_dump is megabytes; one pause longer
            // than SO_RCVTIMEO anywhere in it lost the whole frame and surfaced
            // as an intermittently "truncated" dump. Keep waiting while the
            // peer is still feeding us, and only give up once it has gone quiet
            // for two full windows -- a genuinely dead runtime still cannot
            // wedge the worker, which is what the timeout is there for.
            if (recv_timed_out() && stalls < 2) {
                ++stalls;
                continue;
            }
            close_socket();
            return false;
        }
        stalls = 0;   // progress: the peer is alive and sending
        rx_.append(buf, static_cast<size_t>(n));
        if (rx_.size() > (64u << 20)) { close_socket(); return false; }  // runaway
    }
}

bool DebugClient::poll_frame() {
    std::string resp;
    if (!exchange("\"cmd\":\"ping\"", resp)) {
        std::lock_guard<std::mutex> lk(mu_);
        snap_.state = State::Connecting;
        snap_.error = "no runtime listening on " + host_ + ":" + std::to_string(port_);
        return false;
    }
    try {
        auto j = nlohmann::json::parse(resp);
        if (j.value("ok", false)) {
            std::lock_guard<std::mutex> lk(mu_);
            snap_.frame = j.value("frame", 0ull);
            snap_.state = State::Connected;
            snap_.error.clear();
            return true;
        }
    } catch (const std::exception&) {
        // A malformed reply is not fatal; the next poll re-establishes truth.
    }
    return true;   // the server answered; the payload just was not useful
}

void DebugClient::poll_entries() {
    std::string resp;
    if (!exchange("\"cmd\":\"fn_stats\"", resp)) return;
    uint64_t total = 0;
    try {
        auto j = nlohmann::json::parse(resp);
        if (!j.value("ok", false)) return;
        total = j.value("entry_total", 0ull);
    } catch (const std::exception&) {
        return;
    }
    {
        std::lock_guard<std::mutex> lk(mu_);
        snap_.entry_total = total;
    }
    if (cursor_ == 0 && total > 0) {
        // First look at a session already in progress: start from what the ring
        // still holds rather than claiming counts for evicted entries.
        cursor_ = (total > (1ull << 18)) ? total - (1ull << 18) : 0;
    }

    // Drain in bounded batches so one slow frame cannot block the UI's view of
    // the connection for long.
    for (int batch = 0; batch < 8 && cursor_ < total; ++batch) {
        char cmd[192];
        std::snprintf(cmd, sizeof(cmd),
                      "\"cmd\":\"fn_entry_dump\",\"seq_lo\":\"%llu\",\"count\":%d,"
                      "\"addr_lo\":\"0x%08X\",\"addr_hi\":\"0x%08X\"",
                      static_cast<unsigned long long>(cursor_), kEntryBatch,
                      trace_lo_.load(), trace_hi_.load());
        if (!exchange(cmd, resp)) return;
        try {
            auto j = nlohmann::json::parse(resp);
            if (!j.value("ok", false)) return;
            const auto& entries = j["entries"];
            uint64_t highest = cursor_;
            std::lock_guard<std::mutex> lk(mu_);
            for (const auto& e : entries) {
                const uint32_t fn = static_cast<uint32_t>(
                    std::stoul(e.value("func", "0x0"), nullptr, 16));
                const uint32_t ra = static_cast<uint32_t>(
                    std::stoul(e.value("ra", "0x0"), nullptr, 16));
                snap_.calls[fn]++;
                auto& v = snap_.callers[fn];
                if (v.size() < 32 && std::find(v.begin(), v.end(), ra) == v.end())
                    v.push_back(ra);
                const uint64_t seq = e.value("seq", 0ull);
                if (seq + 1 > highest) highest = seq + 1;
                snap_.consumed++;
            }
            // The server filters by address AFTER selecting the seq window, so
            // an empty batch does not mean the window was empty — advance past
            // the requested span either way or the cursor never moves.
            const uint64_t requested_end =
                cursor_ + static_cast<uint64_t>(j.value("emitted", 0)) ;
            cursor_ = (highest > requested_end) ? highest
                     : (cursor_ + static_cast<uint64_t>(kEntryBatch));
            if (cursor_ > total) cursor_ = total;
        } catch (const std::exception&) {
            return;
        }
    }
}

void DebugClient::worker() {
    // Sockets are per-command now, so "am I connected" is a property of the
    // last exchange, not of a held fd. After a failure, back off before
    // hammering a runtime that is not there.
    bool healthy = false;
    auto last_retry = std::chrono::steady_clock::now() - kRetry;
    while (!quit_.load()) {
        if (!healthy) {
            const auto now = std::chrono::steady_clock::now();
            if (now - last_retry < kRetry) {
                std::unique_lock<std::mutex> lk(mu_);
                cv_.wait_for(lk, std::chrono::milliseconds(200));
                continue;
            }
            last_retry = now;
            if (!ensure_connected()) {
                // Nothing will drain the outbox while the runtime is absent, so
                // resolve retained requests as errors rather than leaving a
                // caller polling take_reply() forever. Only after a retry
                // attempt actually failed — a request issued moments before a
                // successful reconnect still goes out.
                std::lock_guard<std::mutex> lk(mu_);
                for (auto it = outbox_.begin(); it != outbox_.end();) {
                    if (!it->retain) { ++it; continue; }
                    replies_[it->label] =
                        R"({"ok":false,"error":"no runtime listening"})";
                    in_flight_.erase(
                        std::remove(in_flight_.begin(), in_flight_.end(), it->label),
                        in_flight_.end());
                    it = outbox_.erase(it);
                }
                continue;
            }
            close_socket();  // the probe succeeded; commands open their own
        }

        // Caller-issued commands first — they are what a click is waiting on.
        std::vector<Job> jobs;
        {
            std::lock_guard<std::mutex> lk(mu_);
            jobs.swap(outbox_);
        }
        for (auto& job : jobs) {
            std::string resp;
            const bool ok = exchange(job.fields, resp);
            {
                std::lock_guard<std::mutex> lk(mu_);
                if (job.retain) {
                    // A failed exchange still resolves the label, as an error
                    // object — a caller polling take_reply() must never wait
                    // forever on a request the socket already lost.
                    replies_[job.label] = ok
                        ? std::move(resp)
                        : std::string(R"({"ok":false,"error":"debug server connection lost"})");
                    in_flight_.erase(
                        std::remove(in_flight_.begin(), in_flight_.end(), job.label),
                        in_flight_.end());
                }
                if (ok) {
                    snap_.last_command = job.label;
                    snap_.last_response =
                        resp.size() > 4000 ? resp.substr(0, 4000) + " …" : resp;
                }
            }
            if (!ok) {
                // Everything after a dead socket would fail the same way;
                // resolve the rest so no label is left dangling.
                std::lock_guard<std::mutex> lk(mu_);
                for (auto& rest : jobs) {
                    if (!rest.retain) continue;
                    if (replies_.count(rest.label)) continue;
                    replies_[rest.label] =
                        R"({"ok":false,"error":"debug server connection lost"})";
                    in_flight_.erase(
                        std::remove(in_flight_.begin(), in_flight_.end(), rest.label),
                        in_flight_.end());
                }
                break;
            }
        }

        if (trace_on_.load()) poll_entries();
        healthy = poll_frame();

        std::unique_lock<std::mutex> lk(mu_);
        cv_.wait_for(lk, trace_on_.load() ? kTracePoll : kIdlePoll);
    }
    close_socket();
    std::lock_guard<std::mutex> lk(mu_);
    snap_.state = State::Disconnected;
}

DebugClient& shared_debug_client() {
    static DebugClient client;
    return client;
}

DebugClient& oracle_debug_client() {
    static DebugClient client;
    return client;
}

} // namespace retcomm::studio
