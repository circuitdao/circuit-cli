import argparse
import asyncio
import os
import math
from datetime import datetime
import pprint

from chia.types.spend_bundle import SpendBundle
from clvm_rs.casts import int_from_bytes

from circuit_cli.client import CircuitRPCClient

from clvm_tools.binutils import disassemble

MOJOS = 10**12
MCAT = 10**3
PRICE_PRECISION = 10**2


def make_human_readable(result: dict) -> dict:
    for k, v in result.items():
        if result[k] is not None:
            if k.lower() in [
                    "byc", "available_to_borrow", "byc_to_melt_balance", "debt",
                    "initiator_incentive_balance", "principal", "stability_fees", "total_debt",
                    "accrued_interest", "savings_balance", "discounted_principal"
            ]:
                result[k] = f"{v/MCAT:,} BYC"
            elif k.lower() in ["crt", "amount"]:
                result[k] = f"{v/MCAT:,} CRT"
            elif k.lower() in ["xch", "available_to_withdraw", "collateral"]:
                result[k] = f"{v/10**12:,} XCH"
            elif k.lower() in ["collateral_ratio_bps"]:
                result[k] = "{:.{prec}f}%".format(v/100, prec=2)
            elif k.lower() in ["price_per_collateral"]:
                result[k] = "{:.{prec}f} XCH/BYC".format(v/PRICE_PRECISION, prec=math.log10(PRICE_PRECISION))
            elif k.lower() in ["time_left_until_enactable", "time_left"]:
                result[k] = f"{v} seconds"
            elif k.lower() in ["timestamp", "enactable_at"]:
                result[k] = f"{datetime.fromtimestamp(v).strftime('%Y-%m-%d, %H:%M:%S')}"
    return result


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
        #assert price > 1000
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
    print(f"configuring announcer with deposit={min_deposit} min_deposit={min_deposit} ttl={max_ttl-10}")
    resp = await rpc_client.announcer_configure(coin_name, deposit=min_deposit, min_deposit=min_deposit, ttl=max_ttl-10)
    bundle = SpendBundle.from_json_dict(resp["bundle"])
    await rpc_client.wait_for_confirmation(bundle)
    print("announcer configured")
    # approve announcer
    launcher_id, announcer_coin_name = await get_announcer_name(rpc_client, launcher_id)
    vote_data = await rpc_client.announcer_govern(
        announcer_coin_name,
        approve=True,
        create_conditions=True,
    )
    voting_anns = vote_data["announcements_to_vote_for"]
    bills = await rpc_client.bills_list()
    bill_name = bills[0]["name"]
    resp = await rpc_client.bills_propose(
        INDEX=-1,
        VALUE=voting_anns,
        coin_name=bill_name,
    )
    bundle = SpendBundle.from_json_dict(resp["bundle"])
    await rpc_client.wait_for_confirmation(bundle)
    print("bill to approve announcer proposed")
    bills = await rpc_client.bills_list()
    bill_name = bills[0]["name"]
    print("Waiting for time to pass to enact bill (farm blocks if in simulator)...")
    await rpc_client.wait_for_confirmation(blocks=1)
    print("Implementation delay has passsed, enacting bill")
    launcher_id, coin_name = await get_announcer_name(rpc_client, launcher_id)
    # enacting announcer approval
    resp = await rpc_client.announcer_govern(
        coin_name,
        approve=True,
        enact_bill_name=bill_name,
    )
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

    ### UPKEEP ###
    upkeep_parser = subparsers.add_parser("upkeep", help="Commands to upkeep protocol and RPC server")
    upkeep_subparsers = upkeep_parser.add_subparsers(dest="action")

    ## protocol info ##
    upkeep_subparsers.add_parser("info", help="Show protocol info", description="Displays BYC and CRT asset IDs.") # LATER: added launcher ID of Statutes (& Oracle?)

    ## protocol state ##
    upkeep_subparsers.add_parser("state", help="Show protocol state", description="Displays current state of protocol coins that are relevenat for keepers or governance. For SF transfers please see upkeep vaults command")
    # LATER: show current protocol state. RPC endpoint: /protocol/state

    ## RPC server ##
    upkeep_rpc_parser = upkeep_subparsers.add_parser("rpc", help="Info on Circuit RPC server")
    upkeep_rpc_subparsers = upkeep_rpc_parser.add_subparsers(dest="subaction")
    upkeep_rpc_subparsers.add_parser("status", help="Status of Circuit RPC server") # LATER: implement. display block height and hash synced to.
    upkeep_rpc_subparsers.add_parser("sync", help="Synchronize Circuit RPC server with Chia blockchain")
    upkeep_rpc_subparsers.add_parser("version", help="Version of Circuit RPC server")

    ## vaults ##
    upkeep_vaults_parser = upkeep_subparsers.add_parser("vaults", help="Manage collateral vaults")
    upkeep_vaults_subparsers = upkeep_vaults_parser.add_subparsers(dest="subaction")
    upkeep_vaults_show_parser = upkeep_vaults_subparsers.add_parser("show", help="Show all vaults")
    upkeep_vaults_show_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")
    # LATER: add -o/--ordered arg to order by outstanding SFs
    upkeep_vaults_transfer_parser = upkeep_vaults_subparsers.add_parser("transfer", help="Transfer stability fees from vault to treasury", description="Transfers stability fees from specified vault to treasury.")
    upkeep_vaults_transfer_parser.add_argument("COIN_NAME", type=str, help="Vault ID")
    upkeep_vaults_auction_parser = upkeep_vaults_subparsers.add_parser("auction", help="Participate in liquidation auction")
    upkeep_vaults_auction_parser.add_argument("COIN_NAME", type=str, help="Vault ID")
    upkeep_vaults_auction_parser.add_argument("-s", "--start", action="store_true", help="Start or restart a liquidation auction")
    upkeep_vaults_auction_parser.add_argument("-b", "--bid-amount", type=int, help="Submit a bid in a liquidation auction. Specify bid amount in mBYC")
    upkeep_vaults_recover_parser = upkeep_vaults_subparsers.add_parser("recover", help="Recover bad debt")
    upkeep_vaults_recover_parser.add_argument("COIN_NAME", type=str, help="Vault ID")
    upkeep_vaults_recover_parser.add_argument("AMOUNT", type=int, help="Amount (in mBYC) of bad debt to recover")

    ### BILLS ###
    bills_parser = subparsers.add_parser("bills", help="Command to manage bills and governance")
    bills_subparsers = bills_parser.add_subparsers(dest="action")

    ## propose ##
    bills_propose_parser = bills_subparsers.add_parser("propose", help="Propose a new bill to be enacted")
    bills_propose_parser.add_argument("INDEX", type=int, help="Statute index. Specify -1 for custom conditions")
    bills_propose_parser.add_argument("VALUE", nargs="?", default=None, type=str, help="Value of bill, ie Statute value or custom announcements. Omit to keep current value. Must be a Program in hex format if INDEX = -1, a 32-byte hex string if INDEX = 0, and an integer otherwise")
    bills_propose_parser.add_argument("-id", "--coin-name", default=None, type=str, help="Governance coin to use for proposal. If not specified, a suitable coin is chosen automatically")
    bills_propose_parser.add_argument("-f", "--force", action="store_true", help="Propose bill even if resulting Statutes would not be consistent")
    bills_propose_parser.add_argument("--proposal-threshold", default=None, type=int, help="Min amount of CRT required to propose new Statute value")
    bills_propose_parser.add_argument("--veto-seconds", type=int, default=None, help="Veto period in seconds")
    bills_propose_parser.add_argument("--delay-seconds", type=int, default=None, help="Implementation delay in seconds")
    bills_propose_parser.add_argument("--max-delta", type=int, default=None, help="Max absolute amount in bps by which Statues value may change")

    ## enact ##
    bills_enact_subparser = bills_subparsers.add_parser("enact", help="Enact a bill into statute", description="Enact a bill.")
    bills_enact_subparser.add_argument("COIN_NAME", nargs="?", default=None, type=str, help="[optional] Coin name of bill to enact")
    bills_enact_subparser.add_argument("-i", "--info", action="store_true", help="Show info on when next bill can be enacted")
    bills_enact_subparser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")

    ## reset ##
    bills_reset_subparser = bills_subparsers.add_parser("reset", help="Reset a bill", description="Sets bill of a governance coin to nil.")
    bills_reset_subparser.add_argument("COIN_NAME", type=str, help="Coin name")

    ## list ##
    bills_list_parser = bills_subparsers.add_parser("list", help="List governance coins", description="Lists goverenance coins of user.")
    bills_list_parser.add_argument("-a", "--all", action="store_true", help="List all goverenance coins irrespective of who they belong to")
    bills_list_parser.add_argument("-e", "--empty-only", action="store_true", help="Only list empty governance coins, ie those with bill equal to nil")
    bills_list_parser.add_argument("-n", "--non-empty-only", action="store_true", help="Only list non-empty governance coins, ie those with bill not equal to nil")
    bills_list_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")
    bills_list_parser.add_argument("--incl-spent", action="store_true", help="Include spent governance coins")

    ## toggle governance mode ##
    bill_toggle_parser = bills_subparsers.add_parser(
        "toggle", help="Convert a plain CRT coin into a governance coin or vice versa",
        description="If coin is in governance mode, convert to plain CRT. If coin is plain CRT, activate governance mode."
    )
    bill_toggle_parser.add_argument("COIN_NAME", type=str, help="Coin name")

    ### WALLET ###
    wallet_parser = subparsers.add_parser("wallet", help="Wallet commands")
    wallet_subparsers = wallet_parser.add_subparsers(dest="action")

    ## balances ##
    wallet_balances_parser = wallet_subparsers.add_parser("balances", help="Get wallet balances")
    wallet_balances_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")

    ## coins ##
    wallet_coins_parser = wallet_subparsers.add_parser("coins", help="Get wallet coins")
    wallet_coins_parser.add_argument("-t", "--type", type=str, choices=["byc", "crt", "xch"], help="Return coins of given type only")

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
    announcer_list_subparser.add_argument("-a", "--approved", action="store_true", help="List approved announcers only")
    announcer_list_subparser.add_argument("--all", action="store_true", help="List all approved announcers irrespective of who they belong to")
    announcer_list_subparser.add_argument("-v", "--valid", action="store_true", help="List valid announcers only (valid = approved, not expired, not banned)")
    announcer_list_subparser.add_argument("--incl-spent", action="store_true", help="Include spent announcer coins")

    ## update price ##
    announcer_update_parser = announcer_subparsers.add_parser("update", help="Update announcer price", description="Updates the announcer price. The puzzle automatically updates the expiry timestamp.")
    announcer_update_parser.add_argument("-id", "--coin-name", type=str, help="Announcer coin name. Only required if user owns more than one announcer")
    announcer_update_parser.add_argument("PRICE", type=int, help="New announcer price")

    ## configure ##
    announcer_configure_parser = announcer_subparsers.add_parser("configure", help="Configure the announcer", description="Configures the announcer.")
    announcer_configure_parser.add_argument("-id", "--coin-name", type=str, help="Announcer coin name. Only required if user owns more than one announcer")
    announcer_configure_parser.add_argument("--deposit", type=int, help="New deposit amount")
    announcer_configure_parser.add_argument("--min-deposit", type=int, help="New minimum deposit amount")
    announcer_configure_parser.add_argument("--inner-puzzle-hash", type=int, help="New inner puzzle hash (re-key)")
    announcer_configure_parser.add_argument("--price", type=int, help="New announcer price. If only updating price, it's more effcient to use 'update' operation")
    announcer_configure_parser.add_argument("--ttl", type=int, help="New time to live in seconds")

    ## govern ##
    announcer_govern_parser = announcer_subparsers.add_parser(
        "govern",
        help="Govern announcer",
        description="Create custom conditions required to approve or disapprove announcer.",
    )
    announcer_govern_parser.add_argument("COIN_NAME", type=str, help="Announcer coin name")
    announcer_govern_parser.add_argument("-a", "--approve", action="store_true", help="Approve the announcer")
    announcer_govern_parser.add_argument("-d", "--disapprove", action="store_true", help="Disapprove the announcer")
    announcer_govern_parser.add_argument(
        "-c", "--create-conditions", action="store_true", help="Create custom conditions for bill only, no spend bundle"
    )
    #announcer_govern_parser.add_argument("-e", "--enact-bill", action="store_true", help="Enact the previously proposed bill containing custom conditions to govern announcer")
    announcer_govern_parser.add_argument("-e", "--enact-bill-name", type=str, default=None, help="Enact the previously proposed bill containing custom conditions to govern announcer")
    #announcer_govern_parser.add_argument("-b", "--bill-name", type=str, default=None, help="Name of bill to enact. Must be provided when enacting")

    ### ORACLE ###
    oracle_parser = subparsers.add_parser("oracle", help="Oracle commands")
    oracle_subparsers = oracle_parser.add_subparsers(dest="action")

    ## show ##
    oracle_subparsers.add_parser("show", help="Show oracle prices", description="Shows oracle prices.")

    ## update price ##
    oracle_update_parser = oracle_subparsers.add_parser("update", help="Update oracle price", description="Adds new price to Oracle price queue.")
    oracle_update_parser.add_argument("-i", "--info", action="store_true", help="Show info on whether Oracle can be updated")

    ## resolve outlier ##
    oracle_outlier_parser = oracle_subparsers.add_parser("outlier", help="Deal with an oracle price outlier", description="Vote on or resolve an oracle price outlier")
    oracle_outlier_subparsers = oracle_outlier_parser.add_subparsers(dest="subaction")
    oracle_outlier_vote_parser = oracle_outlier_subparsers.add_parser("vote", help="Vote on an oracle price outlier")
    oracle_outlier_vote_parser.add_argument("COIN_NAME", nargs="?", type=str, default=None, help="[optional] CRT coin to use to vote on outlier")
    oracle_outlier_vote_parser.add_argument("-a", "--accept", action="store_true", help="Accept price outlier")
    oracle_outlier_vote_parser.add_argument("-r", "--reject", action="store_true", help="Reject price outlier")
    oracle_outlier_resolve_parser = oracle_outlier_subparsers.add_parser("resolve", help="Resolve outlier according to latest vote")

    ### STATUTES ###
    statutes_parser = subparsers.add_parser("statutes", help="Manage statutes")
    statutes_subparsers = statutes_parser.add_subparsers(dest="action")

    ## list ##
    statutes_list_subparser = statutes_subparsers.add_parser("list", help="List Statutes")
    statutes_list_subparser.add_argument("-f", "--full", action="store_true", help="Show Statutes incl constraints and additional info")

    ## update price ##
    statutes_update_subparser = statutes_subparsers.add_parser("update", help="Update Statutes Price")
    statutes_update_subparser.add_argument("-i", "--info", action="store_true", help="Show info on when Statues can be updated next")

    ### COLLATERAL VAULT ###
    vault_parser = subparsers.add_parser("vault", help="Manage a collateral vault")
    vault_subparsers = vault_parser.add_subparsers(dest="action")

    ## show ##
    vault_show_parser = vault_subparsers.add_parser("show", help="Show vault")
    vault_show_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")

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
    savings_show_parser = savings_subparsers.add_parser("show", help="Show vault")
    savings_show_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")

    ## deposit ##
    savings_deposit_subparser = savings_subparsers.add_parser("deposit", help="Deposit to vault")
    savings_deposit_subparser.add_argument("AMOUNT", type=float, help="Amount of BYC to deposit")

    ## withdraw ##
    savings_withdraw_subparser = savings_subparsers.add_parser("withdraw", help="Withdraw from vault")
    savings_withdraw_subparser.add_argument("AMOUNT", type=float, help="Amount of BYC to withdraw")

    # TODO: liq auction, recovering bad debt
    # TODO: surplus + recharge auctions

    args = parser.parse_args()
    rpc_client = CircuitRPCClient(args.base_url, args.private_key, args.add_sig_data, args.fee_per_cost)
    try:
        kwargs = dict(vars(args))
        #print(kwargs)
        function_name = f"{args.command}_{args.action}"
        if "subaction" in kwargs.keys():
            function_name += f"_{args.subaction}"
        del kwargs["command"]
        del kwargs["action"]
        kwargs.pop("subaction", None)
        del kwargs["base_url"]
        del kwargs["private_key"]
        del kwargs["add_sig_data"]
        del kwargs["fee_per_cost"]
        if args.command == "announcer" and args.action == "fasttrack":
            # special case for fasttrack
            result = await announcer_fasttrack(rpc_client, **kwargs)
        else:
            # run commands method dynamically based on the parser command
            print(f"running {function_name} with kwargs {kwargs}")
            result = await getattr(rpc_client, f"{function_name}")(**kwargs)

        if isinstance(result, dict) and "bundle" in result.keys() and "status" in result.keys():
            # we assume we are dealing with a spend bundle that was broadcast
            # all we care about is whether broadcast was successful or not
            print(f"Command status: {result['status']}")
        elif isinstance(result, dict):
            if "human_readable" in result.keys():
                del result["human_readable"]
                result = make_human_readable(result)
            pprint.pprint(result)
        elif isinstance(result, list):
            results = []
            for r in result:
                if "human_readable" in r.keys():
                    del r["human_readable"]
                    results.append(make_human_readable(r))
                else:
                    results.append(r)
            pprint.pprint(results)
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
