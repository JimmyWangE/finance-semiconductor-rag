# 在线研报上传与入库

入口：`http://127.0.0.1:8090/library`，研究台顶部的“文档与知识库”也可进入。

## 使用流程

1. 选择 PDF，填写报告标题、发布机构、发布日期，提交“上传并解析”。默认开启图表识别。
2. 后台提取文本层与表格，扫描页自动调用当前本地多模态模型识别。查看任务状态与页数进度。
3. “待校对”后打开“校对与入库”。逐页对照原 PDF，点击“定位原文”查看文字块或表格所在区域。
4. 可修改识别文字、勾选/排除内容块、排除整页，并修正机构、标题、日期。视觉识别应特别核对数字、符号、单位和年份。
5. 保存校对并“确认入库”。任务显示“已入库”后，研究台可立即检索新内容，无需重启服务。

相同 PDF 按完整文件 SHA256 去重，即使改了文件名也返回原任务。去重范围是在线上传文件；
与旧离线语料之间的内容重复仍由检索阶段处理。已入库 PDF 的审核文本只读，以保证已发出的引用稳定；
修订后的新版 PDF 可作为新文档上传。

## 提取与定位

| 内容 | 提取方法 | 定位粒度 |
| --- | --- | --- |
| PDF 文本层 | PyMuPDF，沿用已有双栏阅读顺序与清理规则 | 原始文字块的位置框 |
| 可识别的表格 | PyMuPDF find_tables，保留 Markdown 表头 | 原始表格的位置框 |
| 扫描页/损坏文本层 | 本地 Qwen 多模态逐页转录 | PDF 整页，明确标记视觉识别 |
| 图表 | 对含图像/较多矢量绘图的页面进行视觉识别 | PDF 整页，保留页面渲染图 |

页码为从 1 开始的 PDF 物理页号。上传文件原字节不修改；高亮是在内存中的页面副本上绘制。
分块不跨页，片段保留 `upload_id`、`pdf_sha256`、`page`、`source_blocks`
（块 ID、bbox、提取方法、是否被校对修改）。超长表格再次切分时保留表头。
原始提取文字与人工修改后的文字分别保存，不能将“修改后的语料”误称为原 PDF 原始文字。

重复页眉/页脚只在页边缘判断。疑似免责声明保留在校对页但默认不纳入。
扫描页全文识别失败时需要重新解析或排除后才能发布。已有 PDF 文本层的页面若仅图表识别失败，
仍可发布其原生文字和结构化表格，页面会保留警告且未识别的图表不会入库。
空白页可能进入低文本量检测，需要校对时排除。
复杂版式、跨页表格和图表数字不能保证全部自动识别正确，因此保留人工校对。
视觉转录没有文字级坐标，不伪造位置框；跨页表格目前保持分开的页面证据。

