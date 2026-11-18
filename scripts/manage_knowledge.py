"""公共知识目录入库/撤回脚本（M3.2，issue #41）。

在仓库根目录用 backend 的 Python 运行：

    uv run --project backend python scripts/manage_knowledge.py publish
    uv run --project backend python scripts/manage_knowledge.py retract <source_id> <version>

publish 读取 knowledge/ 的审核目录（sources.json + cards/，sha256 校验不过会
整体拒绝），把新版本发布到 MongoDB 与独立的公共向量集合；重复运行幂等跳过
已发布版本。retract 先阻断查询再物理清理向量，可重复执行直至清理完成。
生产/staging 环境默认禁止，需显式设置 ALLOW_KNOWLEDGE_WRITE=1。
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pymongo.errors import PyMongoError  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.knowledge import (  # noqa: E402
    publish_catalog,
    retract_source,
)


def main() -> int:
    settings = get_settings()
    args = sys.argv[1:]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    command, rest = args[0], args[1:]
    if command == "publish":
        if rest:
            print("usage: manage_knowledge.py publish", file=sys.stderr)
            return 2
    elif command == "retract":
        if len(rest) != 2:
            print("usage: manage_knowledge.py retract <source_id> <version>", file=sys.stderr)
            return 2
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 2
    if settings.app_env not in {"local", "test"} and os.environ.get("ALLOW_KNOWLEDGE_WRITE") != "1":
        print(
            "Knowledge writes are only allowed in local/test; set ALLOW_KNOWLEDGE_WRITE=1 "
            "in staging/production to override.",
            file=sys.stderr,
        )
        return 1
    try:
        if command == "publish":
            report = asyncio.run(publish_catalog(settings))
            for key in report.published:
                print(f"published  {key}")
            for key in report.skipped:
                print(f"skipped    {key} (already present)")
            for key, error in report.failed.items():
                print(f"FAILED     {key}: {error}", file=sys.stderr)
            if report.failed:
                return 1
        else:
            source_id, version = rest
            asyncio.run(retract_source(settings, source_id=source_id, source_version=version))
            print(f"retracted  {source_id}@{version}")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except PyMongoError:
        print("Database operation failed. Check MongoDB and its indexes.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
