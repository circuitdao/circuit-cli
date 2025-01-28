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
    print("Fasttracking announcer")
    if not launcher_id:
        assert price > 1000
        print("Launching announcer...")
        resp = await rpc_client.announcer_launch(price=price)
        print("Waiting for time to pass to approve announcer (farm blocks if in simulator)...")
        bundle = SpendBundle.from_json_dict(resp["bundle"])
        print("Approving announcer...")
        await rpc_client.wait_for_confirmation(bundle)
        print("Announcer approved.")
    launcher_id, coin_name = await get_announcer_name(rpc_client, launcher_id)
    print(f"announcer {launcher_id=} {coin_name=}")
    statutes = await rpc_client.statutes_list(full=True)
    # find min deposit amount
    min_deposit = int(statutes["enacted_statutes"]["ANNOUNCER_MINIMUM_DEPOSIT"]) #int_from_bytes(bytes.fromhex(statutes["enacted_statutes"]["ANNOUNCER_MINIMUM_DEPOSIT"]))
    max_ttl = int(statutes["enacted_statutes"]["ANNOUNCER_PRICE_TTL"]) #int_from_bytes(bytes.fromhex(statutes["enacted_statutes"]["ANNOUNCER_PRICE_TTL"]))
    custom_ann_statute = statutes["full_enacted_statutes"]["CUSTOM_ANNOUNCEMENTS"]
    print(f"configuring announcer with amount={min_deposit + 1000} ttl={max_ttl - 10}")
    resp = await rpc_client.announcer_configure(coin_name, amount=min_deposit + 1000, ttl=max_ttl - 10)
    bundle = SpendBundle.from_json_dict(resp["bundle"])
    await rpc_client.wait_for_confirmation(bundle)
    print("announcer configured")
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
    print("announcer proposed")
    bills = await rpc_client.bills_list()
    bill_name = bills[0]["name"]
    print("Waiting for time to pass to enact bill (farm blocks if in simulator)...")
    await rpc_client.wait_for_confirmation(blocks=1)
    launcher_id, coin_name = await get_announcer_name(rpc_client, launcher_id)
    resp = await rpc_client.announcer_propose(coin_name, approve=True, enact=True, bill_name=bill_name)
    return resp


