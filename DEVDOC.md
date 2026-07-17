# NeonSweep — 硬碟垃圾清理工具 開發文件(DEVDOC)

> 本文件是**完整實作規格**,交給任何 AI 或接手者都應能照做不出錯。
> 實作前請完整讀完,特別是「§10 常見出包點」——那些是 Windows 清理工具的經典地雷。

---

## 0. 專案定位

- Windows 專用的硬碟垃圾清理 GUI 程式,取代舊有的土炮 bat 檔。
- 核心理念:**「掃描 → 預覽 → 清理」三段式**。任何刪除動作前,使用者一定先看到「會刪什麼、能清多少」並打勾確認。程式內**不存在**「輸入任意路徑去刪」的功能——只清白名單模組定義的位置。
- 使用者是開發者本人,單機使用,不需要多語系、不需要自動更新。介面文字用繁體中文。
- 依賴極簡:只用 `PyQt6` 與 `send2trash`,其餘一律標準庫(含 `ctypes` 呼叫 WinAPI)。**不要**引入 psutil、pywin32 等額外套件。

### 技術棧

| 項目 | 選擇 |
|---|---|
| 語言 | Python 3.11+ |
| GUI | PyQt6 >= 6.5 |
| 回收桶(安全刪除) | send2trash >= 1.8 |
| WinAPI | ctypes(標準庫) |
| 打包 | PyInstaller(最後階段) |

`requirements.txt`:

```
PyQt6>=6.5.0
send2trash>=1.8.2
```

---

## 1. 專案結構

```
Cleaner/
├── README.md                # 對外介紹(最後再寫)
├── DEVDOC.md                # 本文件
├── requirements.txt
├── main.py                  # 進入點:建 QApplication、套主題、開 MainWindow
└── cleaner/
    ├── __init__.py
    ├── theme.py             # 顏色常數 + 全域 QSS + 發光效果工廠函式
    ├── state.py             # AppState 列舉、dataclass 定義(ScanResult 等)
    ├── workers.py           # 所有 QThread worker(掃描/清理/大檔案/重複檔/開發空間)
    ├── report.py            # 清理日誌:寫入 %LOCALAPPDATA%\NeonSweep\logs\
    ├── utils/
    │   ├── __init__.py
    │   ├── fs.py            # safe_walk、format_size、long_path、drive 列舉
    │   └── admin.py         # is_admin()、relaunch_as_admin()
    ├── widgets/
    │   ├── __init__.py
    │   ├── neon_button.py   # 圓形霓虹掃描按鈕(含呼吸光暈動畫)
    │   ├── neon_progress.py # 漸層進度條
    │   ├── drive_bar.py     # 磁碟容量霓虹燈條
    │   └── glow_line.py     # 裝飾用霓虹燈條(氛圍元素)
    ├── views/
    │   ├── __init__.py
    │   ├── main_window.py   # 主視窗:側邊導覽 + QStackedWidget 換頁
    │   ├── clean_page.py    # 主頁:三段式清理流程(狀態機)
    │   ├── bigfile_page.py  # 大檔案掃描器
    │   ├── dupe_page.py     # 重複檔案偵測
    │   └── devspace_page.py # node_modules / venv 掃描
    └── modules/
        ├── __init__.py      # ALL_MODULES 清單(依序註冊所有模組實例)
        ├── base.py          # CleanerModule 抽象基底 + 共用刪除邏輯
        ├── user_temp.py
        ├── system_temp.py
        ├── browser_cache.py
        ├── recycle_bin.py
        ├── thumbnail_cache.py
        ├── windows_update.py
        ├── crash_dumps.py
        └── dev_caches.py
```

---

## 2. 多硬碟策略(重要設計決策,不要改)

使用者電腦有多顆硬碟(例:2 SSD + 1 HDD)。策略如下:

1. **清理頁(主功能)不選硬碟、只跑一次。** 所有清理模組的目標路徑天然都在系統碟 C:(`%TEMP%`、`%LOCALAPPDATA%`、`C:\Windows\...`)。其他硬碟上不存在這些系統垃圾。UI 上不要出現硬碟選擇器。
2. **回收桶例外但不用處理**:每顆磁碟各有 `$Recycle.Bin`,但 `SHEmptyRecycleBinW(None, None, flags)` 傳 `None` 路徑就是一次清空**所有**磁碟的回收桶;`SHQueryRecycleBinW` 傳 `None` 也是查全部總量。所以一個 API 呼叫搞定,不需要逐碟。
3. **分析功能(大檔案/重複檔/開發空間)才需要選硬碟。** 每頁頂部放一排磁碟機切換按鈕(neon toggle chip,例:`C:` `D:` `E:`),可複選,預設只勾系統碟。多選時**依序掃描**(一顆掃完才掃下一顆),絕對不要多執行緒同時掃不同實體硬碟——HDD 會磁頭亂跳、整體更慢且看似當機。
4. 磁碟列舉方式(不用 psutil):

```python
import os, string, ctypes

def list_drives() -> list[str]:
    """回傳存在且為固定磁碟的磁碟機根目錄,如 ['C:\\', 'D:\\']"""
    DRIVE_FIXED = 3
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED:
            drives.append(root)
    return drives
```

只列 `DRIVE_FIXED`(排除光碟、USB 隨身碟、網路磁碟機)。

---

## 3. 核心資料結構(`state.py`)

```python
from dataclasses import dataclass, field
from enum import Enum, auto

class AppState(Enum):
    IDLE = auto()       # 只有一顆掃描按鈕
    SCANNING = auto()   # 掃描中
    PREVIEW = auto()    # 顯示掃描結果,等使用者勾選確認
    CLEANING = auto()   # 清理中
    DONE = auto()       # 顯示成果報告

@dataclass
class FileEntry:
    path: str
    size: int           # bytes

@dataclass
class ScanResult:
    module_id: str
    entries: list[FileEntry] = field(default_factory=list)
    total_size: int = 0
    locked_count: int = 0      # 掃描時就已無法存取的數量
    error_count: int = 0
    is_api_module: bool = False  # True = 回收桶這種不列個別檔案的模組

@dataclass
class CleanResult:
    module_id: str
    freed_bytes: int = 0
    deleted_count: int = 0
    skipped_count: int = 0     # 使用中/無權限而跳過
    errors: list[str] = field(default_factory=list)  # 只留前 100 筆,避免爆記憶體
```

---

## 4. 清理模組規格(`modules/`)

### 4.1 基底類別(`base.py`)

```python
class CleanerModule:
    module_id: str          # 唯一識別,如 "user_temp"
    display_name: str       # UI 顯示,如 "使用者暫存檔"
    description: str        # 一行說明,顯示在 UI 副標
    requires_admin: bool    # True 時 UI 顯示 🛡 標記,非管理員模式下停用該項
    min_age_hours: int = 24 # 只刪「最後修改時間超過 N 小時」的檔案;0 = 不過濾

    def scan(self, progress_cb) -> ScanResult: ...
    def clean(self, result: ScanResult, progress_cb) -> CleanResult: ...
```

- `progress_cb(module_id: str, current_path: str, count: int)`:worker 會把它接到 Qt signal。**節流規定:每處理 200 個檔案或每 100ms 才呼叫一次**,不要每個檔案都呼叫(signal 洪水會凍死 UI)。
- `base.py` 內實作共用函式 `scan_directory(root, min_age_hours, progress_cb)` 與 `delete_entries(entries, progress_cb)`,大多數模組直接組合這兩個函式 + 自己的路徑清單即可。

**共用刪除邏輯(`delete_entries`)的硬性規則:**

0. **路徑守衛(最後一道防線,不可省略)**:每個模組必須宣告 `allowed_roots: list[str]`(即它掃描的根目錄清單)。`delete_entries` 刪除每個檔案前,先把路徑 `os.path.normcase(os.path.abspath(path))` 正規化,驗證它以某個 `allowed_root`(同樣正規化後)為前綴;**不符者一律不刪**,記入 errors 並寫警告日誌。此守衛的目的是:即使上游掃描邏輯有 bug 混入了範圍外的路徑,刪除層也會拒絕執行。這條規則沒有例外。
1. **逐檔 try/except**。每個檔案獨立 `os.remove()`,捕 `PermissionError` / `OSError`,記入 `skipped_count`,繼續下一個。**絕對不要用 `shutil.rmtree()` 清整個目錄**——遇到第一個鎖定檔就整批失敗,這正是舊 bat 檔滿螢幕報錯的原因。
2. 檔案全部處理完後,對掃到的子目錄**由深到淺**嘗試 `os.rmdir()`(只會刪空目錄,非空會丟 OSError,吞掉即可)。**不要刪除模組根目錄本身**(如 `%TEMP%` 資料夾自己)。
3. 唯讀檔案先 `os.chmod(path, stat.S_IWRITE)` 再刪。
4. 路徑長度 > 250 字元時加 `\\?\` 前綴再操作(見 §10.4)。

### 4.2 各模組定義

註冊順序(= UI 顯示順序)如下表。路徑一律用 `os.environ` / `os.path.expandvars` 取得,不要寫死使用者名稱。

| module_id | display_name | requires_admin | min_age_hours |
|---|---|---|---|
| `user_temp` | 使用者暫存檔 | ✗ | 24 |
| `system_temp` | 系統暫存檔 | ✓ | 24 |
| `browser_cache` | 瀏覽器快取 | ✗ | 0 |
| `thumbnail_cache` | 縮圖快取 | ✗ | 0 |
| `crash_dumps` | 錯誤報告與傾印檔 | ✓ | 0 |
| `windows_update` | Windows Update 快取 | ✓ | 0 |
| `dev_caches` | 開發者快取 | ✗ | 0 |
| `ai_caches` | AI 工具快取 | ✗ | 0 |
| `recycle_bin` | 資源回收桶 | ✗ | 0 |

**`user_temp`** — 掃描 `%TEMP%`(即 `%LOCALAPPDATA%\Temp`)整棵樹。

**`system_temp`** — `C:\Windows\Temp`。

**`browser_cache`** — 只刪快取,**絕不碰** Cookies、History、Login Data、Bookmarks 等任何資料庫檔。目標路徑(全部要支援多 profile,用 `glob`):

```
Chrome  : %LOCALAPPDATA%\Google\Chrome\User Data\{Default,Profile *}\Cache\Cache_Data
          %LOCALAPPDATA%\Google\Chrome\User Data\{Default,Profile *}\Code Cache
          %LOCALAPPDATA%\Google\Chrome\User Data\{Default,Profile *}\GPUCache
