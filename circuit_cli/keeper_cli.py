import argparse
import asyncio
import os

import httpx
from chia.types.spend_bundle import SpendBundle
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_hash_for_synthetic_public_key

from circuit_cli.client import CircuitRPCClient

MOJOS = 10**12


async def fetch_okx_price():
    """
    Fetches the latest price of a cryptocurrency from OKX API v5.

    :return: The latest price as a float, or None if an error occurs.
    """
    url = "https://www.okx.com/api/v5/market/ticker?instId=XCH-USDT"
    headers = {"Accept": "application/json"}

    async with httpx.AsyncClient() as http_client:
        try:
            okx_response = await http_client.get(url, headers=headers)
            okx_response.raise_for_status()  # Raises an error for bad responses
            okx_data = okx_response.json()
            return int(float(okx_data["data"][0]["last"]) * 100)
        except Exception as e:
            print(f"Error fetching crypto price: {e}")
            return None


async def run_keeper():
    # TODO: simple bot liquidation strategy, bid % diff between current price and the price in the vault bid
    #       - start auction when it finds a pending vault for liquidation (vault with debt > 0)
    parser = argparse.ArgumentParser(description="Circuit reference price announcer CLI tool")
    parser.add_argument(
        "--base-url",
        type=str,
        help="Base URL for the Circuit RPC API server",
        default="http://localhost:8000",
    )
    parser.add_argument("--add-sig-data", type=str, help="Additional signature data")
    parser.add_argument(
        "--private_key", "-p", type=str, default=os.environ.get("PRIVATE_KEY"), help="Private key for your coins"
    )
    parser.add_argument("--max-bid-amount", type=int, required=True, help="Max amount bot should bid in BYC")
    parser.add_argument(
        "--min-discount", type=float, required=True, help="Min discount between market XCH price and bid price to bid"
    )
    args = parser.parse_args()
    rpc_client = CircuitRPCClient(args.base_url, args.private_key)
    while True:
        # any vaults to liquidate?
        response = rpc_client.client.get("/protocol/state")
        if response.status_code != 200:
            print("Failed to get protocol state", response.content)
            await asyncio.sleep(60)
            continue
        state = response.json()
        my_puzzle_hash = puzzle_hash_for_synthetic_public_key(rpc_client.synthetic_public_keys[0])
        balances = await rpc_client.wallet_balances()
        print("Balances", balances)
        print("State", state)
        if state["vaults_pending_liquidation"]:
            print("Found vaults pending liquidation", state["vaults_pending_liquidation"])
            vaults_pending = state["vaults_pending_liquidation"]
            for vault_pending in vaults_pending:
                vault_pending_name = vault_pending["name"]
                response = rpc_client.client.post(
                    "/vaults/start_auction",
                    json={
                        "vault_name": vault_pending_name,
                        "synthetic_pks": [key.to_bytes().hex() for key in rpc_client.synthetic_public_keys],
                        "initiator_puzzle_hash": my_puzzle_hash.hex(),
                    },
                )
                if response.status_code != 200:
                    print("Failed to start auction", response.content)
                    await asyncio.sleep(60)
                    continue
                auction_bundle = response.json()
                # sign
                signed_data = await rpc_client.sign_and_push(SpendBundle.from_json_dict(auction_bundle))
                print("Auction started", signed_data)
        elif state["vaults_in_liquidation"]:
            print("Found vaults in liquidation", state["vaults_in_liquidation"])
            vaults_in_liquidation = state["vaults_in_liquidation"]
            for vault_in_liquidation in vaults_in_liquidation:
                vault_name = vault_in_liquidation["name"]
                # get vault info first
                response = rpc_client.client.get(
                    "/vaults/" + vault_in_liquidation["name"],
                )
                if response.status_code != 200:
                    print("Failed to get vault info", response.content)
                    continue
                vault_info = response.json()
                bid_price_per_xch = vault_info["price_per_collateral"]
                print("Vault info", vault_info)
                assert bid_price_per_xch
                while tries := 3:
                    try:
                        xch_price = await fetch_okx_price()
                        break
                    except (TypeError, ValueError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError):
                        tries -= 1
                        await asyncio.sleep(60)
                else:
                    print("Failed to fetch price, skipping.")
                    continue
                print("Current price is", xch_price, "bid price", bid_price_per_xch)
                max_bid_price = xch_price * (1 - args.min_discount)
                if bid_price_per_xch > max_bid_price:
                    print("Bid price too high, skipping")
                    # wait for next block to check again
                    await asyncio.sleep(60)
                    continue
                # check how much BYC we have
                byc_balance = balances.get("byc", 0)
                print("My BYC balance: %r" % byc_balance)
                if byc_balance < 1:
                    print("Not enough BYC to bid, skipping")
                    continue
                available_xch = vault_info["deposited"]
                if available_xch < 1:
                    print("Not enough XCH to bid, skipping (%s)" % available_xch)
                    continue
                # bid
                print(f"Calculating xch to acquire with params: {args.max_bid_amount}, {bid_price_per_xch}")
                xch_to_acquire = (args.max_bid_amount / 1000) / (bid_price_per_xch / 100) * MOJOS
                print(f"XCH to acquire: {xch_to_acquire} vs available: {available_xch}")
                if xch_to_acquire > available_xch:
                    byc_bid_amount = int((available_xch / MOJOS) * bid_price_per_xch) * 100
                    xch_to_acquire = available_xch
                    print(f"Not enough XCH to bid ({available_xch}), lowering bid amount", byc_bid_amount)
                else:
                    byc_bid_amount = args.max_bid_amount
                    print("Enough XCH to bid, bidding full amount", byc_bid_amount)
                print(f"Bidding {byc_bid_amount} BYC for {xch_to_acquire / MOJOS} XCH")
                response = rpc_client.client.post(
                    "/vaults/bid_auction",
                    json={
                        "vault_name": vault_name,
                        "synthetic_pks": [key.to_bytes().hex() for key in rpc_client.synthetic_public_keys],
                        "bidder_puzzle_hash": my_puzzle_hash.hex(),
                        "max_bid_price": bid_price_per_xch + 1,
                        "amount": byc_bid_amount,
                    },
                )
                if response.status_code != 200:
                    print("Failed to bid auction", response.content)
                    continue
                bid_bundle = response.json()
                # sign
                await rpc_client.sign_and_push(SpendBundle.from_json_dict(bid_bundle))
                print("Bid placed, acquired more xch", vaults_in_liquidation)
        elif state["vaults_with_bad_debt"]:
            print("Found vaults with bad debt", state["vaults_with_bad_debt"])
            vaults_with_bad_debt = state["vaults_with_bad_debt"]
            for vault_with_bad_debt in vaults_with_bad_debt:
                vault_name = vault_with_bad_debt["name"]
                response = rpc_client.client.post(
                    "/vaults/recover_bad_debt",
                    json={
                        "vault_name": vault_name,
                        "synthetic_pks": [key.to_bytes().hex() for key in rpc_client.synthetic_public_keys],
                    },
                )
                if response.status_code != 200:
                    print("Failed to liquidate vault", response.content)
                    await asyncio.sleep(60)
                    continue
                liquidation_bundle = response.json()
                # sign
                signed_data = await rpc_client.sign_and_push(SpendBundle.from_json_dict(liquidation_bundle))
                print("Recovered some debt", signed_data)
        print("Waiting for next upkeep...")
        await asyncio.sleep(30)


def main():
    import asyncio

    asyncio.run(run_keeper())


if __name__ == "__main__":
    main()
