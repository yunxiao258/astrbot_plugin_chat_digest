# -*- coding: utf-8 -*-
"""astrbot_plugin_chat_digest 存储层单元测试：原子写、会话隔离、清理、检索与导入导出。"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrbot_plugin_chat_digest.storage import DigestStore, atomic_write_json, safe_read_json  # noqa: E402


class TestDigestStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="chat_digest_store_")
        self.store = DigestStore(self.tmp, max_messages_per_day=20, retention_days=7)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_atomic_write_and_corrupt_fallback(self) -> None:
        p = os.path.join(self.tmp, "sub", "sample.json")
        atomic_write_json(p, {"ok": True, "cn": "群聊记忆"})
        self.assertEqual(safe_read_json(p, {}), {"ok": True, "cn": "群聊记忆"})
        with open(p, "w", encoding="utf-8") as f:
            f.write("{broken json")
        self.assertEqual(safe_read_json(p, {"fallback": 1}), {"fallback": 1})
        # 类型不匹配时也回退默认值
        atomic_write_json(p, [1, 2, 3])
        self.assertEqual(safe_read_json(p, {}), {})

    def test_session_isolation_and_daily_cap(self) -> None:
        now = datetime(2026, 11, 21, 10, 0, 0)
        for i in range(25):
            self.store.append_message("g:1", f"u{i}", f"用户{i}", f"第{i}条消息", now=now)
        self.store.append_message("g:2", "u9", "乙", "二群消息", now=now)
        self.store.append_message("p:1", "u1", "甲", "私聊消息", now=now)

        g1 = self.store.get_messages("g:1", target_date="2026-11-21")
        self.assertEqual(len(g1), 20)
        self.assertEqual(g1[0]["text"], "第5条消息")
        self.assertEqual(g1[-1]["text"], "第24条消息")
        self.assertEqual(len(self.store.get_messages("g:2")), 1)
        self.assertEqual(len(self.store.get_messages("p:1")), 1)

    def test_retention_pruning(self) -> None:
        old_dt = datetime(2026, 11, 1, 9, 0, 0)
        cur_dt = datetime(2026, 11, 21, 9, 0, 0)
        self.store.append_message("g:1", "u1", "甲", "旧消息", now=old_dt)
        self.store.save_digest("g:1", {"date": "2026-11-01", "summary": "旧摘要", "mode": "rule"})
        self.store.append_message("g:1", "u1", "甲", "新消息", now=cur_dt)
        self.assertEqual(self.store.get_messages("g:1", target_date="2026-11-01"), [])
        self.assertEqual(len(self.store.get_messages("g:1", target_date="2026-11-21")), 1)
        self.assertEqual(self.store.list_digests("g:1", target_date="2026-11-01"), [])

    def test_highlights_todos_links_rss_crud_and_reload(self) -> None:
        h = self.store.add_highlight("g:1", "AstrBot 必须用 async initialize", "云晓", tags=["开发", "规范"])
        self.assertEqual(len(self.store.list_highlights("g:1")), 1)
        self.assertEqual(self.store.list_highlights("g:2"), [])

        t = self.store.add_todo("g:1", "整理发布日志", "云晓", assignee="小明")
        self.assertEqual(len(self.store.list_todos("g:1")), 1)
        done = self.store.complete_todo("g:1", t["id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(self.store.list_todos("g:1", include_done=False), [])
        self.assertEqual(len(self.store.list_todos("g:1", include_done=True)), 1)

        now = datetime(2026, 11, 21, 12, 0, 0)
        lk = self.store.add_link("g:1", "https://example.com/a", "示例文章", "摘录", "核心摘要", "甲", now=now)
        self.assertTrue(self.store.has_link_today("g:1", "https://example.com/a", target_date="2026-11-21"))
        self.assertFalse(self.store.has_link_today("g:2", "https://example.com/a", target_date="2026-11-21"))

        feed = self.store.add_rss_feed("g:1", "科技周刊", "https://example.com/feed.xml")
        self.assertEqual(len(self.store.list_rss_feeds("g:1")), 1)

        # 重载验证持久化且无残留
        store2 = DigestStore(self.tmp)
        self.assertEqual(len(store2.list_highlights("g:1")), 1)
        self.assertEqual(len(store2.list_links("g:1")), 1)
        self.assertTrue(store2.delete_highlight("g:1", h["id"]))
        self.assertTrue(store2.delete_todo("g:1", t["id"]))
        self.assertTrue(store2.delete_link("g:1", lk["id"]))
        self.assertTrue(store2.delete_rss_feed("g:1", feed["id"]))
        self.assertFalse(store2.delete_highlight("g:1", "not_exist"))

    def test_search_across_all_buckets(self) -> None:
        self.store.append_message("g:1", "u1", "云晓", "今天讨论了部署方案")
        self.store.add_highlight("g:1", "部署前要先跑 CI", "云晓", tags=["部署"])
        self.store.add_todo("g:1", "核对部署文档", "云晓", assignee="小张")
        self.store.add_link("g:1", "https://example.com/deploy", "部署指南", "指南", "自动化部署最佳实践", "云晓")
        self.store.save_digest("g:1", {"date": "2026-11-21", "summary": "围绕部署流程形成共识", "mode": "llm"})

        hits = self.store.search("g:1", "部署")
        self.assertEqual(len(hits["messages"]), 1)
        self.assertEqual(len(hits["highlights"]), 1)
        self.assertEqual(len(hits["todos"]), 1)
        self.assertEqual(len(hits["links"]), 1)
        self.assertEqual(len(hits["digests"]), 1)
        # 会话隔离验证
        hits_other = self.store.search("g:other", "部署")
        self.assertEqual(sum(len(v) for v in hits_other.values()), 0)

    def test_export_import_markdown_and_json_roundtrip(self) -> None:
        self.store.add_highlight("g:1", "精华一", "甲", tags=["标签A"])
        self.store.add_todo("g:1", "待办一", "甲", assignee="乙")
        self.store.add_link("g:1", "https://example.com", "标题", "摘录", "概要", "甲")
        self.store.add_rss_feed("g:1", "源一", "https://example.com/rss")
        self.store.save_digest("g:1", {"date": "2026-11-21", "summary": "今日总结", "mode": "llm", "highlights": ["点1"]})

        md = self.store.export_session_markdown("g:1")
        self.assertIn("# 会话记忆与摘要档案", md)
        self.assertIn("精华一", md)
        self.assertIn("待办一", md)
        self.assertIn("今日总结", md)

        raw_json = self.store.export_session_json("g:1")
        parsed = json.loads(raw_json)
        self.assertEqual(parsed["schema_version"], 1)

        # 导入到新会话
        counts = self.store.import_session_json("g:2", raw_json)
        self.assertEqual(counts, {"highlights": 1, "todos": 1, "links": 1, "rss_feeds": 1, "digests": 1})
        # 重复合并导入自动按 ID 去重
        counts_dup = self.store.import_session_json("g:2", raw_json)
        self.assertEqual(sum(counts_dup.values()), 0)
        # 覆盖导入
        counts_rep = self.store.import_session_json("g:2", raw_json, replace=True)
        self.assertEqual(counts_rep["highlights"], 1)

        # 非法 JSON 抛异常
        with self.assertRaises(ValueError):
            self.store.import_session_json("g:2", "[]")

        # 导出到文件
        md_file = self.store.write_export_file("g:1", fmt="md")
        json_file = self.store.write_export_file("g:1", fmt="json")
        self.assertTrue(os.path.isfile(md_file))
        self.assertTrue(os.path.isfile(json_file))

    def test_subscription_and_clear(self) -> None:
        self.store.set_subscription("g:1", True, send_card=True)
        self.assertIn("g:1", self.store.list_subscribed_umos())
        self.store.mark_pushed("g:1", "2026-11-21")
        self.assertEqual(self.store.get_last_push_date("g:1"), "2026-11-21")
        self.store.clear_session("g:1")
        self.assertNotIn("g:1", self.store.list_subscribed_umos())
        self.assertEqual(self.store.session_stats("g:1")["total_messages"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