async def cli():
    parser = argparse.ArgumentParser(description="Circuit CLI tool")
    subparsers = parser.add_subparsers(dest="command")
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://localhost:8000",
        help="Base URL for the Circuit RPC API server",
    )
    parser.add_argument("--add-sig-data", type=str, help="Additional signature data")
    parser.add_argument("--fee-per-cost", "-fpc", type=str, default=0, help="Add transaction fee, set as fee per cost.")
    parser.add_argument(
        "--private-key", "-p", type=str, default=os.environ.get("PRIVATE_KEY"), help="Private key for your coins"
    )
    upkeep_parser = subparsers.add_parser("upkeep", help="Commands to upkeep protocol and RPC server")
    upkeep_subparsers = upkeep_parser.add_subparsers(dest="action")
    upkeep_subparsers.add_parser("status", help="Get the status of the Circuit RPC server")
    upkeep_subparsers.add_parser("version", help="Get the version of the Circuit RPC server")
    upkeep_subparsers.add_parser("sync", help="Sync the Circuit RPC server with the blockchain")
    upkeep_subparsers.add_parser("vaults", help="List all vaults")
    transfer_sf_parser = upkeep_subparsers.add_parser("transfer_sf", help="Transfer SF to treasury from given vault")
    transfer_sf_parser.add_argument("--vault-id", type=str, help="Vault id")

    ### BILLS ###
    bills_parser = subparsers.add_parser("bills", help="Command to manage bills and governance")
    bills_subparsers = bills_parser.add_subparsers(dest="action")

    ## propose ##
    propose_bills_parser = bills_subparsers.add_parser("propose", help="Propose a new bill to be enacted")
    propose_bills_parser.add_argument("COIN_NAME", type=str, help="Coin name for the bill")
    propose_bills_parser.add_argument("--value", type=str, help="Value of the bill")
    propose_bills_parser.add_argument("--threshold-amount-to-propose", type=int, help="Threshold amount to propose")
    propose_bills_parser.add_argument("--veto-seconds", type=int, help="Veto seconds")
    propose_bills_parser.add_argument("--delay-seconds", type=int, help="Delay seconds")
    propose_bills_parser.add_argument("--max-delta", type=int, help="Max delta")
    propose_bills_parser.add_argument("--statute-index", type=int, help="Statute index")
    propose_bills_parser.add_argument("--proposal-times", default=None, type=int, help="Proposal times")

    ## enact ##
    enact_subparser = bills_subparsers.add_parser("enact", help="Enact a bill into a statue")
    enact_subparser.add_argument("COIN_NAME", type=str, help="Coin name for the bill")

    ## reset ##
    reset_bill_subparser = bills_subparsers.add_parser("reset", help="Reset a bill", description="Sets bill of a governance coin to nil.")
    reset_bill_subparser.add_argument("COIN_NAME", type=str, help="Coin name")

    ## list ##
    list_bills = bills_subparsers.add_parser("list", help="List governance coins", description="By default lists unspent goverenance coins of user.")
    list_bills.add_argument("-a", "--all", action="store_true", help="List all goverenance coins irrespective of who they belong to")
    list_bills.add_argument("-e", "--empty-only", action="store_true", help="Only list empty governance coins, ie those with bill equal to nil")
    list_bills.add_argument("--incl-spent", action="store_true", help="Include spent governance coins")
    #list_bills.add_argument("--list-all", type=bool, help="List all bills available, either active or unused")

    ## toggle governance mode ##
    toggle_bill_subparser = bills_subparsers.add_parser(
        "toggle", help="Convert a plain CRT coin into a governance coin or vice versa",
        description="If coin is in governance mode, convert to plain CRT. If coin is plain CRT, activate governance mode."
    )
    toggle_bill_subparser.add_argument("COIN_NAME", type=str, help="Coin name")

    ### WALLET ###
    wallet_parser = subparsers.add_parser("wallet", help="Wallet commands")
    wallet_subparsers = wallet_parser.add_subparsers(dest="action")

    ## balances ##
    wallet_subparsers.add_parser("balances", help="Get wallet balances")

    ## coins ##
    wallet_subparsers.add_parser("coins", help="Get wallet coins")

    ### ANNOUNCER ###
    announcer_parser = subparsers.add_parser("announcer", help="Announcer commands")
    announcer_subparsers = announcer_parser.add_subparsers(dest="action")

    ## launch ##
    announcer_launch_parser = announcer_subparsers.add_parser("launch", help="Launch an announcer")
    announcer_launch_parser.add_argument("-p", "--price", type=int, help="Initial price")

    ## fasttrack (launch + approve) ##
    launch_approve_parser = announcer_subparsers.add_parser(
        "fasttrack", help="Launch and approve an announcer", description="Launches and approves or approves an announcer. Requires a governance coin with empty bill to be available."
    )
    launch_approve_parser.add_argument("-p", "--price", type=int, help="Initial price. Specify when announcer has not been launched yet")
    launch_approve_parser.add_argument(
        "--launcher-id", type=str, help="Announcer launcher ID. Specify when announcer has already been launched but not approved yet"
    )

    ## list ##
    announcer_list_subparser = announcer_subparsers.add_parser("list", help="List announcers", description="By default lists unspent announcers of user, whether approved or not.")
    announcer_list_subparser.add_argument("-a", "--all", action="store_true", help="List all approved announcers irrespective of who they belong to")
    announcer_list_subparser.add_argument("--incl-spent", action="store_true", help="Include spent announcer coins")

    ## update price ##
    announcer_update_parser = announcer_subparsers.add_parser("mutate", help="Update announcer price", description="Updates the announcer price. The puzzle automatically updates the expiry timestamp.")
    announcer_update_parser.add_argument("-id", "--coin-name", type=str, help="Announcer coin name. Only required if user owns more than one announcer")
    announcer_update_parser.add_argument("PRICE", type=int, help="New announcer price")

    ## configure ##
    announcer_configure_parser = announcer_subparsers.add_parser("configure", help="Configure the announcer", description="Configures the announcer.")
    announcer_configure_parser.add_argument("-id", "--coin-name", type=str, help="Announcer coin name. Only required if user owns more than one announcer")
    announcer_configure_parser.add_argument("--amount", type=int, help="New deposit amount")
    announcer_configure_parser.add_argument("--inner-puzzle-hash", type=int, help="New inner puzzle hash (rekey)")
    announcer_configure_parser.add_argument("--price", type=int, help="New announcer price. If only updating price, use 'mutate' operation")
    announcer_configure_parser.add_argument("--ttl", type=int, help="Time to live in seconds")

    ## propose ##
    announcer_propose_parser = announcer_subparsers.add_parser(
        "propose",
        help="Get vote announcements required to propose this announcer to be approved or disapproved by governance",
    )
    announcer_propose_parser.add_argument("COIN_NAME", type=str, help="Announcer coin name")
    announcer_propose_parser.add_argument("--approve", type=bool, required=True, help="Approve or disapprove the announcer")
    announcer_propose_parser.add_argument(
        "--no-bundle", type=bool, default=True, help="Get the voting announcements only, no bundle"
    )
    announcer_propose_parser.add_argument("--enact", type=bool, default=False, help="Enact the previously proposed bill")
    announcer_propose_parser.add_argument("--bill-name", type=str, default=None, help="Bill name to enact")

    ### ORACLE ###
    oracle_parser = subparsers.add_parser("oracle", help="Oracle commands")
    oracle_subparsers = oracle_parser.add_subparsers(dest="action")

    ## show ##
    oracle_subparsers.add_parser("show", help="Show oracle prices", description="Shows oracle prices.")

    ## update price ##
    oracle_subparsers.add_parser("update", help="Update oracle price", description="Adds new price to Oracle price queue.")

    ### STATUTES ###
    statutes_parser = subparsers.add_parser("statutes", help="Manage statutes")
    statutes_subparsers = statutes_parser.add_subparsers(dest="action")

    ## list ##
    statutes_list_subparser = statutes_subparsers.add_parser("list", help="List Statutes")
    statutes_list_subparser.add_argument("--full", action="store_true", help="Show Statutes incl constraints and additional info")

    ## update price ##
    statutes_subparsers.add_parser("update", help="Update Statutes Price")

    ### COLLATERAL VAULT ###
    vault_parser = subparsers.add_parser("vault", help="Manage a collateral vault")
    vault_subparsers = vault_parser.add_subparsers(dest="action")

    ## show ##
    vault_subparsers.add_parser("show", help="Show vault")

    ## deposit ##
    vault_deposit_subparser = vault_subparsers.add_parser("deposit", help="Deposit to vault")
    vault_deposit_subparser.add_argument("AMOUNT", type=float, help="Amount of XCH to deposit")

    ## withdraw ##
    vault_withdraw_subparser = vault_subparsers.add_parser("withdraw", help="Withdraw from vault")
    vault_withdraw_subparser.add_argument("AMOUNT", type=float, help="Amount of XCH to withdraw")

    ## borrow ##
    vault_borrow_subparser = vault_subparsers.add_parser("borrow", help="Borrow from vault")
    vault_borrow_subparser.add_argument("AMOUNT", type=float, help="Amount of BYC to borrow")

    ## repay ##
    vault_repay_subparser = vault_subparsers.add_parser("repay", help="Repay to vault")
    vault_repay_subparser.add_argument("AMOUNT", type=float, help="Amount of BYC to repay")

    ### SAVINGS VAULT ###

    savings_parser = subparsers.add_parser("savings", help="Manage a savings vault")
    savings_subparsers = savings_parser.add_subparsers(dest="action")

    ## show ##
    savings_subparsers.add_parser("show", help="Show vault")

    ## deposit ##
    savings_deposit_subparser = savings_subparsers.add_parser("deposit", help="Deposit to vault")
    savings_deposit_subparser.add_argument("AMOUNT", type=float, help="Amount of BYC to deposit")

    ## withdraw ##
    savings_withdraw_subparser = savings_subparsers.add_parser("withdraw", help="Withdraw from vault")
    savings_withdraw_subparser.add_argument("AMOUNT", type=float, help="Amount of BYC to withdraw")

    # TODO: SF transfer, liq auction, recovering bad debt
    # TODO: surplus + recharge auctions

    args = parser.parse_args()
    rpc_client = CircuitRPCClient(args.base_url, args.private_key, args.add_sig_data, args.fee_per_cost)
    try:
        kwargs = dict(vars(args))
        #print(kwargs)
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
            print(f"running {args.command}_{args.action} with kwargs {kwargs}")
            result = await getattr(rpc_client, f"{args.command}_{args.action}")(**kwargs)

        if isinstance(result, dict) and "bundle" in result.keys() and "status" in result.keys():
            # we assume we are dealing with a spend bundle that was broadcast
            # all we care about is whether broadcast was successful or not
            print(f"Command status: {result['status']}")
        else:
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
