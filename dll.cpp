#include <windows.h>


BOOL APIENTRY DllMain(HMODULE hModule,  DWORD  nReason, LPVOID lpReserved) {
	switch (nReason) {
	case DLL_PROCESS_ATTACH:
		// here
    	break;
	case DLL_PROCESS_DETACH:
    	break;
	case DLL_THREAD_ATTACH:
		break;
	case DLL_THREAD_DETACH:
		break;
	}
	return TRUE;
}
// x86_64-w64-mingw32-gcc -shared -o config.dll dll.cpp -fpermissive
