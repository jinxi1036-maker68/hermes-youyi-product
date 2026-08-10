"""Run Xiaoyou read-only social platform market research."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from plugins.tuoguan_core.social_market_research import run_social_market_batch, run_social_market_research
from plugins.tuoguan_core.store import TuoguanStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Xiaoyou social market research runner.")
    parser.add_argument("--platform", choices=["xiaohongshu", "douyin"], default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--command", default="search")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--all", action="store_true", help="Run configured platforms and queries from social_market_research_config.json.")
    parser.add_argument("--queries", default="", help="Optional comma-separated queries for --all.")
    parser.add_argument("--platforms", default="", help="Optional comma-separated platforms for --all.")
    parser.add_argument("--sleep-between", action="store_true", help="Apply configured random delay between non-dry-run searches.")
    args = parser.parse_args(argv)
    if not args.all and not args.platform:
        parser.error("--platform is required unless --all is used")

    store = TuoguanStore(Path(args.data_dir)) if args.data_dir else TuoguanStore()
    if args.all:
        result = run_social_market_batch(
            store=store,
            dry_run=args.dry_run,
            platforms=[item.strip() for item in args.platforms.split(",") if item.strip()] if args.platforms else None,
            queries=[item.strip() for item in args.queries.split(",") if item.strip()] if args.queries else None,
            limit=args.limit,
            sleep_between=args.sleep_between,
        )
    else:
        result = run_social_market_research(
            args.platform,
            store=store,
            query=args.query,
            command=args.command,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
