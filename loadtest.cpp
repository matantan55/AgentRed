// loadtest.cpp — single-process harness for AppInit_DLLs.dll.
// Loads the DLL exactly the way Windows would (DllMain runs), then keeps the
// process alive so you can watch the agent connect in the server log.
// If this process crashes: the DLL itself faults (note the exception code).
// If it loads fine but AppInit deployment still breaks the machine: the
// problem is the injection context (every-process), not the DLL's logic.
//
//   Build:  x86_64-w64-mingw32-g++ --static loadtest.cpp -o loadtest.exe -luser32
//   Run on VM (cmd, same folder as the DLL):   loadtest.exe
//   Ctrl+C / close window = detach; agent thread dies with the process.
#include <windows.h>
#include <stdio.h>

int main() {
    printf("[loadtest] LoadLibrary AppInit_DLLs.dll ...\n");
    HMODULE h = LoadLibraryA("AppInit_DLLs.dll");
    if (!h) {
        printf("[loadtest] LOAD FAILED — Win32 error %lu\n", GetLastError());
        return 1;
    }
    printf("[loadtest] DLL loaded OK. Keeping process alive for 120 s.\n");
    printf("[loadtest] Watch the C2 server log for the agent connect line.\n");
    for (int i = 0; i < 120; i++) {
        Sleep(1000);
        if (i % 15 == 14) printf("[loadtest] still alive... %d s\n", i + 1);
    }
    printf("[loadtest] done — FreeLibrary + exit.\n");
    FreeLibrary(h);
    return 0;
}
