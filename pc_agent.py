# -*- coding: utf-8 -*-
"""
pc_agent.py — 让本地 Ollama 拥有「手」：最小可运行的电脑控制 Agent

核心思路（一句话）：
    Ollama 只负责「想」（LLM 推理），本文件负责「做」（工具执行）+「循环」（多步推理）+「把关」（权限/安全）。

用法：
    python pc_agent.py                        # 交互模式，默认 qwen2.5:3b
    python pc_agent.py -m qwen2.5:7b-instruct # 指定模型
    python pc_agent.py --mode react           # 模型不支持 function calling 时降级
    python pc_agent.py --yes                  # 跳过危险操作确认（慎用）

依赖：
    pip install requests mss pyautogui psutil
    （只有 requests 是必须的，其余按需；缺失时对应工具自动禁用）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# 0. 全局配置
# ----------------------------------------------------------------------------

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = "qwen2.5:3b"
VISION_MODEL = "qwen2.5vl:3b"          # look_at_screen 专用，没有也能跑（工具会提示）
MAX_STEPS = 16                          # 单次任务最多推理/执行轮数，防失控（开发类任务需要更多轮）
SHOT_DIR = Path(__file__).parent / "shots"

# === 外部服务配置（由 gui 从 settings.json 注入；留空则对应能力给出使用指引）===
# 包含：文生图 provider/密钥/本地 SD 地址。不内置大模型权重，调用本机或云端推理服务。
EXTERNAL_CONFIG = {}

# === LLM Provider 抽象层 ===
# provider: ollama（本地，默认） / openai（OpenAI 兼容协议：OpenAI、DeepSeek、通义、Groq、vLLM、LM Studio 等） / anthropic（Claude）
# base_url: openai 默认 https://api.openai.com/v1；anthropic 默认 https://api.anthropic.com
# 入参 messages 统一使用 OpenAI 格式（role/content/tool_calls/tool），call_llm 内部按 provider 转换。
LLM_PROVIDER = "ollama"
LLM_API_KEY = ""
LLM_BASE_URL = ""


def configure(cfg: dict):
    """gui 在加载/保存设置后调用，把图像生成、LLM provider 等外部配置注入给工具函数使用。"""
    global EXTERNAL_CONFIG, LLM_PROVIDER, LLM_API_KEY, LLM_BASE_URL
    EXTERNAL_CONFIG = cfg or {}
    LLM_PROVIDER = (cfg.get("llm_provider") or "ollama").lower()
    LLM_API_KEY = cfg.get("llm_api_key") or ""
    LLM_BASE_URL = cfg.get("llm_base_url") or ""


# GitHub 默认归属组织（自动建仓库 / 上传 Release 默认写入此组织下）
GITHUB_ORG = "W-zc-lang"

# 推荐模型清单（用于前端下拉展示与下载弹窗预设；大小为参考值）
RECOMMENDED_MODELS = [
    {"name": "qwen2.5:0.5b", "label": "极速"},
    {"name": "qwen2.5:1.5b", "label": ""},
    {"name": "qwen2.5:3b", "label": "推荐起步"},
    {"name": "qwen2.5:7b", "label": ""},
    {"name": "qwen2.5:14b", "label": "大模型"},
    {"name": "qwen2.5:32b", "label": "旗舰"},
    {"name": "qwen2.5-coder:7b", "label": "编程"},
    {"name": "qwen2.5-coder:14b", "label": "编程"},
    {"name": "qwen3:4b", "label": ""},
    {"name": "qwen3:8b", "label": ""},
    {"name": "deepseek-r1:1.5b", "label": "推理"},
    {"name": "deepseek-r1:7b", "label": "推理"},
    {"name": "deepseek-r1:8b", "label": "推理"},
    {"name": "deepseek-r1:14b", "label": "推理"},
    {"name": "llama3.2:1b", "label": ""},
    {"name": "llama3.2:3b", "label": ""},
    {"name": "llama3.1:8b", "label": ""},
    {"name": "phi4:14b", "label": ""},
    {"name": "gemma2:2b", "label": ""},
    {"name": "gemma2:9b", "label": ""},
    {"name": "mistral:7b", "label": ""},
    # ---- 轻量与经典 ----
    {"name": "tinyllama:1.1b", "label": "极速"},
    {"name": "phi3:3.8b", "label": "轻量"},
    {"name": "phi3.5:3.8b", "label": "轻量"},
    {"name": "gemma:7b", "label": "经典"},
    {"name": "qwen2:7b", "label": "经典"},
    # ---- 编程专用 ----
    {"name": "codellama:7b", "label": "编程"},
    {"name": "codellama:13b", "label": "编程"},
    {"name": "codellama:34b", "label": "编程·大"},
    {"name": "starcoder2:3b", "label": "编程"},
    {"name": "starcoder2:7b", "label": "编程"},
    {"name": "starcoder2:15b", "label": "编程·大"},
    {"name": "deepseek-coder:6.7b", "label": "编程"},
    # ---- 中文 / 国产 ----
    {"name": "glm4:9b", "label": "中文"},
    {"name": "chatglm3:6b", "label": "中文"},
    {"name": "yi:34b", "label": "中文·大"},
    {"name": "baichuan2:13b", "label": "中文"},
    # ---- MoE / 混合专家 ----
    {"name": "mixtral:8x7b", "label": "MoE"},
    {"name": "mistral-nemo:12b", "label": ""},
    {"name": "deepseek-v2:16b", "label": "MoE"},
    # ---- 其他对话模型 ----
    {"name": "openchat:7b", "label": ""},
    {"name": "zephyr:7b", "label": ""},
]

# 模型大小参考表（GB，用于前端展示）
MODEL_SIZE_GB = {
    "qwen2.5:0.5b": 0.4,
    "qwen2.5:1.5b": 1.0,
    "qwen2.5:3b": 2.0,
    "qwen2.5:7b": 4.4,
    "qwen2.5:14b": 9.0,
    "qwen2.5:32b": 20.0,
    "qwen2.5-coder:7b": 4.4,
    "qwen2.5-coder:14b": 9.0,
    "qwen3:4b": 2.6,
    "qwen3:8b": 5.4,
    "deepseek-r1:1.5b": 1.1,
    "deepseek-r1:7b": 4.7,
    "deepseek-r1:8b": 4.9,
    "deepseek-r1:14b": 9.0,
    "llama3.2:1b": 1.3,
    "llama3.2:3b": 2.0,
    "llama3.1:8b": 4.7,
    "phi4:14b": 9.0,
    "gemma2:2b": 1.6,
    "gemma2:9b": 5.5,
    "mistral:7b": 4.1,
    # 轻量与经典
    "tinyllama:1.1b": 0.6,
    "phi3:3.8b": 2.2,
    "phi3.5:3.8b": 2.2,
    "gemma:7b": 4.4,
    "qwen2:7b": 4.4,
    # 编程专用
    "codellama:7b": 3.8,
    "codellama:13b": 7.4,
    "codellama:34b": 19.0,
    "starcoder2:3b": 1.7,
    "starcoder2:7b": 4.0,
    "starcoder2:15b": 9.0,
    "deepseek-coder:6.7b": 3.8,
    # 中文 / 国产
    "glm4:9b": 5.5,
    "chatglm3:6b": 3.5,
    "yi:34b": 19.0,
    "baichuan2:13b": 7.7,
    # MoE / 混合专家
    "mixtral:8x7b": 26.0,
    "mistral-nemo:12b": 7.1,
    "deepseek-v2:16b": 8.9,
    # 其他对话模型
    "openchat:7b": 4.4,
    "zephyr:7b": 4.4,
}


def model_display_name(name: str) -> str:
    """把原始模型名转成带大小与标签的展示文本。"""
    size = MODEL_SIZE_GB.get(name)
    size_text = f"（{size}GB）" if size else ""
    label = next((m["label"] for m in RECOMMENDED_MODELS if m["name"] == name), "")
    if label:
        return f"{name}{size_text}，{label}" if size_text else f"{name}，{label}"
    return f"{name}{size_text}" if size_text else name

# === 安全边界：所有文件操作只允许在这些目录内进行 ===
SAFE_ROOTS = [
    Path(r"C:\Users\win\WorkBuddy"),
    Path(r"C:\Users\win\Desktop\agent-sandbox"),
]

# === 命令黑名单（正则，命中即拒绝执行）===
BLOCKED_PATTERNS = [
    r"\bformat\b", r"\bdel\s+/[sfq]\b", r"\brm\s+-rf\b", r"\brmdir\s+/s\b",
    r"\bshutdown\b", r"\breg\s+delete\b", r"\bbcdedit\b", r"\bdiskpart\b",
    r"\btakeown\b", r"\bcipher\s+/w\b", r"\bnet\s+user\b", r"\bschtasks\s+/create\b",
    r">\s*\\?\\\\[Pp][Ii][Pp][Ee]\\", r"\bcurl\b.*\|\s*(sh|bash|powershell)",
    r"\biex\b", r"Invoke-Expression",
]

# === 需要人工二次确认的命令前缀 ===
NEED_CONFIRM = [
    r"\bRemove-Item\b", r"\brm\b", r"\bdel\b", r"\bStop-Process\b",
    r"\bStart-Process\b", r"\bSet-Content\b", r"\bNew-Item\b", r"\bMove-Item\b",
    r"\bCopy-Item\b", r"\bpowershell\b", r"\bcmd\b",
]

# GUI 模式下的二次确认回调（pywebview 弹窗）。CLI 默认 None → 退回 input() 询问。
CONFIRM_FUNC = None

# === 电脑访问权限总开关（GUI 底部勾选框控制，默认关闭）===
# 关闭时：文件读写、命令执行、截图、键鼠、进程等工具一律不可用，
# 模型只能做纯文本回答。开启后才把工具清单交给模型。
PC_ACCESS = False


def set_pc_access(value: bool) -> None:
    global PC_ACCESS
    PC_ACCESS = bool(value)


# 受权限开关管控的「电脑操作类」工具
RESTRICTED_TOOLS = {
    "list_dir", "read_file", "write_file", "run_command", "screenshot",
    "look_at_screen", "mouse_click", "type_text", "open", "list_processes",
    # 自主开发能力工具（同样受电脑访问权限总开关约束）
    "edit_file", "run_python_code", "generate_image",
    "create_github_repo", "upload_github_release", "web_fetch",
}


# ----------------------------------------------------------------------------
# 1. 安全层
# ----------------------------------------------------------------------------

def is_path_safe(p: str | Path) -> tuple[bool, str]:
    """校验路径是否落在白名单根目录内（含 .. 逃逸检查）。"""
    try:
        target = Path(os.path.abspath(os.path.expanduser(str(p))))
    except Exception as e:
        return False, f"路径解析失败: {e}"
    for root in SAFE_ROOTS:
        try:
            if target == root or root in target.parents:
                return True, ""
        except Exception:
            continue
    return False, f"拒绝：{target} 不在允许目录内 {[str(r) for r in SAFE_ROOTS]}"


def is_command_blocked(cmd: str) -> str | None:
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, cmd, re.IGNORECASE):
            return f"拒绝执行（命中黑名单规则 /{pat}/）"
    return None


def need_confirm(cmd: str) -> bool:
    return any(re.search(p, cmd, re.IGNORECASE) for p in NEED_CONFIRM)


def ask_user(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    if CONFIRM_FUNC is not None:
        try:
            return bool(CONFIRM_FUNC(prompt))
        except Exception:
            return False
    try:
        return input(f"[确认] {prompt}  (y/N): ").strip().lower() == "y"
    except EOFError:
        return False


# ----------------------------------------------------------------------------
# 2. 工具实现 —— 这就是「手」
# ----------------------------------------------------------------------------

def tool_list_dir(path: str = ".") -> str:
    ok, err = is_path_safe(path)
    if not ok:
        return err
    p = Path(path)
    if not p.exists():
        return f"路径不存在: {p}"
    lines = []
    for item in sorted(p.iterdir())[:100]:
        kind = "D" if item.is_dir() else "F"
        size = item.stat().st_size if item.is_file() else 0
        lines.append(f"{kind}  {size:>10}  {item.name}")
    return "\n".join(lines) or "(空目录)"


def tool_read_file(path: str, max_chars: int = 4000) -> str:
    ok, err = is_path_safe(path)
    if not ok:
        return err
    p = Path(path)
    if not p.is_file():
        return f"不是文件: {p}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"读取失败: {e}"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...[截断，共 {len(text)} 字符]"
    return text


def tool_write_file(path: str, content: str, auto_yes: bool = False) -> str:
    ok, err = is_path_safe(path)
    if not ok:
        return err
    p = Path(path)
    if not ask_user(f"写入文件 {p}（{len(content)} 字符）", auto_yes):
        return "用户取消"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {p}"
    except Exception as e:
        return f"写入失败: {e}"


def tool_run_command(command: str, auto_yes: bool = False) -> str:
    """执行 PowerShell 命令，带黑名单 + 二次确认 + 超时。"""
    blocked = is_command_blocked(command)
    if blocked:
        return blocked
    if need_confirm(command) and not ask_user(f"执行命令: {command}", auto_yes):
        return "用户取消"

    # 保证中文输出不乱码
    prefix = (
        "$OutputEncoding=[System.Text.Encoding]::UTF8;"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=str(SAFE_ROOTS[0]),
        )
    except subprocess.TimeoutExpired:
        return "命令超时（60s）"
    except Exception as e:
        return f"执行失败: {e}"

    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    out = out.strip()
    return out[:4000] if out else "(无输出)"


def tool_screenshot() -> str:
    try:
        import mss
    except ImportError:
        return "缺少依赖，请先 pip install mss"
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    file = SHOT_DIR / f"shot_{time.strftime('%Y%m%d_%H%M%S')}.png"
    with mss.mss() as sct:
        sct.shot(mon=-1, output=str(file))
    return f"截图已保存: {file}"


def tool_look_at_screen(question: str) -> str:
    """截图 + 交给视觉模型理解 —— 让 Agent 能「看见」屏幕。"""
    # 屏幕截图是本地内容，仅在本地 Ollama 视觉模型下处理（避免上传到云端）
    if (LLM_PROVIDER or "ollama").lower() != "ollama":
        return "「看屏幕」功能需本地 Ollama 视觉模型（如 qwen2.5vl），当前 provider 不支持，请改用本地 Ollama 或更换任务方式。"
    try:
        import mss
    except ImportError:
        return "缺少依赖，请先 pip install mss"
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    file = SHOT_DIR / f"look_{time.strftime('%Y%m%d_%H%M%S')}.png"
    with mss.mss() as sct:
        sct.shot(mon=-1, output=str(file))
    b64 = base64.b64encode(file.read_bytes()).decode()

    payload = {
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": question + "（请只描述你看到的客观内容，不要臆测）",
            "images": [b64],
        }],
        "stream": False,
    }
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=300)
        r.raise_for_status()
        return r.json()["message"]["content"]
    except Exception as e:
        return f"视觉模型调用失败（是否已 ollama pull {VISION_MODEL}？）: {e}"


def _gui():
    import pyautogui
    pyautogui.FAILSAFE = True      # 鼠标甩到左上角立即中止
    pyautogui.PAUSE = 0.2
    return pyautogui


def tool_mouse_click(x: int, y: int, auto_yes: bool = False) -> str:
    if not ask_user(f"鼠标点击坐标 ({x}, {y})", auto_yes):
        return "用户取消"
    try:
        _gui().click(int(x), int(y))
        return f"已点击 ({x}, {y})"
    except Exception as e:
        return f"点击失败（缺少 pyautogui？）: {e}"


def tool_type_text(text: str, auto_yes: bool = False) -> str:
    if not ask_user(f"模拟键盘输入: {text[:60]}", auto_yes):
        return "用户取消"
    try:
        _gui().write(text, interval=0.02)
        return f"已输入 {len(text)} 个字符"
    except Exception as e:
        return f"输入失败（缺少 pyautogui？）: {e}"


def tool_open(target: str, auto_yes: bool = False) -> str:
    """打开 URL、文件或程序。"""
    if not ask_user(f"打开: {target}", auto_yes):
        return "用户取消"
    try:
        os.startfile(target)  # noqa: S606
        return f"已请求打开 {target}"
    except Exception as e:
        return f"打开失败: {e}"


def tool_list_processes(top_n: int = 15) -> str:
    import psutil
    rows = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            mem = p.info["memory_info"].rss / 1024 / 1024
            rows.append((mem, p.info["pid"], p.info["name"]))
        except Exception:
            continue
    rows.sort(reverse=True)
    return "\n".join(f"{pid:>7}  {mem:8.1f}MB  {name}" for mem, pid, name in rows[:top_n])


def tool_finish(answer: str) -> str:
    """任务完成时调用，把最终答复交给用户。"""
    return answer


# ----------------------------------------------------------------------------
# 2.5 自主开发能力工具（写代码 / 跑程序 / 出图 / 发布到 GitHub）
# ----------------------------------------------------------------------------

def tool_edit_file(path: str, old_string: str = None, new_string: str = "",
                   auto_yes: bool = False) -> str:
    """局部替换或新建文件。old_string 为空且文件已存在 → 整体覆盖（需确认）。

    找不到 old_string 时明确报错，以便 Agent 读取现状后自行纠正（自纠错循环）。
    """
    ok, err = is_path_safe(path)
    if not ok:
        return err
    p = Path(path)
    if not p.exists():
        if not ask_user(f"创建新文件 {p}（{len(new_string)} 字符）", auto_yes):
            return "用户取消"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new_string, encoding="utf-8")
            return f"已创建 {p}"
        except Exception as e:
            return f"创建失败: {e}"
    if old_string is None:
        if not ask_user(f"覆盖文件 {p}（{len(new_string)} 字符）", auto_yes):
            return "用户取消"
        try:
            p.write_text(new_string, encoding="utf-8")
            return f"已覆盖 {p}"
        except Exception as e:
            return f"覆盖失败: {e}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if old_string not in text:
        return ("未找到待替换文本（old_string 不匹配），请先用 read_file 查看当前内容再修正。"
                "可用 edit_file 时保证 old_string 与文件原文逐字一致。")
    new_text = text.replace(old_string, new_string, 1)
    if not ask_user(f"在 {p} 中替换 {len(old_string)} 字符为 {len(new_string)} 字符", auto_yes):
        return "用户取消"
    try:
        p.write_text(new_text, encoding="utf-8")
        return f"已在 {p} 完成 1 处替换"
    except Exception as e:
        return f"替换失败: {e}"


def tool_run_python_code(code: str, timeout: int = 30, auto_yes: bool = False) -> str:
    """把生成的 Python 代码写入沙箱并运行，返回 stdout/stderr/exit code。

    用于「写→运行→看报错→改→再跑」的自纠错闭环；超时自动终止。
    """
    if not ask_user(f"运行生成的 Python 代码（{len(code)} 字符，超时 {timeout}s）", auto_yes):
        return "用户取消"
    d = SAFE_ROOTS[0] / "_sandbox_run"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"无法创建沙箱目录: {e}"
    f = d / f"run_{time.strftime('%Y%m%d_%H%M%S')}.py"
    try:
        f.write_text(code, encoding="utf-8")
    except Exception as e:
        return f"写入代码失败: {e}"
    exe = None
    for c in ("python", "python3", "py"):
        exe = shutil.which(c)
        if exe:
            break
    if not exe:
        return ("未找到 Python 解释器（python/python3/py）。请在系统中安装 Python 并加入 PATH，"
                "或将生成的代码保存为 .py 手动运行。")
    try:
        r = subprocess.run([exe, str(f)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(d))
    except subprocess.TimeoutExpired:
        return f"代码运行超时（{timeout}s），已终止。"
    except Exception as e:
        return f"运行失败: {e}"
    out = r.stdout or ""
    if r.stderr:
        out += "\n[stderr]\n" + r.stderr
    out += f"\n[exit code] {r.returncode}"
    return out[:4000] if out.strip() else "(无输出，exit 0)"


def _gen_openai(prompt: str, out_path: str, cfg: dict) -> str:
    key = cfg.get("image_openai_key")
    if not key:
        return "未配置 image_openai_key，无法使用 OpenAI 出图。"
    try:
        r = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "dall-e-3", "prompt": prompt, "n": 1,
                  "size": "1024x1024", "response_format": "b64_json"},
            timeout=120,
        )
        r.raise_for_status()
        b64 = r.json()["data"][0]["b64_json"]
        Path(out_path).write_bytes(base64.b64decode(b64))
        return f"图片已生成(OpenAI): {out_path}"
    except Exception as e:
        return f"OpenAI 出图失败: {e}"


def _gen_stability(prompt: str, out_path: str, cfg: dict) -> str:
    key = cfg.get("image_stability_key")
    if not key:
        return "未配置 image_stability_key，无法使用 Stability 出图。"
    try:
        r = requests.post(
            "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            json={"text_prompts": [{"text": prompt, "weight": 1}],
                  "height": 1024, "width": 1024, "samples": 1},
            timeout=120,
        )
        r.raise_for_status()
        b64 = r.json()["artifacts"][0]["base64"]
        Path(out_path).write_bytes(base64.b64decode(b64))
        return f"图片已生成(Stability): {out_path}"
    except Exception as e:
        return f"Stability 出图失败: {e}"


def _gen_local(prompt: str, out_path: str, cfg: dict) -> str:
    """调用本机运行的 Automatic1111 /sdapi/v1/txt2img（真正的本地推理，不占 exe 体积）。"""
    url = (cfg.get("image_local_url") or "").rstrip("/")
    if not url:
        return "未配置 image_local_url（本地 SD 服务地址，如 http://127.0.0.1:7860）。"
    try:
        r = requests.post(
            url + "/sdapi/v1/txt2img",
            json={"prompt": prompt, "steps": 20, "width": 512, "height": 512, "batch_size": 1},
            timeout=300,
        )
        r.raise_for_status()
        b64 = (r.json().get("images") or [None])[0]
        if not b64:
            return "本地 SD 服务未返回图片数据（请确认 prompt 合法且服务正常）。"
        Path(out_path).write_bytes(base64.b64decode(b64))
        return f"图片已生成(本地SD): {out_path}"
    except Exception as e:
        return f"本地 SD 出图失败（确认服务已启动且地址正确）: {e}"


def tool_generate_image(prompt: str, output_path: str = None, provider: str = None,
                        auto_yes: bool = False) -> str:
    """文生图。provider 取 (入参 > 设置) ：openai / stability / local。

    出于体积与硬件限制，本软件不内置大模型权重；改为调用你本机或云端的推理服务。
    """
    cfg = EXTERNAL_CONFIG
    provider = provider or cfg.get("image_provider", "") or ""
    if not prompt:
        return "缺少 prompt（图像描述）。"
    if output_path:
        ok, err = is_path_safe(output_path)
        if not ok:
            return err
        out_path = output_path
    else:
        d = SAFE_ROOTS[0] / "generated_images"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"无法创建输出目录: {e}"
        out_path = str(d / f"img_{time.strftime('%Y%m%d_%H%M%S')}.png")
    if provider == "openai":
        return _gen_openai(prompt, out_path, cfg)
    if provider == "stability":
        return _gen_stability(prompt, out_path, cfg)
    if provider == "local":
        return _gen_local(prompt, out_path, cfg)
    return ("未配置图像生成方式。请在设置中选择 provider 并填写：\n"
            "- OpenAI：image_openai_key\n"
            "- Stability：image_stability_key\n"
            "- 本地：image_local_url（指向你本机运行的 Automatic1111 /sdapi/v1/txt2img 或 ComfyUI）\n"
            "说明：为控制安装包体积并兼容你的硬件，本软件不内置大模型权重，"
            "而是调用你本机或云端的推理服务进行文生图。")


def tool_create_github_repo(name: str, description: str = "", private: bool = False,
                           auto_init: bool = True) -> str:
    """在 GitHub 默认组织下自动创建仓库（需用户授权 + 本机 gh 已登录）。"""
    if not ask_user(f"将在 GitHub 创建仓库 {GITHUB_ORG}/{name}（{'私有' if private else '公开'}，"
                   f"{'含 README' if auto_init else '空仓库'}）", _AUTO_YES):
        return "用户取消"
    gh = shutil.which("gh")
    if not gh:
        return "未找到 gh 命令行工具。请安装 GitHub CLI 并登录（gh auth login）后再创建仓库。"
    name = (name or "").strip().replace(" ", "-")
    if not re.match(r"^[A-Za-z0-9._-]+$", name):
        return "仓库名含非法字符，仅允许字母/数字/./_/-。"
    repo_arg = f"{GITHUB_ORG}/{name}"
    cmd = [gh, "repo", "create", repo_arg, "--description", description or name]
    cmd += ["--private" if private else "--public"]
    if auto_init:
        cmd.append("--add-readme")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except Exception as e:
        return f"创建失败: {e}"
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    if r.returncode != 0:
        return f"创建仓库失败（退出码 {r.returncode}）：\n{out[:2000]}"
    return f"已创建仓库：https://github.com/{repo_arg}\n{out.strip()}"


def tool_upload_github_release(repo: str, tag: str, title: str, notes: str = "",
                              files: list = None) -> str:
    """向 GitHub 仓库发布 Release 并上传附件（需用户授权 + 本机 gh 已登录）。"""
    files = files or []
    if not repo:
        return "缺少 repo 参数（格式 owner/repo 或仅仓库名）。"
    if "/" not in repo:
        repo = f"{GITHUB_ORG}/{repo}"
    if not tag or not title:
        return "缺少 tag 或 title。"
    safe_files = []
    for fpath in files:
        ok, err = is_path_safe(fpath)
        if not ok:
            return f"附件路径不合规已拒绝：{err}"
        if not os.path.isfile(fpath):
            return f"附件不存在：{fpath}"
        safe_files.append(fpath)
    detail = (f"向 {repo} 发布 Release {tag}（标题：{title}，附件 {len(safe_files)} 个）")
    if not ask_user(detail, _AUTO_YES):
        return "用户取消"
    gh = shutil.which("gh")
    if not gh:
        return "未找到 gh 命令行工具，请安装并登录 GitHub CLI（gh auth login）。"
    cmd = [gh, "release", "create", tag, "--repo", repo,
           "--title", title, "--notes", notes or title]
    cmd += safe_files
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception as e:
        return f"上传失败: {e}"
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    if r.returncode != 0:
        return f"上传失败（退出码 {r.returncode}）：\n{out[:2000]}"
    return f"已发布 Release：https://github.com/{repo}/releases/tag/{tag}\n{out.strip()}"


def tool_web_fetch(url: str, max_chars: int = 4000) -> str:
    """抓取网页并返回去标签后的纯文本（截断）。"""
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = r.text
        text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return (text[:max_chars] + f"\n...[截断，共 {len(text)} 字符]") if len(text) > max_chars else text
    except Exception as e:
        return f"抓取失败: {e}"


# 图片类后缀（不内联文本，仅提示）
_IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


def read_attachment(path: str, max_chars: int = 8000) -> str:
    """把用户附加的文件读成可注入 prompt 的文本上下文。"""
    p = Path(path)
    if not p.is_file():
        return f"[附件] {p.name} 不存在"
    try:
        if p.suffix.lower() in _IMAGE_SUFFIX:
            return f"[图片附件 {p.name}，当前为纯文本模型，无法内联图片内容；如需识别请使用视觉模型]"
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n...[截断，共 {len(text)} 字符]"
        return f"[文件 {p.name}]\n{text}"
    except Exception as e:
        return f"[附件 {p.name} 读取失败: {e}]"


# ----------------------------------------------------------------------------
# 3. 工具注册表 + OpenAI/Ollama 兼容的 JSON Schema
# ----------------------------------------------------------------------------

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "列出目录内容。用于查看某路径下有哪些文件/文件夹。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录绝对路径"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "读取文本文件内容。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件绝对路径"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "把内容写入文件（会覆盖）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_command", "description": "执行一条 PowerShell 命令并返回输出。用于查系统信息、跑脚本、安装依赖等。",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "PowerShell 命令"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "screenshot", "description": "截取当前屏幕并保存到本地，返回文件路径。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "look_at_screen", "description": "截图并用视觉模型理解屏幕内容，回答『屏幕上有什么』类问题。",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"}}, "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "mouse_click", "description": "在屏幕坐标 (x,y) 处点击鼠标左键。需先 look_at_screen 确认目标位置。",
        "parameters": {"type": "object", "properties": {
            "x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "type_text", "description": "模拟键盘输入一段文本到当前焦点窗口。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "open", "description": "打开一个网址、文件或应用程序。",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "URL / 文件路径 / 程序名"}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "list_processes", "description": "按内存占用列出当前运行的进程。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "finish", "description": "任务已完成，把最终答复返回给用户。",
        "parameters": {"type": "object", "properties": {
            "answer": {"type": "string"}}, "required": ["answer"]}}},
    # ---- 自主开发能力工具 ----
    {"type": "function", "function": {
        "name": "edit_file", "description": "新建或局部修改文件：给 old_string 时做精准替换（找不到会报错，便于自纠错）；不给 old_string 且文件已存在则整体覆盖；文件不存在则新建。仅限允许目录。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件绝对路径"},
            "old_string": {"type": "string", "description": "待替换的原文本（需与文件逐字一致）；留空表示新建/覆盖"},
            "new_string": {"type": "string", "description": "替换后的新内容"}},
            "required": ["path", "new_string"]}}},
    {"type": "function", "function": {
        "name": "run_python_code", "description": "把生成的 Python 代码写入沙箱并运行，返回 stdout/stderr/exit code。用于验证程序是否能跑通、捕获报错以便修正（自纠错闭环）。需要本机已安装 Python。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "完整 Python 源码"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 30"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "generate_image", "description": "文生图：根据 prompt 生成图片并保存到本地。provider 可选 openai / stability / local（本机 SD 服务）。未配置时在设置里填写对应密钥或本地 SD 地址。",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "图像描述"},
            "output_path": {"type": "string", "description": "可选，输出图片绝对路径，缺省存到 generated_images 目录"},
            "provider": {"type": "string", "description": "openai / stability / local，缺省用设置中的默认值"}},
            "required": ["prompt"]}}},
    {"type": "function", "function": {
        "name": "create_github_repo", "description": "在默认 GitHub 组织下新建仓库（需用户授权、且本机 gh 已登录）。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "仓库名（英文/数字/.-_）"},
            "description": {"type": "string", "description": "仓库描述"},
            "private": {"type": "boolean", "description": "是否私有，默认 false（公开）"},
            "auto_init": {"type": "boolean", "description": "是否带 README 初始化，默认 true"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "upload_github_release", "description": "向 GitHub 仓库发布 Release 并上传本地附件（需用户授权、且本机 gh 已登录）。repo 形如 owner/repo 或仅仓库名（默认归入设置中的组织）。",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string", "description": "owner/repo 或仅仓库名"},
            "tag": {"type": "string", "description": "版本标签，如 v1.0.0"},
            "title": {"type": "string", "description": "Release 标题"},
            "notes": {"type": "string", "description": "Release 说明（可选）"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "要上传的本地附件路径列表"}},
            "required": ["repo", "tag", "title"]}}},
    {"type": "function", "function": {
        "name": "web_fetch", "description": "抓取网页并返回去标签纯文本，用于联网查资料。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "目标网址"}},
            "required": ["url"]}}},
]

DISPATCH = {
    "list_dir": lambda a: tool_list_dir(a.get("path", ".")),
    "read_file": lambda a: tool_read_file(a["path"]),
    "write_file": lambda a: tool_write_file(a["path"], a["content"], _AUTO_YES),
    "run_command": lambda a: tool_run_command(a["command"], _AUTO_YES),
    "screenshot": lambda a: tool_screenshot(),
    "look_at_screen": lambda a: tool_look_at_screen(a["question"]),
    "mouse_click": lambda a: tool_mouse_click(a["x"], a["y"], _AUTO_YES),
    "type_text": lambda a: tool_type_text(a["text"], _AUTO_YES),
    "open": lambda a: tool_open(a["target"], _AUTO_YES),
    "list_processes": lambda a: tool_list_processes(a.get("top_n", 15)),
    "edit_file": lambda a: tool_edit_file(a["path"], a.get("old_string"), a.get("new_string", ""), _AUTO_YES),
    "run_python_code": lambda a: tool_run_python_code(a["code"], int(a.get("timeout", 30)), _AUTO_YES),
    "generate_image": lambda a: tool_generate_image(a.get("prompt", ""), a.get("output_path"), a.get("provider"), _AUTO_YES),
    "create_github_repo": lambda a: tool_create_github_repo(a["name"], a.get("description", ""), bool(a.get("private", False)), bool(a.get("auto_init", True))),
    "upload_github_release": lambda a: tool_upload_github_release(a["repo"], a["tag"], a["title"], a.get("notes", ""), a.get("files") or []),
    "web_fetch": lambda a: tool_web_fetch(a["url"]),
    "finish": lambda a: tool_finish(a["answer"]),
}

_AUTO_YES = False

SYSTEM_PROMPT = """你是运行在 Windows 上的本地电脑助理「小螃蟹 CrabClaw」，由本地 Ollama 驱动，可调用工具完成各类任务，包括自主编写并验证程序、生成图片、发布到 GitHub。