Edge    : %LOCALAPPDATA%\Microsoft\Edge\User Data\{Default,Profile *}\(同上三種)
Firefox : %LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\cache2
```

路徑不存在(沒裝該瀏覽器)就靜默跳過。瀏覽器開著時大量檔案會被鎖定——照常掃、清理時跳過即可,不要提示使用者關瀏覽器以外的動作(在預覽頁加一行小字提示「關閉瀏覽器可清得更乾淨」即可)。

**`thumbnail_cache`** — `%LOCALAPPDATA%\Microsoft\Windows\Explorer\` 下的 `thumbcache_*.db` 與 `iconcache_*.db`。這些檔通常被 Explorer 鎖住,刪不掉就跳過,**絕對不要**殺 explorer.exe 或停任何程序。

**`crash_dumps`** — 以下位置整棵樹:
```
%LOCALAPPDATA%\CrashDumps
C:\ProgramData\Microsoft\Windows\WER\ReportQueue
C:\ProgramData\Microsoft\Windows\WER\ReportArchive
C:\Windows\Minidump
C:\Windows\MEMORY.DMP        (單一檔案)
```

**`windows_update`** — `C:\Windows\SoftwareDistribution\Download` 整棵樹。**不要**停止/重啟 wuauserv 服務,被鎖的檔案跳過即可。

**`dev_caches`** — 各套件管理器的下載/HTTP 快取(刪了只是重新下載,零風險)。存在才列入:
```
pip   : %LOCALAPPDATA%\pip\cache
npm   : %LOCALAPPDATA%\npm-cache
yarn  : %LOCALAPPDATA%\Yarn\Cache
NuGet : %LOCALAPPDATA%\NuGet\v3-cache
```
注意:**不要**清 `%USERPROFILE%\.nuget\packages`(那是專案正在引用的套件本體,不是快取)。

**`ai_caches`**(使用者後續要求新增,不屬於 M1-M5 原始規格)— HuggingFace / PyTorch Hub / InsightFace 的模型下載快取,以及 NVIDIA CUDA 編譯快取。刪了只是下次使用時該套件自動重新下載/重新編譯,不是唯一副本。存在才列入:
```
HuggingFace : %USERPROFILE%\.cache\huggingface
PyTorch Hub : %USERPROFILE%\.cache\torch
InsightFace : %USERPROFILE%\.insightface
CUDA 編譯快取: %LOCALAPPDATA%\NVIDIA\ComputeCache
```
注意:部分模型檔案單一就是好幾 GB,PREVIEW 頁照常顯示大小讓使用者自行評估,不特別加額外警語。

**`recycle_bin`** — 特殊模組,`is_api_module = True`,不列個別檔案。用 ctypes:

```python
import ctypes
from ctypes import wintypes

class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong)]

def query_recycle_bin() -> tuple[int, int]:
    """回傳 (總 bytes, 項目數),涵蓋所有磁碟"""
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    return info.i64Size, info.i64NumItems

def empty_recycle_bin() -> int:
    SHERB_NOCONFIRMATION = 0x1
    SHERB_NOPROGRESSUI   = 0x2
    SHERB_NOSOUND        = 0x4
    return ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7)
```

回傳值非 0 **不視為錯誤**(回收桶已空時 API 會回 `0x8000FFFF`),clean 前先再查一次大小,為 0 就直接回報成功。**絕對不要**自己去遍歷 `X:\$Recycle.Bin` 刪檔案。

### 4.3 刻意不做的項目(不要自作主張加回來)

- **Prefetch(`C:\Windows\Prefetch`)**:是開機/程式啟動加速資料,清了反而變慢。
- **瀏覽器 Cookies / 歷史紀錄**:會把使用者登出所有網站。
- **登錄檔清理**:風險高、收益趨近於零,不做。
- **記憶體優化 / 服務管理**:超出範圍。

---

## 5. 安全規則(全域強制)

1. **白名單制**:刪除只發生在模組寫死的路徑。任何 UI 都不提供自訂路徑刪除。
2. **不跟隨 junction / symlink**:見 §10.3,`safe_walk` 是唯一允許的遍歷方式,所有模組與分析功能都必須用它。
3. **年齡過濾**:temp 類模組預設只刪 24 小時前的檔案(`min_age_hours`),以 `st_mtime` 判斷。
4. **清理日誌**(`report.py`):每次清理在 `%LOCALAPPDATA%\NeonSweep\logs\clean_YYYYMMDD_HHMMSS.log` 寫入純文字:每行 `刪除|跳過 <size> <path>`,結尾寫總結。報告頁提供「開啟日誌」按鈕(`os.startfile`)。
5. **分析功能的刪除一律走 `send2trash`**(進回收桶可救回),且需要確認對話框。清理模組(temp 等)則直接刪(它們本來就是垃圾,進回收桶等於沒清)。
6. **刪除前重新驗證**:分析功能執行 `send2trash` 前,對每個檔案重新 `stat`;檔案已不存在 → 靜默跳過;**大小與掃描時不符 → 拒刪**(檔案在掃描後被修改過,當初的判斷已失效),記入跳過清單並在結果中告知。
7. **超大檔案警示**:單檔 > 10 GB 的項目,確認對話框需額外標示:「此檔案可能超過回收桶容量上限,系統或將直接永久刪除」。
8. **零自動刪除原則**:整個程式不存在任何未經使用者本次點擊確認的刪除路徑——沒有排程清理、沒有開機自動執行、沒有「掃描完自動清理」選項。任何與此牴觸的功能請求都應拒絕實作。

---

## 6. 執行緒模型(`workers.py`)

**規定寫法:worker 繼承 `QObject`、`moveToThread`,不要子類化 QThread。**

```python
class ScanWorker(QObject):
    module_started  = pyqtSignal(str)                 # module_id
    module_finished = pyqtSignal(str, object)         # module_id, ScanResult
    progress        = pyqtSignal(str, str, int)       # module_id, current_path, count
    finished        = pyqtSignal()

    def __init__(self, modules: list):
        super().__init__()
        self._modules = modules
        self._cancelled = False

    def cancel(self):          # 由主執行緒呼叫,只設旗標
        self._cancelled = True

    def run(self):
        for m in self._modules:
            if self._cancelled:
                break
            self.module_started.emit(m.module_id)
            result = m.scan(self._make_cb(m.module_id))
            self.module_finished.emit(m.module_id, result)
        self.finished.emit()
```

啟動樣板(每個 worker 都一樣):

```python
self._thread = QThread(self)
self._worker = ScanWorker(modules)
self._worker.moveToThread(self._thread)
self._thread.started.connect(self._worker.run)
self._worker.finished.connect(self._thread.quit)
self._worker.finished.connect(self._worker.deleteLater)
self._thread.finished.connect(self._thread.deleteLater)
# 接 UI 更新 signal 後:
self._thread.start()
```

- 取消機制:所有長迴圈(掃描、雜湊、刪除)每個項目都檢查 `self._cancelled`。
- `CleanWorker`、`BigFileWorker`、`DupeWorker`、`DevSpaceWorker` 同樣模式。
- **鐵律:worker 內不准 touch 任何 QWidget,只准 emit signal。**

---

## 7. GUI 規格

### 7.1 視覺主題(`theme.py`)— Cyberpunk 霓虹

與使用者其他專案(VaultMe 等)風格區隔:**純黑底 + 粉/藍霓虹**。

**色票(定義為常數,全專案只准用這些):**

```python
BG          = "#000000"   # 主背景,純黑
BG_PANEL    = "#0a0a0f"   # 卡片/面板底
BG_HOVER    = "#12121a"
NEON_PINK   = "#ff2e88"
NEON_PINK_L = "#ff6ec7"   # 亮粉(hover / 漸層端點)
NEON_BLUE   = "#00e5ff"
NEON_BLUE_D = "#4da6ff"
TEXT_MAIN   = "#e8e8f0"
TEXT_DIM    = "#8a8a9a"
DANGER      = "#ff3b5c"
OK          = "#39ffb0"
```

**漸層**:粉→藍是本專案的招牌,QSS 寫法:
`qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff2e88, stop:1 #00e5ff)`

**光暈(glow)**:QSS **沒有** box-shadow,發光一律用 `QGraphicsDropShadowEffect`:

```python
def make_glow(color: str, radius: int = 25) -> QGraphicsDropShadowEffect:
    eff = QGraphicsDropShadowEffect()
    eff.setBlurRadius(radius)
    eff.setColor(QColor(color))
    eff.setOffset(0, 0)
    return eff
# 注意:一個 effect 實例只能掛一個 widget,每個 widget 要 new 一個
```

**呼吸光暈動畫**(掃描按鈕 IDLE 時):`QPropertyAnimation` 打在 effect 的 `blurRadius` 上,15 ↔ 40 往返,週期 2 秒,`QEasingCurve.InOutSine`,無限循環。

**氛圍燈條(`glow_line.py`)**:高度 2px 的 QFrame,背景設粉藍漸層,掛 glow effect。用在:視窗頂部一條橫貫燈條、側邊欄與內容區的分隔線、報告頁數字下方。這是氛圍感的主要來源,便宜又有效。

**字體**:數字與路徑用 `Consolas`,一般文字用 `Microsoft JhengHei UI`。大數字(釋放容量)用 48pt Consolas 粉色 + glow。

**深色標題列**(加分,失敗就算了,包 try/except):

```python
import ctypes
hwnd = int(self.winId())
ctypes.windll.dwmapi.DwmSetWindowAttribute(
    hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)  # DWMWA_USE_IMMERSIVE_DARK_MODE
```

