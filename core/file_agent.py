"""可控的本地文件/系统操作代理。

设计原则：
1. 默认关闭，必须由用户在设置里显式开启。
2. 首次开启需阅读免责声明并确认。
3. 所有"写"操作（移动/删除/执行命令）要求 AI 先给出方案，用户确认后再执行。
4. 路径访问限制：禁止访问系统关键目录、禁止跨出用户主目录到系统根目录。
5. 命令执行采用白名单，拒绝任何可能破坏数据或系统的命令。
6. 所有操作记录到日志文件，便于审计。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import data_dir

# 禁止访问的系统关键目录/前缀（大小写不敏感）
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\ProgramData",
    "C:\\$Recycle.Bin",
    "/System",
    "/usr/bin",
    "/usr/sbin",
    "/bin",
    "/sbin",
    "/etc",
    "/dev",
    "/proc",
    "/sys",
)

# 命令白名单：只有这些命令允许执行
ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "dir", "ls", "echo", "type", "cat", "head", "tail", "find", "where", "which",
    "python", "python3", "node", "npm", "git", "gh",
    "code", "notepad", "explorer",
    "tar", "zip", "unzip",
    "tree", "pwd", "cd",
})

# 命令中绝对禁止出现的子串（大小写不敏感）
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "rm -rf", "del /f", "del /q", "del /s", "rmdir /s", "format", "diskpart",
    "reg delete", "reg add", "shutdown", "rd /s", "> nul 2>&1",
    ":(){ :|:& };:", "mkfs", "dd if",
)

# 需要用户确认后才执行的操作
CONFIRM_OPS = {"move_file", "delete_file", "execute_command"}
# 只读操作，无需确认
READONLY_OPS = {"list_dir", "read_file"}


def _home() -> str:
    return os.path.expanduser("~")


def _safe_path_check(path: str | None) -> None:
    """检查路径是否在允许范围内。path 为 None 时跳过（用于初始化日志等）。"""
    if path is None:
        return
    abs_path = os.path.abspath(path)
    for prefix in FORBIDDEN_PREFIXES:
        if abs_path.lower().startswith(prefix.lower()):
            raise PermissionError(f"禁止访问系统目录：{abs_path}")
    home = os.path.abspath(_home())
    # Windows 下允许访问用户主目录及其子目录；Unix 同理
    if not abs_path.lower().startswith(home.lower()):
        raise PermissionError(f"只能访问用户主目录内的路径：{abs_path}")


def _log(message: str) -> None:
    """记录操作日志到数据目录。"""
    log_dir = os.path.join(data_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "file_agent.log")
    line = f"{datetime.now().isoformat()}  {message}\n"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)


def list_dir(path: str) -> dict[str, Any]:
    """列出目录内容。"""
    try:
        _safe_path_check(path)
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            st = os.stat(full)
            entries.append({
                "name": name,
                "path": full,
                "is_dir": os.path.isdir(full),
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        _log(f"list_dir: {path} -> {len(entries)} entries")
        return {"ok": True, "path": path, "entries": entries}
    except Exception as exc:  # noqa: BLE001
        _log(f"list_dir failed: {path} -> {exc}")
        return {"ok": False, "error": str(exc)}


def read_file(path: str, max_lines: int = 50) -> dict[str, Any]:
    """读取文本文件，默认最多 max_lines 行。"""
    try:
        _safe_path_check(path)
        if not os.path.isfile(path):
            return {"ok": False, "error": "不是文件或不存在"}
        lines = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                lines.append(line.rstrip("\n"))
        _log(f"read_file: {path} -> {len(lines)} lines")
        return {"ok": True, "path": path, "lines": lines, "truncated": len(lines) >= max_lines}
    except Exception as exc:  # noqa: BLE001
        _log(f"read_file failed: {path} -> {exc}")
        return {"ok": False, "error": str(exc)}


def move_file(src: str, dst: str) -> dict[str, Any]:
    """移动或重命名文件/目录。"""
    try:
        _safe_path_check(src)
        _safe_path_check(dst)
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        shutil.move(src, dst)
        _log(f"move_file: {src} -> {dst}")
        return {"ok": True, "message": f"已移动：{src} -> {dst}"}
    except Exception as exc:  # noqa: BLE001
        _log(f"move_file failed: {src} -> {dst} -> {exc}")
        return {"ok": False, "error": str(exc)}


def delete_file(path: str) -> dict[str, Any]:
    """删除文件或空目录。"""
    try:
        _safe_path_check(path)
        if os.path.isfile(path):
            os.remove(path)
            _log(f"delete_file: {path}")
            return {"ok": True, "message": f"已删除文件：{path}"}
        if os.path.isdir(path):
            os.rmdir(path)
            _log(f"delete_dir: {path}")
            return {"ok": True, "message": f"已删除空目录：{path}"}
        return {"ok": False, "error": "路径不存在"}
    except Exception as exc:  # noqa: BLE001
        _log(f"delete_file failed: {path} -> {exc}")
        return {"ok": False, "error": str(exc)}


def execute_command(cmd: str) -> dict[str, Any]:
    """执行白名单内的安全命令。"""
    cmd = cmd.strip()
    if not cmd:
        return {"ok": False, "error": "命令为空"}
    first = cmd.split()[0].lower()
    if first not in ALLOWED_COMMANDS:
        return {"ok": False, "error": f"命令 '{first}' 不在白名单内"}
    for bad in FORBIDDEN_SUBSTRINGS:
        if bad.lower() in cmd.lower():
            return {"ok": False, "error": f"命令包含被禁止的子串：{bad}"}
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=_home(),
        )
        _log(f"execute_command: {cmd} -> returncode={result.returncode}")
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:2000],
            "message": (result.stdout[:4000] if result.returncode == 0 else result.stderr[:2000]) or "（无输出）",
        }
    except subprocess.TimeoutExpired:
        _log(f"execute_command timeout: {cmd}")
        return {"ok": False, "error": "命令执行超时（30 秒）"}
    except Exception as exc:  # noqa: BLE001
        _log(f"execute_command failed: {cmd} -> {exc}")
        return {"ok": False, "error": f"执行失败：{exc}"}


def preview_operation(op: str, args: dict[str, Any]) -> str:
    """生成给用户看的操作预览文本。"""
    if op == "list_dir":
        return f"列出目录：{args.get('path', '')}"
    if op == "read_file":
        return f"读取文件：{args.get('path', '')}（最多 {args.get('max_lines', 50)} 行）"
    if op == "move_file":
        return f"移动/重命名：\n  从：{args.get('src', '')}\n  到：{args.get('dst', '')}"
    if op == "delete_file":
        return f"删除：{args.get('path', '')}\n（此操作不可撤销，请确认文件已备份）"
    if op == "execute_command":
        return f"执行命令：{args.get('cmd', '')}"
    return f"未知操作：{op}，参数：{args}"


def apply_operation(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """根据操作名分派执行。"""
    handlers = {
        "list_dir": lambda a: list_dir(a.get("path", "")),
        "read_file": lambda a: read_file(a.get("path", ""), a.get("max_lines", 50)),
        "move_file": lambda a: move_file(a.get("src", ""), a.get("dst", "")),
        "delete_file": lambda a: delete_file(a.get("path", "")),
        "execute_command": lambda a: execute_command(a.get("cmd", "")),
    }
    handler = handlers.get(op)
    if not handler:
        return {"ok": False, "error": f"不支持的操作：{op}"}
    result = handler(args)
    _log(f"apply_operation: {op}({args}) -> ok={result.get('ok')}")
    return result


def read_log() -> str:
    """读取最近的文件操作日志。"""
    log_file = os.path.join(data_dir(), "logs", "file_agent.log")
    if not os.path.isfile(log_file):
        return "暂无操作记录"
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-50:]) or "暂无操作记录"
    except Exception as exc:  # noqa: BLE001
        return f"读取日志失败：{exc}"
