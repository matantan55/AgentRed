"""
Activity Monitor Module — C2 Feature #4
Reports running processes, CPU/RAM/disk/network stats, active network
connections, and top resource consumers on the target agent.
Relies on psutil (cross-platform).
For educational/lab use only.
"""

import time
import threading
import datetime
from typing import Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


class ActivityModule:
    """Cross-platform activity/resource monitor."""

    def __init__(self):
        self._snapshots: list[dict] = []
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle(self, payload: dict) -> dict:
        """
        Expected payload keys:
            action   : "processes" | "system_stats" | "network_connections"
                       | "top_cpu" | "top_mem" | "start_monitor" | "stop_monitor"
                       | "get_snapshots"
            pid      : int  (optional — filter processes by PID)
            name     : str  (optional — filter processes by name substring)
            interval : int  (seconds between monitor snapshots, default 5)
            limit    : int  (top-N for top_cpu / top_mem, default 10)
        """
        if not PSUTIL_AVAILABLE:
            return {
                "status":  "error",
                "message": "psutil is not installed. Run: pip install psutil",
            }

        action = payload.get("action", "")
        handlers = {
            "processes":          self._list_processes,
            "system_stats":       self._system_stats,
            "network_connections": self._network_connections,
            "top_cpu":            self._top_cpu,
            "top_mem":            self._top_mem,
            "start_monitor":      self._start_monitor,
            "stop_monitor":       self._stop_monitor,
            "get_snapshots":      self._get_snapshots,
        }
        fn = handlers.get(action)
        if fn is None:
            return {"status": "error", "message": f"Unknown activity action: {action!r}"}
        return fn(payload)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _list_processes(self, p: dict) -> dict:
        pid_filter  = p.get("pid")
        name_filter = (p.get("name") or "").lower()

        procs = []
        for proc in psutil.process_iter(
            ["pid", "name", "username", "status", "cpu_percent",
             "memory_percent", "create_time", "cmdline"]
        ):
            try:
                info = proc.info
                if pid_filter is not None and info["pid"] != int(pid_filter):
                    continue
                if name_filter and name_filter not in (info["name"] or "").lower():
                    continue
                info["create_time"] = datetime.datetime.fromtimestamp(
                    info["create_time"]
                ).isoformat() if info.get("create_time") else None
                info["cmdline"] = " ".join(info.get("cmdline") or [])
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return {"status": "ok", "count": len(procs), "processes": procs}

    def _system_stats(self, _p: dict) -> dict:
        cpu_times   = psutil.cpu_times_percent(interval=1)
        vm          = psutil.virtual_memory()
        swap        = psutil.swap_memory()
        disk_parts  = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disk_parts.append({
                    "device":     part.device,
                    "mountpoint": part.mountpoint,
                    "fstype":     part.fstype,
                    "total_gb":   round(usage.total / 1e9, 2),
                    "used_gb":    round(usage.used  / 1e9, 2),
                    "free_gb":    round(usage.free  / 1e9, 2),
                    "percent":    usage.percent,
                })
            except PermissionError:
                pass

        net_io = psutil.net_io_counters()

        return {
            "status": "ok",
            "timestamp": datetime.datetime.now().isoformat(),
            "cpu": {
                "physical_cores": psutil.cpu_count(logical=False),
                "logical_cores":  psutil.cpu_count(logical=True),
                "percent":        psutil.cpu_percent(interval=1),
                "user":           cpu_times.user,
                "system":         cpu_times.system,
                "idle":           cpu_times.idle,
                "frequency_mhz":  (psutil.cpu_freq().current
                                   if psutil.cpu_freq() else None),
            },
            "memory": {
                "total_gb":   round(vm.total   / 1e9, 2),
                "available_gb": round(vm.available / 1e9, 2),
                "used_gb":    round(vm.used    / 1e9, 2),
                "percent":    vm.percent,
                "swap_total_gb": round(swap.total / 1e9, 2),
                "swap_used_gb":  round(swap.used  / 1e9, 2),
                "swap_percent":  swap.percent,
            },
            "disk":    disk_parts,
            "network": {
                "bytes_sent":   net_io.bytes_sent,
                "bytes_recv":   net_io.bytes_recv,
                "packets_sent": net_io.packets_sent,
                "packets_recv": net_io.packets_recv,
                "errin":        net_io.errin,
                "errout":       net_io.errout,
            },
        }

    def _network_connections(self, _p: dict) -> dict:
        conns = []
        for c in psutil.net_connections(kind="inet"):
            laddr = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""
            raddr = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
            conns.append({
                "fd":     c.fd,
                "family": str(c.family),
                "type":   str(c.type),
                "laddr":  laddr,
                "raddr":  raddr,
                "status": c.status,
                "pid":    c.pid,
            })
        return {"status": "ok", "count": len(conns), "connections": conns}

    def _top_cpu(self, p: dict) -> dict:
        limit = int(p.get("limit", 10))
        procs = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent"]):
            try:
                procs.append(proc.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        # Need two samples for accurate cpu_percent
        time.sleep(0.5)
        updated = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent"]):
            try:
                updated.append(proc.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        top = sorted(updated, key=lambda x: x.get("cpu_percent") or 0,
                     reverse=True)[:limit]
        return {"status": "ok", "top_cpu": top}

    def _top_mem(self, p: dict) -> dict:
        limit = int(p.get("limit", 10))
        procs = []
        for proc in psutil.process_iter(["pid", "name", "memory_percent"]):
            try:
                procs.append(proc.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        top = sorted(procs, key=lambda x: x.get("memory_percent") or 0,
                     reverse=True)[:limit]
        return {"status": "ok", "top_mem": top}

    # ------------------------------------------------------------------
    # Background monitor (continuous snapshots)
    # ------------------------------------------------------------------

    def _start_monitor(self, p: dict) -> dict:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return {"status": "error", "message": "Monitor already running"}

        interval = int(p.get("interval", 5))
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.is_set():
                snap = self._system_stats({})
                with self._lock:
                    self._snapshots.append(snap)
                    # Keep last 200 snapshots (~16 min at 5s)
                    if len(self._snapshots) > 200:
                        self._snapshots = self._snapshots[-200:]
                self._stop_event.wait(interval)

        self._monitor_thread = threading.Thread(target=_loop, daemon=True,
                                                name="c2-activity-monitor")
        self._monitor_thread.start()
        return {"status": "ok",
                "message": f"Activity monitor started (interval={interval}s)"}

    def _stop_monitor(self, _p: dict) -> dict:
        self._stop_event.set()
        return {"status": "ok", "message": "Activity monitor stopped"}

    def _get_snapshots(self, p: dict) -> dict:
        last_n = int(p.get("limit", 10))
        with self._lock:
            snaps = self._snapshots[-last_n:]
        return {"status": "ok", "count": len(snaps), "snapshots": snaps}
