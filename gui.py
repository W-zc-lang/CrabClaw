# -*- coding: utf-8 -*-
"""
gui.py — 给本地 Ollama Agent 套一个 pywebview 聊天界面

运行：
    pip install pywebview        # Windows 需已装 WebView2 Runtime（Win10/11 一般自带）
    python gui.py

前端在 index.html，后端 Agent 逻辑复用 pc_agent.py，两者通过：
    window.pywebview.api.chat(task, model, pc_access, files)   前端 → 后端
    window.pywebview.api.get_models() / get_settings() / ...   前端 → 后端
    window.evaluate_js("onAgentEvent(...)")                    后端 → 前端（实时推送工具调用进度）
"""

import datetime
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

import requests
import webview

import pc_agent

# --windowed 打包后 stdout/stderr 全被丢弃，pywebview 内部的 logger.exception 会完全静默。
# 落一份文件日志，出问题时可直接查看（含 pywebview 自身的 API 注入异常）。
LOG_PATH = os.path.join(tempfile.gettempdir(), "CrabClaw.log")
logging.basicConfig(
    filename=LOG_PATH,
    filemode="w",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("CrabClaw")

# 让 PyInstaller 静态分析到可选依赖（运行时仍容错，缺失只影响对应工具）
for _m in ("mss", "pyautogui", "psutil"):
    try:
        __import__(_m)
    except Exception:
        pass

# 默认设置（缺项时回退，保证向前兼容）
DEFAULT_SETTINGS = {
    "model": pc_agent.DEFAULT_MODEL,
    "pc_access": False,
    "ollama_host": pc_agent.OLLAMA_HOST,
    "theme": "light",
    "language": "zh-CN",
    "storage_path": str(pc_agent.SAFE_ROOTS[0]),
    "safe_roots": [str(r) for r in pc_agent.SAFE_ROOTS],
    # 文生图配置（不内置大模型，调用本机/云端推理服务）
    "image_provider": "",
    "image_openai_key": "",
    "image_stability_key": "",
    "image_local_url": "",
    # GitHub 自动发布
    "github_org": "W-zc-lang",
    # LLM Provider（接入非本地 AI）：ollama / openai / anthropic
    "llm_provider": "ollama",
    "llm_api_key": "",
    "llm_base_url": "",
}


def _settings_path():
    # 打包后 sys.executable 即 exe 自身路径；开发态回退到脚本目录
    return os.path.join(_app_dir(), "settings.json")


def _app_dir():
    # 打包后 exe 所在目录；开发态为脚本所在目录
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _workspaces_dir():
    p = os.path.join(_app_dir(), "workspaces")
    try:
        os.makedirs(p, exist_ok=True)
    except Exception as e:
        log.warning("create workspaces dir failed: %s", e)
    return p


def _automations_path():
    return os.path.join(_app_dir(), "automations.json")


def _powershell_pick_folder(initial_path=""):
    """PowerShell 文件夹浏览对话框（pywebview 对话框不可用时的兜底）。"""
    import tempfile

    initial = (initial_path or "").replace('"', '`"')
    script = (
        'Add-Type -AssemblyName System.Windows.Forms\n'
        '$f = New-Object System.Windows.Forms.FolderBrowserDialog\n'
        '$f.Description = "选择 小螃蟹 CrabClaw 数据存储位置"\n'
        'if (Test-Path "%s") { $f.SelectedPath = "%s" }\n'
        '$ret = $f.ShowDialog()\n'
        'if ($ret -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.SelectedPath }\n'
    ) % (initial, initial)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8")
    tmp.write(script)
    tmp.close()
    # 在部分受限 shell（如 Git Bash）里可能找不到 powershell，优先用 .exe 绝对路径尝试
    ps_cmds = []
    for base in (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                 r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"):
        if os.path.exists(base):
            ps_cmds.append(base)
    ps_cmds += ["powershell.exe", "powershell", "pwsh.exe", "pwsh"]

    last_err = None
    try:
        for cmd in ps_cmds:
            try:
                p = subprocess.Popen(
                    [cmd, "-WindowStyle", "Hidden", "-NoProfile",
                     "-ExecutionPolicy", "Bypass", "-File", tmp.name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                out, err = p.communicate()
                out = out.strip()
                if out and os.path.isdir(out):
                    return {"ok": True, "path": out}
                last_err = err.strip() if err else "未选择文件夹"
                break
            except FileNotFoundError:
                continue
            except Exception as e:
                last_err = str(e)
                continue
        return {"ok": False, "error": last_err or "未选择文件夹"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        try:
            os.remove(tmp.name)
        except Exception:
            pass


def _sanitize_name(name, default="workspace"):
    if not name:
        return default
    name = str(name).strip()
    name = "".join(c for c in name if c.isalnum() or c in " _-\u4e00-\u9fff")
    name = name.strip()[:40]
    return name or default


def _load_automations_raw():
    p = _automations_path()
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        log.warning("load automations failed: %s", e)
    return []


def _save_automations_raw(items):
    p = _automations_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        log.warning("save automations failed: %s", e)
        return {"ok": False, "error": str(e)}


class _AutomationScheduler:
    """极简自动化调度器：每分钟扫描一次启用的任务，到点触发。"""

    def __init__(self, api):
        self.api = api
        self._timer = None
        self._lock = threading.Lock()
        self._last_triggered = {}  # id -> 日期字符串，防止同一任务一天内多次触发
        self.start()

    def start(self):
        self._schedule_next()

    def stop(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def _schedule_next(self):
        with self._lock:
            self._timer = threading.Timer(60.0, self._tick)
            self._timer.daemon = True
            self._timer.start()

    def _tick(self):
        try:
            self._run_due()
        finally:
            self._schedule_next()

    def _run_due(self):
        items = _load_automations_raw()
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        for item in items:
            if not item.get("enabled"):
                continue
            sched = item.get("schedule") or {}
            stype = sched.get("type", "cycle")
            try:
                if stype == "once":
                    t = datetime.datetime.fromisoformat(sched.get("time", ""))
                    if now >= t and self._last_triggered.get(item["id"]) != today_str:
                        self._trigger(item)
                        self._last_triggered[item["id"]] = today_str
                        # 单次执行后自动停用
                        item["enabled"] = False
                elif stype == "cycle":
                    t = datetime.datetime.strptime(sched.get("time", "00:00"), "%H:%M").time()
                    if now.time() >= t and self._last_triggered.get(item["id"]) != today_str:
                        unit = sched.get("unit", "daily")
                        if unit == "daily" or (unit == "weekly" and now.weekday() == 0) or (unit == "monthly" and now.day == 1):
                            self._trigger(item)
                            self._last_triggered[item["id"]] = today_str
                elif stype == "interval":
                    last = self._last_triggered.get(item["id"])
                    num = int(sched.get("num", 1))
                    unit = sched.get("unit", "hour")
                    delta = datetime.timedelta(hours=num) if unit == "hour" else datetime.timedelta(minutes=num) if unit == "minute" else datetime.timedelta(days=num)
                    if last is None:
                        # 首次启用时按创建时间偏移，避免立即触发
                        created = item.get("created_at", now.isoformat())
                        try:
                            base = datetime.datetime.fromisoformat(created)
                        except Exception:
                            base = now
                        self._last_triggered[item["id"]] = base.isoformat()
                    else:
                        last_dt = datetime.datetime.fromisoformat(last)
                        if now - last_dt >= delta:
                            self._trigger(item)
                            self._last_triggered[item["id"]] = now.isoformat()
            except Exception as e:
                log.warning("schedule check failed for %s: %s", item.get("id"), e)
        _save_automations_raw(items)

    def _trigger(self, item):
        log.info("trigger automation: %s", item.get("name"))
        try:
            self.api.chat(
                item.get("prompt", ""),
                model=item.get("model") or self.api._settings.get("model"),
                pc_access=item.get("pc_access", False),
                files=None,
            )
        except Exception as e:
            log.exception("automation trigger failed")


AUTOMATION_SCHEDULER = None


def _sync_external_config(cfg: dict):
    """把图像生成 / GitHub / LLM provider 等外部配置注入 pc_agent 全局，供工具函数使用。"""
    pc_agent.GITHUB_ORG = cfg.get("github_org") or "W-zc-lang"
    pc_agent.configure({
        "image_provider": cfg.get("image_provider", ""),
        "image_openai_key": cfg.get("image_openai_key", ""),
        "image_stability_key": cfg.get("image_stability_key", ""),
        "image_local_url": cfg.get("image_local_url", ""),
        # LLM provider
        "llm_provider": cfg.get("llm_provider", "ollama"),
        "llm_api_key": cfg.get("llm_api_key", ""),
        "llm_base_url": cfg.get("llm_base_url", ""),
    })


def load_settings():
    p = _settings_path()
    cfg = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    except Exception as e:
        log.warning("load settings failed: %s", e)
    # 同步到 pc_agent 全局
    pc_agent.OLLAMA_HOST = cfg["ollama_host"]
    pc_agent.set_pc_access(cfg["pc_access"])
    _sync_external_config(cfg)
    return cfg


def save_settings(cfg: dict):
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in (cfg or {}).items() if k in DEFAULT_SETTINGS})
    p = _settings_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save settings failed: %s", e)
        return {"ok": False, "error": str(e)}
    # 同步到 pc_agent 全局
    pc_agent.OLLAMA_HOST = merged["ollama_host"]
    pc_agent.set_pc_access(merged["pc_access"])
    _sync_external_config(merged)
    return {"ok": True, "settings": merged}


def _resource_path(rel):
    # PyInstaller onefile 会把资源解压到 sys._MEIPASS；开发态回退到脚本目录
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


HTML_PATH = _resource_path("index.html")
ICON_PATH = _resource_path("icon.ico") if os.path.exists(_resource_path("icon.ico")) else _resource_path("icon.png")


class AgentAPI:
    """暴露给 JS 的 API。

    ⚠️ 关键约束：所有实例属性必须以 `_` 开头。
    pywebview 6.x 的 util.py:get_functions() 会遍历本对象所有「非下划线开头」的属性，
    遇到非 callable 对象还会**递归深入**。若把 Window 对象存成 self.window，
    它会递归进 Window → .NET/COM 绑定树，导致 generate_func() 抛异常，
    结果 finish.js 永不注入 → 前端 window.pywebview.api 恒为空 {} →
    报 "api.chat is not a function"，而窗口本身看起来完全正常（异常被静默吞掉）。
    """

    def __init__(self):
        self._window = None
        self._settings = load_settings()

    # webview.start 会在窗口就绪前调用一次，用来绑定窗口与确认回调
    def bind_window(self, window):
        self._window = window
        pc_agent.CONFIRM_FUNC = self._confirm  # 危险操作走 GUI 弹窗
        log.info("window bound, CONFIRM_FUNC installed")

    # 页面加载完成后推送当前模型名。
    # Event.set() 会先尝试 func(window, *args)，故用 *args 兼容两种签名。
    def on_page_loaded(self, *args):
        log.info("page loaded")
        if self._window:
            self._window.evaluate_js("setModel('%s')" % self._settings["model"])

    def _confirm(self, prompt):
        if not self._window:
            return False
        try:
            return bool(self._window.create_confirmation_dialog("需要确认", prompt))
        except Exception as e:
            # 某些环境子线程弹窗不可用，保守拒绝而非放行
            log.warning("confirm dialog failed: %s", e)
            return False

    # ---- 供前端自检：确认 API 通路真的活着 ----
    def ping(self):
        return {"ok": True, "model": self._settings["model"], "log": LOG_PATH}

    # ---- Ollama / 远程 LLM 启动检测（按 provider 路由）----
    def check_ollama(self):
        return pc_agent.check_llm()

    # ---- 打开 Ollama 安装包下载页 ----
    def open_ollama_download_install(self):
        url = "https://ollama.com/download"
        try:
            webbrowser.open(url)
            return {"ok": True, "url": url}
        except Exception as e:
            log.warning("open_ollama_download failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ---- 模型列表：按 provider 拉取 ----
    def get_models(self):
        try:
            current = self._settings["model"]
            provider = pc_agent.LLM_PROVIDER or "ollama"
            installed = pc_agent.list_models()
            # 合并已拉取模型与推荐模型（仅 ollama 有推荐意义），优先展示已拉取
            seen = set()
            models = []
            for name in installed:
                if name and name not in seen:
                    seen.add(name)
                    models.append({"value": name, "text": pc_agent.model_display_name(name)})
            if provider == "ollama":
                for rec in pc_agent.RECOMMENDED_MODELS:
                    name = rec["name"]
                    if name not in seen:
                        seen.add(name)
                        models.append({"value": name, "text": pc_agent.model_display_name(name)})
            # 当前模型若未出现在列表里，补一项避免下拉框丢失选择
            if current and current not in seen:
                models.insert(0, {"value": current, "text": current})
            if not models:
                models = [{"value": current, "text": current}]
            return {"ok": True, "models": models, "current": current, "provider": provider}
        except Exception as e:
            return {"ok": False, "error": str(e), "models": [{"value": current, "text": current}], "provider": pc_agent.LLM_PROVIDER}

    # ---- 切换并持久化模型 ----
    def set_model(self, model):
        self._settings["model"] = model
        save_settings(self._settings)
        return {"ok": True, "model": model}

    # ---- 电脑访问权限开关 ----
    def set_pc_access(self, enabled):
        self._settings["pc_access"] = bool(enabled)
        save_settings(self._settings)
        pc_agent.set_pc_access(bool(enabled))
        return {"ok": True, "pc_access": bool(enabled)}

    # ---- 文件选择（多选）----
    def pick_files(self):
        if not self._window:
            return []
        try:
            files = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
            return list(files) if files else []
        except Exception as e:
            log.warning("pick_files failed: %s", e)
            return []

    # ---- 文件夹选择：设置页数据存储位置 ----
    def pick_folder(self, initial_path=""):
        if self._window:
            try:
                result = self._window.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
                if result and isinstance(result, (list, tuple)) and result[0]:
                    return {"ok": True, "path": result[0]}
            except Exception as e:
                log.warning("pywebview folder dialog failed: %s", e)
        # 兜底：PowerShell 对话框
        return _powershell_pick_folder(initial_path or self._settings.get("storage_path") or str(pc_agent.SAFE_ROOTS[0]))

    # ---- 打开当前/指定存储目录 ----
    def open_folder(self, path=""):
        target = path or self._settings.get("storage_path") or str(pc_agent.SAFE_ROOTS[0])
        try:
            os.makedirs(target, exist_ok=True)
            subprocess.Popen(["explorer", target], shell=True)
            return {"ok": True, "path": target}
        except Exception as e:
            log.warning("open_folder failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ---- 下载按钮：跳转 Ollama 模型库（用系统默认浏览器打开）----
    def open_model_download(self):
        import webbrowser

        url = "https://ollama.com/library"
        try:
            webbrowser.open(url)
            return {"ok": True, "url": url}
        except Exception as e:
            log.warning("open_model_download failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ---- 在软件内真正下载模型：执行 ollama pull 并实时推送进度 ----
    def download_model(self, name):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "未提供模型名"}
        # 放到子线程跑，避免阻塞 UI 线程
        threading.Thread(target=self._run_download, args=(name,), daemon=True).start()
        return {"ok": True}

    def _run_download(self, name):
        import subprocess

        self._push_download(name, "开始执行：ollama pull %s\n" % name)
        try:
            proc = subprocess.Popen(
                ["ollama", "pull", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            for line in proc.stdout:
                self._push_download(name, line)
            rc = proc.wait()
            if rc == 0:
                self._push_download(name, "\n✅ 下载完成。可在顶部模型下拉框中选择使用。", done=True)
            else:
                self._push_download(name, "\n❌ 下载失败（退出码 %d）。请确认 Ollama 已启动且可联网。" % rc, done=True)
        except FileNotFoundError:
            self._push_download(name, "\n❌ 未找到 ollama 命令，请先安装 Ollama 并加入 PATH。", done=True)
        except Exception as e:
            log.exception("download_model failed")
            self._push_download(name, "\n❌ 下载异常：" + str(e), done=True)

    def _push_download(self, name, text, done=False):
        if not self._window:
            return
        try:
            self._window.evaluate_js(
                "onDownload(%s)" % json.dumps({"name": name, "text": text, "done": done}, ensure_ascii=False)
            )
        except Exception as e:
            log.warning("push download failed: %s", e)

    # ---- 设置读写 ----
    def get_settings(self):
        return {"ok": True, "settings": self._settings}

    def save_settings(self, cfg):
        self._settings = save_settings(cfg).get("settings", self._settings)
        return {"ok": True, "settings": self._settings}

    # ---- 新建任务：前端自行清空即可，这里仅用于把后端状态复位 ----
    def new_task(self):
        return {"ok": True}

    # ---- 工作空间：在软件目录下的 workspaces 中管理 ----
    def list_workspaces(self):
        ws = _workspaces_dir()
        try:
            items = []
            for n in os.listdir(ws):
                p = os.path.join(ws, n)
                if os.path.isdir(p):
                    items.append({"name": n, "path": p, "mtime": os.path.getmtime(p)})
            items.sort(key=lambda x: x["mtime"], reverse=True)
            return {"ok": True, "path": ws, "workspaces": [x["name"] for x in items]}
        except Exception as e:
            return {"ok": False, "error": str(e), "path": ws, "workspaces": []}

    def create_workspace(self, name=None):
        ws = _workspaces_dir()
        base = _sanitize_name(name, "workspace")
        folder = os.path.join(ws, base)
        suffix = 0
        while os.path.exists(folder):
            suffix += 1
            folder = os.path.join(ws, f"{base}_{suffix}")
        try:
            os.makedirs(folder, exist_ok=True)
            subprocess.Popen(["explorer", folder], shell=True)
            return {"ok": True, "path": folder, "name": os.path.basename(folder)}
        except Exception as e:
            log.warning("create_workspace failed: %s", e)
            return {"ok": False, "error": str(e)}

    def open_workspace_folder(self, name):
        ws = _workspaces_dir()
        folder = os.path.join(ws, _sanitize_name(name, "")) if name else ws
        if name and not os.path.isdir(folder):
            return {"ok": False, "error": "工作空间不存在"}
        try:
            subprocess.Popen(["explorer", folder], shell=True)
            return {"ok": True, "path": folder}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_workspaces_root(self):
        ws = _workspaces_dir()
        try:
            subprocess.Popen(["explorer", ws], shell=True)
            return {"ok": True, "path": ws}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 兼容旧入口：为当前任务创建工作空间
    def open_workspace(self, title):
        return self.create_workspace(title)

    # ---- 自动化任务 CRUD ----
    def list_automations(self):
        return {"ok": True, "automations": _load_automations_raw()}

    def save_automation(self, data):
        items = _load_automations_raw()
        now = datetime.datetime.now().isoformat()
        aid = data.get("id")
        if aid:
            for i, it in enumerate(items):
                if it.get("id") == aid:
                    it.update(data)
                    it["updated_at"] = now
                    items[i] = it
                    _save_automations_raw(items)
                    return {"ok": True, "automation": it}
        # 新建
        new_item = dict(data)
        new_item["id"] = "auto_" + str(int(time.time() * 1000))
        new_item["created_at"] = now
        new_item["updated_at"] = now
        new_item.setdefault("enabled", True)
        items.append(new_item)
        res = _save_automations_raw(items)
        if not res["ok"]:
            return res
        return {"ok": True, "automation": new_item}

    def delete_automation(self, aid):
        items = _load_automations_raw()
        items = [it for it in items if it.get("id") != aid]
        _save_automations_raw(items)
        return {"ok": True}

    def toggle_automation(self, aid, enabled):
        items = _load_automations_raw()
        for it in items:
            if it.get("id") == aid:
                it["enabled"] = bool(enabled)
                it["updated_at"] = datetime.datetime.now().isoformat()
                break
        _save_automations_raw(items)
        return {"ok": True}

    def chat(self, task, model=None, pc_access=None, files=None):
        task = (task or "").strip()
        if not task:
            return
        if model:
            self._settings["model"] = model
        if pc_access is not None:
            self._settings["pc_access"] = bool(pc_access)
            pc_agent.set_pc_access(bool(pc_access))
        use_model = model or self._settings["model"]
        use_access = pc_access if pc_access is not None else self._settings["pc_access"]
        log.info("chat task=%s model=%s pc_access=%s files=%s",
                 task, use_model, use_access, files)
        # 在子线程跑 Agent，避免阻塞 UI 主线程
        threading.Thread(
            target=self._run, args=(task, use_model, use_access, files), daemon=True
        ).start()

    def _run(self, task, model, pc_access, files):
        def on_event(ev):
            if self._window:
                try:
                    self._window.evaluate_js(
                        "onAgentEvent(%s)" % json.dumps(ev, ensure_ascii=False)
                    )
                except Exception as e:
                    log.warning("evaluate_js failed: %s", e)

        try:
            pc_agent.run_agent(
                task, model, mode="tools",
                verbose=False, on_event=on_event,
                pc_access=pc_access, files=files,
            )
        except Exception as e:
            log.exception("run_agent failed")
            on_event({"type": "error", "text": "%s: %s" % (type(e).__name__, e)})


def main():
    log.info("starting, html=%s, frozen=%s", HTML_PATH, getattr(sys, "frozen", False))
    api = AgentAPI()
    global AUTOMATION_SCHEDULER
    AUTOMATION_SCHEDULER = _AutomationScheduler(api)
    window = webview.create_window(
        "CrabClaw · Ollama Agent",
        url=HTML_PATH,
        js_api=api,
        width=1000,
        height=740,
    )
    window.events.loaded += api.on_page_loaded
    # 不指定 gui：Windows 下自动选 winforms + WebView2(edgechromium)。
    # 注意 pywebview 6.x 合法值只有 qt/gtk/cef/mshtml/edgechromium/android/cocoa，
    # 传 "edge" 是无效值（会被静默忽略）。
    webview.start(api.bind_window, window, debug=("--debug" in sys.argv))


if __name__ == "__main__":
    main()
