#!/usr/bin/env python3
"""
Sync local Circuit backend with blockchain.

Usage: python sync_backend.py [-e]

Specify -e option to exit once sync completed. Otherwise loop will continue to run.
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


async def call_upkeep_sync(client: CircuitRPCClient, blockstats: bool) -> Dict[str, Any]:
    """Call the appropriate RPC sync method based on blockstats toggle."""
    try:
        if blockstats:
            result = await client.upkeep_rpc_sync_block_stats()
        else:
            result = await client.upkeep_rpc_sync()
        return dict(result)
    except Exception as exc:
        raise RuntimeError(f"RPC call failed: {exc}") from exc


async def sync_loop(client: CircuitRPCClient, sleep_sec: int, continue_on_zero: bool, blockstats: bool):
    """Main async loop that keeps calling the sync method."""
    total_blocks = 0
    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        endpoint = "/sync_block_stats" if blockstats else "/sync_chain_data"
        print(f"\n[{now}] POST {endpoint}")

        try:
            result = await call_upkeep_sync(client, blockstats)
            # print("Response:", json.dumps(result, indent=2))

            status = result.get("status", "missing")
            blocks = result.get("blocks_synced", -1)
            total_blocks += blocks

            if status == "error":
                msg = result.get("message", "No message")
                print(f"RPC error: {msg}")
                print(f"Sleeping {sleep_sec}s...")
                await asyncio.sleep(sleep_sec)
                continue

            if status != "done":
                print(f"Unexpected status: {status}")
                print(f"Sleeping {sleep_sec}s...")
                await asyncio.sleep(sleep_sec)
                continue

            if blocks == 0:
                print("blocks_synced = 0 → sync appears complete for now")
                print(f"total blocks synced: {total_blocks}")
                if continue_on_zero:
                    print(f"Sleeping {sleep_sec}s before next check")
                    await asyncio.sleep(sleep_sec)
                    continue
                else:
                    print("Fully syned. Exiting")
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
    parser = argparse.ArgumentParser(description="Sync Circuit backend with Chia blockchain.")
    parser.add_argument(
        "-b",
        "--blockstats",
        action="store_true",
        help="sync block stats only",
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

    base_url = os.environ.get("BASE_URL")
    if not base_url:
        print("Error: Environment variable BASE_URL is not set.", file=sys.stderr)
        print("Example: export BASE_URL=http://localhost:8000", file=sys.stderr)
        sys.exit(1)

    print("CircuitDAO RPC sync loop started")
    print(f"  Base URL: {base_url}")
    print("  Mode: " + ("sync all" if not args.blockstats else "sync block stats only"))
    if args.continue_on_zero:
        print(f"  Sleep on failure: {args.sleep}s")
        print(f"  Wait for new blocks when synced (sleep for {args.sleep}s)")
    else:
        print(f"  Sleep on failure: {args.sleep}s")
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
        loop.run_until_complete(sync_loop(client, args.sleep, args.continue_on_zero, args.blockstats))
    finally:
        # Clean shutdown
        loop.run_until_complete(client.close())  # if client has async close method
        loop.close()


if __name__ == "__main__":
    main()
