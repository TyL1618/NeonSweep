"""診斷結果的輸出:LINE 可貼的文字摘要、自包含 HTML 報告、可回傳的診斷包(zip)。

**個資遮蔽是這個模組的核心責任**:這個工具會發給朋友在他們的電腦上跑,輸出要傳回給開發者,
所以任何會洩漏身分的欄位(使用者名稱、電腦名稱、IP、MAC、磁碟序號)都要在輸出前處理掉。
做法是在「輸出前」對整份資料做一次遞迴取代,而不是靠各處記得別寫——新增欄位時不會漏。

唯一無法遮蔽的是 .dmp 傾印檔(二進位,可能含記憶體片段),所以預設不附,使用者明確勾選才附。
"""

import copy
import html
import json
import os
import re
import zipfile
from datetime import datetime

from . import bugcheck_db
from .crash_analysis import CATEGORY_LABELS, spec_rows

TOOL_NAME = "NeonSweep 診斷"
MAX_DUMPS = 3
MAX_DUMP_BYTES = 200 * 1024 * 1024

# 事件類資料(來自事件檢視器的自由文字欄位)才套用 IP/MAC 遮蔽;驅動版本號長得像 IP
# (如 10.0.26100.1),套用到全部會誤傷。
_EVENT_KEYS = {"bugchecks", "power", "storage_events", "whea", "gpu_tdr", "memdiag"}
_IPV4_RE = re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])")
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_SERIAL_RE = re.compile(r"(?im)^(\s*(?:Serial Number|LU WWN Device Id|Device Id)\s*:\s*).+$")


