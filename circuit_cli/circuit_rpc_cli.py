import argparse
import asyncio
import os
import pprint

from chia.types.spend_bundle import SpendBundle
from clvm_rs.casts import int_from_bytes

from circuit_cli.client import CircuitRPCClient


async def get_announcer_name(rpc_client, launcher_id: str = None):
    data = await rpc_client.announcer_list()
    if not launcher_id:
        return data[0]["launcher_id"], data[0]["name"]
    for announcer in data:
        if announcer["launcher_id"] == launcher_id:
            return launcher_id, announcer["name"]
    raise ValueError(f"Announcer with launcher_id {launcher_id.hex()} not found")


async def announcer_fasttrack(rpc_client, price: int, launcher_id: str = None):
    assert price > 1000
    if not launcher_id:
        print("Launching announcer...")
        resp = await rpc_client.announcer_launch(price=price)
        print("Waiting for time to pass to approve announcer (farm blocks if in simulator)...")
        bundle = SpendBundle.from_json_dict(resp["bundle"])
        print("Approving announcer...")
        await rpc_client.wait_for_confirmation(bundle)
        print("Announcer approved.")
    launcher_id, coin_name = await get_announcer_name(rpc_client, launcher_id)
    statutes = await rpc_client.protocol_statutes()
    # find min deposit amount
    min_deposit = int_from_bytes(bytes.fromhex(statutes["enacted_statutes"]["ANNOUNCER_MINIMUM_DEPOSIT"]))
    max_delay = int_from_bytes(bytes.fromhex(statutes["enacted_statutes"]["ANNOUNCER_PRICE_TTL"]))
    custom_ann_statute = statutes["full_enacted_statutes"]["CUSTOM_ANNOUNCEMENTS"]
    resp = await rpc_client.announcer_configure(coin_name, amount=min_deposit + 1000, delay=max_delay - 10)
    bundle = SpendBundle.from_json_dict(resp["bundle"])
    await rpc_client.wait_for_confirmation(bundle)
    # propose announcer
    launcher_id, announcer_coin_name = await get_announcer_name(rpc_client, launcher_id)
    vote_data = await rpc_client.announcer_propose(announcer_coin_name, approve=True, no_bundle=True)
    voting_anns = vote_data["announcements_to_vote_for"]
    bills = await rpc_client.bills_list()
    bill_name = bills[0]["name"]
    resp = await rpc_client.bills_propose(
        bill_name,
        voting_anns,
        custom_ann_statute["threshold_amount_to_propose"],
        custom_ann_statute["veto_seconds"],
        custom_ann_statute["delay_seconds"],
        custom_ann_statute["max_delta"],
        statute_index=-1,
    )
    bundle = SpendBundle.from_json_dict(resp["bundle"])
    await rpc_client.wait_for_confirmation(bundle)
    bills = await rpc_client.bills_list()
    bill_name = bills[0]["name"]
    print("Waiting for time to pass to enact bill (farm blocks if in simulator)...")
    await rpc_client.wait_for_confirmation(blocks=3)
    launcher_id, coin_name = await get_announcer_name(rpc_client, launcher_id)
    resp = await rpc_client.announcer_propose(coin_name, approve=True, enact=True, bill_name=bill_name)
    print("Fasttrack result:")
    return resp


