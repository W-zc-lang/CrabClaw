"""临时冒烟测试：不依赖真实模型 / GUI，验证后端链路。

- 用 monkeypatch 替换 ollama.chat_stream，模拟流式输出
- 验证：会话创建、消息持久化、流式累积、stats、重新生成、删除
"""

import sys
import tempfile
import threading
import time
import os
from unittest import mock

import core.ollama as ollama
from core.api import Api
from core.store import Store


def fake_stream(model, messages, options=None, cancel=None, stats_out=None, keep_alive=None):
    chunks = ["你好", "，这是", "一条", "模拟的", "流式回复。"]
    for c in chunks:
        if cancel and cancel():
            break
        if stats_out is not None:
            stats_out["eval_count"] = 5
            stats_out["eval_duration"] = 5_000_000_000  # 5s -> 1 tok/s
        yield c
        time.sleep(0.02)


def wait_done(api: Api, timeout=5):
    t0 = time.time()
    while api._busy and time.time() - t0 < timeout:
        time.sleep(0.02)


import base64, json


class FakeWindow:
    def __init__(self):
        self.events = []

    def evaluate_js(self, js):
        # 把 base64 事件解出来看一眼
        try:
            b64 = js[len("__pyEvent(") : -1]
            ev = json.loads(base64.b64decode(b64).decode("utf-8"))
            self.events.append(ev)
            if ev.get("type") == "gen_done":
                print("    EVENT gen_done:", json.dumps(ev, ensure_ascii=False))
        except Exception:
            pass


def main():
    # 用临时文件数据库，避免污染真实数据（:memory: 在多连接下 schema 不共享）
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.remove(tmp.name)  # 让 Store 自己创建
    store = Store(tmp.name)
    api = Api()
    api.window = FakeWindow()
    api.store = store

    with mock.patch.object(ollama, "chat_stream", fake_stream), \
         mock.patch.object(ollama, "is_running", return_value=True):

        # 设置默认模型
        store.set_setting("model", "fake-model:latest")
        store.set_setting("num_ctx", 4096)

        # bootstrap
        boot = api.bootstrap()
        assert boot["ok"], "bootstrap 失败"
        print("[1] bootstrap ok, ollama_running =", boot["ollama_running"])

        # 新建会话 + 发消息
        ns = api.new_session("测试会话")
        sid = ns["session"]["id"]
        res = api.send(sid, "你好")
        assert res["ok"], res
        print("[2] send ok, user_msg_id =", res["user_msg_id"],
              "assistant_msg_id =", res["assistant_msg_id"])

        wait_done(api)
        msgs = store.get_messages(sid)
        assert len(msgs) == 2, msgs
        assert msgs[1]["role"] == "assistant"
        assert "模拟的流式回复" in msgs[1]["content"], msgs[1]["content"]
        print("[3] 流式累积正确：", repr(msgs[1]["content"]))
        print("    stats =", msgs[1]["stats"])

        # 再发一轮，测试多轮
        res2 = api.send(sid, "继续")
        wait_done(api)
        msgs = store.get_messages(sid)
        assert len(msgs) == 4, len(msgs)
        print("[4] 多轮对话条数正确：", len(msgs))

        # 重新生成最后一条
        last_asst = [m for m in msgs if m["role"] == "assistant"][-1]
        api.regenerate(sid, last_asst["id"])
        wait_done(api)
        msgs = store.get_messages(sid)
        print("[5] 重新生成后条数：", len(msgs), "（应为 4）")
        assert len(msgs) == 4

        # 删除会话
        api.delete_session(sid)
        assert api.store.get_session(sid) is None
        print("[6] 删除会话 ok")

    print("\n全部后端链路测试通过 ✅")


if __name__ == "__main__":
    main()
