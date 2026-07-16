"""空間視覺化(treemap)的純邏輯層:遞迴建立「資料夾大小樹」+ squarified treemap 版面配置。
不碰任何 Qt 物件(比照 analysis.py 慣例),方便獨立測試;Qt 層(workers.TreeSizeWorker /
views.treemap_page)只負責把這裡的結果包進執行緒並繪圖。

與大檔案掃描器(analysis.scan_top_files)互補:大檔案抓「單一巨大檔案」,treemap 抓
「一堆小檔案加起來很肥的資料夾」。
"""

import os
import time

from .analysis import EXCLUDED_DIRS, PROGRESS_INTERVAL, PROGRESS_TIME_INTERVAL
from .utils.fs import is_reparse_point

# Node 結構(dict):
#   path      完整路徑(aggregate 聚合節點為空字串)
#   name      顯示名稱(basename)
#   size      位元組數(資料夾 = 子節點加總)
#   is_dir    是否為資料夾(可下鑽)
#   children  list[Node](資料夾)或 None(檔案 / 聚合節點)
#   aggregate 選填,True 表示「其他 N 項」聚合節點,不可下鑽

_EXCLUDE_LOWER = [os.path.normcase(d) for d in EXCLUDED_DIRS]


def _excluded(path: str) -> bool:
    norm = os.path.normcase(path)
    return any(ex in norm for ex in _EXCLUDE_LOWER)


def build_size_tree(targets: list[str], progress_cb=None, cancel_check=None):
    """對每個 target(磁碟根目錄或任意資料夾)遞迴建立大小樹。
    多個 target 時包一個虛擬根;單一 target 直接回傳該節點。取消時回傳 None。
    progress_cb(scanned_count, current_path)。
    """
    counter = {"n": 0, "last": 0.0}
    children = []
    for t in targets:
        if cancel_check and cancel_check():
            return None
        node = _scan_dir(t, progress_cb, cancel_check, counter)
        if node is not None:
            children.append(node)
    if cancel_check and cancel_check():
        return None
    if not children:
        return None
    if len(children) == 1:
        return children[0]
    return {
        "path": "",
        "name": "掃描結果",
        "size": sum(c["size"] for c in children),
        "is_dir": True,
        "children": children,
    }


def _scan_dir(path: str, progress_cb, cancel_check, counter):
    """遞迴走訪單一目錄,回傳資料夾節點。安全性比照 fs.safe_walk:
    不進 reparse point、命中排除目錄不下探、權限/OSError 跳過。
    """
    children = []
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if cancel_check and cancel_check():
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if is_reparse_point(entry) or _excluded(entry.path):
                            continue
                        child = _scan_dir(entry.path, progress_cb, cancel_check, counter)
                        if child is not None and child["size"] > 0:
                            children.append(child)
                            total += child["size"]
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            sz = entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
                        children.append(
                            {"path": entry.path, "name": entry.name, "size": sz, "is_dir": False, "children": None}
                        )
                        total += sz
                        counter["n"] += 1
                        now = time.monotonic()
                        if counter["n"] % PROGRESS_INTERVAL == 0 or (now - counter["last"]) >= PROGRESS_TIME_INTERVAL:
                            counter["last"] = now
                            if progress_cb:
                                progress_cb(counter["n"], entry.path)
                except OSError:
                    continue
    except (PermissionError, OSError):
        pass
    return {
        "path": path,
        "name": os.path.basename(path.rstrip("\\/")) or path,
        "size": total,
        "is_dir": True,
        "children": children,
    }


def top_children(node, limit: int = 120):
    """回傳某節點底下、依大小排序的前 limit 個子節點;其餘合併成一個「其他 N 項」聚合節點,
    避免對一個含數萬個檔案的資料夾一次鋪出數萬個小矩形(既慢又看不清)。
    """
    kids = node.get("children") or []
    kids = sorted((c for c in kids if c["size"] > 0), key=lambda c: c["size"], reverse=True)
    if len(kids) <= limit:
        return kids
    head = kids[:limit]
    rest = kids[limit:]
    agg = {
        "path": "",
        "name": f"其他 {len(rest)} 項",
        "size": sum(c["size"] for c in rest),
        "is_dir": False,
        "children": None,
        "aggregate": True,
    }
    return head + [agg]


# ----------------------------------------------------------------------
# Squarified treemap 版面配置(Bruls, Huizing, van Wijk 2000)
# ----------------------------------------------------------------------


def squarify(nodes, rect):
    """把 nodes 依 size 比例鋪滿 rect=(x, y, w, h),盡量讓每塊長寬比接近正方形。
    回傳 list[(node, (x, y, w, h))]。size<=0 的節點與過小的矩形會被略過。
    """
    x, y, w, h = rect
    items = [n for n in nodes if n["size"] > 0]
    if not items or w <= 1 or h <= 1:
        return []
    total = sum(n["size"] for n in items)
    if total <= 0:
        return []
    scale = (w * h) / total
    items.sort(key=lambda n: n["size"], reverse=True)
    areas = [n["size"] * scale for n in items]
    return _squarify_layout(areas, items, x, y, w, h)


def _worst(row_areas, length):
    """一列矩形在 strip 長度 length 下的最差長寬比(越接近 1 越好)。"""
    s = sum(row_areas)
    if s <= 0 or length <= 0:
        return float("inf")
    rmax = max(row_areas)
    rmin = min(row_areas)
    return max((length * length * rmax) / (s * s), (s * s) / (length * length * rmin))


def _squarify_layout(areas, nodes, x, y, w, h):
    result = []
    i = 0
    n = len(areas)
    while i < n:
        length = min(w, h)
        row_a = [areas[i]]
        row_n = [nodes[i]]
        j = i + 1
        while j < n:
            if _worst(row_a, length) >= _worst(row_a + [areas[j]], length):
                row_a.append(areas[j])
                row_n.append(nodes[j])
                j += 1
            else:
                break
        row_sum = sum(row_a)
        if w >= h:
            strip_w = row_sum / h if h else 0
            oy = y
            for a, node in zip(row_a, row_n):
                rh = (a / row_sum) * h if row_sum else 0
                result.append((node, (x, oy, strip_w, rh)))
                oy += rh
            x += strip_w
            w -= strip_w
        else:
            strip_h = row_sum / w if w else 0
            ox = x
            for a, node in zip(row_a, row_n):
                rw = (a / row_sum) * w if row_sum else 0
                result.append((node, (ox, y, rw, strip_h)))
                ox += rw
            y += strip_h
            h -= strip_h
        i = j
    return result
