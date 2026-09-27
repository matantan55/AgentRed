// main.cpp — demo: show which process PickRandomSignedProcess selects
// Compile (MSVC):
//   cl /std:c++17 /W3 main.cpp /link wintrust.lib
// Compile (MinGW):
//   g++ -std=c++17 main.cpp -o picker.exe -lwintrust -lntdll

#include "process_steal.h"

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
        {
            result = std::wstring(domain) + L"\\" + name;
        }
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

        if      (*subAuth < SECURITY_MANDATORY_LOW_RID)      label = L"Untrusted";
        else if (*subAuth < SECURITY_MANDATORY_MEDIUM_RID)   label = L"Low";
        else if (*subAuth < SECURITY_MANDATORY_HIGH_RID)     label = L"Medium";
        else if (*subAuth < SECURITY_MANDATORY_SYSTEM_RID)   label = L"High";
        else                                                  label = L"System";
    }

    CloseHandle(hTok);
    return label;
}

// ── Banner ───────────────────────────────────────────────────────────────────
static void PrintBanner()
{
    printf("╔══════════════════════════════════════════════════╗\n");
    printf("║       Process Picker — signed + unmonitored      ║\n");
    printf("╚══════════════════════════════════════════════════╝\n\n");
}

// ─────────────────────────────────────────────────────────────────────────────
int main()
{
    PrintBanner();

    // 1. Init ntdll resolver (needed by ReadRemoteProcessStrings)
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

    // 3. Print full details
    printf("\n");
    printf("┌─────────────────────────────────────────────────────┐\n");
    printf("│                  SELECTED PROCESS                   │\n");
    printf("├──────────────────────┬──────────────────────────────┤\n");
    printf("│ PID                  │ %-28lu │\n", pick.pid);
    wprintf(L"│ Name                 │ %-28ws │\n", pick.exeName.c_str());
    wprintf(L"│ Image path           │ %-28ws │\n", pick.imagePath.c_str());

    // Token user
    std::wstring tokenUser = GetTokenUser(pick.pid);
    wprintf(L"│ Token user           │ %-28ws │\n", tokenUser.c_str());

    // Integrity level
    const wchar_t* integrity = GetIntegrityLevel(pick.pid);
    wprintf(L"│ Integrity level      │ %-28ws │\n", integrity);

    // Thread count
    DWORD threads = GetThreadCount(pick.pid);
    printf("│ Thread count         │ %-28lu │\n", threads);

    // Session
    DWORD sessionId = 0;
    ProcessIdToSessionId(pick.pid, &sessionId);
    printf("│ Session              │ %-28lu │\n", sessionId);

    printf("├──────────────────────┴──────────────────────────────┤\n");
    printf("│ Authenticode signed  │ YES                          │\n");
    printf("│ EDR DLLs detected    │ NO                           │\n");
    printf("│ Debugger attached    │ NO                           │\n");
    printf("└──────────────────────────────────────────────────────┘\n");

    // 4. Read PEB strings
    printf("\n[*] Reading PEB strings from target...\n");
    ProcessStrings strings;
    if (ReadRemoteProcessStrings(pick.pid, strings))
    {
        wprintf(L"\n    CommandLine   : %ws\n", strings.commandLine.c_str());
        wprintf(L"    ImagePathName : %ws\n",   strings.imagePathName.c_str());
        if (!strings.windowTitle.empty())
            wprintf(L"    WindowTitle   : %ws\n", strings.windowTitle.c_str());
    }
    else
    {
        printf("    (PEB read failed — process may have restricted access)\n");
    }

    printf("\n[*] Done. Press Enter to exit...\n");
    getchar();
    return 0;
}
