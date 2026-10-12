"""Step 2 — 图表转述：用本地 Qwen3.8-27B（多模态）把研报里的图表转成文字。

为什么必须做：triage 标出 140 份文档文字密度 <600 字符/页或图密度 >3 张/页，
核心结论画在图里，纯文本抽取拿不到。

做法：把图 + 该页上下文一起喂给 VLM，让它输出结构化中文转述。
给上下文很关键 —— ���看一张柱状图模型不知道纵轴单位是"亿元"还是"万片/月"，
page 正文里通常写着。

产出 data/captions/<doc_id>.jsonl（可断点续跑，重跑只补缺失的图）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path

from PIL import Image

from utils import DATA, get_logger, read_jsonl

log = get_logger("caption")

DEFAULT_BASE = os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:190/v1")
DEFAULT_MODEL = os.environ.get("VLLM_MODEL", "Qwen3.8-27B")
KEY_FILE = Path(os.environ.get("VLLM_API_KEY_FILE", ""))

MAX_SIDE = 1024          # 限制单图 token 消耗；PPU 视觉编码器对大图+高并发很敏感
MIN_SIDE = 160           # 再小基本是图标
CTX_CHARS = 1200         # 喂给模型的同页上下文长度

# 熔断：PPU 的多模态 kernel 在高并发下会让 EngineCore 硬崩（实测 concurrency 16 崩、3 稳定）。
# 引擎一死，后续每个请求都是秒失败，不熔断就会把整个任务列表烧光且全部标记失败。
BREAKER_THRESHOLD = 12

SYSTEM = (
    "你是半导体行业金融研报的图表分析助手。用户会给你一张券商研报里的图表，"
    "以及该图所在页面的正文上下文。请把图表内容转述成中文文字。"
)

PROMPT = """请转述这张研报图表，严格按以下格式输出：

【类型】柱状图/折线图/饼图/表格/流程图/架构图/示意图 之一
【主题】这张图在讲什么（一句话）
【坐标轴】横轴是什么、纵轴是什么及单位；无坐标轴则写"无"
【关键数据】逐条列出图中可读出的具体数值（系列名、时间点、数值）。看不清的写"不可读"
【结论】这张图支持的核心判断（一到两句）

硬性要求：
1. 只转述图中真实可见的内容。数值看不清就写"不可读"，绝对不要根据上下文推测或编造数字。
2. 如果这张图是公司logo、装饰图案、页面分割线、二维码，只输出一行：【无效图】
3. 不要输出格式之外的任何解释。

