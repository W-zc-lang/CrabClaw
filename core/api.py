"""暴露给前端的 JS 桥接层。

流式输出的实现要点：
    pywebview 的 evaluate_js 是同步阻塞的（内部走 Invoke 封送到 UI 线程），
    所以不能「收到一个 token 就推一次」——那会把推理线程拖死。
    这里按「攒够 N 个字符 或 距上次推送超过 50ms」做批量冲刷，
    实测在 CPU 推理（3-5 字/秒）下完全够用，界面也不会卡顿。

所有推送内容统一走 base64，避免换行 / 引号 / emoji 在 eval() 里出转义问题。
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from . import file_agent
from . import ollama
from .knowledge import Knowledge
from .store import Store, data_dir

FLUSH_MIN_CHARS = 48
FLUSH_INTERVAL = 0.05

# 内置模型定义：打包时把 local-ai-model/Modelfile 作为数据打进 exe，
# 首次启动若本地没有 local-assistant 就自动 ollama create 出来。
# 内置模型清单：首次启动若本地没有就自动拉基础模型并 ollama create。
# name = 模型名；file = 打包内置的 Modelfile 文件名（assets/ 下，打进 exe 后落在程序根目录）；
# base = 依赖的基础模型（多个内置可共享同一底座，只拉一次）；label = 下拉里展示的名称。
BUILTIN_MODELS = [
    {
        "name": "local-assistant",
        "file": "Modelfile",
        "base": "qwen2.5:3b",
        "label": "小墨（通用助手）",
    },
    {
        "name": "zc",
        "file": "Modelfile.zc",
        "base": "qwen2.5:3b",
        "label": "子成（你的专属 AI）",
    },
]
# 向后兼容：默认仍用列表里第一个
BUILTIN_MODEL_NAME = BUILTIN_MODELS[0]["name"]
BUILTIN_BASE_MODEL = BUILTIN_MODELS[0]["base"]

# 思考强度 -> 注入到 system 的指令。0 表示不额外要求思考。
THINKING_INSTRUCTIONS = {
    1: (
        "回答前先用一两句话简要梳理你的总体思路，再给出正式答案；"
        "简单问题可省略这一步。"
    ),
    2: (
        "遇到非平凡的问题，先在内部按「拆解问题 → 列出关键条件/假设 → 逐步推导 → "
        "自检合理性 → 给结论」的步骤思考，再作答。简单问题可直接回答。"
    ),
    3: (
        "你必须显式输出推导过程：先用「思路：」写出对问题的拆解与逐步推理，"
        "再用「结论：」给出最终答案。数学、逻辑、规划、排错类问题尤其要逐步推导，"
        "不要直接给结果。"
    ),
}

# 文件控制开启时注入到 system 的指令
FILE_CONTROL_INSTRUCTION = """\
你当前已开启「电脑文件控制」功能。当用户要求你操作其电脑上的文件时，你可以使用以下能力：
1. list_dir(path)：列出指定目录的内容。
2. read_file(path, max_lines=50)：读取文本文件内容（默认最多 50 行）。
3. move_file(src, dst)：移动或重命名文件/目录。
4. delete_file(path)：删除文件或空目录。
5. execute_command(cmd)：执行白名单内的安全命令（如 dir、ls、echo、python、git status 等）。

对于任何可能修改文件系统的操作（move、delete、execute），你必须：
- 先向用户清晰说明你要做什么、影响哪些文件。
- 在回复末尾输出一段可被程序解析的 JSON 操作请求，格式严格如下：
[[FILE_OP]]
{"op": "move|delete|execute", "args": {"...": "..."}, "reason": "简短说明"}
[[/FILE_OP]]
- 如果用户只是询问文件内容或列目录（只读操作），你可以直接回答，不需要输出 [[FILE_OP]]。
- 严禁访问系统目录（如 C:\\Windows、Program Files 等）。

