import argparse
import asyncio
import os
import time
import traceback

import httpx
from chia.types.spend_bundle import SpendBundle

from circuit_cli.client import CircuitRPCClient


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


async def fetch_gateio_price():
    url = "https://api.gate.io/api2/1/ticker/xch_usdt"
    async with httpx.AsyncClient() as client:
        gateio_response = await client.get(url)
        if gateio_response.status_code == 200:
            gateio_data = gateio_response.json()
            return int(float(gateio_data.get("last")) * 100)
        else:
            raise ValueError(f"Failed to fetch price from gate.io: {gateio_response.text}")


async def run_announcer():
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
    parser.add_argument("--fee-per-cost", "-fpc", type=str, help="Add transaction fee, set as fee per cost.")
    parser.add_argument("--launcher_id", "-l", type=str, required=True, help="Announcer launcher id")
    args = parser.parse_args()
    rpc_client = CircuitRPCClient(args.base_url, args.private_key, args.add_sig_data, args.fee_per_cost)

    while True:
        coin_name = args.launcher_id
        # find XCH/USD price
        try:
            # price = random.randint(700, 15000)
            price = await fetch_okx_price()
        except (TypeError, ValueError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError):
            print("Failed to fetch price, skipping.")
            await asyncio.sleep(60)
            continue
        if price is None:
            print("Failed to fetch price, skipping.")
            await asyncio.sleep(60)
            continue
        print("Price is", price, "using coin name", coin_name)
        try:
            announcers = await rpc_client.announcer_list()
            print("Announcers list", announcers)
            for announcer in announcers:
                if announcer["launcher_id"] == coin_name:
                    print("Found announcer", announcer)
                    break
            else:
                raise ValueError(f"Announcer with launcher id {coin_name} not found")
        except (ValueError, httpx.RequestError, httpx.HTTPStatusError) as ve:
            print("Failed to fetch announcer list", ve)
            await asyncio.sleep(30)
            continue
        if announcer["expires"] < time.time() - 120 or abs(price / announcer["price"] - 1) > 0.05:
            print(f"Announcer price expired, updating: {announcer['expires']} < {time.time() - 120}")
            try:
                # mutate announcer
                data = await rpc_client.announcer_mutate(coin_name, price=price)
                print("Got back data", data)
                # wait for transaction to be confirmed
                final_bundle: SpendBundle = SpendBundle.from_json_dict(data["bundle"])
                coin_name = final_bundle.additions()[0].name().hex()
                # update to new coin name
                print(
                    "Updated coin name",
                    coin_name,
                    "all coins",
                    [coin.name().hex() for coin in final_bundle.additions()],
                )
                await rpc_client.wait_for_confirmation(final_bundle)
            except Exception as ve:
                print("Failed to mutate announcer", ve)
        else:
            print(f"Announcer price is still valid, skipping update: {announcer['expires']} > {time.time() - 120}")
        try:
            # try to update oracle
            await rpc_client.upkeep_sync()
            print("Updating oracle")
            data = await rpc_client.oracle_update()
        except (ValueError, httpx.TransportError) as ve:
            print("Error updating oracle", ve)
        # first we announce so we can update statutes after
        try:
            data = await rpc_client.statutes_announce()
            print("Announce statutes", data)
            bundle = SpendBundle.from_json_dict(data["bundle"])
            await rpc_client.wait_for_confirmation(bundle)
        except (ValueError, httpx.TransportError):
            print("Failed to announce statutes")
        # update statutes price if oracle update was successful
        try:
            data = await rpc_client.statutes_update_price()
            print("Statutes updated, waiting for confirmation")
            try:
                await rpc_client.wait_for_confirmation(SpendBundle.from_json_dict(data["bundle"]))
                print("Statutes price updated!")
            except (ValueError, httpx.TransportError) as ve:
                print("Failed to confirm statutes update", ve)
                continue
        except (ValueError, httpx.TransportError) as ve:
            print("Failed to update statutes price", ve)
            traceback.print_exc()
        await asyncio.sleep(50)


def main():
    import asyncio

    asyncio.run(run_announcer())


if __name__ == "__main__":
    main()
