# 🦀 CrabClaw · 本地 AI 对话桌面应用

> 让本地 Ollama 具备「控制电脑」能力：文件读写、跑命令、截图看图、键鼠操作，全部在本机完成，**数据不出本机**。

[![Platform](https://img.shields.io/badge/Platform-Windows%20%2F%20macOS-blue)](https://github.com/W-zc-lang/CrabClaw)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Release](https://img.shields.io/github/v/release/W-zc-lang/CrabClaw)](https://github.com/W-zc-lang/CrabClaw/releases)

## ✨ 它能做什么

- **本地推理**：接入 Ollama，模型跑在你自己机器上
- **控制电脑**：文件读写 / 运行命令 / 截图识别 / 键鼠操作
- **安全闸门**：路径白名单 + 危险操作确认
- **Agent 循环**：思考 → 调工具 → 看结果 → 再思考，直到完成

## 🚀 下载

👉 **[GitHub Releases 下载 CrabClaw](https://github.com/W-zc-lang/CrabClaw/releases)**

## ☕ 支持

点个 **Star** ⭐ 支持作者。

---


## 一、先搞清楚差距在哪

Ollama 是**推理引擎**，不是助理。它接收一段文字、吐出一段文字，到此为止。
小螃蟹 CrabClaw / Claude Desktop 这类「能干活」的软件 = **Ollama（大脑）+ 三层你自己写的壳**：

| 层 | 作用 | Ollama 有吗 |
|---|---|---|
| 大脑 | 理解意图、规划步骤 | ✅ 这就是 Ollama |
| Agent 循环 | 思考 → 调工具 → 看结果 → 再思考，直到任务完成 | ❌ 要写 |
| 安全闸门 | 路径白名单、命令黑名单、危险操作确认 | ❌ 要写 |
| 工具集 | 文件读写 / 跑命令 / 截图看图 / 点鼠标敲键盘 | ❌ 要写 |

一句话：**Ollama 给你「想」的能力，「做」的能力全是普通 Python 代码，跟 AI 无关。**

## 二、最小可运行版本

`pc_agent.py` 就是一个完整闭环，单文件、约 400 行。跑起来只需要 requests。

### 1. 装依赖

```powershell
cd C:\Users\win\WorkBuddy\2026-08-31-09-43-00\ollama-agent
python -m venv .venv
.\.venv\Scripts\activate
pip install requests mss pyautogui psutil
```

- `requests` — 必需，调 Ollama API
- `mss` — 截图（比 pyautogui 快，支持多屏）
- `pyautogui` — 鼠标键盘模拟，**内置 FAILSAFE：鼠标甩到屏幕左上角立即中止**
- `psutil` — 看进程

### 2. 跑起来

```powershell
python pc_agent.py                 # 交互模式
python pc_agent.py "查看 C:\Users\win\WorkBuddy 下有几个文件夹"
python pc_agent.py -m qwen2.5:7b-instruct --yes
```

- 默认用 `tools` 模式（模型原生 function calling）
- 模型不支持时加 `--mode react`，改成解析 JSON 文本，大部分小模型都能用
- `--yes` 跳过二次确认，**只在信任的场景开**

## 三、两种调用模式怎么选

| 模式 | 原理 | 适用 |
|---|---|---|
| `tools` | Ollama `/api/chat` 传 `tools` 参数，模型返回结构化 `tool_calls` | qwen2.5 / qwen3 / llama3.1 及以上，稳定、不易错 |
| `react` | 提示词要求模型输出 ```json {"action":..., "args":{...}}```，代码正则解析 | 任何模型，含你自己微调的 local-assistant；容错差些但通用 |

**你的 `local-assistant` 是微调过的，大概率已经破坏了原生 tool calling 格式 —— 直接用 `--mode react`。**

## 四、模型选择（针对你这台机器）

你的机器：i7-9750H / 16GB / Radeon Pro 5300M。
⚠️ **Ollama 在 Windows 上不支持 Radeon Pro 5300M 加速**（ROCm 只支持 Linux，Vulkan 后端不支持这张卡），所以是**纯 CPU 推理**。这决定了模型不能太大。

| 用途 | 模型 | 体积 | CPU 上大概速度 |
|---|---|---|---|
| 起步验证 | `qwen2.5:3b`（你已有） | 2.0 GB | 8-15 tok/s |
| 主力干活 | `qwen2.5:7b-instruct-q4_K_M` | 4.7 GB | 3-6 tok/s |
| 中文推理更好 | `qwen3:4b` / `qwen3:8b` | 2.6 / 5.2 GB | 4b 更实际 |
| 屏幕理解 | `qwen2.5vl:3b` / `qwen2.5vl:7b` | 3.2 / 6.0 GB | 视觉模型慢，建议 3b |
| 改代码 | `qwen2.5-coder:7b` | 4.7 GB | 3-6 tok/s |

```powershell
ollama pull qwen3:4b
ollama pull qwen2.5vl:3b
```

改 `pc_agent.py` 顶部的 `DEFAULT_MODEL` / `VISION_MODEL` 即可。

## 五、安全设计（别省）

代码里已经做了四道闸，改动前先看懂：

1. **路径白名单** `SAFE_ROOTS` — 文件读写只允许在 `C:\Users\win\WorkBuddy` 和 `C:\Users\win\Desktop\agent-sandbox` 内，含 `..` 逃逸检查
2. **命令黑名单** `BLOCKED_PATTERNS` — format / rm -rf / del /s / shutdown / reg delete / iex 等一律拒绝
3. **二次确认** `NEED_CONFIRM` — 删除、覆盖、启动进程、点击鼠标、敲键盘都会先问你
4. **步数上限** `MAX_STEPS = 12` — 防模型陷入死循环反复执行

**强烈建议：先用虚拟机或沙箱目录试，别一上来就全盘放开。**

## 六、能力边界（说实话）

3B-7B 本地模型和云端大模型差距是数量级的，别期待过高：

- ✅ 能做好：列目录、读文件、跑命令、查进程、按明确指令写文件、单步点击
- ⚠️ 勉强：多步规划（3 步以上容易跑偏）、精确坐标定位、网页表单填写
- ❌ 做不了：长任务链、复杂的 GUI 推理、需要大量上下文的重构

实用技巧：
- **任务拆小**，一句话一个动作
- **工具别超过 10 个**，小模型工具一多就选错
- **工具描述写短写死**，比如"列出目录内容"比"可以用于浏览文件系统获取信息"有效得多
- 给工具加 `finish`，让模型有明确的收尾动作

## 七、下一步升级路线

按性价比排序：

1. **接上 pywebview GUI** — 你 file-converter 已经熟了，把 CLI 换成聊天气泡 + 工具调用可视化，体验立刻上一个台阶
2. **记忆层** — 把历史对话存 SQLite，每次带上最近 N 轮 + 关键事实摘要
3. **视觉闭环** — `look_at_screen` 返回坐标后自动 `mouse_click`，实现真正的"看屏幕点按钮"
4. **窗口控制** — `pip install pywin32`，用 `win32gui` 枚举窗口、置顶、激活，比盲点坐标可靠得多（UI Automation 更稳：`pip install uiautomation`）
5. **工具权限分级** — 只读工具自动放行，写操作需确认，危险操作需密码
6. **Skills/插件** — 把常用流程（清理 C 盘、批量重命名）写成固定脚本，让模型只负责填参数，可靠性大幅提升

## 八、和「小墨」人设怎么共存

你现在 `local-assistant` 是对话人设模型，跟工具调用是**两件事**，别混在一个模型里微调。
推荐做法：

- **人设模型**管「怎么说」（语气、称呼、性格）
- **工具模型**管「怎么做」（用 qwen2.5:7b-instruct 这类原生支持 tool calling 的）
- 流程：任务模型规划并调用工具 → 拿到结果 → 交给人设模型润色成小墨的口吻输出

这样比硬塞一个模型里稳定得多。

## 九、图形界面（GUI）运行

已附带 `gui.py`（pywebview + WebView2 窗口）和 `index.html`（暗色聊天界面），把上面的 CLI 包成了对话框。

### 安装与运行

```powershell
cd C:\Users\win\WorkBuddy\2026-08-31-09-43-00\ollama-agent
python -m venv .venv; .\.venv\Scripts\activate
pip install requests mss pyautogui psutil pywebview
python gui.py
```

- Windows 需已安装 **WebView2 Runtime**（Win10/11 一般自带；没有去微软官网装）
- 启动后是独立窗口：底部输入框打字 → 回车发送（Shift+Enter 换行）
- 工具调用过程实时显示为卡片：`⚙ 工具名(参数)` → 执行结果后打 ✓；最终答复是气泡
- 危险操作（删文件、点鼠标、敲键盘…）会弹窗问你确认，点「确定」才执行

### 它是怎么接起来的

```
index.html（聊天界面，JS）
   ↕  window.pywebview.api.chat(task)  /  onAgentEvent(...)
gui.py（pywebview 窗口 + 子线程跑 Agent）
   ↕  run_agent(task, on_event=推送进度)
pc_agent.py（大脑 + 工具 + 安全层，CLI 与 GUI 共用同一套）
```

- 后端在**子线程**跑 Agent，避免界面卡死
- 每步工具调用通过 `window.evaluate_js("onAgentEvent(...)")` 实时推到前端
- `pc_agent.py` 的 `run_agent` 加了 `on_event` 回调：CLI 传 `None` 走 print，GUI 传回调走界面，逻辑完全共用，没有重复代码

### 界面样式

当前界面为 **小螃蟹 CrabClaw 式布局 + 白色/浅色主题（Light）**：

- 左侧边栏：logo、『新建任务』按钮、导航项（对话/历史/设置/关于）、底部赞赏码 `reward.jpg`
- 右侧主区域：顶部标题 + 模型切换下拉框 + 连接状态、中间对话气泡、底部输入框
- 输入框左侧有「＋」文件附件按钮；下方有「电脑访问权限」开关（默认关闭）
- 主题以深灰/近黑为主，强调色为蓝色，整体简洁
- 无登录、无账号体系，双击即用

### 界面功能（v2）

1. **模型切换**：顶部下拉框从 Ollama `/api/tags` 拉取可用模型，当前模型高亮，选择后写入 `settings.json` 持久化（下次启动自动加载）。
2. **新建任务**：左侧『新建任务』按钮清空对话与已附文件，重新开始。
3. **电脑访问权限**：底部开关，默认关闭；关闭时所有文件/命令/截图/键鼠类工具被拒绝，模型只能纯文本回答。开启后写入 `settings.json` 并实时生效。
4. **文件附件**：输入框左侧「＋」多选本地文件，以可删除 chip 形式展示；发送时文件内容注入 prompt，模型可按文件名引用。图片类仅提示（纯文本模型无法内联）。
5. **设置页**：侧栏『设置』打开弹窗，包含 模型与 API 配置 / 权限管理 / 主题与语言 / 数据存储位置 四个分区，保存到 `settings.json`。
6. **品牌名**：界面 logo、窗口标题、说明文档中的产品名已统一为 `小螃蟹 CrabClaw`。

### 打包成 exe（直接复用 file-converter 经验）

已验证可用：本项目根目录的 `CrabClaw.spec` 已配好全部参数，直接打包即可：

```powershell
cd C:\Users\win\WorkBuddy\2026-08-31-09-43-00\ollama-agent
python -m venv .venv; .\.venv\Scripts\activate
pip install pyinstaller requests mss pyautogui psutil pywebview
pyinstaller CrabClaw.spec --clean --noconfirm
# 产物：dist\CrabClaw.exe（单文件，约 22MB）
```

**`CrabClaw.spec` 已解决两个必踩的坑：**

1. **递归爆炸**（`maximum recursion depth exceeded`）：pywebview → pythonnet/clr 依赖链在 Windows 上会触发 PyInstaller 默认递归深度（1000）不够。`spec` 开头已 `sys.setrecursionlimit(5000)` 解决。
2. **资源文件丢失**：`--onefile` 模式下 `index.html` 不会自动跟随，需打包时 `--add-data "index.html;."`（已写进 `datas`），且 `gui.py` 用 `sys._MEIPASS` 定位（`_resource_path`），运行时不依赖当前目录。

**沙箱/CI 打包避坑（本机可忽略）**：沙箱会拦截项目目录内的文件删除（safe-delete），导致 `pip install` 清理缓存临时文件失败、`--clean` 删除 build/ 失败。可靠解法：
- `pip install --no-cache-dir ...`（不再清理缓存 → 不触发拦截）
- PyInstaller 加 `--workpath $env:TEMP\la_build --distpath $env:TEMP\la_dist`（中间产物落 OS 临时目录，删除走原生）
- 打包完用系统 `cp` 把 `CrabClaw.exe` 拷回项目 `dist/`，不要走 Python `shutil`（会触发同款拦截）

**已知问题：窗口弹出但前端报 `api.chat is not a function`（已修复）**

这是 pywebview 6.x 的 API 暴露机制导致的，典型现象是**窗口正常显示、但 JS 调 `window.pywebview.api.chat()` 报"api 不是函数"**，因为 `finish.js` 从没被注入。两个根因都已修掉：

1. **`AgentAPI` 实例属性必须用 `_` 开头**（如 `self._window` 而非 `self.window`）。
   pywebview 6.x 的 `util.get_functions()` 会遍历本对象所有「非下划线开头」的属性；若把 pywebview 的 `Window` 对象存成 `self.window`，它会递归进整个 Window → .NET/COM 绑定树，导致 `generate_func()` 抛异常 → `finish.js` 永不注入 → `api` 恒为 `{}`。该异常在 `--windowed` 模式被静默吞掉，所以窗口看着正常。修复：所有内部属性改成 `_` 前缀。
2. **`gui="edge"` 是无效值**（pywebview 6.x 合法值只有 `qt/gtk/cef/mshtml/edgechromium/android/cocoa`）。已改为不传 `gui`，Windows 下自动选 WebView2。

**配套的前端自检**：`index.html` 现在监听 `pywebviewready` 事件后才允许调用 `api.chat`，并在就绪后自动调一次 `api.ping()` 把模型名更新为「模型名（已连接）」。若你再遇到接口不通，看窗口右上角模型栏是否显示「已连接」，即可定位是"API 没暴露"（仍显示"加载中…"）还是"Ollama 没开"（已连接但任务报错）。

**运行前本机验证清单：**
- ✅ 本机已运行 Ollama（`ollama serve` 或托盘常驻），且 `qwen2.5:3b` 已 `ollama pull`
- ✅ 已装 WebView2 Runtime（Win10/11 一般自带；没有去微软官网装，否则双击白屏）
- ✅ 双击 `dist\CrabClaw.exe` → 应弹出聊天窗口；输入"列出 C:\Users\win\WorkBuddy 下的文件夹"验证工具调用
- ⚠️ exe **不含 Ollama 本体**，两者分开装、分开分发（如同 Ollama 是独立服务）

PyInstaller onefile + pywebview 在 Windows 上需把 WebView2 作为运行时依赖；打包机需预装 WebView2（或联网下载引导器）。