示例：
用户说"把下载目录的 report.txt 移到桌面"，你可以回复：
我将把 C:\\Users\\win\\Downloads\\report.txt 移动到 C:\\Users\\win\\Desktop\\report.txt。
[[FILE_OP]]
{"op": "move", "args": {"src": "C:\\Users\\win\\Downloads\\report.txt", "dst": "C:\\Users\\win\\Desktop\\report.txt"}, "reason": "按用户要求移动文件"}
[[/FILE_OP]]
"""

# 提取 AI 回复中的文件操作请求标记
FILE_OP_RE = re.compile(r"\[\[FILE_OP\]\]\s*(\{.*?\})\s*\[\[/FILE_OP\]\]", re.DOTALL)


def _extract_file_op(text: str) -> dict[str, Any] | None:
    """从 AI 回复中提取 [[FILE_OP]] JSON 操作请求。"""
    m = FILE_OP_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _fmt_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """把 Ollama 的原始纳秒统计换算成人能看的数字。"""
    out: dict[str, Any] = {}
    eval_count = stats.get("eval_count") or 0
    eval_dur = (stats.get("eval_duration") or 0) / 1e9
    prompt_count = stats.get("prompt_eval_count") or 0
    load_dur = (stats.get("load_duration") or 0) / 1e9
    if eval_count and eval_dur:
        out["tokens"] = eval_count
        out["tps"] = round(eval_count / eval_dur, 1)
    if prompt_count:
        out["prompt_tokens"] = prompt_count
    if load_dur > 0.05:
        out["load_s"] = round(load_dur, 1)
    return out


class Api:
    """pywebview js_api 的实现对象，public 方法都能在 JS 里直接调用。"""

    def __init__(self) -> None:
        self.store = Store()
        self.knowledge = Knowledge()
        self.window = None
        self._cancel = threading.Event()
        self._pull_cancel = threading.Event()
        self._busy = False
        self._lock = threading.Lock()
        self._ensure_lock = threading.Lock()
        self._ollama_env = None  # 内置 ollama 时写入 OLLAMA_MODELS 等环境变量
        # 办公能力运行时状态
        self._sheets: dict[str, Any] = {}
        self._current_sheet_id: str | None = None
        self._sheet_busy = False
        self._sheet_cancel = threading.Event()

    # ------------------------------------------------------------ 内部工具
    def bind(self, window) -> None:
        self.window = window

    def _push(self, event: dict[str, Any]) -> None:
        if self.window is None:
            return
        raw = json.dumps(event, ensure_ascii=False).encode("utf-8")
        b64 = base64.b64encode(raw).decode("ascii")
        try:
            self.window.evaluate_js(f"__pyEvent({json.dumps(b64)})")
        except Exception:
            # 窗口已关闭 / 正在销毁，忽略即可
            pass

    def _settings(self) -> dict[str, Any]:
        return self.store.get_settings()

    def _model_name(self) -> str:
        return (self._settings().get("model") or "").strip()

    def _require_model(self) -> str:
        model = self._model_name()
        if not model:
            raise RuntimeError("还没选择模型，先在左侧「模型」里拉一个下来。")
        return model

    # ------------------------------------------------------------ 初始化
    def bootstrap(self) -> dict[str, Any]:
        """前端启动时一次性拉取所有初始状态。

        注意：这里不再同步调用 ollama.list_models() / version()，因为 Ollama
        若处于半启动状态，HTTP 探测可能阻塞数秒，导致前端窗口被 Windows 标为
        「未响应」。Ollama 状态改为后台线程刷新，通过事件推给前端。
        """
        running = ollama.is_running()
        settings = self._settings()

        # 后台刷新 Ollama 模型列表与版本，避免阻塞 GUI
        threading.Thread(target=self._refresh_ollama_worker, daemon=True).start()

        return {
            "ok": True,
            "ollama_running": running,  # 快速探测结果，可能随后被事件覆盖
            "ollama_version": "",
            "models": [],
            "recommended": ollama.RECOMMENDED_MODELS,
            "settings": settings,
            "sessions": self.store.list_sessions(),
            "data_dir": data_dir(),
        }

    def _refresh_ollama_worker(self) -> None:
        """后台获取 Ollama 状态、版本和模型列表，通过事件推给前端。"""
        if not ollama.is_running():
            self._push({"type": "ollama_status", "running": False, "version": "", "models": []})
            return
        try:
            models = ollama.list_models()
            version = ollama.version()
        except Exception as exc:  # noqa: BLE001
            self._push({"type": "ollama_status", "running": True, "version": f"读取失败：{exc}", "models": []})
            return

        # 默认模型没设或已被删掉时，自动挑一个本地已有的
        settings = self._settings()
        if not settings.get("model") and models:
            self.store.set_setting("model", models[0]["name"])
            self._push({"type": "settings", "settings": self._settings()})

        self._push({"type": "ollama_status", "running": True, "version": version, "models": models})

        # 如果本地有内置模型缺失，自动在后台准备（不阻塞前端）
        names = [m["name"] for m in models]
        if not all(spec["name"] in names for spec in BUILTIN_MODELS):
            self._ensure_model_worker()

    def refresh_models(self) -> dict[str, Any]:
        """前端手动刷新模型列表（同步，但由用户主动触发，可接受短暂等待）。"""
        running = ollama.is_running()
        models: list[dict[str, Any]] = []
        if running:
            try:
                models = ollama.list_models()
            except Exception:  # noqa: BLE001
                models = []
        return {"ok": True, "ollama_running": running, "models": models}

    def retry_connection(self) -> dict[str, Any]:
        """用户点击重试时，后台重新探测。"""
        threading.Thread(target=self._refresh_ollama_worker, daemon=True).start()
        return {"ok": True, "started": True}

    # --------------------------------------------------------------------------
    # Ollama 启动：主线程必须「立即返回」，否则 time.sleep 轮询会把 GUI 消息循环
    # 饿死，窗口被 Windows 标为「未响应」。真正的拉起 + 就绪检测放到后台线程。
    # --------------------------------------------------------------------------
    def _launch_ollama_process(self, exe: str) -> bool:
        """拉起 ollama serve 子进程（只启动，不等就绪）。成功返回 True。"""
        env = None
        if self._is_bundled_ollama(exe):
            models_dir = os.path.join(self._app_dir(), "models")
            os.makedirs(models_dir, exist_ok=True)
            env = dict(os.environ)
            env["OLLAMA_MODELS"] = models_dir
            self._ollama_env = env
        try:
            if sys.platform == "win32":
                # 完全脱离当前进程，后台常驻
                creationflags = 0x00000008 | 0x00000200  # DETACHED + CREATE_NEW_PROCESS_GROUP
                subprocess.Popen(  # noqa: S603
                    [exe, "serve"],
                    creationflags=creationflags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
            else:
                subprocess.Popen(  # noqa: S603
                    [exe, "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
            return True
        except Exception:  # noqa: BLE001
            return False

    def _wait_ollama_ready(self, timeout: float = 30.0) -> bool:
        """轮询 Ollama 是否就绪（在后台线程调用，不阻塞 GUI 主线程）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            if ollama.is_running():
                time.sleep(1.0)  # 给 /api/tags 一点热身时间
                return True
        return False

    def start_ollama(self) -> dict[str, Any]:
        """JS API：异步启动 Ollama。主线程立即返回，拉起与就绪检测在后台线程完成。

        启动结果通过 ollama_status 事件推给前端（onOllamaStatus 已处理），
        因此这个方法本身不能做任何同步等待。
        """
        if ollama.is_running():
            # 已经在跑，直接后台刷新一次模型列表并返回
            threading.Thread(target=self._refresh_ollama_worker, daemon=True).start()
            return {"ok": True, "started": True}
        exe = self._find_ollama_exe()
        if not exe:
            return {
                "ok": False,
                "error": "没找到 ollama.exe。请先去 https://ollama.com 安装，"
                "或把 ollama.exe 放到本程序所在目录。",
            }
        # 后台真正去拉起并等待就绪，主线程立刻返回，避免窗口「未响应」
        threading.Thread(target=self._start_ollama_worker, args=(exe,), daemon=True).start()
        return {"ok": True, "started": True}

    def _start_ollama_worker(self, exe: str) -> None:
        """后台线程：拉起 ollama serve，轮询就绪后刷新模型并推事件给前端。"""
        if not self._launch_ollama_process(exe):
            self._push({"type": "model_error", "message": "启动 Ollama 失败，请检查 ollama.exe 是否完整。"})
            return
        if self._wait_ollama_ready():
            # 就绪：刷新模型列表与版本，事件会驱动前端更新状态
            self._refresh_ollama_worker()
        else:
            self._push({"type": "model_error", "message": "已尝试启动 Ollama，但 30 秒内没连上。"})

    def ensure_model(self) -> dict[str, Any]:
        """后台确保内置模型 local-assistant 存在：缺失则自动拉基础模型并创建。"""
        threading.Thread(target=self._ensure_model_worker, daemon=True).start()
        return {"ok": True, "started": True}

    def _ensure_model_worker(self) -> None:
        """后台确保内置模型存在。加锁避免 bootstrap 事件与前端手动触发重复执行。"""
        if not self._ensure_lock.acquire(blocking=False):
            return
        # 清掉上次可能残留的取消状态，否则内置模型下载会立刻被判为取消
        self._pull_cancel.clear()
        try:
            if not ollama.is_running():
                exe = self._find_ollama_exe()
                if not exe:
                    self._push({"type": "model_error", "message": "没找到 ollama.exe，无法准备内置模型。请先安装 Ollama。"})
                    return
                if not self._launch_ollama_process(exe):
                    self._push({"type": "model_error", "message": "启动 Ollama 失败。"})
                    return
                if not self._wait_ollama_ready():
                    self._push({"type": "model_error", "message": "已尝试启动 Ollama，但 30 秒内没连上。"})
                    return
            models = ollama.list_models()
            names = [m["name"] for m in models]

            # 所有内置模型都已存在：直接结束
            if all(spec["name"] in names for spec in BUILTIN_MODELS):
                self._finish_ensure(models)
                return

            # 逐个确保内置模型存在（共享的 base 只拉一次）
            pulled_bases: set[str] = set()
            for spec in BUILTIN_MODELS:
                if spec["name"] in names:
                    continue
                # 拉基础模型（仅首次需要联网；多个内置共享同一 base 时只拉一次）
                if spec["base"] not in names and spec["base"] not in pulled_bases:
                    self._push({"type": "model_status", "message": f"正在下载基础模型 {spec['base']}（首次约 2GB，请耐心等待）…"})
                    if not self._pull_with_progress(
                        spec["base"], self._pull_cancel.is_set, auto=True
                    ):
                        self._push(
                            {
                                "type": "model_error",
                                "message": f"下载基础模型 {spec['base']} 失败，请检查网络（或挂代理）后重试。",
                            }
                        )
                        return
                    pulled_bases.add(spec["base"])
                    self._push({"type": "pull_done", "model": spec["base"]})
                    models = ollama.list_models()
                    names = [m["name"] for m in models]
                # 找内置 Modelfile：程序根 → assets/ → 回退通用 Modelfile
                mf = self._res(spec["file"])
                if not os.path.isfile(mf):
                    mf = self._res(os.path.join("assets", spec["file"]))
                if not os.path.isfile(mf):
                    mf = self._res("Modelfile")
                if not mf or not os.path.isfile(mf):
                    self._push({"type": "model_error", "message": f"未找到内置模型定义 {spec['file']}，无法创建 {spec['name']}。"})
                    continue
                self._push({"type": "model_status", "message": f"正在创建 {spec['name']}（{spec.get('label', '')}）…"})
                oexe = self._find_ollama_exe() or "ollama"
                proc = subprocess.run(  # noqa: S603
                    [oexe, "create", spec["name"], "-f", mf],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=self._ollama_env,
                )
                if proc.returncode != 0:
                    self._push({"type": "model_error", "message": f"创建模型 {spec['name']} 失败：{proc.stderr or proc.stdout}"})
                    continue
                models = ollama.list_models()
                names = [m["name"] for m in models]

            self._finish_ensure(models)
        except Exception as exc:  # noqa: BLE001
            self._push({"type": "model_error", "message": f"准备模型时出错：{exc}"})
        finally:
            self._ensure_lock.release()

    def _finish_ensure(self, models: list[dict[str, Any]]) -> None:
        # 自动把默认模型设为 local-assistant
        cur = self._model_name()
        if not cur or cur not in [m["name"] for m in models]:
            self.store.set_setting("model", BUILTIN_MODEL_NAME)
        self._push({"type": "models", "models": models})
        self._push({"type": "model_ready", "model": BUILTIN_MODEL_NAME})
        self._push({"type": "settings", "settings": self._settings()})

    @staticmethod
    def _res(rel: str) -> str:
        # 资源定位：
        # - onefile：解压在 sys._MEIPASS
        # - onedir（frozen 但无 _MEIPASS）：datas 落在程序目录下的 _internal
        # - 源码运行：api.py 在 core/ 下，项目根是其父目录
        if getattr(sys, "_MEIPASS", ""):
            base = sys._MEIPASS
        elif getattr(sys, "frozen", False):
            base = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "_internal")
        else:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, rel)

    @staticmethod
    def _app_dir() -> str:
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        # 源码运行：指向项目根（local-ai-chat）
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _is_bundled_ollama(self, exe: str) -> bool:
        exe = os.path.abspath(exe)
        app = os.path.abspath(self._app_dir())
        meipass = getattr(sys, "_MEIPASS", "")
        return exe.startswith(app) or (meipass and exe.startswith(os.path.abspath(meipass)))

    @staticmethod
    def _find_ollama_exe() -> str | None:
        candidates: list[str] = []
        # 1) 打包内置（onedir 模式会放在程序目录）
        if getattr(sys, "frozen", False):
            exe_in_app = os.path.join(Api._app_dir(), "ollama.exe")
            if os.path.isfile(exe_in_app):
                candidates.append(exe_in_app)
        else:
            # 源码调试时若把 ollama.exe 放到项目目录也能用
            local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ollama.exe")
            if os.path.isfile(local):
                candidates.append(local)
        # 2) 系统安装
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA", "")
            candidates.append(os.path.join(local, "Programs", "Ollama", "ollama.exe"))
            candidates.append(r"C:\Program Files\Ollama\ollama.exe")
        else:
            candidates.append("/usr/local/bin/ollama")
            candidates.append("/usr/bin/ollama")
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return None

    # ------------------------------------------------------------ 会话管理
    def new_session(self, title: str = "新对话") -> dict[str, Any]:
        model = self._model_name()
        s = self.store.create_session(title=title, model=model)
        return {"ok": True, "session": s, "sessions": self.store.list_sessions()}

    def open_session(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if not session:
            return {"ok": False, "error": "会话不存在"}
        msgs = self.store.get_messages(session_id)
        return {"ok": True, "session": session, "messages": msgs}

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        self.store.rename_session(session_id, title)
        return {"ok": True, "sessions": self.store.list_sessions()}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        self.store.delete_session(session_id)
        return {"ok": True, "sessions": self.store.list_sessions()}

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        for k, v in (patch or {}).items():
            self.store.set_setting(k, v)
        return {"ok": True, "settings": self._settings()}

    def reset_settings(self) -> dict[str, Any]:
        """恢复默认设置，但保留模型选择（如果本地有模型）。"""
        from .store import DEFAULT_SETTINGS
        current_model = self._settings().get("model", "")
        self.store.set_setting("system_prompt", DEFAULT_SETTINGS["system_prompt"])
        self.store.set_setting("temperature", DEFAULT_SETTINGS["temperature"])
        self.store.set_setting("top_p", DEFAULT_SETTINGS["top_p"])
        self.store.set_setting("num_ctx", DEFAULT_SETTINGS["num_ctx"])
        self.store.set_setting("keep_alive", DEFAULT_SETTINGS["keep_alive"])
        self.store.set_setting("thinking_level", DEFAULT_SETTINGS["thinking_level"])
        self.store.set_setting("knowledge_enabled", DEFAULT_SETTINGS["knowledge_enabled"])
        self.store.set_setting("bionic_enabled", DEFAULT_SETTINGS["bionic_enabled"])
        # 文件控制开关也恢复默认（关闭），但保留免责声明同意记录便于再次开启
        self.store.set_setting("file_control_enabled", DEFAULT_SETTINGS["file_control_enabled"])
        # 如果当前有选中的模型，保留它，否则用默认空值
        if current_model:
            self.store.set_setting("model", current_model)
        return {"ok": True, "settings": self._settings()}

    def open_data_dir(self) -> dict[str, Any]:
        path = data_dir()
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    # ------------------------------------------------------------ 模型管理
    def pull_model(self, name: str) -> dict[str, Any]:
        if not name:
            return {"ok": False, "error": "模型名为空"}
        if not ollama.is_running():
            return {"ok": False, "error": "Ollama 服务没启动"}
        if self._busy:
            return {"ok": False, "error": "正在生成回复，等它结束再下载"}

        self._pull_cancel.clear()
        t = threading.Thread(target=self._pull_worker, args=(name,), daemon=True)
        t.start()
        return {"ok": True, "started": True}

    def _pull_with_progress(
        self,
        name: str,
        cancel: Callable[[], bool] | None = None,
        auto: bool = False,
    ) -> bool:
        """拉取模型并推送进度事件，供前端显示进度条。

        auto=True 表示这是内置模型自动准备触发的下载，前端会自动弹出进度条。
        成功返回 True，失败或被取消返回 False。
        """
        last_pct = -1
        try:
            for ev in ollama.pull_stream(name, cancel=cancel):
                if cancel and cancel():
                    self._push({"type": "pull_error", "model": name, "message": "已取消"})
                    return False
                status = ev.get("status", "")
                completed = ev.get("completed") or 0
                total = ev.get("total") or 0
                pct = int(completed / total * 100) if total else 0
                # 下载进度每 1% 推一次就够，否则 UI 线程会被刷爆
                if status == "downloading" and pct == last_pct:
                    continue
                last_pct = pct
                self._push(
                    {
                        "type": "pull",
                        "model": name,
                        "status": status,
                        "completed": completed,
                        "total": total,
                        "percent": pct,
                        "auto": auto,
                    }
                )
            return True
        except Exception as exc:  # noqa: BLE001
            self._push({"type": "pull_error", "model": name, "message": str(exc)})
            return False

    def _pull_worker(self, name: str) -> None:
        if not self._pull_with_progress(name, self._pull_cancel.is_set):
            return
        self._push({"type": "pull_done", "model": name})
        models = ollama.list_models()
        self._push({"type": "models", "models": models})
        if not self._model_name() and models:
            self.store.set_setting("model", name)
            self._push({"type": "settings", "settings": self._settings()})

    def cancel_pull(self) -> dict[str, Any]:
        self._pull_cancel.set()
        return {"ok": True}

    def delete_model(self, name: str) -> dict[str, Any]:
        try:
            ollama.delete_model(name)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        models = ollama.list_models()
        cur = self._model_name()
        if cur == name and models:
            self.store.set_setting("model", models[0]["name"])
        return {"ok": True, "models": models, "settings": self._settings()}

    # ------------------------------------------------------------ 知识库
    def list_documents(self) -> dict[str, Any]:
        try:
            docs = self.knowledge.list_documents()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "documents": docs}

    def import_document(self, path: str) -> dict[str, Any]:
        if not path:
            return {"ok": False, "error": "路径为空"}
        try:
            r = self.knowledge.import_file(path)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if r.get("ok"):
            docs = self.knowledge.list_documents()
            return {"ok": True, "documents": docs, "imported": r}
        return {"ok": False, "error": r.get("error", "导入失败")}

    def delete_document(self, doc_id: int) -> dict[str, Any]:
        try:
            self.knowledge.delete_document(int(doc_id))
            docs = self.knowledge.list_documents()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "documents": docs}

    def import_documents_dialog(self) -> dict[str, Any]:
        """用 pywebview 原生文件对话框选文件并导入（浏览器 file input 拿不到本地路径）。"""
        import webview

        try:
            files = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=(
                    (
                        "文档",
                        "*.txt;*.md;*.markdown;*.py;*.js;*.ts;*.tsx;*.jsx;*.json;"
                        "*.csv;*.log;*.yaml;*.yml;*.html;*.htm;*.css;*.java;*.go;*.rs;"
                        "*.c;*.cpp;*.cc;*.h;*.hpp;*.sh;*.bash;*.bat;*.ps1;*.sql;*.toml;"
                        "*.ini;*.cfg;*.docx;*.pdf;*.xlsx;*.xls",
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"无法打开文件对话框：{exc}"}
        if not files:
            return {"ok": True, "documents": self.knowledge.list_documents(), "imported": []}
        results: list[dict[str, Any]] = []
        for path in files:
            results.append(self.knowledge.import_file(path))
        docs = self.knowledge.list_documents()
        failed = [r for r in results if not r.get("ok")]
        return {
            "ok": not failed,
            "documents": docs,
            "imported": results,
            "error": "; ".join(r.get("error", "") for r in failed) if failed else None,
        }

    # ------------------------------------------------------------ 对话
    def send(self, session_id: str, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "内容为空"}
        try:
            model = self._require_model()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        if not ollama.is_running():
            return {"ok": False, "error": "Ollama 服务没启动"}
        if self._busy:
            return {"ok": False, "error": "还有一条正在生成"}

        session = self.store.get_session(session_id)
        if not session:
            return {"ok": False, "error": "会话不存在"}

        user_id = self.store.append_message(session_id, "user", text)
        assistant_id = self.store.append_message(session_id, "assistant", "")

        history = self.store.get_messages(session_id)
        history = [m for m in history if m["id"] != assistant_id]

        self._start_generation(session_id, assistant_id, model, history)
        return {
            "ok": True,
            "user_msg_id": user_id,
            "assistant_msg_id": assistant_id,
            "session_id": session_id,
        }

    def regenerate(self, session_id: str, assistant_msg_id: int) -> dict[str, Any]:
        """把这条回复和它之后的内容清掉，用同样的上下文重新生成。"""
        try:
            model = self._require_model()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        if self._busy:
            return {"ok": False, "error": "还有一条正在生成"}

        msgs = self.store.get_messages(session_id)
        history = [m for m in msgs if m["id"] < assistant_msg_id]
        # 最后一条必须是 assistant，否则说明点错了对象
        if not history or history[-1]["role"] != "user":
            return {"ok": False, "error": "找不到对应的提问"}
        self.store.delete_from(session_id, assistant_msg_id)
        self._start_generation(session_id, None, model, history)
        return {"ok": True, "assistant_msg_id": None, "regenerated": True}

    def stop(self) -> dict[str, Any]:
        self._cancel.set()
        return {"ok": True}

    def _start_generation(
        self,
        session_id: str,
        assistant_id: int | None,
        model: str,
        history: list[dict[str, Any]],
    ) -> None:
        """建好占位消息后开线程，立即返回让前端进入「正在生成」状态。"""
        if assistant_id is None:
            # 重新生成：先插一条空回复占位，拿到 id 再开始
            assistant_id = self.store.append_message(session_id, "assistant", "")

        settings = self._settings()
        persona = (settings.get("system_prompt") or "").strip()
        level = int(settings.get("thinking_level", 2) or 0)
        sys_parts: list[str] = []
        if persona:
            sys_parts.append(persona)
        instr = THINKING_INSTRUCTIONS.get(level)
        if instr:
            sys_parts.append(instr)
        sys_prompt = "\n\n".join(sys_parts)
        messages: list[dict[str, str]] = []
        # 文件控制：开启时注入能力说明与调用格式
        if settings.get("file_control_enabled"):
            sys_parts.append(FILE_CONTROL_INSTRUCTION)
        sys_prompt = "\n\n".join(sys_parts)

        messages: list[dict[str, str]] = []
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        for m in history:
            if m["role"] in ("user", "assistant", "system"):
                messages.append({"role": m["role"], "content": m["content"]})

        # 知识库：开启时用最后一条用户问题做检索，命中块作为额外 system 上下文注入
        sources: list[str] = []
        if settings.get("knowledge_enabled"):
            query = ""
            for m in reversed(messages):
                if m["role"] == "user":
                    query = m["content"]
                    break
            if query:
                try:
                    hits = self.knowledge.search(query, top_k=4)
                except Exception:  # noqa: BLE001
                    hits = []
                if hits:
                    sources = [h["doc"] for h in hits]
                    ctx = "\n\n".join(
                        f"[资料来源：{h['doc']}]\n{h['content']}" for h in hits
                    )
                    messages.insert(
                        0,
                        {
                            "role": "system",
                            "content": (
                                "你拥有一个本地知识库。以下是与用户问题相关的资料，"
                                "请优先依据这些资料作答，并适当注明来源；"
                                "资料未覆盖到的内容再用你的通用知识补充。\n\n" + ctx
                            ),
                        },
                    )

        options = {
            "temperature": float(settings.get("temperature", 0.7) or 0.7),
            "top_p": float(settings.get("top_p", 0.9) or 0.9),
            "num_ctx": int(settings.get("num_ctx", 8192) or 8192),
        }

        self._cancel.clear()
        self._busy = True
        self._push(
            {
                "type": "gen_start",
                "session_id": session_id,
                "msg_id": assistant_id,
                "model": model,
            }
        )
        if sources:
            self._push({"type": "kb_sources", "msg_id": assistant_id, "sources": sources})
        t = threading.Thread(
            target=self._generate_worker,
            args=(session_id, assistant_id, model, messages, options,
                  settings.get("keep_alive", "5m")),
            daemon=True,
        )
        t.start()

    def _generate_worker(
        self,
        session_id: str,
        assistant_id: int,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any],
        keep_alive: str | int,
    ) -> None:
        buf: list[str] = []
        full: list[str] = []
        stats: dict[str, Any] = {}
        last_flush = time.time()
        error: str | None = None

        try:
            for chunk in ollama.chat_stream(
                model,
                messages,
                options=options,
                cancel=self._cancel.is_set,
                stats_out=stats,
                keep_alive=keep_alive,
            ):
                if self._cancel.is_set():
                    break
                buf.append(chunk)
                full.append(chunk)
                now = time.time()
                if (
                    sum(len(x) for x in buf) >= FLUSH_MIN_CHARS
                    or now - last_flush >= FLUSH_INTERVAL
                ):
                    self._push(
                        {
                            "type": "token",
                            "msg_id": assistant_id,
                            "text": "".join(buf),
                        }
                    )
                    buf.clear()
                    last_flush = now
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            if buf:
                self._push({"type": "token", "msg_id": assistant_id, "text": "".join(buf)})
                buf.clear()

            text = "".join(full)
            cancelled = self._cancel.is_set()
            pretty = _fmt_stats(stats)

            # 检测 AI 是否请求执行文件操作（写操作需用户确认）
            file_op = _extract_file_op(text) if not cancelled and not error else None

            if file_op:
                # 只保留标记前的文本作为可见回复，操作请求通过事件推给前端确认
                visible_text = text.split("[[FILE_OP]]")[0].strip()
                self.store.update_message(assistant_id, visible_text, {**pretty, "model": model})
                preview = file_agent.preview_operation(file_op.get("op", ""), file_op.get("args", {}))
                self._push(
                    {
                        "type": "file_op_request",
                        "msg_id": assistant_id,
                        "session_id": session_id,
                        "op": file_op.get("op", ""),
                        "args": file_op.get("args", {}),
                        "reason": file_op.get("reason", ""),
                        "preview": preview,
                    }
                )
            elif not text.strip() and error is None:
                # 一个字都没吐出来（多半是被立刻停止了），删掉空占位
                self.store.delete_message(assistant_id)
                self._push({"type": "gen_empty", "msg_id": assistant_id})
            else:
                self.store.update_message(assistant_id, text, {**pretty, "model": model})
                self._push(
                    {
                        "type": "gen_done",
                        "msg_id": assistant_id,
                        "session_id": session_id,
                        "stats": pretty,
                        "model": model,
                        "cancelled": cancelled,
                        "error": error,
                    }
                )

            # 首轮对话结束后，用第一条提问自动命名会话
            try:
                session = self.store.get_session(session_id)
                if session and session["title"] == "新对话":
                    first_user = next(
                        (m["content"] for m in messages if m["role"] == "user"), ""
                    )
                    if first_user:
                        title = first_user.strip().replace("\n", " ")[:24]
                        self.store.rename_session(session_id, title)
                        self._push(
                            {
                                "type": "session_renamed",
                                "session_id": session_id,
                                "title": title,
                                "sessions": self.store.list_sessions(),
                            }
                        )
            except Exception:  # noqa: BLE001
                pass

            self._busy = False
            self._push({"type": "status", "state": "idle"})

    # ------------------------------------------------------------ 文档摘要
    def summarize_document(self, doc_id: int) -> dict[str, Any]:
        """JS API：后台生成某文档的中文摘要，结果通过 doc_summary 事件推送。

        不能直接在主线程跑 chat_stream，否则整段推理期间窗口「未响应」。
        """
        threading.Thread(target=self._summarize_worker, args=(int(doc_id),), daemon=True).start()
        return {"ok": True, "started": True}

    def _summarize_worker(self, doc_id: int) -> None:
        try:
            text = self.knowledge.get_document_text(doc_id)
        except Exception as exc:  # noqa: BLE001
            self._push({"type": "doc_summary", "doc_id": doc_id, "error": f"读取文档失败：{exc}"})
            return
        if not text.strip():
            self._push({"type": "doc_summary", "doc_id": doc_id, "error": "文档内容为空"})
            return
        text = text[:6000]
        model = self._model_name()
        if not model:
            self._push({"type": "doc_summary", "doc_id": doc_id, "error": "请先在左上角选择模型"})
            return
        try:
            messages = [
                {
                    "role": "system",
                    "content": "你是文档摘要助手。请用简洁中文分条总结下面文档的核心要点，"
                    "只基于给定内容，不要编造；若文档较长，抓重点。",
                },
                {"role": "user", "content": text},
            ]
            parts: list[str] = []
            for ch in ollama.chat_stream(
                model,
                messages,
                options={"temperature": 0.3, "num_ctx": 8192},
                keep_alive="5m",
            ):
                parts.append(ch)
            summary = "".join(parts)
        except Exception as exc:  # noqa: BLE001
            self._push({"type": "doc_summary", "doc_id": doc_id, "error": f"生成摘要失败：{exc}"})
            return
        self._push({"type": "doc_summary", "doc_id": doc_id, "summary": summary})

    # ------------------------------------------------------------ 表格分析
    def import_sheet_dialog(self) -> dict[str, Any]:
        import webview

        try:
            files = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=(("表格文件", "*.csv;*.xlsx;*.xls"),),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"无法打开文件对话框：{exc}"}
        if not files:
            return {"ok": True, "cancelled": True}
        return self._load_sheet(files[0])

    def _load_sheet(self, path: str) -> dict[str, Any]:
        try:
            from .sheets import load_table

            table = load_table(path)
        except ImportError as exc:
            return {"ok": False, "error": f"解析表格需要 openpyxl：{exc}。请先 pip install openpyxl。"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取失败：{exc}"}
        if table.n_rows == 0:
            return {"ok": False, "error": "表格为空"}
        sheet_id = str(int(time.time() * 1000))
        self._sheets[sheet_id] = table
        self._current_sheet_id = sheet_id
        return {
            "ok": True,
            "sheet_id": sheet_id,
            "name": table.name,
            "n_rows": table.n_rows,
            "n_cols": table.n_cols,
            "headers": table.headers,
            "stats": table.column_stats(),
            "preview": table.preview(),
        }

    def analyze_sheet(self, question: str) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            return {"ok": False, "error": "问题为空"}
        table = self._sheets.get(self._current_sheet_id) if self._current_sheet_id else None
        if not table:
            return {"ok": False, "error": "请先导入一个表格"}
        try:
            model = self._require_model()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        if not ollama.is_running():
            return {"ok": False, "error": "Ollama 服务没启动"}
        if self._sheet_busy:
            return {"ok": False, "error": "上一次分析还没结束"}

        context = table.to_context()
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个数据分析助手。下面是一份表格的结构化信息（列统计 + 前若干行数据）。"
                    "用户会针对这份表格提问，请基于给定数据用中文回答，可以给出分析思路、"
                    "Excel / Python 处理建议或具体公式；不要编造数据中不存在的内容。"
                    "若需要计算，请明确说明计算方式。\n\n" + context
                ),
            },
            {"role": "user", "content": question},
        ]
        options = {"temperature": 0.3, "num_ctx": 8192}
        self._sheet_cancel.clear()
        self._sheet_busy = True
        self._push({"type": "sheet_start", "sheet_id": self._current_sheet_id})
        t = threading.Thread(
            target=self._sheet_worker,
            args=(self._current_sheet_id, model, messages, options),
            daemon=True,
        )
        t.start()
        return {"ok": True, "started": True}

    def _sheet_worker(self, sheet_id, model, messages, options) -> None:
        buf: list[str] = []
        full: list[str] = []
        last = time.time()
        error: str | None = None
        try:
            for ch in ollama.chat_stream(
                model,
                messages,
                options=options,
                cancel=self._sheet_cancel.is_set,
                keep_alive="5m",
            ):
                if self._sheet_cancel.is_set():
                    break
                buf.append(ch)
                full.append(ch)
                now = time.time()
                if len("".join(buf)) >= FLUSH_MIN_CHARS or now - last >= FLUSH_INTERVAL:
                    self._push(
                        {"type": "sheet_token", "sheet_id": sheet_id, "text": "".join(buf)}
                    )
                    buf.clear()
                    last = now
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            if buf:
                self._push(
                    {"type": "sheet_token", "sheet_id": sheet_id, "text": "".join(buf)}
                )
            self._sheet_busy = False
            self._push(
                {
                    "type": "sheet_done",
                    "sheet_id": sheet_id,
                    "text": "".join(full),
                    "error": error,
                }
            )

    # ------------------------------------------------------------ 日常自动化
    def pick_folder_dialog(self) -> dict[str, Any]:
        import webview

        try:
            folder = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"无法打开目录对话框：{exc}"}
        if not folder:
            return {"ok": True, "cancelled": True}
        path = folder[0] if isinstance(folder, (list, tuple)) else folder
        return {"ok": True, "path": path}

    def automation_scan(self, path: str) -> dict[str, Any]:
        from . import automation

        return automation.scan_dir(path)

    def automation_archive(self, path: str) -> dict[str, Any]:
        from . import automation

        plan = automation.plan_archive(path)
        if not plan.get("ok"):
            return plan
        return automation.apply_moves(path, plan["moves"])

    def automation_preview_rename(self, path: str, mode: str, params: dict) -> dict[str, Any]:
        from . import automation

        return automation.plan_rename(path, mode, **(params or {}))

    def automation_apply_rename(self, path: str, renames: list) -> dict[str, Any]:
        from . import automation

        return automation.apply_renames(path, renames)

    def automation_convert(self, path: str, target: str) -> dict[str, Any]:
        from . import automation

        fc = automation.find_fileconverter()
        if not fc:
            return {
                "ok": False,
                "error": "没找到 file-converter 项目（预期在 ~/WorkBuddy/file-converter）。",
            }
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(fc, "main.py"), "convert", path, "--to", target],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"调用失败：{exc}"}
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    # ------------------------------------------------------------ 文件控制（可开关，需免责声明）
    def file_control_status(self) -> dict[str, Any]:
        s = self._settings()
        return {
            "ok": True,
            "enabled": bool(s.get("file_control_enabled")),
            "accepted": bool(s.get("file_control_disclaimer_accepted")),
            "log": file_agent.read_log(),
        }

    def accept_file_control_disclaimer(self) -> dict[str, Any]:
        """用户同意免责声明后持久化标记。"""
        self.store.set_setting("file_control_disclaimer_accepted", True)
        # 如果当前设置里没开启，也一并开启
        if not self._settings().get("file_control_enabled"):
            self.store.set_setting("file_control_enabled", True)
        return {"ok": True, "settings": self._settings()}

    def apply_file_op(self, session_id: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """JS API：后台执行文件操作。主线程立即返回，结果通过 file_op_done 事件推送。

        subprocess 可能跑几秒（尤其是 execute_command 执行 python），必须在后台线程，
        否则用户点「确认」后界面会卡住。
        """
        s = self._settings()
        if not s.get("file_control_enabled"):
            return {"ok": False, "error": "文件控制功能未开启"}
        if not s.get("file_control_disclaimer_accepted"):
            return {"ok": False, "error": "请先同意免责声明"}
        threading.Thread(
            target=self._apply_file_op_worker,
            args=(session_id, op, args),
            daemon=True,
        ).start()
        return {"ok": True, "started": True}

    def _apply_file_op_worker(self, session_id: str, op: str, args: dict[str, Any]) -> None:
        try:
            result = file_agent.apply_operation(op, args)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"执行失败：{exc}"}

        # 把操作结果作为 system 消息追加到当前会话，便于后续上下文理解
        try:
            status = "成功" if result.get("ok") else "失败"
            summary = f"[文件操作 {status}] {op}({json.dumps(args, ensure_ascii=False)}): {result.get('message', result.get('error', ''))}"
            self.store.append_message(session_id, "system", summary)
        except Exception:  # noqa: BLE001
            pass

        self._push({"type": "file_op_done", "result": result, "settings": self._settings()})

    def clear_file_control_log(self) -> dict[str, Any]:
        log_file = os.path.join(data_dir(), "logs", "file_agent.log")
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                f.write("")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True}
