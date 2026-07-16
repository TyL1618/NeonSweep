"""空間視覺化(treemap)的純邏輯層:遞迴建立「資料夾大小樹」+ squarified treemap 版面配置。
不碰任何 Qt 物件(比照 analysis.py 慣例),方便獨立測試;Qt 層(workers.TreeSizeWorker /
views.treemap_page)只負責把這裡的結果包進執行緒並繪圖。

與大檔案掃描器(analysis.scan_top_files)互補:大檔案抓「單一巨大檔案」,treemap 抓
「一堆小檔案加起來很肥的資料夾」。
"""

import heapq
import os
import time

from .analysis import EXCLUDED_DIRS, PROGRESS_INTERVAL, PROGRESS_TIME_INTERVAL
from .utils.fs import is_excluded_dir, is_reparse_point, split_excludes

# Node 結構(dict):
#   path      完整路徑(aggregate 聚合節點為空字串)
#   name      顯示名稱(basename)
#   size      位元組數(資料夾 = 子節點加總)
#   is_dir    是否為資料夾(可下鑽)
#   children  list[Node](資料夾)或 None(檔案 / 聚合節點)
#   aggregate 選填,True 表示「其他 N 項」聚合節點,不可下鑽

# 每個資料夾最多保留幾個「檔案」子節點,其餘併成一個「其他 N 個檔案」聚合節點。含數萬~數十萬
# 檔案的資料夾若把每個檔案都建成節點,整碟掃描的樹會吃掉大量記憶體;反正顯示端 top_children
# 也只鋪前 120 大,建樹時就用有界 min-heap 只留前 FILE_CAP 大的檔案節點,記憶體可差一個數量級。
# 資料夾節點不設上限(數量遠少於檔案,且下鑽需要完整階層)。
FILE_CAP = 200


def _dir_node(path: str) -> dict:
    return {
        "path": path,
        "name": os.path.basename(path.rstrip("\\/")) or path,
        "size": 0,
        "is_dir": True,
        "children": [],
    }


def build_size_tree(targets: list[str], progress_cb=None, cancel_check=None):
    """對每個 target(磁碟根目錄或任意資料夾)建立大小樹(iterative,不遞迴)。
    多個 target 時包一個虛擬根;單一 target 直接回傳該節點。取消時回傳 None。
    progress_cb(scanned_count, current_path)。
    """
    counter = {"n": 0, "last": 0.0}
    ex_prefixes, ex_names = split_excludes(EXCLUDED_DIRS)
    children = []
    for t in targets:
        if cancel_check and cancel_check():
            return None
        node = _scan_dir(t, progress_cb, cancel_check, counter, ex_prefixes, ex_names)
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


def _new_frame(path: str) -> dict:
    return {"node": _dir_node(path), "subdirs": None, "sidx": 0,
            "files_total": 0, "heap": [], "rest_n": 0, "rest_size": 0}


def _scan_dir(root_path, progress_cb, cancel_check, counter, ex_prefixes, ex_names):
    """走訪單一目錄樹,回傳資料夾節點。改用顯式堆疊做後序走訪(不遞迴)——安全性比照
    fs.safe_walk:不進 reparse point、命中排除目錄不下探、權限/OSError 跳過;同時避免深層目錄樹
    觸發 Python 遞迴上限(RecursionError,舊版遞迴實作會炸,而且它不是 OSError、不會被內層
    except 接住,會一路穿出去卡死掃描)。

    每個 frame 記錄:node、待處理子目錄清單與索引、檔案累計(有界 top-FILE_CAP 的 min-heap +
    其餘檔案的計數/總大小)。子目錄整棵處理完(後序)才把檔案子節點與大小併進 node、附到父節點。
    """
    root_frame = _new_frame(root_path)
    stack = [root_frame]
    seq = 0  # heap tie-breaker,避免元素比較落到 name/path 字串上

    while stack:
        if cancel_check and cancel_check():
            return None
        frame = stack[-1]
        node = frame["node"]

        # 第一次碰到這個 frame:scandir 一次,累計檔案、收集子目錄路徑。
        if frame["subdirs"] is None:
            subdirs: list[str] = []
            try:
                with os.scandir(node["path"]) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if is_reparse_point(entry) or is_excluded_dir(
                                    entry.path, entry.name, ex_prefixes, ex_names
                                ):
                                    continue
                                subdirs.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                try:
                                    sz = entry.stat(follow_symlinks=False).st_size
                                except OSError:
                                    continue
                                frame["files_total"] += sz
                                seq += 1
                                item = (sz, seq, entry.name, entry.path)
                                if len(frame["heap"]) < FILE_CAP:
                                    heapq.heappush(frame["heap"], item)
                                elif sz > frame["heap"][0][0]:
                                    popped = heapq.heapreplace(frame["heap"], item)
                                    frame["rest_n"] += 1
                                    frame["rest_size"] += popped[0]
                                else:
                                    frame["rest_n"] += 1
                                    frame["rest_size"] += sz
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
            frame["subdirs"] = subdirs

        # 還有子目錄沒處理:推一個子 frame,下一輪先把它整棵處理完(後序)。
        if frame["sidx"] < len(frame["subdirs"]):
            child_path = frame["subdirs"][frame["sidx"]]
            frame["sidx"] += 1
            stack.append(_new_frame(child_path))
            continue

        # 子目錄全部處理完:把檔案子節點(heap + 聚合)併入,結算大小,附到父節點。
        for (sz, _seq, nm, p) in frame["heap"]:
            node["children"].append({"path": p, "name": nm, "size": sz, "is_dir": False, "children": None})
        if frame["rest_n"] > 0:
            node["children"].append({
                "path": "", "name": f"其他 {frame['rest_n']} 個檔案", "size": frame["rest_size"],
                "is_dir": False, "children": None, "aggregate": True,
            })
        node["size"] += frame["files_total"]

        stack.pop()
        if stack:
            parent_node = stack[-1]["node"]
            if node["size"] > 0:
                parent_node["children"].append(node)
                parent_node["size"] += node["size"]
        else:
            return node

    return root_frame["node"]


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