规则：
1. 一次只调用一个工具，看到结果后再决定下一步；不要凭空猜测文件内容或系统状态。
2. 涉及删除、覆盖、启动程序、点击鼠标等不可逆操作前，先用工具确认现状。
3. 路径必须使用 Windows 绝对路径；文件操作仅限 C:\\Users\\win\\WorkBuddy 与其子目录。
4. 完成任务后用 finish 工具给出简洁的中文答复，不要罗列过程。
5. 严禁执行格式化、删除系统文件、关闭计算机等破坏性命令。

自主开发工作流（当你被要求「写程序 / 做工具 / 生成应用」时）：
- 先 edit_file 把代码写入 C:\\Users\\win\\WorkBuddy 下的项目目录（可新建文件）。
- 再 run_python_code 运行它，读取 stdout/stderr/exit code。
- 若报错：根据报错用 edit_file 的 old_string 精准修正后再次运行，最多迭代 3 轮；仍失败则向用户报告错误与尝试，不要假装成功。
- 需要配图时用 generate_image（先确认设置里已配置 provider/密钥或本地 SD 地址）。
- 要把成果发布出去时，用 create_github_repo 建仓库、upload_github_release 上传可执行文件/压缩包；这两类操作会弹出授权确认，必须等用户同意。
- 遇到不确定的 API 用法，可用 web_fetch 查官方文档。
"""


# ----------------------------------------------------------------------------
# 4. Ollama 客户端
# ----------------------------------------------------------------------------

def call_llm(model: str, messages: list[dict], tools=None) -> dict:
    """统一 LLM 调用入口（provider 路由）。

    入参 messages 为 OpenAI 格式；tools 为 OpenAI function schema（None/False → 不带工具；
    True → 完整工具集；list → 指定工具 schema）。

    返回统一结构（仿 Ollama）：
        {"message": {"role": "assistant",
                     "content": str,
                     "tool_calls": [{"function": {"name": str, "arguments": dict_or_str}}]}}
    run_agent 只需读取 message.content 与 message.tool_calls，无需关心底层 provider。
    """
    provider = (LLM_PROVIDER or "ollama").lower()
    try:
        if provider == "openai":
            return _call_openai(model, messages, tools)
        if provider == "anthropic":
            return _call_anthropic(model, messages, tools)
        # 默认 / ollama
        return _call_ollama(model, messages, tools)
    except Exception:
        # 兜底走 ollama，避免 provider 配置异常时彻底无法工作
        if provider in ("ollama", ""):
            raise
        try:
            return _call_ollama(model, messages, tools)
        except Exception:
            raise


def _call_ollama(model: str, messages: list[dict], tools=None) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }
    if tools is True:
        payload["tools"] = TOOLS_SCHEMA
    elif isinstance(tools, list):
        payload["tools"] = tools
    r = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=600)
    r.raise_for_status()
    return r.json()


def _call_openai(model: str, messages: list[dict], tools=None) -> dict:
    base = (LLM_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    url = base + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % (LLM_API_KEY or ""),
    }
    # 规范化 messages：assistant 的 tool_calls 必须带 id/type 且 arguments 为 JSON 字符串
    norm = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            tcs = []
            for i, tc in enumerate(m["tool_calls"]):
                fn = tc.get("function", {}) or {}
                args = fn.get("arguments", {})
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                tcs.append({
                    "id": tc.get("id") or ("call_%d" % i),
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args},
                })
            norm.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": tcs})
        else:
            norm.append(m)
    payload = {
        "model": model,
        "messages": norm,
        "stream": False,
        "temperature": 0.2,
    }
    if tools is True:
        payload["tools"] = TOOLS_SCHEMA
        payload["tool_choice"] = "auto"
    elif isinstance(tools, list):
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    r = requests.post(url, headers=headers, json=payload, timeout=600)
    r.raise_for_status()
    data = r.json()
    msg = (data.get("choices") or [{}])[0].get("message", {})
    content = msg.get("content") or ""
    calls = []
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {}) or {}
        args = fn.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        calls.append({"function": {"name": fn.get("name", ""), "arguments": args}})
    return {"message": {"role": "assistant", "content": content, "tool_calls": calls}}


def _to_anthropic_tools(openai_tools):
    out = []
    for t in (openai_tools or []):
        fn = t.get("function", t)
        params = fn.get("parameters", {}) or {}
        out.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": {
                "type": "object",
                "properties": params.get("properties", {}),
                "required": params.get("required", []),
            },
        })
    return out


def _call_anthropic(model: str, messages: list[dict], tools=None) -> dict:
    base = (LLM_BASE_URL or "https://api.anthropic.com").rstrip("/")
    url = base + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": LLM_API_KEY or "",
        "anthropic-version": "2023-06-01",
    }
    sys_text = ""
    conv = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            sys_text += (m.get("content") or "") + "\n"
            continue
        if role == "assistant":
            content = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {}) or {}
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                content.append({
                    "type": "tool_use",
                    "id": "tool_" + fn.get("name", ""),
                    "name": fn.get("name", ""),
                    "input": args,
                })
            if content:
                conv.append({"role": "assistant", "content": content})
        elif role == "tool":
            conv.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool_" + (m.get("name") or ""),
                    "content": m.get("content") or "",
                }],
            })
        else:  # user
            c = m.get("content")
            if isinstance(c, list):
                conv.append({"role": "user", "content": c})
            else:
                conv.append({"role": "user", "content": [{"type": "text", "text": c or ""}]})
    payload = {
        "model": model,
        "messages": conv,
        "max_tokens": 4096,
        "temperature": 0.2,
    }
    if sys_text.strip():
        payload["system"] = sys_text.strip()
    if tools is True:
        payload["tools"] = _to_anthropic_tools(TOOLS_SCHEMA)
    elif isinstance(tools, list):
        payload["tools"] = _to_anthropic_tools(tools)
    r = requests.post(url, headers=headers, json=payload, timeout=600)
    r.raise_for_status()
    data = r.json()
    content = ""
    calls = []
    for block in (data.get("content") or []):
        if block.get("type") == "text":
            content += block.get("text", "")
        elif block.get("type") == "tool_use":
            calls.append({"function": {"name": block.get("name", ""), "arguments": block.get("input", {})}})
    return {"message": {"role": "assistant", "content": content, "tool_calls": calls}}


def get_models() -> list[str]:
    """从 Ollama 拉取可用模型名列表（用于前端下拉框）。"""
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        r.raise_for_status()
        data = r.json().get("models", [])
        names = []
        for m in data:
            n = m.get("name") or m.get("model")
            if n:
                names.append(n)
        return names
    except Exception:
        return []


def list_models() -> list[str]:
    """按当前 provider 拉取可用模型名（用于前端下拉框）。"""
    provider = (LLM_PROVIDER or "ollama").lower()
    if provider == "openai":
        try:
            base = (LLM_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            r = requests.get(base + "/models",
                             headers={"Authorization": "Bearer %s" % (LLM_API_KEY or "")},
                             timeout=10)
            r.raise_for_status()
            data = r.json().get("data", [])
            names = sorted({m["id"] for m in data if m.get("id")})
            return names
        except Exception as e:
            log.warning("openai list models failed: %s", e)
            return []
    if provider == "anthropic":
        # Anthropic 无公开列举接口，返回空（前端让用户手填模型名）
        return []
    # 默认 / ollama
    return get_models()


def check_llm() -> dict:
    """按当前 provider 自检 API 通路是否可达。返回 {ok, error, provider}。"""
    provider = (LLM_PROVIDER or "ollama").lower()
    if provider == "openai":
        try:
            base = (LLM_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            r = requests.get(base + "/models",
                             headers={"Authorization": "Bearer %s" % (LLM_API_KEY or "")},
                             timeout=10)
            if r.status_code == 200:
                return {"ok": True, "provider": "openai"}
            return {"ok": False, "error": "API 返回 HTTP %d（请检查 Key / Base URL）" % r.status_code, "provider": "openai"}
        except Exception as e:
            return {"ok": False, "error": "无法连接 OpenAI 兼容端点：%s" % e, "provider": "openai"}
    if provider == "anthropic":
        try:
            base = (LLM_BASE_URL or "https://api.anthropic.com").rstrip("/")
            r = requests.post(base + "/v1/messages",
                             headers={"Content-Type": "application/json",
                                      "x-api-key": LLM_API_KEY or "",
                                      "anthropic-version": "2023-06-01"},
                             json={"model": "claude-3-5-haiku-latest",
                                   "messages": [{"role": "user", "content": "hi"}],
                                   "max_tokens": 1},
                             timeout=10)
            # 200=正常；400/401 说明已连通但请求/鉴权不当，仍视为服务可达
            if r.status_code in (200, 400, 401, 403, 429):
                return {"ok": True, "provider": "anthropic"}
            return {"ok": False, "error": "API 返回 HTTP %d（请检查 Key / Base URL）" % r.status_code, "provider": "anthropic"}
        except Exception as e:
            return {"ok": False, "error": "无法连接 Anthropic：%s" % e, "provider": "anthropic"}
    # 默认 / ollama
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if r.status_code == 200:
            return {"ok": True, "provider": "ollama"}
        return {"ok": False, "error": "Ollama 返回 HTTP %d" % r.status_code, "provider": "ollama"}
    except Exception as e:
        return {"ok": False, "error": "未检测到本机 Ollama 服务，请先安装并运行 Ollama", "provider": "ollama"}


# ----------------------------------------------------------------------------
# 5. Agent 循环 —— 「大脑 → 手 → 观察 → 再想」的闭环
# ----------------------------------------------------------------------------

def run_agent(user_task: str, model: str, mode: str = "tools",
              verbose: bool = True, on_event: callable = None,
              pc_access: bool = None, files: list = None) -> str:
    """运行 Agent 闭环。

    on_event: 可选回调，签名 (event: dict) -> None。
        {"type":"step","step":n,"name":str,"args":dict}
        {"type":"tool_result","result":str}
        {"type":"final","text":str}
        {"type":"error","text":str}
        {"type":"image","data":"data:image/png;base64,..."}   # 文生图预览
    CLI 模式传 None 即可（走 print）；GUI 模式用它把进度推到前端。

    pc_access: 是否授予电脑访问权限（None=沿用全局 PC_ACCESS）。
    files:     本次对话附带的文件路径列表，内容会注入 prompt 供模型引用。
    """
    if pc_access is not None:
        set_pc_access(pc_access)

    def emit(ev):
        if verbose:
            _print_event(ev)
        if on_event:
            on_event(ev)

    # ---- 组装用户消息：把附件内容拼接在任务前，便于模型按文件名引用 ----
    user_content = user_task
    if files:
        blocks = [read_attachment(f) for f in files]
        ctx = "\n\n".join(blocks)
        user_content = (
            "以下是本次对话附带的参考文件内容（模型可按文件名引用其中信息）：\n"
            f"{ctx}\n\n用户任务：{user_task}"
        )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    if mode == "react":
        messages[0]["content"] += REACT_SUFFIX

    # 权限关闭时只给纯文本能力（不挂工具），模型无法操作电脑
    effective_tools = TOOLS_SCHEMA if PC_ACCESS else []

    for step in range(1, MAX_STEPS + 1):
        try:
            resp = call_llm(model, messages, tools=effective_tools)
        except Exception as e:
            prov = (LLM_PROVIDER or "ollama").lower()
            label = {"ollama": "Ollama", "openai": "OpenAI 兼容服务", "anthropic": "Anthropic"}.get(prov, prov)
            emit({"type": "error", "text": f"[调用 {label} 失败] {e}"})
            return f"[调用 {label} 失败] {e}"

        msg = resp.get("message", {})
        calls = msg.get("tool_calls") or []

        # ---- 降级路径：模型不支持 function calling，改解析 JSON 文本 ----
        if not calls and mode == "react":
            calls = parse_react(msg.get("content", ""))
        if not calls and mode == "tools" and looks_like_json_action(msg.get("content", "")):
            calls = parse_react(msg.get("content", ""))

        # ---- 没有工具调用 = 直接回答 ----
        if not calls:
            final = msg.get("content", "(模型未返回内容)")
            emit({"type": "final", "text": final})
            return final

        for call in calls:
            fn = call["function"]["name"]
            args = call["function"].get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}

            emit({"type": "step", "step": step, "name": fn, "args": args})

            # ---- 权限闸门：未开启时拒绝一切电脑操作类工具 ----
            if fn in RESTRICTED_TOOLS and not PC_ACCESS:
                result = "权限未开启：请在界面底部勾选「授予电脑访问权限」后再执行该操作。"
            elif fn not in DISPATCH:
                result = f"未知工具 {fn}"
            else:
                try:
                    result = DISPATCH[fn](args)
                except Exception as e:
                    result = f"工具执行异常: {type(e).__name__}: {e}"

            emit({"type": "tool_result", "result": str(result)})

            # 文生图成功后，额外推送图片预览事件（base64 data URL，绕开 file:// 限制）
            if fn == "generate_image":
                m = re.search(r"图片已生成\S*\s*[：:]\s*(\S+)", str(result))
                if m:
                    try:
                        b = Path(m.group(1)).read_bytes()
                        data_url = "data:image/png;base64," + base64.b64encode(b).decode()
                        emit({"type": "image", "data": data_url})
                    except Exception:
                        pass

            if fn == "finish":
                emit({"type": "final", "text": str(result)})
                return str(result)

            messages.append({"role": "assistant", "content": msg.get("content", ""),
                             "tool_calls": calls})
            messages.append({"role": "tool", "name": fn, "content": str(result)})

    msg = f"[超过最大步数 {MAX_STEPS}，已中止]"
    emit({"type": "final", "text": msg})
    return msg


def _print_event(ev):
    t = ev.get("type")
    if t == "step":
        print(f"\n[{ev['step']}] ▶ {ev['name']}({json.dumps(ev['args'], ensure_ascii=False)[:200]})")
    elif t == "tool_result":
        print(f"    ◀ {ev['result'][:500]}")
    # "final" 由调用方自行打印，避免重复输出


REACT_SUFFIX = """

