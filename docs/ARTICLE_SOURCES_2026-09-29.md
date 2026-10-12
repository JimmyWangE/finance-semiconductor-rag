# 行业文章来源评估与接入（2026-09-29）

国内中文来源的后续接入及评估见 [国内半导体文章来源评估](ARTICLE_SOURCES_DOMESTIC_2026-09-29.md)。

## 决策与边界

选用 Semiconductor Digest、Semiconductor Today 的官方 RSS，复用现有手动“更新文章”入口；保留微信公众号渠道，不依赖它完成新媒体的同步。两个 RSS 不要求账号或 API Key。本功能是带出处的文章索引，不是取得全文转载、RAG 训练或再分发授权：保存标题、发布时间、来源和链接；Digest 另外保存 RSS 提供的至多 180 字摘要，Today 只索引未改写的标题及链接。`content_html`（正文）置空，读者在原站阅读。需要存储全文时，应先取得相应授权并单独实现许可控制。

## 来源评估

更新频率是 2026-09-29 的观察与编辑栏目定位，不是发布保证；“允许 RSS 访问”不等于允许批量转载。

| 来源 | 官方入口/订阅 | robots 与版权约束 | 频率/覆盖/稳定性 | 决定 |
| --- | --- | --- | --- | --- |
| [Semiconductor Digest](https://www.semiconductor-digest.com/) | [官方 RSS](https://www.semiconductor-digest.com/feed/)，无公开通用 API；网页可订阅 | [robots](https://www.semiconductor-digest.com/robots.txt) 对 `*` 放行；未找到可确认全文转载授权，保守仅索引 RSS 元数据/短摘要 | 近期更新；制造、设备、材料、设计、封装及市场；在本机返回 200、RSS 约 644 KB，稳定性中高 | **接入** |
| [Semiconductor Today](https://www.semiconductor-today.com/rss.shtml) | [官方 News RSS](https://www.semiconductor-today.com/rss/news.xml)与 Features RSS；无需认证，无公开通用 API | [robots](https://www.semiconductor-today.com/robots.txt)对 `*` 未限制；[官方 RSS 规则](https://www.semiconductor-today.com/rss.shtml)允许使用订阅流与非全文展示，须注明来源并链接原站、不得改写展示文本；站方保留终止使用的权利 | 高频；光电、化合物半导体、材料、设备和市场；本机 200、约 2.7 MB/3600 多条，稳定性中高 | **接入** |
| [EE Times](https://www.eetimes.com/) | [官方 RSS](https://www.eetimes.com/feed/)，另有邮件订阅；无公开通用文章 API | [robots](https://www.eetimes.com/robots.txt)未禁 `/feed/`；但[出版集团条款](https://aspencore.com/terms-of-use/)要求自动访问/采集取得事先书面许可，不能仅凭 RSS 可读就默认合规 | 高频；设计、存储、制造、市场；本机 RSS 200，但授权不确定 | **暂不接入**，申请书面许可 |
| [Semiconductor Engineering](https://semiengineering.com/) | [RSS](https://semiengineering.com/feed/)及电子邮件简报 | [robots](https://semiengineering.com/robots.txt)对一般路径未禁止；[Terms of Service](https://semiengineering.com/terms-of-service/)明确限制复制内容/数据和转载 | 高频；设计、制程、设备、材料；本机 RSS 获取超时一次，稳定性待核验 | 暂不接入，先取得使用许可 |
| [SIA 美国半导体行业协会](https://www.semiconductors.org/news-events/latest-news/) | 官网 [SIA News 邮件订阅](https://www.semiconductors.org/news-events/latest-news/)；`/feed/` 实测跳回首页，未确认公开 RSS/API | [robots](https://www.semiconductors.org/robots.txt)没有禁止新闻页；[条款入口](https://www.semiconductors.org/terms-of-use/)与站点版权应核对后另议 | 月度/不定期；行业销售、政策、市场；站点稳定但缺少已验证机器订阅 | 暂不自动抓网页 |
| [TechSpot](https://www.techspot.com/) | [官方 RSS](https://www.techspot.com/backend.xml)，无已验证官方文章 API | [robots](https://www.techspot.com/robots.txt)未禁 RSS；RSS 明示“non-commercial use only”，企业场景需另获许可 | 高频；芯片/CPU/GPU、消费电子混合，相关性较低；本机 200 | 暂不接入 |

以上 robots/条款为本日核查快照，运营时需复核变动；版权判断按保守口径执行。

## 已落地接入方式

`ArticleStore` 在原 `sources` 表幂等插入两条新来源，保留原两条微信来源及旧文章。管理员点击“更新文章”后，独立请求媒体的固定 HTTPS 官方 RSS；微信公众号仍走原本机 WeWe RSS 服务。官方来源 URL 与条目域名分别绑定，拒绝自定义外部 feed、跨站条目及 HTTP 链接，避免 SSRF/混入假来源。Semiconductor Today 的条目同时出现 `www` 与无 `www` 官网地址，两者统一为已验证可达的 `www` HTTPS 原文链接。Today 只展示原有标题，不改写摘要或展示全文，并明确标出来源与原文链接。

请求无鉴权，单来源成功同步后最短 15 分钟再请求一次；优先使用 ETag/Last-Modified 条件 GET，304 不产生写入；429、500、502、503、504 最多重试两次，指数退避（0.5/1 秒；`Retry-After` 小于 4 秒时等待指定时间，超过 4 秒时直接失败并须延后人工重试），其他失败只更新错误状态，旧文章不删除。一次最多处理前 100 条，响应上限 2 MB（Semiconductor Today 4 MB），不抓文章原文页。服务器不提供对方保证的请求配额，15 分钟间隔是本项目主动限制。

沿用 `articles` 表 `title`、`published_at`、`content_html`（正文，此类未授权源为空）、`source_id -> sources.name`、`url`，额外使用现有 `description` 保存 Digest 有限 RSS 摘要（Today 留空）。移除跟踪参数/fragment 的原文 URL 上有 UNIQUE 约束；同日同标题也抑制跨源重复；SQLite 迁移只为 `sources` 增加 `etag` 和 `last_modified`，不改既有 article 格式。每次读取最新 RSS 列表并做幂等插入，不清理不再出现在 RSS 的历史记录；RSS 只能覆盖其窗口内的新文，停机超过窗口时须另行合规补录。

## 操作与验证

- 浏览 `/articles`，点击“更新文章”，切换 Semiconductor Digest、Semiconductor Today 观察来源、发布时间、摘要和原文链接。
- 连续同步检查新增数量、后续跳过/节流及按链接去重；微信旧条目仍在。
- 本机真实同步结果：Semiconductor Digest 新增 100、Semiconductor Today 新增 100（首次 75，校正来源官网 `www`/无 `www` 地址后补齐 25），最终失败 0；原 28 条微信文章未变化。二次调用在 15 分钟窗口内无新请求。原公众号订阅服务先前返回 500 的状态仍保留，不影响两家媒体。EE Times 曾在许可复核前暂时导入 10 条，发现集团条款后已停止采集并清理这批条目。
- `PYTHONPATH=src .venv-rag/bin/python -m pytest tests/test_articles.py -q --disable-warnings`
- 若某站收紧条款/robots，清空对应来源 `feed_url` 即停止采集，不删除已存记录；涉及权利人的删除请求按文章 URL 定向处理。

## 待办

- 若企业确需全文本地阅读/搜索，先与媒体确认内部商业使用及存储授权，再将正文抓取能力单独开关。
- 若需要自动更新，复用现有任务队列按每来源低频调度，先增加长期错误与增量延迟监控；本次继续沿用手动更新，避免未经评估的后台高频访问。