参考：[PyMuPDF 文本提取与位置](https://pymupdf.readthedocs.io/en/latest/app1.html)、
[表格 API](https://pymupdf.readthedocs.io/en/latest/page.html)、
[FastAPI UploadFile](https://fastapi.tiangolo.com/tutorial/request-files/)。

## 状态与存储

```text
queued → parsing → recognizing → review → queued_index → indexing → indexed
                    ↓             ↑                         ↓
                  failed       人工校对                  index_failed
```

任务持久化在 `data/rag/uploads/jobs.sqlite3`。服务启动时恢复未完成的解析/入库任务。
处理线程使用目录独占锁，同一上传目录仅允许一个实例处理。解析在有超时/内存限制的子进程中执行。
视觉模型调用串行，识别期间查询可能返回带重试信息的 503。

每份文件位于 `data/rag/uploads/<SHA256>/`：

- `source.pdf`：不可变原文件。
- `parsed.json`：原生解析结果。
- `document.json`：逐页节点、原始提取文字、视觉结果、校对内容及排除状态。
- `page-N.png`：扫描页/图表页渲染图。
- `approved.json`：确认的发布内容，也是重启后恢复入库的依据。
- `parse.log`、`progress.json`：解析日志和进度。

入库时保留旧索引证据，复用旧向量，只编码新增内容。新快照完成完整性检查和 BM25 准备后，
短暂取得查询锁，切换磁盘 current 与进程内检索器。失败时旧索引继续服务，失败任务可以再次确认入库。
之后执行离线 `bash rag.sh build` 也会合并已确认的上传文件。离线完整重建仍需重启服务激活；
在线上传通过热切换自动激活。

默认每文件 50 MB、200 页、原生解析 180 秒、视觉请求每页 60 秒。
配置字段为 `upload_max_mb`、`upload_max_pages`、`upload_parse_timeout`。
损坏/加密 PDF 会失败并给出提示，请使用可读取的 PDF。视觉输出截断或结构无效时不自动入库。

## API

```bash
curl --noproxy '*' http://127.0.0.1:8090/uploads \
  -F 'file=@/path/to/report.pdf' \
  -F 'title=半导体行业研究报告' \
  -F 'org=某某证券' \
  -F 'publish_date=2026-09-07' \
  -F 'visual=true'
```

返回 HTTP 202 和任务 ID。接口如下：

| 方法与路径 | 用途 |
| --- | --- |
| GET /uploads | 上传任务与文件限制 |
| GET /uploads/{id} | 单个任务状态 |
| GET /uploads/{id}/pages/{page} | 一页解析内容及校对状态 |
| GET /uploads/{id}/pdf | 原始 PDF |
| GET /uploads/{id}/pages/{page}/image?block_id=... | 原页图片及指定块高亮 |
| PUT /uploads/{id}/review | 保存元数据、修改与排除状态，要求匹配 revision |
| POST /uploads/{id}/publish | 提交 revision，确认入库 |
| POST /uploads/{id}/retry | 失败/待校对任务重新解析，清除原校对修改 |
| GET /sources/{chunk_id}/page | 检索证据对应的原页高亮 |

校对请求示例：

```json
{
  "revision": 0,
  "metadata": {"title":"报告标题","org":"发布机构","publish_date":"2026-09-07"},
  "excluded_pages": [5],
  "edits": [{"block_id":"p1-b2","text":"校对后的文字","included":true}]
}
```

未列在 edits 中的内容保留原状态；excluded_pages 是全部排除页面的完整列表。
原始文字和 bbox 不能被客户端覆盖。发布请求为 `{"revision":N}`。
上传相关写请求要求 Content-Length，浏览器和上述 curl 会自动发送。
现有 RAG_API_KEY 认证同样保护上传、校对和原文件接口。

## 验证

```bash
PYTHONPATH=src .venv-rag/bin/python -m pytest tests/test_rag.py tests/test_uploads.py -q

# 创建独立的数据/索引目录与测试PDF，避免污染正式知识库
PYTHONPATH=src .venv-rag/bin/python tests/prepare_upload_sandbox.py
bash rag.sh --config /tmp/rag-upload-acceptance-.../config.json serve --port 8091
.venv-rag/bin/python tests/upload_acceptance.py /tmp/rag-upload-acceptance-...
```

浏览器脚本 `tests/browser_uploads.cjs` 需要 Playwright，使用 RAG_SANDBOX 和可选 RAG_URL 指向隔离环境。
2026-09-07 验收覆盖网页上传、校对保存、表格原文高亮像素、发布、桌面/手机布局及已入库内容只读。
真实模型验收包含纯扫描页转录、发布前不可检索、热更新后数值问答、原 PDF 字节一致性与来源高亮。
入库期间同时执行检索，检查不出现分词器并发异常。

验收结果与桌面/手机截图保存在 `data/rag/eval/uploads-20260907/`。12 项自动测试通过。
隔离验收首次新增 5 个证据块，复用正式索引的 13,205 个旧向量；真实问答返回测试报告中的
营业收入 12.34 亿元，纯扫描页的月产能 4.8 万片也被正确识别并定位到第 3 页。
另外抽查现有东海证券 28 页研报，新解析器提取 497 个内容块，并将已知的 95.8% 定位到第 2 页。
所有验收 PDF 均留在 `/tmp` 隔离知识库，正式知识库仍为原有 395 份研报。
