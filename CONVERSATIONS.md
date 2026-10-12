# 多轮研究对话

研究台 `/` 已支持服务端会话历史。左侧可以新建、切换、重命名和删除会话。
刷新网页后恢复当前会话；每轮可以单独查看引用证据及系统实际使用的检索问题。
原文检索模式仍为独立检索，不写入研究对话。

## 问题理解

```text
用户追问 + 当前会话近期历史 + 界面检索范围
                    ↓
本地模型：指代消解、时间/指标/单位补全、话题切换
                    ↓
         独立问题，或缺少对象时请求澄清
                    ↓
       重新执行混合检索、重排与证据问答
                    ↓
     保存原问题、改写问题、答案、引用和索引版本
```

例：先问“中芯国际2025年第三季度产能利用率是多少”，再问“它同期的营业收入是多少亿美元”，
系统补全公司、季度；再问“那华虹呢”，继承最近一轮的营收指标、期间和美元单位。
显式的新话题优先，不强行继承旧公司和期间。首次提问“它的营收呢”缺少对象时先澄清。
历史答案只帮助识别话题，不能直接作为新回答的证据；每轮重新取当前索引的原文。
历史引用按轮保存，查看旧轮不会把旧 S1 误认为当前轮的 S1。
问题理解后将“营收”统一为“营业收入”以减少检索措辞波动；生成侧对明确美元口径的问题增加检查，
避免用人民币金额替代。此检查不是通用单位换算或完整财务一致性验证。

默认最多将最近 8 轮、5,000 tokens 传给问题理解模块；历史答案每轮最多取 1,200 字符，
来源仅带文档标识和标题等元数据。超预算先移除最早轮次，不把无限历史塞入生成上下文。
完整历史仍保存在数据库中。每会话最多 200 轮，达到后需新建会话。
相关配置：`conversation_history_turns`、`conversation_history_tokens`、`conversation_max_turns`。

检索范围由界面或 API 控制，不允许模型任意修改日期、机构等条件。省略 filters 沿用会话范围；
显式提交 filters 替换整个范围，`{}` 清空限制。模型推断的“2025年第三季度”是指标所属期间，
不是报告发布日期过滤。只有明确指定历史某份报告时，才允许从历史来源 ID 选择该报告；
若界面已有文档范围，取交集。图表识别质量和引用校验的原有限制仍然适用。

## 保存与隔离

数据位于 `data/rag/conversations/history.sqlite3`，使用 SQLite WAL。
每轮成功或拒答/澄清结果一起原子写入；上游调用报错、超时不增加轮次。
服务重启不清空会话。网络响应丢失后使用同一个 request_id 重试会返回原结果，不重复调用模型。
每轮带 revision，旧页面基于过期上下文发送时返回 409，并要求刷新历史。
删除为软删除，界面和 API 不再返回该会话，磁盘记录仍保留，不等于彻底擦除。

会话按浏览器随机 cookie `rag_client` 隔离，cookie 使用 HttpOnly、SameSite=Strict，HTTPS 时 Secure。
这适合当前单机研究台，不是多用户账号系统：清除 cookie 或更换浏览器将无法看到原浏览器会话。
随机 cookie 是访问该浏览器历史的凭据，不应共享。现有 RAG_API_KEY 同时保护会话 API。
跨设备账号同步、团队授权、历史导出与流式输出尚未实现。

## API

不带 conversation_id 的原 `/ask` 请求继续保持单轮行为。
多轮 API 客户端必须保留服务端 cookie，例如 httpx.Client 或 curl 的 cookie jar。

| 接口 | 用途 |
| --- | --- |
| POST /conversations | 新建，body 为 `{}` 或 `{"title":"名称"}` |
| GET /conversations?offset=0&limit=50 | 当前浏览器会话列表 |
| GET /conversations/{id} | 完整历史、revision 和当前检索范围 |
| PATCH /conversations/{id} | `{"title":"新名称","expected_revision":N}` |
| DELETE /conversations/{id}?expected_revision=N | 软删除 |
| POST /ask | 单轮，或附会话字段执行多轮问答 |

```json
{
  "query": "它同期的营业收入是多少亿美元？",
  "conversation_id": "创建会话返回的UUID",
  "expected_revision": 1,
  "request_id": "为本条请求生成的UUID",
  "top_k": 6
}
```

响应保留原有 answer/sources/claims，新增 resolved_query、understanding、conversation_id、
turn_number、conversation_revision、request_id。`status=needs_clarification` 时 answer 是澄清问题，
sources 为空；用户在同一会话补充对象后继续处理。服务繁忙返回 503，网络超时返回 504，
这些失败可以保留原 request_id 重试。若修改问题或检索范围则使用新 request_id。

## 验证

```bash
PYTHONPATH=src .venv-rag/bin/python -m pytest tests/test_conversations.py tests/test_rag.py tests/test_uploads.py -q
.venv-rag/bin/python tests/conversation_acceptance.py --url http://127.0.0.1:8091 \
  --output /tmp/conversation-acceptance.json
```

真实模型测试涵盖指代、继承最近指标、公司切换、话题切换、澄清后补充、历史时间过滤和请求去重。
`tests/browser_conversations.cjs` 验证连续对话、刷新恢复、会话管理、按轮引用、浏览器隔离及手机布局。
问题改写仍有模型误判可能，界面可展开“本轮检索问题”核对；必要时明确公司、期间、单位后追问。

2026-09-07 验证：18 项自动测试通过，真实模型完成 7 轮验收，验证中芯国际产能利用率 95.8%、
追问美元营收 23.82 亿美元、切换华虹营收 6.35 亿美元，以及光刻胶话题切换、澄清后补充和历史过滤拒答。
浏览器通过桌面 1440×1000 与手机 390×844 检查，无脚本错误和横向溢出。
验收输出及截图保存在 `data/rag/eval/conversations-20260907/`，测试会话保留在独立临时数据库中。