class Redactor:
    """把使用者名稱/電腦名稱等個資換成佔位符。以環境變數取得真實值,不寫死任何人的名字。"""

    def __init__(self):
        names = {}
        for env, placeholder in (("USERNAME", "<user>"), ("COMPUTERNAME", "<pc>"),
                                 ("USERDOMAIN", "<pc>")):
            v = os.environ.get(env, "").strip()
            if len(v) >= 2:
                names[v] = placeholder
        profile = os.path.basename(os.environ.get("USERPROFILE", "").rstrip("\\/"))
        if len(profile) >= 2:
            names.setdefault(profile, "<user>")
        # 長的先換,避免短名稱把長名稱切碎
        self._patterns = [
            (re.compile(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", re.IGNORECASE), ph)
            for n, ph in sorted(names.items(), key=lambda kv: -len(kv[0]))
        ]

    def text(self, s: str, events: bool = False) -> str:
        for pat, ph in self._patterns:
            s = pat.sub(ph, s)
        if events:
            s = _IPV4_RE.sub("<ip>", s)
            s = _MAC_RE.sub("<mac>", s)
        return s

    def deep(self, obj, events: bool = False):
        if isinstance(obj, str):
            return self.text(obj, events)
        if isinstance(obj, list):
            return [self.deep(x, events) for x in obj]
        if isinstance(obj, dict):
            return {k: self.deep(v, events or k in _EVENT_KEYS) for k, v in obj.items()}
        return obj


def redact_data(data: dict, analysis: dict) -> tuple[dict, dict]:
    """回傳遮蔽後的 (data, analysis) 副本,原物件不動。SMART 完整報告另外抹掉序號。"""
    r = Redactor()
    d = r.deep(copy.deepcopy(data))
    a = r.deep(copy.deepcopy(analysis))
    smart = d.get("smart") or {}
    smart["reports"] = {
        k: _SERIAL_RE.sub(r"\1<redacted>", v) for k, v in (smart.get("reports") or {}).items()
    }
    # dump 的完整路徑不輸出(只留檔名),避免路徑洩漏
    for f in (d.get("dumps") or {}).get("files", []):
        f.pop("path", None)
    if (d.get("dumps") or {}).get("full_dump"):
        d["dumps"]["full_dump"].pop("path", None)
    return d, a


# ── 文字摘要(貼 LINE 用) ────────────────────────────────────────────────────

_SEV_ICON = {"bad": "🔴", "warn": "🟡", "info": "ℹ️", "ok": "✅"}


def build_summary_text(data: dict, analysis: dict) -> str:
    d, a = redact_data(data, analysis)
    lines = [f"【{TOOL_NAME}摘要】{d.get('collected_at', '')}",
             f"結論:{_SEV_ICON[a['overall']]} {a['headline']}"]
    if a["suspects"]:
        top = "、".join(f"{s['label']}" for s in a["suspects"][:3])
        lines.append(f"最可能方向:{top}")
    lines.append("")
    lines.append("▍電腦規格")
    for label, value in spec_rows(d):
        lines.append(f"{label}:{value.replace(chr(10), ' / ')}")
    lines.append("")
    lines.append("▍診斷發現")
    for f in a["findings"]:
        lines.append(f"{_SEV_ICON[f['severity']]} {f['title']}")
        if f["severity"] in ("bad", "warn") and f["detail"]:
            for ln in f["detail"].splitlines()[:4]:
                lines.append(f"    {ln}")
    bug = d.get("bugchecks", [])
    if bug:
        lines.append("")
        lines.append("▍藍屏紀錄(最近 5 次)")
        for b in bug[:5]:
            lines.append(f"{b['time_local']}  {b['code_hex']} {b['name']}  參數 {', '.join(b['params'][:4])}")
    if d.get("errors"):
        lines.append("")
        lines.append("⚠ 以下項目讀不到:" + ";".join(d["errors"]))
    lines.append("")
    lines.append("(已自動遮蔽使用者名稱與電腦名稱;完整診斷包可另外傳)")
    return "\n".join(lines)


# ── HTML 報告 ────────────────────────────────────────────────────────────────

_CSS = """
:root{--bg:#0b0b12;--panel:#14141f;--text:#e8e8f0;--dim:#8a8a9a;--pink:#ff2e88;--blue:#00e5ff;
--bad:#ff3b5c;--warn:#ffcc33;--ok:#39ffb0;--info:#4da6ff}
@media (prefers-color-scheme:light){:root{--bg:#f6f6fa;--panel:#fff;--text:#1a1a24;--dim:#666}}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 "Microsoft JhengHei UI",sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px;color:var(--blue)}
.dim{color:var(--dim);font-size:13px}
.verdict{border:2px solid;border-radius:12px;padding:16px 20px;margin:16px 0;background:var(--panel)}
.verdict.bad{border-color:var(--bad)}.verdict.warn{border-color:var(--warn)}.verdict.ok{border-color:var(--ok)}
.verdict b{font-size:20px}
.card{background:var(--panel);border-left:4px solid var(--info);border-radius:8px;padding:10px 14px;margin:8px 0}
.card.bad{border-color:var(--bad)}.card.warn{border-color:var(--warn)}.card.ok{border-color:var(--ok)}
.card .t{font-weight:bold}.card pre{margin:6px 0 0;white-space:pre-wrap;font:13px/1.5 Consolas,monospace;color:var(--dim)}
.card ul{margin:6px 0 0;padding-left:20px;font-size:14px}
table{border-collapse:collapse;width:100%;background:var(--panel);border-radius:8px;overflow:hidden}
td,th{padding:6px 12px;text-align:left;vertical-align:top;border-bottom:1px solid rgba(128,128,128,.2);font-size:14px}
th{width:150px;color:var(--dim);font-weight:normal}
code{font-family:Consolas,monospace}
"""


def _esc(s) -> str:
    return html.escape(str(s))


def build_html(data: dict, analysis: dict) -> str:
    d, a = redact_data(data, analysis)
    out = [f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{TOOL_NAME}報告</title><style>{_CSS}</style></head><body><div class='wrap'>",
           f"<h1>{TOOL_NAME}報告</h1><div class='dim'>產生時間 {_esc(d.get('collected_at', ''))}"
           f"　·　個資(使用者名稱、電腦名稱)已自動遮蔽</div>",
           f"<div class='verdict {a['overall']}'><b>{_SEV_ICON[a['overall']]} {_esc(a['headline'])}</b>"]
    if a["suspects"]:
        out.append("<ol>")
        for s in a["suspects"][:3]:
            out.append(f"<li><b>{_esc(s['label'])}</b>"
                       f"<span class='dim'>(證據分 {s['score']})</span><br>"
                       f"<span class='dim'>{_esc('；'.join(s['reasons']))}</span></li>")
        out.append("</ol>")
    out.append("</div>")

    out.append("<h2>電腦規格</h2><table>")
    for label, value in spec_rows(d):
        out.append(f"<tr><th>{_esc(label)}</th><td>{_esc(value).replace(chr(10), '<br>')}</td></tr>")
    out.append("</table>")

    out.append("<h2>診斷發現</h2>")
    for f in a["findings"]:
        out.append(f"<div class='card {f['severity']}'><div class='t'>{_SEV_ICON[f['severity']]} {_esc(f['title'])}</div>")
        if f["detail"]:
            out.append(f"<pre>{_esc(f['detail'])}</pre>")
        if f["advice"]:
            out.append("<ul>" + "".join(f"<li>{_esc(x)}</li>" for x in f["advice"]) + "</ul>")
        out.append("</div>")

    bug = d.get("bugchecks", [])
    if bug:
        out.append("<h2>藍屏紀錄</h2><table><tr><th>時間</th><td><b>停止代碼 / 說明</b></td></tr>")
        for b in bug[:20]:
            info = bugcheck_db.describe(b["code"])
            out.append(f"<tr><th>{_esc(b['time_local'])}</th><td><code>{_esc(b['code_hex'])} {_esc(b['name'])}</code>"
                       f"<br><span class='dim'>{_esc(info['plain'])}</span>"
                       f"<br><span class='dim'>參數:{_esc(', '.join(b['params'][:4]))}</span></td></tr>")
        out.append("</table>")

    dumps = d.get("dumps", {})
    if dumps.get("files"):
        out.append("<h2>傾印檔</h2><table>")
        for f in dumps["files"][:10]:
            out.append(f"<tr><th>{_esc(f['time_local'])}</th><td><code>{_esc(f['name'])}</code> "
                       f"{f['size'] / 1024:.0f} KB {_esc(f.get('code_hex', ''))}</td></tr>")
        out.append("</table>")

    drivers = d.get("system", {}).get("drivers", [])
    if drivers:
        out.append("<h2>驅動版本(顯示/網路/儲存/音效/藍牙,不含微軟內建)</h2><table>")
        for drv in sorted(drivers, key=lambda x: (x.get("cls") or "", x.get("name") or "")):
            if "microsoft" in (drv.get("vendor") or "").lower() or drv.get("date") == "2006-06-21":
                continue
            out.append(f"<tr><th>{_esc(drv.get('cls'))}</th><td>{_esc(drv.get('name'))}<br>"
                       f"<span class='dim'>{_esc(drv.get('version'))} · {_esc(drv.get('date'))} · {_esc(drv.get('vendor'))}</span></td></tr>")
        out.append("</table>")

    if d.get("errors"):
        out.append("<h2>讀不到的項目</h2><ul>" + "".join(f"<li>{_esc(e)}</li>" for e in d["errors"]) + "</ul>")
    out.append("</div></body></html>")
    return "".join(out)


# ── 診斷包 ───────────────────────────────────────────────────────────────────

_README = """這是 NeonSweep 診斷工具產生的診斷包。

summary.txt   文字摘要(可直接貼到聊天軟體)
report.html   完整報告,用瀏覽器開啟
data.json     原始資料(供分析用)
smart/        磁碟 SMART 完整報告(若有)
dumps/        藍屏傾印檔(僅在寄件人勾選附上時才有)

使用者名稱、電腦名稱、IP、MAC、磁碟序號已自動遮蔽。
傾印檔是二進位檔,無法遮蔽,可能含少量當時記憶體內容。
"""


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "device"


def default_bundle_name() -> str:
    return f"NeonSweep-diag-{datetime.now():%Y%m%d-%H%M}.zip"


def export_bundle(zip_path: str, data: dict, analysis: dict, include_dumps: bool = False) -> dict:
    """寫出診斷包。回傳 {"path", "size", "dumps": [檔名...], "dumps_skipped": [(檔名, 原因)...]}。"""
    d, a = redact_data(data, analysis)
    result = {"path": zip_path, "size": 0, "dumps": [], "dumps_skipped": []}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", _README)
        z.writestr("summary.txt", build_summary_text(data, analysis))
        z.writestr("report.html", build_html(data, analysis))
        # smart.reports 是大段文字,獨立成檔較好讀,data.json 裡不重複放
        slim = copy.deepcopy(d)
        reports = (slim.get("smart") or {}).pop("reports", {})
        slim["analysis"] = a
        z.writestr("data.json", json.dumps(slim, ensure_ascii=False, indent=1))
        for dev, text in reports.items():
            z.writestr(f"smart/{_safe_name(dev)}.txt", text)
        if include_dumps:
            # 用原始 data(未遮蔽)才有 path;只取最近 MAX_DUMPS 個
            for f in (data.get("dumps") or {}).get("files", [])[:MAX_DUMPS]:
                try:
                    if f["size"] > MAX_DUMP_BYTES:
                        result["dumps_skipped"].append((f["name"], "檔案過大"))
                        continue
                    z.write(f["path"], f"dumps/{f['name']}")
                    result["dumps"].append(f["name"])
                except OSError as e:
                    result["dumps_skipped"].append((f["name"], f"讀取失敗:{type(e).__name__}"))
    result["size"] = os.path.getsize(zip_path)
    return result
