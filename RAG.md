# 半导体研报 RAG

本工程已用现有 395 份研报构建可运行的检索问答系统，复用
`output/qwen35-semi-merged` 的 SFT 模型服务。研究工作台默认位于
`http://127.0.0.1:8090/`，API 文档在 `/docs`。同一服务还提供独立的通用问答
工作台 `http://127.0.0.1:8090/chat`。

两个工作台共用 Qwen3.8-27B SFT 推理服务，但边界不同：研报研究台始终经过
检索、证据阈值和引用校验，只依据知识库证据回答；通用问答台不调用 Retriever，
使用模型自身知识回答，也不生成研报引用。两类会话在 SQLite 中按 `rag` 和
`general` 隔离，不能通过另一类接口读取或续写。

在线新增研报进入 `/library`：上传 PDF、后台解析、逐页校对后确认入库，索引自动更新。
完整说明见 [UPLOADS.md](UPLOADS.md)。

研究台支持多轮追问与服务端会话历史，说明见 [CONVERSATIONS.md](CONVERSATIONS.md)。

## 链路与选型

```text
data/clean/*.md + data/raw_md/*.md + metadata.jsonl
                     ↓ 恢复原始页号、保留报告来源、正文/表格切分
captions.jsonl ─────→ 带原图和页号的图表片段
                     ↓ tokenizer 校验，超长片段再切分
              13,205 个证据片段
                     ↓
          BGE-M3 normalized CLS，1024 维
                     ↓
         FAISS IndexFlatIP + 中文 BM25
                     ↓ 日期/机构/文档/类型前置过滤
         各路 Top48 → RRF(k=60) → Top32
                     ↓
         BGE-reranker-v2-m3 → Top6 + 来源去重
                     ↓ 相关性门槛 + 真实 tokenizer 上下文预算
         已有 Qwen3.8-27B SFT /v1/chat/completions
                     ↓ JSON 结论 + 引用摘录
         引用存在 / 连续摘录 / 数字出现校验
                     ↓ 失败重试一次，仍失败则拒答
          答案 + 研报/券商/日期/页码/PDF/原图
```

采用 BGE 官方推荐的混合检索与重排路线，模型推理由现有 Transformers
完成。此规模的向量矩阵约 52 MiB，FAISS 精确内积检索足够，避免近似检索的
召回损失与额外数据库服务。元数据保存在 JSONL，向量缓存使用 SQLite，
BM25 使用 `rank-bm25` 与 `jieba`。未来百万级数据或多进程部署时，可将
存储层替换为 Qdrant/Milvus，保留上层证据结构与问答接口。

