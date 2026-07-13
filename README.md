# ⚡ NeonSweep

Windows 硬碟垃圾清理工具，PyQt6 Cyberpunk 風格 GUI。掃描 → 預覽 → 清理三段式流程，任何刪除動作前都會先讓你看到「會刪什麼、能清多少」並打勾確認。

## 功能

- **清理**：使用者/系統暫存檔、瀏覽器快取、縮圖快取、錯誤傾印檔、Windows Update 快取、開發者套件快取(pip/npm/yarn/NuGet)、資源回收桶
- **大檔案掃描**：列出最大的檔案,自動標示用途(AI 模型 / 影片 / 遊戲 / 映像壓縮 / 其他)與最後使用時間
- **重複檔案偵測**：依內容雜湊(非檔名)找出位元組完全相同的重複檔案,三階段漏斗加速
- **開發空間掃描**：找出 `node_modules` / `venv` / `.tox` / Rust `target` 等可重新產生的快取
- **安全設計**：白名單制刪除、路徑守衛、不跟隨 junction/symlink、年齡過濾、分析功能一律進回收桶且刪前重新驗證、零自動刪除

## 安裝(開發模式)

```bash
pip install -r requirements.txt
python main.py
```

## 打包成 EXE

```bash
pyinstaller NeonSweep.spec
# 輸出:dist\NeonSweep.exe
```

## 資料位置

| 內容 | 路徑 |
|---|---|
| 清理日誌 | `%LOCALAPPDATA%\NeonSweep\logs\clean_*.log` |

## 技術棧

Python 3.11+、PyQt6、send2trash、ctypes(WinAPI)。不使用 psutil / pywin32。
