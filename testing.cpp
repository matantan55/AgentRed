// main.cpp — demo: show which process PickRandomSignedProcess selects
// Compile (MinGW):
//   x86_64-w64-mingw32-g++ --static testing.cpp -o proc.exe -lwintrust
// Compile (MSVC):
//   cl /std:c++17 /W3 main.cpp /link wintrust.lib

#include "process_steal.h"

// ── Convert wstring to narrow string for printf ──────────────────────────────
// Avoids mixing printf/wprintf which breaks on MinGW CRT
static std::string W(const std::wstring& ws)
{
    if (ws.empty()) return {};
    int sz = WideCharToMultiByte(CP_ACP, 0, ws.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string s(sz - 1, '\0');
    WideCharToMultiByte(CP_ACP, 0, ws.c_str(), -1, s.data(), sz, nullptr, nullptr);
    return s;
}

static std::string W(const wchar_t* ws)
{
    return ws ? W(std::wstring(ws)) : std::string("<null>");
}

// ── Resolve token user string for display ────────────────────────────────────
static std::wstring GetTokenUser(DWORD pid)
{
    HANDLE hProc = OpenProcess(PROCESS_QUERY_INFORMATION, FALSE, pid);
    if (!hProc) return L"<access denied>";

    HANDLE hTok = NULL;
    if (!OpenProcessToken(hProc, TOKEN_QUERY, &hTok))
    {
        CloseHandle(hProc);
        return L"<token denied>";
    }
    CloseHandle(hProc);

    DWORD needed = 0;
    GetTokenInformation(hTok, TokenUser, NULL, 0, &needed);

    std::vector<BYTE> buf(needed);
    std::wstring result = L"<unknown>";

    if (GetTokenInformation(hTok, TokenUser, buf.data(), needed, &needed))
    {
        auto* tu = reinterpret_cast<TOKEN_USER*>(buf.data());
        wchar_t name[256] = {}, domain[256] = {};
        DWORD nLen = 256, dLen = 256;
        SID_NAME_USE use;
        if (LookupAccountSidW(NULL, tu->User.Sid, name, &nLen, domain, &dLen, &use))
            result = std::wstring(domain) + L"\\" + name;
    }

    CloseHandle(hTok);
    return result;
}

// ── Get integrity level label ────────────────────────────────────────────────
static const wchar_t* GetIntegrityLevel(DWORD pid)
{
    HANDLE hProc = OpenProcess(PROCESS_QUERY_INFORMATION, FALSE, pid);
    if (!hProc) return L"<denied>";

    HANDLE hTok = NULL;
    if (!OpenProcessToken(hProc, TOKEN_QUERY, &hTok))
    {
        CloseHandle(hProc);
        return L"<denied>";
    }
    CloseHandle(hProc);

    DWORD needed = 0;
    GetTokenInformation(hTok, TokenIntegrityLevel, NULL, 0, &needed);

    std::vector<BYTE> buf(needed);
    const wchar_t* label = L"<unknown>";

    if (GetTokenInformation(hTok, TokenIntegrityLevel, buf.data(), needed, &needed))
    {
        auto* tml = reinterpret_cast<TOKEN_MANDATORY_LABEL*>(buf.data());
        DWORD* subAuth = GetSidSubAuthority(
            tml->Label.Sid,
            *GetSidSubAuthorityCount(tml->Label.Sid) - 1);

        if      (*subAuth < SECURITY_MANDATORY_LOW_RID)    label = L"Untrusted";
        else if (*subAuth < SECURITY_MANDATORY_MEDIUM_RID) label = L"Low";
        else if (*subAuth < SECURITY_MANDATORY_HIGH_RID)   label = L"Medium";
        else if (*subAuth < SECURITY_MANDATORY_SYSTEM_RID) label = L"High";
        else                                               label = L"System";
    }

    CloseHandle(hTok);
    return label;
}

// ── Simple ASCII table helpers ───────────────────────────────────────────────
static void Row(const char* label, const char* value)
{
    printf("  %-22s: %s\n", label, value);
}
static void Row(const char* label, DWORD value)
{
    char buf[32];
    sprintf(buf, "%lu", value);
    Row(label, buf);
}
static void Divider() { printf("  %s\n", std::string(50, '-').c_str()); }

// ─────────────────────────────────────────────────────────────────────────────
int main()
{
    // Set console to UTF-8 so narrow strings with non-ASCII don't garble
    SetConsoleOutputCP(CP_UTF8);

    printf("\n");
    printf("  === Process Picker: signed + unmonitored ===\n\n");

    // 1. Init ntdll resolver
    if (!InitNtdll())
    {
        fprintf(stderr, "[-] Failed to resolve NtQueryInformationProcess.\n");
        return 1;
    }

    // 2. Pick a process
    printf("[*] Scanning for a signed + unmonitored process...\n\n");
    SignedProcessInfo pick = PickRandomSignedProcess(/*maxAttempts=*/64);

    if (pick.pid == 0)
    {
        fprintf(stderr, "\n[-] No suitable process found.\n");
        return 1;
    }

    // 3. Print details — all via printf with narrow strings (no wprintf)
    printf("\n");
    Divider();
    printf("  SELECTED PROCESS\n");
    Divider();
    Row("PID",               pick.pid);
    Row("Name",              W(pick.exeName).c_str());
    Row("Image path",        W(pick.imagePath).c_str());
    Row("Token user",        W(GetTokenUser(pick.pid)).c_str());
    Row("Integrity level",   W(GetIntegrityLevel(pick.pid)).c_str());
    Row("Thread count",      GetThreadCount(pick.pid));
    DWORD sessionId = 0;
    ProcessIdToSessionId(pick.pid, &sessionId);
    Row("Session",           sessionId);
    Divider();
    Row("Authenticode signed", "YES");
    Row("EDR DLLs detected",   "NO");
    Row("Debugger attached",   "NO");
    Divider();

    // 4. PEB strings
    printf("\n[*] Reading PEB strings from target...\n\n");
    ProcessStrings strings;
    if (ReadRemoteProcessStrings(pick.pid, strings))
    {
        Row("CommandLine",   W(strings.commandLine).c_str());
        Row("ImagePathName", W(strings.imagePathName).c_str());
        if (!strings.windowTitle.empty())
            Row("WindowTitle", W(strings.windowTitle).c_str());
    }
    else
    {
        printf("  (PEB read failed - process may have restricted access)\n");
    }

    printf("\n[*] Done. Press Enter to exit...\n");
    getchar();
    return 0;
}
