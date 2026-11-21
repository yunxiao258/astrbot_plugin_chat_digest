# -*- coding: utf-8 -*-
"""会话隔离的原子 JSON 持久化存储、全文检索与 Markdown/JSON 导入导出。"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import date, datetime, timedelta
from typing import Any


def atomic_write_json(path: str, data: Any, do_fsync: bool = False) -> None:
    """原子写入 JSON（临时文件 + os.replace），防止写入中断导致文件损坏；高频路径默认不阻塞 fsync。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            if do_fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def safe_read_json(path: str, default: Any) -> Any:
    """容错读取 JSON，不存在或损坏时回退默认值副本。"""
    if not os.path.isfile(path):
        return json.loads(json.dumps(default))
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if type(data) is not type(default):
            return json.loads(json.dumps(default))
        return data
    except Exception:
        return json.loads(json.dumps(default))


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")


def _today_str(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


class DigestStore:
    """按会话 UMO 隔离管理聊天记录、历史摘要、精华、待办、链接、RSS 与订阅。"""

    def __init__(
        self,
        data_dir: str,
        max_messages_per_day: int = 500,
        retention_days: int = 30,
    ) -> None:
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.max_messages_per_day = max(10, int(max_messages_per_day))
        self.retention_days = max(1, int(retention_days))

        self.messages_path = os.path.join(self.data_dir, "messages.json")
        self.digests_path = os.path.join(self.data_dir, "digests.json")
        self.memories_path = os.path.join(self.data_dir, "memories.json")
        self.subs_path = os.path.join(self.data_dir, "subscriptions.json")
        self.exports_dir = os.path.join(self.data_dir, "exports")

        self._messages: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._digests: dict[str, list[dict[str, Any]]] = {}
        self._memories: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._subs: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        """从磁盘重新加载所有数据文件（先清空内存避免残留）。"""
        self._messages = safe_read_json(self.messages_path, {})
        self._digests = safe_read_json(self.digests_path, {})
        self._memories = safe_read_json(self.memories_path, {})
        self._subs = safe_read_json(self.subs_path, {})

    def _save_messages(self) -> None:
        atomic_write_json(self.messages_path, self._messages)

    def _save_digests(self) -> None:
        atomic_write_json(self.digests_path, self._digests)

    def _save_memories(self) -> None:
        atomic_write_json(self.memories_path, self._memories)

    def _save_subs(self) -> None:
        atomic_write_json(self.subs_path, self._subs)

    def save_all(self) -> None:
        atomic_write_json(self.messages_path, self._messages, do_fsync=True)
        atomic_write_json(self.digests_path, self._digests, do_fsync=True)
        atomic_write_json(self.memories_path, self._memories, do_fsync=True)
        atomic_write_json(self.subs_path, self._subs, do_fsync=True)

    def _ensure_memory_bucket(self, umo: str) -> dict[str, list[dict[str, Any]]]:
        bucket = self._memories.get(umo)
        if not isinstance(bucket, dict):
            bucket = {}
            self._memories[umo] = bucket
        for k in ("highlights", "todos", "links", "rss_feeds"):
            if not isinstance(bucket.get(k), list):
                bucket[k] = []
        return bucket

    # ==================== 聊天记录 ====================

    def append_message(
        self,
        umo: str,
        user_id: str,
        nickname: str,
        text: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        clean = (text or "").strip()
        if not umo or not clean:
            return None
        dt = now or datetime.now()
        day = _today_str(dt)
        sess = self._messages.setdefault(umo, {})
        if not isinstance(sess, dict):
            sess = {}
            self._messages[umo] = sess
        day_list = sess.setdefault(day, [])
        if not isinstance(day_list, list):
            day_list = []
            sess[day] = day_list

        entry = {
            "ts": _now_iso(dt),
            "user_id": str(user_id or ""),
            "nickname": str(nickname or user_id or "群友")[:40],
            "text": clean[:1000],
        }
        day_list.append(entry)
        if len(day_list) > self.max_messages_per_day:
            sess[day] = day_list[-self.max_messages_per_day :]

        self._prune_session_dates(umo, dt.date())
        self._save_messages()
        return entry

    def _prune_session_dates(self, umo: str, today: date) -> None:
        cutoff = today - timedelta(days=self.retention_days)
        sess = self._messages.get(umo)
        if isinstance(sess, dict):
            expired = []
            for d_str in list(sess.keys()):
                try:
                    if date.fromisoformat(d_str) < cutoff:
                        expired.append(d_str)
                except ValueError:
                    continue
            for d_str in expired:
                sess.pop(d_str, None)

        digests = self._digests.get(umo)
        if isinstance(digests, list):
            kept = []
            for item in digests:
                d_str = str(item.get("date", ""))
                try:
                    if date.fromisoformat(d_str) >= cutoff:
                        kept.append(item)
                except ValueError:
                    kept.append(item)
            if len(kept) != len(digests):
                self._digests[umo] = kept
                self._save_digests()

    def get_messages(
        self,
        umo: str,
        target_date: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sess = self._messages.get(umo)
        if not isinstance(sess, dict):
            return []
        if target_date:
            msgs = list(sess.get(target_date, []) or [])
        else:
            msgs = []
            for d_str in sorted(sess.keys()):
                day_items = sess.get(d_str)
                if isinstance(day_items, list):
                    msgs.extend(day_items)
        if limit is not None and limit > 0:
            msgs = msgs[-limit:]
        return msgs

    def count_messages(self, umo: str, target_date: str | None = None) -> int:
        return len(self.get_messages(umo, target_date=target_date))

    # ==================== 摘要档案 ====================

    def save_digest(self, umo: str, digest: dict[str, Any]) -> dict[str, Any]:
        record = dict(digest)
        record.setdefault("id", _short_id())
        record.setdefault("date", _today_str())
        record.setdefault("created_at", _now_iso())
        lst = self._digests.setdefault(umo, [])
        if not isinstance(lst, list):
            lst = []
            self._digests[umo] = lst
        lst.append(record)
        if len(lst) > max(60, self.retention_days * 3):
            self._digests[umo] = lst[-max(60, self.retention_days * 3) :]
        self._save_digests()
        return record

    def list_digests(
        self,
        umo: str,
        target_date: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        lst = self._digests.get(umo)
        if not isinstance(lst, list):
            return []
        if target_date:
            items = [d for d in lst if d.get("date") == target_date]
        else:
            items = list(lst)
        return list(reversed(items[-max(1, limit) :]))

    # ==================== 精华记忆 ====================

    def add_highlight(
        self,
        umo: str,
        text: str,
        added_by: str,
        tags: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        bucket = self._ensure_memory_bucket(umo)
        item = {
            "id": _short_id(),
            "text": text.strip()[:600],
            "added_by": str(added_by or "群友")[:40],
            "tags": [t.strip()[:24] for t in (tags or []) if t.strip()][:5],
            "created_at": _now_iso(now),
        }
        bucket["highlights"].append(item)
        if len(bucket["highlights"]) > 300:
            bucket["highlights"] = bucket["highlights"][-300:]
        self._save_memories()
        return item

    def list_highlights(self, umo: str, limit: int = 20) -> list[dict[str, Any]]:
        bucket = self._ensure_memory_bucket(umo)
        return list(reversed(bucket["highlights"][-max(1, limit) :]))

    def delete_highlight(self, umo: str, item_id: str) -> bool:
        bucket = self._ensure_memory_bucket(umo)
        before = len(bucket["highlights"])
        bucket["highlights"] = [x for x in bucket["highlights"] if x.get("id") != item_id]
        if len(bucket["highlights"]) != before:
            self._save_memories()
            return True
        return False

    # ==================== 待办 / 决议 ====================

    def add_todo(
        self,
        umo: str,
        title: str,
        created_by: str,
        assignee: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        bucket = self._ensure_memory_bucket(umo)
        item = {
            "id": _short_id(),
            "title": title.strip()[:300],
            "assignee": assignee.strip()[:40],
            "status": "open",
            "created_by": str(created_by or "群友")[:40],
            "created_at": _now_iso(now),
            "done_at": "",
        }
        bucket["todos"].append(item)
        if len(bucket["todos"]) > 200:
            bucket["todos"] = bucket["todos"][-200:]
        self._save_memories()
        return item

    def list_todos(self, umo: str, include_done: bool = False) -> list[dict[str, Any]]:
        bucket = self._ensure_memory_bucket(umo)
        items = bucket["todos"]
        if not include_done:
            items = [x for x in items if x.get("status") != "done"]
        return list(items)

    def complete_todo(self, umo: str, todo_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        bucket = self._ensure_memory_bucket(umo)
        for item in bucket["todos"]:
            if item.get("id") == todo_id:
                item["status"] = "done"
                item["done_at"] = _now_iso(now)
                self._save_memories()
                return item
        return None

    def delete_todo(self, umo: str, todo_id: str) -> bool:
        bucket = self._ensure_memory_bucket(umo)
        before = len(bucket["todos"])
        bucket["todos"] = [x for x in bucket["todos"] if x.get("id") != todo_id]
        if len(bucket["todos"]) != before:
            self._save_memories()
            return True
        return False

    # ==================== 网页链接收录 ====================

    def has_link_today(self, umo: str, url: str, target_date: str | None = None) -> bool:
        bucket = self._ensure_memory_bucket(umo)
        day = target_date or _today_str()
        for item in bucket["links"]:
            if item.get("url") == url and str(item.get("created_at", "")).startswith(day):
                return True
        return False

    def add_link(
        self,
        umo: str,
        url: str,
        title: str,
        excerpt: str,
        summary: str,
        shared_by: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        bucket = self._ensure_memory_bucket(umo)
        item = {
            "id": _short_id(),
            "url": url.strip()[:1000],
            "title": (title or url).strip()[:200],
            "excerpt": (excerpt or "").strip()[:500],
            "summary": (summary or "").strip()[:500],
            "shared_by": str(shared_by or "群友")[:40],
            "created_at": _now_iso(now),
        }
        bucket["links"].append(item)
        if len(bucket["links"]) > 300:
            bucket["links"] = bucket["links"][-300:]
        self._save_memories()
        return item

    def list_links(
        self,
        umo: str,
        target_date: str | None = None,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        bucket = self._ensure_memory_bucket(umo)
        items = bucket["links"]
        if target_date:
            items = [x for x in items if str(x.get("created_at", "")).startswith(target_date)]
        return list(reversed(items[-max(1, limit) :]))

    def delete_link(self, umo: str, link_id: str) -> bool:
        bucket = self._ensure_memory_bucket(umo)
        before = len(bucket["links"])
        bucket["links"] = [x for x in bucket["links"] if x.get("id") != link_id]
        if len(bucket["links"]) != before:
            self._save_memories()
            return True
        return False

    # ==================== RSS 订阅 ====================

    def add_rss_feed(
        self,
        umo: str,
        name: str,
        url: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        bucket = self._ensure_memory_bucket(umo)
        for existing in bucket["rss_feeds"]:
            if existing.get("url") == url:
                existing["name"] = name.strip()[:60] or existing.get("name", url)
                self._save_memories()
                return existing
        item = {
            "id": _short_id(),
            "name": name.strip()[:60] or url[:40],
            "url": url.strip()[:1000],
            "added_at": _now_iso(now),
        }
        bucket["rss_feeds"].append(item)
        if len(bucket["rss_feeds"]) > 30:
            bucket["rss_feeds"] = bucket["rss_feeds"][-30:]
        self._save_memories()
        return item

    def list_rss_feeds(self, umo: str) -> list[dict[str, Any]]:
        return list(self._ensure_memory_bucket(umo)["rss_feeds"])

    def delete_rss_feed(self, umo: str, feed_id: str) -> bool:
        bucket = self._ensure_memory_bucket(umo)
        before = len(bucket["rss_feeds"])
        bucket["rss_feeds"] = [x for x in bucket["rss_feeds"] if x.get("id") != feed_id]
        if len(bucket["rss_feeds"]) != before:
            self._save_memories()
            return True
        return False

    # ==================== 定时日报订阅 ====================

    def set_subscription(self, umo: str, enabled: bool, send_card: bool | None = None) -> dict[str, Any]:
        entry = self._subs.get(umo)
        if not isinstance(entry, dict):
            entry = {"enabled": False, "last_push_date": "", "send_card": False}
            self._subs[umo] = entry
        entry["enabled"] = bool(enabled)
        if send_card is not None:
            entry["send_card"] = bool(send_card)
        self._save_subs()
        return dict(entry)

    def get_subscription(self, umo: str) -> dict[str, Any]:
        entry = self._subs.get(umo)
        if not isinstance(entry, dict):
            return {"enabled": False, "last_push_date": "", "send_card": False}
        return dict(entry)

    def list_subscribed_umos(self) -> list[str]:
        return [u for u, v in self._subs.items() if isinstance(v, dict) and v.get("enabled")]

    def get_last_push_date(self, umo: str) -> str:
        entry = self._subs.get(umo)
        if isinstance(entry, dict):
            return str(entry.get("last_push_date", ""))
        return ""

    def mark_pushed(self, umo: str, push_date: str) -> None:
        entry = self._subs.get(umo)
        if not isinstance(entry, dict):
            entry = {"enabled": False, "last_push_date": "", "send_card": False}
            self._subs[umo] = entry
        entry["last_push_date"] = push_date
        self._save_subs()

    # ==================== 全文检索 ====================

    def search(self, umo: str, keyword: str, limit: int = 12) -> dict[str, list[dict[str, Any]]]:
        kw = (keyword or "").strip().lower()
        if not kw:
            return {"digests": [], "highlights": [], "todos": [], "links": [], "messages": []}

        bucket = self._ensure_memory_bucket(umo)
        hits_digests = []
        for d in reversed(self._digests.get(umo, []) or []):
            blob = " ".join(
                [
                    str(d.get("summary", "")),
                    " ".join(d.get("highlights", []) or []),
                    " ".join(d.get("decisions", []) or []),
                    " ".join(d.get("todos", []) or []),
                ]
            ).lower()
            if kw in blob:
                hits_digests.append(d)
                if len(hits_digests) >= limit:
                    break

        hits_highlights = [
            h
            for h in reversed(bucket["highlights"])
            if kw in str(h.get("text", "")).lower()
            or any(kw in t.lower() for t in (h.get("tags") or []))
        ][:limit]

        hits_todos = [
            t
            for t in reversed(bucket["todos"])
            if kw in str(t.get("title", "")).lower() or kw in str(t.get("assignee", "")).lower()
        ][:limit]

        hits_links = [
            lk
            for lk in reversed(bucket["links"])
            if kw in str(lk.get("title", "")).lower()
            or kw in str(lk.get("summary", "")).lower()
            or kw in str(lk.get("url", "")).lower()
        ][:limit]

        hits_msgs = []
        for m in reversed(self.get_messages(umo)):
            if kw in str(m.get("text", "")).lower() or kw in str(m.get("nickname", "")).lower():
                hits_msgs.append(m)
                if len(hits_msgs) >= limit:
                    break

        return {
            "digests": hits_digests,
            "highlights": hits_highlights,
            "todos": hits_todos,
            "links": hits_links,
            "messages": hits_msgs,
        }

    # ==================== 导出 / 导入 / 清空 ====================

    def export_session_dict(self, umo: str) -> dict[str, Any]:
        bucket = self._ensure_memory_bucket(umo)
        return {
            "schema_version": 1,
            "exported_at": _now_iso(),
            "umo": umo,
            "digests": list(self._digests.get(umo, []) or []),
            "highlights": list(bucket["highlights"]),
            "todos": list(bucket["todos"]),
            "links": list(bucket["links"]),
            "rss_feeds": list(bucket["rss_feeds"]),
        }

    def export_session_json(self, umo: str) -> str:
        return json.dumps(self.export_session_dict(umo), ensure_ascii=False, indent=2)

    def export_session_markdown(self, umo: str) -> str:
        data = self.export_session_dict(umo)
        lines = [
            "# 会话记忆与摘要档案",
            "",
            f"- 导出时间：{data['exported_at']}",
            f"- 历史摘要：{len(data['digests'])} 份",
            f"- 精华记忆：{len(data['highlights'])} 条",
            f"- 待办事项：{len(data['todos'])} 条",
            f"- 收录链接：{len(data['links'])} 条",
            f"- RSS 订阅：{len(data['rss_feeds'])} 个",
            "",
        ]
        if data["highlights"]:
            lines.append("## 精华记忆")
            lines.append("")
            for h in data["highlights"]:
                tag_s = f" `{'` `'.join(h['tags'])}`" if h.get("tags") else ""
                lines.append(f"- [{h['id']}] {h['text']} — {h.get('added_by', '')} ({h.get('created_at', '')}){tag_s}")
            lines.append("")

        if data["todos"]:
            lines.append("## 待办与决议")
            lines.append("")
            for t in data["todos"]:
                mark = "x" if t.get("status") == "done" else " "
                who = f" (@{t['assignee']})" if t.get("assignee") else ""
                lines.append(f"- [{mark}] [{t['id']}] {t['title']}{who}")
            lines.append("")

        if data["links"]:
            lines.append("## 收录链接")
            lines.append("")
            for lk in data["links"]:
                lines.append(f"- [{lk['title']}]({lk['url']})（{lk.get('shared_by', '')}）")
                if lk.get("summary"):
                    lines.append(f"  - 摘要：{lk['summary']}")
            lines.append("")

        if data["rss_feeds"]:
            lines.append("## RSS 订阅")
            lines.append("")
            for f in data["rss_feeds"]:
                lines.append(f"- [{f['id']}] {f['name']}: {f['url']}")
            lines.append("")

        if data["digests"]:
            lines.append("## 历史摘要")
            lines.append("")
            for d in reversed(data["digests"][-15:]):
                mode = "AI 智能" if d.get("mode") == "llm" else "本地规则"
                lines.append(f"### {d.get('date', '')}（{mode}，{d.get('message_count', 0)} 条消息）")
                lines.append("")
                lines.append(str(d.get("summary", "")))
                lines.append("")
                for sec_title, key in (("核心要点", "highlights"), ("决议事项", "decisions"), ("提取待办", "todos")):
                    vals = d.get(key) or []
                    if vals:
                        lines.append(f"**{sec_title}**")
                        for v in vals:
                            lines.append(f"- {v}")
                        lines.append("")
        return "\n".join(lines).strip() + "\n"

    def write_export_file(self, umo: str, fmt: str = "md") -> str:
        os.makedirs(self.exports_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_key = "".join(c if c.isalnum() else "_" for c in umo)[-24:] or "session"
        ext = "json" if fmt == "json" else "md"
        path = os.path.join(self.exports_dir, f"digest_{safe_key}_{stamp}.{ext}")
        content = self.export_session_json(umo) if ext == "json" else self.export_session_markdown(umo)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return path

    def import_session_json(
        self,
        umo: str,
        raw_json: str,
        replace: bool = False,
    ) -> dict[str, int]:
        """校验并导入 JSON 档案，默认合并去重，replace=True 时覆盖当前会话记忆。"""
        payload = json.loads(raw_json)
        if not isinstance(payload, dict):
            raise ValueError("导入内容必须是 JSON 对象")

        bucket = self._ensure_memory_bucket(umo)
        if replace:
            bucket["highlights"] = []
            bucket["todos"] = []
            bucket["links"] = []
            bucket["rss_feeds"] = []
            self._digests[umo] = []

        counts = {"highlights": 0, "todos": 0, "links": 0, "rss_feeds": 0, "digests": 0}

        def _merge(dst: list[dict[str, Any]], src: Any, required_key: str) -> int:
            if not isinstance(src, list):
                return 0
            existing_ids = {x.get("id") for x in dst if isinstance(x, dict) and x.get("id")}
            added = 0
            for item in src:
                if not isinstance(item, dict) or not item.get(required_key):
                    continue
                rec = dict(item)
                rid = str(rec.get("id") or _short_id())
                if rid in existing_ids:
                    continue
                rec["id"] = rid
                dst.append(rec)
                existing_ids.add(rid)
                added += 1
            return added

        counts["highlights"] = _merge(bucket["highlights"], payload.get("highlights"), "text")
        counts["todos"] = _merge(bucket["todos"], payload.get("todos"), "title")
        counts["links"] = _merge(bucket["links"], payload.get("links"), "url")
        counts["rss_feeds"] = _merge(bucket["rss_feeds"], payload.get("rss_feeds"), "url")
        digests_dst = self._digests.setdefault(umo, [])
        if not isinstance(digests_dst, list):
            digests_dst = []
            self._digests[umo] = digests_dst
        counts["digests"] = _merge(digests_dst, payload.get("digests"), "summary")

        self._save_memories()
        self._save_digests()
        return counts

    def clear_session(self, umo: str) -> None:
        self._messages.pop(umo, None)
        self._digests.pop(umo, None)
        self._memories.pop(umo, None)
        self._subs.pop(umo, None)
        self.save_all()

    def session_stats(self, umo: str) -> dict[str, Any]:
        bucket = self._ensure_memory_bucket(umo)
        today = _today_str()
        sub = self.get_subscription(umo)
        return {
            "today_messages": self.count_messages(umo, target_date=today),
            "total_messages": self.count_messages(umo),
            "digests": len(self._digests.get(umo, []) or []),
            "highlights": len(bucket["highlights"]),
            "open_todos": len([t for t in bucket["todos"] if t.get("status") != "done"]),
            "total_todos": len(bucket["todos"]),
            "links": len(bucket["links"]),
            "rss_feeds": len(bucket["rss_feeds"]),
            "subscribed": bool(sub.get("enabled")),
            "last_push_date": str(sub.get("last_push_date", "")),
        }
