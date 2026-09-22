// studio_snes_data.cpp — the ImGui-free half of the SNES Diagnostics tab.
//
// Split out for the same reason studio_frames_data.cpp is: everything here can
// be exercised by a load test with no GPU and no window, and a parser that can
// be tested is a parser that can be trusted. The UI half never parses.

#include "studio/studio_snes.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>

#ifdef _WIN32
// windows.h arrives through winsock2.h and defines min/max as macros — see
// the NOMINMAX note in CMakeLists.txt.
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

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace retcomm::studio {

namespace {

constexpr auto kIdlePoll = std::chrono::milliseconds(400);
constexpr auto kRetry = std::chrono::seconds(2);

#ifdef _WIN32
struct WsaInit {
    WsaInit() { WSADATA d; WSAStartup(MAKEWORD(2, 2), &d); }
};
void ensure_wsa() { static WsaInit once; (void)once; }
#else
void ensure_wsa() {}
#endif

std::string trim(std::string s) {
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' ||
                          s.back() == ' ' || s.back() == '\t'))
        s.pop_back();
    size_t i = 0;
    while (i < s.size() && (s[i] == ' ' || s[i] == '\t')) ++i;
    return s.substr(i);
}

} // namespace

// ---- SnesDebugClient -------------------------------------------------------

SnesDebugClient::~SnesDebugClient() { stop(); }

void SnesDebugClient::start(std::string host, int port) {
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

void SnesDebugClient::stop() {
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

void SnesDebugClient::request(std::string command, std::string label) {
    {
        std::lock_guard<std::mutex> lk(mu_);
        in_flight_.push_back(label);
        outbox_.push_back(Job{std::move(command), std::move(label)});
    }
    cv_.notify_all();
}

bool SnesDebugClient::take_reply(const std::string& label, std::string& out) {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = replies_.find(label);
    if (it == replies_.end()) return false;
    out = std::move(it->second);
    replies_.erase(it);
    return true;
}

bool SnesDebugClient::pending(const std::string& label) const {
    std::lock_guard<std::mutex> lk(mu_);
    return std::find(in_flight_.begin(), in_flight_.end(), label) != in_flight_.end();
}

SnesDebugClient::Snapshot SnesDebugClient::snapshot() {
    std::lock_guard<std::mutex> lk(mu_);
    return snap_;
}

void SnesDebugClient::close_socket() {
    if (sock_ >= 0) CLOSESOCK(sock_);
    sock_ = -1;
    rx_.clear();
}

bool SnesDebugClient::ensure_connected() {
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
        snap_.error = "no snesrecomp runtime listening on " + host_ + ":" +
                      std::to_string(port_) +
                      " — a build without SNESRECOMP_ENABLE_TRACE opens no port";
        return false;
    }

    // A wedged runtime must never stall the worker; every read is bounded.
    // dump_vram is 128 KB of hex over loopback, so this is generous rather
    // than snappy on purpose.
#ifdef _WIN32
    DWORD tv = 8000;
#else
    timeval tv{};
    tv.tv_sec = 8;
#endif
    ::setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&tv),
                 sizeof(tv));
    int one = 1;
    ::setsockopt(s, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&one),
                 sizeof(one));

    sock_ = s;
    rx_.clear();
    std::lock_guard<std::mutex> lk(mu_);
    snap_.state = State::Connected;
    snap_.error.clear();
    return true;
}

// The wire format is one bare command line in, one JSON line out. There are no
// request ids to match a reply to a command, so the connection is held and used
// strictly one command at a time — which is also what makes `screenshot` then
// `dump_cgram` describe the same moment rather than two moments a reconnect
// apart.
bool SnesDebugClient::exchange(const std::string& command, std::string& response) {
    if (!ensure_connected()) return false;

    const std::string line = command + "\n";
    size_t sent = 0;
    while (sent < line.size()) {
        const auto n = ::send(sock_, line.data() + sent,
                              static_cast<int>(line.size() - sent), 0);
        if (n <= 0) { close_socket(); return false; }
        sent += static_cast<size_t>(n);
    }

    for (;;) {
        const size_t nl = rx_.find('\n');
        if (nl != std::string::npos) {
            response = rx_.substr(0, nl);
            rx_.erase(0, nl + 1);
            return true;
        }
        char buf[65536];
        const auto n = ::recv(sock_, buf, sizeof(buf), 0);
        if (n <= 0) { close_socket(); return false; }
        rx_.append(buf, static_cast<size_t>(n));
    }
}