async def cli():
    parser = argparse.ArgumentParser(description="Circuit CLI tool")
    subparsers = parser.add_subparsers(dest="command")
    parser.add_argument(
        "--base-url",
        type=str,
        help="Base URL for the Circuit RPC API server",
        default="http://localhost:8000",
    )
    parser.add_argument("--add-sig-data", type=str, help="Additional signature data")
    parser.add_argument("--fee-per-cost", "-fpc", type=str, help="Add transaction fee, set as fee per cost.")
    parser.add_argument(
        "--private_key", "-p", type=str, default=os.environ.get("PRIVATE_KEY"), help="Private key for your coins"
    )
    upkeep_parser = subparsers.add_parser("upkeep", help="Commands to upkeep protocol and RPC server")
    upkeep_subparsers = upkeep_parser.add_subparsers(dest="action")
    upkeep_subparsers.add_parser("status", help="Get the status of the Circuit RPC server")
    upkeep_subparsers.add_parser("version", help="Get the version of the Circuit RPC server")
    upkeep_subparsers.add_parser("sync", help="Sync the Circuit RPC server with the blockchain")
    upkeep_subparsers.add_parser("vaults", help="List all vaults")
    transfer_sf_parser = upkeep_subparsers.add_parser("transfer_sf", help="Transfer SF to treasury from given vault")
    transfer_sf_parser.add_argument("--vault-id", type=str, help="Vault id")
    bills_parser = subparsers.add_parser("bills", help="Command to manage bills and governance")
    bills_subparsers = bills_parser.add_subparsers(dest="action")
    propose_bills_parser = bills_subparsers.add_parser("propose", help="Propose a new bill to be enacted")
    propose_bills_parser.add_argument("coin_name", type=str, help="Coin name for the bill")
    propose_bills_parser.add_argument("--value", type=str, help="Value of the bill")
    propose_bills_parser.add_argument("--threshold-amount-to-propose", type=int, help="Threshold amount to propose")
    propose_bills_parser.add_argument("--veto-seconds", type=int, help="Veto seconds")
    propose_bills_parser.add_argument("--delay-seconds", type=int, help="Delay seconds")
    propose_bills_parser.add_argument("--max-delta", type=int, help="Max delta")
    propose_bills_parser.add_argument("--statute-index", type=int, help="Statute index")
    propose_bills_parser.add_argument("--proposal-times", default=None, type=int, help="Proposal times")

    enact_subparser = bills_subparsers.add_parser("enact", help="Enact a bill into a statue")
    enact_subparser.add_argument("coin_name", type=str, help="Coin name for the bill")

    reset_bill_subparser = bills_subparsers.add_parser("reset", help="Reset a bill")
    reset_bill_subparser.add_argument("coin_name", type=str, help="Coin name for the bill")

    list_bills = bills_subparsers.add_parser("list", help="List all bills available, either active or unused")
    list_bills.add_argument("--list-all", type=bool, help="List all bills available, either active or unused")
    toggle_bill_subparser = bills_subparsers.add_parser(
        "toggle", help="Convert a CRT coin into a governance coin or vice versa"
    )
    toggle_bill_subparser.add_argument("--coin-name", type=str, help="Coin name for the bill")
    toggle_bill_subparser.add_argument("--set-governance", type=bool, help="Enable or disable governance")

    wallet_parser = subparsers.add_parser("wallet", help="Wallet commands")
    wallet_subparsers = wallet_parser.add_subparsers(dest="action")

    wallet_subparsers.add_parser("balances", help="Get wallet balances")
    wallet_subparsers.add_parser("coins", help="Get wallet coins")
    announcer_parser = subparsers.add_parser("announcer", help="Announcer commands")
    announcer_subparsers = announcer_parser.add_subparsers(dest="action")

    run_parser = announcer_subparsers.add_parser("run", help="Run the announcer")
    run_parser.add_argument("--coin_name", type=str, help="Announcer coin name")
    launch_parser = announcer_subparsers.add_parser("launch", help="Launch the announcer")
    launch_parser.add_argument("--price", type=int, help="Initial price")
    launch_approve_parser = announcer_subparsers.add_parser(
        "fasttrack", help="Launch the announcer with approval (only if you also have enough CRT available)"
    )
    launch_approve_parser.add_argument("--price", type=int, help="Initial price")
    launch_approve_parser.add_argument(
        "--launcher-id", type=str, help="Announcer launcher id if already launched, but not approved"
    )
    announcer_subparsers.add_parser("list", help="List all announcers that belong to given key")
    mutate_parser = announcer_subparsers.add_parser("mutate", help="Mutate the announcer")
    mutate_parser.add_argument("--coin_name", type=str, help="Announcer coin name")
    mutate_parser.add_argument("--delay", type=int, help="Delay in seconds")
    mutate_parser.add_argument("--amount", type=int, help="New deposit amount")
    mutate_parser.add_argument("--price", type=int, help="New price")
    mutate_parser.add_argument("--deactivate", type=bool, default=False, help="Deactivate the announcer")
    mutate_parser.add_argument("--inner-puzzle-hash", type=int, help="New inner puzzle hash (rekey)")

    propose_parser = announcer_subparsers.add_parser(
        "propose",
        help="Get vote announcements required to propose this announcer to be approved or disapproved by governance",
    )
    propose_parser.add_argument("coin_name", type=str, help="Announcer coin name")
    propose_parser.add_argument("--approve", type=bool, required=True, help="Approve or disapprove the announcer")
    propose_parser.add_argument(
        "--no-bundle", type=bool, default=True, help="Get the voting announcements only, no bundle"
    )
    propose_parser.add_argument("--enact", type=bool, default=False, help="Enact the previously proposed bill")
    propose_parser.add_argument("--bill-name", type=str, default=None, help="Bill name to enact")

    oracle_parser = subparsers.add_parser("oracle", help="Oracle commands")
    oracle_subparsers = oracle_parser.add_subparsers(dest="action")
    oracle_subparsers.add_parser("update", help="Update the oracle price")
    statutes_parser = subparsers.add_parser("statutes", help="Manage statutes")
    statutes_subparsers = statutes_parser.add_subparsers(dest="action")
    statutes_subparsers.add_parser("list", help="List all statutes")
    statutes_subparsers.add_parser("update_price", help="Update the price in the statutes")

    vault_parser = subparsers.add_parser("vault", help="Manage a collateral vault")
    vault_subparsers = vault_parser.add_subparsers(dest="action")
    borrow_subparser = vault_subparsers.add_parser("borrow", help="Borrow from the vault")
    borrow_subparser.add_argument("--amount", type=int, help="Amount to borrow")

    deposit_subparser = vault_subparsers.add_parser("deposit", help="Deposit to the vault")
    deposit_subparser.add_argument("--amount", type=int, help="Amount to deposit")
    vault_subparsers.add_parser("show", help="Show the vault")

    args = parser.parse_args()
    rpc_client = CircuitRPCClient(args.base_url, args.private_key, args.add_sig_data, args.fee_per_cost)
    try:
        kwargs = dict(vars(args))
        print(kwargs)
        del kwargs["command"]
        del kwargs["action"]
        del kwargs["base_url"]
        del kwargs["private_key"]
        del kwargs["add_sig_data"]
        del kwargs["fee_per_cost"]
        if args.command == "announcer" and args.action == "fasttrack":
            # special case for fasttrack
            result = await announcer_fasttrack(rpc_client, **kwargs)
        else:
            # run commands method dynamically based on the parser command
            result = await getattr(rpc_client, f"{args.command}_{args.action}")(**kwargs)

        pprint.pprint(result)
    except (AttributeError, KeyError) as e:
        print(e)
        parser.print_help()
    finally:
        rpc_client.close()


def main():
    asyncio.run(cli())


if __name__ == "__main__":
    main()
