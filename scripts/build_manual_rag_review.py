"""Build a deterministic, human-review-first RAG question set.

The generated JSONL is intentionally not a benchmark dataset.  A reviewer must
fill ``human_answerable``, ``evidence_ok``, ``approved_answer`` and
``approved_chunk_ids`` before it can be materialized for RAG evaluation.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


TASK_QUOTAS = {"qa_num": 16, "qa_logic": 14, "chart_qa": 10, "risk": 10}
TASK_ORDER = tuple(TASK_QUOTAS)


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def grams(text: str, size: int = 12) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    return {compact[i : i + size] for i in range(max(0, len(compact) - size + 1))}


def support_score(row: dict, chunk: dict, source_overlap: float) -> float:
    target = row.get("instruction", "") + " " + row.get("output", "")
    text = re.sub(r"\s+", "", chunk.get("text", ""))
    target_grams = grams(target, 3)
    text_grams = grams(text, 3)
    phrase_coverage = len(target_grams & text_grams) / max(1, len(target_grams))
    target_numbers = set(re.findall(r"\d+(?:\.\d+)?", target))
    number_coverage = sum(number in text for number in target_numbers) / max(1, len(target_numbers))
    return 3 * number_coverage + phrase_coverage + 0.1 * source_overlap


def matched_chunks(row: dict, by_doc: dict[str, list[dict]], by_id: dict[str, dict]) -> list[tuple[dict, float]]:
    query_grams = grams(row.get("input", ""))
    scored = []
    original = by_id.get(row.get("chunk_id"))
    if original is not None:
        # The SFT row records the exact chunk shown to the question-writing model.
        scored.append((original, 1.0))
    for chunk in by_doc.get(row["doc_id"], []):
        chunk_grams = grams(chunk.get("text", ""))
        if not chunk_grams:
            continue
        compact = re.sub(r"\s+", "", chunk.get("text", ""))
        if len(compact) < 80 or ("免责声明" in compact and len(compact) < 180):
            continue
        overlap = len(query_grams & chunk_grams)
        # SFT material can span several current index chunks, so the original
        # 35% overlap gate is too strict after re-chunking.  Keep a small gate
        # to exclude accidental matches, then rank by answer/entity support.
        threshold = max(12, 0.05 * len(chunk_grams))
        if overlap >= threshold and chunk.get("chunk_id") != row.get("chunk_id"):
            # Absolute overlap is used for ranking.  Ratio by chunk length would
            # incorrectly promote repeated 71-character PDF footer chunks.
            scored.append((chunk, overlap / max(1, len(query_grams))))
    return sorted(scored, key=lambda item: (
        -support_score(row, item[0], item[1]), -item[1],
        item[0].get("page", 10**9), item[0]["chunk_id"]))


def select_rows(candidates: list[dict], by_doc: dict[str, list[dict]], by_id: dict[str, dict], excluded_docs: set[str]) -> list[dict]:
    pools: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        if row.get("doc_id") in excluded_docs or row.get("task") not in TASK_QUOTAS:
            continue
        matches = matched_chunks(row, by_doc, by_id)
        if not matches:
            continue
        row = dict(row)
        row["_matches"] = matches
        pools[row["task"]].append(row)

    # Prefer document diversity first, then use a stable lexical tie-breaker.
    for task in TASK_ORDER:
        pools[task].sort(key=lambda row: (
            row["doc_id"], row.get("date", ""), row.get("instruction", "")))

    selected: list[dict] = []
    used_per_doc: defaultdict[str, int] = defaultdict(int)
    for task in TASK_ORDER:
        target = TASK_QUOTAS[task]
        pool = pools[task]
        # First pass: one question per document.
        for row in pool:
            if len([item for item in selected if item["task"] == task]) >= target:
                break
            if used_per_doc[row["doc_id"]] == 0:
                selected.append(row)
                used_per_doc[row["doc_id"]] += 1
        # Second pass: fill the quota, with at most four questions per document.
        for row in pool:
            if len([item for item in selected if item["task"] == task]) >= target:
                break
            if row in selected or used_per_doc[row["doc_id"]] >= 4:
                continue
            selected.append(row)
            used_per_doc[row["doc_id"]] += 1
        actual = len([item for item in selected if item["task"] == task])
        if actual != target:
            raise RuntimeError(f"Could only select {actual}/{target} questions for {task}")

    # Keep the task mix visible in the review file while giving each row a stable ID.
    selected.sort(key=lambda row: (TASK_ORDER.index(row["task"]), row["doc_id"], row["instruction"]))
    output = []
    for index, row in enumerate(selected, 1):
        primary, score = row["_matches"][0]
        evidence_candidates = [{
            "chunk_id": chunk["chunk_id"],
            "page": chunk.get("page"),
            "kind": chunk.get("kind", ""),
            "pdf_url": f"/sources/{chunk['chunk_id']}/pdf#page={chunk.get('page', 1)}",
            "evidence_text": chunk.get("text", ""),
        } for chunk, _ in row["_matches"][:3]]
        output.append({
            "id": f"manual50-{index:03d}",
            "query": row["instruction"].strip(),
            "task": row["task"],
            "candidate_answer": row.get("output", "").strip(),
            "source": {
                "doc_id": row["doc_id"],
                "title": primary.get("title", ""),
                "org": primary.get("org", ""),
                "publish_date": primary.get("publish_date", row.get("date", "")),
                "file": primary.get("file", ""),
                "page": primary.get("page"),
                "primary_chunk_id": primary["chunk_id"],
                "candidate_chunk_ids": [chunk["chunk_id"] for chunk, _ in row["_matches"]],
                "match_score": round(score, 4),
                "pdf_url": f"/sources/{primary['chunk_id']}/pdf#page={primary.get('page', 1)}",
                "evidence_text": primary.get("text", ""),
            },
            "evidence_candidates": evidence_candidates,
            "review": {
                "human_answerable": None,
                "evidence_ok": None,
                "approved_answer": "",
                "approved_chunk_ids": [],
                "review_notes": "",
            },
            "label_origin": "SFT validation question with automatically matched source passage; pending human review",
        })
    return output


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=root / "data/rag/eval/manual_review_50.jsonl")
    parser.add_argument("--markdown", type=Path, default=root / "data/rag/eval/manual_review_50.md")
    args = parser.parse_args()

    data = root / "data"
    val = json.loads((data / "sft/val.json").read_text(encoding="utf-8"))
    verified = read_jsonl(data / "sft/verified.jsonl")
    chunks = read_jsonl(data / "rag/index/current/chunks.jsonl")
    by_doc: dict[str, list[dict]] = defaultdict(list)
    by_id: dict[str, dict] = {}
    for chunk in chunks:
        by_doc[chunk["doc_id"]].append(chunk)
        by_id[chunk["chunk_id"]] = chunk

    existing = read_jsonl(data / "rag/eval/questions.jsonl") + read_jsonl(data / "rag/eval/retrieval-dev.jsonl")
    excluded_docs = {doc_id for row in existing for doc_id in row.get("gold_doc_ids", [])}
    val_keys = {(row.get("instruction", ""), row.get("input", "")) for row in val}
    candidates = [row for row in verified
                  if (row.get("instruction", ""), row.get("input", "")) in val_keys]
    rows = select_rows(candidates, by_doc, by_id, excluded_docs)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    lines = [
        "# 人工审核前置 RAG 题集（50 题）",
        "",
        "这不是可直接运行的 benchmark。请逐题打开 `source.file` 对应 PDF，核对 `source.page` 和 `source.evidence_text` 是否包含问题所需答案。",
        "",
        "审核后填写 JSONL 每行的 `review`：",
        "- `human_answerable`: 能否仅依据该研报可靠回答问题。",
        "- `evidence_ok`: 给出的页码/原文是否确实支持答案；不支持时填 `false`。",
        "- `approved_answer`: 人工确认后的最短答案，保留原文单位、时间和事实/预测口径。",
        "- `approved_chunk_ids`: 支持答案的 chunk ID；可从 `source.candidate_chunk_ids` 选择或补充。",
        "- `review_notes`: 记录数字、公司、日期、单位、预测/事实等核对结果。",
        "",
        "审核通过后运行：",
        "```bash",
        "python scripts/materialize_manual_rag.py \\",
        "  --input data/rag/eval/manual_review_50.jsonl \\",
        "  --output data/rag/eval/manual_approved.jsonl",
        "```",
        "",
        f"题目数量：{len(rows)}；任务分布：" + ", ".join(
            f"{task}={sum(row['task'] == task for row in rows)}" for task in TASK_ORDER),
        "",
    ]
    for row in rows:
        source = row["source"]
        lines.extend([
            f"## {row['id']} [{row['task']}]",
            f"**问题**：{row['query']}",
            f"**候选答案**：{row['candidate_answer']}",
            f"**来源**：{source['org']}｜{source['publish_date']}｜第 {source['page']} 页｜`{source['file']}`",
            "**候选证据**：",
        ])
        for candidate in row["evidence_candidates"]:
            excerpt = re.sub(r"\s+", " ", candidate["evidence_text"]).strip()
            if len(excerpt) > 900:
                excerpt = excerpt[:900] + "..."
            lines.append(f"- 第 {candidate['page']} 页，`{candidate['chunk_id']}`：{excerpt}")
        lines.extend([
            "**审核**：`human_answerable`: ☐ true ☐ false；`evidence_ok`: ☐ true ☐ false；备注：",
            "",
        ])
    args.markdown.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"questions": len(rows), "output": str(args.output), "markdown": str(args.markdown)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
