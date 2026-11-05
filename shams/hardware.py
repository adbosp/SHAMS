"""Real hardware of the machine the controller runs on (ADR-118).

Reads GPUs (name, total / used VRAM, utilisation), system RAM and CPU without extra dependencies:

* NVIDIA: ``nvidia-smi`` (Windows, Linux).
* AMD: ``rocm-smi --json`` (Linux), else the Windows registry value ``HardwareInformation.qwMemorySize``.
* Apple Silicon: unified memory reported by ``sysctl`` (VRAM = shared RAM).
* Anything else: total only, from the OS (Windows WMI); ``used_mb`` is then None.

Results are cached for a short time so the UI can poll every second. Nothing here is required for
SHAMS to run: on a PC without GPU ``detect()["gpus"]`` is simply empty (ADR-114).
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time

_CACHE: dict[str, tuple[float, object]] = {}
_LOCK = threading.Lock()
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW: no console flashes on Windows


def _run(argv: list[str], timeout: float = 5.0) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                           errors="replace", creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


# --------------------------------------------------------------------------- GPUs
def parse_nvidia_smi(out: str) -> list[dict]:
    """Rows of `nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu,driver_version
    --format=csv,noheader,nounits`."""
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue

        def num(s: str) -> int | None:
            try:
                return int(float(s))
            except ValueError:
                return None
        gpus.append({"index": num(parts[0]) or 0, "name": parts[1], "vendor": "NVIDIA",
                     "vram_total_mb": num(parts[2]) or 0, "vram_used_mb": num(parts[3]),
                     "utilization_pct": num(parts[4]) if len(parts) > 4 else None,
                     "driver": parts[5] if len(parts) > 5 else None, "source": "nvidia-smi"})
    return gpus


def _nvidia() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe and os.name == "nt":
        cand = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "NVSMI",
                            "nvidia-smi.exe")
        exe = cand if os.path.exists(cand) else None
    if not exe:
        return []
    out = _run([exe, "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,driver_version",
                "--format=csv,noheader,nounits"])
    return parse_nvidia_smi(out) if out else []


def parse_rocm_smi(out: str) -> list[dict]:
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    gpus = []
    for i, (card, v) in enumerate(sorted(data.items())):
        if not isinstance(v, dict) or not card.startswith("card"):
            continue
        total = v.get("VRAM Total Memory (B)") or v.get("vram Total Memory (B)")
        used = v.get("VRAM Total Used Memory (B)") or v.get("vram Total Used Memory (B)")
        if total is None:
            continue
        gpus.append({"index": i, "name": v.get("Card series") or v.get("Card SKU") or card, "vendor": "AMD",
                     "vram_total_mb": int(total) // 2**20,
                     "vram_used_mb": int(used) // 2**20 if used is not None else None,
                     "utilization_pct": None, "driver": None, "source": "rocm-smi"})
    return gpus


def _amd_linux() -> list[dict]:
    if not shutil.which("rocm-smi"):
        return []
    out = _run(["rocm-smi", "--showmeminfo", "vram", "--showproductname", "--json"])
    return parse_rocm_smi(out) if out else []


_PS_GPUS = r"""
$out = @()
Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}' -ErrorAction SilentlyContinue |
 ForEach-Object { $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
  if ($p.DriverDesc) { $out += [pscustomobject]@{ name = $p.DriverDesc; bytes = $p.'HardwareInformation.qwMemorySize'; legacy = $p.'HardwareInformation.MemorySize' } } }
$out | ConvertTo-Json -Compress
"""


def _windows_generic() -> list[dict]:
    """Total VRAM of every display adapter from the registry (used VRAM is not exposed here)."""
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_GPUS], timeout=8)
    if not out or not out.strip():
        return []
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return []
    rows = rows if isinstance(rows, list) else [rows]
    gpus = []
    for r in rows:
        b = r.get("bytes") or r.get("legacy")
        if isinstance(b, list):           # REG_BINARY comes back as a byte list
            b = int.from_bytes(bytes(b), "little")
        try:
            total = int(b) // 2**20 if b else 0
        except (TypeError, ValueError):
            total = 0
        name = str(r.get("name") or "GPU")
        if total < 256 or "basic display" in name.lower() or "remote" in name.lower():
            continue                          # software / virtual adapters
        vendor = "NVIDIA" if "nvidia" in name.lower() else "AMD" if re.search(r"amd|radeon", name, re.I) \
            else "Intel" if "intel" in name.lower() else ""
        gpus.append({"index": len(gpus), "name": name, "vendor": vendor, "vram_total_mb": total, "vram_used_mb": None,
                     "utilization_pct": None, "driver": None, "source": "windows-registry"})
    return gpus


def _apple() -> list[dict]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return []
    mem = _run(["sysctl", "-n", "hw.memsize"])
    name = (_run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon").strip()
    if not mem:
        return []
    return [{"index": 0, "name": f"{name} GPU (unified memory)", "vendor": "Apple",
             "vram_total_mb": int(mem) // 2**20, "vram_used_mb": None, "utilization_pct": None,
             "driver": None, "source": "sysctl", "unified": True}]


def gpus() -> list[dict]:
    found = _nvidia()
    if not found and platform.system() == "Linux":
        found = _amd_linux()
    if not found and os.name == "nt":
        found = _windows_generic()
    if not found:
        found = _apple()
    return found


# --------------------------------------------------------------------------- RAM / CPU
def memory() -> dict:
    """{"total_mb", "available_mb"} of system RAM, or Nones when unknown."""
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            s = MEMORYSTATUSEX()
            s.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return {"total_mb": s.ullTotalPhys // 2**20, "available_mb": s.ullAvailPhys // 2**20}
        if os.path.exists("/proc/meminfo"):
            info = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0]) // 1024
            return {"total_mb": info.get("MemTotal"), "available_mb": info.get("MemAvailable")}
        if platform.system() == "Darwin":
            total = _run(["sysctl", "-n", "hw.memsize"])
            return {"total_mb": int(total) // 2**20 if total else None, "available_mb": None}
    except Exception:  # never let hardware probing break the controller
        pass
    return {"total_mb": None, "available_mb": None}


def cpu() -> dict:
    name = platform.processor() or platform.machine()
    if platform.system() == "Linux" and os.path.exists("/proc/cpuinfo"):
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            m = re.search(r"model name\s*:\s*(.+)", f.read())
            name = m.group(1).strip() if m else name
    return {"name": name, "cores": os.cpu_count() or 1}


# --------------------------------------------------------------------------- public
def detect(max_age_s: float = 2.0) -> dict:
    """Snapshot of this machine. GPU list is cached for `max_age_s` seconds (static info for 60 s)."""
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get("detect")
        if hit and now - hit[0] < max_age_s:
            return hit[1]  # type: ignore[return-value]
        static = _CACHE.get("static")
        if not static or now - static[0] > 60:
            static = (now, {"os": f"{platform.system()} {platform.release()}", "hostname": platform.node(),
                            "cpu": cpu()})
            _CACHE["static"] = static
        g = gpus()
        snap = {**static[1], "ram": memory(), "gpus": g, "ts": time.time()}
        _CACHE["detect"] = (now, snap)
        return snap


def local_node(snapshot: dict | None = None, node_id: str = "node.local") -> dict:
    """A nodes.toml [[node]] for this machine, from real hardware."""
    s = snapshot or detect()
    return {"id": node_id, "kind": "local", "cpu_slots": max(1, min(4, s["cpu"]["cores"] // 4)),
            "gpus": [{"index": g["index"], "vram_mb": g["vram_total_mb"], "name": g["name"]} for g in s["gpus"]]}
