# -*- coding: utf-8 -*-
"""AstrBot 群聊记忆与摘要助手插件（astrbot_plugin_chat_digest）。"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover
    get_astrbot_data_path = None

from .card_renderer import render_digest_card
try:
    from .hub_bridge import HUB_PROTOCOL_VERSION, emit_hub_event
except ImportError:
    from hub_bridge import HUB_PROTOCOL_VERSION, emit_hub_event
from .storage import DigestStore
from .summarizer import format_digest_text, generate_chat_digest, summarize_webpage
from .web_fetcher import UnsafeURLError, WebFetcher, extract_urls

PLUGIN_NAME = "astrbot_plugin_chat_digest"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = (
    "群聊/私聊记忆与智能摘要助手：LLM 摘要（自动降级本地规则）、精华收藏、决议/待办追踪、"
    "网页链接静默收录与摘要、RSS 订阅汇总、每日定时日报、Markdown/JSON 导入导出与摘要长图"
)
PLUGIN_VERSION = "1.1.1"
PLUGIN_REPO = "https://github.com/yunxiao258/astrbot_plugin_chat_digest"

_CMD_PREFIX_RE = re.compile(r"^[\\/／]?\s*(?:摘要|digest|纪要)\b\s*(.*)$", re.IGNORECASE | re.S)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _cfg_bool(cfg: Any, key: str, default: bool) -> bool:
    """安全解析布尔配置，避免字符串 'false' / '0' 被当成 True。"""
    if not isinstance(cfg, dict) or key not in cfg:
        return default
    val = cfg.get(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "on", "y"):
        return True
    if s in ("false", "0", "no", "off", "n", ""):
        return False
    return default


def _cfg_int(cfg: Any, key: str, default: int, min_val: int | None = None) -> int:
    """安全解析整数配置，不吞 0 值，脏值回退默认。"""
    if not isinstance(cfg, dict) or key not in cfg:
        val = default
    else:
        raw = cfg.get(key)
        if raw is None or isinstance(raw, bool):
            val = default
        else:
            try:
                val = int(raw)
            except (TypeError, ValueError):
                val = default
    if min_val is not None and val < min_val:
        return min_val
    return val


def _cfg_str(cfg: Any, key: str, default: str = "") -> str:
    if not isinstance(cfg, dict) or key not in cfg:
        return default
    raw = cfg.get(key)
    if raw is None:
        return default
    return str(raw).strip()


def _parse_hhmm(raw: str, default: tuple[int, int] = (22, 0)) -> tuple[int, int]:
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", (raw or "").strip())
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return (h, mi)
    return default


def _split_csv(raw: str) -> set[str]:
    return {x.strip() for x in (raw or "").split(",") if x.strip()}


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION, repo=PLUGIN_REPO)
class ChatDigestPlugin(Star):
    """群聊/私聊记忆与摘要助手主插件类。"""

    HUB_PROTOCOL_VERSION = HUB_PROTOCOL_VERSION

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
        data_dir: str | None = None,
    ) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config if isinstance(config, dict) else {}
        if data_dir:
            self.data_dir = os.path.abspath(data_dir)
        else:
            base = get_astrbot_data_path() if callable(get_astrbot_data_path) else os.path.dirname(os.path.abspath(__file__))
            self.data_dir = os.path.join(base, "plugin_data", PLUGIN_NAME)
        os.makedirs(self.data_dir, exist_ok=True)

        self.store = DigestStore(
            self.data_dir,
            max_messages_per_day=_cfg_int(self.config, "max_messages_per_day", 500, min_val=10),
            retention_days=_cfg_int(self.config, "retention_days", 30, min_val=1),
        )
        self._scheduler_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._capture_sem: asyncio.Semaphore | None = None
        self._fetcher_cache: WebFetcher | None = None
        logger.info(f"【{PLUGIN_NAME}】插件初始化完成，数据目录: {self.data_dir}")

    # ==================== 生命周期 ====================

    async def initialize(self) -> None:
        """AstrBot 异步生命周期钩子：启动每日定时日报巡检。"""
        if self._capture_sem is None:
            self._capture_sem = asyncio.Semaphore(3)
        if _cfg_bool(self.config, "daily_push_enabled", False):
            self._ensure_scheduler_task()

    def _ensure_scheduler_task(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._scheduler_task = loop.create_task(self._daily_scheduler_loop())

    async def terminate(self) -> None:
        """卸载清理：停止后台定时任务与抓取协程，释放 HTTP 会话并落盘。"""
        if self._scheduler_task is not None and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._scheduler_task = None

        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
            self._bg_tasks.clear()

        if self._fetcher_cache is not None:
            try:
                self._fetcher_cache.session.close()
            except Exception:
                pass
            self._fetcher_cache = None

        try:
            self.store.save_all()
        except Exception:
            pass
        logger.info(f"【{PLUGIN_NAME}】插件已卸载并清理后台任务")

    # ==================== 内部辅助 ====================

    def _get_fetcher(self) -> WebFetcher:
        if self._fetcher_cache is None:
            self._fetcher_cache = WebFetcher(
                proxy=_cfg_str(self.config, "proxy", ""),
                timeout=_cfg_int(self.config, "http_timeout_seconds", 15, min_val=3),
                max_bytes=_cfg_int(self.config, "max_fetch_bytes", 524288, min_val=4096),
                allow_private=_cfg_bool(self.config, "allow_private_urls", False),
            )
        return self._fetcher_cache

    def _get_umo(self, event: Any) -> str:
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            return str(umo)
        gid = getattr(event, "get_group_id", lambda: "")()
        if gid:
            return f"default:GroupMessage:{gid}"
        uid = getattr(event, "get_sender_id", lambda: "")()
        return f"default:FriendMessage:{uid or 'unknown'}"

    def _get_sender(self, event: Any) -> tuple[str, str]:
        uid = ""
        nick = ""
        try:
            uid = str(event.get_sender_id() or "")
        except Exception:
            pass
        try:
            nick = str(event.get_sender_name() or "")
        except Exception:
            pass
        return uid, nick or uid or "群友"

    def _is_admin(self, event: Any) -> bool:
        try:
            if callable(getattr(event, "is_admin", None)) and event.is_admin():
                return True
        except Exception:
            pass
        umo = self._get_umo(event)
        uid, _ = self._get_sender(event)
        if umo in _split_csv(_cfg_str(self.config, "admin_umos", "")):
            return True
        if uid and uid in _split_csv(_cfg_str(self.config, "admin_user_ids", "")):
            return True
        return False

    @staticmethod
    def _plain(event: Any, text: str) -> Any:
        if hasattr(event, "plain_result"):
            return event.plain_result(text)
        return MessageChain([Plain(text)])

    def _active_platform_ids(self) -> set[str]:
        pm = getattr(self.context, "platform_manager", None)
        if pm is None or not hasattr(pm, "get_insts"):
            return set()
        try:
            insts = pm.get_insts() or []
        except Exception:
            return set()
        ids: set[str] = set()
        for inst in insts:
            meta = getattr(inst, "meta", None)
            if callable(meta):
                try:
                    m = meta()
                    if getattr(m, "id", None):
                        ids.add(str(m.id))
                        continue
                except Exception:
                    pass
            pid = getattr(inst, "id", None) or getattr(inst, "client_self_id", None)
            if pid:
                ids.add(str(pid))
        return ids

    # ==================== 后台静默消息收集 & 链接收录 ====================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        """被动收集普通聊天消息，并在启用时静默抓取网页链接收录（始终返回 None，不拦截消息）。"""
        if not _cfg_bool(self.config, "enabled", True):
            return None
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return None
        # 跳过指令消息，不污染聊天摘要语料
        if text[0] in ("/", "／", "!", "！"):
            return None
        if _CMD_PREFIX_RE.match(text):
            return None

        umo = self._get_umo(event)
        uid, nick = self._get_sender(event)
        self.store.append_message(umo, uid, nick, text)

        if _cfg_bool(self.config, "auto_capture_links", True):
            urls = extract_urls(text, max_count=2)
            for url in urls:
                if self.store.has_link_today(umo, url):
                    continue
                self._schedule_silent_link_capture(umo, url, nick)
        return None

    def _schedule_silent_link_capture(self, umo: str, url: str, shared_by: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._capture_link_silent(umo, url, shared_by))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _capture_link_silent(self, umo: str, url: str, shared_by: str) -> dict[str, Any] | None:
        """后台静默抓取网页并生成简短摘要写入记忆库（不发群消息打扰）。"""
        if self._capture_sem is None:
            self._capture_sem = asyncio.Semaphore(3)
        try:
            async with self._capture_sem:
                page = await asyncio.to_thread(self._get_fetcher().fetch_page, url)
                summary = await summarize_webpage(
                    self.context,
                    title=page.title,
                    description=page.description,
                    text=page.text,
                    enable_llm=_cfg_bool(self.config, "enable_llm_summary", True),
                    provider_id=_cfg_str(self.config, "provider_id", ""),
                    umo=umo,
                    timeout=_cfg_int(self.config, "llm_timeout_seconds", 45, min_val=5),
                )
                rec = self.store.add_link(
                    umo=umo,
                    url=page.url,
                    title=page.title,
                    excerpt=page.description or page.text[:240],
                    summary=summary,
                    shared_by=shared_by,
                )
                emit_hub_event(PLUGIN_NAME, "digest.link_captured", {"umo": umo, "url": rec["url"], "title": rec["title"], "shared_by": shared_by}, self.context)
                return rec
        except Exception as exc:
            logger.debug(f"【{PLUGIN_NAME}】静默抓取链接失败 ({url}): {exc}")
            return None

    # ==================== 每日定时日报推送 ====================

    async def _daily_scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                await self.run_daily_push_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"【{PLUGIN_NAME}】定时日报巡检异常: {exc}")

    async def run_daily_push_once(self, now: datetime | None = None) -> list[str]:
        """执行一次定时日报检查与推送（方便单元测试直接调用），返回实际推送的 UMO 列表。"""
        dt = now or datetime.now()
        target_h, target_m = _parse_hhmm(_cfg_str(self.config, "daily_push_time", "22:00"), (22, 0))
        if (dt.hour, dt.minute) < (target_h, target_m):
            return []

        today = dt.strftime("%Y-%m-%d")
        targets = set(self.store.list_subscribed_umos())
        targets.update(_split_csv(_cfg_str(self.config, "daily_push_umos", "")))
        if not targets:
            return []

        min_msgs = _cfg_int(self.config, "daily_push_min_messages", 5, min_val=0)
        active_pids = self._active_platform_ids()
        pushed: list[str] = []

        for umo in sorted(targets):
            if self.store.get_last_push_date(umo) == today:
                continue
            if active_pids and ":" in umo:
                pid = umo.split(":", 1)[0]
                if pid not in active_pids:
                    logger.debug(f"【{PLUGIN_NAME}】跳过未激活平台会话 {umo}")
                    continue

            msgs = self.store.get_messages(umo, target_date=today)
            if len(msgs) < min_msgs:
                continue

            links = self.store.list_links(umo, target_date=today, limit=10)
            digest = await generate_chat_digest(
                self.context,
                messages=msgs,
                links=links,
                target_date=today,
                enable_llm=_cfg_bool(self.config, "enable_llm_summary", True),
                provider_id=_cfg_str(self.config, "provider_id", ""),
                umo=umo,
                timeout=_cfg_int(self.config, "llm_timeout_seconds", 45, min_val=5),
            )
            saved = self.store.save_digest(umo, digest)
            text = format_digest_text(saved, title="每日群聊记忆日报")
            emit_hub_event(PLUGIN_NAME, "digest.created", {"umo": umo, "date": today, "summary": saved.get("summary", ""), "text": text, "message_count": saved.get("message_count", 0)}, self.context)

            sub_cfg = self.store.get_subscription(umo)
            want_card = (
                _cfg_bool(self.config, "enable_card_image", True)
                and (_cfg_bool(self.config, "daily_push_card", False) or bool(sub_cfg.get("send_card")))
            )
            chain = MessageChain([Plain(text)])
            if want_card:
                card_path = os.path.join(self.data_dir, "exports", f"daily_{saved['id']}.png")
                rendered = await asyncio.to_thread(render_digest_card, saved, card_path, "每日群聊记忆日报")
                if rendered:
                    chain = MessageChain([Image.fromFileSystem(rendered), Plain("\n" + text)])

            try:
                await self.context.send_message(umo, chain)
                self.store.mark_pushed(umo, today)
                pushed.append(umo)
            except Exception as exc:
                logger.warning(f"【{PLUGIN_NAME}】向 {umo} 推送日报失败 ({type(exc).__name__}): {exc}")
        return pushed

    # ==================== 统一主指令：/摘要（别名 /digest、/纪要） ====================

    @filter.command("摘要", alias={"digest", "纪要"}, priority=200)
    async def digest_command(self, event: AstrMessageEvent) -> Any:
        """统一指令入口：分发全部子功能，不占用其他顶层命令。"""
        raw_text = str(getattr(event, "message_str", "") or "").strip()
        m = _CMD_PREFIX_RE.match(raw_text)
        rest = (m.group(1) if m else raw_text).strip()

        if not rest:
            return await self._handle_generate_digest(event, "")

        parts = rest.split(None, 1)
        sub = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("帮助", "help", "-h", "--help", "?"):
            return self._plain(event, self._help_text())
        if sub in ("精华", "highlight", "hl"):
            return await self._handle_highlights(event, arg)
        if sub in ("待办", "todo", "todos"):
            return await self._handle_todos(event, arg)
        if sub in ("搜索", "搜", "search", "find"):
            return await self._handle_search(event, arg)
        if sub in ("回顾", "历史", "history", "recap"):
            return await self._handle_recap(event, arg)
        if sub in ("链接", "网页", "link", "links", "url"):
            return await self._handle_links(event, arg)
        if sub in ("rss", "订阅源"):
            return await self._handle_rss(event, arg)
        if sub in ("订阅", "日报", "subscribe", "sub"):
            return await self._handle_subscription(event, arg)
        if sub in ("导出", "export"):
            return await self._handle_export(event, arg)
        if sub in ("导入", "import"):
            return await self._handle_import(event, arg)
        if sub in ("状态", "status", "stat"):
            return await self._handle_status(event)
        if sub in ("清空", "clear", "reset"):
            return await self._handle_clear(event, arg)

        # 默认视为摘要参数：今日 / 条数 / YYYY-MM-DD / --card
        return await self._handle_generate_digest(event, rest)

    # ==================== 子指令实现 ====================

    async def _handle_generate_digest(self, event: Any, arg: str) -> Any:
        umo = self._get_umo(event)
        tokens = arg.split() if arg else []
        want_card = False
        cleaned_tokens: list[str] = []
        for tok in tokens:
            if tok.lower() in ("--card", "-c", "图", "图片", "长图"):
                want_card = True
            else:
                cleaned_tokens.append(tok)

        target_date: str | None = None
        limit: int | None = None
        if not cleaned_tokens or cleaned_tokens[0] in ("今日", "今天", "today"):
            target_date = datetime.now().strftime("%Y-%m-%d")
            msgs = self.store.get_messages(umo, target_date=target_date)
            if not msgs:
                # 若今日暂无消息，自动回退最近 N 条历史消息
                target_date = None
                limit = _cfg_int(self.config, "default_message_limit", 120, min_val=5)
                msgs = self.store.get_messages(umo, limit=limit)
        elif _DATE_RE.match(cleaned_tokens[0]):
            target_date = cleaned_tokens[0]
            msgs = self.store.get_messages(umo, target_date=target_date)
        elif cleaned_tokens[0].isdigit():
            limit = max(5, min(500, int(cleaned_tokens[0])))
            msgs = self.store.get_messages(umo, limit=limit)
        else:
            return self._plain(
                event,
                f"❌ 未识别参数「{cleaned_tokens[0]}」。发送 `/摘要 帮助` 查看完整用法。",
            )

        if not msgs:
            scope = f" {target_date} " if target_date else ""
            return self._plain(event, f"📭 当前会话{scope}暂无可总结的聊天记录，聊几句后再试试吧。")

        date_label = target_date or datetime.now().strftime("%Y-%m-%d")
        links = self.store.list_links(umo, target_date=target_date, limit=10)
        digest = await generate_chat_digest(
            self.context,
            messages=msgs,
            links=links,
            target_date=date_label,
            enable_llm=_cfg_bool(self.config, "enable_llm_summary", True),
            provider_id=_cfg_str(self.config, "provider_id", ""),
            umo=umo,
            timeout=_cfg_int(self.config, "llm_timeout_seconds", 45, min_val=5),
        )
        saved = self.store.save_digest(umo, digest)
        text = format_digest_text(saved)
        emit_hub_event(PLUGIN_NAME, "digest.created", {"umo": umo, "date": date_label, "summary": saved.get("summary", ""), "text": text, "message_count": saved.get("message_count", 0)}, self.context)

        if want_card and _cfg_bool(self.config, "enable_card_image", True):
            card_path = os.path.join(self.data_dir, "exports", f"card_{saved['id']}.png")
            rendered = await asyncio.to_thread(render_digest_card, saved, card_path)
            if rendered and hasattr(event, "chain_result"):
                return event.chain_result([Image.fromFileSystem(rendered), Plain("\n" + text)])
        return self._plain(event, text)

    async def _handle_highlights(self, event: Any, arg: str) -> Any:
        umo = self._get_umo(event)
        _, nick = self._get_sender(event)
        if not arg or arg.lower() in ("列表", "list"):
            items = self.store.list_highlights(umo, limit=15)
            if not items:
                return self._plain(event, "✨ 本会话暂无精华记忆。使用 `/摘要 精华 添加 <内容> [#标签]` 收藏。")
            lines = [f"✨ 本会话精华记忆（最新 {len(items)} 条）："]
            for h in items:
                tags = f" [{' '.join('#' + t for t in h['tags'])}]" if h.get("tags") else ""
                lines.append(f"• [{h['id']}] {h['text']}{tags} — {h.get('added_by', '')}")
            return self._plain(event, "\n".join(lines))

        parts = arg.split(None, 1)
        act = parts[0].lower()
        body = parts[1].strip() if len(parts) > 1 else ""
        if act in ("添加", "add", "+"):
            if not body:
                return self._plain(event, "❌ 用法：`/摘要 精华 添加 <内容> [#标签]`")
            tags = re.findall(r"#([^\s#]{1,20})", body)
            clean_text = re.sub(r"#[^\s#]{1,20}", "", body).strip() or body
            item = self.store.add_highlight(umo, clean_text, nick, tags=tags)
            emit_hub_event(PLUGIN_NAME, "digest.highlight_added", {"umo": umo, "id": item["id"], "text": item["text"], "added_by": nick}, self.context)
            return self._plain(event, f"✅ 已收藏精华 [{item['id']}]：{item['text']}")
        if act in ("删除", "删", "del", "rm"):
            if not self._is_admin(event):
                return self._plain(event, "⛔ 删除精华记忆仅限管理员操作。")
            if not body:
                return self._plain(event, "❌ 用法：`/摘要 精华 删除 <ID>`")
            ok = self.store.delete_highlight(umo, body)
            return self._plain(event, f"🗑️ 已删除精华 [{body}]" if ok else f"❌ 未找到精华 ID：{body}")
        # 快捷用法：`/摘要 精华 <直接内容>` 视为添加
        item = self.store.add_highlight(umo, arg, nick)
        return self._plain(event, f"✅ 已收藏精华 [{item['id']}]：{item['text']}")

    async def _handle_todos(self, event: Any, arg: str) -> Any:
        umo = self._get_umo(event)
        _, nick = self._get_sender(event)
        if not arg or arg.lower() in ("列表", "list", "all", "全部"):
            include_done = arg.lower() in ("all", "全部")
            items = self.store.list_todos(umo, include_done=include_done)
            if not items:
                return self._plain(event, "📋 当前没有待办事项。使用 `/摘要 待办 添加 <内容> [@负责人]` 新增。")
            lines = [f"📋 本会话待办清单（共 {len(items)} 项）："]
            for t in items:
                box = "☑" if t.get("status") == "done" else "☐"
                who = f" (@{t['assignee']})" if t.get("assignee") else ""
                lines.append(f"{box} [{t['id']}] {t['title']}{who}")
            return self._plain(event, "\n".join(lines))

        parts = arg.split(None, 1)
        act = parts[0].lower()
        body = parts[1].strip() if len(parts) > 1 else ""
        if act in ("添加", "add", "+"):
            if not body:
                return self._plain(event, "❌ 用法：`/摘要 待办 添加 <内容> [@负责人]`")
            m = re.search(r"@([^\s@]{1,30})", body)
            assignee = m.group(1) if m else ""
            title = re.sub(r"@[^\s@]{1,30}", "", body).strip() or body
            item = self.store.add_todo(umo, title, nick, assignee=assignee)
            emit_hub_event(PLUGIN_NAME, "digest.todo_added", {"umo": umo, "id": item["id"], "title": item["title"], "assignee": assignee, "created_by": nick}, self.context)
            who = f"（负责：@{assignee}）" if assignee else ""
            return self._plain(event, f"✅ 已新增待办 [{item['id']}]：{item['title']}{who}")
        if act in ("完成", "done", "ok"):
            if not body:
                return self._plain(event, "❌ 用法：`/摘要 待办 完成 <ID>`")
            item = self.store.complete_todo(umo, body)
            return self._plain(event, f"🎉 待办已完成 [{body}]：{item['title']}" if item else f"❌ 未找到待办 ID：{body}")
        if act in ("删除", "删", "del", "rm"):
            if not self._is_admin(event):
                return self._plain(event, "⛔ 删除待办事项仅限管理员操作。")
            if not body:
                return self._plain(event, "❌ 用法：`/摘要 待办 删除 <ID>`")
            ok = self.store.delete_todo(umo, body)
            return self._plain(event, f"🗑️ 已删除待办 [{body}]" if ok else f"❌ 未找到待办 ID：{body}")
        item = self.store.add_todo(umo, arg, nick)
        return self._plain(event, f"✅ 已新增待办 [{item['id']}]：{item['title']}")

    async def _handle_search(self, event: Any, arg: str) -> Any:
        umo = self._get_umo(event)
        if not arg:
            return self._plain(event, "❌ 用法：`/摘要 搜索 <关键词>`")
        res = self.store.search(umo, arg, limit=5)
        total = sum(len(v) for v in res.values())
        if total == 0:
            return self._plain(event, f"🔍 未找到与「{arg}」相关的记忆、摘要或消息。")

        lines = [f"🔍 检索「{arg}」共命中 {total} 条："]
        if res["highlights"]:
            lines.append("\n✨ 精华记忆：")
            for h in res["highlights"]:
                lines.append(f"  • [{h['id']}] {h['text'][:90]}")
        if res["todos"]:
            lines.append("\n📋 待办事项：")
            for t in res["todos"]:
                mark = "☑" if t.get("status") == "done" else "☐"
                lines.append(f"  • {mark} [{t['id']}] {t['title'][:90]}")
        if res["links"]:
            lines.append("\n🔗 收录链接：")
            for lk in res["links"]:
                lines.append(f"  • [{lk['id']}] {lk['title'][:60]} ({lk['url']})")
        if res["digests"]:
            lines.append("\n📝 历史摘要：")
            for d in res["digests"]:
                lines.append(f"  • [{d.get('date', '')}] {str(d.get('summary', ''))[:90]}")
        if res["messages"]:
            lines.append("\n💬 近期消息：")
            for msg in res["messages"]:
                lines.append(f"  • [{str(msg.get('ts', ''))[5:16]}] {msg.get('nickname', '')}: {str(msg.get('text', ''))[:80]}")
        return self._plain(event, "\n".join(lines))

    async def _handle_recap(self, event: Any, arg: str) -> Any:
        umo = self._get_umo(event)
        if arg and _DATE_RE.match(arg):
            items = self.store.list_digests(umo, target_date=arg, limit=3)
            if not items:
                return self._plain(event, f"📅 未找到 {arg} 的历史摘要档案。")
            return self._plain(event, format_digest_text(items[0], title="历史摘要回顾"))

        limit = int(arg) if arg.isdigit() else 5
        items = self.store.list_digests(umo, limit=max(1, min(15, limit)))
        if not items:
            return self._plain(event, "📚 本会话暂无历史摘要档案。发送 `/摘要` 生成第一份吧。")
        lines = [f"📚 最近 {len(items)} 份摘要档案："]
        for d in items:
            mode = "AI" if d.get("mode") == "llm" else "规则"
            lines.append(f"• [{d.get('date', '')}] ({mode}/{d.get('message_count', 0)}条) {str(d.get('summary', ''))[:95]}")
        lines.append("\n💡 发送 `/摘要 回顾 YYYY-MM-DD` 查看指定日期完整摘要。")
        return self._plain(event, "\n".join(lines))

    async def _handle_links(self, event: Any, arg: str) -> Any:
        umo = self._get_umo(event)
        _, nick = self._get_sender(event)
        if not arg or arg.lower() in ("列表", "list"):
            items = self.store.list_links(umo, limit=12)
            if not items:
                return self._plain(event, "🔗 本会话暂无收录链接。群里分享链接会自动静默收录，或发送 `/摘要 链接 <URL>` 手动抓取摘要。")
            lines = [f"🔗 本会话收录链接（最新 {len(items)} 条）："]
            for lk in items:
                lines.append(f"• [{lk['id']}] {lk['title']}\n  {lk['url']}")
                if lk.get("summary"):
                    lines.append(f"  摘要：{lk['summary'][:120]}")
            return self._plain(event, "\n".join(lines))

        parts = arg.split(None, 1)
        if parts[0].lower() in ("删除", "删", "del", "rm"):
            if not self._is_admin(event):
                return self._plain(event, "⛔ 删除收录链接仅限管理员操作。")
            target_id = parts[1].strip() if len(parts) > 1 else ""
            if not target_id:
                return self._plain(event, "❌ 用法：`/摘要 链接 删除 <ID>`")
            ok = self.store.delete_link(umo, target_id)
            return self._plain(event, f"🗑️ 已删除链接 [{target_id}]" if ok else f"❌ 未找到链接 ID：{target_id}")

        url = parts[0]
        try:
            page = await asyncio.to_thread(self._get_fetcher().fetch_page, url)
        except UnsafeURLError as exc:
            return self._plain(event, f"🛡️ 安全拦截：{exc}")
        except Exception as exc:
            return self._plain(event, f"❌ 网页抓取失败：{exc}")

        summary = await summarize_webpage(
            self.context,
            title=page.title,
            description=page.description,
            text=page.text,
            enable_llm=_cfg_bool(self.config, "enable_llm_summary", True),
            provider_id=_cfg_str(self.config, "provider_id", ""),
            umo=umo,
            timeout=_cfg_int(self.config, "llm_timeout_seconds", 45, min_val=5),
        )
        saved = self.store.add_link(
            umo=umo,
            url=page.url,
            title=page.title,
            excerpt=page.description or page.text[:240],
            summary=summary,
            shared_by=nick,
        )
        return self._plain(
            event,
            f"🔗 网页已抓取并收录 [{saved['id']}]\n标题：{saved['title']}\n链接：{saved['url']}\n摘要：{saved['summary']}",
        )

    async def _handle_rss(self, event: Any, arg: str) -> Any:
        umo = self._get_umo(event)
        if not arg or arg.lower() in ("列表", "list"):
            feeds = self.store.list_rss_feeds(umo)
            if not feeds:
                return self._plain(event, "📡 本会话尚未订阅 RSS。管理员可用 `/摘要 rss 添加 <名称> <URL>` 添加。")
            lines = [f"📡 本会话 RSS 订阅（共 {len(feeds)} 个）："]
            for f in feeds:
                lines.append(f"• [{f['id']}] {f['name']} — {f['url']}")
            lines.append("\n💡 发送 `/摘要 rss 拉取` 汇总最新条目。")
            return self._plain(event, "\n".join(lines))

        parts = arg.split(None, 2)
        act = parts[0].lower()
        if act in ("添加", "add", "+"):
            if not self._is_admin(event):
                return self._plain(event, "⛔ 管理 RSS 订阅仅限管理员操作。")
            if len(parts) < 2:
                return self._plain(event, "❌ 用法：`/摘要 rss 添加 <名称> <URL>` 或 `/摘要 rss 添加 <URL>`")
            if len(parts) == 2:
                name, url = parts[1], parts[1]
            else:
                name, url = parts[1], parts[2]
            try:
                title, items = await asyncio.to_thread(
                    self._get_fetcher().fetch_rss,
                    url,
                    _cfg_int(self.config, "rss_max_items", 5, min_val=1),
                )
            except UnsafeURLError as exc:
                return self._plain(event, f"🛡️ 安全拦截：{exc}")
            except Exception as exc:
                return self._plain(event, f"❌ RSS 校验抓取失败：{exc}")
            feed = self.store.add_rss_feed(umo, title if name == url and title else name, url)
            return self._plain(
                event,
                f"✅ 已订阅 RSS [{feed['id']}]：{feed['name']}（首检读取 {len(items)} 条）",
            )
        if act in ("删除", "删", "del", "rm"):
            if not self._is_admin(event):
                return self._plain(event, "⛔ 管理 RSS 订阅仅限管理员操作。")
            if len(parts) < 2:
                return self._plain(event, "❌ 用法：`/摘要 rss 删除 <ID>`")
            ok = self.store.delete_rss_feed(umo, parts[1])
            return self._plain(event, f"🗑️ 已移除 RSS [{parts[1]}]" if ok else f"❌ 未找到 RSS ID：{parts[1]}")
        if act in ("拉取", "刷新", "fetch", "now"):
            feeds = self.store.list_rss_feeds(umo)
            if not feeds:
                return self._plain(event, "📡 本会话尚未订阅任何 RSS 源。")
            max_items = _cfg_int(self.config, "rss_max_items", 5, min_val=1)
            lines = ["📡 RSS 最新摘要汇总："]
            for feed in feeds[:8]:
                try:
                    _, items = await asyncio.to_thread(self._get_fetcher().fetch_rss, feed["url"], max_items)
                    lines.append(f"\n【{feed['name']}】")
                    if not items:
                        lines.append("  （暂无条目）")
                    for idx, it in enumerate(items, 1):
                        lines.append(f"  {idx}. {it.title}")
                        if it.link:
                            lines.append(f"     {it.link}")
                except Exception as exc:
                    lines.append(f"\n【{feed['name']}】抓取失败：{exc}")
            return self._plain(event, "\n".join(lines))
        return self._plain(event, "❌ 用法：`/摘要 rss [添加 <名称> <URL> | 列表 | 拉取 | 删除 <ID>]`")

    async def _handle_subscription(self, event: Any, arg: str) -> Any:
        umo = self._get_umo(event)
        sub = self.store.get_subscription(umo)
        if not arg or arg.lower() in ("状态", "status"):
            st = "✅ 已开启" if sub["enabled"] else "⏸️ 未开启"
            push_time = _cfg_str(self.config, "daily_push_time", "22:00")
            glob = "开启" if _cfg_bool(self.config, "daily_push_enabled", False) else "未开启（需在配置开启 daily_push_enabled）"
            return self._plain(
                event,
                f"🗓️ 本会话每日日报订阅：{st}\n"
                f"推送时间：{push_time} | 卡片长图：{'开' if sub.get('send_card') else '关'}\n"
                f"上次推送：{sub.get('last_push_date') or '暂无'}\n"
                f"全局巡检：{glob}",
            )
        if not self._is_admin(event):
            return self._plain(event, "⛔ 修改每日日报订阅状态仅限管理员操作。")
        low = arg.lower()
        if low in ("on", "开启", "开", "enable", "card"):
            send_card = True if low == "card" else None
            self.store.set_subscription(umo, True, send_card=send_card)
            if _cfg_bool(self.config, "daily_push_enabled", False):
                self._ensure_scheduler_task()
            return self._plain(event, f"✅ 已为当前会话开启每日记忆日报推送（每日 {_cfg_str(self.config, 'daily_push_time', '22:00')}）。")
        if low in ("off", "关闭", "关", "disable"):
            self.store.set_subscription(umo, False)
            return self._plain(event, "⏸️ 已关闭当前会话的每日记忆日报推送。")
        return self._plain(event, "❌ 用法：`/摘要 订阅 [on|off|card|状态]`")

    async def _handle_export(self, event: Any, arg: str) -> Any:
        if not self._is_admin(event):
            return self._plain(event, "⛔ 导出会话记忆档案仅限管理员操作。")
        umo = self._get_umo(event)
        fmt = (arg or "md").strip().lower()
        if fmt in ("card", "img", "png", "图", "长图"):
            if not _cfg_bool(self.config, "enable_card_image", True):
                return self._plain(event, "❌ 摘要长图已在配置中关闭（enable_card_image=false）。")
            digests = self.store.list_digests(umo, limit=1)
            if not digests:
                return self._plain(event, "📭 当前会话还没有历史摘要，请先运行 `/摘要` 生成。")
            card_path = os.path.join(self.data_dir, "exports", f"export_{digests[0]['id']}.png")
            rendered = await asyncio.to_thread(render_digest_card, digests[0], card_path)
            if not rendered:
                return self._plain(event, "⚠️ 当前环境未安装 Pillow 或渲染失败，已自动降级文本：\n\n" + format_digest_text(digests[0]))
            if hasattr(event, "chain_result"):
                return event.chain_result([Image.fromFileSystem(rendered)])
            return self._plain(event, f"🖼️ 摘要长图已生成：{rendered}")

        if fmt not in ("md", "markdown", "json"):
            return self._plain(event, "❌ 支持的导出格式：`md`、`json`、`card`")

        real_fmt = "json" if fmt == "json" else "md"
        path = self.store.write_export_file(umo, fmt=real_fmt)
        fname = os.path.basename(path)
        if hasattr(event, "chain_result"):
            return event.chain_result([
                Plain(f"📦 已导出本会话记忆档案：{fname}\n"),
                File(name=fname, file=path),
            ])
        return self._plain(event, f"📦 已导出本会话记忆档案：{path}")

    async def _handle_import(self, event: Any, arg: str) -> Any:
        if not self._is_admin(event):
            return self._plain(event, "⛔ 导入会话记忆档案仅限管理员操作。")
        if not arg:
            return self._plain(event, "❌ 用法：`/摘要 导入 [--replace] <JSON内容>`")
        replace = False
        raw = arg
        if raw.startswith("--replace"):
            replace = True
            raw = raw[len("--replace") :].strip()
        try:
            counts = self.store.import_session_json(self._get_umo(event), raw, replace=replace)
        except Exception as exc:
            return self._plain(event, f"❌ JSON 导入失败：{exc}")
        mode = "覆盖导入" if replace else "合并导入"
        return self._plain(
            event,
            f"✅ {mode}完成：精华 +{counts['highlights']}、待办 +{counts['todos']}、"
            f"链接 +{counts['links']}、RSS +{counts['rss_feeds']}、摘要 +{counts['digests']}",
        )

    async def _handle_status(self, event: Any) -> Any:
        umo = self._get_umo(event)
        st = self.store.session_stats(umo)
        proxy = _cfg_str(self.config, "proxy", "") or "系统默认/直连"
        return self._plain(
            event,
            f"📊 群聊记忆与摘要助手 状态（v{PLUGIN_VERSION}）\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💬 消息缓存：今日 {st['today_messages']} 条 / 保留期共 {st['total_messages']} 条\n"
            f"📝 历史摘要：{st['digests']} 份\n"
            f"✨ 精华记忆：{st['highlights']} 条\n"
            f"📋 待办事项：未完成 {st['open_todos']} / 总计 {st['total_todos']}\n"
            f"🔗 收录链接：{st['links']} 条（自动静默收录：{'开' if _cfg_bool(self.config, 'auto_capture_links', True) else '关'}）\n"
            f"📡 RSS 订阅：{st['rss_feeds']} 个\n"
            f"🗓️ 每日日报：{'已订阅' if st['subscribed'] else '未订阅'}（上次：{st['last_push_date'] or '无'}）\n"
            f"🤖 LLM 摘要：{'优先 LLM + 规则兜底' if _cfg_bool(self.config, 'enable_llm_summary', True) else '纯本地规则'}\n"
            f"🌐 网络代理：{proxy}",
        )

    async def _handle_clear(self, event: Any, arg: str) -> Any:
        if not self._is_admin(event):
            return self._plain(event, "⛔ 清空会话记忆仅限管理员操作。")
        if arg.strip() not in ("确认", "confirm", "--yes"):
            return self._plain(event, "⚠️ 该操作将清空本会话全部聊天缓存、精华、待办、链接与摘要档案。确认请发送：`/摘要 清空 确认`")
        self.store.clear_session(self._get_umo(event))
        return self._plain(event, "🧹 已清空当前会话的全部记忆与摘要数据。")

    @staticmethod
    def _help_text() -> str:
        return (
            f"📖 群聊记忆与摘要助手 v{PLUGIN_VERSION}（别名 /digest、/纪要）\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "• `/摘要 [今日|条数|YYYY-MM-DD] [--card]`：生成会话摘要（优先 LLM，自动降级规则）\n"
            "• `/摘要 精华 [添加 <内容> [#标签] | 列表 | 删除 <ID>]`：收藏/管理精华记忆\n"
            "• `/摘要 待办 [添加 <内容> [@负责人] | 列表 | 全部 | 完成 <ID> | 删除 <ID>]`：管理决议待办\n"
            "• `/摘要 搜索 <关键词>`：跨摘要/精华/待办/链接/近期消息检索\n"
            "• `/摘要 回顾 [YYYY-MM-DD | N]`：查看历史摘要档案\n"
            "• `/摘要 链接 [<URL> | 列表 | 删除 <ID>]`：手动摘要网页或查看自动静默收录的链接\n"
            "• `/摘要 rss [添加 <名称> <URL> | 列表 | 拉取 | 删除 <ID>]`：RSS 订阅汇总\n"
            "• `/摘要 订阅 [on|off|card|状态]`：开关本会话每日定时日报（管理员）\n"
            "• `/摘要 导出 [md|json|card]`：导出档案文件或摘要长图（管理员）\n"
            "• `/摘要 导入 [--replace] <JSON>`：导入恢复记忆档案（管理员）\n"
            "• `/摘要 状态` / `/摘要 清空 确认`：查看状态或清空本会话数据"
        )

    # ==================== 插件生态中枢标准联动协议 ====================

    def hub_status(self) -> dict[str, Any]:
        umos = set(self.store._messages.keys()) | set(self.store._memories.keys()) | set(self.store._digests.keys())
        total_msgs = sum(self.store.count_messages(u) for u in umos)
        total_digests = sum(len(self.store._digests.get(u, []) or []) for u in umos)
        total_hl = sum(len(self.store.list_highlights(u, limit=500)) for u in umos)
        total_todos = sum(len(self.store.list_todos(u, include_done=False)) for u in umos)
        return {
            "version": PLUGIN_VERSION,
            "status": "online",
            "metrics": {"会话数": len(umos), "消息缓存": total_msgs, "摘要档案": total_digests, "精华": total_hl, "待办": total_todos},
            "actions": ["get_overview", "add_highlight", "add_todo", "generate_digest"],
        }

    async def hub_action(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        p = params or {}
        if action == "get_overview":
            umos = sorted(set(self.store._messages.keys()) | set(self.store._memories.keys()) | set(self.store._digests.keys()))
            return {"ok": True, "data": {u: self.store.session_stats(u) for u in umos[:30]}}
        if action == "add_highlight":
            umo = str(p.get("umo", "")).strip()
            text = str(p.get("text", "")).strip()
            if not umo or not text:
                return {"ok": False, "error": "缺少 umo 或 text"}
            item = self.store.add_highlight(umo, text, str(p.get("added_by", "hub")), tags=list(p.get("tags") or []))
            return {"ok": True, "data": item}
        if action == "add_todo":
            umo = str(p.get("umo", "")).strip()
            title = str(p.get("title", "")).strip()
            if not umo or not title:
                return {"ok": False, "error": "缺少 umo 或 title"}
            item = self.store.add_todo(umo, title, str(p.get("created_by", "hub")), assignee=str(p.get("assignee", "")))
            return {"ok": True, "data": item}
        if action == "generate_digest":
            umo = str(p.get("umo", "")).strip()
            if not umo:
                return {"ok": False, "error": "缺少 umo"}
            msgs = self.store.get_messages(umo, limit=_cfg_int(self.config, "default_message_limit", 120, min_val=5))
            if not msgs:
                return {"ok": False, "error": "该会话暂无消息可生成摘要"}
            today = datetime.now().strftime("%Y-%m-%d")
            digest = await generate_chat_digest(self.context, msgs, self.store.list_links(umo, limit=10), today, _cfg_bool(self.config, "enable_llm_summary", True), _cfg_str(self.config, "provider_id", ""), umo, _cfg_int(self.config, "llm_timeout_seconds", 45, min_val=5))
            saved = self.store.save_digest(umo, digest)
            return {"ok": True, "data": saved}
        return {"ok": False, "error": f"不支持的动作：{action}"}
