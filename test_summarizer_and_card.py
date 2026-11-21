# -*- coding: utf-8 -*-
"""astrbot_plugin_chat_digest 摘要引擎（LLM + 规则兜底）与 Pillow 卡片渲染测试。"""

import asyncio
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrbot_plugin_chat_digest.card_renderer import render_digest_card  # noqa: E402
from astrbot_plugin_chat_digest.summarizer import (  # noqa: E402
    build_rule_digest,
    extract_keywords,
    format_digest_text,
    generate_chat_digest,
    summarize_webpage,
)


class _FakeProvider:
    def __init__(self, reply: str = "", raise_exc: Exception | None = None) -> None:
        self.reply = reply
        self.raise_exc = raise_exc
        self.last_prompt = ""
        self.last_system = ""

    async def text_chat(self, prompt: str = "", system_prompt: str = "", **kwargs):
        self.last_prompt = prompt
        self.last_system = system_prompt
        if self.raise_exc:
            raise self.raise_exc
        return types.SimpleNamespace(completion_text=self.reply)


class _FakeContext:
    def __init__(self, provider=None, named_providers=None) -> None:
        self.provider = provider
        self.named = named_providers or {}

    def get_using_provider(self, umo: str = ""):
        return self.provider

    def get_provider_by_id(self, pid: str):
        return self.named.get(pid)


SAMPLE_MSGS = [
    {"ts": "2026-11-21T10:00:00", "user_id": "1", "nickname": "云晓", "text": "大家讨论一下新插件 astrbot_plugin_chat_digest 的接口设计"},
    {"ts": "2026-11-21T10:02:00", "user_id": "2", "nickname": "小明", "text": "同意统一收拢到 /摘要 指令下面，这样不会和别的插件冲突"},
    {"ts": "2026-11-21T10:05:00", "user_id": "1", "nickname": "云晓", "text": "决定采用原子写 JSON 加 Pillow 可选长图方案"},
    {"ts": "2026-11-21T10:08:00", "user_id": "3", "nickname": "小红", "text": "待办：小明今晚负责补齐 SSRF 防护单元测试，别忘了"},
]


class TestSummarizerAndCard(unittest.TestCase):
    def test_rule_digest_extracts_keywords_decisions_todos(self) -> None:
        kws = extract_keywords(SAMPLE_MSGS, top_k=5)
        self.assertTrue(len(kws) >= 1)
        d = build_rule_digest(
            SAMPLE_MSGS,
            links=[{"title": "文档", "url": "https://example.com"}],
            target_date="2026-11-21",
        )
        self.assertEqual(d["mode"], "rule")
        self.assertEqual(d["message_count"], 4)
        self.assertEqual(d["participants"], 3)
        self.assertTrue(any("决定" in x or "同意" in x for x in d["decisions"]))
        self.assertTrue(any("待办" in x for x in d["todos"]))
        txt = format_digest_text(d)
        self.assertIn("本地规则摘要", txt)
        self.assertIn("决议共识", txt)
        self.assertIn("待办线索", txt)

    def test_llm_digest_with_fence_and_injection_boundary(self) -> None:
        reply = """```json
        {
          "summary": "群内敲定了新插件统一采用 /摘要 入口与原子 JSON 持久化。",
          "topics": ["摘要插件", "SSRF"],
          "highlights": ["统一子命令入口零冲突"],
          "decisions": ["采用原子 JSON + 可选 Pillow 长图"],
          "todos": ["小明今晚补齐 SSRF 单元测试"]
        }
        ```"""
        prov = _FakeProvider(reply=reply)
        ctx = _FakeContext(provider=prov)
        d = asyncio.run(generate_chat_digest(ctx, SAMPLE_MSGS, target_date="2026-11-21", enable_llm=True))
        self.assertEqual(d["mode"], "llm")
        self.assertIn("原子 JSON", d["summary"])
        self.assertEqual(d["decisions"], ["采用原子 JSON + 可选 Pillow 长图"])
        # 验证防注入边界符存在于 system_prompt 与 prompt
        self.assertIn("CHAT_TRANSCRIPT_", prov.last_system)
        self.assertIn("CHAT_TRANSCRIPT_", prov.last_prompt)

    def test_llm_failure_and_bad_json_fallback_to_rule(self) -> None:
        ctx_err = _FakeContext(provider=_FakeProvider(raise_exc=RuntimeError("API timeout")))
        d1 = asyncio.run(generate_chat_digest(ctx_err, SAMPLE_MSGS, target_date="2026-11-21", enable_llm=True))
        self.assertEqual(d1["mode"], "rule")

        ctx_bad = _FakeContext(provider=_FakeProvider(reply="not a json"))
        d2 = asyncio.run(generate_chat_digest(ctx_bad, SAMPLE_MSGS, target_date="2026-11-21", enable_llm=True))
        self.assertEqual(d2["mode"], "rule")

        ctx_none = _FakeContext(provider=None)
        d3 = asyncio.run(generate_chat_digest(ctx_none, SAMPLE_MSGS, target_date="2026-11-21", enable_llm=True))
        self.assertEqual(d3["mode"], "rule")

    def test_provider_id_override_and_webpage_summary(self) -> None:
        p_custom = _FakeProvider(reply="该文章详细介绍了 AstrBot 4.27 插件生命周期规范。")
        ctx = _FakeContext(provider=None, named_providers={"deepseek_fast": p_custom})
        res = asyncio.run(
            summarize_webpage(ctx, "插件开发", "描述", "正文内容", enable_llm=True, provider_id="deepseek_fast")
        )
        self.assertIn("生命周期规范", res)

        # 无 LLM 降级为 meta description
        res_fb = asyncio.run(summarize_webpage(_FakeContext(), "标题", "元描述兜底", "长正文", enable_llm=True))
        self.assertEqual(res_fb, "元描述兜底")

    def test_card_renderer_and_graceful_missing_pillow(self) -> None:
        tmp = tempfile.mkdtemp(prefix="chat_digest_card_")
        try:
            d = build_rule_digest(SAMPLE_MSGS, target_date="2026-11-21")
            out = os.path.join(tmp, "digest.png")
            rendered = render_digest_card(d, out)
            try:
                import PIL  # noqa: F401
                has_pil = True
            except ImportError:
                has_pil = False
            if has_pil:
                self.assertEqual(rendered, out)
                self.assertTrue(os.path.isfile(out))
                with open(out, "rb") as f:
                    self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
            else:
                self.assertIsNone(rendered)

            # 模拟未安装 PIL 时平稳返回 None
            with patch.dict(sys.modules, {"PIL": None}):
                self.assertIsNone(render_digest_card(d, os.path.join(tmp, "none.png")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
