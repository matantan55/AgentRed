/**
 * UNDO.cpp — C2 Full Cleanup / Uninstaller
 *
 * Reverses every change made by AppInit_DLLs.dll and SAFE.exe:
 *
 *  1. Best-effort: terminate the process holding the C2 agent singleton mutex
 *  2. Strip ACL protection from %USERPROFILE%\AppInit_DLLs.dll and SAFE.exe
 *  3. Delete both files
 *  4. Remove AppInit_DLLs persistence registry keys from HKLM
 *  5. Remove "Windows Security Health" Run key from HKCU
 *  6. Restore Windows Defender:
 *       - Remove Defender policy keys (DisableAntiSpyware, DisableAntiVirus,
 *         Real-Time Protection sub-keys)
 *       - Restore TamperProtection to enabled (5)
 *       - Re-enable + start WinDefend and WdNisSvc services
 *       - PowerShell: Set-MpPreference to restore real-time scanning
 *
 * Run as Administrator. Auto-requests elevation via UAC if not already admin.
 *
 * Build:
 *   x86_64-w64-mingw32-g++ -o UNDO.exe UNDO.cpp \
 *       -ladvapi32 -lshell32 -lshlwapi \
 *       -static -std=c++17 -O2
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <aclapi.h>
#include <tlhelp32.h>
#include <string>
#include <cstdio>

// ── Constants ─────────────────────────────────────────────────────────────────

static const char* DLL_NAME      = "AppInit_DLLs.dll";
static const char* EXE_NAME      = "SAFE.exe";
static const char* APINIT_KEY    = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Windows";
static const char* RUN_KEY       = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
static const char* RUN_VALUE     = "Windows Security Health";
static const char* SINGLETON_MTX = "Global\\__C2AgentMtx__";
static const char* DEF_KEY       = "SOFTWARE\\Policies\\Microsoft\\Windows Defender";
static const char* DEF_RT_KEY    = "SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Real-Time Protection";
static const char* DEF_FEAT_KEY  = "SOFTWARE\\Microsoft\\Windows Defender\\Features";

// ── Console helpers ───────────────────────────────────────────────────────────

static void print_ok(const char* msg)   { printf("  [OK]   %s\n", msg); }
static void print_fail(const char* msg) { printf("  [FAIL] %s\n", msg); }
static void print_info(const char* msg) { printf("  [..]   %s\n", msg); }

static void section(const char* title) {
    printf("\n--- %s ---\n", title);
}

// ── Elevation ─────────────────────────────────────────────────────────────────

static bool is_elevated() {
    BOOL el = FALSE;
    HANDLE tok = nullptr;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok)) {
        TOKEN_ELEVATION e{};
        DWORD sz = 0;
        if (GetTokenInformation(tok, TokenElevation, &e, sizeof(e), &sz))
            el = e.TokenIsElevated;
        CloseHandle(tok);
    }
    return el != FALSE;
}

static void relaunch_elevated() {
    char self[MAX_PATH]{};
    GetModuleFileNameA(nullptr, self, MAX_PATH);
    SHELLEXECUTEINFOA sei{};
    sei.cbSize    = sizeof(sei);
    sei.fMask     = SEE_MASK_NOCLOSEPROCESS;
    sei.lpVerb    = "runas";
    sei.lpFile    = self;
    sei.nShow     = SW_SHOWNORMAL;
    ShellExecuteExA(&sei);
    if (sei.hProcess) {
        WaitForSingleObject(sei.hProcess, INFINITE);
        CloseHandle(sei.hProcess);
    }
}

// ── Misc helpers ──────────────────────────────────────────────────────────────

static std::string get_userprofile() {
    char buf[MAX_PATH]{};
    if (GetEnvironmentVariableA("USERPROFILE", buf, MAX_PATH)) return buf;
    ExpandEnvironmentStringsA("%HOMEDRIVE%%HOMEPATH%", buf, MAX_PATH);
    return buf;
}

static bool file_exists(const std::string& p) {
    return GetFileAttributesA(p.c_str()) != INVALID_FILE_ATTRIBUTES;
}

static DWORD run_hidden(const std::string& cmd, DWORD timeout_ms = 30000) {
    int wn = MultiByteToWideChar(CP_UTF8, 0, cmd.c_str(), -1, nullptr, 0);
    std::wstring wcmd(wn - 1, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, cmd.c_str(), -1, &wcmd[0], wn);

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(nullptr, &wcmd[0], nullptr, nullptr, FALSE,
                        CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi))
        return (DWORD)-1;
    CloseHandle(pi.hThread);
    WaitForSingleObject(pi.hProcess, timeout_ms);
    DWORD ec = 1;
    GetExitCodeProcess(pi.hProcess, &ec);
    CloseHandle(pi.hProcess);
    return ec;
}

static bool run_ps(const std::string& ps_cmd) {
    return run_hidden(
        "powershell.exe -NonInteractive -WindowStyle Hidden "
        "-ExecutionPolicy Bypass -Command \"" + ps_cmd + "\""
    ) == 0;
}

// ── Registry helpers ──────────────────────────────────────────────────────────

static bool reg_del_value(HKEY root, const char* sub, const char* name) {
    HKEY hk;
    if (RegOpenKeyExA(root, sub, 0, KEY_SET_VALUE, &hk) != ERROR_SUCCESS) return false;
    bool ok = RegDeleteValueA(hk, name) == ERROR_SUCCESS;
    RegCloseKey(hk);
    return ok;
}

static bool reg_set_dword(HKEY root, const char* sub, const char* name, DWORD val) {
    HKEY hk;
    if (RegCreateKeyExA(root, sub, 0, nullptr, REG_OPTION_NON_VOLATILE,
                        KEY_SET_VALUE, nullptr, &hk, nullptr) != ERROR_SUCCESS)
        return false;
    bool ok = RegSetValueExA(hk, name, 0, REG_DWORD,
                             (const BYTE*)&val, sizeof(val)) == ERROR_SUCCESS;
    RegCloseKey(hk);
    return ok;
}

// ── File ACL strip ────────────────────────────────────────────────────────────
// Replaces the locked-down DACL with a world-accessible one so the file
// can be deleted by any admin.
static bool strip_acl(const std::string& path) {
    PSID pWorld = nullptr;
    SID_IDENTIFIER_AUTHORITY wAuth = SECURITY_WORLD_SID_AUTHORITY;
    AllocateAndInitializeSid(&wAuth, 1, SECURITY_WORLD_RID, 0,0,0,0,0,0,0, &pWorld);

    EXPLICIT_ACCESSA ea{};
    ea.grfAccessPermissions = GENERIC_ALL;
    ea.grfAccessMode        = GRANT_ACCESS;
    ea.grfInheritance       = NO_INHERITANCE;
    ea.Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType  = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea.Trustee.ptstrName    = (LPSTR)pWorld;

    PACL pACL = nullptr;
    bool result = false;
    if (SetEntriesInAclA(1, &ea, nullptr, &pACL) == ERROR_SUCCESS) {
        // No PROTECTED_DACL flag → clears the protection so inheritance can flow
        DWORD r = SetNamedSecurityInfoA(
            (LPSTR)path.c_str(), SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            nullptr, nullptr, pACL, nullptr
        );
        result = (r == ERROR_SUCCESS);
        LocalFree(pACL);
    }
    if (pWorld) FreeSid(pWorld);
    return result;
}

// ── Step 1: Kill agent process ────────────────────────────────────────────────
static void step_kill_agent() {
    section("1. Terminate C2 agent process");

    // If the mutex doesn't exist at all, no agent is running
    HANDLE test = OpenMutexA(SYNCHRONIZE, FALSE, SINGLETON_MTX);
    if (!test) {
        print_ok("No active agent mutex — agent not running");
        return;
    }
    CloseHandle(test);
    print_info("Agent mutex exists, searching for owner process...");

    // Walk all processes; for each one try to inject a remote thread that
    // opens the mutex. If it opens with MUTEX_MODIFY_STATE and the wait
    // returns WAIT_OBJECT_0, that process owns it. This is complex, so we
    // use the practical approach: scan processes whose module list includes
    // AppInit_DLLs.dll and terminate them.
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) {
        print_fail("Cannot create process snapshot");
        return;
    }

    DWORD self_pid = GetCurrentProcessId();
    int killed = 0;

    // Case-insensitive wide-string search
    auto wcs_istr = [](const wchar_t* hay, const wchar_t* needle) -> bool {
        if (!hay || !needle) return false;
        size_t hn = wcslen(needle);
        for (size_t i = 0; hay[i]; ++i) {
            size_t j = 0;
            while (j < hn && towlower((wint_t)hay[i+j]) == towlower((wint_t)needle[j])) ++j;
            if (j == hn) return true;
        }
        return false;
    };

    PROCESSENTRY32W pe{ sizeof(pe) };
    if (Process32FirstW(snap, &pe)) {
        do {
            if (pe.th32ProcessID == self_pid) continue;

            // Check if AppInit_DLLs.dll is loaded in this process
            HANDLE msnap = CreateToolhelp32Snapshot(
                TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pe.th32ProcessID);
            if (msnap == INVALID_HANDLE_VALUE) continue;

            MODULEENTRY32W me{ sizeof(me) };
            bool found = false;
            if (Module32FirstW(msnap, &me)) {
                do {
                    if (wcs_istr(me.szModule,  L"AppInit_DLLs") ||
                        wcs_istr(me.szExePath, L"AppInit_DLLs")) {
                        found = true;
                        break;
                    }
                } while (Module32NextW(msnap, &me));
            }
            CloseHandle(msnap);

            if (!found) continue;

            HANDLE hp = OpenProcess(PROCESS_TERMINATE, FALSE, pe.th32ProcessID);
            if (hp) {
                if (TerminateProcess(hp, 0)) {
                    printf("  [OK]   Terminated PID %lu (%ls)\n",
                           pe.th32ProcessID, pe.szExeFile);
                    killed++;
                } else {
                    printf("  [FAIL] Cannot terminate PID %lu (%ls)\n",
                           pe.th32ProcessID, pe.szExeFile);
                }
                CloseHandle(hp);
            }
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);

    if (killed == 0) {
        print_info("No process found with DLL loaded.");
        print_info("A reboot will fully unload it from memory.");
    }
}

// ── Step 2: Unprotect + delete files ─────────────────────────────────────────
static void step_delete_files() {
    section("2. Remove agent files from %USERPROFILE%");

    std::string up  = get_userprofile();
    std::string dll = up + "\\" + DLL_NAME;
    std::string exe = up + "\\" + EXE_NAME;

    for (const std::string& path : {dll, exe}) {
        if (!file_exists(path)) {
            print_ok(("Not found (already gone): " + path).c_str());
            continue;
        }

        if (strip_acl(path))
            print_info(("ACL stripped:   " + path).c_str());
        else
            print_fail(("Could not strip ACL: " + path).c_str());

        if (DeleteFileA(path.c_str())) {
            print_ok(("Deleted: " + path).c_str());
        } else {
            // DLL may be locked by a live process — schedule for next boot
            if (MoveFileExA(path.c_str(), nullptr, MOVEFILE_DELAY_UNTIL_REBOOT))
                print_ok(("Scheduled for deletion on reboot: " + path).c_str());
            else
                print_fail(("Cannot delete (still in use): " + path).c_str());
        }
    }
}

// ── Step 3: Remove AppInit_DLLs registry keys ─────────────────────────────────
static void step_remove_appinitdlls() {
    section("3. Remove AppInit_DLLs registry keys (HKLM)");

    const char* vals[] = {
        "AppInit_DLLs",
        "LoadAppInit_DLLs",
        "RequireSignedAppInit_DLLs",
    };
    for (const char* name : vals) {
        if (reg_del_value(HKEY_LOCAL_MACHINE, APINIT_KEY, name))
            print_ok(("Removed: HKLM\\...\\Windows\\" + std::string(name)).c_str());
        else
            print_info(("Not present: " + std::string(name)).c_str());
    }
}

// ── Step 4: Remove Run key ────────────────────────────────────────────────────
static void step_remove_run_key() {
    section("4. Remove startup Run key (HKCU)");

    if (reg_del_value(HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE))
        print_ok(("Removed: HKCU\\...\\Run\\" + std::string(RUN_VALUE)).c_str());
    else
        print_info("Run key not present (already removed)");
}

// ── Step 5: Restore Windows Defender ──────────────────────────────────────────
static void step_restore_defender() {
    section("5. Restore Windows Defender");

    // 5a. Restore TamperProtection to enabled (5)
    if (reg_set_dword(HKEY_LOCAL_MACHINE, DEF_FEAT_KEY, "TamperProtection", 5))
        print_ok("TamperProtection restored to 5 (enabled)");
    else
        print_fail("Could not set TamperProtection (may need SYSTEM token)");

    // 5b. Delete all policy keys set by defender_mod::disable()
    struct { HKEY root; const char* sub; const char* name; } keys[] = {
        { HKEY_LOCAL_MACHINE, DEF_KEY,    "DisableAntiSpyware"          },
        { HKEY_LOCAL_MACHINE, DEF_KEY,    "DisableAntiVirus"             },
        { HKEY_LOCAL_MACHINE, DEF_RT_KEY, "DisableRealtimeMonitoring"    },
        { HKEY_LOCAL_MACHINE, DEF_RT_KEY, "DisableBehaviorMonitoring"    },
        { HKEY_LOCAL_MACHINE, DEF_RT_KEY, "DisableOnAccessProtection"    },
        { HKEY_LOCAL_MACHINE, DEF_RT_KEY, "DisableScanOnRealtimeEnable"  },
        { HKEY_LOCAL_MACHINE, DEF_RT_KEY, "DisableIOAVProtection"        },
    };
    for (auto& k : keys) {
        if (reg_del_value(k.root, k.sub, k.name))
            print_ok(("Removed policy key: " + std::string(k.name)).c_str());
        else
            print_info(("Not present: " + std::string(k.name)).c_str());
    }

    // 5c. PowerShell: restore real-time protection preferences
    print_info("Running Set-MpPreference to restore protection...");
    bool ps_ok = run_ps(
        "Set-MpPreference "
        "-DisableRealtimeMonitoring $false "
        "-DisableBehaviorMonitoring $false "
        "-DisableBlockAtFirstSeen $false "
        "-DisableIOAVProtection $false "
        "-MAPSReporting Advanced "
        "-SubmitSamplesConsent SendSafeSamples"
    );
    if (ps_ok) print_ok("Set-MpPreference: real-time protection restored");
    else       print_fail("Set-MpPreference failed (Defender may already be re-enabled or blocked)");

    // 5d. Re-enable and start Defender services
    const char* services[] = { "WinDefend", "WdNisSvc", "WdFilter" };
    for (const char* svc_name : services) {
        SC_HANDLE scm = OpenSCManagerA(nullptr, nullptr, SC_MANAGER_ALL_ACCESS);
        if (!scm) { print_fail("Cannot open SCM"); continue; }

        SC_HANDLE svc = OpenServiceA(scm, svc_name,
            SERVICE_START | SERVICE_CHANGE_CONFIG | SERVICE_QUERY_STATUS);
        if (svc) {
            // Restore automatic start
            ChangeServiceConfigA(svc, SERVICE_NO_CHANGE, SERVICE_AUTO_START,
                SERVICE_NO_CHANGE, nullptr, nullptr, nullptr,
                nullptr, nullptr, nullptr, nullptr);

            SERVICE_STATUS ss{};
            QueryServiceStatus(svc, &ss);
            if (ss.dwCurrentState != SERVICE_RUNNING) {
                if (StartServiceA(svc, 0, nullptr))
                    print_ok((std::string("Started service: ") + svc_name).c_str());
                else
                    print_info((std::string("Could not start (may need reboot): ") + svc_name).c_str());
            } else {
                print_ok((std::string("Already running: ") + svc_name).c_str());
            }
            CloseServiceHandle(svc);
        } else {
            print_info((std::string("Service not found (driver-level, normal): ") + svc_name).c_str());
        }
        CloseServiceHandle(scm);
    }
}

// ── Entry point ───────────────────────────────────────────────────────────────

int main() {
    printf("========================================\n");
    printf("  C2 Cleanup / Undo  (UNDO.exe)\n");
    printf("========================================\n");

    if (!is_elevated()) {
        printf("\n  Not running as Administrator.\n");
        printf("  Requesting elevation via UAC...\n\n");
        relaunch_elevated();
        return 0;
    }

    printf("\n  Running as Administrator.\n");

    step_kill_agent();
    step_delete_files();
    step_remove_appinitdlls();
    step_remove_run_key();
    step_restore_defender();

    printf("\n========================================\n");
    printf("  Cleanup complete.\n");
    printf("  RECOMMENDATION: Reboot the machine to\n");
    printf("  fully unload the DLL from all processes.\n");
    printf("========================================\n\n");

    printf("Press Enter to exit...\n");
    getchar();
    return 0;
}
