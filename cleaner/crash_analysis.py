"""藍屏與穩定性診斷:蒐集系統事件、傾印檔、硬體健康資訊,並推論「最可能的原因方向」。

**純讀取、零修改、零刪除。** 呼叫的外部程式只有 wevtutil(讀事件記錄)與 PowerShell 的
Get-CimInstance/Get-PhysicalDisk(讀 WMI),全部是 Windows 內建工具,不需要額外安裝。
smartctl 若在(見 smart_health.py)就一併採用,不在也不影響其他項目。

設計原則(給接手者):
1. 每個蒐集函式**絕不丟例外**——別人的電腦環境千奇百怪(事件記錄被清空、沒有權限、
   WMI 壞掉),某一項失敗只能讓那一項缺資料,不能讓整個診斷失敗。失敗的項目記進
   data["errors"],報告會誠實標示「這項讀不到」,而不是假裝沒問題。
2. 輸出語系無關:事件用 /f:xml 取結構化欄位,PowerShell 輸出 JSON,不比對任何中英文標籤。
3. 這個模組只負責「蒐集 + 推論」,不碰 Qt;報告輸出在 crash_report.py。
"""

import base64
import ctypes
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import bugcheck_db, smart_health
from .utils.fs import list_drives

try:
    import winreg
except ImportError:  # 非 Windows 環境(例如在別的系統跑單元測試)
    winreg = None

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 90

CATEGORY_LABELS = {
    "storage": "儲存裝置(SSD/硬碟/儲存驅動)",
    "driver": "驅動程式或軟體衝突",
    "memory": "記憶體",
    "gpu": "顯示卡/顯示驅動",
    "hardware": "CPU/主機板/散熱等硬體",
    "power": "電源/睡眠喚醒",
}

# 儲存事件的白話說明(provider, event id) -> 說明。只收「確實代表異常」的 ID,
# 刻意不收 disk 157/158(USB 隨身碟被直接拔掉會觸發,屬於正常使用)以免誤報。
STORAGE_EVENT_NOTES = {
    ("disk", 7): "磁碟有壞區(bad block)",
    ("disk", 11): "磁碟控制器錯誤",
    ("disk", 15): "磁碟裝置未就緒",
    ("disk", 51): "分頁 I/O 時磁碟出錯",
    ("disk", 52): "磁碟預測即將故障",
    ("disk", 153): "磁碟 I/O 需要重試",
    ("stornvme", 129): "NVMe 裝置逾時被重置",
    ("storahci", 129): "SATA 裝置逾時被重置",
    ("iaStorAVC", 129): "Intel RST 儲存裝置逾時被重置",
    ("iaStorAC", 129): "Intel RST 儲存裝置逾時被重置",
    ("iaStorV", 129): "Intel RST 儲存裝置逾時被重置",
    ("Ntfs", 55): "NTFS 檔案系統結構損毀",
    ("Ntfs", 140): "NTFS 寫入快取清除失敗",
}
# 較嚴重(有資料損毀/硬體故障意涵)的 ID,出現就直接判紅燈
STORAGE_SEVERE = {("disk", 7), ("disk", 52), ("Ntfs", 55), ("disk", 51)}

_EVT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 微軟內建驅動的日期常是 2006 年(系統映像日期),不代表「過舊」,所以計算驅動老舊時排除
_DRIVER_OLD_YEARS = 3


# ── 低階工具 ────────────────────────────────────────────────────────────────

