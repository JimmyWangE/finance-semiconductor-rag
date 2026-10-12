# 金融半导体平台服务启动流程

本文面向迁移到新 Linux 服务器后的 agent。命令默认在项目根目录执行；生产启动需要 root/sudo 权限。文档中的 `<...>` 是迁移时必须替换的占位符。

## 0. 配置清单

| 项目 | 当前值 | 新服务器替换 |
|---|---|---|
| 项目根目录 | `/home/wjs/finance/semi_llm` | `<APP_ROOT>` |
| RAG Python 环境 | `<APP_ROOT>/.venv-rag` | 在 `<APP_ROOT>` 创建或修改 `infra/bin/run-app`、`infra/bin/stack` |
| RAG 配置/数据 | `<APP_ROOT>/configs/rag.json`、`<APP_ROOT>/data` | 整体迁移；`data/rag/index/current/manifest.json` 必须存在 |
| 研报 PDF | `/home/wjs/finance/eastmoney_reports/pdfs` | `<PDF_ROOT>`，改 `rag.json:pdf_dir` |
| BGE-M3 | `/home/wjs/models/rag/bge-m3` | `<EMBEDDING_MODEL>`，改 `rag.json:embedding_model` |
| BGE Reranker | `/home/wjs/models/rag/bge-reranker-v2-m3` | `<RERANKER_MODEL>`，改 `rag.json:reranker_model` |
| Qwen SFT 合并模型 | `/home/wjs/finance/semi_llm/output/qwen35-semi-merged` | `<LLM_MODEL_DIR>`，传给 `VLLM_MODEL_PATH` |
| vLLM 项目/环境 | `/home/wjs/deployLLm`、`.venv-vllm` | `<VLLM_ROOT>`、`<VLLM_VENV>` |
| vLLM 密钥 | `/home/wjs/deployLLm/.vllm_api_key` | `<VLLM_API_KEY_FILE>`，权限 600 |
| Kokoro TTS | `/home/wjs/kokoro-open-tts` | `<TTS_ROOT>`，修改脚本中的 PYTHONPATH |
| TTS 模型缓存 | `/home/wjs/kokoro-open-tts/cache/huggingface` | `<TTS_CACHE>`，设置 `HF_HOME` |
| 数字人静态文件 | `/home/wjs/chatagent/apps/stage-web/dist` | `<DIGITAL_HUMAN_DIST>`，改 `api.py:DIGITAL_HUMAN_DIR` |
| WeWe RSS | `/home/wjs/wewe-rss` | `<RSS_ROOT>` |
| RSS SQLite | `<RSS_ROOT>/apps/server/data/wewe-rss.db` | 随 RSS 项目迁移 |

| 服务 | 监听 | 说明 |
|---|---|---|
| Nginx | `0.0.0.0:189` | 唯一对外入口：`http://<PUBLIC_IP>:189` |
| RAG FastAPI | `127.0.0.1:8090` | Nginx 上游，内嵌前端页面 |
| Keycloak | `127.0.0.1:8180` | 仅经 Nginx `/sso/` 访问 |
| Keycloak 管理健康 | `127.0.0.1:9000` | 本机检查 |
| PostgreSQL 16 | `127.0.0.1:5432` | `finance_rag`、`keycloak` |
| Redis 7 | `127.0.0.1:6379` | RAG 持久化任务队列 |
| vLLM | `127.0.0.1:190` | OpenAI 兼容 Qwen3.8-27B API |
| Kokoro TTS | `127.0.0.1:191` | 普通话语音 API |
| WeWe RSS | `0.0.0.0:192` | 公众号文章服务，建议迁移后改为本机监听 |
| Prometheus（可选） | `0.0.0.0:9090` | 抓取 RAG `/metrics` |
| Grafana（可选） | `0.0.0.0:8081` | 展示 Prometheus 指标 |

依赖关系：PostgreSQL → Keycloak/RAG；Redis → RAG；vLLM/TTS → RAG；WeWe RSS → RAG 文章接口；RAG → Nginx → 浏览器；Prometheus → RAG `/metrics` → Grafana。核心顺序为：系统依赖 → `stack setup`/PostgreSQL 初始化 → vLLM → Redis/Keycloak/RAG/Nginx → TTS/RSS；监控栈可最后启动。

