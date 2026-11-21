# -*- coding: utf-8 -*-
"""astrbot_plugin_chat_digest 网页/RSS 抓取模块单元测试（纯离线 mock，含 SSRF 与重定向校验）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrbot_plugin_chat_digest.web_fetcher import (  # noqa: E402
    UnsafeURLError,
    WebFetcher,
    extract_urls,
    parse_html_content,
    parse_rss_xml,
    validate_url,
)


class _FakeResponse:
    def __init__(
        self,
        body: bytes = b"",
        status_code: int = 200,
        headers: dict | None = None,
        encoding: str | None = "utf-8",
    ) -> None:
        self._body = body
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = encoding
        self.apparent_encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size: int = 16384):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self.responses = responses
        self.proxies = {}
        self.trust_env = False
        self.calls: list[str] = []

    def get(self, url: str, **kwargs):
        self.calls.append(url)
        return self.responses[url]

    def close(self) -> None:
        pass


def _public_resolver(host, port, type=None):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def _private_resolver(host, port, type=None):
    return [(2, 1, 6, "", ("10.0.0.8", port))]


class TestWebFetcher(unittest.TestCase):
    def test_extract_urls_dedupe_and_skip_media(self) -> None:
        text = (
            "看看这篇 https://example.com/post/1，还有重复的 https://example.com/post/1 "
            "和图片 https://cdn.example.com/pic.png 以及视频 https://cdn.example.com/a.mp4 "
            "另一篇 https://docs.python.org/3/。"
        )
        urls = extract_urls(text)
        self.assertEqual(urls, ["https://example.com/post/1", "https://docs.python.org/3/"])

    def test_ssrf_blocks_private_and_non_http(self) -> None:
        bad_urls = [
            "file:///etc/passwd",
            "ftp://example.com/a",
            "http://localhost:8080/admin",
            "http://127.0.0.1/secret",
            "http://10.1.2.3/internal",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]:6185/",
            "http://[::ffff:127.0.0.1]/",
            "http://service.internal/api",
        ]
        for u in bad_urls:
            with self.assertRaises(UnsafeURLError, msg=f"应拦截 {u}"):
                validate_url(u, allow_private=False, resolver=_public_resolver)

        # DNS 解析到内网 IP 也必须拦截
        with self.assertRaises(UnsafeURLError):
            validate_url("https://rebind.example.com/", allow_private=False, resolver=_private_resolver)

        # 公网正常放行
        self.assertEqual(
            validate_url("https://example.com/ok", allow_private=False, resolver=_public_resolver),
            "https://example.com/ok",
        )
        # 显式允许私网时放行 http://127.0.0.1
        self.assertEqual(validate_url("http://127.0.0.1:8000", allow_private=True), "http://127.0.0.1:8000")

    def test_redirect_to_private_ip_blocked(self) -> None:
        sess = _FakeSession(
            {
                "https://example.com/go": _FakeResponse(
                    status_code=302,
                    headers={"Location": "http://127.0.0.1:6185/secret"},
                ),
            }
        )
        fetcher = WebFetcher(session=sess, resolver=_public_resolver)
        with self.assertRaises(UnsafeURLError):
            fetcher.fetch_page("https://example.com/go")

    def test_parse_html_content(self) -> None:
        html_doc = """
        <html><head>
            <title>  AstrBot 插件指南  </title>
            <meta name="description" content="介绍如何编写高质量 AstrBot 插件">
            <style>.x{color:red}</style>
            <script>alert('xss')</script>
        </head><body>
            <nav>导航栏应被忽略</nav>
            <article><h1>插件开发</h1><p>使用 async def initialize 启动后台任务。</p></article>
            <footer>页脚应忽略</footer>
        </body></html>
        """
        page = parse_html_content("https://example.com/guide", html_doc)
        self.assertEqual(page.title, "AstrBot 插件指南")
        self.assertEqual(page.description, "介绍如何编写高质量 AstrBot 插件")
        self.assertIn("使用 async def initialize 启动后台任务", page.text)
        self.assertNotIn("alert", page.text)
        self.assertNotIn("导航栏应被忽略", page.text)

    def test_fetch_page_proxy_and_byte_cap(self) -> None:
        big_html = b"<html><head><title>T</title></head><body><p>" + (b"a" * 20000) + b"</p></body></html>"
        sess = _FakeSession({"https://example.com/big": _FakeResponse(body=big_html)})
        fetcher = WebFetcher(
            proxy="http://127.0.0.1:7890",
            max_bytes=4096,
            session=sess,
            resolver=_public_resolver,
        )
        self.assertEqual(sess.proxies["https"], "http://127.0.0.1:7890")
        page = fetcher.fetch_page("https://example.com/big")
        self.assertEqual(page.title, "T")
        self.assertLessEqual(len(page.text), 4096)

    def test_parse_rss2_and_atom(self) -> None:
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
            <title>技术早报</title>
            <item>
                <title>Python 3.13 发布</title>
                <link>https://example.com/py313</link>
                <description><![CDATA[<p>带来<b>自由线程</b>实验特性。</p>]]></description>
                <pubDate>Sat, 21 Nov 2026 08:00:00 GMT</pubDate>
            </item>
        </channel></rss>
        """
        title, items = parse_rss_xml(rss_xml, max_items=5)
        self.assertEqual(title, "技术早报")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Python 3.13 发布")
        self.assertEqual(items[0].link, "https://example.com/py313")
        self.assertIn("自由线程", items[0].summary)
        self.assertNotIn("<b>", items[0].summary)

        atom_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <title>Atom 博客</title>
            <entry>
                <title>AstrBot 架构笔记</title>
                <link rel="alternate" href="https://example.com/astrbot" />
                <summary>深入剖析事件管线</summary>
                <updated>2026-11-21T10:00:00Z</updated>
            </entry>
        </feed>
        """
        atitle, aitems = parse_rss_xml(atom_xml, max_items=5)
        self.assertEqual(atitle, "Atom 博客")
        self.assertEqual(len(aitems), 1)
        self.assertEqual(aitems[0].link, "https://example.com/astrbot")
        self.assertEqual(aitems[0].summary, "深入剖析事件管线")


if __name__ == "__main__":
    unittest.main(verbosity=1)
