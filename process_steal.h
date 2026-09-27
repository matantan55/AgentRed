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

    // ── 3. Verify one-by-one; stop at first signed hit or after maxAttempts ──
    DWORD attempts = 0;
    for (const auto& e : entries)
    {
        if (attempts++ >= maxAttempts) break;

        std::wstring path = GetProcessImagePath(e.pid);
        if (path.empty()) continue;

        if (VerifyFileSignature(path) == ERROR_SUCCESS)
        {
            SignedProcessInfo result;
            result.pid       = e.pid;
            result.exeName   = e.exeName;
            result.imagePath = path;
            printf("[+] Selected signed process: PID %-6lu %ws\n",
                   result.pid, result.exeName.c_str());
            return result;
        }
    }

    printf("[-] No signed process found within %lu attempts.\n", maxAttempts);
    return {}; // pid == 0 signals failure
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

static void ApplyToCurrentProcess(const ProcessStrings& src)
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

    printf("\n[+] Done. This process now looks like the target in Task Manager.\n");
}
