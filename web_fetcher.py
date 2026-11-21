# -*- coding: utf-8 -*-
"""安全网页抓取、HTML 正文抽取、RSS/Atom 解析与群聊 URL 识别（内置代理与 SSRF 防护）。"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

URL_REGEX = re.compile(r"https?://[^\s<>\"'\]\)}，。；！？、]+", re.IGNORECASE)

SKIP_MEDIA_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".svg",
    ".mp4", ".webm", ".avi", ".mov", ".mkv", ".mp3", ".wav", ".ogg", ".amr",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".dmg", ".apk", ".pdf",
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36 AstrBotChatDigest/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class UnsafeURLError(ValueError):
    """URL 协议非法或命中 SSRF 私有/回环地址防护时抛出。"""


@dataclass
class PageContent:
    url: str
    title: str
    description: str
    text: str
    content_type: str = ""


@dataclass
class RssItem:
    title: str
    link: str
    summary: str
    published: str = ""


def extract_urls(text: str, max_count: int = 3) -> list[str]:
    """从聊天文本提取去重后的可抓取网页 URL（过滤明显二进制/图片链接）。"""
    if not text:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for raw in URL_REGEX.findall(text):
        cleaned = raw.rstrip(".,;:!?)\"'》】")
        parsed = urlparse(cleaned)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            continue
        path_lower = (parsed.path or "").lower()
        if any(path_lower.endswith(ext) for ext in SKIP_MEDIA_EXTENSIONS):
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
            if len(urls) >= max_count:
                break
    return urls


def _parse_ip(ip_str: str):
    try:
        return ipaddress.ip_address(ip_str)
    except ValueError:
        return None


def _is_blocked_ip_obj(ip: Any) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and _is_blocked_ip_obj(mapped):
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url(
    url: str,
    allow_private: bool = False,
    resolver: Any = None,
) -> str:
    """校验 URL 合法性并拦截回环/内网地址（防 SSRF）。"""
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https"):
        raise UnsafeURLError(f"仅支持 http/https 链接：{parsed.scheme or '空协议'}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise UnsafeURLError("链接缺少有效主机名")

    if not allow_private:
        if host in ("localhost", "localhost.localdomain") or host.endswith(".local") or host.endswith(".internal"):
            raise UnsafeURLError(f"已拦截本地/内网域名：{host}")
        # 若 host 本身就是 IP 字面量，直接判定（注意不要把 UnsafeURLError 包进 except ValueError）
        host_ip = _parse_ip(host)
        if host_ip is not None:
            if _is_blocked_ip_obj(host_ip):
                raise UnsafeURLError(f"已拦截私网/回环 IP：{host}")
            return cleaned
        # 域名解析校验，防止 DNS 指向内网地址
        resolve_fn = resolver or socket.getaddrinfo
        try:
            infos = resolve_fn(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeURLError(f"域名解析失败（{host}）：{exc}") from exc
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            ip_s = str(sockaddr[0]).split("%")[0]
            resolved_ip = _parse_ip(ip_s)
            if resolved_ip is not None and _is_blocked_ip_obj(resolved_ip):
                raise UnsafeURLError(f"域名 {host} 解析到受保护地址 {ip_s}，已拦截")
    return cleaned


class _HtmlExtractor(HTMLParser):
    """基于标准库的轻量 HTML 标题/描述/正文提取器。"""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "header"}
    _BLOCK_TAGS = {"p", "div", "article", "section", "li", "h1", "h2", "h3", "h4", "br", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta_desc: str = ""
        self.og_title: str = ""
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if t == "title" and self._skip_depth == 0:
            self._in_title = True
        elif t == "meta":
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            name = attr_map.get("name", "").lower()
            prop = attr_map.get("property", "").lower()
            content = attr_map.get("content", "").strip()
            if content:
                if name == "description" or prop == "og:description":
                    if not self.meta_desc:
                        self.meta_desc = content
                elif prop == "og:title" and not self.og_title:
                    self.og_title = content

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if t == "title":
            self._in_title = False
        elif t in self._BLOCK_TAGS and self._skip_depth == 0:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


def parse_html_content(url: str, raw_html: str) -> PageContent:
    """从 HTML 字符串解析标题、meta description 与正文摘要片段。"""
    parser = _HtmlExtractor()
    parser.feed(raw_html or "")
    parser.close()

    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip() or parser.og_title
    desc = re.sub(r"\s+", " ", parser.meta_desc).strip()
    raw_body = "".join(parser.text_parts)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw_body.splitlines()]
    non_empty = [ln for ln in lines if len(ln) >= 2]
    body_text = "\n".join(non_empty)[:4000]
    return PageContent(
        url=url,
        title=title[:200] or url,
        description=desc[:400],
        text=body_text,
        content_type="text/html",
    )


def _strip_tags(raw: str) -> str:
    if not raw:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def parse_rss_xml(xml_text: str, max_items: int = 5) -> tuple[str, list[RssItem]]:
    """解析 RSS 2.0 与 Atom 1.0 XML，返回 (频道标题, 条目列表)。"""
    cleaned = (xml_text or "").strip()
    if not cleaned:
        return "", []
    root = ET.fromstring(cleaned)
    root_name = _local_name(root.tag)
    feed_title = ""
    items: list[RssItem] = []

    if root_name == "feed":
        # Atom 1.0
        for child in root:
            cname = _local_name(child.tag)
            if cname == "title" and child.text:
                feed_title = child.text.strip()
            elif cname == "entry":
                title, link, summary, pub = "", "", "", ""
                for sub in child:
                    sname = _local_name(sub.tag)
                    if sname == "title" and sub.text:
                        title = _strip_tags(sub.text)
                    elif sname == "link":
                        href = sub.attrib.get("href", "")
                        rel = sub.attrib.get("rel", "alternate")
                        if href and (not link or rel == "alternate"):
                            link = href.strip()
                    elif sname in ("summary", "content") and sub.text and not summary:
                        summary = _strip_tags(sub.text)
                    elif sname in ("published", "updated") and sub.text and not pub:
                        pub = sub.text.strip()
                if title or link:
                    items.append(RssItem(title=title or link, link=link, summary=summary[:280], published=pub[:32]))
                    if len(items) >= max_items:
                        break
    else:
        # RSS 2.0 / RDF
        channel = None
        for child in root:
            if _local_name(child.tag) == "channel":
                channel = child
                break
        container = channel if channel is not None else root
        for child in container:
            cname = _local_name(child.tag)
            if cname == "title" and child.text and not feed_title:
                feed_title = child.text.strip()
            elif cname == "item":
                title, link, summary, pub = "", "", "", ""
                for sub in child:
                    sname = _local_name(sub.tag)
                    if sname == "title" and sub.text:
                        title = _strip_tags(sub.text)
                    elif sname == "link" and sub.text:
                        link = sub.text.strip()
                    elif sname in ("description", "encoded") and sub.text and not summary:
                        summary = _strip_tags(sub.text)
                    elif sname in ("pubdate", "date") and sub.text and not pub:
                        pub = sub.text.strip()
                if title or link:
                    items.append(RssItem(title=title or link, link=link, summary=summary[:280], published=pub[:32]))
                    if len(items) >= max_items:
                        break
    return feed_title[:120], items


class WebFetcher:
    """支持代理、字节上限、重定向逐跳 SSRF 校验的网页与 RSS 抓取器。"""

    def __init__(
        self,
        proxy: str = "",
        timeout: int = 15,
        max_bytes: int = 524288,
        allow_private: bool = False,
        session: requests.Session | None = None,
        resolver: Any = None,
    ) -> None:
        self.proxy = (proxy or "").strip()
        self.timeout = max(3, int(timeout))
        self.max_bytes = max(4096, int(max_bytes))
        self.allow_private = bool(allow_private)
        self.resolver = resolver
        self.session = session or requests.Session()
        self.session.trust_env = True
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

    def fetch_raw(self, url: str, max_redirects: int = 5) -> tuple[str, str, str]:
        """安全抓取 URL，返回 (最终 URL, content_type, 解码后的文本)。"""
        current_url = validate_url(url, allow_private=self.allow_private, resolver=self.resolver)
        for _ in range(max_redirects + 1):
            resp = self.session.get(
                current_url,
                headers=DEFAULT_HEADERS,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "").strip()
                    if not location:
                        raise RuntimeError(f"HTTP {resp.status_code} 缺少重定向目标")
                    next_url = urljoin(current_url, location)
                    current_url = validate_url(next_url, allow_private=self.allow_private, resolver=self.resolver)
                    continue

                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP 状态码异常：{resp.status_code}")

                ctype = (resp.headers.get("Content-Type") or "").lower()
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=16384):
                    if not chunk:
                        continue
                    remain = self.max_bytes - total
                    if remain <= 0:
                        break
                    chunks.append(chunk[:remain])
                    total += min(len(chunk), remain)
                raw_bytes = b"".join(chunks)
                enc = resp.encoding
                if not enc or enc.lower() == "iso-8859-1":
                    enc = getattr(resp, "apparent_encoding", None) or "utf-8"
                try:
                    text = raw_bytes.decode(enc, errors="replace")
                except LookupError:
                    text = raw_bytes.decode("utf-8", errors="replace")
                return current_url, ctype, text
            finally:
                resp.close()
        raise RuntimeError("重定向次数过多")

    def fetch_page(self, url: str) -> PageContent:
        final_url, ctype, text = self.fetch_raw(url)
        if "html" in ctype or "<html" in text[:400].lower() or "<title" in text[:1000].lower():
            page = parse_html_content(final_url, text)
            page.content_type = ctype
            return page
        clean = re.sub(r"\s+", " ", text).strip()
        return PageContent(
            url=final_url,
            title=final_url,
            description=clean[:300],
            text=clean[:4000],
            content_type=ctype,
        )

    def fetch_rss(self, url: str, max_items: int = 5) -> tuple[str, list[RssItem]]:
        _, _, xml_text = self.fetch_raw(url)
        return parse_rss_xml(xml_text, max_items=max_items)
