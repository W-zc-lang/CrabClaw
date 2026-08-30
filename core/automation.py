"""日常办公自动化 —— 纯标准库实现，所有操作先预览后执行。

提供三类能力：
1. 按类型归档：把目录下文件按扩展名分到「图片/文档/表格/...」子文件夹。
2. 批量重命名：序号模式（前缀+三位序号）或查找替换模式。
3. 调用 file-converter：探测到本机 file-converter 项目时，用它做格式转换。

所有「写操作」前都由前端展示预览，用户确认后才会真正执行，
不会静默删除或覆盖文件（重命名前会校验目标是否已存在）。
"""

from __future__ import annotations

import os
import shutil
from typing import Any

# 常见分类（扩展名 → 类别）。命中不了的归为「其他」，不移动。
CATEGORY_MAP: dict[str, set[str]] = {
    "图片": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".tiff", ".heic"},
    "文档": {".doc", ".docx", ".pdf", ".txt", ".md", ".rtf", ".odt", ".epub"},
    "表格": {".xls", ".xlsx", ".csv"},
    "演示": {".ppt", ".pptx"},
    "压缩包": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"},
    "音频": {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"},
    "视频": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv"},
    "代码": {".py", ".js", ".ts", ".java", ".go", ".cpp", ".c", ".rs", ".html", ".htm", ".css", ".json"},
    "安装包": {".exe", ".msi", ".dmg", ".apk", ".iso"},
}


def _category_of(ext: str) -> str:
    for cat, exts in CATEGORY_MAP.items():
        if ext in exts:
            return cat
    return "其他"


def scan_dir(path: str) -> dict[str, Any]:
    if not os.path.isdir(path):
        return {"ok": False, "error": f"目录不存在：{path}"}
    files: list[dict[str, Any]] = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            ext = os.path.splitext(name)[1].lower()
            files.append(
                {"name": name, "ext": ext or "(无扩展名)", "size": os.path.getsize(full)}
            )
    by_ext: dict[str, int] = {}
    for f in files:
        by_ext[f["ext"]] = by_ext.get(f["ext"], 0) + 1
    return {"ok": True, "path": path, "count": len(files), "files": files, "by_ext": by_ext}


def plan_archive(path: str) -> dict[str, Any]:
    sc = scan_dir(path)
    if not sc.get("ok"):
        return sc
    moves: list[dict[str, Any]] = []
    for f in sc["files"]:
        cat = _category_of(f["ext"])
        if cat == "其他":
            continue  # 其他类保持原位
        dst_dir = os.path.join(path, cat)
        moves.append(
            {"src": os.path.join(path, f["name"]), "dst_dir": dst_dir, "name": f["name"]}
        )
    return {"ok": True, "path": path, "moves": moves, "count": len(moves)}


def plan_rename(path: str, mode: str, **kw: Any) -> dict[str, Any]:
    sc = scan_dir(path)
    if not sc.get("ok"):
        return sc
    renames: list[dict[str, Any]] = []
    if mode == "sequence":
        prefix = kw.get("prefix", "file")
        ext_filter = (kw.get("ext") or "").lower()
        start = int(kw.get("start", 1) or 1)
        idx = start
        for f in sc["files"]:
            if ext_filter and f["ext"] != ext_filter:
                continue
            ext = os.path.splitext(f["name"])[1]
            new_name = f"{prefix}_{idx:03d}{ext}"
            idx += 1
            renames.append(
                {"src": os.path.join(path, f["name"]), "dst": os.path.join(path, new_name)}
            )
    elif mode == "replace":
        old = kw.get("old", "")
        new = kw.get("new", "")
        if not old:
            return {"ok": False, "error": "查找内容为空"}
        for f in sc["files"]:
            if old in f["name"]:
                new_name = f["name"].replace(old, new)
                renames.append(
                    {"src": os.path.join(path, f["name"]), "dst": os.path.join(path, new_name)}
                )
    else:
        return {"ok": False, "error": f"未知重命名模式：{mode}"}
    return {"ok": True, "path": path, "renames": renames, "count": len(renames)}


def apply_moves(path: str, moves: list[dict[str, Any]]) -> dict[str, Any]:
    done = 0
    errors: list[str] = []
    for m in moves:
        try:
            os.makedirs(m["dst_dir"], exist_ok=True)
            shutil.move(m["src"], os.path.join(m["dst_dir"], m["name"]))
            done += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{m['name']}: {exc}")
    return {"ok": True, "done": done, "errors": errors}


def apply_renames(path: str, renames: list[dict[str, Any]]) -> dict[str, Any]:
    # 先校验目标冲突，避免半途覆盖
    for r in renames:
        if os.path.exists(r["dst"]) and os.path.abspath(r["src"]) != os.path.abspath(r["dst"]):
            return {"ok": False, "errors": [f"目标已存在：{os.path.basename(r['dst'])}"]}
    done = 0
    errors: list[str] = []
    for r in renames:
        try:
            os.rename(r["src"], r["dst"])
            done += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{os.path.basename(r['src'])}: {exc}")
    return {"ok": True, "done": done, "errors": errors}


def find_fileconverter() -> str | None:
    """探测常见的 file-converter 项目位置。"""
    candidates = [
        os.path.expanduser("~/WorkBuddy/file-converter"),
        "C:/Users/win/WorkBuddy/file-converter",
        os.path.join(os.getcwd(), "file-converter"),
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return None
