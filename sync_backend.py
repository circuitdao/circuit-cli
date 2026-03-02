#!/usr/bin/env python3
"""
Sync local Circuit backend with blockchain.

Usage: python sync_backend.py [options]

Default (no flags): sync both live state and block stats.
Use -l to sync live state only, -b to sync block stats only.
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict

from circuit_cli.client import CircuitRPCClient


# ── Silence non-warning/error logs ───────────────────────────────────────
logging.getLogger().setLevel(logging.WARNING)

loggers_to_quiet = [
    "asyncio",
    "httpx",
    "httpcore",
    "anyio",
    "circuit_cli",
]

for name in loggers_to_quiet:
    logger = logging.getLogger(name)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    logger.handlers.clear()

# If you discover more noisy loggers later, add their names here
# ─────────────────────────────────────────────────────────────────────────

DEFAULT_SLEEP = 30
MODE_BOTH = "both"
MODE_LIVE = "live"
MODE_STATS = "stats"


async def call_sync(client: CircuitRPCClient, mode: str) -> Dict[str, Any]:
    """Call the appropriate RPC sync endpoint(s) based on mode.

    Returns a dict with keys:
      status        "done" | "error" | "skipped"
      blocks_synced total blocks processed across all endpoints called
      message       error message (only on error)
    """
    try:
        if mode == MODE_LIVE:
            return dict(await client.upkeep_rpc_sync(live=True))
        elif mode == MODE_STATS:
            return dict(await client.upkeep_rpc_sync(blockstats=True))
        else:  # MODE_BOTH
            return dict(await client.upkeep_rpc_sync())
    except Exception as exc:
        raise RuntimeError(f"RPC call failed: {exc}") from exc


async def sync_loop(client: CircuitRPCClient, sleep_sec: int, continue_on_zero: bool, mode: str):
    """Main async loop that keeps calling the sync endpoint(s)."""
    mode_label = {"both": "live state + block stats", "live": "live state", "stats": "block stats"}[mode]
    total_blocks = 0

    if mode == MODE_BOTH:
        endpoints = "/sync_chain_data + /sync_block_stats"
    elif mode == MODE_LIVE:
        endpoints = "/sync_chain_data"
    else:
        endpoints = "/sync_block_stats"

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] POST {endpoints}")

        try:
            result = await call_sync(client, mode)

            status = result.get("status", "missing")
            blocks = result.get("blocks_synced", -1)
            total_blocks += max(blocks, 0)

            if status == "error":
                msg = result.get("message", "No message")
                print(f"RPC error: {msg}")
                print(f"Sleeping {sleep_sec}s...")
                await asyncio.sleep(sleep_sec)
                continue

            if status == "skipped":
                print(f"Sync skipped (another sync in progress). Sleeping {sleep_sec}s...")
                await asyncio.sleep(sleep_sec)
                continue

            if status != "done":
                print(f"Unexpected status: {status}")
                print(f"Sleeping {sleep_sec}s...")
                await asyncio.sleep(sleep_sec)
                continue

            if blocks == 0:
                print(f"no more blocks to sync → complete for now")
                print(f"total blocks synced: {total_blocks}")
                if continue_on_zero:
                    print(f"Sleeping {sleep_sec}s before next check")
                    await asyncio.sleep(sleep_sec)
                    continue
                else:
                    print(f"Fully synced {mode_label}. Exiting")
                    sys.exit(10)

            print(f"Success: {blocks} blocks synced (total: {total_blocks}) → running again immediately")
            # no sleep → fast loop during catch-up

        except KeyboardInterrupt:
            print("\nStopped by user.")
            sys.exit(0)

        except Exception as exc:
            print(f"Error during sync: {exc}")
            print(f"Sleeping {sleep_sec}s...")
            await asyncio.sleep(sleep_sec)


def main():
    parser = argparse.ArgumentParser(
        description="Sync Circuit backend with Chia blockchain.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Default (no flags): sync both live state and block stats.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-l",
        "--live",
        action="store_true",
        help="sync live state only (coin tables, statutes cache)",
    )
    mode_group.add_argument(
        "-b",
        "--blockstats",
        action="store_true",
        help="sync block stats only (BlockStatsV2/DailyBlockStatsV2)",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_on_zero",
        action="store_true",
        help="wait for new blocks when synced instead of exiting",
    )
    parser.add_argument(
        "-s",
        "--sleep",
        type=int,
        default=DEFAULT_SLEEP,
        help=f"seconds to sleep when waiting for new blocks or error (default: {DEFAULT_SLEEP})",
    )
    args = parser.parse_args()

    if args.live:
        mode = MODE_LIVE
    elif args.blockstats:
        mode = MODE_STATS
    else:
        mode = MODE_BOTH

    base_url = os.environ.get("BASE_URL")
    if not base_url:
        print("Error: Environment variable BASE_URL is not set.", file=sys.stderr)
        print("Example: export BASE_URL=http://localhost:8000", file=sys.stderr)
        sys.exit(1)

    mode_label = {"both": "live state + block stats", "live": "live state", "stats": "block stats"}[mode]
    print("CircuitDAO RPC sync loop started")
    print(f"  Base URL: {base_url}")
    print(f"  Mode: {mode_label}")
    print(f"  Sleep on failure: {args.sleep}s")
    if args.continue_on_zero:
        print(f"  Wait for new blocks when synced (sleep for {args.sleep}s)")
    else:
        print(f"  Exit when synced")
    print("Press Ctrl+C to stop")
    print("-" * 40)

    # Create client once
    client = CircuitRPCClient(base_url, None)
    # Re-silence circuit_cli after CircuitRPCClient's constructor may have re-enabled it
    logging.getLogger("circuit_cli").setLevel(logging.ERROR)
    logging.getLogger("circuit_cli").propagate = False

    # Create one event loop and run the async loop forever
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(sync_loop(client, args.sleep, args.continue_on_zero, mode))
    finally:
        # Clean shutdown
        loop.run_until_complete(client.close())
        loop.close()


if __name__ == "__main__":
    main()
