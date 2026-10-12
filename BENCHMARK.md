# RAG 测评

入口 `scripts/benchmark_rag.py`。直接访问已运行的 RAG HTTP 服务，不加载第二份嵌入模型，
不修改并发策略、索引或知识库。只使用现有 httpx 和 Python 标准库，无新增依赖。
原有 `bash rag.sh evaluate` 保留为离线算法比较，并补充 Precision/F1/AP 指标。

已运行的实测报告：[2026-09-08 结果](data/rag/eval/benchmarks/RESULTS-20260908.md)。
包含完整检索/问答基线、search/ask 的并发和阶梯压力，以及 30 秒并发和 60 秒持续负载。

## 快速运行

```bash
cd /home/wjs/finance/semi_llm

# 完整基线：60题、4种检索模式、7个问答回归、检索并发与压力测试
.venv-rag/bin/python scripts/benchmark_rag.py \
  --chunks data/rag/index/current/chunks.jsonl

# 只测检索质量
.venv-rag/bin/python scripts/benchmark_rag.py --sections retrieval \
  --ks 1,5,10 --modes bm25 dense hybrid rerank \
  --chunks data/rag/index/current/chunks.jsonl

# 单独测完整生成链路（比纯检索慢，限制请求数量）
.venv-rag/bin/python scripts/benchmark_rag.py --sections concurrency stress \
  --endpoints ask --concurrency 1,2,4,8 --requests 12 \
  --concurrency-duration 180 --rates 0.25,0.5,1,2 --stage-seconds 15
```

默认输出至新的 `data/rag/eval/benchmarks/<UTC时间>-<随机后缀>/`。
`--output` 可以指定新目录；已有目录不会覆盖。执行时逐阶段输出进度，逐请求落盘。
服务设置密钥时读取环境变量 `RAG_API_KEY`，也可用 `--api-key-env OTHER_VARIABLE`；
报告只保存环境变量名，不保存密钥。所有 HTTP 请求绕过机器的代理环境变量。

## 检索质量

对相同问题、相同过滤范围比较 BM25 / dense / hybrid / rerank。线上保留来源去重和
每份报告数量上限，和旧离线 `diversify=False` 的算法评估不是完全相同的条件。
按每题统计、宏平均、微平均，并按 task 分组；宏平均给出 1,000 次问题级 bootstrap 的
95% 均值置信区间。来自同一报告的题目有关联，区间可能偏窄，不用于独立样本显著性结论。

| 指标 | 定义 |
| --- | --- |
| Precision@K | 前 K 位命中的不同相关片段数 / K；不足 K 条仍除以 K |
| Recall@K / evidence_recall@K | 前 K 位命中的不同相关片段数 / 全部标注相关片段数 |
| F1@K | Precision 与 Recall 的调和平均 |
| Hit@K / evidence_hit@K | 前 K 条是否至少命中一个相关片段，宏平均为命中题比例 |
| MRR@K | 首个相关结果排名的倒数；没有则为 0 |
| nDCG@K | 二元相关性、log2 折扣的归一化累计增益 |
| AP@K | 相关结果所在位置的 Precision 累加 / min(相关片段总数,K)；宏平均相当于 MAP@K |
| doc_hit@K | 前 K 条是否命中至少一份标注来源报告 |
| micro_precision@K | 各题 TP 总和 / (题数×K) |
| micro_recall@K | 各题 TP 总和 / 各题相关片段总数 |

重复片段只在第一次出现时计分。请求失败作为空结果计入质量分母，同时单独记录失败数量；
不会只评估成功请求而抬高分数。任何过滤越界单独计数并令测评不通过。
成功请求的延迟另算，不把快速拒绝混入成功请求分位数。

默认数据是 `data/rag/eval/questions.jsonl`：从 SFT 测试题原材料与索引片段重合自动生成的弱标注。
其他报告可能存在同样正确的内容而没有被标注，因此未标注即不相关会低估精确率。
这些结果不能写成“人工答案准确率”。建议人工标注企业、年份、季度、单位、风险、图表等不同类别，
汇集多种检索器的候选后共同审核正例和负例；保留一套未用于调参的最终测试集。

检索 JSONL 格式：

```json
{"id":"q001","query":"问题","task":"qa_num","gold_doc_ids":["doc-id"],"gold_chunk_ids":["chunk-id-1","chunk-id-2"],"filters":{},"label_origin":"human_reviewed"}
```

