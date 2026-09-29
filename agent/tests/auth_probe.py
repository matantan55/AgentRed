import json, socket, sys

def probe(token, timeout=6):
    s = socket.create_connection(("127.0.0.1", 4444), timeout=timeout)
    s.sendall((json.dumps({"token": token}) + "\n").encode())
    line = s.makefile("rb").readline().decode()
    s.close()
    return line.strip()

for tok in (sys.argv[1] if len(sys.argv) > 1 else "labtoken", "changeme"):
    try:
        print(f"token={tok!r:12} ->", probe(tok))
    except Exception as exc:
        print(f"token={tok!r:12} -> ERROR {exc}")
