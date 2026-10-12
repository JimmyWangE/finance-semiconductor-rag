"""Convert human-approved review rows into the RAG retrieval benchmark format."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = read_jsonl(args.input)
    approved = []
    rejected = []
    for row in rows:
        review = row.get("review", {})
        ok = (review.get("human_answerable") is True
              and review.get("evidence_ok") is True
              and isinstance(review.get("approved_answer"), str)
              and bool(review["approved_answer"].strip())
              and isinstance(review.get("approved_chunk_ids"), list)
              and all(isinstance(value, str) and value for value in review["approved_chunk_ids"]))
        if not ok:
            rejected.append(row["id"])
            continue
        source = row["source"]
        approved.append({
            "id": row["id"],
            "query": row["query"],
            "task": row["task"],
            "gold_doc_ids": [source["doc_id"]],
            "gold_chunk_ids": list(dict.fromkeys(review["approved_chunk_ids"])),
            "reference_answer": review["approved_answer"].strip(),
            "expected_answerable": True,
            "expected_all": [],
            "expected_any": [],
            "forbidden": [],
            "filters": {},
            "source_date": source["publish_date"],
            "label_origin": "human_reviewed_manual50",
            "review_notes": review.get("review_notes", ""),
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        for row in approved:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"approved": len(approved), "pending_or_rejected": len(rejected),
                      "rejected_ids": rejected}, ensure_ascii=False))


if __name__ == "__main__":
    main()