void SnesDebugClient::worker() {
    while (!quit_.load()) {
        std::vector<Job> jobs;
        {
            std::unique_lock<std::mutex> lk(mu_);
            cv_.wait_for(lk, kIdlePoll, [&] { return quit_.load() || !outbox_.empty(); });
            if (quit_.load()) break;
            jobs.swap(outbox_);
        }

        for (auto& job : jobs) {
            std::string reply;
            const bool ok = exchange(job.command, reply);
            std::lock_guard<std::mutex> lk(mu_);
            snap_.last_command = job.command;
            replies_[job.label] =
                ok ? reply : std::string("{\"ok\":false,\"error\":\"not connected\"}");
            in_flight_.erase(
                std::remove(in_flight_.begin(), in_flight_.end(), job.label),
                in_flight_.end());
        }

        // Heartbeat. `frame` is the cheapest command the server has and is the
        // only thing polled on a timer: everything else costs a dump.
        std::string reply;
        if (exchange("frame", reply)) {
            uint64_t f = 0;
            try {
                const json j = json::parse(reply);
                f = j.value("frame", 0ull);
            } catch (...) {
                // A non-JSON answer means we are talking to something that is
                // not a snesrecomp debug server. Say that instead of showing a
                // frame of 0, which reads as "running but stuck".
                std::lock_guard<std::mutex> lk(mu_);
                snap_.state = State::Failed;
                snap_.error = "reply was not JSON — is this a snesrecomp port?";
                continue;
            }
            std::lock_guard<std::mutex> lk(mu_);
            snap_.state = State::Connected;
            snap_.frame = f;
            snap_.error.clear();
        } else {
            std::lock_guard<std::mutex> lk(mu_);
            if (snap_.state == State::Connected) snap_.state = State::Connecting;
            std::this_thread::sleep_for(kRetry);
        }
    }
    close_socket();
}

SnesDebugClient& snes_debug_client() {
    static SnesDebugClient c;
    return c;
}

// ---- probes ----------------------------------------------------------------

std::string snes_rom_for(const std::string& build_dir) {
    if (build_dir.empty()) return {};
    std::error_code ec;
    const fs::path cfg = fs::path(build_dir) / "rom.cfg";
    if (!fs::is_regular_file(cfg, ec)) return {};
    std::ifstream in(cfg);
    std::string line;
    if (!std::getline(in, line)) return {};
    line = trim(line);
    if (line.empty()) return {};
    return fs::is_regular_file(fs::path(line), ec) ? line : std::string();
}

std::string snes_tool_path(const std::string& tool) {
    const char* env = std::getenv("RETCOMM_STUDIO_TOOLKIT");
    if (!env || !*env) return {};
    std::error_code ec;
    const fs::path p = fs::path(env).parent_path() / "snes_analysis" / tool;
    return fs::is_regular_file(p, ec) ? p.string() : std::string();
}

MesenStatus parse_mesen_status(const std::string& json_text) {
    MesenStatus st;
    st.probed = true;
    if (json_text.empty()) {
        st.error = "mesen_oracle.py produced no output";
        st.summary = st.error;
        return st;
    }
    try {
        const json j = json::parse(json_text);
        st.root = j.value("root", "");
        st.binary = j.value("binary", "");
        st.provider = j.value("provider", "");
        st.installed = j.value("installed", false);
        if (!st.installed && !st.binary.empty()) {
            std::error_code ec;
            st.installed = fs::is_regular_file(fs::path(st.binary), ec);
        }
    } catch (const std::exception& e) {
        st.error = std::string("could not parse status: ") + e.what();
        st.summary = st.error;
        return st;
    }
    if (st.installed) {
        st.summary = "Mesen ready" +
                     (st.provider.empty() ? std::string() : " (" + st.provider + ")") +
                     (st.binary.empty() ? std::string() : " — " + st.binary);
    } else {
        st.summary = "Mesen not installed — run Setup below";
    }
    return st;
}

// ---- CGRAM -----------------------------------------------------------------

uint32_t bgr555_to_rgb(uint16_t v) {
    // 5 -> 8 bits by replication, which is what the PPU does; a plain <<3
    // leaves white at 0xF8F8F8 and makes every swatch read slightly dark.
    const uint32_t r = (v & 31u);
    const uint32_t g = ((v >> 5) & 31u);
    const uint32_t b = ((v >> 10) & 31u);
    const auto x8 = [](uint32_t c) { return (c << 3) | (c >> 2); };
    return (x8(r) << 16) | (x8(g) << 8) | x8(b);
}

