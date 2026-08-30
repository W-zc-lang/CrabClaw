"""打包成「自带本地 AI」的可分发文件夹。

前置：
    pip install pywebview PyInstaller
    Windows 11 自带 WebView2 运行时，无需额外安装
    （可选）本机已安装 Ollama，会把 ollama.exe 一起打进包里

用法：
    python build_exe.py
成品：  dist/LocalAIChat/LocalAIChat.exe  （内含 ollama.exe 与 Modelfile）

首次运行时若没有 local-assistant 模型，软件会：
    1) 自动启动内置 ollama，并把模型存到程序目录下的 models/
    2) 联网拉取基础模型 qwen2.5:3b（约 2GB，仅首次）
    3) 用内置 Modelfile 自动创建 local-assistant
之后完全离线可用。
"""

import os
import shutil
import sys

import PyInstaller.__main__

HERE = os.path.dirname(os.path.abspath(__file__))


def find_ollama_exe() -> str | None:
    """构建时把本机 ollama.exe 一起打进包；找不到就跳过（仍可联网后手动装）。"""
    if sys.platform != "win32":
        for p in ("/usr/local/bin/ollama", "/usr/bin/ollama"):
            if os.path.isfile(p):
                return p
        return None
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local, "Programs", "Ollama", "ollama.exe"),
        r"C:\Program Files\Ollama\ollama.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def main() -> None:
    ollama_bin = find_ollama_exe()
    add_binary = []
    if ollama_bin:
        # Windows 用分号分隔；把 ollama.exe 放到包根目录
        add_binary = ["--add-binary", f"{ollama_bin};."]
        print(f"[build] 已找到 ollama.exe：{ollama_bin}")
    else:
        print("[build] 未找到 ollama.exe，打包将不含它。")
        print("        用户需自行安装 Ollama，或把 ollama.exe 放到程序目录。")

    # 旧产物由 PyInstaller 的 --clean / --noconfirm 自动处理，无需手动删

    cmd = [
        "main.py",
        "--name",
        "LocalAIChat",
        "--noconfirm",
        "--clean",
        "--windowed",  # 无控制台窗口
        "--add-data",
        "ui;ui",  # Windows 用分号；macOS/Linux 换成 ui:ui
        # 注意：dest 用 "." 让文件直接落到 _internal 根并保持原名；
        # 若写 "Modelfile"（无扩展名）PyInstaller 会把它当成目录，变成 _internal/Modelfile/Modelfile
        "--add-data",
        "assets/Modelfile;.",  # 内置模型定义：小墨（通用助手）
        "--add-data",
        "assets/Modelfile.zc;.",  # 内置模型定义：子成（专属 AI）
    ]
    if add_binary:
        cmd += add_binary

    cmd += [
        "--hidden-import",
        "core",
        "--hidden-import",
        "core.api",
        "--hidden-import",
        "core.ollama",
        "--hidden-import",
        "core.store",
        "--hidden-import",
        "core.knowledge",
        "--hidden-import",
        "core.sheets",
        "--hidden-import",
        "core.automation",
        "--hidden-import",
        "core.file_agent",
        # pywebview 会被动态加载平台后端，必须显式收集，否则 exe 报 No module named 'webview'
        "--hidden-import",
        "webview",
        "--hidden-import",
        "webview.platforms.edgechromium",
        "--collect-all",
        "webview",
    ]

    print("[build] 开始打包（onedir 模式，首次较慢）…")
    PyInstaller.__main__.run(cmd)
    print("[build] 完成。产物在 dist/LocalAIChat/ ，双击 LocalAIChat.exe 即可。")


if __name__ == "__main__":
    main()