该图所在页面的正文上下文（仅供理解术语和单位，不要把上下文内容当成图里的数据）：
---
{ctx}
---"""


def load_key() -> str:
    if KEY_FILE.is_file():
        return KEY_FILE.read_text().strip()
    return os.environ.get("VLLM_API_KEY", "EMPTY")


def encode_image(path: Path) -> tuple[str, str] | None:
    """缩放并转 base64；同时返回像素哈希用于跨页去重（logo 在每页都出现）。"""
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    if min(im.size) < MIN_SIDE:
        return None
    if max(im.size) > MAX_SIDE:
        s = MAX_SIDE / max(im.size)
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    phash = hashlib.blake2b(im.resize((32, 32)).tobytes(), digest_size=8).hexdigest()
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode(), phash


def page_context(pages: list[dict], pageno: int) -> str:
    lines: list[str] = []
    for p in pages:
        if abs(p["page"] - pageno) <= 0:
            lines += p["lines"]
    return "\n".join(lines)[:CTX_CHARS] or "（该页无正文）"


def build_tasks(doc_json: Path, done: set[str], seen_hash: dict[str, str]) -> list[dict]:
    rec = json.loads(doc_json.read_text(encoding="utf-8"))
    tasks = []
    for fig in rec.get("figures", []):
        f = Path(fig["file"])
        if fig["file"] in done or not f.is_file():
            continue
        enc = encode_image(f)
        if enc is None:
            continue
        b64, phash = enc
        if phash in seen_hash:      # 同一张 logo/水印在多页重复，只转述一次
            continue
        seen_hash[phash] = fig["file"]
        tasks.append({
            "doc_id": rec["doc_id"], "file": fig["file"], "page": fig["page"],
            "pdf_caption": fig.get("caption", ""), "b64": b64,
            "ctx": page_context(rec["pages"], fig["page"]),
        })
    return tasks


class Breaker:
    """连续 N 次连接级失败即判定引擎已死，停止投递剩余请求。"""

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.consecutive = 0
        self.tripped = False

    def record(self, ok: bool) -> None:
        if ok:
            self.consecutive = 0
        else:
            self.consecutive += 1
            if self.consecutive >= self.threshold:
                self.tripped = True


async def caption_one(client, model: str, t: dict, sem: asyncio.Semaphore,
                      br: Breaker, retries: int = 2) -> dict:
    base = {k: t[k] for k in ("doc_id", "file", "page", "pdf_caption")}
    if br.tripped:
        return base | {"ok": False, "error": "skipped: breaker tripped"}
    msg = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{t['b64']}"}},
            {"type": "text", "text": PROMPT.format(ctx=t["ctx"])},
        ]},
    ]
    async with sem:
        for attempt in range(retries + 1):
            if br.tripped:
                return base | {"ok": False, "error": "skipped: breaker tripped"}
            try:
                r = await client.chat.completions.create(
                    model=model, messages=msg, temperature=0.1, max_tokens=800,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                txt = (r.choices[0].message.content or "").strip()
                br.record(True)
                return base | {"vlm_text": txt, "invalid": "【无效图】" in txt, "ok": True}
            except Exception as e:
                if attempt == retries:
                    br.record(False)
                    return base | {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
                await asyncio.sleep(2 * (attempt + 1))
    return base | {"ok": False, "error": "unreachable"}


async def run(tasks: list[dict], out: Path, model: str, base: str, conc: int) -> None:
    import httpx
    from openai import AsyncOpenAI

    # 本机设了 http_proxy=127.0.0.1:17890，会把发往 localhost 的请求也代理掉导致 502。
    # trust_env=False 让 httpx 忽略所有代理环境变量，直连 vLLM。
    http_client = httpx.AsyncClient(trust_env=False, timeout=180.0)
    client = AsyncOpenAI(base_url=base, api_key=load_key(), timeout=180.0,
                         max_retries=0, http_client=http_client)
    sem = asyncio.Semaphore(conc)
    br = Breaker(BREAKER_THRESHOLD)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = ok = bad = invalid = 0
    try:
        with out.open("a", encoding="utf-8") as f:
            for fut in asyncio.as_completed(
                    [caption_one(client, model, t, sem, br) for t in tasks]):
                r = await fut
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                done += 1
                ok += r.get("ok", False)
                bad += not r.get("ok", False)
                invalid += r.get("invalid", False)
                if done % 25 == 0:
                    log.info("  %d/%d  成功%d 失败%d 无效图%d", done, len(tasks), ok, bad, invalid)
    finally:
        await http_client.aclose()
    if br.tripped:
        log.error("熔断触发：连续 %d 次连接失败，判定 vLLM 引擎已死。"
                  "检查 /tmp/vllm_server.log，重启服务后再跑一次即可续跑。", BREAKER_THRESHOLD)
    log.info("完成 %d：成功 %d，失败 %d，判定为无效图 %d", done, ok, bad, invalid)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DATA / "raw_md")
    ap.add_argument("--out", type=Path, default=DATA / "captions" / "captions.jsonl")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=4,
                    help="PPU 多模态 kernel 在高并发下会崩（实测 16 崩、3~4 稳），别轻易调高")
    ap.add_argument("--only-needs-vlm", action="store_true",
                    help="只跑 triage 标记图表密集的 140 份（推荐先这么试）")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    keep: set[str] | None = None
    if a.only_needs_vlm:
        keep = {r["doc_id"] for r in read_jsonl(DATA / "report" / "triage.jsonl")
                if r["ok"] and r.get("needs_vlm")}
        log.info("限定 needs_vlm 子集：%d 份文档", len(keep))

    done: set[str] = set()
    if a.out.is_file():
        done = {r["file"] for r in read_jsonl(a.out) if r.get("ok")}
        log.info("断点续跑：已完成 %d 张", len(done))

    seen_hash: dict[str, str] = {}
    tasks: list[dict] = []
    for j in sorted(a.src.glob("*.json")):
        if keep is not None and j.stem not in keep:
            continue
        tasks += build_tasks(j, done, seen_hash)
    if a.limit:
        tasks = tasks[: a.limit]

    log.info("待转述图表 %d 张（已按像素哈希跨页去重）", len(tasks))
    if not tasks:
        return
    asyncio.run(run(tasks, a.out, a.model, a.base_url, a.concurrency))
    log.info("写出 -> %s", a.out)


if __name__ == "__main__":
    main()