参考：[BGE-M3 模型说明](https://huggingface.co/BAAI/bge-m3)、
[BGE 重排模型](https://huggingface.co/BAAI/bge-reranker-v2-m3)、
[FAISS](https://github.com/facebookresearch/faiss)、
[RRF 混合检索说明](https://qdrant.tech/documentation/search/hybrid-queries/)。

## 数据与溯源

- 使用 `data/clean`，不使用 SFT 生成答案作为知识库。训练阶段跨报告去重
  会删除其他报告的相同段落；RAG 保留每个来源，返回时按内容哈希去重，
  并限制每份报告最多 3 条。相似但不完全相同的段落仍可能同时返回。
- 复用 `clean.py` 的清洗规则，从已有 raw Markdown 重放清洗并保留页号，
  然后仅保留已经出现在 clean 文件中的段落。本批次未定位段落数为 0。
  对新批次不能匹配的段落保留 `page=null`，不猜测页码。
- 原始解析及图表记录的页号从 0 开始，对用户统一转换为从 1 开始的 PDF
  物理页号；它不一定等于研报印刷页码。
- 图表来自成功且非无效的 captions 记录，不从 corpus 文末反推页号。
  同一图的成功记录去重；每条保留原图地址与 `vlm_caption` 标记。
- 图表转述本身未经人工审核，可能识别错误。界面和答案提示核对原图。
  PDF 题注也来自既有最近邻提取，不能保证题注与图像语义完全对应。
- `publish_date` 使用已有 CSV 元数据，正文中的日期可能不同。日期过滤
  是报告发布日期过滤，并不是财务指标所属期间过滤。
- 当前文档发布日期覆盖 **2025-11-30 至 2026-08-19**。没有实时行情源。

## 运行

```bash
cd /home/wjs/finance/semi_llm

# 后台启动，等待健康检查；已启动时只报告现有地址
.venv-rag/bin/python scripts/rag_service.py start
.venv-rag/bin/python scripts/rag_service.py status
.venv-rag/bin/python scripts/rag_service.py stop

# 前台运行；端口冲突时自行选择另一个端口
bash rag.sh serve --port 8091

# 命令行检索与问答（每次会重新加载检索模型，日常优先使用 HTTP 服务）
bash rag.sh search '中芯国际2025年第三季度产能利用率是多少？'
bash rag.sh ask '中芯国际2025年第三季度产能利用率是多少？'
bash rag.sh search 'DRAM价格变化' --kind chart --date-from 2026-01-01
bash rag.sh search '先进封装' --org 开源证券 --as-of 2026-03-31
```

远程开发机可将 8090 端口转发到本地浏览器。默认只监听 localhost。
若显式监听其他地址，启动器要求设置 `RAG_API_KEY`；API 客户端使用
`Authorization: Bearer ...`。启用密钥后，浏览器工作台需通过可注入该请求头
的受控反向代理访问。目前没有独立用户账户和登录页。

后台 PID、日志在 `data/rag/run/`。启动器只终止自己记录且校验过启动时间与
命令行的 RAG 进程，不管理现有 vLLM 服务。未配置开机自启。

## HTTP 接口

```bash
curl --noproxy '*' http://127.0.0.1:8090/health

curl --noproxy '*' http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"中芯国际2025年第三季度产能利用率是多少？","top_k":6,"filters":{"as_of":"2026-08-31"}}'

curl --noproxy '*' http://127.0.0.1:8090/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"存储价格","mode":"rerank","filters":{"kind":"chart","org":"开源证券"}}'
```

| 接口 | 作用 |
| --- | --- |
| `GET /` | 无外部前端依赖的研究工作台 |
| `GET /chat` | 不连接研报知识库的通用问答工作台 |
| `GET /health` | 同时检查 RAG 与生成服务，生成不可用返回 503 |
| `GET /stats` | 索引版本、语料统计、构建参数 |
| `GET /documents?offset=0&limit=50` | 报告列表与机构统计 |
| `POST /search` | 返回证据与各阶段分数；mode 支持 bm25/dense/hybrid/rerank |
| `POST /ask` | 返回 answer、claims、sources、status 和耗时 |
| `POST /general/ask` | 通用模型问答，不执行文档检索 |
| `POST /general/ask/stream` | NDJSON 增量输出；完成后才写入会话 |
| `/general/conversations` | 通用问答会话的创建、列表、读取、改名和删除 |
| `GET /sources/{chunk_id}/pdf` | 对应原 PDF，可附加 `#page=N` |
| `GET /sources/{chunk_id}/figure` | 对应原图 |

过滤字段：`date_from`、`date_to`、`as_of`、`org`、`doc_ids`、`kind`。
`as_of` 默认当天，与 `date_to` 取较早者。过滤在排序候选之前生效。
`org` 精确匹配；空 `doc_ids` 表示不匹配任何文档。未知字段返回 422。
问答不接受客户端指定跳过重排。当前单次处理一个模型请求，繁忙时返回
503 和 `Retry-After`；生成超时返回 504，上游异常返回 502。

## 配置、依赖与更新

配置在 `configs/rag.json`，相对路径以项目根目录为基准。
可通过 `RAG_CONFIG` 或 `bash rag.sh --config path ...` 指定配置；
`RAG_DEVICE`、`RAG_LLM_BASE_URL`、`RAG_LLM_MODEL`、`RAG_INDEX_DIR`
支持环境变量覆盖。默认在 `cuda:0` 使用 PPU 的兼容接口加载两个 BGE 模型，
batch size 为 8，eager attention；可设置 `RAG_DEVICE=cpu`，但速度会下降。

依赖遵循本机 PPU 环境技能：独立 `.venv-rag` 继承基础环境，仅新增 FAISS CPU、
BM25、jieba，未替换 Torch、Transformers 或现有推理服务。

```bash
python -m venv --system-site-packages .venv-rag
.venv-rag/bin/python -m pip install -r requirements-rag.txt \
  --trusted-host art-pub.eng.t-head.cn

# 下载到独立路径，保留原有不完整的 /home/wjs/models/bge-m3
.venv-rag/bin/python scripts/download_rag_models.py --repo BAAI/bge-m3
.venv-rag/bin/python scripts/download_rag_models.py --repo BAAI/bge-reranker-v2-m3

bash rag.sh build
```

依赖源需要有效认证，凭据应由环境配置提供。模型下载从模型仓库 API
解析 revision，文件按 revision 下载，LFS 权重以 SHA256 检验，记录到
`rag_model_manifest.json`；支持中断后续传。

新增报告时先沿用现有解析、元数据、清洗和图表转述流程更新输入，再执行
`bash rag.sh build`。构建锁避免同时写入；每批编码提交 SQLite 缓存，缓存键
包含模型指纹、编码参数和实际检索文本。删除的文档会从新快照消失，旧缓存
保留以便复用。不要手工编辑运行中的快照。

每次成功构建写入 `index/generations/<版本>/`，完成后原子替换 `current`
符号链接；失败不会替换旧版本。服务启动时校验文件哈希、模型指纹与维度，
加载一个固定快照。离线完整重建后需重启服务；通过在线上传入库则自动热切换，无需重启。
旧快照仍保留，可供审计与手动回滚；缓存与快照目前没有自动清理策略。

## 验证和评估

新增线上完整测评工具 `scripts/benchmark_rag.py`，详见 [BENCHMARK.md](BENCHMARK.md)。
支持 Precision/Recall/F1、排序指标、并发和定速压力、恢复检查、CPU/RSS、逐请求数据及报告。

```bash
PYTHONPATH=src .venv-rag/bin/python -m pytest tests/test_rag.py -q
.venv-rag/bin/python scripts/rag_smoke.py
bash rag.sh prepare-eval --limit 60
bash rag.sh evaluate --dataset data/rag/eval/questions.jsonl \
  --output data/rag/eval/retrieval_report.json
```

2026-09-05 的首轮检索评估：60 题、26 份测试报告，含数值、逻辑、图表、风险。

| 检索方式 | 证据 Hit@5 | 证据 Hit@10 | MRR@10 | 检索中位耗时 |
| --- | ---: | ---: | ---: | ---: |
| BM25 | 65.0% | 70.0% | 0.558 | 121ms |
| BGE-M3 | 46.7% | 58.3% | 0.350 | 34ms |
| RRF 混合 | 63.3% | 75.0% | 0.463 | 149ms |
| 混合 + BGE 重排 | 80.0% | 85.0% | 0.700 | 381ms |

Hit@K 表示前 K 条至少命中一个标注片段，不能写成 Recall@K。
完整 Recall、nDCG、逐题排名在 `data/rag/eval/`。这些是 SFT 测试题的源材料
与索引片段自动对应得到的弱标注，不是独立人工基准，也不是回答正确率。
相同事实的其他报告可能没有被标成正例。测试题的措辞可能依赖原任务材料。
评估不把原任务 input 或答案传给检索器，所有检索方式使用同一问题与过滤条件。
评估暂不使用按报告多样性截断，以便比较排序算法本身；线上默认有该截断。

实测问答：中芯国际 2025Q3 产能利用率回答 95.8%，包含可核对的报告页码与摘录；
2035 年实际利润问题拒答；截至 2020 年的过滤不返回未来报告；PDF/图表链接可访问。

验证记录：8 项自动测试通过，覆盖引用/数字校验、日期过滤、页码、表格切分、
上下文 token 计数、API 认证与路径限制，以及增量缓存、文档删除和失败构建保持旧快照。
`smoke_report.json` 保存真实模型的业务验证输出。
Playwright 在 1440×1000 与 390×844 下通过问答、图表检索及原图加载检查，
无脚本错误或横向溢出，截图保存在 `data/rag/eval/screenshots/`。
浏览器测试脚本为 `tests/browser_rag.cjs`，运行需要测试环境安装 Playwright/Chromium。
本机测试用的浏览器共享库与中文字体仅解压到 `/tmp/semi-rag-browser-UjBP3J/`，
通过进程环境变量加载，未安装进基础运行环境。

全量构建首次耗时约 117 秒；重复构建全部 13,205 个向量命中缓存，约 23 秒。
最终片段为正文 7,461、表格 1,886、图表 3,858；最长嵌入输入 1,021 tokens，
没有超过配置的 1,024 token 上限。评估版本与当前版本的三个索引数据文件 SHA256
一致，后续版本仅修正了切分后类型统计。

## 当前边界

这是单机可用的第一版研究系统。结构校验只验证“引用存在、摘录匹配、数字出现在
引用中”，不验证结论被原文完整蕴含，也不证明源报告或图表提取本身正确。
例如同一数字对应错误公司、符号、单位，仍需语义评估与人工核验。
`min_rerank_score=0.15` 是初始相关性门槛，不是校准后的正确率概率。

回答默认使用 `answer_policy=best_effort`：核心事实有直接证据时允许给出完整或部分回答，
次要限定缺失会通过 `coverage=partial` 和 `limitations` 显示证据边界，不会整题拒答。
数字仍须存在于所引用的完整证据片段中，引用摘录仍必须是连续原文。调用 `/ask` 时可传
`answer_policy=strict` 恢复公司、期间、指标和单位全部匹配的严格模式。

后续质量迭代应建立独立人工题集，覆盖企业/时间/单位混淆、跨报告冲突、
不存在的信息与提示注入，并按检索召回和回答忠实度分别评估。
研究台已支持多轮指代重写、会话保存与澄清，旧 API 的单轮模式保留；尚未实现流式输出、自动计算或实时联网取数。
这些扩展不应绕过来源过滤与证据校验。