**全域 QSS 要涵蓋**:QScrollBar(細、深灰、hover 變粉)、QCheckBox(勾選時粉藍漸層方塊)、QTableWidget/QTreeWidget(黑底、格線 #1a1a24、選取列半透明粉)、QPushButton(透明底 + 1px 霓虹描邊,hover 時底色 BG_HOVER)、QToolTip。

### 7.2 主視窗(`main_window.py`)

- 初始大小 1060×700,最小 960×640。
- 版面:左側 64px 窄側欄(只有 icon 的導覽按鈕,選中的有粉色 glow + 左緣 3px 粉色指示條)+ 右側 `QStackedWidget`。
- 側欄頁面:① 清理(主頁) ② 大檔案 ③ 重複檔案 ④ 開發空間。icon 用 Unicode 字元即可(⚡ 📦 ⧉ ⌘ 之類),不要引入 icon 套件。
- 視窗標題:`NeonSweep`。
- **清理進行中(SCANNING / CLEANING)時鎖定側欄導覽**,避免使用者切頁後狀態混亂。

### 7.3 清理頁(`clean_page.py`)— 狀態機驅動

用 `QStackedWidget` 內嵌四個子畫面,依 `AppState` 切換:

**IDLE**:
- 頂部:磁碟概覽列——每顆磁碟一個 `drive_bar`(見 §8.2),這也是加分功能 2 的落點。
- 正中央:直徑 180px 的圓形掃描按鈕(`neon_button.py`):黑底、3px 粉藍漸層圓環描邊、中央文字「掃描」、呼吸光暈。圓形按鈕作法:固定大小 QPushButton + `border-radius: 90px` + 漸層 border。
- 按鈕下方一行 TEXT_DIM 小字:「掃描系統垃圾,不會刪除任何檔案」。
- 非管理員時,右下角顯示「⛨ 以管理員身分重新啟動」小按鈕(見 §9)。

**SCANNING**:
- 模組清單(每列:模組名 + 狀態)。進行中的列顯示動態「掃描中…」與當前路徑(TEXT_DIM、`QFontMetrics.elidedText` 中間截斷),完成的列顯示大小(如 `1.24 GB`)並亮綠色 ✓。
- 底部:不定長度進度條(粉藍漸層跑馬燈)+「取消」按鈕。
- 掃描結束自動切到 PREVIEW。

**PREVIEW**:
- 每個模組一列卡片:霓虹 checkbox + 模組名 + description + 右側大小(Consolas、粉色)。
- 掃到 0 bytes 的模組:disabled、不勾。requires_admin 但目前非管理員的模組:disabled + 🛡 提示。其餘預設全勾。
- 每列可展開(點列展開/收合)看檔案明細:QTreeWidget 或子列表,**最多顯示前 500 筆**,超過顯示「…以及另外 N 個檔案」。明細只是讓人抽查,不提供單檔取消勾選(粒度到模組為止,簡化狀態)。
- 底部固定列:「總計可釋放 X.XX GB」(大字、glow)+ 漸層大按鈕「開始清理」+ 次要按鈕「重新掃描」。
- browser_cache 有被鎖檔案時,卡片下加一行小字:「部分檔案使用中,關閉瀏覽器可清得更乾淨」。

**CLEANING**:
- 頂部:總進度條(`neon_progress.py`:黑底、粉藍漸層 chunk、掛 glow)。進度分母 = 所有勾選模組的檔案總數,分子 = 已處理數(刪除+跳過都算)。
- 中間資訊區(Consolas):目前模組名、目前檔案路徑(elided)、已釋放 X.XX GB、已刪除 N 檔、已跳過 N 檔——全部即時跳動。
- 下方:唯讀 `QPlainTextEdit` 即時日誌,`setMaximumBlockCount(2000)` 防爆,黑底綠字(OK 色)、跳過的行用 TEXT_DIM。
- 「取消」按鈕:停止後直接進 DONE,報告已完成的部分。

**DONE**:
- 中央大字:`已釋放 X.XX GB`(48pt 粉色 glow),下方氛圍燈條。
- 統計行:刪除 N 個檔案 · 跳過 N 個(使用中或無權限)· 耗時 M 分 S 秒。
- 磁碟概覽列刷新,顯示清理前後對比(C: 剩餘空間 +X GB)。
- 按鈕:「開啟日誌」「回到首頁」(回 IDLE 並重置)。

### 7.4 尺寸格式化

統一函式 `format_size(n)`:以 1024 為底,輸出 `B / KB / MB / GB`,保留兩位小數。全 UI 只准用這個函式。

---

## 8. 分析功能頁(加分功能 1–4)

四頁共用元素:**磁碟選擇列**(§2 的 toggle chips)+「開始掃描」霓虹按鈕 + 掃描中顯示「已掃描 N 個檔案」計數器與當前路徑(整碟掃描無法預知總量,用不定進度條 + 計數器,不要假裝有百分比)+「取消」。

### 8.1 大檔案掃描器(`bigfile_page.py`)— 含用途分類與閒置判斷

本頁是使用者的核心需求之一:硬碟上有大量 AI 模型檔(ComfyUI、FaceFusion),需要列出「多大、什麼用途、多久沒用」供人工評估刪除。

**掃描:**
- 對選定磁碟 `safe_walk` 全樹,維護一個 top-200 最小堆(`heapq`,元素 `(size, path, mtime, atime)`),記憶體恆定。
- 排除目錄(寫成 `EXCLUDED_DIRS` 常數,比對不分大小寫):`C:\Windows\WinSxS`、`C:\Windows\servicing`、`System Volume Information`、`$Recycle.Bin`、`%LOCALAPPDATA%\NeonSweep`。C:\Windows 其他部分照掃(只是列出,不會誤刪)。

**用途分類器(`classify(path) -> tuple[category, role]`):**

規則順序:**先比對路徑規則,再比對副檔名**,都沒中歸「其他」。比對一律不分大小寫。

路徑規則(`role` 是顯示在 UI 的具體角色說明):

| 路徑片段(包含即中) | category | role |
|---|---|---|
| `\ComfyUI\models\checkpoints\` | AI 模型 | Checkpoint 主模型 |
| `\ComfyUI\models\diffusion_models\` 或 `\unet\` | AI 模型 | 擴散模型 |
| `\ComfyUI\models\loras\` | AI 模型 | LoRA |
| `\ComfyUI\models\controlnet\` | AI 模型 | ControlNet |
| `\ComfyUI\models\vae\` | AI 模型 | VAE |
| `\ComfyUI\models\clip\` 或 `\text_encoders\` | AI 模型 | 文字編碼器 |
| `\ComfyUI\models\upscale_models\` | AI 模型 | 放大模型 |
| `\ComfyUI\models\embeddings\` | AI 模型 | Embedding |
| `\ComfyUI\models\`(其餘子目錄) | AI 模型 | ComfyUI 模型(<子目錄名>) |
| `\facefusion\` 且含 `\.assets\` 或 `\models\` | AI 模型 | FaceFusion 模型 |
| `\steamapps\` | 遊戲 | Steam 遊戲檔 |
| `\Epic Games\` | 遊戲 | Epic 遊戲檔 |

副檔名規則:

| 副檔名 | category |
|---|---|
| `.safetensors` `.ckpt` `.pt` `.pth` `.gguf` `.onnx` | AI 模型(role = 模型檔) |
| `.bin`(僅當路徑含 `model`)| AI 模型 |
| `.mp4` `.mkv` `.mov` `.avi` `.webm` `.ts` | 影片 |
| `.iso` `.img` `.vhd` `.vhdx` `.vmdk` `.wim` | 映像檔 |
| `.zip` `.7z` `.rar` `.tar` `.gz` | 壓縮檔 |

**「多久沒用」— atime 可靠性偵測(重要,不要跳過):**

NTFS 的最後存取時間(atime)更新預設多半只在系統碟啟用,其他磁碟的 atime 可能凍結在檔案建立當下,**直接顯示會誤導使用者**。必須偵測並誠實標示:

```python
import os, winreg

def atime_reliable(drive_root: str) -> bool:
    """該磁碟的『最後存取時間』是否可信"""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            val, _ = winreg.QueryValueEx(k, "NtfsDisableLastAccessUpdate")
    except OSError:
        return False
    mode = val & 0xF          # 實際值常是 0x80000002,取低位
    if mode in (1, 3):        # 明確停用
        return False
    if mode == 0:             # 使用者手動啟用,全磁碟有效
        return True
    # mode == 2:系統管理模式,只保證系統碟有更新
    sysdrive = os.environ.get("SystemDrive", "C:").upper()
    return drive_root.upper().startswith(sysdrive)
```

- atime 不可信的磁碟:結果表格上方顯示警告橫幅(DANGER 色細字):「⚠ 此磁碟未啟用存取時間記錄,『最後存取』欄僅供參考,請搭配『最後修改』判斷」,且該欄數值以 TEXT_DIM 淡色顯示。
- **不要**提供「幫使用者開啟 atime 記錄」的功能(要改登錄檔且只對未來有效,超出範圍)。

**結果 UI:**
- 表格欄位:大小(預設遞減排序)、類型(category)、用途(role)、檔名、完整路徑、最後存取、最後修改。各欄可點擊排序。
- 「最後存取」「最後修改」顯示為「2024/08/12(11 個月前)」格式,超過 180 天的相對時間用粉色標示「久未使用」。
- 表格上方一排**類型篩選 chips**:`全部` `AI 模型` `影片` `映像/壓縮` `遊戲` `其他`,樣式同磁碟選擇 chips。
- 篩選列旁顯示**分類統計條**:每個類型的「N 檔 / 合計 X GB」——這是使用者評估「整批 ComfyUI 模型佔多少」的關鍵數字。
- 每列操作:「開啟位置」(`subprocess.Popen(['explorer', '/select,', path])`)、「刪除」(確認對話框 → `send2trash`)。AI 模型類的確認對話框加一行:「模型檔可從 Civitai / HuggingFace 重新下載」。
- 表格右鍵選單:複製路徑。
- 支援 checkbox 多選 + 底部「刪除勾選項目(共 X GB)」按鈕,同樣走確認對話框 + `send2trash`。

### 8.2 磁碟空間概覽(`drive_bar.py`,嵌在清理頁頂部)

- 每顆磁碟一條橫向燈條:`shutil.disk_usage(root)` 取得容量。
- 造型:圓角膠囊,已用部分 = 粉藍漸層,剩餘部分 = BG_PANEL;使用率 > 90% 時漸層改成 DANGER 單色。左標 `C:`,右標 `已用 412 GB / 931 GB`。掛淡淡 glow。
- 清理完成後呼叫 `refresh()` 重新讀取。

### 8.3 重複檔案偵測(`dupe_page.py`)

使用場景:使用者的 2TB HDD 存大量影片/照片,常發生同一檔案重複下載、只是檔名不同(`video_A.mp4` vs `video_A (2).mp4`)。偵測靠**內容雜湊**,與檔名完全無關。

**範圍限制(寫給實作者):這個頁面只偵測位元組完全相同的檔案。** 不在本頁做感知雜湊(perceptual hash)、不在本頁做「相似但重新編碼」的影片比對——本頁刻意維持「有就是有、沒有就是沒有」的絕對正確性(零誤判)。感知式的相似比對(縮放/轉檔/剪輯)另外做在獨立的「相似檔案」頁面(§8.6),兩者定位不同、刻意分離,不要把感知雜湊塞進本頁。

**掃描選項列:**
- 磁碟選擇 chips(§2)之外,加**檔案類型 chips**:`全部` `影片` `圖片` `音訊`(可複選,預設全部)。副檔名:影片同 §8.1 表;圖片 `.jpg .jpeg .png .gif .webp .bmp .heic .tif .tiff`;音訊 `.mp3 .flac .wav .m4a .ogg`。選了類型就只掃該類副檔名,大幅縮短整碟掃描時間。
- 最小檔案門檻下拉:`500 KB / 1 MB(預設)/ 10 MB / 100 MB`。

三階段漏斗(效率關鍵,不要跳步):

1. **依大小分組**:`safe_walk` 收集 `size → paths`,只保留同大小 ≥ 2 個且 size ≥ 門檻的組。此階段只讀 metadata 不讀內容。**記憶體**:整碟掃描絕大多數大小是單例,dict 值用「先存單一字串、第二次撞到同大小才升級成 list」,避免每個單例都揹一個 list 物件。
2. **前 4 KB 快速雜湊**:同組內讀每檔前 4096 bytes 算 `hashlib.blake2b`,再分組,淘汰不同者。
3. **全檔雜湊**:剩餘的以 1 MB chunk 迭代算 blake2b。讀檔全程 try/except,讀不到就踢出該組。**大檔雜湊要能中途取消**:cancel 檢查放進 chunk 讀取迴圈裡(不是只在檔案之間),否則雜湊一個數十 GB 的映像檔期間按取消要等整個檔案跑完。

**硬連結去重(在最後成組時做,不在階段 1)**:pnpm 全域 store、部分系統檔會讓同一份實體資料(`st_dev, st_ino`)出現在多個路徑,它們必然 byte 相同而落在同組,但刪任一個都不會釋放空間(「可省 X GB」會造假)、還白白重複讀取。**不能在階段 1 做**:Windows 上 `os.scandir` 的 `DirEntry.stat` 回傳的 `st_nlink`/`st_ino` 都是 0(用廉價的目錄掃描資料),拿不到硬連結資訊;對全碟每個檔案都改用 `os.stat`(能拿到真值)太貴。改在最後把已確定 byte 相同的少數候選,用 `os.stat` 取 `(st_dev, st_ino)` 去重、每個 inode 只留一個代表(`st_ino` 為 0 的卷不去重、照常保留)。

進度 UI 要顯示目前階段:「第 1/3 階段:比對檔案大小(已掃 N 檔)」→「第 2/3 階段:快速比對(M 組候選)」→「第 3/3 階段:完整雜湊(第 i/j 檔)」。第 3 階段有明確分母,用真實百分比進度條。

**結果 UI:**
- QTreeWidget,頂層 = 重複組(顯示「N 個相同檔案 × 每個 X MB,可省 (N-1)×X MB」),子層 = 各檔路徑 + checkbox + 修改日期。頂部顯示總計:「共 K 組重複,合計可釋放 X GB」。
- **右側預覽窗格**:點選任一子列,若為圖片檔就用 `QPixmap` 載入縮圖(等比縮至最長邊 320px,失敗顯示「無法預覽」);影片與其他類型只顯示中繼資料(大小、修改日期),不解碼影片、不做影片縮圖。
- **複本命名自動勾選**:載入結果時,若組內同時存在「乾淨檔名」與「複本樣式檔名」,自動預先勾選複本樣式者。複本樣式判定(對不含副檔名的檔名):regex `[ _-]?\(\d+\)$`、結尾為 ` - 複製`、` - Copy`、`_copy`。全組都是複本樣式或全組都乾淨時不預勾,交給使用者。
- 組內快捷鈕:「保留最舊」「保留最新」自動勾選其餘。
- **防呆:不允許整組全勾**——若使用者把一組全勾,底部刪除鈕 disabled 並提示「每組至少保留一個」。
- 刪除 = 確認對話框(列出總數與總大小)→ `send2trash`(進回收桶,可救回)。

### 8.4 開發空間掃描(`devspace_page.py`)

- 對選定磁碟 `safe_walk` 找目錄名 ∈ `{node_modules, .venv, venv, .tox, target}`(target 只在旁邊有 `Cargo.toml` 時才算,避免誤判)。
- **找到即停止深入**(不進入該目錄繼續遞迴找),但要另開計算流程算它的總大小。
- 專案識別:該目錄的**上層目錄**即專案根;「最後活動時間」= 專案根下第一層檔案的最大 mtime(不含該快取目錄本身)。
- 表格:專案路徑、類型(node_modules/venv/…)、大小、最後活動。依大小遞減。超過 180 天未動的列,最後活動欄顯示粉色標記「久未使用」。
- 操作:「開啟位置」+「刪除」(確認對話框寫明「重新安裝依賴即可復原(npm install / pip install)」→ `send2trash`)。**不提供全選刪除**,一次刪一個,防手滑。

### 8.5 系統空間診斷(`diagnostic_page.py` / `diagnostics.py`)— 唯讀,零刪除

使用者後續要求新增,不屬於 M1-M5 原始規格。這裡列的都是「風險太高、這套工具不會去清」的系統空間佔用類別,只負責讀取/估算大小並顯示,**不提供任何刪除功能**——凡是這套工具沒把握安全逐檔刪除的東西,寧可只顯示數字、附上建議的外部工具,也不要冒險自己動手。呼應 §5 規則 1 的白名單精神。

五個類別,依序查詢:

| key | 顯示名稱 | 資料來源 | 建議處理方式 |
|---|---|---|---|
| `winsxs` | 元件存放區(WinSxS) | `safe_walk` 累計 `%SystemRoot%\WinSxS` 大小 | 磁碟清理→清理系統檔案,或 `DISM /Online /Cleanup-Image /StartComponentCleanup` |
| `driverstore` | 驅動殘留(DriverStore) | `safe_walk` 累計 `%SystemRoot%\System32\DriverStore\FileRepository` 大小 | 驅動廠商解安裝工具(如 NVIDIA DDU)或裝置管理員手動移除舊版 |
| `hiberfil` | 休眠檔 | `os.path.getsize` 讀 `%SystemDrive%\hiberfil.sys` | 不需要休眠功能的話 `powercfg /hibernate off` |
| `pagefile` | 分頁檔 | `os.path.getsize` 讀 `%SystemDrive%\pagefile.sys` | 通常不需處理 |
| `shadow_copy` | System Restore 還原點 | `vssadmin list shadowstorage /for=<drive>` 解析輸出 | 控制台→系統保護→調整還原點磁碟空間上限 |

**實作細節:**
- WinSxS/DriverStore 用 `safe_walk` 遞迴加總,權限錯誤時 `complete=False`,UI 顯示「部分項目因權限被略過,僅供參考」而非假裝精確。
- `vssadmin` 輸出的文字標籤會隨系統語系不同,**不比對標籤字串**,改用正規表示式 `([\d.]+)\s*(TB|GB|MB|KB|B)\s*\((\d+)%\)` 抓「數字+單位+百分比」格式,固定取第一筆(Used,vssadmin 固定先印 Used 再印 Allocated/Maximum)。失敗(非管理員、`vssadmin` 不存在、逾時)一律回傳 `None`,UI 顯示「無法讀取(可能需要管理員權限)」。
- 五個類別依序查詢(非平行),前兩個是耗時的目錄樹掃描、有進度回報,後三個是單一系統呼叫、即查即回。

### 8.6 相似圖片/影片偵測(`similarity_page.py` / `similarity.py`)— 感知雜湊,使用者後續要求新增

與 §8.3 重複檔案(位元組完全相同)**刻意分離**:本頁用感知雜湊做**機率性**相似判斷,天生會有誤判,故 UI 一律呈現候選 + 縮圖 +(影片)相似片段,刪除決定權在使用者。純邏輯層 `similarity.py` 不碰 Qt(比照 `analysis.py`);Qt 層在 `workers.SimilarityWorker`。

**依賴:opencv-python**(numpy 隨之)。`workers.SimilarityWorker.run()` **延遲載入** `cleaner.similarity`(其頂層 `import cv2`),沒裝 opencv 的環境仍能啟動 App、其餘功能照常,只有本頁掃描會 emit `error` 回報。**打包關鍵**:`NeonSweep.spec` 的 `excludes` 原本排除 `numpy`,加入本功能後**必須移除該排除**並在 `hiddenimports` 加 `cv2`、`numpy`,否則 frozen exe 內相似偵測會壞掉;代價是 exe 體積約 +80~120MB(見 `NeonSweep.spec` 與 `requirements.txt` 註解)。

**指紋:dHash。** 灰階 → resize 9×8 → 相鄰像素亮度差 → 64-bit。圖片走 `cv2.imdecode(np.fromfile(path))` 而非 `cv2.imread`(後者對 Windows 非 ASCII 路徑會失敗)。

- **圖片** `find_similar_images`:`safe_walk` 收 `IMAGE_EXTS` → 算 dHash → **錨點分群**(不是 Union-Find)。pairwise 是 O(n²),用 numpy 向量化 popcount 加速。**抓得到**縮放/轉檔/重壓縮/亮度微調;**抓不到**裁切/局部塗改/浮水印遮蓋(那要 ORB/SIFT 局部特徵匹配,明確不在範圍)。
  - **分群演算法(重要,不要改回 Union-Find)**:最早用 Union-Find 遞移合併(A~B、B~C 在門檻內就焊進同一組),使用者實測掃 `C:\Windows\Web` 這種平滑漸層佈景桌布時,炸出一組 27,787 個成員、組內任兩張圖可能完全不像的長鏈——dHash 對平滑漸層圖案的鑑別力本來就弱,加上 Union-Find 的遞移合併沒有距離上限,只要有夠多「稍微像一點」的過渡圖當墊腳石就會焊成一條長鏈。改成**錨點分群(star clustering)**:依序把每張還沒分組的圖片當錨點,只收「跟錨點本身距離 ≤ threshold」的圖片進同一組(不是「跟組內任何成員像就收」),已分組的圖片不會再被其他錨點搶走。因為 Hamming 距離滿足三角不等式,**同一組內任兩張圖的距離保證 ≤ threshold×2**——現在的 Union-Find 版本沒有這個保證,這是唯一決定性的差異。運算量仍是 O(n²),不會變慢;取捨是分群結果會跟掃描/走訪順序有點關係(哪張圖先當錨點會影響最終怎麼分組),不是全域最優解,但遠勝過「完全沒有距離上限、看緣分連成長鏈」。
- **影片** `find_similar_videos`(**兩階段:粗篩→精修**):
  1. **粗篩指紋** `build_video_print`:每部影片各自按時間點(`CAP_PROP_POS_MSEC`)取樣算 dHash,間隔 = `max(base_interval, duration/max_samples)`——**時長 <= base_interval×max_samples(預設 300 秒)的短片,間隔維持 base_interval(1 秒)不受影響;只有長片才自動放寬間隔**,保證固定的 `max_samples`(預設 300)個樣本點就能涵蓋全片。這是修過的設計:早期版本用固定 1 秒間隔 + 樣本數上限,長片(例如 60 分鐘)只會取樣到前 5 分鐘,中後段剪出來的片段完全偵測不到,已改掉。
  2. **兩邊都還沒被放寬過(短片對短片)**:兩邊本來就是同一個間隔、相位天生一致(都從 t=0 開始),直接對兩者的粗篩指紋跑 **Smith-Waterman 區域比對**(`_local_align`,允許 gap → 處理掐頭去尾/抽掉中段的剪輯)即可,不必再解一次影片。
  3. **任一邊被放寬過(牽涉到長片)**:改用 **位移投票**(`_estimate_offset`):不要求兩邊取樣點對齊到同一個網格相位(兩邊各自從 t=0 取樣,真正的重疊位移是未知數、不會剛好是取樣間隔的整數倍,硬要對齊網格反而讓兩邊取樣點集合幾乎不重疊而找不到匹配——這是實作時踩到的一個坑,類比 Shazam 音訊指紋的做法改成位移投票才修好);粗篩比對抓出「哪些樣本對內容相近」,每一對估出一個位移量,投票選出票數最高的候選,再把短的那部投影到長的那部時間軸上,得到候選重疊窗。
  4. **精修** `_refine_match`:只在候選窗附近(留 margin)重新用 `base_interval` 密集取樣兩段短內容、重新跑一次(範圍小、成本低的)Smith-Waterman,拿到精確到秒的邊界,同時二次驗證排除粗篩階段的巧合匹配。`min_match_seconds`(最短連續相似秒數)在這裡把關,過濾黑畫面/共用片頭之類的巧合。
  - 非 ASCII 路徑後援:`GetShortPathNameW` 取 8.3 短路徑重試。效能:粗篩樣本數固定(≤ max_samples),DP 成本不隨影片長度增長;精修只在候選窗這種小範圍內重新解碼,不必為了長片全片精細比對而讓運算量爆炸。
  - **`_refine_match` 精修窗必須有樣本上限(別再拿掉)**:候選窗長度沒有先天上限——兩部都是長片、offset≈0 時窗長會逼近整片。若固定用 `base_interval` 密集取樣,解碼次數(每秒一次 seek)與 `_local_align` 的 O(na·nb) 純 Python DP 會隨窗長無上限爆掉(兩部 60 分鐘影片 = 3600×3600 DP + 數千次 seek,記憶體數百 MB、單一對就要數分鐘)。所以精修間隔依窗長自動放寬,確保兩邊取樣數都 ≤ `VIDEO_REFINE_MAX_SAMPLES`(600):窗短維持秒級精度、長窗退到幾秒精度(顯示相似區間夠用)。這是跟粗篩 `max_samples` 同性質的「把無上界變有界」保護。
  - **相似秒數用「真正對到的幀數」而非「對齊跨度」**:`_local_align` 回傳的 `match_count` 是最佳區段回溯路徑上真正 match 的幀數;`matched_sec = match_count × interval`。Smith-Waterman 的區段裡可以夾雜 mismatch/gap,用跨度當相似時間會高估、放進假匹配,用實際對到的幀數才擋得住「跨度長但一堆錯配」的巧合(比舊的跨度版更嚴,不會更寬鬆)。
  - **`image_dhash` 用 `IMREAD_REDUCED_GRAYSCALE_8` 先 1/8 解碼**再 resize 到 9×8(大 JPEG 快數倍;縮完任一邊 < 9 才退回全解析度重解)。**注意這不是無損替換**:縮小解碼與全解析度解碼的 dHash 不保證逐 bit 相同(INTER_AREA 來源像素不同),差異僅一兩 bit、遠在門檻內——是速度/精度取捨,跟 `_popcount` 那次(完全等價)不同。
  - **退化指紋直接跳過**:純色圖 dHash 全 0、平滑漸層可能全 1,鑑別力極低(純黑圖彼此距離恆 0),`popcount` 落在 `[IMAGE_DEGENERATE_POPCOUNT, 64-IMAGE_DEGENERATE_POPCOUNT]` 之外的不納入分群,避免製造假群組。
  - **完全相同的 dHash 先摺疊再分群**:O(n²) 錨點分群前先把相同指紋收成同一桶,只對「相異指紋的代表」跑分群,完全重複的圖(重存/重複下載很常見)可大幅縮小 n;距離保證不變(代表 ≤ threshold、桶內成員 = 0,同組任兩張 ≤ threshold×2)。
  - **`_popcount_u64` 用 `np.bitwise_count`(numpy>=2.0)算,不要改回 `np.unpackbits`**:實測後者慢約 240 倍,兩兩比對是這個模組最熱的路徑,大型影片庫(千部等級)會從小時等級的等待時間變成分鐘等級。numpy<2.0 環境有 unpackbits 版本的後援實作。
  - **階段 1 平行解碼**:`find_similar_videos` 先用 `safe_walk` 蒐集完所有影片路徑(快、事先知道總數),再用 `ThreadPoolExecutor(max_workers=VIDEO_FINGERPRINT_WORKERS)`(預設 4)平行呼叫 `build_video_print`。實測 cv2 的解碼呼叫確實會釋放 GIL,執行緒能拿到真實加速(不需要換成多程序,省掉 IPC/pickle 的複雜度)。worker 數刻意保守——傳統硬碟上開太多平行 seek 反而會互相拖累,4 是加速與 HDD 友善之間的折衷,沒有做成 UI 可調選項。取消時呼叫 `executor.shutdown(wait=False, cancel_futures=True)` 取消還沒開始跑的工作。副作用:`videos` 清單順序不再等於目錄走訪順序(誰先解完誰先進清單),不影響階段 2 分群正確性,只是同一組裡「哪個檔案是錨點/顯示在前面」不再固定。
  - **階段 2 分群演算法**:跟圖片那邊同一個理由,一樣改用**錨點分群**(不是 Union-Find):依序把還沒分組的影片當錨點,只跟它比對,已分組的影片直接跳過(不會再被拿去測試),省下不少昂貴的位移投票/精修運算。影片理論上一樣有 Union-Find 遞移合併的長鏈風險(雖然因為要求真的有時間軸對齊,實務上機率比圖片低很多),為了一致性與正確性保證一併修掉。
  - **浮水印寬容度**:算指紋前(僅限影片路徑:`_sample_window`、`build_video_print`)先把畫面四邊各裁掉 `WATERMARK_CROP_MARGIN`(預設 0.10,即留中間 80%×80%)才 resize 成 9×8。**刻意不影響 `image_dhash`(圖片路徑)**——`_dhash_from_gray` 的 `crop_margin` 參數預設 0.0,圖片呼叫完全不傳這個參數,維持原行為。這只能緩解「浮水印在角落/邊緣」的常見情況,對滿版/置中的大浮水印沒有幫助,是提高抓到機率而非保證。
  - **音軌指紋:目前沒有做,而且已知這個 opencv-python 打包版本的音訊 API 不可靠**。`scripts/probe_audio.py`(開發期診斷用,不進打包)可以驗證:實測 `cv2.VideoCapture` 開檔時帶 `CAP_PROP_AUDIO_STREAM` 參數會直接開檔失敗("unsupported parameters"),退回「先正常開檔、再 `.set(CAP_PROP_AUDIO_STREAM, ...)`」也一樣拿不到音訊(`CAP_PROP_AUDIO_TOTAL_STREAMS` 回傳 `-1`,代表這個屬性根本不支援,不是「這部影片沒音軌」)。也就是說**這條路徑要做音軌指紋,得重新考慮外掛 ffmpeg.exe 或裝 PyAV 這類額外依賴**——不要在沒有先跑過 `probe_audio.py` 確認可行之前,就動手寫音訊取樣/雜湊/比對的正式管線。
  - **開發環境的終端機雜訊**:掃到解不開的圖片/影片(常見於 `C:\Windows\Web`、`SystemApps` 這類系統資源檔,不嚴格遵守 PNG/GIF 規格)時,OpenCV/libpng 會直接把警告寫到 stderr,不是 Python 例外——`image_dhash`/`build_video_print` 的 try/except 本來就正常跳過這些檔案,不影響掃描結果。`cleaner/similarity.py` 模組載入時呼叫 `cv2.utils.logging.setLogLevel(LOG_LEVEL_SILENT)` 可以壓掉 OpenCV 自己印的 `[ERROR:...]`,但**壓不掉 `libpng warning: ...`**(libpng 是另一個函式庫,沒有開放 API 可以關掉它的警告輸出)。打包後的 exe 是 `console=False`,兩種輸出使用者都看不到,純粹是開發時的雜訊,不代表功能有問題。
- 設定列:類型 chips(圖片/影片,**單選**)+ **依類型分開的相似程度下拉**(`IMAGE_STRICTNESS_OPTIONS` / `VIDEO_STRICTNESS_OPTIONS`,兩個 `QComboBox` 都建好、依目前選的類型互相切換顯示/隱藏,而不是共用一份選項——圖片跟影片的「嚴格」在技術上是不同的兩組數字,分開才不會誤導)+ 磁碟/資料夾範圍(見 §8.7 註)。標籤直接把數字寫進文字裡(例如「標準(64 bit 指紋最多容許 10 bit 不同)」),不要只寫「標準」兩個字讓使用者猜。
  - 圖片三檔(寬鬆/標準/嚴格)只調一個數字:兩張圖 64-bit dHash 的 Hamming 距離門檻(14/10/6)。
  - 影片三檔調**兩個**數字:每幀 Hamming 門檻(14/10/6,決定「這一幀算不算同一畫面」)**與** `min_match_seconds`(12/20/30 秒,決定「連續相似要多久才不算巧合」)——**這兩個以前是分開的,`SimilarityWorker` 一度只把嚴格程度接到 `min_match_seconds`,每幀門檻被寫死在 `VIDEO_FRAME_THRESHOLD=10` 不受 UI 影響,已經修掉**:`SimilarityWorker.__init__` 的 `threshold` 參數現在對圖片模式是傳給 `find_similar_images(threshold=...)`,對影片模式是傳給 `find_similar_videos(frame_threshold=...)`,同一個參數依模式路由到不同語意,不要誤會成兩種模式共用同一組數字的意思。
- 結果 UI 沿用 §8.3 骨架:QTreeWidget 分群 + `QPixmap` 縮圖預覽(影片只顯示中繼資料)+ checkbox + 「保留最舊/最新」+ 防呆「每組至少保留一個」+ `send2trash` 刪除。影片群另把相似片段區間掛成不可勾選的說明列(`↳ A 00:32–04:18 ≈ B 01:05–04:51`)。
- **資料夾對資料夾模式**(`find_similar_videos(group_b=...)`,UI 的「比對範圍」chips):`targets` 當群組 A、`group_b` 當群組 B,只保留跨群組配對(A 內部、B 內部都不比)。**只支援影片**——圖片路徑有「相同雜湊先摺疊成桶」的優化(桶內成員可能同時混著兩組),疊加跨群組過濾會明顯複雜化,而圖片比對本來就不是效能痛點,故 UI 在這個範圍下把類型鎖定成影片。兩組資料夾重疊/巢狀時,同一個檔案以先蒐集到的 A 為準(`path_group` dict 去重),否則它會被算兩次指紋、跟自己配成一組假重複。`group_b=None`(預設)完全不影響原本的「全部互比」。注意這只是縮小比對範圍,**不是**演算法優化:效益上限是 `a×b ≤ (a+b)²/4`,兩邊均分時只省一半。

#### 效能:這個模組的成本結構(改之前先看這裡,別憑直覺猜)

使用者實測 3,400 部影片要跑一整天。**實際量測後,時間幾乎全部在「精修解碼」,不在演算法本身**,以下數字都是量出來的(3400 部 = 578 萬對):

| 項目 | 成本 | 說明 |
|---|---|---|
| 階段 1 指紋 | ~85 分鐘 | 每部最多 300 次 seek;**HDD 上主導因素是 seek 次數,不是解碼** |
| 階段 2 全部 `match_matrix` | ~0.6 小時 | 0.35ms/對 × 578 萬,純 numpy |
| 階段 2 `_local_align` DP | 7.1ms/次 | 比 match_matrix 貴 20 倍 |
| 階段 2 `_refine_match` | **~14 秒/次(HDD)** | 1200 次 seek。**只要 0.1% 的配對誤觸發就是 22 小時** |

- **指紋快取(`print_cache.py`)**:key 是 `(size, mtime_ns, quick_hash)`,**刻意不用路徑**——整理影片庫常常整個資料夾搬移/改名,路徑當 key 會讓那一整批 miss、重解碼一輪,等於沒有快取。內容特徵當 key 則搬到哪裡(甚至跨磁碟)都命中,內容真的改過才失效。`quick_hash` 只讀頭尾各 64KB,作用是把「size+mtime 剛好都一樣」的碰撞壓到可忽略,不是證明兩檔相同(那是 dHash 的事)。DB 在 `%LOCALAPPDATA%\NeonSweep\fingerprint_cache.db`(已列在 `EXCLUDED_DIRS`,掃描不會掃到自己的快取)。
  - **`FP_VERSION` 要跟著改**:取樣邏輯、`WATERMARK_CROP_MARGIN`、dHash 演算法等有**語意**變更時 +1,否則使用者會拿到舊演算法算的指紋。純效能改動(取樣點與雜湊結果不變)不用動。
  - **sqlite 連線只在協調執行緒用**:查快取在丟給 `ThreadPoolExecutor` 之前做完,寫回只在 `as_completed` 迴圈裡做。不要為了「平行查快取」把連線帶進 worker。
  - 存快取在 `min_match_seconds` 門檻**之前**:那是 UI 可調的,指紋本身跟它無關,依它篩選過的快取會讓使用者改嚴格程度後莫名 miss 一整輪。
  - DB 壞掉一律降級成「這次不快取」,絕不讓掃描失敗——快取是純效能優化,不該把功能一起拖下水。
- **`_refine_match` 的候選窗必須收窄到「證據所在範圍」(最重要的一條,別退回去)**:`_estimate_offset` 除了位移與票數,還回傳 `hit_span`——投給勝出位移的那些樣本在 A 時間軸上的時間範圍。呼叫端用它把候選窗從**幾何重疊**夾到**證據範圍 ± min_match_seconds**。
  - 為什麼:兩部 30 分鐘影片在 offset≈0 時,幾何重疊 = 整整 1800 秒,精修會對兩邊各取樣 `VIDEO_REFINE_MAX_SAMPLES`(600)= 1200 次 seek ≈ **14 秒**,而絕大多數走到這裡的配對只是 2 票的巧合(共用片頭、相似轉場),根本不相似。這是「跑一整天」的真正元凶,是設計缺陷而不是程式碼慢。
  - 為什麼不會漏抓:真的有 T 秒重疊時,粗篩會在整段 T 上取到約 `T/bucket` 個樣本、全部投給同一個位移,所以 `hit_span` 本來就涵蓋整段重疊,窗幾乎不變、精度不受影響。只有巧合配對會被夾小。
  - 實測 A/B(兩部 400 秒影片共用 8 秒):800 → 116 次取樣(6.9x),30 分鐘長片約 10x。`scripts/test_similarity.py` 有回歸測試盯著取樣次數上限。
- **`_estimate_offset` 用 `times` 陣列而不是「索引 × interval」**:等距取樣時兩者等價,但指紋 dict 的 `times`/`backend` 欄位是為了讓非等距取樣後端也能共用同一套位移計算(見下方「評估過不做」)。
- **評估過、量測後決定不做的項目(別再提案,除非有新數據)**:
  - **PyAV 關鍵幀取樣**:提案理由是「cv2 每次取樣都要從關鍵幀解碼整串 P/B 幀,這是 CPU 大頭」——**量測後不成立**。cv2 的 seek+decode 已經夠快(640×480 約 8ms/樣本),而且 PyAV 的 `seek(backward=False)`(往後找下一個關鍵幀)在實測的 demuxer 上直接丟 `Operation not permitted`,只能 seek 到 ≤t 的關鍵幀 → **seek 次數仍是 300 次/部,HDD 上完全沒省到**(HDD 的成本就是 seek 次數)。實測:關鍵幀取樣 1.2~1.8x CPU、PyAV 等距取樣反而只有 0.4~0.6x(比 cv2 慢)。外加關鍵幀位置本來就依編碼器設定而異(場景偵測關掉時只有 GOP 邊界對得上,實測 6/24),會引入假陰性。投報率為負。
  - **Multi-index hashing 建倒排索引取代 O(n²)**:門檻 14 依鴿籠原理**強制**切 8-bit band = 只有 256 個桶,但語料是 102 萬幀 → 每桶約 4,000 幀 → 桶內全部互為候選 → **1,463 億候選幀對**(要取代的只有 578 萬影片對,慢 25,000 倍)、索引 7.3GB。MIH 要有效的前提是「項目在 band 空間中稀疏」,這裡剛好相反。而且它針對的 0.6 小時,在精修窗收窄後也已經不是瓶頸了。
  - **`duration` 前置篩選**:會漏掉「短片剪自長片」的合法配對(預告片 vs 完整影片),違反「不引入假陰性」。
  - **numba JIT 加速 DP**:專案跑 Python 3.14,numba 不支援;且退化幀過濾 + `match.sum()` 剪枝之後 DP 呼叫次數已經很少。
  - **階段 2 批次向量化(把同一錨點對多個候選的逐對小 numpy 呼叫合併成一次大呼叫)**:假設是
    「578 萬次小呼叫,成本主要是呼叫開銷」——**量測後推翻**。正確性先用 300 組隨機測資(含
    強制退化幀邊界情況)驗證跟逐對版逐位元相同,併入後 20 個測試全過,但真實規模量測
    (1500 部影片、112 萬對,同使用者實際情境的密度)顯示批次版 **514 秒,原版只要 162 秒
    ——慢了 3.2 倍**,不是變快。原因:每對的成本大頭是真正的陣列運算(300×300 網格的
    XOR+popcount+比較+遮罩+加總),批次化沒有減少總運算量;為了補齊不同長度候選要用 3D
    陣列廣播(記憶體局部性比 2D 差)、多兩次遮罩運算,總成本反而增加。已完整 revert(`git
    diff` 確認乾淨,測試回到 20/20,沒有留下半套程式碼或殘留註解)。**跟 PyAV 關鍵幀、MIH
    倒排索引同一類「先有合理假設、量測後推翻」的案例,別再提案除非有新數據或新的分析角度。**
- **效能儀表**:`cleaner.similarity` 的 logger 會印兩個階段的耗時、實際比對對數、剪枝/DP/精修次數、快取命中率。打包後 `console=False`,所以 `main.py` 把 log 寫到 `%LOCALAPPDATA%\NeonSweep\neonsweep.log`(輪替)——要診斷使用者的效能問題就叫他給這個檔。
- **驗收測試**:`scripts/test_similarity.py`(合成影片,不需外部素材,不依賴 pytest,比照 `scripts/` 其他獨立腳本)。涵蓋黑幕誤判防護、資料夾對資料夾(含重疊資料夾去重)、剪枝不變量、重編碼、長片涵蓋、精修窗上限、快取(命中/搬移/失效/DB 損壞)。**改這個模組前後都跑一次。**

### 8.7 空間視覺化 Treemap(`treemap_page.py` / `treemap.py`)— 純檢視,零刪除,使用者後續要求新增

補足 §8.1 大檔案掃描抓不到「一堆小檔案加起來很肥的資料夾」的盲點,用 WinDirStat 式 treemap 以**面積**呈現佔用。純邏輯層 `treemap.py` 不碰 Qt;Qt 層在 `workers.TreeSizeWorker` + 自訂 `QPainter` 繪圖 widget `TreemapView`。零新依賴。

- `build_size_tree(targets)`:**顯式堆疊後序走訪(不遞迴)** `os.scandir`(安全性比照 `safe_walk`:不進 reparse point、命中 `EXCLUDED_DIRS` 不下探、OSError 跳過),但**保留階層結構**;資料夾 size = 子節點加總(bottom-up)。Node 為 dict `{path,name,size,is_dir,children,aggregate?}`。**刻意不用遞迴**:超深目錄樹(失控程式狂建 `a\a\a\...`)會撞 Python 遞迴上限,`RecursionError` 不是 `OSError`、不會被內層 except 接住,會一路穿出去卡死掃描(safe_walk 早就是 iterative,treemap 一併看齊)。
- `top_children(node, limit)`:只取前 limit 大的子節點,其餘併成「其他 N 項」聚合節點,避免對含數萬檔案的資料夾一次鋪出數萬個小矩形。
- `squarify(nodes, rect)`:Squarified treemap(Bruls et al. 2000),同層依 size 遞迴切割矩形、盡量正方形。**只鋪當前層級一層**,下鑽時對子樹重算,不預先鋪整棵樹。
- `TreemapView`:`paintEvent` 逐塊 `fillRect` + 標籤,顏色複用 `analysis.classify` 的類型分類(資料夾另有專色);左鍵點資料夾**下鑽**、頂部麵包屑回上層、hover 顯示完整路徑 + 大小、右鍵「開啟位置」。**不提供刪除**(純檢視,要刪去 §8.1/§8.3)。
- 記憶體:整碟掃描把整棵樹留在記憶體。主要壓力來自**檔案節點**(數量遠多於資料夾),故建樹時每個資料夾只用有界 min-heap 留前 `FILE_CAP`(200)大的檔案節點、其餘併成「其他 N 個檔案」聚合節點(反正顯示端 `top_children` 也只鋪前 120 大);含數十萬檔案的資料夾記憶體可差一個數量級。資料夾節點不設上限(下鑽需要完整階層)。搭配「指定資料夾範圍」(§8.7 註)進一步縮小。

> **§8 共用:掃描範圍不限整碟(`views/common.py::FolderPicker`)。** dupe/bigfile/treemap/similarity 四個掃描頁都可用磁碟 chips 之外的 `FolderPicker` 指定任意資料夾(含子目錄)當掃描根,清單非空時改掃指定資料夾、否則掃勾選磁碟。底層 `safe_walk` / `build_size_tree` 對「磁碟根」或「任意資料夾路徑」一視同仁,故只是 UI 傳不同的 `targets: list[str]`。注意 bigfile 的 atime 可靠性查表要用**磁碟代號**還原(`targets` 可能是子資料夾),見 `bigfile_page._start_scan`。

---

## 9. 管理員權限(`utils/admin.py`)

```python
import ctypes, sys, os

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def relaunch_as_admin():
    if getattr(sys, "frozen", False):        # PyInstaller 打包後
        exe, params = sys.executable, ""
    else:
        exe, params = sys.executable, f'"{os.path.abspath(sys.argv[0])}"'
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if r > 32:                                # 成功才退出目前實例
        QApplication.quit()
```

- 程式**預設以一般權限啟動**(PyInstaller manifest 維持 asInvoker,**不要**設 uac_admin,否則每次開啟都跳 UAC)。
- 非管理員時:requires_admin 模組在預覽中顯示但 disabled,清理頁右下角提供「以管理員身分重新啟動」按鈕。使用者按 UAC 取消(`ShellExecuteW` 回傳 ≤ 32)時靜默不動作,不要報錯。

---

## 10. 常見出包點(每一條都是真雷,實作時逐條對照)

### 10.1 Qt 執行緒
- Worker 執行緒碰 QWidget = 隨機崩潰。只准 emit signal。
- Signal 每檔案都發 = UI 凍結。**節流:每 200 檔或 100ms 一次。**
- 執行緒收尾用 §6 的樣板(`quit` + `deleteLater`),否則關窗時 `QThread: Destroyed while thread is still running` 崩潰。視窗 `closeEvent` 要先 cancel 並 `thread.wait(2000)`。
- **每個 worker 的 `run()` 最外層一定要 `try/finally`,`finally` 裡保證 emit `finished`(帶預設空結果)**:任何未預期例外若讓 `finished` 不發出,`thread.quit()` 就不會被呼叫 → UI 永遠卡在掃描頁、取消鈕失效,關窗時 `wait(2000)` 逾時後 QThread 在執行中被銷毀而崩潰。有 `error` signal 的 worker(如 `SimilarityWorker`)順便把訊息回報 UI;逐項目的 worker(掃描/清理各模組)則 per-item catch、給 UI 一個標記 error 的空結果後繼續下一個,不讓單一壞項目拖垮整輪。
- **長迴圈的取消要放在真正耗時的內層**,不能只在項目之間:全檔雜湊放進 chunk 讀取迴圈、影片分群的內層 `for j`(每對可能觸發精修解碼)、`_sample_window` 的取樣迴圈都要檢查取消旗標,否則 `wait(2000)` 等不到、關窗崩潰的風險仍在。

### 10.2 QSS 限制
- QSS 沒有 `box-shadow`、`text-shadow`、`transition`。發光只能用 `QGraphicsDropShadowEffect`,動畫用 `QPropertyAnimation`。
- 一個 graphics effect 實例只能掛一個 widget,共用會靜默失效。
- `QProgressBar::chunk` 可以吃 qlineargradient,直接用。

### 10.3 junction / symlink(最重要的一條)
Windows 目錄樹充滿 junction(如 `C:\Users\<user>\AppData\Local\Application Data` 指回上層造成無限迴圈,`Documents and Settings` 指向 `C:\Users`)。跟著走輕則無限遞迴,重則**跨到別的目錄刪錯檔案**。

Python 的 `os.path.islink()` 對 junction 回傳 False(它只認 symlink),所以必須查 reparse point 屬性:

```python
import os, stat

def is_reparse_point(entry: os.DirEntry) -> bool:
    try:
        st = entry.stat(follow_symlinks=False)
        return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True   # 讀不到就當作危險,跳過

def safe_walk(root: str, on_error=None):
    """唯一允許的遍歷器:不進入 reparse point,權限錯誤跳過"""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not is_reparse_point(entry):
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            yield entry
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue
```

所有掃描(清理模組 + 三個分析功能)一律經過 `safe_walk`,不准直接用 `os.walk`。

### 10.4 長路徑
node_modules 巢狀路徑常超過 260 字元,直接操作會 `FileNotFoundError`。刪除/stat 前:

```python
def long_path(p: str) -> str:
    p = os.path.abspath(p)
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p
```

`delete_entries` 與雜湊讀檔一律先過 `long_path`。

### 10.5 檔案系統雜項
- `entry.stat()` 也可能丟 OSError(掃描中檔案被別的程序刪了)——所有 stat 都要 try/except。
- 唯讀屬性:刪除前 `os.chmod(path, stat.S_IWRITE)`。
- 空目錄清理由深到淺:對收集到的目錄列表 `sorted(dirs, key=len, reverse=True)` 再逐一 `os.rmdir`。
- `send2trash` 對正在使用的檔案會丟例外,同樣 try/except 記 skipped。

### 10.6 其他
- **改影片指紋演算法就要 `print_cache.FP_VERSION += 1`**(§8.6):取樣邏輯、`WATERMARK_CROP_MARGIN`、dHash 算法只要有**語意**變更,舊快取就不再等價。忘了加版本號不會有任何錯誤訊息——使用者會拿到用舊演算法算的指紋、silently 得到錯的分群結果,而且重灌程式也不會好(快取在 `%LOCALAPPDATA%`,不隨 exe 更新)。這是無聲失敗,測試也抓不到。
- 掃描結果的檔案清單可能數十萬筆,PREVIEW 明細只渲染前 500 筆(§7.3),否則 QTreeWidget 卡死。
- 磁碟剩餘空間對比要在清理**前**先快照 `shutil.disk_usage`。
- 所有 emit 傳 dataclass 時 signal 型別用 `object`,不要讓 Qt 嘗試轉換。
- `main.py` 開頭設 `QApplication.setStyle("Fusion")`,QSS 在各 Windows 版本上表現才一致。

---

## 11. 里程碑與驗收標準

### M1 — 骨架與主題
主視窗 + 側欄換頁 + theme.py 全域 QSS + 氛圍燈條 + 磁碟概覽燈條。
✅ 驗收:純黑底、四頁可切換、燈條有 glow、磁碟容量顯示正確。

### M2 — 三段式清理流程(僅 user_temp + browser_cache + recycle_bin)
狀態機、ScanWorker/CleanWorker、掃描按鈕動畫、預覽勾選、清理進度、報告頁、日誌。
✅ 驗收:完整走完 IDLE→DONE 不凍結 UI;掃描與清理可取消;鎖定檔被跳過且計數正確;日誌檔生成;過程中 UI 無任何 traceback。

### M3 — 補齊清理模組
system_temp、thumbnail_cache、crash_dumps、windows_update、dev_caches + 管理員偵測/重啟。
✅ 驗收:非管理員時 admin 模組正確 disabled;以管理員重啟後可清 Windows\Temp。

### M4 — 分析功能
大檔案(top-100 heap)、重複檔(三階段漏斗 + 防全刪)、開發空間掃描。多硬碟依序掃描。
✅ 驗收:掃整顆 C: 不崩潰、可取消、記憶體不失控;重複檔組無法全勾;junction 不會造成無限迴圈(可用 `mklink /J` 造一個自指 junction 測試)。

### M5 — 打包
PyInstaller `--noconsole --onefile`,manifest 維持 asInvoker,附 icon。
✅ 驗收:打包後的 exe 在乾淨環境可執行,「以管理員重新啟動」在 frozen 模式下正常。

---

## 12. 測試檢查清單(交付前逐項手測)

- [ ] 開著 Chrome 掃描+清理,瀏覽器不崩、跳過數 > 0
- [ ] 掃描中按取消,回到 IDLE,再掃一次正常
- [ ] 清理中按取消,報告顯示部分結果
- [ ] 非管理員:admin 模組 disabled;UAC 按取消不報錯
- [ ] `%TEMP%` 放一個剛建立的檔案 → 不會被刪(24h 年齡過濾)
- [ ] `mklink /J` 自指 junction → 掃描不無限迴圈
- [ ] 超長路徑(>260 字)的 node_modules 可正常計算大小與刪除
- [ ] 重複檔案頁:一組全勾時刪除鈕 disabled
- [ ] 大檔案頁:ComfyUI `models\loras` 下的 `.safetensors` 正確標為「AI 模型 / LoRA」
- [ ] 大檔案頁:非系統碟顯示 atime 警告橫幅(預設登錄檔值 0x80000002 時)
- [ ] 重複檔案頁:複製一個 mp4 產生 `xxx (2).mp4` → 掃描抓到且 (2) 版被自動預勾
- [ ] 重複檔案頁:點選圖片檔顯示縮圖預覽,點選影片檔不嘗試解碼
- [ ] 路徑守衛:單元測試餵一個 `allowed_roots` 之外的路徑給 `delete_entries` → 拒刪且記入 errors
- [ ] 刪除前驗證:掃描後改動某檔案內容再執行刪除 → 該檔被拒刪並回報
- [ ] 清理完成後磁碟燈條數字有更新
- [ ] 視窗在清理中直接關閉 → 執行緒正常收尾,無崩潰訊息
- [ ] 掃描範圍:dupe/bigfile/treemap/similarity 加一個資料夾後只掃該資料夾樹,不加則掃勾選磁碟
- [ ] 空間圖:掃一個資料夾 → 方塊面積比例正確、點資料夾可下鑽、麵包屑可回上層、hover 顯示路徑
- [ ] 相似圖片:同一張圖的原圖 + 縮放版 + 重壓縮版被歸為同一群;不相干圖片不誤入
- [ ] 相似影片:同一部影片不同解析度/FPS + 剪掉頭尾的版本被歸為同一群,並顯示相似片段區間
- [ ] 相似影片:長片(超過 base_interval×max_samples,預設 300 秒)剪出中後段的短片,仍能被偵測到(驗證粗篩+位移投票有涵蓋全片,不是只有前 5 分鐘)
- [ ] 相似檔案頁:每組全勾時刪除鈕 disabled(沿用重複檔案頁防呆)
- [ ] 打包後 exe:相似偵測頁能實際讀圖/解影片(確認 cv2 有打包進 onefile)
- [ ] 相似影片:大量影片時階段 1 進度條有跳真實百分比(不再是跑馬燈),平行解碼沒有讓分群結果錯亂
- [ ] 相似影片:角落有色塊/小浮水印的畫面,裁邊後 Hamming 距離應該變小(不會變大)
- [ ] 相似圖片:掃一個有大量平滑漸層桌布(如 `C:\Windows\Web`)的資料夾,不應該再出現單一組吃下上萬張、組內圖片明顯長不一樣的情況
- [ ] 相似影片:`python scripts/test_similarity.py` 全綠(合成影片,不需外部素材;改這個模組前後都要跑)
- [ ] 相似影片:同一個資料夾連掃兩次,第二次階段 1 幾乎瞬間完成(指紋快取命中);把資料夾整個改名後再掃,仍然命中(key 是內容特徵不是路徑,見 §8.6)
- [ ] 相似影片:大型影片庫掃完後,`%LOCALAPPDATA%\NeonSweep\neonsweep.log` 有兩個階段的耗時與精修次數——**回報效能問題時要附這個檔**
- [ ] 相似影片(cross):選「資料夾對資料夾」時類型鎖成影片、A 或 B 沒選資料夾會擋下來;A 內部的重複不出現在結果裡

---

## 13. 磁碟健康診斷(SMART,加分功能 5)

`cleaner/smart_health.py`(純邏輯,Qt-free)+ `cleaner/workers.py` 的 `SmartHealthWorker` +
`cleaner/views/health_page.py`。跟 §9 的系統空間診斷同一種精神:**唯讀,不提供任何修復
功能**——健康狀態異常時只顯示「良好/異常/未知」跟關鍵指標,使用者應自行備份資料並更換
硬碟,這套工具不會、也不該代為處理。

### 13.1 第三方依賴:smartmontools

磁區壞軌/健康值是硬碟韌體層級的 S.M.A.R.T. 資料,不同廠牌/控制器的相容性資料庫太龐大,
不值得也不該自己重寫解析邏輯(等於重寫 smartmontools 十幾年累積的裝置相容性資料庫)。
所以底層直接呼叫開源工具 **smartmontools**(GPLv2,<https://www.smartmontools.org>)的
`smartctl.exe`,以獨立子行程呼叫、不修改、不重新散布原始碼——這是 GPL 認可的
「單純聚合(mere aggregation)」用法,不會傳染到本專案的授權。

需要的檔案(**不隨 git 版控**,見 `.gitignore`):

```
third_party/smartmontools/
├── smartctl.exe
├── drivedb.h
└── LICENSE.txt        ← 打包散布時務必附上這份 GPLv2 授權聲明
```

取得方式:自行到官方網站下載 Windows 版本,解壓縮後把 `smartctl.exe`、`drivedb.h`
放進上述資料夾;`LICENSE.txt` 從壓縮包內附的授權檔複製過來。放置細節見
`third_party/smartmontools/PLACE_FILES_HERE.txt`。

路徑解析用 `utils/fs.py` 既有的 `app_root()`(開發模式回傳專案根目錄,frozen 模式回傳
`sys._MEIPASS`),跟 `icon_path()` 是同一套機制。

**踩過的雷,別再犯**:`NeonSweep.spec` 的 `datas` 一開始寫成
`('third_party/smartmontools', 'third_party/smartmontools')`——直接指一整個資料夾路徑。
**實測證實這樣不會遞迴展開資料夾內容**,打包出來的 onefile exe 解壓後 `_MEIPASS` 裡完全
沒有這個資料夾,`smartctl.exe` 從來沒有真的被包進去過。這個 bug 曾一度誤導成「防毒軟體
對內嵌的原生磁碟工具做深度掃描,導致啟動變慢」的錯誤方向,浪費了不少排查時間——當時
觀察到的啟動延遲確實是防毒軟體造成的(用「關掉防毒 vs 開著防毒」連續啟動計時驗證過,
差異非常明顯),但延遲的觸發原因不會是 `smartctl.exe`,因為它根本不在封裝檔裡;比較
可能是防毒軟體對「未簽章、剛建置出來的大型 onefile exe」本身的一般性反應。

現在改成在 `smart_health.py` 上方的 `_SMARTCTL_FILES` 清單裡**逐一列出檔名**,用
`(source_file, dest_dir)` 的個別檔案 tuple 加進 `datas`——這是官方文件明確支援、不會
有歧義的寫法。驗證方式:打包後不要只看 build log,**要實際執行一次、趁程序還活著時去看
`%TEMP%\_MEI*\third_party\smartmontools\` 底下有沒有東西**,這是唯一可信的驗證方法
(對編譯後的封裝檔做 grep 找字串不可靠——UPX 壓縮跟 CArchive 內部的 zlib 壓縮都會讓
明碼字串消失,即使檔案真的有包進去也一樣搜不到,這個誤判也發生過一次)。

### 13.2 行為

- `smart_health.is_available()`:偵測 `smartctl.exe` 是否存在。不存在時 `HealthPage` 顯示
  下載提示,「開始診斷」按鈕停用,不影響軟體其他功能。
- `scan_devices()`:呼叫 `smartctl --scan-open -j` 列出所有偵測到的實體磁碟。
- `query_health(device, dev_type)`:呼叫 `smartctl -a -j <device>` 取得 JSON,依磁碟類型
  抽取關鍵指標——SATA/HDD 看 SMART attribute ID 5(已重新對應磁區)/197(待處理磁區)/
  198(無法修正錯誤數);NVMe 看 `percentage_used`(耗損百分比)/`media_errors`。查詢失敗
  (權限不足、裝置忙碌、控制器不支援)一律回傳 `None`,UI 顯示「未知」而非誤導使用者。
- `query_health_text(device, dev_type)`:回傳 `smartctl -a` 純文字報告,供「詳細報告」
  對話框顯示完整原始輸出。
- 讀取實體磁碟(`\\.\PhysicalDriveN`)在多數環境下需要系統管理員權限;非管理員模式下
  查詢失敗時,`HealthPage` 會提示「可能需要以系統管理員身分重新啟動」,不會假裝成功。

### 13.3 刻意不做的項目

- 不提供「修復壞軌」「隔離壞磁區」等功能——這些操作風險極高,不是這套工具的定位。
- 不自己解析原始 ATA/NVMe SMART 位元組(不透過 ctypes 直接發 IOCTL)——相容性資料庫
  太龐大,交給 smartmontools 處理。
- 不因為健康狀態異常就自動跳出「請立刻更換硬碟」之類的強烈警語彈窗——只在頁面上如實
  顯示數字與狀態,判斷交給使用者。

### 13.4a 相似影片效能優化進行中(2026-07-17,對照 §8.6)

使用者硬碟實測環境:約 3,400 部影片、800GB、小至數百 KB(3 秒)大至 3~4GB(2 小時)。
§8.6 的「評估過不做」清單依然有效,別重提那四項。下面是根據 §8.6 量測數字規劃的優化,
**進度追蹤用,做完一項就更新這裡,未完成項目照抄下方待辦不要憑印象重寫**:

- [x] **短片提前跳過解碼**:`build_video_print` 加 `min_duration` 參數,時長不足時在算完
  duration(免費的 metadata 讀取)後、進取樣迴圈(貴的 seek+decode)前就回傳 None。
  `find_similar_videos` 呼叫時傳 `VIDEO_MIN_CACHEABLE_DURATION`(2.0 秒)這個**固定常數**,
  刻意不用呼叫端這次的 `min_match_seconds`——後者是 UI 可調的,若拿它當跳過標準,使用者
  改嚴格程度後,4~20 秒的影片會發現快取沒收它們、要重新解碼,等於重新引入 §8.6 已經修過的
  「存快取要在 min_match_seconds 門檻之前」那個 bug。`VIDEO_MIN_CACHEABLE_DURATION` 必須
  維持 `<= views/similarity_page.py::VIDEO_STRICTNESS_OPTIONS` 裡最寬鬆檔位的
  `min_match_seconds`(目前「非常寬鬆」= 4 秒),兩處改動要一起看。測試:20/20 全綠
  (`scripts/test_similarity.py` 沒有專門新增案例,因為既有的短片/精修測試都間接覆蓋到
  「太短的候選被跳過」這條路徑,行為不變)。

- [x] **影片相同指紋快速通道**:實作方式跟原規劃(§階段 1 前先分桶)不同,改成更簡單、
  風險更低的版本——**不預先分桶**,在階段 2 的逐對比對迴圈裡,`both_fine`/位移投票之前
  先插一個 `np.array_equal(va["hashes"], vb["hashes"])` 短路檢查:陣列逐 bit 相同(常見於
  同內容不同容器重新封裝)代表 Hamming 距離處處為 0,直接判定整段相符,不必再跑
  `_match_matrix`/`_local_align`(DP,§8.6 量測過 7.1ms/次)或位移投票。**注意實際省下的量
  比原規劃描述的小**:既有的錨點分群本來就只需要 anchor 對每個候選各比對一次(不是每對
  互比),所以這個優化省的是「單次比對裡的 DP/位移投票運算」,不是省比對次數本身——重複
  越多、省得越多,但不是原本設想的量級。正確性:陣列相同 ⇒ 距離必為 0,不會有假陽性;
  `min_match_seconds` 門檻一樣照套用。新增測試 `test_exact_duplicate_fastpath`(同內容、
  不同容器/編碼參數的兩部影片仍歸同組,相似片段涵蓋全片)。22/22 全綠。

- [x]（否決,不要重做)**階段 2 比對批次向量化**:另一個 session 已經實作+量測+否決,細節
  併入 §8.6「評估過、量測後決定不做的項目」——真實規模(1500 部、112 萬對)下批次版
  514 秒比原版 162 秒慢 3.2 倍(成本大頭是陣列運算本身,不是呼叫開銷),已完整 revert。

- [ ] **配對結論快取**:`print_cache.py` 新增一張表,key = `(內容key_A, 內容key_B,
  frame_threshold, min_match_seconds, FP_VERSION)` 排序後組合,value = 不相似 / 相似+
  片段區間。庫沒變、參數沒變時,重掃可以跳過整個階段 2(目前每次重掃仍要重算全部 n² 對)。
  這是效益最大但改動面最大的一項(要動 `print_cache.py` schema + `find_similar_videos`
  比對迴圈兩處)。**未開始,今晚時間不夠沒動工。**

- [ ] **短片循序解碼**:時長 <= `base_interval × max_samples`(即間隔還沒被放寬,目前
  300 秒)的影片,`build_video_print` 目前仍用 `cap.set(POS_MSEC)` 隨機 seek 取樣;§8.6
  量測過 HDD 上階段 1 的主導成本就是 seek 次數。提案是改成從頭循序 `grab()`/`retrieve()`
  到尾,只在取樣時間點取幀——同一批取樣點、同一套 dHash,結果應逐 bit 相同(不用動
  `FP_VERSION`),純粹換 I/O 模式。**未開始,而且效益需要在使用者的真實 HDD 上 A/B 才知道
  (`NEONSWEEP_FP_WORKERS` 環境變數已經有,可以先測 worker 數本身的甜蜜點)。**

### 13.4 啟動畫面(`splash.png` / `NeonSweep.spec` 的 `Splash()`)

onefile 打包的 exe,不管有沒有內嵌 smartctl.exe,本身就會因為要在啟動時解壓縮而有幾秒
延遲(遇到防毒軟體對未簽章執行檔的一般性檢查時,延遲可能更明顯)。加這個純粹是為了讓
使用者在等待期間知道「程式正在啟動、不是點了沒反應」,跟磁碟健康本身沒有直接關係。

- `scripts/gen_splash.py`:用 PIL 產生 480×280 的 `splash.png`(黑底、粉藍漸層圓環 +
  「NeonSweep」標題 + 副標題),風格對齊 `scripts/gen_icon.py` 的圖示配色。只在需要重新
  產生圖片時手動執行。
- `NeonSweep.spec` 用 PyInstaller 的 `Splash()` 機制:這張圖會在 bootloader 解壓縮階段
  就顯示(比 Python 直譯器啟動還早),`text_pos`/`text_color`/`text_default` 疊一行「啟動
  中…」文字。**這個機制內部依賴 Tcl/Tk**,所以 `excludes` 不能再排除 `tkinter`,即使
  本專案完全不 import 它。
- `main.py` 在 `MainWindow` 顯示後呼叫 `pyi_splash.close()` 關掉啟動畫面。`pyi_splash`
  只在 frozen + 有搭配 `Splash()` 打包時才存在,開發模式下用 `try/except ImportError`
  吞掉,不影響 `python main.py` 直接執行。
- 這只能讓使用者知道「正在啟動、沒當掉」,不會讓啟動真的變快;如果延遲來源是防毒軟體
  攔截在「exe 連第一行程式碼都還沒被允許執行」的層級,連 Splash 都不會比它更早顯示。
  這種情況下沒有應用層級的解法,只能考慮日後幫 exe 做程式碼簽章。
