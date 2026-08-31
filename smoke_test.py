import inspect
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pc_agent as pa
import gui

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        ok = False

sig = inspect.signature(pa.run_agent)
check("run_agent 含 pc_access", "pc_access" in sig.parameters)
check("run_agent 含 files", "files" in sig.parameters)

check("PC_ACCESS 默认 False", pa.PC_ACCESS is False)
pa.set_pc_access(True)
check("set_pc_access(True) 生效", pa.PC_ACCESS is True)
pa.set_pc_access(False)

check("RESTRICTED_TOOLS 非空", len(pa.RESTRICTED_TOOLS) > 0)
check("get_models 可调用", callable(pa.get_models))

tf = os.path.join(tempfile.gettempdir(), "smoke_attach.txt")
with open(tf, "w", encoding="utf-8") as f:
    f.write("hello file content")
res = pa.read_attachment(tf)
check("read_attachment 返回文本", "hello file content" in res)

api_methods = [m for m in dir(gui.AgentAPI)
               if not m.startswith("_") and callable(getattr(gui.AgentAPI, m))]
needed = {"ping", "get_models", "set_model", "set_pc_access",
          "pick_files", "pick_folder", "open_folder",
          "get_settings", "save_settings", "new_task", "chat",
          "bind_window", "on_page_loaded",
          "list_automations", "save_automation", "delete_automation", "toggle_automation",
          "check_ollama", "open_ollama_download_install"}
check("AgentAPI 公开方法齐全", needed.issubset(set(api_methods)))

print("\nRESULT:", "ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