namespace {

void summarise_cgram(CgramView& out) {
    std::map<uint16_t, int> census;
    for (uint16_t v : out.entry) census[v]++;
    out.distinct = static_cast<int>(census.size());
    out.top_count = 0;
    out.top_value = 0;
    for (const auto& kv : census) {
        if (kv.second > out.top_count) {
            out.top_count = kv.second;
            out.top_value = kv.first;
        }
    }
    auto z = census.find(0);
    out.zero_count = z == census.end() ? 0 : z->second;
    out.loaded = true;
}

} // namespace

bool parse_cgram(CgramView& out, const std::string& reply) {
    out = CgramView{};
    std::string hex;
    try {
        const json j = json::parse(reply);
        if (!j.value("ok", true)) {
            out.error = j.value("error", "dump_cgram refused");
            return false;
        }
        hex = j.value("hex", "");
    } catch (const std::exception& e) {
        out.error = std::string("dump_cgram reply was not JSON: ") + e.what();
        return false;
    }
    // The server answers with one contiguous hex string, not space-separated
    // bytes. Tolerate whitespace anyway rather than depend on that.
    std::string clean;
    clean.reserve(hex.size());
    for (char c : hex)
        if (!std::isspace(static_cast<unsigned char>(c))) clean.push_back(c);
    if (clean.size() < 1024) {
        out.error = "dump_cgram returned " + std::to_string(clean.size() / 2) +
                    " bytes, expected 512";
        return false;
    }
    for (int i = 0; i < 256; ++i) {
        const auto byte = [&](int k) -> uint16_t {
            return static_cast<uint16_t>(
                std::stoi(clean.substr(static_cast<size_t>(k) * 2, 2), nullptr, 16));
        };
        out.entry[i] = static_cast<uint16_t>(byte(i * 2) | (byte(i * 2 + 1) << 8));
    }
    summarise_cgram(out);
    return true;
}

bool load_cgram_bin(CgramView& out, const std::string& path) {
    out = CgramView{};
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        out.error = "cannot open " + path;
        return false;
    }
    unsigned char buf[512];
    in.read(reinterpret_cast<char*>(buf), sizeof(buf));
    if (in.gcount() != static_cast<std::streamsize>(sizeof(buf))) {
        out.error = "expected 512 bytes, got " + std::to_string(in.gcount());
        return false;
    }
    for (int i = 0; i < 256; ++i)
        out.entry[i] = static_cast<uint16_t>(buf[i * 2] | (buf[i * 2 + 1] << 8));
    summarise_cgram(out);
    return true;
}

int cgram_diff_count(const CgramView& a, const CgramView& b) {
    if (!a.loaded || !b.loaded) return -1;
    int n = 0;
    for (int i = 0; i < 256; ++i)
        if (a.entry[i] != b.entry[i]) ++n;
    return n;
}

// ---- artifacts -------------------------------------------------------------

void scan_snes_bundles(StudioModel& model) {
    model.snes_bundles.clear();
    if (model.snes_frames_dir.empty()) return;
    std::error_code ec;
    if (!fs::is_directory(model.snes_frames_dir, ec)) return;
    for (const auto& e : fs::directory_iterator(model.snes_frames_dir, ec)) {
        if (ec) break;
        if (!e.is_regular_file()) continue;
        const fs::path p = e.path();
        if (p.extension() != ".json") continue;
        SnesBundle b;
        b.tag = p.stem().string();
        b.path = p.string();
        try {
            std::ifstream in(p);
            json j;
            in >> j;
            b.frame = j.value("frame", 0ull);
        } catch (...) {
            // A half-written manifest from a crashed capture is a normal thing
            // to find. List it; the frame just reads as 0.
        }
        const fs::path assets = fs::path(model.snes_frames_dir).parent_path() / "assets";
        b.decoded = fs::is_directory(assets / b.tag, ec);
        b.attributed = fs::is_regular_file(assets / b.tag / "attribution.json", ec) ||
                       fs::is_regular_file(assets / (b.tag + ".attribution.json"), ec);
        model.snes_bundles.push_back(std::move(b));
    }
    std::sort(model.snes_bundles.begin(), model.snes_bundles.end(),
              [](const SnesBundle& a, const SnesBundle& b) { return a.tag < b.tag; });
}