def _run(args: list[str], timeout: int = 30) -> bytes | None:
    """執行外部程式,回傳 stdout(bytes);失敗/逾時回傳 None。不顯示黑窗。"""
    try:
        result = subprocess.run(
            args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("run %s failed: %s", args[0], e)
        return None
    return result.stdout


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _parse_event_time(s: str) -> datetime | None:
    """'2026-10-20T03:18:29.2435029Z' -> aware datetime(UTC)。Python 只吃到微秒,要截斷。"""
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z?", s or "")
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    try:
        return datetime.fromisoformat(f"{m.group(1)}.{frac}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _local_str(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else ""


def query_events(cond: str, max_events: int = 200, days: int | None = LOOKBACK_DAYS,
                 channel: str = "System", newest_first: bool = True) -> list[dict] | None:
    """用 wevtutil 查事件記錄,回傳事件清單(新到舊);查詢本身失敗回傳 None(≠ 查到 0 筆)。

    cond 是 XPath 的 System 條件式,例如 "Provider[@Name='disk'] and EventID=7"。
    只保留診斷需要的欄位——刻意**不**帶 Computer、Binary、完整訊息,減少外洩個資的面積。
    """
    if days is None:
        query = f"*[System[({cond})]]"
    else:
        ms = days * 86400 * 1000
        query = f"*[System[({cond}) and TimeCreated[timediff(@SystemTime) <= {ms}]]]"
    raw = _run(
        ["wevtutil", "qe", channel, f"/q:{query}", f"/c:{max_events}",
         f"/rd:{'true' if newest_first else 'false'}", "/f:xml"],
        timeout=45,
    )
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(f"<Events>{text}</Events>")
    except ET.ParseError:
        # wevtutil 查不到東西或權限不足時會輸出純文字錯誤訊息,不是 XML
        return None

    events = []
    for ev in root.findall(f"{_EVT_NS}Event"):
        sysel = ev.find(f"{_EVT_NS}System")
        if sysel is None:
            continue
        prov = sysel.find(f"{_EVT_NS}Provider")
        eid = sysel.find(f"{_EVT_NS}EventID")
        lvl = sysel.find(f"{_EVT_NS}Level")
        tc = sysel.find(f"{_EVT_NS}TimeCreated")
        when = _parse_event_time(tc.get("SystemTime", "")) if tc is not None else None

        named: dict[str, str] = {}
        plain: list[str] = []
        ed = ev.find(f"{_EVT_NS}EventData")
        if ed is not None:
            for d in ed.findall(f"{_EVT_NS}Data"):
                val = (d.text or "").strip()[:300]
                if d.get("Name"):
                    named[d.get("Name")] = val
                else:
                    plain.append(val)
        try:
            event_id = int(eid.text) if eid is not None else 0
        except (TypeError, ValueError):
            event_id = 0
        events.append({
            "provider": prov.get("Name", "") if prov is not None else "",
            "id": event_id,
            "level": int(lvl.text) if lvl is not None and (lvl.text or "").isdigit() else 0,
            "time_utc": when.isoformat() if when else "",
            "time_local": _local_str(when),
            "data": named or plain,
        })
    return events


# ── 各項蒐集 ────────────────────────────────────────────────────────────────

_BUGCHECK_RE = re.compile(r"0x([0-9a-fA-F]+)\s*\(([^)]*)\)")


def collect_bugchecks() -> list[dict] | None:
    """藍屏後重開機時 Windows 寫的事件 1001(來源 WER-SystemErrorReporting)。"""
    events = query_events(
        "Provider[@Name='Microsoft-Windows-WER-SystemErrorReporting'] and EventID=1001"
    )
    if events is None:
        return None
    out = []
    for ev in events:
        data = ev["data"]
        first = (list(data.values())[0] if isinstance(data, dict) and data
                 else (data[0] if isinstance(data, list) and data else ""))
        m = _BUGCHECK_RE.search(first)
        if not m:
            continue
        code = int(m.group(1), 16)
        params = [p.strip() for p in m.group(2).split(",")]
        info = bugcheck_db.describe(code)
        out.append({
            "time_utc": ev["time_utc"],
            "time_local": ev["time_local"],
            "code": code,
            "code_hex": f"0x{code:X}",
            "name": info["name"],
            "params": params,
            "source": "event",
        })
    return out


def collect_power_events() -> dict:
    """Kernel-Power 41(非正常關機)與 6008(上次關機不正常)。

    41 的 BugcheckCode=0 代表「沒有藍屏的意外關機」:當機凍結後長按電源鍵、突然斷電,
    都會留下這種紀錄——和藍屏是不同類的問題,所以分開統計。
    """
    result = {"unexpected": None, "freeze_or_power_loss": 0, "with_bugcheck": 0}
    events = query_events("Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41")
    if events is None:
        return result
    items = []
    for ev in events:
        d = ev["data"] if isinstance(ev["data"], dict) else {}
        try:
            code = int(d.get("BugcheckCode", "0"), 0)
        except ValueError:
            code = 0
        items.append({"time_local": ev["time_local"], "time_utc": ev["time_utc"], "bugcheck_code": code})
        if code == 0:
            result["freeze_or_power_loss"] += 1
        else:
            result["with_bugcheck"] += 1
    result["unexpected"] = items
    return result


def collect_storage_events() -> list[dict] | None:
    """磁碟/儲存控制器層級的錯誤事件。出現這類事件是「儲存裝置有問題」最直接的證據。"""
    cond = (
        "(Provider[@Name='disk'] and (EventID=7 or EventID=11 or EventID=15 or EventID=51 "
        "or EventID=52 or EventID=153)) "
        "or (Provider[@Name='stornvme' or @Name='storahci' or @Name='iaStorAVC' or "
        "@Name='iaStorAC' or @Name='iaStorV'] and EventID=129) "
        "or (Provider[@Name='Ntfs'] and (EventID=55 or EventID=140))"
    )
    events = query_events(cond, max_events=300)
    if events is None:
        return None
    for ev in events:
        ev["note"] = STORAGE_EVENT_NOTES.get((ev["provider"], ev["id"]), "")
        # disk 事件的第一個欄位是 \Device\Harddisk1\DR1,取出磁碟編號才知道是哪一顆
        first = ev["data"][0] if isinstance(ev["data"], list) and ev["data"] else ""
        m = re.search(r"Harddisk(\d+)", first)
        ev["disk_number"] = int(m.group(1)) if m else None
    return events


def collect_whea() -> list[dict] | None:
    """WHEA-Logger:硬體自己回報的錯誤(CPU/記憶體/PCIe)。有紀錄就代表硬體層級出過事。"""
    return query_events("Provider[@Name='Microsoft-Windows-WHEA-Logger']", max_events=100)


def collect_gpu_tdr() -> list[dict] | None:
    """Display 4101:顯示驅動沒回應後被系統重設(畫面閃一下又恢復)。"""
    return query_events("Provider[@Name='Display'] and EventID=4101", max_events=100)


def collect_memory_diag() -> list[dict] | None:
    """Windows 記憶體診斷的結果:1101=沒發現錯誤、1102=發現硬體錯誤。"""
    return query_events(
        "Provider[@Name='Microsoft-Windows-MemoryDiagnostics-Results'] "
        "and (EventID=1101 or EventID=1102)",
        max_events=10,
        days=365,
    )


def _read_dump_header(path: str) -> dict | None:
    """直接讀 64 位元核心傾印檔(PAGEDU64)的標頭取得停止代碼與參數,不需要 WinDbg。

    標頭佈局(DUMP_HEADER64):0x00 'PAGE' + 0x04 'DU64';0x38 BugCheckCode(4 bytes);
    0x40 起四個 8-byte 的 BugCheckParameter。這是 Windows 長期穩定的格式。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(0x60)
    except OSError:
        return None
    if len(head) < 0x60 or head[:8] != b"PAGEDU64":
        return None
    code = struct.unpack_from("<I", head, 0x38)[0]
    params = struct.unpack_from("<4Q", head, 0x40)
    return {"code": code, "params": [f"0x{p:X}" for p in params]}


def _minidump_dir() -> str:
    default = os.path.expandvars(r"%SystemRoot%\Minidump")
    if winreg is None:
        return default
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CrashControl") as k:
            val, _ = winreg.QueryValueEx(k, "MinidumpDir")
            return os.path.expandvars(val)
    except OSError:
        return default


def collect_dumps() -> dict:
    """列出傾印檔(不複製、不讀內容,只讀標頭)。沒權限時標記 access_denied 而不是假裝沒有。"""
    out = {"minidump_dir": _minidump_dir(), "files": [], "access_denied": False,
           "full_dump": None}
    try:
        names = os.listdir(out["minidump_dir"])
    except PermissionError:
        out["access_denied"] = True
        names = []
    except OSError:
        names = []
    for name in names:
        if not name.lower().endswith(".dmp"):
            continue
        path = os.path.join(out["minidump_dir"], name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        entry = {"name": name, "path": path, "size": st.st_size,
                 "time_utc": mtime.isoformat(), "time_local": _local_str(mtime)}
        header = _read_dump_header(path)
        if header:
            entry.update({
                "code": header["code"],
                "code_hex": f"0x{header['code']:X}",
                "name_code": bugcheck_db.describe(header["code"])["name"],
                "params": header["params"],
            })
        out["files"].append(entry)
    out["files"].sort(key=lambda e: e["time_utc"], reverse=True)

    full = os.path.expandvars(r"%SystemRoot%\MEMORY.DMP")
    try:
        st = os.stat(full)
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        out["full_dump"] = {"path": full, "size": st.st_size, "time_local": _local_str(mtime)}
    except OSError:
        pass
    return out


def _reg_value(hive, path: str, name: str):
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(hive, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def collect_crash_settings() -> dict:
    """藍屏後有沒有產生傾印檔、有沒有自動重開機——設定被關掉的話,就算藍屏也不會留證據。"""
    if winreg is None:
        return {}
    cc = r"SYSTEM\CurrentControlSet\Control\CrashControl"
    hk = winreg.HKEY_LOCAL_MACHINE
    return {
        "dump_type": _reg_value(hk, cc, "CrashDumpEnabled"),   # 0=無 1=完整 2=核心 3=小 7=自動
        "auto_reboot": _reg_value(hk, cc, "AutoReboot"),
        "fast_startup": _reg_value(hk, r"SYSTEM\CurrentControlSet\Control\Session Manager\Power",
                                   "HiberbootEnabled"),
        # 快速啟動是用休眠機制實作的:休眠被關掉(powercfg /h off 或公司政策)時,
        # HiberbootEnabled 即使是 1 也不會生效,所以兩個值要一起看。
        "hibernate_enabled": _reg_value(hk, r"SYSTEM\CurrentControlSet\Control\Power", "HibernateEnabled"),
    }


# 快的規格查詢(約 2~3 秒)與慢的驅動查詢(Win32_PnPSignedDriver 在這類機器上要 10 秒以上)分成
# 兩次 PowerShell 呼叫並行執行:規格先出來,驅動晚點補上,總時間 = 最慢那一個,不是相加。
_PS_PREAMBLE = r"""
$ErrorActionPreference = 'SilentlyContinue'
# 不要設 [Console]::OutputEncoding:stdout 是管線時它要 ~18 秒(實測)。改成輸出純 ASCII 的 JSON,
# 非 ASCII 字元一律轉成 \uXXXX,Python 的 json.loads 會還原,完全不依賴主控台編碼。
function J($x) {
  $j = $x | ConvertTo-Json -Depth 5 -Compress
  [regex]::Replace($j, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
}
function D($x) { if ($x) { $x.ToString('yyyy-MM-dd') } else { $null } }
$o = [ordered]@{}
"""

_PS_SPECS = _PS_PREAMBLE + r"""
$cs = Get-CimInstance Win32_ComputerSystem
$o.manufacturer = $cs.Manufacturer
$o.model = $cs.Model
$o.ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
$bb = Get-CimInstance Win32_BaseBoard
$o.board = (($bb.Manufacturer + ' ' + $bb.Product)).Trim()
$b = Get-CimInstance Win32_BIOS
$o.bios_version = $b.SMBIOSBIOSVersion
$o.bios_date = D $b.ReleaseDate
$os = Get-CimInstance Win32_OperatingSystem
$o.os_caption = $os.Caption
$o.os_version = $os.Version
$o.os_arch = $os.OSArchitecture
$o.install_date = D $os.InstallDate
$o.last_boot = if ($os.LastBootUpTime) { $os.LastBootUpTime.ToString('yyyy-MM-dd HH:mm') } else { $null }
$p = Get-CimInstance Win32_Processor | Select-Object -First 1
$o.cpu = ($p.Name + '').Trim()
$o.cpu_cores = $p.NumberOfCores
$o.cpu_threads = $p.NumberOfLogicalProcessors
$o.memory_slots = (Get-CimInstance Win32_PhysicalMemoryArray | Select-Object -First 1).MemoryDevices
$o.memory_modules = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object {
  @{ gb = [math]::Round($_.Capacity / 1GB, 1); speed = $_.Speed; type = $_.SMBIOSMemoryType; vendor = ($_.Manufacturer + '').Trim(); part = ($_.PartNumber + '').Trim() } })
$o.gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object {
  @{ name = $_.Name; driver_version = $_.DriverVersion; driver_date = (D $_.DriverDate);
     width = $_.CurrentHorizontalResolution; height = $_.CurrentVerticalResolution; hz = $_.CurrentRefreshRate } })
$o.physical_disks = @(Get-PhysicalDisk | ForEach-Object {
  @{ number = $_.DeviceId; name = $_.FriendlyName; media = [string]$_.MediaType; bus = [string]$_.BusType;
     health = [string]$_.HealthStatus; status = [string]$_.OperationalStatus; gb = [math]::Round($_.Size / 1GB, 0) } })
$o.disks = @(Get-Disk | ForEach-Object {
  @{ number = $_.Number; bytes = $_.Size; style = [string]$_.PartitionStyle; allocated = $_.AllocatedSize;
     largest_free = $_.LargestFreeExtent; bus = [string]$_.BusType; name = $_.FriendlyName } })
$o.partitions = @(Get-Partition | ForEach-Object {
  $v = $_ | Get-Volume -ErrorAction SilentlyContinue
  @{ disk = $_.DiskNumber; number = $_.PartitionNumber; letter = [string]$_.DriveLetter; type = [string]$_.Type;
     bytes = $_.Size; hidden = [bool]$_.IsHidden; is_system = [bool]$_.IsSystem; fs = [string]$v.FileSystem } })
$design = (Get-CimInstance -Namespace root\wmi -ClassName BatteryStaticData | Select-Object -First 1).DesignedCapacity
$full = (Get-CimInstance -Namespace root\wmi -ClassName BatteryFullChargedCapacity | Select-Object -First 1).FullChargedCapacity
if ($design -and $full) { $o.battery = @{ design_mwh = $design; full_mwh = $full } }
J $o
"""

_PS_DRIVERS = _PS_PREAMBLE + r"""
$classes = 'DISPLAY','NET','MEDIA','SCSIADAPTER','HDC','BLUETOOTH'
$o.drivers = @(Get-CimInstance Win32_PnPSignedDriver | Where-Object { $classes -contains $_.DeviceClass -and $_.DeviceName } | ForEach-Object {
  @{ cls = $_.DeviceClass; name = $_.DeviceName; version = $_.DriverVersion; date = (D $_.DriverDate); vendor = $_.Manufacturer } })
J $o
"""


def _run_ps_json(script: str, timeout: int) -> dict | None:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    raw = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        timeout=timeout,
    )
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace").lstrip("\ufeff"))
    except ValueError:
        return None


def _as_list(v) -> list:
    # PowerShell 的 ConvertTo-Json 對單元素陣列行為不穩,統一正規化成 list
    if v is None:
        return []
    return [v] if isinstance(v, dict) else v


def collect_system_info() -> dict | None:
    """機型/BIOS/OS/CPU/記憶體條/顯示卡/實體磁碟/電池(快速)。失敗回傳 None。"""
    info = _run_ps_json(_PS_SPECS, timeout=60)
    if info is None:
        return None
    for key in ("memory_modules", "gpus", "physical_disks", "disks", "partitions"):
        info[key] = _as_list(info.get(key))
    for part in info["partitions"]:
        # PowerShell 對「沒有磁碟代號」回傳 NUL 字元(\u0000),不是空字串——不清掉會被當成有代號
        part["letter"] = (part.get("letter") or "").strip("\x00 ")
    return info


def collect_drivers() -> list[dict] | None:
    """驅動清單(慢)。失敗回傳 None。"""
    info = _run_ps_json(_PS_DRIVERS, timeout=120)
    if info is None:
        return None
    return _as_list(info.get("drivers"))


def collect_volumes() -> list[dict]:
    out = []
    for root in list_drives():
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        out.append({"drive": root, "total": u.total, "free": u.free})
    return out


def uptime_hours() -> float | None:
    try:
        k32 = ctypes.windll.kernel32
        k32.GetTickCount64.restype = ctypes.c_uint64
        return round(k32.GetTickCount64() / 3_600_000, 1)
    except Exception:
        return None


def collect_smart() -> dict:
    """沿用 smart_health:有 smartctl 就查,沒有就標記不可用(不是錯誤)。"""
    result = {"available": smart_health.is_available(), "devices": [], "reports": {}}
    if not result["available"]:
        return result
    for dev in smart_health.scan_devices():
        name = dev.get("name")
        if not name:
            continue
        health = smart_health.query_health(name, dev.get("type")) or {
            "device": name, "model": name, "passed": None}
        result["devices"].append(health)
        text = smart_health.query_health_text(name, dev.get("type"))
        if text:
            result["reports"][name] = text
    return result


# ── 還原類軟體與事件記錄跨度 ────────────────────────────────────────────────

# 還原卡/還原軟體(網咖、學校、公司電腦常見)會在每次重開機把系統碟還原成固定狀態。
# 後果有兩個,都直接影響診斷可信度:
#   1. 事件記錄、傾印檔、使用者做的設定(含「修好」的設定)重開機後全部消失 → 診斷看不到過去的事。
#   2. 它們在儲存堆疊裡掛過濾驅動,本身就是 stornvme 逾時/DPC watchdog 這類問題的常見嫌疑。
# 關鍵字刻意不放「還原」單字:Windows 自己的「系統還原」服務會誤中。
_RESTORE_KEYWORDS = (
    "deep freeze", "deepfreeze", "faronics", "dfserv", "farflt",
    "shadow defender", "shadowdefender", "reboot restore", "rollback rx", "rollbackrx",
    "horizon datasys", "returnil", "time freeze", "timefreeze", "steadystate", "hdguard",
    "drive vaccine", "driveshield", "reborn card",
    "還原卡", "還原精靈", "還原軟體", "硬碟還原", "冰點", "小海豚", "凍結",
)


def _matches_restore(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    return any(k in blob for k in _RESTORE_KEYWORDS)


def collect_restore_software() -> list[str]:
    """掃描服務/驅動與已安裝程式,找出還原類軟體。只讀登錄檔,毫秒級。

    只能偵測「軟體型」。純硬體還原卡(桌機 PCI 卡,在 BIOS/磁碟控制層運作)Windows 內看不到,
    那種情況靠 collect_log_span() 的「事件記錄被重置」間接發現。
    """
    if winreg is None:
        return []
    found: list[str] = []

    def add(name: str):
        name = (name or "").strip()
        if name and name not in found:
            found.append(name)

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services") as root:
            i = 0
            while True:
                try:
                    svc = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                # 先用服務名稱快篩,命中才去讀細節,避免對幾百個服務都開一次機碼
                display = image = ""
                if not _matches_restore(svc):
                    continue
                try:
                    with winreg.OpenKey(root, svc) as k:
                        display = str(winreg.QueryValueEx(k, "DisplayName")[0])
                except OSError:
                    pass
                add(display if display and not display.startswith("@") else svc)
    except OSError:
        pass

    for hive, path in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ):
        try:
            with winreg.OpenKey(hive, path) as root:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(root, sub) as k:
                            name = str(winreg.QueryValueEx(k, "DisplayName")[0])
                            try:
                                pub = str(winreg.QueryValueEx(k, "Publisher")[0])
                            except OSError:
                                pub = ""
                    except OSError:
                        continue
                    if _matches_restore(name, pub, sub):
                        add(name)
        except OSError:
            pass
    return found[:6]


def collect_log_span() -> dict:
    """System 事件記錄最舊的一筆是什麼時候,用來判斷記錄是不是被重置過。

    正常電腦的 System 記錄通常能回溯數週到數月。最舊事件只有幾小時/幾天前、但系統其實裝了
    很久,代表記錄被清掉了(還原機制、有人手動清除、記錄設定過小)——這時「近 90 天沒有藍屏」
    完全不能當證據。
    """
    out = {"oldest_utc": None, "oldest_local": None, "age_days": None}
    events = query_events("EventID>=0", max_events=1, days=None, newest_first=False)
    if not events:
        return out
    when = _parse_event_time(events[0]["time_utc"]) if events[0].get("time_utc") else None
    if when:
        out["oldest_utc"] = when.isoformat()
        out["oldest_local"] = _local_str(when)
        out["age_days"] = round((datetime.now(timezone.utc) - when).total_seconds() / 86400, 1)
    return out


# ── 總蒐集 ──────────────────────────────────────────────────────────────────

STEPS = [
    ("system", "讀取系統資訊與驅動"),
    ("bugchecks", "查詢藍屏紀錄"),
    ("power", "查詢意外關機紀錄"),
    ("storage", "查詢儲存裝置錯誤"),
    ("whea", "查詢硬體錯誤"),
    ("gpu", "查詢顯示驅動重設紀錄"),
    ("memdiag", "查詢記憶體診斷結果"),
    ("dumps", "檢查傾印檔"),
    ("smart", "讀取磁碟 SMART"),
    ("drivers", "讀取驅動版本(這步較慢,請稍候)"),
]


def collect_all(progress_cb=None, cancel_check=None) -> dict:
    """依序蒐集所有項目。progress_cb(step_label, index, total);cancel_check() 回 True 就中止。"""
    data: dict = {
        "collected_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
        "lookback_days": LOOKBACK_DAYS,
        "is_admin": is_admin(),
        "errors": [],
    }
    total = len(STEPS)

    def step(i: int, key: str, label: str, fn):
        if progress_cb:
            progress_cb(label, i, total)
        try:
            value = fn()
        except Exception as e:  # 蒐集項目絕不能讓整體失敗
            logger.exception("collect %s failed", key)
            value = None
            data["errors"].append(f"{label}失敗:{type(e).__name__}")
        if value is None and key in ("system", "bugchecks", "power", "storage", "whea", "gpu", "memdiag"):
            data["errors"].append(f"{label}:讀不到(可能需要系統管理員權限)")
        return value

    # 驅動查詢最慢(10 秒以上),一開始就丟到背景執行緒並行跑,等到最後一步才取結果
    pool = ThreadPoolExecutor(max_workers=1)
    drivers_future = pool.submit(collect_drivers)
    funcs = {
        "drivers": drivers_future.result,
        "system": collect_system_info,
        "bugchecks": collect_bugchecks,
        "power": collect_power_events,
        "storage": collect_storage_events,
        "whea": collect_whea,
        "gpu": collect_gpu_tdr,
        "memdiag": collect_memory_diag,
        "dumps": collect_dumps,
        "smart": collect_smart,
    }
    results = {}
    try:
        for i, (key, label) in enumerate(STEPS):
            if cancel_check and cancel_check():
                break
            results[key] = step(i, key, label, funcs[key])
    finally:
        pool.shutdown(wait=False)
    if progress_cb:
        progress_cb("整理結果", total, total)

    data["system"] = results.get("system") or {}
    data["system"]["drivers"] = results.get("drivers") or []
    data["bugchecks"] = results.get("bugchecks") or []
    data["power"] = results.get("power") or {}
    data["storage_events"] = results.get("storage") or []
    data["whea"] = results.get("whea") or []
    data["gpu_tdr"] = results.get("gpu") or []
    data["memdiag"] = results.get("memdiag") or []
    data["dumps"] = results.get("dumps") or {}
    data["smart"] = results.get("smart") or {"available": False, "devices": [], "reports": {}}
    data["crash_settings"] = collect_crash_settings()
    data["restore_software"] = collect_restore_software()
    data["log_span"] = collect_log_span()
    data["volumes"] = collect_volumes()
    data["uptime_hours"] = uptime_hours()
    data["bugchecks"] = _merge_dump_only(data["bugchecks"], data["dumps"])
    return data


def _merge_dump_only(bugchecks: list[dict], dumps: dict) -> list[dict]:
    """事件記錄被清掉但 dump 還在時,用 dump 標頭補上藍屏紀錄(10 分鐘內視為同一次)。"""
    merged = list(bugchecks)
    known = []
    for b in bugchecks:
        try:
            known.append(datetime.fromisoformat(b["time_utc"]))
        except (ValueError, KeyError):
            pass
    cutoff_days = LOOKBACK_DAYS
    now = datetime.now(timezone.utc)
    for d in dumps.get("files", []):
        if "code" not in d:
            continue
        try:
            t = datetime.fromisoformat(d["time_utc"])
        except ValueError:
            continue
        if (now - t).days > cutoff_days:
            continue
        if any(abs((t - k).total_seconds()) < 600 for k in known):
            continue
        merged.append({
            "time_utc": d["time_utc"], "time_local": d["time_local"],
            "code": d["code"], "code_hex": d["code_hex"], "name": d["name_code"],
            "params": d["params"], "source": "dump",
        })
    merged.sort(key=lambda b: b["time_utc"], reverse=True)
    return merged


# ── 推論 ────────────────────────────────────────────────────────────────────

def _finding(sev: str, title: str, detail: str = "", advice: list[str] | None = None) -> dict:
    return {"severity": sev, "title": title, "detail": detail, "advice": advice or []}


def _driver_age_years(date_str: str | None) -> float | None:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return (datetime.now() - d).days / 365.25


def analyze(data: dict) -> dict:
    """把蒐集到的資料推論成:整體燈號、最可能的原因方向排名、逐項發現。

    評分邏輯刻意簡單透明(每條規則加幾分、列出理由),不裝作精確的機率——目的是幫
    使用者(與開發者)縮小範圍、決定先查哪裡,而不是下定論。
    """
    findings: list[dict] = []
    scores: dict[str, float] = {k: 0.0 for k in CATEGORY_LABELS}
    reasons: dict[str, list[str]] = {k: [] for k in CATEGORY_LABELS}

    def add(cat: str, pts: float, reason: str):
        scores[cat] += pts
        if reason not in reasons[cat]:
            reasons[cat].append(reason)

    system = data.get("system", {})
    disks_by_number = {d.get("number"): d for d in system.get("physical_disks", [])}

    # 0. 權限
    if not data.get("is_admin"):
        findings.append(_finding(
            "info", "目前不是系統管理員身分,部分資料可能讀不到",
            "傾印檔資料夾與部分磁碟 SMART 需要管理員權限。若結果顯示「沒有問題」,"
            "建議以系統管理員身分重新啟動後再診斷一次。"))

    # 0.5 還原機制:它會讓「過去的紀錄」整體失效,所以要在判讀其他項目之前先確定
    restore = data.get("restore_software") or []
    span = data.get("log_span") or {}
    log_age = span.get("age_days")
    try:
        install_age = (datetime.now() - datetime.strptime(system.get("install_date") or "", "%Y-%m-%d")).days
    except ValueError:
        install_age = None
    # 記錄比系統安裝時間短很多才算「被重置」(剛重灌的電腦記錄本來就短,不能誤判)
    log_short = (log_age is not None and log_age < 14
                 and (install_age is None or install_age > log_age + 7))
    records_unreliable = bool(restore) or log_short
    if restore:
        findings.append(_finding(
            "warn", "偵測到還原/凍結類軟體:" + "、".join(restore),
            "這類軟體在每次重開機時把系統碟還原成固定狀態。後果:\n"
            "① 事件記錄、藍屏傾印檔、以及你在電腦上做的任何設定或驅動更新,重開機後都會消失,"
            "所以這份診斷只能看到「本次開機以來」的事,看不到過去的藍屏。\n"
            "② 它們會在儲存堆疊掛過濾驅動,本身就是 NVMe 逾時、DPC_WATCHDOG 這類問題的常見嫌疑。",
            ["要修改任何東西(更新驅動、改電源設定、開傾印)之前,先把還原軟體切到「解凍/維護模式」,"
             "否則重開機就全部還原回去,等於白改",
             "確認問題的方法:在管理員同意下,暫時停用或移除還原軟體使用幾天,看藍屏是否消失",
             "要抓藍屏證據,需在解凍狀態下開啟傾印功能,並讓藍屏發生在解凍的那次開機"]))
        add("storage", 4.0, "還原軟體會在儲存堆疊掛過濾驅動,是儲存逾時的常見嫌疑")
        add("driver", 2.0, "還原軟體的核心驅動可能與其他驅動衝突")
    # 快速啟動讓「關機」變成休眠:下次開機是恢復上次的核心狀態,不是完整開機。
    # 還原軟體只在完整開機時才還原,所以使用者以為「每天關機=每天還原」,實際上可能根本沒還原過。
    # 有些還原軟體(如 Deep Freeze)的官方文件也要求關閉快速啟動。這條是從朋友的真實案例推出來的。
    cs = data.get("crash_settings", {})
    # 休眠被關掉時快速啟動無效(按關機=完整關機),此時不能再說「關機不會還原」。
    # 這條是被朋友的實例糾正的:她的公司筆電休眠被鎖掉,原本的警告在她身上是錯的。
    fast_startup = 1 if (cs.get("fast_startup") == 1 and cs.get("hibernate_enabled") != 0) else 0
    if restore and fast_startup == 1:
        findings.append(_finding(
            "warn", "「快速啟動」是開啟的,按「關機」不會完整重開機",
            "快速啟動會讓關機變成休眠,下次開機直接恢復上次狀態。還原軟體只在完整開機時才還原,"
            "所以即使每天關機,保護空間與系統狀態也可能好幾天都沒被清掉。",
            ["改用「重新啟動」而不是「關機」(重新啟動一定是完整開機)",
             "或在解凍狀態下關閉快速啟動:控制台 → 電源選項 → 選擇電源按鈕的功能 → 取消勾選「啟用快速啟動」"]))
        add("storage", 2.0, "快速啟動讓還原軟體很少真正還原,保護空間可能一直累積")
    if log_short:
        findings.append(_finding(
            "warn", f"事件記錄只回溯到 {span.get('oldest_local')}(約 {log_age:g} 天)",
            "但系統已安裝較久,代表事件記錄被重置過(還原機制、手動清除或記錄空間設太小)。"
            "此時「沒有藍屏紀錄」完全不能當作證據,診斷結果只反映最近這段時間。"))

    # 1. 藍屏
    bugchecks = data.get("bugchecks", [])
    if bugchecks:
        by_code: dict[int, list[dict]] = {}
        for b in bugchecks:
            by_code.setdefault(b["code"], []).append(b)
        lines = []
        for code, items in sorted(by_code.items(), key=lambda kv: -len(kv[1])):
            info = bugcheck_db.describe(code)
            lines.append(f"{items[0]['code_hex']} {info['name']} × {len(items)}(最近 {items[0]['time_local']})")
            add(info["cat"], min(len(items), 5) * 2.0, f"藍屏停止代碼 {info['name']}")
        advice: list[str] = []
        top_code = max(by_code, key=lambda c: len(by_code[c]))
        top_info = bugcheck_db.describe(top_code)
        advice.append(f"最常出現的 {top_info['name']}:{top_info['plain']}")
        advice.extend(top_info["steps"])
        # 0x133 的第一個參數能分辨哪一種超時
        if top_code == 0x133:
            try:
                p1 = int(by_code[top_code][0]["params"][0], 16)
                extra = bugcheck_db.dpc_watchdog_detail(p1)
                if extra:
                    advice.insert(1, f"此次超時類型:{extra}")
            except (ValueError, IndexError):
                pass
        sev = "bad" if len(bugchecks) >= 3 else "warn"
        findings.append(_finding(sev, f"近 {LOOKBACK_DAYS} 天發生 {len(bugchecks)} 次藍屏",
                                 "\n".join(lines), advice))
        distinct = len(by_code)
        if distinct >= 3 and len(bugchecks) >= 4:
            add("memory", 3.0, f"停止代碼每次都不一樣({distinct} 種),這是記憶體或供電不穩的典型特徵")
            add("hardware", 1.5, "停止代碼多變,硬體層級不穩定的可能")
    else:
        dumps_denied = data.get("dumps", {}).get("access_denied")
        if data.get("crash_settings", {}).get("dump_type") == 0 or records_unreliable:
            # 傾印被關掉、或紀錄會被還原/清除時,「沒有紀錄」≠「沒發生過」。
            # 朋友的真實報告就是這樣:他明明遇過藍屏,工具卻打了綠色勾勾,等於說謊。
            why = ("這台電腦有還原機制/事件記錄被重置過" if records_unreliable
                   else "系統設定為藍屏時不留傾印檔")
            findings.append(_finding(
                "info", f"近 {LOOKBACK_DAYS} 天查不到藍屏紀錄,但不能當作沒發生過",
                f"{why},所以就算藍屏過也不會有紀錄。"
                "請以使用者實際遇到的狀況為準;要查原因,需先讓傾印功能開啟且紀錄能保留下來。"))
        else:
            note = "(但目前沒有管理員權限,傾印檔資料夾讀不到,可能漏看)" if dumps_denied else ""
            findings.append(_finding("ok", f"近 {LOOKBACK_DAYS} 天沒有藍屏紀錄", note))

    # 2. 非藍屏的意外關機
    power = data.get("power", {})
    freezes = power.get("freeze_or_power_loss", 0)
    if freezes:
        sev = "warn" if freezes >= 2 else "info"
        findings.append(_finding(
            sev, f"有 {freezes} 次「沒有藍屏的意外關機」",
            "系統沒留下藍屏紀錄就直接斷電/重開,常見是當機凍結後長按電源鍵、供電不穩、"
            "電池/變壓器問題或過熱保護。偶爾一兩次可能是人為,頻繁出現就值得注意。",
            ["檢查變壓器與電池是否正常", "清潔風扇出風口、確認沒有過熱", "觀察當機時正在做什麼(玩遊戲/充電中/睡眠喚醒)"]))
        add("power", min(freezes, 5) * 1.0, "多次沒有藍屏的意外關機")
        if freezes >= 3:
            add("hardware", 1.5, "頻繁無預警關機,供電或過熱")

    # 3. 儲存事件
    storage = data.get("storage_events", [])
    if storage:
        by_key: dict[tuple, list[dict]] = {}
        for ev in storage:
            by_key.setdefault((ev["provider"], ev["id"]), []).append(ev)
        lines = []
        usb_only = True
        severe = False
        for (prov, eid), items in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
            note = STORAGE_EVENT_NOTES.get((prov, eid), "")
            nums = sorted({e["disk_number"] for e in items if e.get("disk_number") is not None})
            where = ""
            if nums:
                labels = []
                for n in nums:
                    d = disks_by_number.get(str(n)) or disks_by_number.get(n) or {}
                    if d:
                        labels.append(f"磁碟{n}({d.get('name')},{d.get('bus')})")
                        removable = d.get("bus") == "USB"
                    else:
                        # 事件裡的磁碟現在已不存在:內建系統碟不可能消失,所以多半是已拔除的外接碟
                        labels.append(f"磁碟{n}(目前已不存在,多半是已拔除的外接碟)")
                        removable = True
                    if not removable:
                        usb_only = False
                where = " 於 " + "、".join(labels)
            else:
                usb_only = False
            lines.append(f"{prov} 事件 {eid} {note} × {len(items)}(最近 {items[0]['time_local']}){where}")
            if (prov, eid) in STORAGE_SEVERE:
                severe = True
        n = len(storage)
        if usb_only:
            findings.append(_finding(
                "info", f"外接 USB 磁碟有 {n} 筆錯誤事件", "\n".join(lines),
                ["多半是傳輸線、USB 埠或外接碟本身問題;若藍屏與此無關可忽略"]))
        else:
            sev = "bad" if (severe or n >= 5) else "warn"
            advice = ["先備份重要資料", "查看該磁碟的 SMART 健康度", "更新 SSD 韌體與儲存驅動;換排線/插槽測試(桌機)"]
            if restore:
                advice.insert(0, "此機有還原軟體:先排除它(解凍後暫停用/移除測試),它的儲存過濾驅動是首要嫌疑")
            findings.append(_finding(
                sev, f"儲存裝置在近 {LOOKBACK_DAYS} 天內有 {n} 筆錯誤事件"
                     + ("(只含最近一次開機以來)" if records_unreliable else ""),
                "\n".join(lines), advice))
            add("storage", 8.0 if (severe or n >= 5) else 4.0, "事件檢視器有磁碟/儲存控制器層級的錯誤")
    else:
        findings.append(_finding("ok", "沒有發現磁碟/儲存控制器錯誤事件"))

    # 4. SMART
    smart = data.get("smart", {})
    if smart.get("available"):
        for h in smart.get("devices", []):
            problems = []
            if h.get("passed") is False:
                problems.append("SMART 整體健康判定為異常")
            for key, label in (("reallocated_sectors", "已重新對應磁區"),
                               ("pending_sectors", "待處理磁區"),
                               ("uncorrectable", "無法修正錯誤")):
                if h.get(key):
                    problems.append(f"{label} {h[key]}")
            wear = h.get("wear_percent_used")
            if wear is not None and wear >= 80:
                problems.append(f"SSD 耗損已達 {wear}%")
            if problems:
                sev = "bad" if (h.get("passed") is False or h.get("pending_sectors")
                                or h.get("uncorrectable") or (wear or 0) >= 95) else "warn"
                findings.append(_finding(
                    sev, f"{h.get('model') or h['device']}:{'、'.join(problems)}", "",
                    ["立即備份資料並準備更換", "這類指標不會自己變好"]))
                add("storage", 10.0 if sev == "bad" else 5.0, f"{h.get('model') or h['device']} 的 SMART 異常")
            elif h.get("passed"):
                temp = h.get("temperature_c")
                findings.append(_finding("ok", f"{h.get('model') or h['device']}:SMART 健康"
                                         + (f"(溫度 {temp}°C)" if temp is not None else "")))
    elif smart and not smart.get("available"):
        findings.append(_finding("info", "沒有 SMART 讀取元件,改用 Windows 回報的磁碟健康狀態"))
    for d in system.get("physical_disks", []):
        if d.get("health") not in ("Healthy", "", None):
            findings.append(_finding(
                "bad", f"Windows 回報磁碟「{d.get('name')}」健康狀態:{d.get('health')}", "",
                ["立即備份資料", "更換或送修該磁碟"]))
            add("storage", 10.0, f"Windows 回報 {d.get('name')} 狀態異常")

    # 5. WHEA
    # WHEA 的事件等級很關鍵:Level 3(警告)是「硬體出錯但已自動修正」(最常見的是事件 17,
    # PCIe 連結的可修正錯誤,新筆電幾乎都有、通常無害);Level 1~2 才是「沒修正成功」的真錯誤。
    # 實測過一台正常機器有 100 筆 ID 17——若一律判紅燈就是嚴重誤報。
    whea = data.get("whea", [])
    whea_fatal = [e for e in whea if e["level"] and e["level"] <= 2]
    whea_corrected = [e for e in whea if e not in whea_fatal]
    if whea_fatal:
        findings.append(_finding(
            "warn", f"硬體回報了 {len(whea_fatal)} 筆「無法修正」的錯誤(WHEA)",
            f"最近一次:{whea_fatal[0]['time_local']}。代表 CPU/記憶體/PCIe 裝置在硬體層級出過錯,"
            "多數時候不是軟體能修的。",
            ["關閉任何超頻或 XMP/EXPO 記憶體設定測試", "檢查散熱與風扇", "跑記憶體診斷"]))
        add("hardware", min(len(whea_fatal), 5) * 2.0, "有無法修正的 WHEA 硬體錯誤")
        add("memory", 1.0, "WHEA 錯誤可能來自記憶體")
    if whea_corrected:
        findings.append(_finding(
            "info", f"有 {len(whea_corrected)} 筆「已自動修正」的硬體錯誤(WHEA)",
            "多為 PCIe 連結的可修正錯誤,很多新機器都有,單獨出現通常無害;"
            "若同時有藍屏或當機才需要留意。"))
        if len(whea_corrected) >= 50:
            add("hardware", 0.5, "大量已修正的 PCIe/硬體錯誤")

    # 6. 顯示驅動重設
    tdr = data.get("gpu_tdr", [])
    if tdr:
        findings.append(_finding(
            "warn", f"顯示驅動曾 {len(tdr)} 次沒有回應而被系統重設",
            f"最近一次:{tdr[0]['time_local']}。畫面閃爍/黑一下再恢復通常就是這個。",
            ["乾淨重裝顯示卡驅動", "檢查 GPU 溫度與散熱"]))
        add("gpu", min(len(tdr), 5) * 1.0, "顯示驅動重設紀錄")

    # 7. 記憶體診斷結果
    for ev in data.get("memdiag", []):
        if ev["id"] == 1102:
            findings.append(_finding(
                "bad", "Windows 記憶體診斷曾發現記憶體錯誤", f"時間:{ev['time_local']}",
                ["更換記憶體條(單條輪流測試找出壞的那條)"]))
            add("memory", 12.0, "記憶體診斷發現錯誤")
            break
    else:
        passed = [ev for ev in data.get("memdiag", []) if ev["id"] == 1101]
        if passed:
            findings.append(_finding("ok", f"記憶體診斷曾通過({passed[0]['time_local']})",
                                     "快速模式通過不代表記憶體 100% 沒問題,但可降低優先度。"))

    # 8. 傾印檔設定
    settings = data.get("crash_settings", {})
    if settings.get("dump_type") == 0:
        findings.append(_finding(
            "warn", "系統設定為「藍屏時不產生傾印檔」",
            "這樣即使藍屏也不會留下證據,無法事後分析。"
            + ("\n⚠ 此機有還原機制:設定會在重開機後被還原回去,可能正是它把傾印關掉的。" if records_unreliable else ""),
            ["系統內容 → 進階 → 啟動及修復 → 設定 → 偵錯資訊寫入:選「自動記憶體傾印」"
             + (("(要先解凍再改,否則重開機會還原)") if records_unreliable else "")]))

    # 9. 剩餘空間
    sysdrive = os.environ.get("SystemDrive", "C:").upper() + "\\"
    for v in data.get("volumes", []):
        if v["drive"].upper() == sysdrive and v["total"]:
            free_pct = v["free"] / v["total"] * 100
            if free_pct < 10 or v["free"] < 10 * 1024**3:
                gb = v["free"] / 1024**3
                findings.append(_finding(
                    "warn", f"系統碟剩餘空間偏低({gb:.1f} GB,{free_pct:.0f}%)",
                    "空間太滿會拖慢系統,也讓 Windows 更新與虛擬記憶體出問題。",
                    ["用本程式的「清理」與「大檔案」功能釋放空間"]))

    # 9.5 磁碟分割:只看中繼資料,不碰內容。大量「看不到」的容量值得讓人知道,但原因多半平淡
    #     (映像沒擴充),所以只給 info,不當成問題、也不亂猜用途。
    for d in system.get("disks", []):
        total = d.get("bytes") or 0
        free = d.get("largest_free") or 0
        if total and free >= 20 * 1024**3 and free / total >= 0.1:
            findings.append(_finding(
                "info", f"磁碟 {d.get('number')} 有 {_gb(free)} 未配置空間(沒被任何分割使用)",
                "常見原因:灌系統映像時沒有擴充分割;有些還原軟體也會把空間留給自己用。"
                "這些空間在檔案總管看不到,只有「磁碟管理」會顯示成黑色未配置區塊。"))
    for p in system.get("partitions", []):
        big = (p.get("bytes") or 0) >= _BIG_PART_BYTES
        if not big or p.get("letter"):
            continue
        if p.get("type") in ("System", "Reserved", "Recovery"):
            continue
        fs = p.get("fs") or ""
        findings.append(_finding(
            "info", f"磁碟 {p.get('disk')} 分割 {p.get('number')}:{_gb(p['bytes'])},沒有磁碟代號"
                    + ("" if fs else ",且無法辨識檔案系統"),
            "沒有代號的分割在檔案總管看不到。可能是還原軟體的保護區、備份區,或是加密/未格式化的磁碟區。"
            "這個工具只讀大小與類型,不會讀取內容,所以無法判斷裡面是什麼。"))

    # 10. 老舊驅動(排除微軟內建)
    old = []
    for drv in system.get("drivers", []):
        vendor = (drv.get("vendor") or "").lower()
        # 2006-06-21 是 Windows 內建(inbox)驅動共用的日期,不代表真的老舊
        if ("microsoft" in vendor or drv.get("date") == "2006-06-21"
                or (drv.get("name") or "").startswith("Standard")):
            continue
        age = _driver_age_years(drv.get("date"))
        if age is not None and age >= _DRIVER_OLD_YEARS and drv.get("cls") in ("DISPLAY", "NET", "SCSIADAPTER", "HDC"):
            old.append((age, drv))
    if old:
        old.sort(key=lambda t: -t[0])
        lines = [f"{d['name']} {d.get('version')}({d.get('date')})" for _, d in old[:6]]
        findings.append(_finding(
            "info", f"有 {len(old)} 個驅動超過 {_DRIVER_OLD_YEARS} 年沒更新", "\n".join(lines),
            ["到廠商官網(HP/Dell/Lenovo 或晶片廠)更新驅動"]))
        add("driver", min(len(old), 3) * 0.5, "有超過 3 年沒更新的驅動")

    # 11. BIOS 年紀
    try:
        bios_age = (datetime.now() - datetime.strptime(system.get("bios_date") or "", "%Y-%m-%d")).days / 365.25
    except ValueError:
        bios_age = None
    if bios_age is not None and bios_age >= 3:
        findings.append(_finding(
            "info", f"BIOS 版本已 {bios_age:.0f} 年沒更新({system.get('bios_date')})", "",
            ["到機器廠商官網確認有沒有新版 BIOS(更新前先充滿電、接妥電源)"]))
        add("hardware", 0.5, "BIOS 偏舊")

    # 12. 電池
    bat = system.get("battery")
    if bat and bat.get("design_mwh"):
        health = bat["full_mwh"] / bat["design_mwh"] * 100
        if health < 60:
            findings.append(_finding(
                "warn", f"電池健康度偏低({health:.0f}%)", "",
                ["電池老化可能造成突然關機,考慮更換"]))
            add("power", 1.5, "電池嚴重老化")

    # 整體燈號與排名
    sev_rank = {"bad": 3, "warn": 2, "info": 1, "ok": 0}
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    findings.sort(key=lambda f: order[f["severity"]])
    worst = max((sev_rank[f["severity"]] for f in findings), default=0)
    overall = {3: "bad", 2: "warn"}.get(worst, "ok")

    suspects = [
        {"cat": c, "label": CATEGORY_LABELS[c], "score": round(s, 1), "reasons": reasons[c]}
        for c, s in sorted(scores.items(), key=lambda kv: -kv[1]) if s > 0
    ]
    # 結論是「沒有異常」時不列推測方向:只剩零星的低分項(例如已修正的 PCIe 錯誤),
    # 列出來會讓人以為真的有問題,等於在「正常」旁邊放一個警告。
    if overall == "ok":
        suspects = []
    if overall == "bad":
        headline = "發現明確的問題,建議優先處理"
    elif overall == "warn":
        headline = "有需要留意的徵兆"
    else:
        headline = "目前沒有發現異常"
        if data.get("errors"):
            headline += "(部分項目讀不到,結果僅供參考)"
    return {"overall": overall, "headline": headline, "findings": findings, "suspects": suspects}


# ── 電腦規格(給不知道自己電腦規格的人;UI、摘要、報告共用) ─────────────────────

_MEMORY_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 29: "LPDDR3", 30: "LPDDR4",
                 34: "DDR5", 35: "LPDDR5"}


def _gb(n: int) -> str:
    """位元組 → 'xxx GB' / 'x.x TB'(磁碟廠商用 1000 進位,但 Windows 顯示 1024;這裡跟 Windows 一致)。"""
    if n < 1024**3:
        return f"{n / 1024**2:.0f} MB"
    gb = n / 1024**3
    return f"{gb / 1024:.1f} TB" if gb >= 1000 else f"{gb:.0f} GB"


_PART_TYPES = {"System": "EFI 系統", "Reserved": "MSR 保留", "Recovery": "復原", "Basic": "一般資料",
               "Unknown": "未知類型", "IFS": "一般資料", "": "未知類型"}
_BIG_PART_BYTES = 20 * 1024**3


def partition_lines(system: dict) -> list[str]:
    """把每顆磁碟的分割表整理成人話:每個分割的大小/類型/代號/檔案系統,以及未配置空間。"""
    out = []
    parts = system.get("partitions", [])
    for d in system.get("disks", []):
        total = d.get("bytes") or 0
        head = f"磁碟 {d.get('number')}({d.get('style')},{_gb(total)})"
        items = []
        for p in sorted((x for x in parts if x.get("disk") == d.get("number")),
                        key=lambda x: x.get("number") or 0):
            label = _PART_TYPES.get(p.get("type", ""), p.get("type") or "未知")
            letter = f" {p['letter']}:" if p.get("letter") else "(無代號)"
            fs = p.get("fs") or ("" if p.get("type") in ("System", "Reserved") else "無法辨識檔案系統")
            items.append(f"{label}{letter} {_gb(p.get('bytes') or 0)} {fs}".rstrip())
        free = d.get("largest_free") or 0
        if free >= 1024**3:
            items.append(f"未配置空間 {_gb(free)}")
        out.append(head + ":" + "; ".join(items) if items else head)
    return out


def spec_rows(data: dict) -> list[tuple[str, str]]:
    """整理成 [(標籤, 值)],缺資料的項目直接略過,不顯示空白行。"""
    s = data.get("system", {})
    rows: list[tuple[str, str]] = []

    def put(label: str, value: str | None):
        if value and value.strip():
            rows.append((label, value.strip()))

    put("機型", f"{s.get('manufacturer') or ''} {s.get('model') or ''}")
    os_txt = f"{s.get('os_caption') or ''} {s.get('os_arch') or ''}".replace("Microsoft ", "")
    if s.get("os_version"):
        os_txt += f"(版本 {s['os_version']})"
    put("作業系統", os_txt)
    cpu = s.get("cpu")
    if cpu:
        put("處理器 CPU", f"{cpu}({s.get('cpu_cores')} 核 {s.get('cpu_threads')} 執行緒)")

    # 記憶體:總量 + 每條的規格
    mods = s.get("memory_modules", [])
    if s.get("ram_gb"):
        detail = ""
        if mods:
            kinds = {_MEMORY_TYPES.get(m.get("type"), "") for m in mods}
            speeds = {m.get("speed") for m in mods if m.get("speed")}
            kind = "/".join(sorted(k for k in kinds if k))
            speed = f"-{max(speeds)}" if speeds else ""
            sizes = "+".join(f"{m['gb']:g}GB" for m in mods)
            slots = s.get("memory_slots")
            detail = f"({len(mods)} 條:{sizes}" + (f",{kind}{speed}" if kind or speed else "")
            detail += (f",共 {slots} 個插槽" if slots else "") + ")"
        put("記憶體 RAM", f"{s['ram_gb']:g} GB {detail}")

    gpus = [g for g in s.get("gpus", []) if g.get("name")]
    if gpus:
        parts = []
        for g in gpus:
            txt = g["name"]
            if g.get("width") and g.get("height"):
                txt += f"(目前 {g['width']}×{g['height']}" + (f" @{g['hz']}Hz" if g.get("hz") else "") + ")"
            parts.append(txt)
        put("顯示卡 GPU", "\n".join(parts))

    for d in s.get("physical_disks", []):
        media = d.get("media") or ""
        kind = {"SSD": "SSD", "HDD": "HDD"}.get(media, media or "")
        bus = d.get("bus") or ""
        put("儲存裝置", f"{d.get('name')}  {d.get('gb')} GB  {kind} {bus}".replace("  ", " ").strip())
    for v in data.get("volumes", []):
        put(f"磁碟 {v['drive'][:1]} 槽", f"總容量 {_gb(v['total'])},剩餘 {_gb(v['free'])}")

    if data.get("restore_software"):
        put("還原軟體", "、".join(data["restore_software"]))
    for line in partition_lines(s):
        put("磁碟分割", line)
    put("主機板", s.get("board"))
    if s.get("bios_version"):
        put("BIOS", f"{s['bios_version']}({s.get('bios_date') or '?'})")
    bat = s.get("battery")
    if bat and bat.get("design_mwh"):
        put("電池", f"健康度約 {bat['full_mwh'] / bat['design_mwh'] * 100:.0f}%")
    if data.get("uptime_hours") is not None:
        put("已連續開機", f"{data['uptime_hours']:g} 小時")
    cset = data.get("crash_settings") or {}
    if cset.get("hibernate_enabled") == 0:
        put("休眠/快速啟動", "休眠已停用(快速啟動無效,關機=完整關機)")
    elif cset.get("fast_startup") is not None:
        put("休眠/快速啟動", "快速啟動開啟(按關機=休眠,不是完整重開機)"
            if cset["fast_startup"] == 1 else "快速啟動關閉")
    return rows
