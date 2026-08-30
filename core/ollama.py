"""Ollama 本地模型 HTTP 客户端。

只依赖标准库（urllib / json），不引入 requests，
这样打包成 exe 时依赖树最小，PyInstaller 出错概率也最低。

Ollama 默认监听 http://127.0.0.1:11434
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

DEFAULT_HOST = "http://127.0.0.1:11434"
TIMEOUT = 30


class OllamaError(RuntimeError):
    """Ollama 调用失败。"""


class OllamaUnavailable(OllamaError):
    """服务没起来 / 端口不通。"""


# --------------------------------------------------------------------------
# 推荐模型预设：按「体积从小到大」排列，界面上直接展示
# 尺寸为近似值，仅用于给用户一个「要下多大」的预期
# --------------------------------------------------------------------------
RECOMMENDED_MODELS: list[dict[str, str]] = [
    {
        "name": "qwen2.5:0.5b",
        "size": "~0.4 GB",
        "desc": "最轻量，老年机/低内存也能跑；质量一般",
    },
    {
        "name": "qwen2.5:1.5b",
        "size": "~1.0 GB",
        "desc": "极快，纯 CPU 也能秒回；适合简单问答、草稿",
    },
    {
        "name": "qwen2.5:3b",
        "size": "~2.0 GB",
        "desc": "速度与质量平衡，16GB 内存首选",
    },
    {
        "name": "qwen2.5:7b",
        "size": "~4.7 GB",
        "desc": "中文能力明显更强，CPU 上约 3-5 字/秒",
    },
    {
        "name": "qwen2.5:14b",
        "size": "~9.0 GB",
        "desc": "需要 16GB 内存且几乎不跑其他程序",
    },
    {
        "name": "deepseek-r1:7b",
        "size": "~4.7 GB",
        "desc": "带思维链，推理题更强，但输出更慢更长",
    },
]


def _request(
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = TIMEOUT,
    stream: bool = False,
):
    url = DEFAULT_HOST.rstrip("/") + path
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:500]
        raise OllamaError(f"Ollama 返回 {exc.code}：{detail}") from exc
    except (urllib.error.URLError, socket.timeout, ConnectionRefusedError) as exc:
        raise OllamaUnavailable(
            "连不上 Ollama 服务（127.0.0.1:11434）。请确认 Ollama 已启动。"
        ) from exc


def is_running(host: str = DEFAULT_HOST, timeout: float = 1.0) -> bool:
    """探测 Ollama 是否在跑。用裸 socket 探测，比发 HTTP 请求快得多。"""
    host_only = host.split("://", 1)[-1].split(":")[0]
    try:
        port = int(host.rsplit(":", 1)[-1])
    except ValueError:
        port = 11434
    try:
        with socket.create_connection((host_only, port), timeout=timeout):
            return True
    except OSError:
        return False


def version() -> str:
    resp = _request("/api/version", timeout=5)
    with resp:
        return json.loads(resp.read().decode("utf-8")).get("version", "未知")


def list_models() -> list[dict[str, Any]]:
    """返回本机已下载的模型列表。"""
    resp = _request("/api/tags", timeout=10)
    with resp:
        data = json.loads(resp.read().decode("utf-8"))
    models = []
    for m in data.get("models", []):
        models.append(
            {
                "name": m.get("name", ""),
                "size": m.get("size", 0),
                "family": (m.get("details") or {}).get("family", ""),
                "parameter_size": (m.get("details") or {}).get("parameter_size", ""),
                "quantization": (m.get("details") or {}).get(
                    "quantization_level", ""
                ),
            }
        )
    # 名字排序，方便界面上稳定展示
    models.sort(key=lambda x: x["name"])
    return models


def running_models() -> list[dict[str, Any]]:
    """当前已加载到内存里的模型（用于显示显存/内存占用）。"""
    try:
        resp = _request("/api/ps", timeout=5)
    except OllamaError:
        return []
    with resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("models", [])


def chat_stream(
    model: str,
    messages: list[dict[str, str]],
    options: dict[str, Any] | None = None,
    cancel: Callable[[], bool] | None = None,
    stats_out: dict[str, Any] | None = None,
    keep_alive: str | int | None = None,
) -> Iterator[str]:
    """流式对话，逐段 yield 模型输出的文本。

    cancel      无参回调，返回 True 时立即停止读取（用户点了「停止」）。
    stats_out   传入一个 dict，结束时会被填入 eval_count / eval_duration 等统计。
    keep_alive  模型在内存里的保留时长，如 "5m"、300、-1（常驻）。
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if options:
        payload["options"] = options
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive

    resp = _request("/api/chat", "POST", payload, timeout=120, stream=True)
    try:
        with resp:
            buf = b""
            while True:
                if cancel and cancel():
                    break
                chunk = resp.read1(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        raise OllamaError(str(obj["error"]))
                    content = (obj.get("message") or {}).get("content") or ""
                    if content:
                        yield content
                    if obj.get("done"):
                        if stats_out is not None:
                            for k in (
                                "total_duration",
                                "load_duration",
                                "prompt_eval_count",
                                "prompt_eval_duration",
                                "eval_count",
                                "eval_duration",
                            ):
                                if k in obj:
                                    stats_out[k] = obj[k]
                        return
    except (urllib.error.URLError, socket.timeout) as exc:
        raise OllamaUnavailable(f"与 Ollama 的连接中断：{exc}") from exc
    finally:
        try:
            resp.close()
        except Exception:
            pass


def pull_stream(model: str, cancel: Callable[[], bool] | None = None):
    """下载模型，yield 进度字典 {"status":..., "completed":..., "total":...}。"""
    payload = {"name": model, "stream": True}
    resp = _request("/api/pull", "POST", payload, timeout=300, stream=True)
    try:
        with resp:
            buf = b""
            while True:
                if cancel and cancel():
                    break
                chunk = resp.read1(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        raise OllamaError(str(obj["error"]))
                    yield obj
                    if obj.get("status") == "success":
                        return
    finally:
        try:
            resp.close()
        except Exception:
            pass


def delete_model(model: str) -> None:
    _request("/api/delete", "DELETE", {"name": model}, timeout=30).close()
