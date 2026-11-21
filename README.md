# AstrBot 群聊记忆与摘要助手（astrbot_plugin_chat_digest）

按群聊/私聊会话完全隔离的智能记忆与摘要助手：支持 LLM 摘要（自动降级本地规则）、精华收藏、决议/待办追踪、群聊链接静默抓取收录与网页摘要、RSS 订阅汇总、每日定时日报、Markdown/JSON 导入导出及 Pillow 摘要长图。

- 作者：云晓
- 版本：1.0.0
- 许可证：MIT（详见 `LICENSE`）

## 核心特性

1. **双引擎智能摘要（LLM + 本地规则自动降级）**
   - 优先调用 AstrBot 配置的 LLM Provider（支持指定 `provider_id` 或沿用会话默认 Provider），内置随机边界符防群聊提示注入。
   - 当未配置 LLM、超时或调用异常时，自动平滑降级为本地规则引擎（提取高频热点词、疑似决议、待办线索与活跃成员统计），永不报错瘫痪。
2. **会话隔离持久化（原子 JSON 写入）**
   - 群聊与私聊按 `unified_msg_origin (UMO)` 完全隔离存储，原子写防损坏，支持按 `retention_days` 自动滚动清理旧记录。
3. **精华记忆、待办追踪与跨源全文检索**
   - 手动收藏群聊金句/知识点（支持 `#标签`）、跟踪团队决议与待办（支持 `@负责人` 与完成打勾）、一条 `/摘要 搜索 <关键词>` 横跨历史摘要、精华、待办、链接与近期聊天记录检索。
4. **网页链接静默收录 & RSS 订阅汇总（支持代理 + SSRF 防护）**
   - 开启 `auto_capture_links` 后，群友分享网页链接时后台自动静默抓取标题与要点写入当日记忆库，**不主动刷屏回复**，日报与检索可直接查看；也支持 `/摘要 链接 <URL>` 手动即时摘要，以及 `/摘要 rss` 订阅 RSS 2.0 / Atom 1.0 源。
   - 内置代理支持（`proxy` + 环境变量）、响应体大小上限（`max_fetch_bytes`）与重定向逐跳 SSRF 防护（默认拦截 `localhost`、回环/私网 IP）。
5. **每日定时日报 & 导入导出 / 摘要长图**
   - 支持按群动态 `/摘要 订阅 on` 或固定 `daily_push_umos` 每日定时推送日报（跳过离线平台、同日防重推）。
   - 支持导出 Markdown / JSON 档案文件、JSON 合并/覆盖导入恢复，以及可选 Pillow 摘要长图（未安装 Pillow 自动降级纯文本）。

## 指令一览

统一入口为 `/摘要`（兼容全角斜杠与别名 `/digest`、`/纪要`），不污染其他顶层指令：

| 指令 | 说明 | 权限 |
|------|------|------|
| `/摘要 [今日\|条数\|YYYY-MM-DD] [--card]` | 生成今日、最近 N 条或指定日期的群聊摘要（加 `--card` 附带长图） | 所有人 |
| `/摘要 精华 [添加 <内容> [#标签] \| 列表 \| 删除 <ID>]` | 收藏、查看或删除本会话精华记忆 | 删除限管理员 |
| `/摘要 待办 [添加 <内容> [@负责人] \| 列表 \| 全部 \| 完成 <ID> \| 删除 <ID>]` | 管理决议与待办事项 | 删除限管理员 |
| `/摘要 搜索 <关键词>` | 跨摘要、精华、待办、链接与聊天记录检索 | 所有人 |
| `/摘要 回顾 [YYYY-MM-DD \| N]` | 查看历史生成的摘要档案 | 所有人 |
| `/摘要 链接 [<URL> \| 列表 \| 删除 <ID>]` | 手动抓取摘要网页，或查看自动静默收录的链接 | 删除限管理员 |
| `/摘要 rss [添加 <名称> <URL> \| 列表 \| 拉取 \| 删除 <ID>]` | 订阅并汇总 RSS / Atom 源最新条目 | 增删限管理员 |
| `/摘要 订阅 [on\|off\|card\|状态]` | 开关当前会话每日定时日报推送 | 开关限管理员 |
| `/摘要 导出 [md\|json\|card]` | 导出本会话 Markdown/JSON 档案或生成摘要长图 | 管理员 |
| `/摘要 导入 [--replace] <JSON>` | 从导出的 JSON 合并或覆盖恢复会话记忆 | 管理员 |
| `/摘要 状态` | 查看当前会话缓存量、记忆统计、订阅与代理配置 | 所有人 |
| `/摘要 清空 确认` | 清空当前会话全部数据（需二次确认） | 管理员 |
| `/摘要 帮助` | 查看指令帮助菜单 | 所有人 |

## 配置项

完整定义见 `_conf_schema.json`，可在 AstrBot WebUI 插件配置页直接调整：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 是否启用被动消息收集与自动收录 |
| `provider_id` | `""` | 指定 LLM Provider ID（留空使用当前会话默认） |
| `enable_llm_summary` | `true` | 优先使用 LLM 总结，失败自动降级本地规则 |
| `llm_timeout_seconds` | `45` | 单次 LLM 总结超时时间（秒） |
| `default_message_limit` | `120` | 未指定条数时默认纳入分析的消息条数 |
| `max_messages_per_day` | `500` | 单会话单日保留的最大消息条数 |
| `retention_days` | `30` | 聊天记录与历史摘要保留天数 |
| `auto_capture_links` | `true` | 自动静默抓取群内分享的网页链接收录到记忆库 |
| `proxy` | `""` | 网页与 RSS 抓取代理地址（如 `http://127.0.0.1:7890`） |
| `http_timeout_seconds` | `15` | 网页与 RSS 抓取超时（秒） |
| `max_fetch_bytes` | `524288` | 单次抓取最大字节数（默认 512KB） |
| `allow_private_urls` | `false` | 是否允许抓取内网/回环地址（默认关，防 SSRF） |
| `daily_push_enabled` | `false` | 是否启用每日定时日报后台巡检 |
| `daily_push_time` | `"22:00"` | 每日定时日报触发时间（HH:MM） |
| `daily_push_umos` | `""` | 固定推送目标 UMO 列表（英文逗号分隔） |
| `daily_push_min_messages` | `5` | 触发定时日报的当日最少消息数 |
| `daily_push_card` | `false` | 定时日报是否默认附带 Pillow 长图 |
| `enable_card_image` | `true` | 是否允许生成 Pillow 摘要长图 |
| `admin_umos` / `admin_user_ids` | `""` | 额外管理员 UMO / 用户 ID 白名单（自动认可平台管理员） |
| `rss_max_items` | `5` | 单个 RSS 源每次拉取展示的最大条目数 |

## 开发与测试

```bash
python test_storage.py
python test_web_fetcher.py
python test_summarizer_and_card.py
python test_chat_digest.py
```

可选依赖：`Pillow`（用于生成 PNG 摘要长图；未安装时自动平滑降级为纯文本）。

## 更新记录

### v1.0.0
- 首发：群聊/私聊会话隔离记忆存储、LLM + 本地规则双引擎摘要、精华/待办/全文检索/历史回顾、网页链接自动静默收录与手动摘要（含 SSRF 与重定向逐跳校验）、RSS 2.0 / Atom 1.0 订阅汇总、每日定时日报、Markdown/JSON 导入导出、Pillow 摘要长图与 GitHub Actions CI。