bool load_loop_compare(LoopCompare& out, const std::string& path) {
    out = LoopCompare{};
    out.path = path;
    std::ifstream in(path);
    if (!in) {
        out.error = "no result yet — run the tool first";
        return false;
    }
    json j;
    try {
        in >> j;
    } catch (const std::exception& e) {
        out.error = std::string("could not parse: ") + e.what();
        return false;
    }
    try {
        if (j.contains("selector")) {
            const auto& s = j["selector"];
            out.selector = s.value("addr", "") + " == " +
                           std::to_string(s.value("value", 0));
        }
        for (const auto& v : j.value("visits", json::array())) {
            LoopVisit lv;
            lv.visit = v.value("visit", 0);
            lv.entered_frame = v.value("entered_frame", 0ull);
            const auto win = v.value("frame_window", json::array());
            if (win.size() == 2) {
                lv.frame_lo = win[0].get<int>();
                lv.frame_hi = win[1].get<int>();
            }
            for (auto it = v["irq_chain"].begin(); it != v["irq_chain"].end(); ++it)
                lv.irq_chain.emplace_back(it.key(), it.value().get<int>());
            std::sort(lv.irq_chain.begin(), lv.irq_chain.end());
            const auto& cg = v["cgram"];
            lv.cgram_distinct = cg.value("distinct", 0);
            lv.cgram_top_count = cg.value("top_count", 0);
            lv.cgram_top_value = cg.value("top_value", "");
            lv.cgram_fill = cg.value("looks_like_fill", false);
            const auto& sh = v["shadow_0900"];
            lv.shadow_distinct = sh.value("distinct", 0);
            lv.shadow_top_count = sh.value("top_count", 0);
            lv.shadow_top_value = sh.value("top_value", "");
            lv.shadow_fill = sh.value("looks_like_fill", false);
            lv.top_present_in_shadow = v.value("cgram_top_present_in_shadow", false);
            lv.upload_frames = v.value("cgram_upload_frames", 0);
            lv.block_ring_entries = v.value("block_ring_entries", 0);
            lv.chain_trustworthy = v.value("irq_chain_trustworthy", true);
            for (const auto& w : v.value("reg_writers", json::array())) {
                char buf[160];
                std::snprintf(buf, sizeof(buf), "%s  %s  x%d",
                              w.value("reg", "?").c_str(),
                              w.value("func", "?").c_str(),
                              w.value("count", 0));
                lv.reg_writers.push_back(buf);
            }
            out.visits.push_back(std::move(lv));
        }
        for (const auto& d : j.value("diffs", json::array())) {
            LoopDiffRow r;
            r.field = d.value("field", "");
            r.visit1 = d["visit1"].is_string() ? d["visit1"].get<std::string>()
                                               : d["visit1"].dump();
            r.visit2 = d["visit2"].is_string() ? d["visit2"].get<std::string>()
                                               : d["visit2"].dump();
            out.diffs.push_back(std::move(r));
        }
    } catch (const std::exception& e) {
        out.error = std::string("unexpected shape: ") + e.what();
        return false;
    }
    out.loaded = true;
    return true;
}

bool load_raster_csv(std::vector<RasterRow>& out, const std::string& path,
                     std::string* err) {
    out.clear();
    std::ifstream in(path);
    if (!in) {
        if (err) *err = "cannot open " + path;
        return false;
    }
    std::string line;
    if (!std::getline(in, line)) {
        if (err) *err = "empty file";
        return false;
    }
    // header: frame,scanline,hclock,addr,reg,value
    while (std::getline(in, line)) {
        line = trim(line);
        if (line.empty()) continue;
        std::vector<std::string> f;
        std::stringstream ss(line);
        std::string cell;
        while (std::getline(ss, cell, ',')) f.push_back(cell);
        if (f.size() < 6) continue;
        RasterRow r;
        try {
            r.frame = std::stoi(f[0]);
            r.scanline = std::stoi(f[1]);
            r.hclock = std::stoi(f[2]);
        } catch (...) {
            continue;
        }
        r.addr = f[3];
        r.reg = f[4];
        r.value = f[5];
        out.push_back(std::move(r));
    }
    if (out.empty() && err) *err = "no rows — the trace armed but caught nothing";
    return true;
}

} // namespace retcomm::studio