## 1. 环境准备

当前实测版本：Python `3.12.3`、Node `20.20.2`、OpenJDK `21.0.12`、PostgreSQL `16.15`、Redis `7.0.15`、Nginx `1.24.0`。建议新机保持主版本一致。

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  postgresql-16 postgresql-client-16 redis-server \
  openjdk-21-jre-headless nginx-light curl git build-essential \
  python3.12 python3.12-venv python3-pip
node --version; python3 --version; java -version; nginx -v
corepack enable
corepack prepare pnpm@8.15.9 --activate
```

部署代码和 RAG 依赖：

```bash
export APP_ROOT=<APP_ROOT>
cd "$APP_ROOT"
python3.12 -m venv .venv-rag
.venv-rag/bin/python -m pip install --upgrade pip
.venv-rag/bin/pip install -r requirements-rag.txt -r requirements.txt
```

关键 RAG 依赖为 `faiss-cpu==1.8.0.post1`、`rank-bm25==0.2.2`、`jieba==0.42.1`、`fastapi==0.128.0`、`httpx==0.28.1`、`pymupdf==1.28.2`、`psycopg[binary]==3.2.10` 和 `redis==6.4.0`。GPU 机应保留原 Torch/Transformers/PPU 栈，不要盲目升级 Torch。

## 2. 配置迁移

### 2.1 RAG 与索引

在 `<APP_ROOT>/configs/rag.json` 替换以下绝对路径：

```json
{
  "data_dir": "data",
  "index_dir": "data/rag/index",
  "pdf_dir": "<PDF_ROOT>",
  "embedding_model": "<EMBEDDING_MODEL>",
  "reranker_model": "<RERANKER_MODEL>",
  "llm_tokenizer": "<LLM_MODEL_DIR>",
  "llm_base_url": "http://127.0.0.1:190/v1",
  "llm_model": "Qwen3.8-27B",
  "llm_api_key_file": "<VLLM_API_KEY_FILE>"
}
```

整体复制 `data/rag/index`；如果 `current/manifest.json` 不存在，先构建索引：

```bash
cd "$APP_ROOT"
PYTHONPATH=src .venv-rag/bin/python -m semi_rag build
```

### 2.2 企业栈和密钥

`infra/bin/stack` 会生成 `<APP_ROOT>/infra/secrets/app.env`，其中包含 OIDC、PostgreSQL、Redis 和指标配置。迁移时复制密钥文件，或在新机执行 `setup` 自动生成：

```text
session_secret
keycloak_client_secret
keycloak_admin_password
postgres_rag_password
postgres_keycloak_password
redis_password
rag_admin_password
rag_visitor_password
metrics_token
```

不要把密钥提交 Git。启动前设置公网地址：

```bash
export PUBLIC_ORIGIN=http://<PUBLIC_IP>:189
```

该地址必须同时出现在 `app.env`、`infra/config/keycloak.conf`、`infra/keycloak/realm-finance.json` 的 hostname、redirect URI 和 Nginx 路由配置中。

### 2.3 vLLM

vLLM 使用合并后的 Qwen SFT 权重，不能用 BGE 目录替代。迁移后设置：

```bash
export VLLM_PROJECT_DIR=<VLLM_ROOT>
export VLLM_PYTHON=<VLLM_VENV>/bin/python
export VLLM_MODEL_PATH=<LLM_MODEL_DIR>
export VLLM_API_KEY_FILE=<VLLM_API_KEY_FILE>
export VLLM_BIND_HOST=127.0.0.1
export VLLM_BIND_PORT=190
```

当前启动参数是 4 卡张量并行、BF16、最大上下文 262144、最大并发序列 64、GPU 利用率 0.85。显卡不足时调整 `CUDA_VISIBLE_DEVICES`、`--tensor-parallel-size`、`VLLM_MAX_MODEL_LEN` 和 `VLLM_MAX_NUM_SEQS`。

### 2.4 TTS、RSS 和数字人

`<TTS_ROOT>/run-finance-tts.sh` 使用 `Kokoro-82M-v1.1-zh`、CPU、普通话 `z`、端口 191；替换其中源码目录并设置 `HF_HOME=<TTS_CACHE>`。数字人只由 RAG 静态挂载，缺少 `<DIGITAL_HUMAN_DIST>` 不影响文本问答。

WeWe RSS 使用 Node `>=20.9`、pnpm `>=8.6.1`，生产变量为：

```text
NODE_ENV=production HOST=0.0.0.0 PORT=192
DATABASE_TYPE=sqlite DATABASE_URL=file:../data/wewe-rss.db
FEED_MODE=fulltext ENABLE_CLEAN_HTML=true
SERVER_ORIGIN_URL=http://<PUBLIC_IP>:189
```

### 2.5 模型加载方式

RAG 进程启动时由 `src/semi_rag/models.py` 使用 Transformers 的 `local_files_only=True` 加载 BGE-M3 Embedding 和 BGE Reranker，因此模型目录必须完整，不能依赖启动时联网下载。`src/semi_rag/answer.py` 使用 `llm_tokenizer` 加载本地 tokenizer，并通过 `llm_base_url/v1/chat/completions` 调用 vLLM，Qwen 权重本身只由 vLLM 进程加载。图表视觉转述、SFT 出题脚本也复用该 OpenAI 兼容端点。Kokoro 由 `app.engine.TTSEngine` 在 TTS 进程启动阶段加载模型缓存，首次启动可能较慢。

### 2.6 可选监控栈

项目提供 `deploy/prometheus/rag-alerts.yml` 和 `/metrics` 接口，但 Prometheus/Grafana 是项目外独立安装。迁移时把 Prometheus target 改为 `<PUBLIC_IP>:189`，使用 `Authorization: Bearer <RAG_METRICS_TOKEN>` 抓取，并将规则文件加入 `rule_files`。安装目录和数据目录使用 `<PROMETHEUS_ROOT>`、`<GRAFANA_ROOT>` 占位符，端口为 9090/8081，不影响核心服务启动。

## 3. 启动命令

### 1) 初始化基础设施（首次迁移或配置变更时执行）

```bash
cd <APP_ROOT>
sudo env PUBLIC_ORIGIN=http://<PUBLIC_IP>:189 infra/bin/stack setup \
  > infra/run/stack-setup-$(date +%Y%m%d-%H%M%S).log 2>&1
