"""astrbot_plugin_chat_digest 插件主流程、权限、静默收录、定时日报与配置契约集成测试。"""

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrbot_plugin_chat_digest.main import ChatDigestPlugin, _cfg_bool, _cfg_int
from astrbot_plugin_chat_digest.web_fetcher import PageContent, RssItem


class FakeEvent:
    def __init__(
        self,
        text: str,
        umo: str = "onebot:GroupMessage:10001",
        user_id: str = "20001",
        nickname: str = "测试群友",
        admin: bool = False,
    ) -> None:
        self.message_str = text
        self.unified_msg_origin = umo
        self._user_id = user_id
        self._nickname = nickname
        self._admin = admin

    def get_sender_id(self) -> str:
        return self._user_id

    def get_sender_name(self) -> str:
        return self._nickname

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str) -> str:
        return text

    def chain_result(self, chain: list) -> list:
        return chain


class FakeContext:
    def __init__(
        self, provider=None, active_platforms: list[str] | None = None
    ) -> None:
        self.provider = provider
        self.sent: list[tuple[str, object]] = []
        self._active = active_platforms or []

    def get_using_provider(self, umo: str = ""):
        return self.provider

    def get_provider_by_id(self, pid: str):
        return self.provider

    @property
    def platform_manager(self):
        insts = [types.SimpleNamespace(id=pid) for pid in self._active]
        return types.SimpleNamespace(get_insts=lambda: insts)

    async def send_message(self, umo: str, chain) -> None:
        self.sent.append((umo, chain))


class FakeFetcher:
    def __init__(self) -> None:
        self.session = types.SimpleNamespace(close=lambda: None)
        self.fetched_pages: list[str] = []

    def fetch_page(self, url: str) -> PageContent:
        self.fetched_pages.append(url)
        return PageContent(
            url=url,
            title="示例技术文章",
            description="一篇讲解群聊摘要设计的文章",
            text="正文内容：采用异步事件循环与原子落盘。",
        )

    def fetch_rss(self, url: str, max_items: int = 5):
        return (
            "开源周报",
            [
                RssItem(
                    title="AstrBot 新特性",
                    link="https://example.com/1",
                    summary="异步加载",
                )
            ],
        )


class TestChatDigestPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="chat_digest_plugin_")
        self.ctx = FakeContext()
        self.plugin = ChatDigestPlugin(
            self.ctx,
            config={
                "enabled": True,
                "enable_llm_summary": False,
                "auto_capture_links": True,
                "daily_push_enabled": False,
                "daily_push_time": "21:00",
                "daily_push_min_messages": 2,
                "admin_user_ids": "99999",
            },
            data_dir=self.tmp,
        )
        self.fake_fetcher = FakeFetcher()
        self.plugin._fetcher_cache = self.fake_fetcher

    def tearDown(self) -> None:
        asyncio.run(self.plugin.terminate())
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_safe_config_helpers(self) -> None:
        self.assertFalse(_cfg_bool({"a": "false"}, "a", True))
        self.assertFalse(_cfg_bool({"a": "0"}, "a", True))
        self.assertTrue(_cfg_bool({"a": "yes"}, "a", False))
        # 不吞 0 值
        self.assertEqual(_cfg_int({"n": 0}, "n", 5, min_val=0), 0)
        self.assertEqual(_cfg_int({"n": "bad"}, "n", 5), 5)

    def test_schema_covers_all_config_keys(self) -> None:
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        with open(
            os.path.join(plugin_dir, "_conf_schema.json"), encoding="utf-8-sig"
        ) as f:
            schema = json.load(f)
        with open(os.path.join(plugin_dir, "main.py"), encoding="utf-8-sig") as f:
            src = f.read()
        used = set(
            re.findall(r'_cfg_(?:bool|int|str)\(\s*self\.config,\s*"([^"]+)"', src)
        )
        self.assertTrue(used)
        self.assertEqual(
            used - set(schema.keys()), set(), "存在未在 _conf_schema.json 声明的配置键"
        )
        self.assertEqual(
            set(schema.keys()) - used,
            set(),
            "存在 _conf_schema.json 中未使用的死配置键",
        )

    def test_passive_message_and_silent_link_capture(self) -> None:
        async def _run():
            await self.plugin.initialize()
            # 指令消息被跳过
            self.assertIsNone(await self.plugin.on_any_message(FakeEvent("/摘要 帮助")))
            self.assertEqual(
                self.plugin.store.count_messages("onebot:GroupMessage:10001"), 0
            )

            # 普通消息带 URL：记录消息并静默抓取，不发送群回复
            res = await self.plugin.on_any_message(
                FakeEvent(
                    "大家看这篇 https://example.com/doc 很有启发", nickname="小王"
                )
            )
            self.assertIsNone(res)
            if self.plugin._bg_tasks:
                await asyncio.gather(*list(self.plugin._bg_tasks))

        asyncio.run(_run())
        self.assertEqual(
            self.plugin.store.count_messages("onebot:GroupMessage:10001"), 1
        )
        links = self.plugin.store.list_links("onebot:GroupMessage:10001")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["title"], "示例技术文章")
        self.assertEqual(self.ctx.sent, [])  # 确认不刷屏回复

    def test_full_command_workflows_and_permissions(self) -> None:
        async def _run():
            # 1. 帮助与别名
            help_res = await self.plugin.digest_command(FakeEvent("/digest 帮助"))
            self.assertIn("/摘要 精华", help_res)
            help_res2 = await self.plugin.digest_command(FakeEvent("／纪要 help"))
            self.assertIn("/摘要 rss", help_res2)

            # 2. 收集聊天并生成摘要
            for line in (
                "今天决定采用 AstrBot 群聊记忆摘要架构",
                "待办：明天完成单元测试并接入 CI",
                "我觉得还要支持 Markdown 与 JSON 导出",
            ):
                await self.plugin.on_any_message(FakeEvent(line, nickname="云晓"))

            digest_out = await self.plugin.digest_command(FakeEvent("/摘要"))
            self.assertIn("群聊记忆摘要", digest_out)
            self.assertIn("消息 3 条", digest_out)

            # 3. 精华添加 & 权限校验
            hl_add = await self.plugin.digest_command(
                FakeEvent("/摘要 精华 添加 关键共识 #架构")
            )
            self.assertIn("已收藏精华", hl_add)
            hl_id = self.plugin.store.list_highlights("onebot:GroupMessage:10001")[0][
                "id"
            ]
            hl_del_deny = await self.plugin.digest_command(
                FakeEvent(f"/摘要 精华 删除 {hl_id}", admin=False)
            )
            self.assertIn("仅限管理员", hl_del_deny)

            # 4. 待办添加、列表、完成
            td_add = await self.plugin.digest_command(
                FakeEvent("/摘要 待办 添加 写文档 @小明")
            )
            self.assertIn("已新增待办", td_add)
            td_id = self.plugin.store.list_todos("onebot:GroupMessage:10001")[0]["id"]
            td_done = await self.plugin.digest_command(
                FakeEvent(f"/摘要 待办 完成 {td_id}")
            )
            self.assertIn("待办已完成", td_done)

            # 5. 手动链接摘要与 RSS 订阅
            lk_out = await self.plugin.digest_command(
                FakeEvent("/摘要 链接 https://example.com/x")
            )
            self.assertIn("网页已抓取并收录", lk_out)

            rss_deny = await self.plugin.digest_command(
                FakeEvent("/摘要 rss 添加 周报 https://example.com/rss", admin=False)
            )
            self.assertIn("仅限管理员", rss_deny)
            rss_add = await self.plugin.digest_command(
                FakeEvent(
                    "/摘要 rss 添加 周报 https://example.com/rss", user_id="99999"
                )
            )
            self.assertIn("已订阅 RSS", rss_add)
            rss_fetch = await self.plugin.digest_command(FakeEvent("/摘要 rss 拉取"))
            self.assertIn("AstrBot 新特性", rss_fetch)

            # 6. 搜索与回顾
            sr = await self.plugin.digest_command(FakeEvent("/摘要 搜索 架构"))
            self.assertIn("检索「架构」", sr)
            recap = await self.plugin.digest_command(FakeEvent("/摘要 回顾"))
            self.assertIn("最近 1 份摘要档案", recap)

            # 7. 导出、导入与清空确认
            exp_deny = await self.plugin.digest_command(
                FakeEvent("/摘要 导出 json", admin=False)
            )
            self.assertIn("仅限管理员", exp_deny)
            exp_ok = await self.plugin.digest_command(
                FakeEvent("/摘要 导出 md", admin=True)
            )
            self.assertTrue(isinstance(exp_ok, list) and len(exp_ok) == 2)

            exported_json = self.plugin.store.export_session_json(
                "onebot:GroupMessage:10001"
            )
            imp_ok = await self.plugin.digest_command(
                FakeEvent(
                    f"/摘要 导入 {exported_json}",
                    umo="onebot:GroupMessage:20002",
                    admin=True,
                )
            )
            self.assertIn("合并导入完成", imp_ok)
            self.assertEqual(
                len(self.plugin.store.list_highlights("onebot:GroupMessage:20002")), 1
            )

            clr_warn = await self.plugin.digest_command(
                FakeEvent("/摘要 清空", admin=True)
            )
            self.assertIn("确认请发送", clr_warn)
            clr_ok = await self.plugin.digest_command(
                FakeEvent("/摘要 清空 确认", admin=True)
            )
            self.assertIn("已清空当前会话", clr_ok)
            self.assertEqual(
                self.plugin.store.session_stats("onebot:GroupMessage:10001")["digests"],
                0,
            )

        asyncio.run(_run())

    def test_daily_push_scheduler_dedupe_and_platform_filter(self) -> None:
        self.ctx._active = ["onebot"]
        now_day = datetime(2026, 11, 21, 21, 5, 0)
        # 活跃平台群：满足最小消息数
        self.plugin.store.set_subscription("onebot:GroupMessage:10001", True)
        self.plugin.store.append_message(
            "onebot:GroupMessage:10001", "1", "甲", "决定今晚发布新插件", now=now_day
        )
        self.plugin.store.append_message(
            "onebot:GroupMessage:10001", "2", "乙", "待办：同步文档和 CI", now=now_day
        )
        # 离线平台群：应被过滤跳过
        self.plugin.store.set_subscription("offline_bot:GroupMessage:888", True)
        self.plugin.store.append_message(
            "offline_bot:GroupMessage:888", "1", "甲", "消息1", now=now_day
        )
        self.plugin.store.append_message(
            "offline_bot:GroupMessage:888", "2", "乙", "消息2", now=now_day
        )

        # 未到时间点（20:30 < 21:00）不推送
        early = asyncio.run(
            self.plugin.run_daily_push_once(now=datetime(2026, 11, 21, 20, 30, 0))
        )
        self.assertEqual(early, [])

        # 到点后仅推送活跃平台群
        pushed = asyncio.run(self.plugin.run_daily_push_once(now=now_day))
        self.assertEqual(pushed, ["onebot:GroupMessage:10001"])
        self.assertEqual(len(self.ctx.sent), 1)

        # 同日重复运行不再重推
        pushed_again = asyncio.run(
            self.plugin.run_daily_push_once(now=datetime(2026, 11, 21, 21, 30, 0))
        )
        self.assertEqual(pushed_again, [])

    def test_async_initialize_and_terminate_lifecycle(self) -> None:
        async def _run():
            self.plugin.config["daily_push_enabled"] = True
            await self.plugin.initialize()
            self.assertIsNotNone(self.plugin._scheduler_task)
            self.assertFalse(self.plugin._scheduler_task.done())
            await self.plugin.terminate()
            self.assertIsNone(self.plugin._scheduler_task)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main(verbosity=1)
