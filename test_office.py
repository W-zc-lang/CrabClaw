"""办公能力综合验证：表格解析/统计、知识库 xlsx、文档摘要、文件自动化。

用 venv 的 python 跑（已装 openpyxl / python-docx / pypdf）。
"""
import os
import shutil
import tempfile
import threading

import core.ollama as ollama_mod

# 用假模型替代真实 Ollama 调用
def _fake_stream(model, messages, **kw):
    yield "这是一份测试文档的自动摘要：包含若干字段与示例数据。"
ollama_mod.chat_stream = _fake_stream

from core.knowledge import Knowledge
from core.store import Store
from core.sheets import load_csv, load_xlsx
from core import automation
from core.api import Api


def main():
    tmp = tempfile.mkdtemp()
    try:
        print("=== 1. sheets CSV 解析与统计 ===")
        csvp = os.path.join(tmp, "t.csv")
        with open(csvp, "w", encoding="utf-8") as f:
            f.write("name,age,score\nAlice,30,95\nBob,25,88\n,40,70\n")
        t = load_csv(csvp)
        print("  rows=%d cols=%d" % (t.n_rows, t.n_cols))
        for s in t.column_stats():
            print("   ", s)
        assert t.n_rows == 3 and t.n_cols == 3
        assert t.column_stats()[1]["type"] == "numeric"
        ctx = t.to_context()
        assert "name,age,score" in ctx
        print("  context OK (len=%d)" % len(ctx))

        print("=== 2. sheets XLSX 解析 ===")
        from openpyxl import Workbook
        xlsx = os.path.join(tmp, "t.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.append(["城市", "人口"])
        ws.append(["北京", 2100])
        ws.append(["上海", 2500])
        wb.save(xlsx)
        tx = load_xlsx(xlsx)
        print("  rows=%d cols=%d headers=%s" % (tx.n_rows, tx.n_cols, tx.headers))
        assert tx.n_rows == 2 and tx.headers == ["城市", "人口"]
        print("  preview:", tx.preview())

        print("=== 3. knowledge 导入 xlsx + 检索 ===")
        kb = Knowledge(os.path.join(tmp, "kb.db"))
        r = kb.import_file(xlsx)
        print("  import ok=%s chunks=%s" % (r.get("ok"), r.get("chunks")))
        assert r.get("ok")
        hits = kb.search("人口")
        print("  hits:", [(h["doc"], h["content"][:16]) for h in hits])
        assert hits and "人口" in hits[0]["content"]

        print("=== 4. summarize_document 链路 ===")
        api = Api()
        api.store = Store(os.path.join(tmp, "store.db"))
        api.knowledge = kb
        api.window = None
        api._cancel = threading.Event()
        api._pull_cancel = threading.Event()
        api._sheets = {}
        api._current_sheet_id = None
        api._sheet_busy = False
        api._sheet_cancel = threading.Event()
        api.store.set_setting("model", "fake-model")
        res = api.summarize_document(r["doc_id"])
        print("  summary ok=%s summary=%s" % (res.get("ok"), res.get("summary")))
        assert res.get("ok") and "摘要" in res.get("summary", "")

        print("=== 5. automation 归档 + 重命名 ===")
        adir = os.path.join(tmp, "auto")
        os.makedirs(adir)
        for n in ["a.png", "b.jpg", "c.pdf", "d.txt", "e.png"]:
            with open(os.path.join(adir, n), "w") as fh:
                fh.write("x")
        sc = automation.scan_dir(adir)
        print("  scan count=%d by_ext=%s" % (sc["count"], sc["by_ext"]))
        assert sc["count"] == 5
        plan = automation.plan_archive(adir)
        print("  archive moves=%d" % plan["count"])
        ar = automation.apply_moves(adir, plan["moves"])
        print("  archive done=%d top=%s" % (ar["done"], sorted(os.listdir(adir))))
        assert ar["done"] == 5  # 全部按类型归档（图片3 + 文档2）
        pngdir = os.path.join(adir, "图片")
        rp = automation.plan_rename(pngdir, "sequence", prefix="img", ext=".png", start=1)
        print("  rename plan=%d" % rp["count"])
        rr = automation.apply_renames(pngdir, rp["renames"])
        print("  rename done=%d files=%s" % (rr["done"], sorted(os.listdir(pngdir))))
        assert rr["done"] == 2 and sorted(os.listdir(pngdir)) == ["b.jpg", "img_001.png", "img_002.png"], sorted(os.listdir(pngdir))

        print("\nALL OFFICE TESTS PASSED ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
