import argparse
import asyncio
import os
import math
from datetime import datetime
import pprint

from chia.types.spend_bundle import SpendBundle
from clvm_rs.casts import int_from_bytes

from circuit_cli.client import CircuitRPCClient, MOJOS

from clvm_tools.binutils import disassemble


async def get_announcer_name(rpc_client, launcher_id: str = None):
    data = await rpc_client.announcer_show()
    if not launcher_id:
        return data[0]["launcher_id"], data[0]["name"]
    for announcer in data:
        if announcer["launcher_id"] == launcher_id:
            return launcher_id, announcer["name"]
    raise ValueError(f"Announcer with launcher_id {launcher_id.hex()} not found")


async def announcer_fasttrack(rpc_client, PRICE: int, launcher_id: str = None):
    print("Fasttracking announcer")
    if not launcher_id:
        #assert price > 1000
        print("Launching announcer...")
        resp = await rpc_client.announcer_launch(price=PRICE)
        print("Waiting for time to pass to approve announcer (farm blocks if in simulator)...")
        bundle = SpendBundle.from_json_dict(resp["bundle"])
        print("Approving announcer...")
        await rpc_client.wait_for_confirmation(bundle)
        print("Announcer approved.")
    launcher_id, coin_name = await get_announcer_name(rpc_client, launcher_id)
    print(f"announcer {launcher_id=} {coin_name=}")
    statutes = await rpc_client.statutes_list(full=True)
    # find min deposit amount
    min_deposit = int(statutes["implemented_statutes"]["ANNOUNCER_MINIMUM_DEPOSIT"]) #int_from_bytes(bytes.fromhex(statutes["enacted_statutes"]["ANNOUNCER_MINIMUM_DEPOSIT"]))
    max_ttl = int(statutes["implemented_statutes"]["ANNOUNCER_PRICE_TTL"]) #int_from_bytes(bytes.fromhex(statutes["enacted_statutes"]["ANNOUNCER_PRICE_TTL"]))
    print(f"configuring announcer with deposit={min_deposit/MOJOS} XCH, min_deposit={min_deposit/MOJOS} XCH, ttl={max_ttl-10} seconds")
    resp = await rpc_client.announcer_configure(coin_name, deposit=min_deposit/MOJOS, min_deposit=min_deposit/MOJOS, price=PRICE, ttl=max_ttl-10)
    bundle = SpendBundle.from_json_dict(resp["bundle"])
    await rpc_client.wait_for_confirmation(bundle)
    print("announcer configured")
    # approve announcer
    launcher_id, announcer_coin_name = await get_announcer_name(rpc_client, launcher_id)
    vote_data = await rpc_client.upkeep_announcers_approve(
        announcer_coin_name,
        #approve=True,
        create_conditions=True,
    )
    voting_anns = vote_data["announcements_to_vote_for"]
    bills = await rpc_client.bills_list()
    bill_name = bills[0]["name"]
    resp = await rpc_client.bills_propose(
        INDEX=-1,
        VALUE=voting_anns,
        coin_name=bill_name,
        force=True,
    )
    bundle = SpendBundle.from_json_dict(resp["bundle"])
    await rpc_client.wait_for_confirmation(bundle)
    print("bill to approve announcer proposed")
    bills = await rpc_client.bills_list()
    bill_name = bills[0]["name"]
    print("Waiting for time to pass to implement bill (farm blocks if in simulator)...")
    await rpc_client.wait_for_confirmation(blocks=1)
    print("Implementation delay has passsed, implementing bill")
    launcher_id, coin_name = await get_announcer_name(rpc_client, launcher_id)
    # implementing announcer approval
    resp = await rpc_client.upkeep_announcers_approve(
        coin_name,
        #approve=True,
        implement_bill_name=bill_name,
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
    upkeep_state_parser = upkeep_subparsers.add_parser(
        "state", help="Show protocol state",
        description="Displays current state of protocol coins that are relevenat for keepers, governance and announcers. By default, the whole protocol state is shown."
    )
    upkeep_state_parser.add_argument("-v", "--vaults", action="store_true", help="Show collateral vaults")
    upkeep_state_parser.add_argument("-r", "--recharge-auctions", action="store_true", help="Show recharge auctions")
    upkeep_state_parser.add_argument("-s", "--surplus-auctions", action="store_true", help="Show surplus auctions")
    upkeep_state_parser.add_argument("-t", "--treasury", action="store_true", help="Show treasury")
    upkeep_state_parser.add_argument("-b", "--bills", action="store_true", help="Show governance coins with non-empty bills")

    ## RPC server ##
    upkeep_rpc_parser = upkeep_subparsers.add_parser("rpc", help="Info on Circuit RPC server")
    upkeep_rpc_subparsers = upkeep_rpc_parser.add_subparsers(dest="subaction")
    upkeep_rpc_subparsers.add_parser("status", help="Status of Circuit RPC server") # LATER: implement. display block height and hash synced to.
    upkeep_rpc_subparsers.add_parser("sync", help="Synchronize Circuit RPC server with Chia blockchain")
    upkeep_rpc_subparsers.add_parser("version", help="Version of Circuit RPC server")

    ## announcers ##
    upkeep_announcers_parser = upkeep_subparsers.add_parser("announcers", help="Commands to manage announcers")
    upkeep_announcers_subparsers = upkeep_announcers_parser.add_subparsers(dest="subaction")
    upkeep_announcers_list_parser = upkeep_announcers_subparsers.add_parser(
        "list", help="List announcers",
        description="Lists approved announcers. If COIN_NAME is specified, info for only this one announcer will be shown, whether approved or not"
    )
    upkeep_announcers_list_parser.add_argument("COIN_NAME", nargs="?", type=str, default=None, help="[optional] Name of announcer")
    upkeep_announcers_list_parser.add_argument("-p", "--penalizable", action="store_true", help="List penalizable announcers")
    upkeep_announcers_list_parser.add_argument("-v", "--valid", action="store_true", help="List valid announcers (approved and not expired)")
    upkeep_announcers_list_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")
    upkeep_announcers_approve_parser = upkeep_announcers_subparsers.add_parser(
        "approve",
        help="Approve an announcer",
        description="Approves an announcer to be used for oracle price updates.",
    )
    upkeep_announcers_approve_parser.add_argument("COIN_NAME", type=str, help="Name of announcer")
    upkeep_announcers_approve_parser.add_argument(
        "-c", "--create-conditions", action="store_true", help="Create custom conditions to propose bill (no spend bundle)"
    )
    upkeep_announcers_approve_parser.add_argument(
        "-i", "--implement-bill-name", type=str, default=None,
        help="Implement the previously proposed bill containing custom conditions to approve announcer"
    )
    upkeep_announcers_disapprove_parser = upkeep_announcers_subparsers.add_parser(
        "disapprove", help="Disapprove an announcer", description="Disapproves an announcer so that it can no longer be used for oracle price updates.",
    )
    upkeep_announcers_disapprove_parser.add_argument("COIN_NAME", type=str, help="Name of announcer")
    upkeep_announcers_disapprove_parser.add_argument(
        "-c", "--create-conditions", action="store_true", help="Create custom conditions for bill only, no spend bundle"
    )
    upkeep_announcers_disapprove_parser.add_argument(
        "-i", "--implement-bill-name", type=str, default=None,
        help="Implement the previously proposed bill containing custom conditions to disapprove announcer"
    )
    upkeep_announcers_penalize_parser = upkeep_announcers_subparsers.add_parser(
        "penalize", help="Penalize an announcer", description="Penalizes an announcer.",
    )
    upkeep_announcers_penalize_parser.add_argument("COIN_NAME", nargs="?", type=str, default=None, help="[optional] Name of announcer")

    ## bills ##
    upkeep_bills_parser = upkeep_subparsers.add_parser("bills", help="Info on governance coins")
    upkeep_bills_subparsers = upkeep_bills_parser.add_subparsers(dest="subaction")
    upkeep_bills_list_parser = upkeep_bills_subparsers.add_parser("list", help="List governance coins", description="Lists all governance coins.")
    upkeep_bills_list_parser.add_argument("-x", "--exitable", action="store_true", default=None, help="Only list empty governance coins that can be exited")
    upkeep_bills_list_parser.add_argument("-e", "--empty", action="store_true", default=None, help="Only list empty governance coins, ie those with bill equal to nil")
    upkeep_bills_list_parser.add_argument("-n", "--non-empty", action="store_true", default=None, help="Only list non-empty governance coins, ie those with bill not equal to nil")
    upkeep_bills_list_parser.add_argument("-v", "--vetoable", action="store_true", default=None, help="Only list governance coins with vetoable bill")
    upkeep_bills_list_parser.add_argument("-c", "--enacted", action="store_true", default=None, help="Only list governance coins with enacted bill (enacted = no longer vetoable, incl lapsed)")
    upkeep_bills_list_parser.add_argument("-d", "--in-implementation-delay", default=None, action="store_true", help="Only list governance coins with bill in implementation delay")
    upkeep_bills_list_parser.add_argument("-i", "--implementable", action="store_true", default=None, help="Only list governance coins with implementable bill")
    upkeep_bills_list_parser.add_argument("-l", "--lapsed", action="store_true", default=None, help="Only list governance coins with lapsed bill")
    upkeep_bills_list_parser.add_argument("-s", "--statute-index", action="store_true", default=None, help="Only list governance coins with bill for specified statute index")
    upkeep_bills_list_parser.add_argument("-b", "--bill", action="store_true", default=None, help="Only list governance coins with given bill (excl propsal times). Specify as program in hex format")
    upkeep_bills_list_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")
    upkeep_bills_list_parser.add_argument("--incl-spent", action="store_true", help="Include spent governance coins")

    ## registry ##
    upkeep_registry_parser = upkeep_subparsers.add_parser("registry", help="Announcer Registry commands")
    upkeep_registry_subparsers = upkeep_registry_parser.add_subparsers(dest="subaction")
    upkeep_registry_subparsers.add_parser("show", help="Show Announcer Registry", description="Shows Announcer Registry.")
    upkeep_registry_reward_parser = upkeep_registry_subparsers.add_parser("reward", help="Distribute CRT Rewards", description="Distributes CRT Rewards to registered Announcers.")
    upkeep_registry_reward_parser.add_argument("-i", "--info", action="store_true", help="Show info on whether rewards can be distributed")

    ## recharge auctions ##
    upkeep_recharge_parser = upkeep_subparsers.add_parser("recharge", help="Participate in recharge auctions", description="Commands to participate in recharge auctions.")
    upkeep_recharge_subparsers = upkeep_recharge_parser.add_subparsers(dest="subaction")
    upkeep_recharge_list_parser = upkeep_recharge_subparsers.add_parser("list", help="List recharge auctions", description="Lists recharge auctions.")
    upkeep_recharge_list_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")
    upkeep_recharge_launch_parser = upkeep_recharge_subparsers.add_parser("launch", help="Launch a recharge auction coin", description="Creates and launches a new recharge auction coin.")
    upkeep_recharge_launch_parser.add_argument(
        "-c", "--create-conditions", action="store_true", help="Create custom conditions for bill only, no spend bundle",
    )
    upkeep_recharge_launch_parser.add_argument(
        "-i", "--implement-bill-name", type=str, default=None,
        help="Implement the previously proposed bill containing custom conditions to launch recharge auction coin",
    )
    upkeep_recharge_start_parser = upkeep_recharge_subparsers.add_parser("start", help="Start a recharge auction", description="Starts a recharge auction.")
    upkeep_recharge_start_parser.add_argument("COIN_NAME", type=str, help="Name of recharge auction coin") # TODO: make optional arg
    upkeep_recharge_bid_parser = upkeep_recharge_subparsers.add_parser("bid", help="Bid in a recharge auction", description="Submits a bid in a recharge auction.")
    upkeep_recharge_bid_parser.add_argument("COIN_NAME", type=str, help="Name of recharge auction coin")
    upkeep_recharge_bid_parser.add_argument("CRT_AMOUNT", type=float, help="Amount of CRT to request")
    upkeep_recharge_bid_parser.add_argument("BYC_AMOUNT", nargs="?", type=float, default=None, help="[optional] Amount of BYC to bid. Default is minimum possible.")
    upkeep_recharge_settle_parser = upkeep_recharge_subparsers.add_parser("settle", help="Settle a recharge auction", description="Settles a recharge auction.")
    upkeep_recharge_settle_parser.add_argument("COIN_NAME", type=str, help="Name of recharge auction coin")

    ## surplus auctions ##
    upkeep_surplus_parser = upkeep_subparsers.add_parser("surplus", help="Participate in surplus auctions", description="Commands to participate in surplus auctions.")
    upkeep_surplus_subparsers = upkeep_surplus_parser.add_subparsers(dest="subaction")
    # TODO: provide info on when bid expires
    upkeep_surplus_list_parser = upkeep_surplus_subparsers.add_parser("list", help="List surplus auctions", description="Lists surplus auctions.")
    upkeep_surplus_list_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")
    upkeep_surplus_start_parser = upkeep_surplus_subparsers.add_parser("start", help="Start a surplus auction", description="Starts a surplus auction.")
    upkeep_surplus_bid_parser = upkeep_surplus_subparsers.add_parser("bid", help="Bid in a surplus auction", description="Submits a bid in a surplus auction.")
    upkeep_surplus_bid_parser.add_argument("COIN_NAME", type=str, help="Name of surplus auction coin")
    upkeep_surplus_bid_parser.add_argument("AMOUNT", type=float, help="Amount of CRT to bid")
    upkeep_surplus_settle_parser = upkeep_surplus_subparsers.add_parser("settle", help="Settle a surplus auction", description="Settles a surplus auction.")
    upkeep_surplus_settle_parser.add_argument("COIN_NAME", type=str, help="Name of surplus auction coin")

    ## treasury ##
    upkeep_treasury_parser = upkeep_subparsers.add_parser("treasury", help="Manage treasury", description="Commands to manage protocol treasury.")
    upkeep_treasury_subparsers = upkeep_treasury_parser.add_subparsers(dest="subaction")
    upkeep_treasury_rebalance_parser = upkeep_treasury_subparsers.add_parser("rebalance", help="Rebalance treasury", description="Redistributes BYC evenly across all treasury coins.")
    upkeep_treasury_rebalance_parser.add_argument("-i", "--info", action="store_true", help="Show info on whether treasury can be rebalanced")
    upkeep_treasury_show_parser = upkeep_treasury_subparsers.add_parser("show", help="Show treasury", description="Shows information on treasury.")
    upkeep_treasury_show_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")
    upkeep_treasury_launch_parser = upkeep_treasury_subparsers.add_parser(
        "launch",
        help="Launch a treasury coin",
        description="Creates and launches a new treasury coin into the treasury ring.",
    )
    upkeep_treasury_launch_parser.add_argument(
        "SUCCESSOR_LAUNCHER_ID", nargs="?", type=str, default=None,
        help="[optional] Launcher ID of the coin that will succeed the newly launched coin in treasury ring",
    )
    upkeep_treasury_launch_parser.add_argument(
        "-c", "--create-conditions", action="store_true", help="Create custom conditions for bill only, no spend bundle",
    )
    upkeep_treasury_launch_parser.add_argument(
        "-i", "--implement-bill-name", type=str, default=None,
        help="Implement the previously proposed bill containing custom conditions to launch treasury coin",
    )

    ## vaults ##
    upkeep_vaults_parser = upkeep_subparsers.add_parser("vaults", help="Manage insufficiently collateralized debt positions")
    upkeep_vaults_subparsers = upkeep_vaults_parser.add_subparsers(dest="subaction")
    upkeep_vaults_list_parser = upkeep_vaults_subparsers.add_parser("list", help="List all vaults", description="Shows information on all collateral vaults.")
    upkeep_vaults_list_parser.add_argument("COIN_NAME", nargs="?", type=str, default=None, help="[optional] Name of vault coin. If specified, info for only this vault is shown")
    upkeep_vaults_list_parser.add_argument("-s", "--seized", action="store_true", help="Only list seized vaults (seized = in liquidation or bad debt)")
    upkeep_vaults_list_parser.add_argument("-n", "--not-seized", action="store_true", help="Only list non-seized vaults")
    upkeep_vaults_list_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")
    # LATER: add option for only listing liquidatable/in liquidation/restartable/in bad debt vaults
    # LATER: add -o/--ordered arg to order by outstanding SFs
    upkeep_vaults_transfer_parser = upkeep_vaults_subparsers.add_parser("transfer", help="Transfer stability fees from vault to treasury", description="Transfers stability fees from specified collateral vault to treasury.")
    upkeep_vaults_transfer_parser.add_argument("COIN_NAME", nargs="?", type=str, default=None, help="[optional] Name of vault coin. If not specified, vault with greatest amount of SFs to transfer is selected")
    upkeep_vaults_liquidate_parser = upkeep_vaults_subparsers.add_parser("liquidate", help="Liquidate a vault", description="Starts or restarts a liquidation auction.")
    upkeep_vaults_liquidate_parser.add_argument("COIN_NAME", type=str, help="Name of vault to liquidate")
    upkeep_vaults_bid_parser = upkeep_vaults_subparsers.add_parser("bid", help="Bid in a liquidation auction", description="Submits a bid in a liquidation auction.")
    upkeep_vaults_bid_parser.add_argument("COIN_NAME", type=str, help="Name of vault in liquidation")
    upkeep_vaults_bid_parser.add_argument("AMOUNT", type=float, help="Amount of BYC to bid")
    #upkeep_vaults_auction_parser.add_argument("-s", "--start", action="store_true", help="Start or restart a liquidation auction")
    #upkeep_vaults_auction_parser.add_argument("-b", "--bid-amount", type=int, help="Submit a bid in a liquidation auction. Specify bid amount in mBYC")
    upkeep_vaults_recover_parser = upkeep_vaults_subparsers.add_parser("recover", help="Recover bad debt", description="Recovers bad debt from a collateral vault.")
    upkeep_vaults_recover_parser.add_argument("COIN_NAME", type=str, help="Vault ID")

    ### BILLS ###
    bills_parser = subparsers.add_parser("bills", help="Command to manage bills and governance")
    bills_subparsers = bills_parser.add_subparsers(dest="action")

    ## list ##
    bills_list_parser = bills_subparsers.add_parser("list", help="List governance coins", description="Lists governance coins of user.")
    bills_list_parser.add_argument("-x", "--exitable", action="store_true", default=None, help="Governance coins that can be exited")
    bills_list_parser.add_argument("-e", "--empty", action="store_true", default=None, help="Empty governance coins, ie those with bill equal to nil")
    bills_list_parser.add_argument("-n", "--non-empty", action="store_true", default=None, help="Non-empty governance coins, ie those with bill not equal to nil")
    bills_list_parser.add_argument("-v", "--vetoable", action="store_true", default=None, help="Governance coins with vetoable bill")
    bills_list_parser.add_argument("-c", "--enacted", action="store_true", default=None, help="Governance coins with enacted bill (enacted = no longer vetoable, incl lapsed)")
    bills_list_parser.add_argument("-d", "--in-implementation-delay", default=None, action="store_true", help="Governance coins with bill in implementation delay")
    bills_list_parser.add_argument("-i", "--implementable", action="store_true", default=None, help="Governance coins with implementable bill")
    bills_list_parser.add_argument("-l", "--lapsed", action="store_true", default=None, help="Governance coins with lapsed bill")
    bills_list_parser.add_argument("-s", "--statute-index", type=int, help="Governance coins with bill to change specified statute")
    bills_list_parser.add_argument("-b", "--bill", type=str, help="Governance coins with specified bill (excl propsal times). Must be program in hex format")
    bills_list_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")
    bills_list_parser.add_argument("--incl-spent", action="store_true", help="Include spent governance coins")

    ## toggle governance mode ##
    bills_toggle_parser = bills_subparsers.add_parser(
        "toggle", help="Convert a plain CRT coin into a governance coin or vice versa",
        description="If coin is in governance mode, exit to plain CRT. If coin is plain CRT, activate governance mode."
    )
    bills_toggle_parser.add_argument("COIN_NAME", type=str, help="Coin name")
    bills_toggle_parser.add_argument("-i", "--info", action="store_true", help="Show info on toggling governance mode")
    bills_toggle_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")

    ## propose ##
    bills_propose_parser = bills_subparsers.add_parser("propose", help="Propose a new bill")
    bills_propose_parser.add_argument("INDEX", type=int, help="Statute index. Specify -1 for custom conditions")
    bills_propose_parser.add_argument("VALUE", nargs="?", default=None, type=str, help="Value of bill, ie Statute value or custom announcements. Omit to keep current value. Must be a Program in hex format if INDEX = -1, a 32-byte hex string if INDEX = 0, and an integer otherwise")
    bills_propose_parser.add_argument("-id", "--coin-name", default=None, type=str, help="Governance coin to use for proposal. If not specified, a suitable coin is chosen automatically")
    bills_propose_parser.add_argument("-f", "--force", action="store_true", help="Propose bill even if resulting Statutes would not be consistent")
    bills_propose_parser.add_argument("--proposal-threshold", default=None, type=int, help="Min amount of CRT required to propose new Statute value")
    bills_propose_parser.add_argument("--veto-seconds", type=int, default=None, help="Veto period in seconds")
    bills_propose_parser.add_argument("--delay-seconds", type=int, default=None, help="Implementation delay in seconds")
    bills_propose_parser.add_argument("--max-delta", type=int, default=None, help="Max absolute amount in bps by which Statues value may change")

    ## implement ##
    bills_implement_subparser = bills_subparsers.add_parser("implement", help="Implement a bill into statute", description="Implement a bill.")
    bills_implement_subparser.add_argument("COIN_NAME", nargs="?", default=None, type=str, help="[optional] Coin name of bill to implement")
    bills_implement_subparser.add_argument("-i", "--info", action="store_true", help="Show info on when next bill can be implemented")
    bills_implement_subparser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")

    ## reset ##
    bills_reset_subparser = bills_subparsers.add_parser("reset", help="Reset a bill", description="Sets bill of a governance coin to nil.")
    bills_reset_subparser.add_argument("COIN_NAME", type=str, help="Coin name")

    ### WALLET ###
    wallet_parser = subparsers.add_parser("wallet", help="Wallet commands")
    wallet_subparsers = wallet_parser.add_subparsers(dest="action")

    ## balances ##
    wallet_balances_parser = wallet_subparsers.add_parser("balances", help="Get wallet balances")
    wallet_balances_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")

    ## coins ##
    wallet_coins_parser = wallet_subparsers.add_parser("coins", help="Get wallet coins")
    wallet_coins_parser.add_argument("-t", "--type", type=str.lower, choices=["byc", "crt", "xch"], help="Return coins of given type only")
    wallet_coins_parser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")

    ### ANNOUNCER ###
    announcer_parser = subparsers.add_parser("announcer", help="Announcer commands")
    announcer_subparsers = announcer_parser.add_subparsers(dest="action")

    ## launch ##
    announcer_launch_parser = announcer_subparsers.add_parser("launch", help="Launch an announcer")
    announcer_launch_parser.add_argument("-p", "--price", type=int, help="Initial price")

    ## fasttrack (launch + approve) ##
    announcer_fasttrack_parser = announcer_subparsers.add_parser(
        "fasttrack", help="Launch and approve an announcer", description="Launches and approves or approves an announcer. Requires a governance coin with empty bill to be available."
    )
    announcer_fasttrack_parser.add_argument("PRICE", type=int, help="New announcer price")
    announcer_fasttrack_parser.add_argument(
        "--launcher-id", type=str, help="Announcer launcher ID. Specify when announcer has already been launched but not approved yet"
    )

    ## show ##
    announcer_show_subparser = announcer_subparsers.add_parser("show", help="Show information on announcer", description="Shows information on announcer.")
    announcer_show_subparser.add_argument("-a", "--approved", action="store_true", help="Show announcer only if approved")
    announcer_show_subparser.add_argument("-v", "--valid", action="store_true", help="Show announcer only if valid (approved and not expired)")
    announcer_show_subparser.add_argument("-p", "--penalizable", action="store_true", help="Show announcer only if penalizable")
    announcer_show_subparser.add_argument("--incl-spent", action="store_true", help="Include spent announcer coins")
    announcer_show_subparser.add_argument("-u", "--human-readable", action="store_true", help="Display numbers in human readable format")

    ## update price ##
    announcer_update_parser = announcer_subparsers.add_parser("update", help="Update announcer price", description="Updates the announcer price. The puzzle automatically updates the expiry timestamp.")
    announcer_update_parser.add_argument("PRICE", type=int, help="New announcer price")
    announcer_update_parser.add_argument("COIN_NAME", nargs="?", type=str, default=None, help="[optional] Announcer coin name. Only required if user owns more than one announcer")

    ## configure ##
    announcer_configure_parser = announcer_subparsers.add_parser("configure", help="Configure the announcer", description="Configures the announcer.")
    announcer_configure_parser.add_argument("COIN_NAME", nargs="?", type=str, default=None, help="[optional] Announcer coin name. Only required if user owns more than one announcer")
    announcer_configure_parser.add_argument("--deposit", type=float, help="New deposit amount in XCH")
    announcer_configure_parser.add_argument("--min-deposit", type=float, help="New minimum deposit amount in XCH")
    announcer_configure_parser.add_argument("--inner-puzzle-hash", type=int, help="New inner puzzle hash (re-key)")
    announcer_configure_parser.add_argument("--price", type=int, help="New announcer price. If only updating price, it's more effcient to use 'update' operation")
    announcer_configure_parser.add_argument("--ttl", type=int, help="New time to live in seconds")
    announcer_configure_parser.add_argument("-d", "--deactivate", action="store_true", help="Deactivate announcer")

    ## register ##
    announcer_register_parser = announcer_subparsers.add_parser("register", help="Register an Announcer", description="Registers an Announcer with Announcer Registry to be eligible for CRT Rewards.")
    announcer_register_parser.add_argument("COIN_NAME", nargs="?", type=str, default=None, help="[optional] Announcer coin name. Only required if user owns more than one announcer")

    ## exit ##
    announcer_exit_parser = announcer_subparsers.add_parser("exit", help="Exit announcer layer", description="Exit announcer layer by melting announcer into plain XCH coin.")
    announcer_exit_parser.add_argument("COIN_NAME", nargs="?", type=str, default=None, help="[optional] Announcer coin name. Only required if user owns more than one announcer")

    ### ORACLE ###
    oracle_parser = subparsers.add_parser("oracle", help="Oracle commands")
    oracle_subparsers = oracle_parser.add_subparsers(dest="action")

    ## show ##
    oracle_subparsers.add_parser("show", help="Show oracle prices", description="Shows oracle prices.")

    ## update price ##
    oracle_update_parser = oracle_subparsers.add_parser("update", help="Update oracle price", description="Adds new price to Oracle price queue.")
    oracle_update_parser.add_argument("-i", "--info", action="store_true", help="Show info on whether Oracle can be updated")

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