```

该命令会安装缺失的系统包、初始化 PostgreSQL、创建 `finance_rag`/`keycloak` 数据库、执行 migrations、安装/构建 Keycloak 并生成 `infra/secrets/app.env`，但不会切换 189 端口。日志：`infra/run/stack-setup-*.log` 和 `infra/postgres/logs/*.log`。

### 2) 启动 vLLM

```bash
cd <VLLM_ROOT>
export VLLM_PROJECT_DIR=<VLLM_ROOT> VLLM_PYTHON=<VLLM_VENV>/bin/python
export VLLM_MODEL_PATH=<LLM_MODEL_DIR> VLLM_API_KEY_FILE=<VLLM_API_KEY_FILE>
export VLLM_BIND_HOST=127.0.0.1 VLLM_BIND_PORT=190
nohup bash <VLLM_ROOT>/start_qwen3_vllm_api.sh \
  > <APP_ROOT>/infra/run/vllm.log 2>&1 &
echo $! > <APP_ROOT>/infra/run/vllm.pid
```

等待 vLLM 的 `/v1/models` 返回 `Qwen3.8-27B` 后再继续；这样 RAG 启动时可以直接完成模型健康检查。

### 3) 启动 PostgreSQL、Redis、Keycloak、RAG 和 Nginx

```bash
cd <APP_ROOT>
sudo env PUBLIC_ORIGIN=http://<PUBLIC_IP>:189 infra/bin/stack start \
  > infra/run/stack-start-$(date +%Y%m%d-%H%M%S).log 2>&1 &
echo $! > infra/run/stack-start.pid
```

该命令会启动 PostgreSQL、Redis、Keycloak、FastAPI `127.0.0.1:8090`，并在健康检查通过后启动 Nginx `:189`。日志：`infra/run/app.log`、`keycloak.log`、`redis/redis.log`、`nginx/logs/*.log`、`postgres/logs/*.log`。

### 4) 启动 Kokoro TTS

```bash
export TTS_ROOT=<TTS_ROOT>
nohup bash "$TTS_ROOT/run-finance-tts.sh" \
  > <APP_ROOT>/infra/run/tts.log 2>&1 &
echo $! > <APP_ROOT>/infra/run/tts.pid
```

### 5) 启动 WeWe RSS

```bash
cd <RSS_ROOT>
export NODE_ENV=production HOST=0.0.0.0 PORT=192
export DATABASE_TYPE=sqlite DATABASE_URL='file:../data/wewe-rss.db'
export FEED_MODE=fulltext ENABLE_CLEAN_HTML=true
export SERVER_ORIGIN_URL=http://<PUBLIC_IP>:189
pnpm install --frozen-lockfile
pnpm --filter server build
nohup pnpm --filter server start:prod \
  > <APP_ROOT>/infra/run/rss.log 2>&1 &
echo $! > <APP_ROOT>/infra/run/rss.pid
```

若 pnpm 不在 PATH，使用其绝对路径；若 RSS 只允许本机访问，将 `HOST=127.0.0.1`，并在 `app.env` 设置 `ARTICLE_FEED_SERVICE_URL=http://127.0.0.1:192`。

### 6) 可选：Vite 开发前端

生产页面已内嵌在 RAG FastAPI，生产不需要此步骤。仅修改 RSS Vue 前端时执行：

```bash
cd <RSS_ROOT>/web_ui
pnpm install --frozen-lockfile
nohup env VITE_API_BASE_URL=http://<PUBLIC_IP>:192 \
  pnpm dev --host 0.0.0.0 --port 3000 \
  > <APP_ROOT>/infra/run/rss-web-dev.log 2>&1 &
```

## 4. 健康检查

```bash
cd <APP_ROOT>
sudo infra/bin/stack status
ss -ltnp | grep -E ':(189|190|191|192|5432|6379|8090|8180|9000)\b'
/usr/lib/postgresql/16/bin/pg_isready -h 127.0.0.1 -p 5432
REDISCLI_AUTH="$(sudo cat infra/secrets/redis_password)" redis-cli -h 127.0.0.1 ping
curl --noproxy '*' -fsS http://127.0.0.1:9000/sso/health/ready
```

模型、TTS、RSS、RAG 和代理：

```bash
VLLM_KEY="$(cat <VLLM_API_KEY_FILE)"
curl --noproxy '*' -fsS -H "Authorization: Bearer $VLLM_KEY" http://127.0.0.1:190/v1/models
curl --noproxy '*' -fsS http://127.0.0.1:191/healthz
curl --noproxy '*' -fsS http://127.0.0.1:191/v1/audio/voices | head -c 300
curl --noproxy '*' -fsS http://127.0.0.1:192/
curl --noproxy '*' -i http://127.0.0.1:8090/health
curl --noproxy '*' -i http://<PUBLIC_IP>:189/proxy-health
curl --noproxy '*' -i http://<PUBLIC_IP>:189/health
```

预期：vLLM 模型 ID 为 `Qwen3.8-27B`；TTS、RSS、RAG 和两个 189 健康接口为 200；RAG JSON 包含 `status=ok`、索引 generation 和 `llm.model`。

浏览器验收：`/` 研究问答、`/chat` 通用问答、`/library` 文档库、`/agent` 金融 Agent、`/articles` 行业文章、`/digital-human/` 数字人、`/docs` OpenAPI。研究问答应返回 PDF/页码引用，通用问答应流式输出，上传任务应更新索引。

## 5. 停止、重启和回滚

```bash
cd <APP_ROOT>
sudo infra/bin/stack stop
kill "$(cat infra/run/vllm.pid 2>/dev/null)" 2>/dev/null || true
kill "$(cat infra/run/tts.pid 2>/dev/null)" 2>/dev/null || true
kill "$(cat infra/run/rss.pid 2>/dev/null)" 2>/dev/null || true
```

更新后重新执行 `sudo infra/bin/stack start`。只更新模型时先停 vLLM；只改前端静态文件时不需要重建 RAG 索引。回滚企业栈：

```bash
cd <APP_ROOT>
sudo infra/bin/stack rollback
```

回滚会停止新栈并恢复旧 FastAPI `0.0.0.0:189`。不要删除 PostgreSQL、Redis、索引或密钥目录。

## 6. 常见故障

### 端口冲突

```bash
ss -ltnp | grep -E ':(189|190|191|192|5432|6379|8090|8180|9000)\b'
pgrep -af 'semi_rag serve|vllm.entrypoints|keycloak|nginx|node dist/main|uvicorn app.server'
```

旧 RAG 直接监听 189 会阻止 Nginx；处理遗留进程后重试。

### RAG 503/退出

```bash
tail -100 <APP_ROOT>/infra/run/app.log
test -f <APP_ROOT>/data/rag/index/current/manifest.json
cat <APP_ROOT>/configs/rag.json
```

检查索引、BGE 路径、CUDA、`RAG_DATABASE_URL`、Redis 密码和 vLLM `/v1/models`。模型未就绪时先修复 vLLM 再重启 RAG。

### vLLM 加载失败

```bash
tail -120 <APP_ROOT>/infra/run/vllm.log
nvidia-smi
curl --noproxy '*' -H "Authorization: Bearer $(cat <VLLM_API_KEY_FILE>)" http://127.0.0.1:190/v1/models
```

检查模型目录、Key 文件权限、GPU 数量和张量并行参数；不要启动多个 vLLM 实例。

### Keycloak 登录失败

```bash
tail -120 <APP_ROOT>/infra/run/keycloak.log
grep -E 'RAG_PUBLIC_ORIGIN|RAG_OIDC|RAG_SESSION' <APP_ROOT>/infra/secrets/app.env
curl --noproxy '*' http://<PUBLIC_IP>:189/sso/realms/finance/.well-known/openid-configuration
```

确认 Keycloak 数据库密码、realm hostname 和 redirect URI 与 `<PUBLIC_ORIGIN>` 完全一致，Nginx `/sso/` 路由存在。

### Redis/任务队列异常

```bash
REDISCLI_AUTH="$(cat <APP_ROOT>/infra/secrets/redis_password)" redis-cli -h 127.0.0.1 ping
grep -E 'RAG_REDIS|RAG_TASK_BACKEND|RAG_WORKER_ID' <APP_ROOT>/infra/secrets/app.env
tail -100 <APP_ROOT>/infra/run/redis/redis.log
```

检查 Redis URL、stream/group、worker id 和数据目录权限。任务状态在 PostgreSQL，Redis 负责持久化队列。

### TTS/RSS 异常

```bash
tail -120 <APP_ROOT>/infra/run/tts.log
curl --noproxy '*' http://127.0.0.1:191/healthz
tail -120 <APP_ROOT>/infra/run/rss.log
curl --noproxy '*' -i http://127.0.0.1:192/
```

TTS 重点检查模型缓存和 PYTHONPATH；RSS 重点检查 SQLite、Prisma 生成、Node 依赖和 `DATABASE_URL` 相对路径。RSS 健康不代表外部公众号源一定可抓取。

### 本机请求被代理成 502

```bash
export NO_PROXY=127.0.0.1,localhost,<PUBLIC_IP>
export no_proxy="$NO_PROXY"
curl --noproxy '*' http://127.0.0.1:8090/health
```

当前环境存在 `http_proxy/https_proxy`；访问本机统一加 `--noproxy '*'`，代码 HTTP 客户端应保持 `trust_env=False`。

## 7. 最小验收清单

- [ ] `infra/bin/stack status` 显示 PostgreSQL、Redis、Keycloak、FastAPI、Nginx running。
- [ ] 189 的 `/proxy-health`、`/health` 和登录页可访问。
- [ ] OIDC discovery 可访问，管理员/访客登录权限正确。
- [ ] vLLM 返回 `Qwen3.8-27B`，TTS `/healthz` 为 200。
- [ ] RAG `/health` 为 200，索引 generation 与迁移前一致。
- [ ] `/search` 返回证据，`/ask` 返回 PDF 页码引用，`/chat` 能流式回答。
- [ ] `/articles` 能读取 RSS；上传/索引任务能在 PostgreSQL/Redis 中完成。
- [ ] `infra/run`、Nginx 和 PostgreSQL 日志可写并可用于故障定位。

