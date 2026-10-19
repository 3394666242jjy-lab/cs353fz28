"""Verify real local infrastructure without echoing connection strings."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.services.readiness import check_dependencies  # noqa: E402


async def main(attempts: int) -> int:
    for attempt in range(attempts):
        statuses = await check_dependencies(get_settings())
        print(json.dumps(statuses), flush=True)
        if all(status == "ok" for status in statuses.values()):
            return 0
        if attempt + 1 < attempts:
            await asyncio.sleep(1)
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, choices=range(1, 31), default=1)
    raise SystemExit(asyncio.run(main(parser.parse_args().attempts)))