每行 ID 唯一、query 非空。检索数据必须有正例。无答案题放入问答回归数据，不把空正例的 Recall
伪记为 100%。可使用 `--chunks` 校验标注 ID、对应文档、日期/机构范围是否有效，并核对本地索引
数据 SHA256 与线上快照；不提供时只能校验格式，无法证明标签仍属于当前索引。
查询期间索引版本变化会中止检索质量测评。每次运行固定 `--as-of`，默认启动当天，避免跨天漂移。

## 问答回归

默认 `data/rag/eval/quality_cases.jsonl`，涵盖数值、美元口径、未来未知信息、历史截止范围、
空文档范围与无证据情况下的编造请求。单轮 `/ask` 不写入会话历史。

```json
{"id":"case-1","query":"问题","expected_answerable":true,"expected_all":["23.82","美元"],"expected_any":[],"forbidden":[],"filters":{},"task":"qa_num"}
```

- expected_all：全部字符串必须出现；expected_any：非空时至少命中一个；forbidden：不得出现。
- 预期可答题要求 `answered`，预期无答案题要求 `insufficient_evidence`。
- `validation_failed` 虽也显示拒答，不能当作正确无答案判断。
- 独立检查引用 ID、连续摘录、每条结论是否有有效摘录、发布日期/过滤范围；不盲信服务自己报告的校验结果。
- 明确报告可答题通过率、无答案题通过率、摘录有效比例及 HTTP 失败。

这些是可复现的回归断言，不是完整语义忠实度或财务正确性评分。关键词匹配不能识别所有
公司、单位、否定关系错误。原图数字仍需人工核对。独立人工评审或经过校准的 judge 可在此基础上
扩展，但不将未校准的 LLM 分数冒充真值。多轮专项继续使用 `tests/conversation_acceptance.py`，
上传定位专项使用 `tests/upload_acceptance.py`，它们需在隔离环境运行以免产生测试历史/文档。

## 并发与压力

### 固定并发用户

`--concurrency 1,2,4,8 --requests 40`：每阶段总计 40 个请求，每个用户等待完整响应后再发下一个。
`--min-interval 0.1` 限定同一用户两次请求开始的最小间隔，避免 503 快速返回形成请求洪泛。
`--concurrency-duration` 是阶段发起请求的时间上限；到时停止新增，已发出的请求按超时边界收尾。
报告实际 peak_inflight、是否达到预期总数。它衡量服务面向这些客户端的实际表现，不是 GPU 并行执行数。
需要按时长而不是总请求数测并发时，设置 `--requests 0 --concurrency-duration 60`；
默认 40 个请求主要用于快速基线，高并发下可能大多被拒绝，成功样本不足 20 个会提示不能据此承诺容量。

### 固定到达率

`--rates 1,2,4,8 --stage-seconds 15`：依次执行每秒 1/2/4/8 个请求的定速测试，每档持续 15 秒。
到达时刻与前一请求是否完成无关，能够揭示排队/拒绝和延迟上升，避免闭环测试的协调遗漏。
记录每个请求的调度延误和从计划时刻到完成的总时间；客户端超过 max_inflight 或明显调度落后时
记录 client_dropped，不建立无限等待队列。client_dropped 表示负载没有完全送达，不能归咎服务端。

默认每阶段 max_requests 为 10,000、max_inflight 为 32。触及请求上限会明确标记阶段截断。
压测无自动重试，避免把第一次失败隐藏起来。HTTP 429 或带 Retry-After 且明确 busy 的 503
计为接纳拒绝；其他 503/5xx、超时、连接失败和格式错误计为真实错误。

| 性能指标 | 口径 |
| --- | --- |
| successful_throughput_rps | 合法成功响应 / 实际阶段总时长（包括收尾） |
| attempt_throughput_rps | 全部已发送请求 / 实际阶段总时长，不得代替有效吞吐 |
| answered_throughput_rps | /ask 中业务状态为 answered 的响应 / 时长 |
| success_latency_ms | 仅成功响应的均值、P50/P90/P95/P99、最大值和样本数 |
| rejection/error latency | 拒绝/错误响应各自独立统计 |
| rejection_rate / error_rate | 拒绝/真实错误数量分别除以已发送请求数 |
| delivery_rate | 已发送数 / 计划到达数 |
| answer_status_counts | answered、insufficient_evidence、validation_failed 等业务状态计数 |

每阶段结束进行健康检查和同端点的真实恢复探测，不只检查 HTTP 服务是否存活。
连续窗口默认 20 个实际请求中真实错误比例达到 30% 时停止加压，busy 拒绝不触发该阈值。
恢复探测默认最多 30 秒。客户端超时可能意味着服务还在处理，因此恢复时间也是必要结果。
不重启服务、不 kill 模型进程、不调整任何并发配置。

### 持续负载与资源

```bash
# 较长的稳定性测试，在低业务时段执行
.venv-rag/bin/python scripts/benchmark_rag.py --sections stress \
  --endpoints search --rates 1,2,4 --stage-seconds 300 \
  --max-inflight 32 --max-requests 10000 --cooldown 10

# 按时长测并发，每档持续一分钟，避免快速拒绝过早消耗样本配额
.venv-rag/bin/python scripts/benchmark_rag.py --sections concurrency \
  --endpoints search --concurrency 1,2,4,8 --requests 0 --concurrency-duration 60

# API 与生成链路都测，指定真实的本机 API PID 才能归因资源占用
.venv-rag/bin/python scripts/benchmark_rag.py --sections concurrency stress \
  --endpoints search ask --requests 12 --concurrency-duration 180 \
  --rates 0.25,0.5,1,2 --stage-seconds 30 \
  --server-pid 12345 --sample-interval 2
```

CPU/RSS 只属于指定本机 API 进程，不包括 vLLM worker、GPU/PPU 内存或远程服务。
CPU 按单核百分比计，多核可超过 100%。资源原始记录在 resources.jsonl；机器可用内存和 load average
是宿主机指标，不能证明都是 RAG 消耗。远程测评省略 server-pid，或在远程机器另采监控数据。
当前脚本测的是非流式完整响应时延，不提供 TTFT、tokens/s 或上传解析吞吐。

## 结果与退出码

- report.json：配置、数据 SHA256、运行前后快照、指标、分组、置信区间、恢复、资源和阈值结论。
- report.md：可阅读的质量/性能表格。
- requests.jsonl：逐请求记录，质量模块包含原始回答供复核，负载模块不落全文。
- requests.csv：便于表格分析的核心请求字段。
- resources.jsonl：资源时间序列。

测试预热单独执行，不计入正式样本；查询顺序与抽样 seed 固定。负载默认轮询同一真实题集，
重复请求可能命中生成服务 prefix cache，所以比较部署时必须保持相同配置并注明热缓存条件。
报告含原始业务回答，按研报数据的权限管理，不上传到外部服务。

```bash
# 可用于回归/CI的显式阈值（示例数值，需根据人工基线调整）
.venv-rag/bin/python scripts/benchmark_rag.py --sections retrieval \
  --modes rerank --gate-k 5 --min-precision 0.15 --min-recall 0.20

.venv-rag/bin/python scripts/benchmark_rag.py --sections concurrency \
  --concurrency 1 --requests 30 --max-p95-ms 1000 --max-rejection-rate 0.01
```

质量阈值应用于 rerank（没有 rerank 时应用第一个模式）。没有配置精确率/召回率阈值时不擅自
判断质量“达标”；请求失败/越界仍会判失败。负载拒绝率默认记录但不判失败，因为饱和阶段预期会拒绝；
要求达标时设置 max-rejection-rate。真实错误、没有成功样本、未完成闭环请求数、客户端丢弃或到达率截断
会导致失败。输出 complete 只代表指定检查完成，不代表可承诺该并发容量。

退出码：0 完成且无阈值失败；1 执行错误或阈值失败；2 CLI 参数错误；130 用户中断。
小样本 P99 仅为观测统计，不是可靠尾延迟估计；确定容量前增加重复次数、样本数量及持续时长。

## 公式与调度测试

```bash
PYTHONPATH=src .venv-rag/bin/python -m pytest tests/test_benchmark.py -q
```

覆盖手算 Precision/Recall/AP/nDCG、重复片段、缺失结果、失败分母、HTTP/业务分类、
定并发数量、开环客户端丢弃、错误停止、过期标签、过滤、伪引用、输出保护和资源采样。
