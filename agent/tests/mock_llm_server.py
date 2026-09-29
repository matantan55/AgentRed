"""Scripted chat-completions server to test agent.py's LLM loop offline."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

SCEN = os.environ.get("SCENARIO", "cpu")
SCRIPTS = {
    "cpu": [
        {"thought": "The researcher wants the busiest process — ask the target directly.",
         "action": {"tool": "top_cpu", "args": {"limit": 5}}},
        {"thought": "Observation is clear; report.",
         "final": "Top CPU consumer on LAB-VM-04 is chrome.exe (pid 2048, 7.5%), "
                  "then MsMpEng.exe (Defender engine, 3.1%). Nothing anomalous."},
    ],
    "reg": [
        {"thought": "Adding the run key mutates the target — propose reg_write; the human must confirm.",
         "action": {"tool": "reg_write",
                    "args": {"hive": "HKCU",
                             "key_path": r"Software\Microsoft\Windows\CurrentVersion\Run",
                             "value_name": "LabNote", "value_data": "notepad.exe"}}},
        {"thought": "The write succeeded after operator approval.",
         "final": "Registry value LabNote = notepad.exe was written under HKCU\\...\\Run "
                  "after operator approval. Note: this is exactly the persistence pattern "
                  "the lab wants us to detect."},
    ],
    "file": [
        {"thought": "File access needs the shell channel; it's state-changing, human confirms.",
         "action": {"tool": "shell_open", "args": {"lhost": "127.0.0.1", "lport": 4445}}},
        {"thought": "Channel open — read the requested file.",
         "action": {"tool": "file_read", "args": {"path": r"C:\Users\lab\notes.txt"}}},
        {"thought": "Content captured; close the channel politely.",
         "action": {"tool": "shell_close", "args": {}}},
        {"thought": "Done — summarize.",
         "final": "notes.txt contains the Lab-4 telemetry notes: agent loaded via "
                  "AppInit_DLLs, beacon ~5s jittered, and a TODO to correlate notify "
                  "events with process creation. Shell channel was opened for the read "
                  "and closed afterwards."},
    ],
}
counter = {"i": 0}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        delay = float(os.environ.get("SLEEP", "0"))
        if delay:
            import time
            time.sleep(delay)
        msgs = payload.get("messages", [])
        sys_len = len(json.dumps(msgs[0])) if msgs else 0
        script = SCRIPTS[SCEN]
        reply = script[min(counter["i"], len(script) - 1)]
        counter["i"] += 1
        body = json.dumps({
            "id": "mock", "object": "chat.completion", "model": payload.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": json.dumps(reply)}}],
            "usage": {"prompt_tokens": sys_len, "completion_tokens": 50, "total_tokens": sys_len + 50},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8899), H).serve_forever()
