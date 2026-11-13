"""Compare the M2 splitter with opt-in M3 structure policies on fixed synthetic data.

Default lexical mode does not invoke a model. --mode embedding calls the configured
real embedding provider, compares vectors in memory and never modifies Qdrant.
The small development set is a structural diagnostic, not a held-out quality score.
"""

import argparse
import asyncio
import hashlib
import json
import math
import re
import statistics
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.rag.bm25 import Bm25Encoder  # noqa: E402
from app.rag.chunking import chunk_document  # noqa: E402
from app.rag.embeddings import get_embedding_provider  # noqa: E402
from app.rag.structured_chunking import ChunkPolicy, normalize_source, split_structured  # noqa: E402

DATA = ROOT / "backend/tests/data/m3_chunking"


def cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    scale = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return dot / scale if scale else 0.0


def load_data():
    spec = json.loads((DATA / "questions.json").read_text(encoding="utf-8"))
    docs = {}
    for source in spec["sources"]:
        raw = (DATA / source["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("Synthetic source changed; review its annotations and fingerprint")
        docs[source["path"]] = {**source, "text": normalize_source(raw.decode("utf-8"))}
    for question in spec["questions"]:
        if docs[question["document"]]["text"].count(question["anchor"]) != 1:
            raise ValueError("Ambiguous annotation")
    return spec, docs


def build_chunks(docs, size, structured):
    rows = []
    for name, document in docs.items():
        text = document["text"]
        if structured:
            chunks = split_structured(
                text, source_id=name, source_version=document["sha256"], policy=ChunkPolicy(size)
            )
            for chunk in chunks:
                rows.append(
                    {
                        **asdict(chunk),
                        "document": name,
                        "role": document["role"],
                        "embedding_text": chunk.embedding_text,
                    }
                )
        else:
            lines = text.splitlines(keepends=True)
            offsets = [0]
            for line in lines:
                offsets.append(offsets[-1] + len(line))
            for chunk in chunk_document(text.splitlines(), max_chars=size):
                start = offsets[chunk.locator.line_start - 1]
                rows.append(
                    {
                        "document": name,
                        "role": document["role"],
                        "text": chunk.text,
                        "char_start": start,
                        "char_end": start + len(chunk.text),
                        "embedding_text": chunk.text,
                    }
                )
    return rows


def structure_metrics(rows, docs, target):
    exact = all(
        row["text"] == docs[row["document"]]["text"][row["char_start"] : row["char_end"]]
        for row in rows
    )
    covered = total = atoms = intact = 0
    for name, document in docs.items():
        text = document["text"]
        spans = [row for row in rows if row["document"] == name]
        covered_positions = set()
        for row in spans:
            covered_positions.update(range(row["char_start"], row["char_end"]))
        covered += sum(i in covered_positions for i, char in enumerate(text) if not char.isspace())
        total += sum(not char.isspace() for char in text)
        patterns = [
            r"(?ms)^```[^\n]*\n.*?^```[ \t]*$",
            r"(?m)^\|[^\n]*\n\|[ \-:|]+\n(?:\|[^\n]*(?:\n|$))+",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                atoms += 1
                intact += any(
                    row["char_start"] <= match.start() and row["char_end"] >= match.end()
                    for row in spans
                )
    lengths = sorted(len(row["text"]) for row in rows)
    return {
        "chunks": len(rows),
        "exact_quotes": exact,
        "non_whitespace_coverage": covered / total,
        "atomic_blocks": atoms,
        "atomic_blocks_intact": intact,
        "target_chars": target,
        "p50_chars": statistics.median(lengths),
        "max_chars": max(lengths),
        "above_target": sum(n > target for n in lengths),
        "hard_limit_violations": sum(n > 6000 for n in lengths),
    }


async def evaluate(mode):
    spec, docs = load_data()
    provider = get_embedding_provider(Settings()) if mode == "embedding" else None
    query_vectors = (
        await provider.embed_texts([q["query"] for q in spec["questions"]]) if provider else None
    )
    report = {
        "split": spec["split"],
        "mode": mode,
        "questions": len(spec["questions"]),
        "source_hashes": spec["sources"],
        "limitations": (
            "Three synthetic development documents; recall means a labeled source span is present. "
            "No generation, production Qdrant, held-out testing "
            "or overall model accuracy is measured."
        ),
        "policies": {},
    }
    if provider:
        report["embedding"] = {
            "configured_model": provider.model_version,
            "dimension": provider.dimension,
        }
    for label, size, structured in [
        ("m2-paragraph-600", 600, False),
        ("m3-structure-300", 300, True),
        ("m3-structure-600", 600, True),
        ("m3-structure-900", 900, True),
    ]:
        rows = build_chunks(docs, size, structured)
        vectors = (
            await provider.embed_texts([r["embedding_text"] for r in rows]) if provider else None
        )
        encoder = Bm25Encoder.fit([r["embedding_text"] for r in rows])
        details = []
        for qi, question in enumerate(spec["questions"]):
            encoded = encoder.encode_query(question["query"])
            query_weights = dict(zip(encoded.indices, encoded.values, strict=True))
            ranked = []
            for i, row in enumerate(rows):
                if row["role"] != question["role"]:
                    continue
                if vectors:
                    score = cosine(query_vectors[qi], vectors[i])
                else:
                    vector = encoder.encode_document(row["embedding_text"])
                    score = sum(
                        query_weights.get(index, 0) * value
                        for index, value in zip(vector.indices, vector.values, strict=True)
                    )
                ranked.append((score, i, row))
            ranked.sort(key=lambda item: (-item[0], item[1]))
            anchor_start = docs[question["document"]]["text"].index(question["anchor"])
            anchor_end = anchor_start + len(question["anchor"])
            ranks = [
                rank
                for rank, (_, _, row) in enumerate(ranked, 1)
                if row["document"] == question["document"]
                and row["char_start"] <= anchor_start
                and row["char_end"] >= anchor_end
            ]
            rank = min(ranks) if ranks else None
            details.append(
                {
                    "question_id": question["id"],
                    "role": question["role"],
                    "first_relevant_rank": rank,
                    "top3": [
                        {
                            "document": row["document"],
                            "start": row["char_start"],
                            "end": row["char_end"],
                        }
                        for _, _, row in ranked[:3]
                    ],
                }
            )
        metrics = structure_metrics(rows, docs, size)
        for k in (1, 3):
            metrics[f"span_recall_at_{k}"] = sum(
                d["first_relevant_rank"] is not None and d["first_relevant_rank"] <= k
                for d in details
            ) / len(details)
        metrics["mrr"] = sum(
            1 / d["first_relevant_rank"] if d["first_relevant_rank"] else 0 for d in details
        ) / len(details)
        report["policies"][label] = {"metrics": metrics, "questions": details}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["lexical", "embedding"], default="lexical")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/m3-chunking-lexical.json")
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.mode))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v["metrics"] for k, v in report["policies"].items()}, ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
