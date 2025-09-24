import argparse
import asyncio
import json
import logging
import os
import sys

import httpx
from circuit_cli.client import CircuitRPCClient

log = logging.getLogger(__name__)


def fee_per_cost_type(value: str):
    """Custom argparse type for fee_per_cost argument."""
    value_lower = value.lower()
    if value_lower in ("fast", "medium"):
        return value_lower
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid fee per cost. Must be 'fast', 'medium', or an integer."
        )


async def cli():
    parser = argparse.ArgumentParser(description="Circuit CLI tool")
    subparsers = parser.add_subparsers(dest="command")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("-dd", type=str, help="Set persistence directory")
    parser.add_argument(
        "--base-url",
        type=str,
        default=os.environ.get("BASE_URL", "http://localhost:8000"),
        help="Base URL for the Circuit RPC API server",
    )
    parser.add_argument(
        "--add-sig-data",
        type=str,
        default=os.environ.get(
            "ADD_SIG_DATA",
            "ccd5bb71183532bff220ba46c268991a3ff07eb358e8255a65c30a2dce0e5fbb",  # simulator0 & mainnet
        ),
        help="Additional signature data",
    )
    parser.add_argument(
        "--no-wait", type=str, default=os.environ.get("NO_WAIT_TX", ""), help="Don't wait for tx to be confirmed."
    )
    parser.add_argument(
        "--fee-per-cost",
        "-fpc",
        type=fee_per_cost_type,
        default=os.environ.get("FEE_PER_COST", "fast"),
        help="Set transaction fee: 'fast', 'medium' or number of mojos per cost. "
        "Fast mode aims to get transactions confirmed in next 1-2 blocks, medium up to 5 blocks.",
    )
    parser.add_argument(
        "--private-key", "-p", type=str, default=os.environ.get("PRIVATE_KEY"), help="Private key for your coins"
    )
    parser.add_argument("-j", "--json", action="store_true", help="Return JSON instead of human readable output")
    parser.add_argument(
        "--progress",
        choices=["off", "text", "json"],
        default=os.environ.get("CIRCUIT_CLI_PROGRESS", "text"),
        help="Stream progress while waiting for confirmations: 'off' (default), 'text' for human output, 'json' for JSONL events",
    )

    ### UPKEEP ###
    upkeep_parser = subparsers.add_parser("upkeep", help="Commands to upkeep protocol and RPC server")
    upkeep_subparsers = upkeep_parser.add_subparsers(dest="action")

    ## little liquidator
    upkeep_little_liquidator_parser = upkeep_subparsers.add_parser(
        "liquidator",
        help="Little Liquidator - simple vault liquidation bot",
        description="Monitors for any vaults that are ready to be liquidated and liquidates "
        "them if possible with optional profit-taking.",
    )
    upkeep_little_liquidator_parser.add_argument(
        "--max-bid-amount", "-ma", type=int, required=False, help="Maximum bid amount"
    )
    upkeep_little_liquidator_parser.add_argument(
        "--min-discount",
        "-mpd",
        type=float,
        required=False,
        help="Minimum price discount relative to market price to bid",
    )
    upkeep_little_liquidator_parser.add_argument(
        "--run-once", action="store_true", help="Run the liquidator once and exit"
    )
    upkeep_little_liquidator_parser.add_argument(
        "--max-offer-amount", "-moa", type=float, default=1.0, help="Maximum XCH amount per offer (for coin splitting)"
    )
    upkeep_little_liquidator_parser.add_argument(
        "--offer-expiry-seconds", "-oes", type=int, default=600, help="Offer expiry time in seconds (default: 600)"
    )
    upkeep_little_liquidator_parser.add_argument(
        "--current-time",
        "-ct",
        type=float,
        default=None,
        help="Override current time (epoch seconds) for testing. If omitted, uses system time.",
    )

    ## protocol info ##
    upkeep_subparsers.add_parser(
        "invariants",
        help="Show protocol invariants",
        description="Shows BYC and CRT asset IDs and various other invariants.",
    )  # LATER: added launcher ID of Statutes (& Oracle?)

    ## protocol state ##
    upkeep_state_parser = upkeep_subparsers.add_parser(
        "state",
        help="Show information on pending operations and protocol state",
        description="Displays current state of protocol coins that are relevenat for keepers, governance and announcers. By default, all information is shown.",
    )
    upkeep_state_parser.add_argument("-v", "--vaults", action="store_true", help="Show collateral vaults")
    upkeep_state_parser.add_argument("-r", "--recharge-auctions", action="store_true", help="Show recharge auctions")
    upkeep_state_parser.add_argument("-s", "--surplus-auctions", action="store_true", help="Show surplus auctions")
    upkeep_state_parser.add_argument("-t", "--treasury", action="store_true", help="Show treasury")
    upkeep_state_parser.add_argument(
        "-b", "--bills", action="store_true", help="Show governance coins with non-empty bills"
    )

    ## RPC server ##
    upkeep_rpc_parser = upkeep_subparsers.add_parser("rpc", help="Info on Circuit RPC server")
    upkeep_rpc_subparsers = upkeep_rpc_parser.add_subparsers(dest="subaction")
    upkeep_rpc_subparsers.add_parser(
        "status",
        help="Status of Circuit RPC server",
        description="Shows synchronization status of backend database with blockchain",
    )
    upkeep_rpc_subparsers.add_parser("sync", help="Synchronize Circuit RPC server with Chia blockchain")
    upkeep_rpc_subparsers.add_parser("version", help="Version of Circuit RPC server")

    ## announcers ##
    upkeep_announcers_parser = upkeep_subparsers.add_parser("announcers", help="Commands to manage announcers")
    upkeep_announcers_subparsers = upkeep_announcers_parser.add_subparsers(dest="subaction")
    upkeep_announcers_list_parser = upkeep_announcers_subparsers.add_parser(
        "list",
        help="List announcers",
        description="Lists approved announcers. If coin_name is specified, info for only this announcer will be shown, whether approved or not.",
    )
    upkeep_announcers_list_parser.add_argument(
        "coin_name",
        nargs="?",
        type=str,
        default=None,
        help="[optional] Name or launcher ID of announcer coin"
    )
    upkeep_announcers_list_parser.add_argument(
        "-v", "--valid", action="store_true", help="List valid announcers only (approved and not expired)"
    )
    upkeep_announcers_list_parser.add_argument(
        "-nv", "--invalid", action="store_true", help="List invalid announcers only (approved and not expired)"
    )
    upkeep_announcers_list_parser.add_argument(
        "-p", "--penalizable", action="store_true", help="List penalizable announcers only"
    )
    upkeep_announcers_list_parser.add_argument(
        "-np", "--non-penalizable", action="store_true", help="List non-penalizable announcers only"
    )
    upkeep_announcers_list_parser.add_argument(
        "-r", "--registered", action="store_true", help="List registered announcers only"
    )
    upkeep_announcers_list_parser.add_argument(
        "-nr", "--unregistered", action="store_true", help="List unregistered announcers only"
    )
    upkeep_announcers_list_parser.add_argument(
        "-a", "--approved", action="store_true", help="List specified announcer only if approved"
    )
    upkeep_announcers_list_parser.add_argument(
        "-na", "--unapproved", action="store_true", help="List specified announcer only if not approved"
    )
    upkeep_announcers_list_parser.add_argument(
        "--incl-spent", action="store_true", help="Include spent announcer coins"
    )
    upkeep_announcers_approve_parser = upkeep_announcers_subparsers.add_parser(
        "approve",
        help="Approve an announcer",
        description="Approves an announcer to be used for oracle price updates.",
    )
    upkeep_announcers_approve_parser.add_argument("coin_name", type=str, help="Name or launcher ID of announcer")
    upkeep_announcers_approve_parser.add_argument(
        "-c",
        "--create-conditions",
        action="store_true",
        help="Create custom conditions for bill to approve announcer",
    )
    upkeep_announcers_approve_parser.add_argument(
        "-b",
        "--bill-coin-name",
        type=str,
        help="Implement previously proposed bill to approve announcer",
    )
    upkeep_announcers_disapprove_parser = upkeep_announcers_subparsers.add_parser(
        "disapprove",
        help="Disapprove an announcer",
        description="Disapproves an announcer so that it can no longer be used for oracle price updates.",
    )
    upkeep_announcers_disapprove_parser.add_argument("coin_name", type=str, help="Name or launcher ID of announcer")
    upkeep_announcers_disapprove_parser.add_argument(
        "-c",
        "--create-conditions",
        action="store_true",
        help="Create custom conditions for bill to disapprove announcer",
    )
    upkeep_announcers_disapprove_parser.add_argument(
        "-b",
        "--bill-coin-name",
        type=str,
        help="Implement previously proposed bill to disapprove announcer",
    )
    upkeep_announcers_penalize_parser = upkeep_announcers_subparsers.add_parser(
        "penalize",
        help="Penalize an announcer",
        description="Penalizes an announcer.",
    )
    upkeep_announcers_penalize_parser.add_argument(
        "coin_name", nargs="?", type=str, default=None, help="[optional] Name or launcher ID of announcer"
    )

    ## bills ##
    upkeep_bills_parser = upkeep_subparsers.add_parser("bills", help="Info on governance coins")
    upkeep_bills_subparsers = upkeep_bills_parser.add_subparsers(dest="subaction")
    upkeep_bills_list_parser = upkeep_bills_subparsers.add_parser(
        "list", help="List governance coins", description="Lists all governance coins."
    )
    upkeep_bills_list_parser.add_argument(
        "-x",
        "--exitable",
        action="store_true",
        default=None,
        help="Only list empty governance coins that can be exited",
    )
    upkeep_bills_list_parser.add_argument(
        "-e",
        "--empty",
        action="store_true",
        default=None,
        help="Only list empty governance coins, ie those with bill equal to nil",
    )
    upkeep_bills_list_parser.add_argument(
        "-n",
        "--non-empty",
        action="store_true",
        default=None,
        help="Only list non-empty governance coins, ie those with bill not equal to nil",
    )
    upkeep_bills_list_parser.add_argument(
        "-v", "--vetoable", action="store_true", default=None, help="Only list governance coins with vetoable bill"
    )
    upkeep_bills_list_parser.add_argument(
        "-c",
        "--enacted",
        action="store_true",
        default=None,
        help="Only list governance coins with enacted bill (enacted = no longer vetoable, incl lapsed)",
    )
    upkeep_bills_list_parser.add_argument(
        "-d",
        "--in-implementation-delay",
        default=None,
        action="store_true",
        help="Only list governance coins with bill in implementation delay",
    )
    upkeep_bills_list_parser.add_argument(
        "-i",
        "--implementable",
        action="store_true",
        default=None,
        help="Only list governance coins with implementable bill",
    )
    upkeep_bills_list_parser.add_argument(
        "-l", "--lapsed", action="store_true", default=None, help="Only list governance coins with lapsed bill"
    )
    upkeep_bills_list_parser.add_argument(
        "-s",
        "--statute-index",
        action="store_true",
        default=None,
        help="Only list governance coins with bill for specified statute index",
    )
    upkeep_bills_list_parser.add_argument(
        "-b",
        "--bill",
        action="store_true",
        default=None,
        help="Only list governance coins with given bill (excl propsal times). Specify as program in hex format",
    )
    upkeep_bills_list_parser.add_argument("--incl-spent", action="store_true", help="Include spent governance coins")

    ## registry ##
    upkeep_registry_parser = upkeep_subparsers.add_parser("registry", help="Announcer Registry commands")
    upkeep_registry_subparsers = upkeep_registry_parser.add_subparsers(dest="subaction")
    # show
    upkeep_registry_show_parser = upkeep_registry_subparsers.add_parser(
        "show", help="Show Announcer Registry", description="Shows Announcer Registry."
    )
    # distribute rewards
    upkeep_registry_reward_parser = upkeep_registry_subparsers.add_parser(
        "reward", help="Distribute CRT Rewards", description="Distributes CRT Rewards to registered Announcers."
    )
    upkeep_registry_reward_parser.add_argument(
        "-t", "--target-puzzle-hash", metavar="PUZZLE_HASH", type=str,
        help="Puzzle hash to which excess CRT Rewards not allocated to any announcer will be be paid. Default is first synthetic derived key",
    )
    upkeep_registry_reward_parser.add_argument(
        "-i", "--info", action="store_true", help="Show info on whether rewards can be distributed"
    )

    ## recharge auctions ##
    upkeep_recharge_parser = upkeep_subparsers.add_parser(
        "recharge", help="Participate in recharge auctions", description="Commands to participate in recharge auctions."
    )
    upkeep_recharge_subparsers = upkeep_recharge_parser.add_subparsers(dest="subaction")
    # list
    upkeep_recharge_list_parser = upkeep_recharge_subparsers.add_parser(
        "list", help="List recharge auctions", description="Lists recharge auction coins."
    )
    # launch
    upkeep_recharge_launch_parser = upkeep_recharge_subparsers.add_parser(
        "launch", help="Launch a recharge auction coin", description="Creates and launches a new recharge auction coin."
    )
    upkeep_recharge_launch_parser.add_argument(
        "-c",
        "--create-conditions",
        action="store_true",
        help="Create custom conditions for bill to launch recharge auction",
    )
    upkeep_recharge_launch_parser.add_argument(
        "-b",
        "--bill-coin-name",
        type=str,
        default=None,
        help="Implement previously proposed bill to launch recharge auction coin",
    )
    # start
    upkeep_recharge_start_parser = upkeep_recharge_subparsers.add_parser(
        "start", help="Start a recharge auction", description="Starts a recharge auction."
    )
    upkeep_recharge_start_parser.add_argument("coin_name", type=str, help="Name of recharge auction coin")
    # bid
    upkeep_recharge_bid_parser = upkeep_recharge_subparsers.add_parser(
        "bid", help="Bid in a recharge auction", description="Submits a bid in a recharge auction."
    )
    upkeep_recharge_bid_parser.add_argument("coin_name", type=str, help="Name of recharge auction coin")
    upkeep_recharge_bid_parser.add_argument(
        "amount",  # "BYC_AMOUNT",
        nargs="?",
        type=float,
        default=None,
        help="[optional] Amount of BYC to bid. Default is minimum amount",
    )
    upkeep_recharge_bid_parser.add_argument(
        "-crt",
        metavar="AMOUNT",
        type=float,
        default=None,
        help="Amount of CRT to request. Default is max amount",
    )
    upkeep_recharge_bid_parser.add_argument(
        "-t",
        "--target-puzzle-hash",
        metavar="PUZZLE_HASH",
        type=str,
        default=None,
        help="Puzzle hash to which CRT is issued if bid wins auction. Default is puzzle hash of funding coin selected by driver",
    )
    upkeep_recharge_bid_parser.add_argument(
        "-i",
        "--info",
        action="store_true",
        help="Show info on a potential bid. If no intended BYC bid amount is specified, the minimum admissible amount is assumed",
    )
    # settle
    upkeep_recharge_settle_parser = upkeep_recharge_subparsers.add_parser(
        "settle", help="Settle a recharge auction", description="Settles a recharge auction."
    )
    upkeep_recharge_settle_parser.add_argument("coin_name", type=str, help="Name of recharge auction coin")

    ## surplus auctions ##
    upkeep_surplus_parser = upkeep_subparsers.add_parser(
        "surplus", help="Participate in surplus auctions", description="Commands to participate in surplus auctions."
    )
    upkeep_surplus_subparsers = upkeep_surplus_parser.add_subparsers(dest="subaction")
    # list
    upkeep_surplus_list_parser = upkeep_surplus_subparsers.add_parser(
        "list", help="List surplus auctions", description="Lists surplus auctions."
    )
    # start
    upkeep_surplus_start_parser = upkeep_surplus_subparsers.add_parser(
        "start", help="Start a surplus auction", description="Starts a surplus auction."
    )
    # bid
    upkeep_surplus_bid_parser = upkeep_surplus_subparsers.add_parser(
        "bid", help="Bid in a surplus auction", description="Submits a bid in a surplus auction."
    )
    upkeep_surplus_bid_parser.add_argument("coin_name", type=str, help="Name of surplus auction coin")
    upkeep_surplus_bid_parser.add_argument(
        "amount", nargs="?", type=float, default=None, help="Amount of CRT to bid. Optional when -i option is set"
    )
    upkeep_surplus_bid_parser.add_argument(
        "-t",
        "--target-puzzle-hash",
        metavar="PUZZLE_HASH",
        type=str,
        default=None,
        help="Puzzle hash to which BYC is sent if bid wins auction. Default is puzzle hash of funding coin selected by driver.",
    )
    upkeep_surplus_bid_parser.add_argument("-i", "--info", action="store_true", help="Show info on a potential bid")
    # settle
    upkeep_surplus_settle_parser = upkeep_surplus_subparsers.add_parser(
        "settle", help="Settle a surplus auction", description="Settles a surplus auction."
    )
    upkeep_surplus_settle_parser.add_argument("coin_name", type=str, help="Name of surplus auction coin")

    ## treasury ##
    upkeep_treasury_parser = upkeep_subparsers.add_parser(
        "treasury", help="Manage treasury", description="Commands to manage protocol treasury."
    )
    upkeep_treasury_subparsers = upkeep_treasury_parser.add_subparsers(dest="subaction")
    # show
    upkeep_treasury_show_parser = upkeep_treasury_subparsers.add_parser(
        "show", help="Show treasury", description="Shows information on treasury."
    )
    # rebalance
    upkeep_treasury_rebalance_parser = upkeep_treasury_subparsers.add_parser(
        "rebalance", help="Rebalance treasury", description="Redistributes BYC evenly across all treasury coins."
    )
    upkeep_treasury_rebalance_parser.add_argument(
        "-i", "--info", action="store_true", help="Show info on whether treasury can be rebalanced"
    )
    # launch
    upkeep_treasury_launch_parser = upkeep_treasury_subparsers.add_parser(
        "launch",
        help="Launch a treasury coin",
        description="Creates and launches a new treasury coin into the treasury ring.",
    )
    upkeep_treasury_launch_parser.add_argument(
        "SUCCESSOR_LAUNCHER_ID",
        nargs="?",
        type=str,
        default=None,
        help="[optional] Launcher ID of the coin that will succeed the newly launched coin in treasury ring",
    )
    upkeep_treasury_launch_parser.add_argument(
        "-c",
        "--create-conditions",
        action="store_true",
        help="Create custom conditions for bill to launch treasury coin",
    )
    upkeep_treasury_launch_parser.add_argument(
        "-b",
        "--bill-coin-name",
        type=str,
        default=None,
        help="Implement previously proposed bill to launch treasury coin",
    )

    ## vaults ##
    upkeep_vaults_parser = upkeep_subparsers.add_parser(
        "vaults", help="Manage insufficiently collateralized debt positions"
    )
    upkeep_vaults_subparsers = upkeep_vaults_parser.add_subparsers(dest="subaction")
    upkeep_vaults_list_parser = upkeep_vaults_subparsers.add_parser(
        "list", help="List all vaults", description="Shows information on all collateral vaults."
    )
    upkeep_vaults_list_parser.add_argument(
        "coin_name",
        nargs="?",
        type=str,
        default=None,
        help="[optional] Name of vault coin. If specified, info for only this vault is shown",
    )
    upkeep_vaults_list_parser.add_argument(
        "-s",
        "--seized",
        action="store_true",
        default=None,
        help="Only list seized vaults (seized = in liquidation or bad debt)",
    )
    upkeep_vaults_list_parser.add_argument(
        "-n", "--not-seized", action="store_true", default=None, help="Only list non-seized vaults"
    )
    # LATER: add option for only listing liquidatable/in liquidation/restartable/in bad debt vaults
    # LATER: add -o/--ordered arg to order by outstanding SFs
    upkeep_vaults_transfer_parser = upkeep_vaults_subparsers.add_parser(
        "transfer",
        help="Transfer stability fees from vault to treasury",
        description="Transfers stability fees from specified collateral vault to treasury.",
    )
    upkeep_vaults_transfer_parser.add_argument(
        "coin_name",
        nargs="?",
        type=str,
        default=None,
        help="[optional] Name of vault coin. If not specified, vault with greatest amount of SFs to transfer is selected",
    )
    upkeep_vaults_liquidate_parser = upkeep_vaults_subparsers.add_parser(
        "liquidate", help="Liquidate a vault", description="Starts or restarts a liquidation auction."
    )
    upkeep_vaults_liquidate_parser.add_argument("coin_name", type=str, help="Name of vault to liquidate")
    upkeep_vaults_liquidate_parser.add_argument(
        "-t",
        "--target-puzzle-hash",
        metavar="PUZZLE_HASH",
        type=str,
        default=None,
        help="Puzzle hash to initiator incentive will be be paid. Default is first synthetic derived key of user's wallet",
    )
    upkeep_vaults_bid_parser = upkeep_vaults_subparsers.add_parser(
        "bid", help="Bid in a liquidation auction", description="Submits a bid in a liquidation auction."
    )
    upkeep_vaults_bid_parser.add_argument("coin_name", type=str, help="Name of vault in liquidation")
    upkeep_vaults_bid_parser.add_argument(
        "amount", nargs="?", type=float, default=None, help="Amount of BYC to bid. Optional if -i option is specified"
    )
    upkeep_vaults_bid_parser.add_argument(
        "--max-bid-price", type=float, default=None, help="Maximum price for bid in XCH/BYC"
    )
    upkeep_vaults_bid_parser.add_argument(
        "-i", "--info", action="store_true", help="Show info on liquidation auction bid"
    )
    upkeep_vaults_recover_parser = upkeep_vaults_subparsers.add_parser(
        "recover", help="Recover bad debt", description="Recovers bad debt from a collateral vault."
    )
    upkeep_vaults_recover_parser.add_argument("coin_name", type=str, help="Vault ID")

    # cli / self
    self_parser = subparsers.add_parser("self", help="Commands to manage the CLI itself")
    self_subparsers = self_parser.add_subparsers(dest="action")

    ## protocol info ##
    self_subparsers.add_parser(
        "unlock",
        help="Release store lock",
    )

    ### BILLS ###
    bills_parser = subparsers.add_parser("bills", help="Command to manage bills and governance")
    bills_subparsers = bills_parser.add_subparsers(dest="action")

    ## list ##
    bills_list_parser = bills_subparsers.add_parser(
        "list", help="List governance coins", description="Lists governance coins of user."
    )
    bills_list_parser.add_argument(
        "-x", "--exitable", action="store_true", default=None, help="Governance coins that can be exited"
    )
    bills_list_parser.add_argument(
        "-e",
        "--empty",
        action="store_true",
        default=None,
        help="Empty governance coins, ie those with bill equal to nil",
    )
    bills_list_parser.add_argument(
        "-n",
        "--non-empty",
        action="store_true",
        default=None,
        help="Non-empty governance coins, ie those with bill not equal to nil",
    )
    bills_list_parser.add_argument(
        "-v", "--vetoable", action="store_true", default=None, help="Governance coins with vetoable bill"
    )
    bills_list_parser.add_argument(
        "-c",
        "--enacted",
        action="store_true",
        default=None,
        help="Governance coins with enacted bill (enacted = no longer vetoable, incl lapsed)",
    )
    bills_list_parser.add_argument(
        "-d",
        "--in-implementation-delay",
        default=None,
        action="store_true",
        help="Governance coins with bill in implementation delay",
    )
    bills_list_parser.add_argument(
        "-i", "--implementable", action="store_true", default=None, help="Governance coins with implementable bill"
    )
    bills_list_parser.add_argument(
        "-l", "--lapsed", action="store_true", default=None, help="Governance coins with lapsed bill"
    )
    bills_list_parser.add_argument(
        "-s", "--statute-index", type=int, help="Governance coins with bill to change specified statute"
    )
    bills_list_parser.add_argument(
        "-b",
        "--bill",
        type=str,
        help="Governance coins with specified bill (excl propsal times). Must be program in hex format",
    )
    bills_list_parser.add_argument("--incl-spent", action="store_true", help="Include spent governance coins")

    ## toggle governance mode ##
    bills_toggle_parser = bills_subparsers.add_parser(
        "toggle",
        help="Convert a plain CRT coin into a governance coin or vice versa",
        description="If coin is in governance mode, exit to plain CRT. If coin is plain CRT, activate governance mode.",
    )
    bills_toggle_parser.add_argument("coin_name", type=str, help="Coin name")
    bills_toggle_parser.add_argument("-i", "--info", action="store_true", help="Show info on toggling governance mode")

    ## propose ##
    bills_propose_parser = bills_subparsers.add_parser("propose", help="Propose a new bill")
    bills_propose_parser.add_argument("index", type=int, help="Statute index. Specify -1 for custom conditions")
    bills_propose_parser.add_argument(
        "value",
        nargs="?",
        default=None,
        type=str,
        help="Value of bill, ie Statute value or custom announcements. Omit to keep current value. Must be a Program in hex format if INDEX = -1, a 32-byte hex string if INDEX = 0, and an integer otherwise",
    )
    bills_propose_parser.add_argument(
        "-n",
        "--coin-name",
        default=None,
        type=str,
        help="Governance coin to use for proposal. If not specified, a suitable coin is chosen automatically",
    )
    bills_propose_parser.add_argument(
        "-f", "--force", action="store_true", help="Propose bill even if resulting Statutes are not consistent"
    )
    bills_propose_parser.add_argument(
        "--proposal-threshold", default=None, type=float, help="Min amount of CRT required to propose new Statute value"
    )
    bills_propose_parser.add_argument("-v", "--veto-interval", type=int, default=None, help="Veto period in seconds")
    bills_propose_parser.add_argument(
        "-d", "--implementation-delay", type=int, default=None, help="Implementation delay in seconds"
    )
    bills_propose_parser.add_argument(
        "--max-delta", type=int, default=None, help="Max absolute amount by which Statues value may change"
    )
    bills_propose_parser.add_argument("-s", "--skip-verify", action="store_true", help="Skip statutes integrity checks")
    bills_propose_parser.add_argument(
        "-l", "--label", type=str, help="Tag this coin with a label that can be used to identify it in other operations"
    )

    ## implement ##
    bills_implement_subparser = bills_subparsers.add_parser(
        "implement", help="Implement a bill into statute", description="Implement a bill."
    )
    bills_implement_subparser.add_argument(
        "coin_name", nargs="?", default=None, type=str,
        help="[optional] Coin name of bill to implement. Default is to implement bill that has been implementable longest"
    )

    ## reset ##
    bills_reset_subparser = bills_subparsers.add_parser(
        "reset", help="Reset a bill", description="Sets bill of a governance coin to nil."
    )
    bills_reset_subparser.add_argument("coin_name", type=str, help="Coin name")

    ### WALLET ###
    wallet_parser = subparsers.add_parser("wallet", help="Wallet commands")
    wallet_subparsers = wallet_parser.add_subparsers(dest="action")

    ## addresses ##
    wallet_addresses_parser = wallet_subparsers.add_parser(
        "addresses",
        help="Get wallet addresses",
        description="Shows wallet addresses and puzzle hashes.",
    )
    wallet_addresses_parser.add_argument(
        "-i",
        "--derivation-index",
        type=int,
        default=5,
        help="Derivation index up to which to show wallet addresses. Default: 5",
    )
    wallet_addresses_parser.add_argument(
        "-p",
        "--puzzle_hashes",
        action="store_true",
        help="Also show puzzle hashes",
    )

    ## balances ##
    wallet_balances_parser = wallet_subparsers.add_parser(
        "balances",
        help="Get wallet balances",
        description="Show wallet balances for XCH, BYC, and CRT coins not in governance mode.",
    )

    ## coins ##
    wallet_coins_parser = wallet_subparsers.add_parser(
        "coins",
        help="Get wallet coins",
        description="Show information on individual coins in wallet. By default, only CRT coins not in governance mode are returned.",
    )
    wallet_coins_parser.add_argument(
        "-t",
        "--type",
        type=str.lower,
        choices=["xch", "byc", "crt", "all", "gov", "empty", "bill"],
        help="Return coins of given type only",
    )

    ## toggle governance mode ##
    wallet_toggle_parser = wallet_subparsers.add_parser(
        "toggle",
        help="Convert a plain CRT coin into a governance coin or vice versa",
        description="If coin is in governance mode, exit to plain CRT. If coin is plain CRT, activate governance mode.",
    )
    wallet_toggle_parser.add_argument("coin_name", type=str, help="Coin name")
    wallet_toggle_parser.add_argument("-i", "--info", action="store_true", help="Show info on toggling governance mode")

    ## take-offer ##
    wallet_take_offer_parser = wallet_subparsers.add_parser(
        "take-offer",
        help="Take an offer",
        description="Take an existing Chia offer.",
    )
    wallet_take_offer_parser.add_argument("offer_bech32", type=str, help="The offer in bech32 format to take")

    ### ANNOUNCER ###
    announcer_parser = subparsers.add_parser("announcer", help="Announcer commands")
    announcer_subparsers = announcer_parser.add_subparsers(dest="action")

    ## launch ##
    announcer_launch_parser = announcer_subparsers.add_parser(
        "launch", help="Launch an announcer", description="Launches an announcer."
    )
    announcer_launch_parser.add_argument("price", type=float, help="Initial announcer price in USD per XCH")

    ## show ##
    announcer_show_subparser = announcer_subparsers.add_parser(
        "show", help="Show information on announcer", description="Shows information on announcer."
    )
    announcer_show_subparser.add_argument(
        "-a", "--approved", action="store_true", help="Show announcer only if approved"
    )
    announcer_show_subparser.add_argument(
        "-na", "--unapproved", action="store_true", help="Show announcer only if not approved"
    )
    announcer_show_subparser.add_argument(
        "-v", "--valid", action="store_true", help="Show announcer only if valid (approved and not expired)"
    )
    announcer_show_subparser.add_argument(
        "-nv", "--invalid", action="store_true", help="Show announcer only if invalid (expired or not approved)"
    )
    announcer_show_subparser.add_argument(
        "-p", "--penalizable", action="store_true", help="Show announcer only if penalizable"
    )
    announcer_show_subparser.add_argument(
        "-np", "--non-penalizable", action="store_true", help="Show announcer only if not penalizable"
    )
    announcer_show_subparser.add_argument(
        "-r", "--registered", action="store_true", help="Show announcer only if registered"
    )
    announcer_show_subparser.add_argument(
        "-nr", "--unregistered", action="store_true", help="Show announcer only if not registered"
    )
    announcer_show_subparser.add_argument("--incl-spent", action="store_true", help="Include spent announcer coins")

    ## update price ##
    announcer_update_parser = announcer_subparsers.add_parser(
        "update",
        help="Update announcer price",
        description="Updates the announcer price. The puzzle automatically updates the expiry timestamp.",
    )
    announcer_update_parser.add_argument("price", type=float, help="New announcer price in USD per XCH")
    announcer_update_parser.add_argument(
        "coin_name",
        nargs="?",
        type=str,
        default=None,
        help="[optional] Announcer coin name. Only required if user owns more than one announcer",
    )
    announcer_update_parser.add_argument(
        "--fee-coin",
        action="store_true",
        help="Use a fee coin instead of announcer deposit to pay for transaction fees",
    )

    ## configure ##
    announcer_configure_parser = announcer_subparsers.add_parser(
        "configure", help="Configure the announcer", description="Configures the announcer."
    )
    announcer_configure_parser.add_argument(
        "coin_name",
        nargs="?",
        type=str,
        default=None,
        help="[optional] Announcer coin name. Only required if user owns more than one announcer",
    )
    announcer_configure_parser.add_argument(
        "-a", "--make-approvable", action="store_true", help="Configure announcer so that is becomes approvable"
    )
    announcer_configure_parser.add_argument("--deposit", type=float, help="New deposit amount in XCH")
    announcer_configure_parser.add_argument("--min-deposit", type=float, help="New minimum deposit amount in XCH")
    announcer_configure_parser.add_argument("--inner-puzzle-hash", type=str, help="New inner puzzle hash (re-key)")
    announcer_configure_parser.add_argument(
        "--price",
        type=float,
        help="New announcer price in USD per XCH. If only updating price, it's more efficient to use 'update' operation",
    )
    announcer_configure_parser.add_argument("--ttl", type=int, help="New price time to live in seconds")
    announcer_configure_parser.add_argument(
        "-c", "--cancel-deactivation", action="store_true", help="Cancel deactivation of announcer"
    )
    announcer_configure_parser.add_argument("-d", "--deactivate", action="store_true", help="Deactivate announcer")

    ## register ##
    announcer_register_parser = announcer_subparsers.add_parser(
        "register",
        help="Register an Announcer",
        description="Registers an Announcer with Announcer Registry to be eligible for CRT Rewards.",
    )
    announcer_register_parser.add_argument(
        "coin_name",
        nargs="?",
        type=str,
        default=None,
        help="[optional] Announcer coin name. Only required if user owns more than one announcer",
    )
    announcer_register_parser.add_argument(
        "-t", "--target-puzzle-hash", metavar="PUZZLE_HASH", type=str,
        help="Inner puzzle hash to which CRT Rewards are paid. Default is announcer inner puzzle hash",
    )

    ## exit ##
    announcer_exit_parser = announcer_subparsers.add_parser(
        "exit",
        help="Exit announcer layer",
        description="Exit announcer layer by melting announcer into plain XCH coin.",
    )
    announcer_exit_parser.add_argument(
        "coin_name",
        nargs="?",
        type=str,
        default=None,
        help="[optional] Announcer coin name. Only required if user owns more than one announcer",
    )

    ### ORACLE ###
    oracle_parser = subparsers.add_parser("oracle", help="Oracle commands")
    oracle_subparsers = oracle_parser.add_subparsers(dest="action")

    ## show ##
    oracle_show_subparser = oracle_subparsers.add_parser(
        "show", help="Show oracle prices", description="Shows oracle prices."
    )

    ## update price ##
    oracle_update_parser = oracle_subparsers.add_parser(
        "update", help="Update oracle price", description="Adds new price to Oracle price queue."
    )
    oracle_update_parser.add_argument(
        "-i", "--info", action="store_true", help="Show info on whether Oracle can be updated"
    )

    ### STATUTES ###
    statutes_parser = subparsers.add_parser("statutes", help="Manage statutes")
    statutes_subparsers = statutes_parser.add_subparsers(dest="action")

    ## list ##
    statutes_list_subparser = statutes_subparsers.add_parser("list", help="List Statutes")
    statutes_list_subparser.add_argument(
        "-f", "--full", action="store_true", help="Show Statutes incl constraints and additional info"
    )

    ## update price ##
    statutes_update_subparser = statutes_subparsers.add_parser("update", help="Update Statutes Price")
    statutes_update_subparser.add_argument(
        "-i", "--info", action="store_true", help="Show info on when Statues can be updated next"
    )

    ### COLLATERAL VAULT ###
    vault_parser = subparsers.add_parser("vault", help="Manage a collateral vault")
    vault_subparsers = vault_parser.add_subparsers(dest="action")

    ## show ##
    vault_show_parser = vault_subparsers.add_parser("show", help="Show vault")

    ## deposit ##
    vault_deposit_subparser = vault_subparsers.add_parser("deposit", help="Deposit to vault")
    vault_deposit_subparser.add_argument("amount", type=float, help="Amount of XCH to deposit")

    ## withdraw ##
    vault_withdraw_subparser = vault_subparsers.add_parser("withdraw", help="Withdraw from vault")
    vault_withdraw_subparser.add_argument("amount", type=float, help="Amount of XCH to withdraw")

    ## borrow ##
    vault_borrow_subparser = vault_subparsers.add_parser("borrow", help="Borrow from vault")
    vault_borrow_subparser.add_argument("amount", type=float, help="Amount of BYC to borrow")

    ## repay ##
    vault_repay_subparser = vault_subparsers.add_parser("repay", help="Repay to vault")
    vault_repay_subparser.add_argument("amount", type=float, help="Amount of BYC to repay")

    ### SAVINGS VAULT ###
    savings_parser = subparsers.add_parser("savings", help="Manage a savings vault")
    savings_subparsers = savings_parser.add_subparsers(dest="action")

    ## show ##
    savings_show_parser = savings_subparsers.add_parser("show", help="Show vault")

    ## deposit ##
    savings_deposit_subparser = savings_subparsers.add_parser(
        "deposit",
        help="Deposit to vault",
        description="Deposit BYC to savings vault. By default, all accrued interest is withdrawn from treasury to savings vault on every deposit. To get paid a different amount of interest (incl 0), specify the desired value via INTEREST argument.",
    )
    savings_deposit_subparser.add_argument("amount", type=float, help="Amount of BYC to deposit")
    savings_deposit_subparser.add_argument(
        "INTEREST",
        nargs="?",
        type=float,
        default=None,
        help="[optional] Amount (in BYC) of accrued interest to withdraw from treasury to savings vault. By default, all accrued interest is withdrawn",
    )

    ## withdraw ##
    savings_withdraw_subparser = savings_subparsers.add_parser(
        "withdraw",
        help="Withdraw from vault",
        description="Withdraw BYC from savings vault. By default, all accrued interest is withdrawn from treasury to savings vault on every withdrawal. To get paid a different amount of interest (incl 0), specify the desired value via INTEREST argument.",
    )
    savings_withdraw_subparser.add_argument("amount", type=float, help="Amount of BYC to withdraw")
    savings_withdraw_subparser.add_argument(
        "INTEREST",
        nargs="?",
        type=float,
        default=None,
        help="[optional] Amount (in BYC) of accrued interest to withdraw from treasury to savings vault. By default, all accrued interest is withdrawn",
    )

    args = parser.parse_args()
    # set log level based on verbosity
    if args.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.WARNING

    # Configure logging
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Add a handler only if one doesn't already exist (e.g. when running in pytest)
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        if args.verbose:
            formatter = logging.Formatter(
                fmt="%(asctime)s | %(levelname).1s | %(name)s:%(lineno)d - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        else:
            formatter = logging.Formatter(fmt="%(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        handler.setLevel(log_level)
        root_logger.addHandler(handler)

    # Tame noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)

    kwargs = dict([(k.lower(), v) for k, v in vars(args).items()])

    # Handle case where action is None (e.g., "circuit-cli wallet" without subcommand)
    if args.action is None:
        parser.print_help()
        return

    function_name = f"{args.command}_{args.action.replace('-', '_')}"
    try:
        # run commands method dynamically based on the parser command
        # Show immediate feedback in text mode while contacting the server
        if not args.json:
            fee_mode = getattr(args, "fee_per_cost", None)
            fee_mode_str = str(fee_mode) if fee_mode is not None else "default"
            no_wait = bool(getattr(args, "no_wait", False))
            progress_mode = getattr(args, "progress", "text")
            base = getattr(args, "base_url", None)
            # First line: contacting + endpoint
            sys.stderr.write(f"⏳ Contacting server at {base}...\n")
            # Second line: show command and key options
            sys.stderr.write(
                f"↳ command: {function_name} | fee_per_cost: {fee_mode_str} | no_wait: {no_wait} | progress: {progress_mode}\n"
            )
            sys.stderr.flush()
    except AssertionError as ae:
        # return error message with status
        result = {"error": str(ae)}

    rpc_client = CircuitRPCClient(
        base_url=args.base_url,
        private_key=args.private_key,
        add_sig_data=args.add_sig_data,
        fee_per_cost=args.fee_per_cost,
        no_wait_for_tx=args.no_wait,
        dict_store_path=args.dd,
    )

    response = await rpc_client.client.get("/protocol/constants")
    response.raise_for_status()
    data = response.json()
    rpc_client.consts = {
        "price_PRECISION": 10 ** data["xch_usd_price_decimals"],
        "MOJOS": data["mojos_per_xch"],
        "MCAT": 10 ** data["cat_decimals"],
    }
    log.info("Protocol constants loaded successfully")

    if args.command == "upkeep" and args.action == "liquidator":
        from circuit_cli.little_liquidator import LittleLiquidator

        # Set up progress handler for liquidator
        progress_handler = None
        if args.progress != "off":
            if args.progress == "json":
                from circuit_cli.progress import make_json_progress_handler
                progress_handler = make_json_progress_handler()
            else:
                from circuit_cli.progress import make_text_progress_handler
                progress_handler = make_text_progress_handler()

        liquidator = LittleLiquidator(
            rpc_client=rpc_client,
            max_bid_milli_amount=args.max_bid_amount * rpc_client.consts["MCAT"] if args.max_bid_amount else None,
            min_discount=args.min_discount,
            min_profit_threshold=getattr(args, "min_profit_threshold", 0.02),
            max_offer_amount=getattr(args, "max_offer_amount", 1.0),
            offer_expiry_seconds=getattr(args, "offer_expiry_seconds", 300),
            current_time=getattr(args, "current_time", None),
            progress_handler=progress_handler,
        )
        if args.run_once:
            result = await liquidator.process_once()
            if args.json:
                print(json.dumps(result))
            else:
                from circuit_cli.json_formatter import format_circuit_response

                print(format_circuit_response(result))
        else:
            await liquidator.run(run_once=False)
        return

    # In text mode, show which HTTP endpoints are being used
    try:
        rpc_client.show_endpoints = not args.json
    except AttributeError:
        pass

    # Set progress handler if requested
    if args.progress != "off":
        if args.progress == "json":
            from circuit_cli.progress import make_json_progress_handler

            rpc_client.progress_handler = make_json_progress_handler()
        else:
            from circuit_cli.progress import make_text_progress_handler

            rpc_client.progress_handler = make_text_progress_handler()
    await rpc_client.set_fee_per_cost()
    # Load protocol constants with improved error handling
    try:
        # Optional: trace endpoint being used in text mode
        try:
            if getattr(rpc_client, "show_endpoints", False):
                import sys as _sys

                base = getattr(rpc_client, "base_url", "")
                _sys.stderr.write(f"→ HTTP GET {base}/protocol/constants\n")
                _sys.stderr.flush()
        except Exception:
            pass
    except Exception as e:
        log.error(f"Failed to load protocol constants: {e}")
        log.warning("Using default constants - some functionality may be limited")
        # Set default constants so client can still be used for testing
    try:
        if "subaction" in kwargs.keys():
            function_name += f"_{args.subaction}"
        del kwargs["command"]
        del kwargs["action"]
        kwargs.pop("subaction", None)
        del kwargs["base_url"]
        del kwargs["private_key"]
        del kwargs["add_sig_data"]
        del kwargs["fee_per_cost"]
        del kwargs["json"]
        del kwargs["no_wait"]
        del kwargs["verbose"]
        del kwargs["progress"]
        del kwargs["dd"]
        # Remove testing-only args if present
        kwargs.pop("current_time", None)
        log.info(f"Calling {function_name} with {kwargs}")
        result = await getattr(rpc_client, f"{function_name}")(**kwargs)
        if args.json:
            print(json.dumps(result))
        else:
            from circuit_cli.json_formatter import format_circuit_response
            print(format_circuit_response(result))
    except (AttributeError, KeyError) as e:
        log.exception(f"Failed to run command: {e}")
        parser.print_help()
    except httpx.HTTPStatusError as err:
        print(err.args[0])
    except:
        log.exception("Unexpected error")


def main():
    asyncio.run(cli())


if __name__ == "__main__":
    main()
