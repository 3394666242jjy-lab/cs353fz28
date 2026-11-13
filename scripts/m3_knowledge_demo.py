"""Inspect the M3 role-filtered source catalog and reference-only context prototype."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.knowledge_policy import compose_context, route_retrieval  # noqa: E402
from app.rag.knowledge import load_catalog, retrieve_local_knowledge  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        choices=["ai_product_manager", "fullstack", "agent_engineer"],
        default="agent_engineer",
    )
    parser.add_argument("--query", default="checkpoint 恢复与幂等")
    parser.add_argument("--catalog", action="store_true")
    args = parser.parse_args()
    if args.catalog:
        records = [
            {"id": source.id, "title": source.title, "roles": source.roles, "url": source.url}
            for source, _ in load_catalog(ROOT / "knowledge")
            if args.role in source.roles
        ]
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return
    plan = route_retrieval(args.role, "learn_topic")
    references = retrieve_local_knowledge(ROOT / "knowledge", role=args.role, query=args.query)
    context = compose_context(plan, personal=[], knowledge=references)
    print(context.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