当你需要调用工具时，必须且只能输出如下 JSON 代码块（不要输出其它内容）：
```json
{"action": "工具名", "args": {"参数名": "参数值"}}
```
可用工具：list_dir / read_file / write_file / run_command / screenshot / look_at_screen / mouse_click / type_text / open / list_processes / finish
"""


def looks_like_json_action(text: str) -> bool:
    return bool(re.search(r"```json\s*\{[^`]*\"action\"", text or "", re.DOTALL))


def parse_react(text: str) -> list[dict]:
    m = re.search(r"```json\s*(\{.*?\})\s*```", text or "", re.DOTALL)
    if not m:
        m = re.search(r"(\{\s*\"action\".*?\})", text or "", re.DOTALL)
    if not m:
        return []
    try:
        obj = json.loads(m.group(1))
        return [{"function": {"name": obj["action"], "arguments": obj.get("args", {})}}]
    except Exception:
        return []


# ----------------------------------------------------------------------------
# 6. CLI
# ----------------------------------------------------------------------------

def main():
    global _AUTO_YES
    ap = argparse.ArgumentParser(description="Ollama 电脑控制 Agent（最小可运行版）")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL)
    ap.add_argument("--mode", choices=["tools", "react"], default="tools",
                    help="tools=原生 function calling；react=JSON 文本降级模式")
    ap.add_argument("--yes", action="store_true", help="跳过危险操作确认（慎用）")
    ap.add_argument("task", nargs="*", help="一次性任务；留空进入交互模式")
    args = ap.parse_args()
    _AUTO_YES = args.yes

    print(f"[模型] {args.model}   [模式] {args.mode}   [工作根目录] {SAFE_ROOTS[0]}")
    print("输入任务，输入 exit 退出。\n")

    if args.task:
        print("\n===== 结果 =====")
        print(run_agent(" ".join(args.task), args.model, args.mode))
        return

    while True:
        try:
            task = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if task.lower() in ("exit", "quit", "退出"):
            break
        if not task:
            continue
        print("\n----- 答复 -----")
        print(run_agent(task, args.model, args.mode))


if __name__ == "__main__":
    main()
