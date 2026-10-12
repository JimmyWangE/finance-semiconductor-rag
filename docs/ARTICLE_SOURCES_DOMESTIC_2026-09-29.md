# 国内半导体文章来源评估与接入（2026-09-29）

## 结果

保留现有微信公众号和两家海外媒体，另接入三个**中文站点的官方 RSS**：IT之家、驱动中国、新时空科技。它们均为混合频道，只保留标题明确涉及半导体/晶圆/芯片/EDA 等领域的文章，站内展示标题、时间、来源和原文链接；不抓取文章网页，不缓存正文或摘要，不将这些索引纳入研报 RAG。默认仍需管理员点击“更新文章”，不新增后台定时抓取。

## 候选评估

| 来源 | 官方订阅与运行验证 | robots、版权与处理 | 更新/覆盖与结论 |
| --- | --- | --- | --- |
| [IT之家](https://www.ithome.com/0/000/037.htm) | [官方 RSS](https://www.ithome.com/rss/)；本机返回 200、RSS 60 条，有当日原文链接；无需鉴权 | [robots](https://www.ithome.com/robots.txt)未限制 `/rss/`；仅以 RSS 标题及原站链接做站内索引，不转载文章正文 | 高频综合科技，涵盖芯片、存储与制造，但标题过滤后密度低；**接入** |
| [驱动中国](https://news.qudong.com/about) | 官网明确列出[官方 RSS](https://news.qudong.com/rss.xml)供第三方平台及阅读器订阅；本机 200、15 条且原文可达；无需鉴权 | [robots](https://news.qudong.com/robots.txt)未限制 RSS；官网要求转载保留来源及原文链接，本站仅呈现标题/来源/原文链接、不复制正文 | 高频综合科技，芯片企业、投资与供应链消息占小部分；**接入** |
| [新时空科技](https://www.newtimespace.com/zh-cn/rss/) | 官网给出[科技频道 RSS](https://www.newtimespace.com/feed/rss_template.xml?id=100003&site=rss&lang=zh-cn)；本机 200、50 条，含有效日期和原文；无需鉴权 | [robots](https://www.newtimespace.com/robots.txt)允许 RSS 路径；官方 RSS 页明确免费使用时注明“新时空”来源；本站同时展示原文链接，无全文复制 | 财经/科技混合，芯片产业与资本市场相关性较高；**接入** |
| [TrendForce 集邦咨询](https://www.trendforce.cn/presscenter/rss.html) | 官网有半导体 RSS，本机返回的最近条目却指向 `test.new.trendforce.com`，且时间为 1970-01-01 | [使用条款](https://www.trendforce.cn/about/terms)对复制、散布等要求事先书面许可；robots 未禁止 RSS 不等于获得授权 | 行业研究质量高，但源数据异常且授权受限；**暂不接入** |
| [芯师爷](https://www.gsi24.com/index/index/channelRssList.html) | 有按制造、设备、设计划分的 RSS；“半导体芯片”频道返回 200，但最后条目停在 2025-06-27 | [robots](https://www.gsi24.com/robots.txt)放行；未确认转载授权，不抓全文 | 垂直覆盖优秀，但当前流已超过一年未更新；**暂不接入** |
| [IC技术圈](https://iccircle.com/column?id=22) | 官网发布 [RSS](https://iccircle.com/feed)，但实测订阅条目原文指向 `mp.weixin.qq.com`，不解决微信依赖 | [robots](https://iccircle.com/robots.txt)仅限制部分爬虫和 `/editor`；仍应尊重原作者版权 | 偏芯片设计与验证；**暂不接入** |
| [36氪](https://www.36kr.com/rss-center) / [工信部](https://wap.miit.gov.cn/RRSdy/index.html) | 前者官方 RSS 在本机返回 HTML 而非 XML；后者 RSS 页面从本机返回 403 | 未尝试绕过网站限制或抓取替代页面 | 无可稳定运行的直连源；**暂不接入** |

评估是本日快照；robots 和使用条款仍需定期复核。来源索引不构成对全文本地阅读、训练或再分发的授权。

## 数据与流程

- 新来源在 `sources` 表中幂等插入；现有文章 `articles` 表结构不变：`title`、`published_at`、`description`（留空）、`content_html`（留空）、`source_id -> sources.name`、`url`。列表点击后打开原站；中文标题与原文 URL 均来自来源的 RSS。
- 同步前对官方 RSS 地址与原文域名进行白名单校验，避免任意外部链接与 SSRF；对 RSS 前 100 条按标题匹配半导体专用关键词，其他条目标记 `filtered` 而非当作失败。统一 UTC 日期与 URL 去重；同日同标题也阻止跨源重复。旧数据不会被删除。
- 沿用成功后至少 15 分钟的请求间隔、ETag/Last-Modified 条件请求、429/5xx 最多两次短时重试、`Retry-After` 遵循、单源响应 2 MB 限制。站点没有公布供本应用使用的具体配额，仍需监测 429 并遵守站方最新规则。
- 正式库首次同步：IT之家 60 条中筛出 2 篇、驱动中国 15 条中筛出 1 篇、新时空 50 条中筛出 7 篇，共新增 **10 篇、0 失败**。旧库 228 篇保持不变。新时空最新匹配文章日期为 2026-09-24，不能把“抓取成功”误认成每天都有半导体新文章。

## 验收与限制

运行 `PYTHONPATH=src .venv-rag/bin/python -m pytest tests/test_articles.py -q --disable-warnings`，核对 `/articles` 的三个来源、新增数量、标题时间和原文链接；重复点击不应重复入库。综合资讯 RSS 窗口有限，即使 15 分钟允许再同步，错过窗口的新闻仍可能漏掉；后续若业务需要更完整的国内覆盖，应与垂直媒体洽谈官方接口或授权订阅。文章数量超过 500 后，前端列表需要真正的分页，不应通过不断提高单次读取上限处理。
