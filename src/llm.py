"""本地 vLLM 客户端的共用封装。

抽出来是因为 caption_figures.py 和 SFT 生成脚本都要用同一套东西：
  - 绕过 http_proxy（本机代理会把 localhost 请求也劫持，导致 502）
  - 熔断（PPU 的 kernel 会偶发把 EngineCore 打死，引擎一死就得停止投递）
  - 断点续跑
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from utils import get_logger

log = get_logger("llm")

DEFAULT_BASE = os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:190/v1")
DEFAULT_MODEL = os.environ.get("VLLM_MODEL", "Qwen3.8-27B")
KEY_FILE = Path(os.environ.get("VLLM_API_KEY_FILE", ""))

BREAKER_THRESHOLD = 12


def load_key() -> str:
    if KEY_FILE.is_file():
        return KEY_FILE.read_text().strip()
    return os.environ.get("VLLM_API_KEY", "EMPTY")


class Breaker:
    """连续 N 次连接级失败即判定引擎已死，停止投递剩余请求。

    没有这个的话，引擎一崩就会把整个任务列表在几十秒内烧光并全部标记失败
    （实测损失过 1311 条）。
    """

    def __init__(self, threshold: int = BREAKER_THRESHOLD) -> None:
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


def make_client(base_url: str = DEFAULT_BASE, timeout: float = 300.0):
    """返回 (AsyncOpenAI, 需要关闭的 http_client)。

    trust_env=False 是关键：本机设了 http_proxy=127.0.0.1:17890，
    httpx 默认会读环境变量，把发往本机 vLLM 的请求也代理掉，结果 502。
    """
    import httpx
    from openai import AsyncOpenAI

    http_client = httpx.AsyncClient(trust_env=False, timeout=timeout)
    client = AsyncOpenAI(base_url=base_url, api_key=load_key(), timeout=timeout,
                         max_retries=0, http_client=http_client)
    return client, http_client


async def chat(client, model: str, messages: list[dict], br: Breaker,
               max_tokens: int = 2048, temperature: float = 0.7,
               thinking: bool = False, retries: int = 2) -> str | None:
    """一次对话调用。返回文本；失败返回 None（并记入熔断计数）。"""
    if br.tripped:
        return None
    for attempt in range(retries + 1):
        if br.tripped:
            return None
        try:
            r = await client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                max_tokens=max_tokens,
                extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
            )
            br.record(True)
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            if attempt == retries:
                br.record(False)
                log.debug("调用失败: %s", str(e)[:150])
                return None
            await asyncio.sleep(2 * (attempt + 1))
    return None


def extract_json(text: str) -> Any | None:
    """从模型输出里抠出 JSON。模型常会包 ```json 或前后加解释文字。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t[3:]
        t = t.removeprefix("json").removeprefix("JSON").strip()
    for lo, hi in (("[", "]"), ("{", "}")):
        i, j = t.find(lo), t.rfind(hi)
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


async def run_pool(tasks: Iterable[Any], worker: Callable[[Any], Awaitable[dict]],
                   out_path: Path, conc: int, br: Breaker,
                   log_every: int = 25) -> tuple[int, int]:
    """并发跑一批任务，逐条落盘（保证崩溃后能续跑）。返回 (成功, 失败)。"""
    tasks = list(tasks)
    sem = asyncio.Semaphore(conc)

    async def guarded(t: Any) -> dict:
        async with sem:
            return await worker(t)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = bad = 0
    with out_path.open("a", encoding="utf-8") as f:
        for fut in asyncio.as_completed([guarded(t) for t in tasks]):
            r = await fut
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            ok += bool(r.get("ok"))
            bad += not bool(r.get("ok"))
            if (ok + bad) % log_every == 0:
                log.info("  %d/%d  成功%d 失败%d", ok + bad, len(tasks), ok, bad)
    if br.tripped:
        log.error("熔断触发：连续 %d 次连接失败，判定 vLLM 引擎已死。"
                  "重启服务后重跑同一命令即可续跑。", br.threshold)
    return ok, bad
