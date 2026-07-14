"""磁碟健康(S.M.A.R.T.)診斷:純讀取,不提供任何修復功能——健康狀態異常時只顯示,
使用者應自行備份資料並更換硬碟,這套工具不會、也不該代為處理。

底層透過第三方開源工具 smartmontools(GPLv2,https://www.smartmontools.org)的
smartctl.exe 讀取,原因見 DEVDOC §13:SMART 原始資料的廠牌相容性資料庫太龐大,
不值得也不該自己重寫解析邏輯。本模組只以獨立子行程呼叫未經修改的官方執行檔,
不重新散布、不修改其原始碼。

smartctl.exe 需自行下載後放到 third_party/smartmontools/(不隨 git 版控),
放置方式與檔名見 DEVDOC §13.1。
"""

import json
import os
import subprocess

from .utils.fs import app_root

SMARTCTL_REL_PATH = os.path.join("third_party", "smartmontools", "smartctl.exe")


def smartctl_path() -> str | None:
    path = os.path.join(app_root(), SMARTCTL_REL_PATH)
    return path if os.path.isfile(path) else None


def is_available() -> bool:
    return smartctl_path() is not None


def _run_json(args: list[str], timeout: int = 15) -> dict | None:
    exe = smartctl_path()
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, *args, "-j"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # smartctl 的結束碼是位元旗標組合,非 0 不代表指令失敗(可能只是「有警告」),
    # 一律嘗試解析 stdout 的 JSON,解不出來才視為失敗。
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        return None


def scan_devices() -> list[dict]:
    """回傳偵測到的實體磁碟清單:[{"name": "/dev/sda", "type": "..."}, ...]。
    找不到 smartctl.exe 或掃描失敗一律回傳空清單。
    """
    data = _run_json(["--scan-open"])
    if not data:
        return []
    return data.get("devices", [])


def query_health(device: str, dev_type: str | None = None) -> dict | None:
    """查詢單一磁碟的健康摘要。無法讀取(需要管理員權限、裝置忙碌、不支援等)回傳 None。"""
    args = ["-a"]
    if dev_type:
        args += ["-d", dev_type]
    args.append(device)
    data = _run_json(args)
    if not data:
        return None

    model = data.get("model_name") or device
    passed = data.get("smart_status", {}).get("passed")

    temperature = None
    if data.get("temperature"):
        temperature = data["temperature"].get("current")

    power_on_hours = None
    if data.get("power_on_time"):
        power_on_hours = data["power_on_time"].get("hours")

    reallocated = pending = uncorrectable = wear_percent_used = None

    if "ata_smart_attributes" in data:
        # SATA/HDD:靠 SMART attribute ID 對照,5=已重新對應磁區、197=待處理磁區、
        # 198=無法修正錯誤數,這三個是判斷硬碟是否「快壞了」最直接的指標。
        for attr in data["ata_smart_attributes"].get("table", []):
            aid = attr.get("id")
            raw = attr.get("raw", {}).get("value")
            if aid == 5:
                reallocated = raw
            elif aid == 197:
                pending = raw
            elif aid == 198:
                uncorrectable = raw
    elif "nvme_smart_health_information_log" in data:
        # NVMe 沒有磁區的概念,改看「耗損百分比」與媒體錯誤數。
        nvme = data["nvme_smart_health_information_log"]
        wear_percent_used = nvme.get("percentage_used")
        uncorrectable = nvme.get("media_errors")

    return {
        "device": device,
        "type": dev_type,
        "model": model,
        "passed": passed,
        "temperature_c": temperature,
        "power_on_hours": power_on_hours,
        "reallocated_sectors": reallocated,
        "pending_sectors": pending,
        "uncorrectable": uncorrectable,
        "wear_percent_used": wear_percent_used,
    }


def query_health_text(device: str, dev_type: str | None = None) -> str:
    """回傳 smartctl -a 的純文字報告,供「詳細報告」對話框顯示用。"""
    exe = smartctl_path()
    if not exe:
        return ""
    args = [exe, "-a"]
    if dev_type:
        args += ["-d", dev_type]
    args.append(device)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout
