# 检索链路改进

本轮以现有 FAISS、BGE-M3、BM25、BGE reranker 为基础做可回退的检索改进。
不重写原始语料，不更换片段 ID，不改变页码或原图引用。已建立与测试报告不重叠的开发集，
用相同问题和相同标签比较方案，完整数据位于 `data/rag/eval/retrieval-upgrade/`。

## 开源依据

- [BGE-M3 官方建议](https://huggingface.co/BAAI/bge-m3)：混合召回后使用交叉编码器重排。
- [Haystack DocumentJoiner](https://docs.haystack.deepset.ai/docs/documentjoiner)：多个检索结果使用 RRF 等方法融合。
- [Haystack 多查询检索实现](https://github.com/deepset-ai/haystack/blob/main/haystack/components/retrievers/multi_query_text_retriever.py)：多个查询表达可以增加检索覆盖。
- [Elasticsearch 多字段检索](https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-multi-match-query.html)：独立字段分析与组合，避免把所有信息混成同一个匹配信号。
- [LangChain Query Construction](https://blog.langchain.dev/query-construction/)：将自然语言中的来源约束与语义检索问题分开。

这里复用这些检索策略，继续使用当前本地库实现，不宣称已安装 Haystack/Elasticsearch/LangChain，
也不引入额外数据库和 GPU 软件包。当前库只有约 1.3 万片段，更换向量数据库本身无法解决排序错误。

## 方案

1. 保留原问题召回，并增加正文独立 BM25 字段，减少标题、机构、日期对正文相关性的干扰。
2. 保留原始问题，同时生成至多一个确定性的检索变体：季度规范化和小型术语别名。不会调用 LLM 编造扩展事实。
3. 扩大每路候选和交叉编码器的重排深度，评估候选池的 Recall 与最终排名，二者不混为一谈。
4. 可将召回排名和交叉编码器排名再次 RRF 融合（交叉编码器权重 2，召回权重 1，常数 60），减少纯重排的噪声放大。
5. 同一报告证据上限可配置，保留精确内容去重。原有上限 3 对多段证据问题可能过强。
6. 来源识别只用于明确指定且唯一的券商，例如“根据东海证券研报”。比较多家券商、券商自身股价等问题不会触发。
   用户显式 org 优先；不将财务年份误当成研报发布日期；原来的日期、文档和类型过滤继续前置生效。

`/search` 增加 retrieval_profile、query_variants、reranked_candidates、source_routing，
`/stats` 增加 retrieval_parameters，方便判断实际运行方案。调试配置 retrieval_debug 可返回候选 ID；
默认不启用。原 rerank_score 保留，生成环节的相关性阈值与逐字摘录校验仍然有效。

## 评估方法

开发集 `data/rag/eval/retrieval-dev.jsonl` 为 SFT validation 的 60 道题、33 份报告，
与原 60 题测试集的报告交集为零。标签仍是原材料重合的弱标注，不等于人工相关性真值。
每种方案按线上每报告配额及内容去重策略评估，保持 K=1/5/10 和固定片段 ID。

```bash
.venv-rag/bin/python scripts/compare_retrieval.py \
  --prepare data/rag/eval/retrieval-dev.jsonl

.venv-rag/bin/python scripts/compare_retrieval.py \
  --dataset data/rag/eval/retrieval-dev.jsonl \
  --profiles legacy wide body expanded body_rerank \
  --output data/rag/eval/retrieval-upgrade/dev-ablation.json
```

仅扩大候选池时，开发集候选 Recall 从 45.71% 提高到 53.57%，但 Precision@5 从 24.67% 降至23.67%。
说明“召回更多”不等于“最终结果更准”，不应只展示其中有利的指标。
正文重排也未单独胜过旧方案，因此没有凭概念直接启用。

开发集冻结的 balanced 参数为：128 候选/路，96 重排候选，正文独立召回和确定性查询扩展，
召回/重排排名融合，单报告最多 5 段，显式来源识别。开发集 Precision@5 为 27.67%（旧24.67%），
Recall@5 为 33.39%（旧30.21%），MRR@5 为0.7456（旧0.7242），Hit@5 都是90%。
纯检索中位耗时约1.12秒（旧0.39秒）。此后固定该组合，只比较原测试集上的 legacy 与 balanced。

固定测试集60题结果：

| 指标 | 旧方案 | balanced |
| --- | ---: | ---: |
| 候选 Recall | 35.44% | 46.97% |
| 候选 Hit | 86.67% | 95.00% |
| Precision@5 | 18.67% | 20.33% |
| Recall@5 | 23.52% | 25.36% |
| Hit@5 | 80.00% | 81.67% |
| nDCG@5 | 0.3308 | 0.3434 |
| MRR@5 | 0.6931 | 0.6903 |
| Precision@10 | 10.00% | 11.83% |
| Recall@10 | 26.02% | 29.28% |
| 中位检索耗时 | 382ms | 1062ms |

提高了覆盖率与前K条证据质量，但 MRR@5 略降，不能声称所有指标都提高。
Precision@5 相对提升约8.9%，Recall@5 相对提升约7.8%；仍不足以称为高准确率系统。
报告包含逐题原始结果，可定位新旧方案得失；延迟增加约2.8倍，需要质量优先时使用。
按题配对 bootstrap 的95%差值区间：Precision@5约[0.00,3.67]个百分点，Recall@5约[-0.03,4.20]个百分点。
样本量和弱标注限制下，不能宣称统计显著改善；应通过新增人工题集继续验证。

原标签把来自同一个 SFT 材料的多个片段都视作正例，其中部分可能并非该问题直接需要的证据；
相同事实的其他报告又可能未标注。接下来仍需人工审核独立题集，覆盖财务期间、币种、公司、
跨报告观点冲突及图表识别错误。候选 Recall、Hit@K、Precision@K 和答案正确率必须分别报告。

## 兼容性

配置中 retrieval_profile=legacy 可以继续旧双路召回；candidate_k=48、rerank_k=32、
max_per_doc=3、source_routing=false、rerank_rank_fusion=false 恢复原检索策略。
新配置需要重启 RAG 服务激活，不需要重建向量索引。在线上传的热更新继续复用同样的检索器配置。
原 bm25/dense 模式仍可用于消融，hybrid/rerank 使用选定的多路方案。

扩展候选与增加正文检索会增加单次查询耗时；不把质量提升宣称为吞吐提升。
当前服务的单请求接纳限制与并发优化是独立问题，本轮不修改其行为。
