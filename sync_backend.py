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


async def call_upkeep_sync(client: CircuitRPCClient) -> Dict[str, Any]:
    """Call the RPC method — reuse existing client instance."""
    try:
        result = await client.upkeep_rpc_sync()
        return dict(result)  # or result.model_dump() if pydantic
    except Exception as exc:
        raise RuntimeError(f"RPC call failed: {exc}") from exc


async def sync_loop(client: CircuitRPCClient, sleep_sec: int, exit_on_zero: bool):
    """Main async loop that keeps calling the sync method."""
    total_blocks = 0
    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] Calling upkeep_rpc_sync()")

        try:
            result = await call_upkeep_sync(client)
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
                if exit_on_zero:
                    print("Exiting (--exit-on-zero)")
                    sys.exit(10)
                else:
                    print(f"Sleeping {sleep_sec}s before next check")
                    await asyncio.sleep(sleep_sec)
                    continue

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
    parser = argparse.ArgumentParser(description="Sync Circuit backend with blockchain")
    parser.add_argument(
        "--sleep",
        type=int,
        default=DEFAULT_SLEEP,
        help=f"seconds to sleep when idle or error (default: {DEFAULT_SLEEP})",
    )
    parser.add_argument(
        "--exit-on-zero", "-e", action="store_true", help="exit when blocks_synced == 0 instead of sleeping"
    )
    args = parser.parse_args()

    base_url = os.environ.get("BASE_URL")
    if not base_url:
        print("Error: Environment variable BASE_URL is not set.", file=sys.stderr)
        print("Example: export BASE_URL=http://localhost:8000", file=sys.stderr)
        sys.exit(1)

    print("CircuitDAO RPC sync loop started")
    print(f"  Base URL: {base_url}")
    if args.exit_on_zero:
        print(f"  Sleep on failure: {args.sleep}s")
        print("  Exit when no more blocks to sync")
    else:
        print(f"  Sleep on failure: {args.sleep}s")
        print(f"  Sleep when no more blocks to sync: {args.sleep}s")
    print("Press Ctrl+C to stop")
    print("-" * 40)

    # Create client once
    client = CircuitRPCClient(base_url, None)

    # Create one event loop and run the async loop forever
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(sync_loop(client, args.sleep, args.exit_on_zero))
    finally:
        # Clean shutdown
        loop.run_until_complete(client.close())  # if client has async close method
        loop.close()


if __name__ == "__main__":
    main()
