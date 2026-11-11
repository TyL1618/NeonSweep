"""藍屏停止代碼(Bug Check Code)白話對照表。

用途:診斷頁把 0x133 這種數字翻成「是什麼意思、最常見原因、先查什麼」,讓不懂的人也看得懂,
也讓回傳給開發者的報告自帶一份可讀的解釋。

欄位:
- name:官方名稱(英文,搜尋用)
- cat:最可能的責任方向,對應 crash_analysis.CATEGORY_LABELS 的 key
- plain:一句白話
- steps:建議的檢查順序(由最可能、最便宜的開始)

資料取自 Microsoft Learn「Bug Check Code Reference」與常見處理經驗;不保證涵蓋全部代碼,
查不到的代碼由 describe() 給通用說明。
"""

BUGCHECKS: dict[int, dict] = {
    0x0A: {
        "name": "IRQL_NOT_LESS_OR_EQUAL",
        "cat": "driver",
        "plain": "某個驅動程式在不該存取的時機碰了記憶體,最常見是驅動有 bug 或與其他軟體衝突。",
        "steps": ["更新或回復最近安裝的驅動(網路卡、顯示卡、藍牙優先)", "移除最近新裝的硬體或軟體(尤其防毒、VPN、虛擬機)", "跑一次記憶體診斷排除壞記憶體"],
    },
    0x19: {
        "name": "BAD_POOL_HEADER",
        "cat": "driver",
        "plain": "系統的記憶體配置區被破壞,通常是驅動寫壞了別人的記憶體。",
        "steps": ["更新所有驅動", "移除最近安裝的軟體", "跑記憶體診斷"],
    },
    0x1A: {
        "name": "MEMORY_MANAGEMENT",
        "cat": "memory",
        "plain": "記憶體管理出現嚴重錯誤,常見是記憶體條故障或接觸不良。",
        "steps": ["跑 Windows 記憶體診斷(或 MemTest86 整晚)", "若有多條記憶體,單條輪流測試", "重新插拔記憶體並清潔金手指"],
    },
    0x1E: {
        "name": "KMODE_EXCEPTION_NOT_HANDLED",
        "cat": "driver",
        "plain": "核心模式的程式拋出沒人處理的例外,多半是驅動錯誤,也可能是記憶體問題。",
        "steps": ["看 dump 點名的 .sys 檔,更新該驅動", "更新 BIOS 與晶片組驅動", "跑記憶體診斷"],
    },
    0x24: {
        "name": "NTFS_FILE_SYSTEM",
        "cat": "storage",
        "plain": "NTFS 檔案系統出錯,常見是硬碟/SSD 有壞區或檔案系統損毀。",
        "steps": ["先備份重要資料", "用 chkdsk /f 修復並看有沒有壞軌", "檢查 SSD/HDD 的 SMART 健康度"],
    },
    0x3B: {
        "name": "SYSTEM_SERVICE_EXCEPTION",
        "cat": "driver",
        "plain": "系統服務執行時發生例外,常見於驅動程式(顯示卡、防毒)有問題。",
        "steps": ["更新顯示卡驅動(建議乾淨安裝)", "暫時移除第三方防毒軟體測試", "執行 sfc /scannow"],
    },
    0x3F: {
        "name": "NO_MORE_SYSTEM_PTES",
        "cat": "driver",
        "plain": "系統用光了某種核心資源,通常是驅動程式資源洩漏。",
        "steps": ["更新驅動", "找出最近更新後才出現的驅動"],
    },
    0x4E: {
        "name": "PFN_LIST_CORRUPT",
        "cat": "memory",
        "plain": "記憶體分頁清單損毀,常見是驅動錯誤或記憶體故障。",
        "steps": ["跑記憶體診斷", "更新驅動"],
    },
    0x50: {
        "name": "PAGE_FAULT_IN_NONPAGED_AREA",
        "cat": "memory",
        "plain": "系統嘗試讀取不存在的記憶體位置,常見是記憶體壞掉、驅動錯誤,或磁碟問題。",
        "steps": ["跑記憶體診斷", "更新驅動、移除最近新軟體", "檢查硬碟/SSD 健康度"],
    },
    0x7A: {
        "name": "KERNEL_DATA_INPAGE_ERROR",
        "cat": "storage",
        "plain": "系統要把資料從磁碟讀進記憶體卻失敗,常見是 SSD/硬碟故障、排線或控制器問題。",
        "steps": ["先備份資料", "檢查 SMART 與事件檢視器的 disk 錯誤", "換 SATA 線/換插槽或測試另一顆硬碟"],
    },
    0x7B: {
        "name": "INACCESSIBLE_BOOT_DEVICE",
        "cat": "storage",
        "plain": "開機時找不到系統碟,常見是儲存驅動、BIOS 模式(AHCI/RAID)變更,或硬碟故障。",
        "steps": ["確認 BIOS 的 SATA 模式沒被改動", "檢查硬碟是否被偵測到", "用安裝媒體做啟動修復"],
    },
    0x7E: {
        "name": "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED",
        "cat": "driver",
        "plain": "某個系統執行緒拋出沒人處理的例外,看畫面上列出的 .sys 檔名就知道是哪個驅動。",
        "steps": ["記下藍屏畫面上的檔名並更新/移除該驅動", "更新 BIOS 與晶片組"],
    },
    0x7F: {
        "name": "UNEXPECTED_KERNEL_MODE_TRAP",
        "cat": "hardware",
        "plain": "CPU 遇到系統無法處理的異常,常見是記憶體或硬體不穩(超頻、過熱)。",
        "steps": ["關閉任何超頻/XMP 測試", "檢查散熱", "跑記憶體診斷"],
    },
    0x8E: {
        "name": "KERNEL_MODE_EXCEPTION_NOT_HANDLED",
        "cat": "driver",
        "plain": "核心模式例外未被處理,與 0x1E 類似,多半是驅動或記憶體。",
        "steps": ["更新驅動", "跑記憶體診斷"],
    },
    0x9C: {
        "name": "MACHINE_CHECK_EXCEPTION",
        "cat": "hardware",
        "plain": "CPU 回報嚴重的硬體錯誤,常見是 CPU 過熱、供電不穩或硬體故障。",
        "steps": ["檢查散熱與風扇", "還原 BIOS 預設值(關閉超頻)", "送修檢測 CPU/主機板"],
    },
    0x9F: {
        "name": "DRIVER_POWER_STATE_FAILURE",
        "cat": "power",
        "plain": "裝置在睡眠/喚醒/關機的電源切換時沒有回應,常見是驅動與電源管理不相容。",
        "steps": ["更新網路卡、顯示卡、藍牙與晶片組驅動", "關閉「快速啟動」測試", "到裝置管理員取消勾選「允許電腦關閉此裝置以節省電源」"],
    },
    0xA0: {
        "name": "INTERNAL_POWER_ERROR",
        "cat": "power",
        "plain": "電源管理子系統發生內部錯誤。",
        "steps": ["更新 BIOS 與晶片組驅動", "檢查電源與電池狀態"],
    },
    0xA5: {
        "name": "ACPI_BIOS_ERROR",
        "cat": "hardware",
        "plain": "BIOS 的 ACPI 表與 Windows 不相容,通常需要更新 BIOS。",
        "steps": ["更新 BIOS 至最新版", "還原 BIOS 預設值"],
    },
    0xBE: {
        "name": "ATTEMPTED_WRITE_TO_READONLY_MEMORY",
        "cat": "driver",
        "plain": "驅動試圖寫入唯讀記憶體。",
        "steps": ["更新或移除最近安裝的驅動"],
    },
    0xC2: {
        "name": "BAD_POOL_CALLER",
        "cat": "driver",
        "plain": "驅動用錯誤的方式配置或釋放記憶體。",
        "steps": ["更新驅動", "移除最近安裝的軟體"],
    },
    0xC4: {
        "name": "DRIVER_VERIFIER_DETECTED_VIOLATION",
        "cat": "driver",
        "plain": "驅動程式驗證器抓到驅動違規(若沒有刻意開啟驗證器,代表有人或某軟體啟用了它)。",
        "steps": ["執行 verifier /reset 關閉驅動程式驗證器", "更新被點名的驅動"],
    },
    0xC5: {
        "name": "DRIVER_CORRUPTED_EXPOOL",
        "cat": "driver",
        "plain": "驅動在記憶體配置區出錯,常見是驅動 bug,也可能是記憶體故障。",
        "steps": ["更新驅動", "跑記憶體診斷"],
    },
    0xCE: {
        "name": "DRIVER_UNLOADED_WITHOUT_CANCELLING_PENDING_OPERATIONS",
        "cat": "driver",
        "plain": "驅動卸載時還有未完成的工作,是驅動本身的 bug。",
        "steps": ["更新被點名的驅動"],
    },
    0xD1: {
        "name": "DRIVER_IRQL_NOT_LESS_OR_EQUAL",
        "cat": "driver",
        "plain": "驅動在錯誤的時機存取記憶體,網路卡、藍牙、音效驅動很常見。",
        "steps": ["看 dump 或畫面上的 .sys 檔名,更新該驅動", "更新網路卡/Wi-Fi 驅動", "移除最近安裝的軟體"],
    },
    0xEA: {
        "name": "THREAD_STUCK_IN_DEVICE_DRIVER",
        "cat": "gpu",
        "plain": "某個驅動卡在無窮迴圈,最常見是顯示卡驅動。",
        "steps": ["以乾淨安裝方式重裝顯示卡驅動(DDU)", "檢查顯示卡散熱與溫度"],
    },
    0xEF: {
        "name": "CRITICAL_PROCESS_DIED",
        "cat": "storage",
        "plain": "Windows 的關鍵程序意外結束,常見是系統檔案損毀、磁碟錯誤或驅動問題。",
        "steps": ["執行 sfc /scannow 與 DISM /Online /Cleanup-Image /RestoreHealth", "檢查磁碟健康度與 chkdsk", "更新儲存驅動"],
    },
    0xF4: {
        "name": "CRITICAL_OBJECT_TERMINATION",
        "cat": "storage",
        "plain": "關鍵系統程序被終止,最常見原因是系統碟讀不到(SSD/硬碟/排線/控制器問題)。",
        "steps": ["先備份資料", "檢查 SMART 與 disk 錯誤事件", "更新儲存驅動與 SSD 韌體"],
    },
    0x101: {
        "name": "CLOCK_WATCHDOG_TIMEOUT",
        "cat": "hardware",
        "plain": "某個 CPU 核心沒有回應中斷,常見於超頻不穩、BIOS 太舊或 CPU 問題。",
        "steps": ["關閉超頻/還原 BIOS 預設", "更新 BIOS 與晶片組驅動", "檢查散熱"],
    },
    0x109: {
        "name": "CRITICAL_STRUCTURE_CORRUPTION",
        "cat": "memory",
        "plain": "核心的關鍵資料結構被改動,可能是記憶體故障、驅動錯誤或硬體不穩。",
        "steps": ["跑記憶體診斷", "更新驅動", "確認沒有超頻"],
    },
    0x116: {
        "name": "VIDEO_TDR_FAILURE",
        "cat": "gpu",
        "plain": "顯示卡沒有回應,系統嘗試重設失敗,常見是顯示卡驅動、過熱或顯示卡故障。",
        "steps": ["乾淨重裝顯示卡驅動", "檢查 GPU 溫度與散熱", "拔掉超頻設定"],
    },
    0x117: {
        "name": "VIDEO_TDR_TIMEOUT_DETECTED",
        "cat": "gpu",
        "plain": "顯示卡逾時未回應,同 0x116。",
        "steps": ["乾淨重裝顯示卡驅動", "檢查散熱"],
    },
    0x119: {
        "name": "VIDEO_SCHEDULER_INTERNAL_ERROR",
        "cat": "gpu",
        "plain": "顯示排程器發生嚴重內部錯誤,多半是顯示驅動或顯示卡硬體。",
        "steps": ["乾淨重裝顯示卡驅動", "檢查顯示卡溫度與供電"],
    },
    0x124: {
        "name": "WHEA_UNCORRECTABLE_ERROR",
        "cat": "hardware",
        "plain": "硬體回報了無法修正的錯誤(CPU、記憶體、PCIe 裝置、供電),通常不是軟體問題。",
        "steps": ["看事件檢視器 WHEA-Logger 指出哪個元件", "關閉超頻、還原 BIOS 預設", "檢查散熱,跑記憶體診斷"],
    },
    0x133: {
        "name": "DPC_WATCHDOG_VIOLATION",
        "cat": "storage",
        "plain": "某個驅動的中斷/DPC 處理跑太久沒回來,最常見是儲存(SSD/儲存驅動)卡住,其次是其他驅動。",
        "steps": [
            "更新或改用 Windows 內建的儲存驅動(Intel RST、NVMe 驅動),並更新 SSD 韌體",
            "檢查 SSD 健康度與事件檢視器的 disk/stornvme 錯誤(事件 ID 129、153)",
            "更新 BIOS、晶片組、Wi-Fi、顯示卡驅動",
            "拔掉外接裝置(USB 擴充座、外接硬碟)觀察是否改善",
        ],
    },
    0x139: {
        "name": "KERNEL_SECURITY_CHECK_FAILURE",
        "cat": "driver",
        "plain": "核心偵測到關鍵資料結構損毀,常見是驅動錯誤,也可能是記憶體問題。",
        "steps": ["更新驅動", "執行 sfc /scannow", "跑記憶體診斷"],
    },
    0x13A: {
        "name": "KERNEL_MODE_HEAP_CORRUPTION",
        "cat": "driver",
        "plain": "核心堆積記憶體被破壞,多為驅動 bug。",
        "steps": ["更新驅動", "移除最近安裝的軟體"],
    },
    0x141: {
        "name": "VIDEO_ENGINE_TIMEOUT_DETECTED",
        "cat": "gpu",
        "plain": "顯示卡引擎逾時,同 TDR 類,多為顯示驅動或顯示卡問題。",
        "steps": ["乾淨重裝顯示卡驅動", "檢查散熱"],
    },
    0x154: {
        "name": "UNEXPECTED_STORE_EXCEPTION",
        "cat": "storage",
        "plain": "儲存元件(Store)發生例外,常見是 SSD/硬碟問題或防毒軟體衝突。",
        "steps": ["檢查 SSD 健康度", "暫時移除第三方防毒測試", "更新 SSD 韌體"],
    },
    0x1CA: {
        "name": "SYNTHETIC_WATCHDOG_TIMEOUT",
        "cat": "hardware",
        "plain": "系統長時間沒有回應心跳,常見於硬體或驅動讓整台機器卡死。",
        "steps": ["更新 BIOS 與所有驅動", "檢查散熱與 SSD 健康度"],
    },
}

GENERIC = {
    "name": "(未收錄)",
    "cat": "driver",
    "plain": "這個停止代碼不在內建對照表,請用代碼到 Microsoft Learn 搜尋「Bug Check 0x…」。",
    "steps": ["用 WinDbg 開啟 dump 檔執行 !analyze -v 找出被點名的驅動"],
}


def describe(code: int) -> dict:
    """查停止代碼;查不到回傳通用說明(name 仍會顯示十六進位代碼,方便搜尋)。"""
    info = BUGCHECKS.get(code)
    if info:
        return info
    return {**GENERIC, "name": f"BUGCHECK_0x{code:X}"}


def dpc_watchdog_detail(param1: int) -> str:
    """0x133 的第一個參數指出是哪一種超時。"""
    if param1 == 0:
        return "單一 DPC/中斷處理常式超過允許時間(某個驅動卡住)"
    if param1 == 1:
        return "累積的 DPC 總時間超過上限(系統整體被拖住)"
    return ""
