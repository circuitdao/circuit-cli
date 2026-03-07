#!/usr/bin/env python3
"""
Sync local Circuit backend with blockchain.

Usage: python sync_backend.py [options]

Default (no flags): sync both live state and block stats.
Use -l to sync live state only, -b to sync block stats only.
Use -s to wipe block stats tables and resync from scratch (local only).
"""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
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

LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")


def is_local_url(url: str) -> bool:
    return any(h in url for h in LOCAL_HOSTS)


def reset_block_stats(database_url: str) -> None:
    sql = (
        "TRUNCATE TABLE blockstatsv2, dailyblockstatsv2; "
        "TRUNCATE TABLE liveblockhash; "
        "DELETE FROM statslastheight WHERE id = 1;"
    )
    result = subprocess.run(
        ["psql", database_url, "-c", sql],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error resetting block stats tables:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("Block stats tables cleared (blockstatsv2, dailyblockstatsv2, liveblockhash, statslastheight).")


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
    total_ops = 0

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
            blocks_with_ops = result.get("blocks_with_ops")
            last_height = result.get("last_height")
            last_timestamp = result.get("last_timestamp")
            total_blocks += max(blocks, 0)
            total_ops += max(blocks_with_ops or 0, 0)

            def height_info() -> str:
                if last_height is None:
                    return ""
                parts = [f"height {last_height}"]
                if last_timestamp:
                    utc = datetime.fromtimestamp(last_timestamp, tz=timezone.utc)
                    parts.append(utc.strftime("%Y-%m-%d %H:%M UTC"))
                return " | " + ", ".join(parts)

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
                print(f"no more blocks to sync → complete for now{height_info()}")
                print(f"total blocks scanned: {total_blocks}, total with ops: {total_ops}")
                if continue_on_zero:
                    print(f"Sleeping {sleep_sec}s before next check")
                    await asyncio.sleep(sleep_sec)
                    continue
                else:
                    print(f"Fully synced {mode_label}. Exiting")
                    sys.exit(10)

            ops_info = f", {blocks_with_ops} with ops" if blocks_with_ops is not None else ""
            print(f"Success: {blocks} blocks scanned{ops_info} (total: {total_blocks}, {total_ops} with ops){height_info()} → running again immediately")
            # no sleep → fast loop during catch-up

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
        "-s",
        "--from-scratch",
        action="store_true",
        help="wipe block stats tables and resync from scratch (implies -b, local backend only)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="skip confirmation prompt when using -s",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_on_zero",
        action="store_true",
        help="wait for new blocks when synced instead of exiting",
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=DEFAULT_SLEEP,
        help=f"seconds to sleep when waiting for new blocks or error (default: {DEFAULT_SLEEP})",
    )
    args = parser.parse_args()

    if args.from_scratch and args.live:
        print("Error: -s/--from-scratch cannot be used with -l/--live (block stats only).", file=sys.stderr)
        sys.exit(1)

    if args.live:
        mode = MODE_LIVE
    elif args.blockstats or args.from_scratch:
        mode = MODE_STATS
    else:
        mode = MODE_BOTH

    base_url = os.environ.get("BASE_URL")
    if not base_url:
        print("Error: Environment variable BASE_URL is not set.", file=sys.stderr)
        print("Example: export BASE_URL=http://localhost:8000", file=sys.stderr)
        sys.exit(1)

    if args.from_scratch:
        if not is_local_url(base_url):
            print(
                f"Error: -s/--scratch is only allowed against a local backend.\n"
                f"  BASE_URL={base_url}\n"
                f"  Set BASE_URL to a local address (localhost / 127.0.0.1) before using -s.",
                file=sys.stderr,
            )
            sys.exit(1)

        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            print(
                "Error: DATABASE_URL is not set. Export it before using -s.\n"
                "  Example: export DATABASE_URL=postgresql://$(whoami)@localhost:5432/circuitdao",
                file=sys.stderr,
            )
            sys.exit(1)

        if not is_local_url(database_url):
            print(
                f"Error: -s/--from-scratch is only allowed against a local database.\n"
                f"  DATABASE_URL points to a non-local host. Aborting.",
                file=sys.stderr,
            )
            sys.exit(1)

        if not args.force:
            print("WARNING: This will wipe blockstatsv2, dailyblockstatsv2, liveblockhash and statslastheight.")
            print(f"  BASE_URL:     {base_url}")
            print(f"  DATABASE_URL: {database_url}")
            answer = input("Proceed? [y/N] ").strip().lower()
            if not answer in ["y", "Y", "yes", "YES", "Yes"]:
                print("Aborted.")
                sys.exit(0)

        reset_block_stats(database_url)

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
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        loop.run_until_complete(client.close())
        loop.close()


if __name__ == "__main__":
    main()
