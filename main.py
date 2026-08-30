"""本地 AI 对话 —— pywebview 桌面端入口。

运行：  python main.py
打包：  python build_exe.py
"""

from __future__ import annotations

import os
import sys

import webview

from core.api import Api


def resource_path(rel: str) -> str:
    """定位打包后的资源文件。

    - onefile：解压在 sys._MEIPASS
    - onedir：datas 落在程序目录下的 _internal
    - 源码运行：用脚本所在目录
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "") or os.path.join(
            os.path.dirname(os.path.abspath(sys.executable)), "_internal"
        )
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def main() -> None:
    api = Api()

    window = webview.create_window(
        title="本地 AI 对话",
        url=resource_path(os.path.join("ui", "index.html")),
        js_api=api,
        width=1200,
        height=780,
        min_size=(920, 600),
        background_color="#0f1115",
        confirm_close=False,
        text_select=True,
    )
    api.bind(window)

    debug = os.environ.get("AICHAT_DEBUG") == "1"
    webview.start(debug=debug, private_mode=False, gui="edgechromium")


if __name__ == "__main__":
    main()
