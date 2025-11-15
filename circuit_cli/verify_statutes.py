import re
import tomllib
from typing import Tuple
from pathlib import Path
from math import ceil

from chia_rs.sized_bytes import bytes32

TOML_FILE = "statutes_ranges.toml"


def load_ranges():
    parent_dir = Path(__file__).resolve().parent.parent
    filepath = parent_dir / TOML_FILE
    if not filepath.exists():
        raise FileNotFoundError(f"Statutes verification failed. File {filepath} not found")
    with open(filepath, "rb") as f:
        return tomllib.load(f)


def parse(s: str) -> int | float:
    """
    Parse strings:
        - With unit: '2hr', '90min', '1_234_567sec' -> convert to raw number
        - No unit: '123', '1_234_567' → raw number

    Rules:
        - Mandatory '000s separator '_'
        - No space between number and unit
        - Valid units as per CONVERSIONS dict

    Raises:
        ValueError: If format is invalid
    """
    CONVERSIONS = {
        "min": 60,
        "hr": 3600,
        "d": 24*3600,
    }

    if not s:
        return float('inf')

    # 1. Try to match a unit
    unit_factor = 1  # default: no conversion
    unit_len = 0

    for unit, factor in CONVERSIONS.items():
        if s.endswith(unit):
            unit_factor = factor
            unit_len = len(unit)
            break

    # 2. Extract number part
    num_str = s[:-unit_len] if unit_len else s

    if not num_str:
        raise ValueError(f"Missing number in '{s}'")

    # 3. Small number (< 1000): just digits
    if len(num_str) <= 3:
        if not num_str.isdigit():
            raise ValueError(f"Non-digit characters in number: '{num_str}'")
        number = int(num_str)
    else:
        # 4. Large number (>= 1000): must have correct _ grouping
        if not re.fullmatch(r'\d{1,3}(?:_\d{3})+', num_str):
            raise ValueError(
                f"Invalid number format in '{s}'. "
                "For numbers >= 1000, must use '_' every 3 digits: e.g. '1_234_567'"
            )
        number = int(num_str.replace("_", ""))

    # 5. Apply conversion (if unit was present)
    return number * unit_factor


def verify_statutes(
        statute_indices: list[tuple[int, str]],
        full_statutes: dict, # as expected immediately prior to bill implementation
        index: int,
        value: str | None,
        proposal_threshold: int  | None,
        veto_interval: int | None,
        implementation_delay: int | None,
        max_delta: int | None,
) -> bool:

    # find statute name
    assert statute_indices[index][1] == index
    bill_statute_name = statute_indices[index][0]

    # check that proposed statute value has expected format
    if index == 0:
        try:
            bytes32.from_hexstr(value)
        except Exception as err:
            print(f"Invalid {bill_statute_name} proposed. Must be convertible to bytes32")
            return False

    # update statutes according to proposed bill
    if value is not None:
        full_statutes[bill_statute_name]["value"] = int(value) if index > 0 else value
    if proposal_threshold is not None:
        full_statutes[bill_statute_name]["threshold_amount_to_propose"] = proposal_threshold
    if veto_interval is not None:
        full_statutes[bill_statute_name]["veto_interval"] = veto_interval
    if implementation_delay is not None:
        full_statutes[bill_statute_name]["implementation_delay"] = implementation_delay
    if max_delta is not None:
        full_statutes[bill_statute_name]["max_delta"] = max_delta

    # load acceptable ranges from file
    r = load_ranges()
    default_constraints = r["default_constraints"]

    # verify all statutes
    failed = [] # keep track of failed verifications
    for idx in range(len(full_statutes)):
        indent = "" if idx == index else "  "

        statute_name = statute_indices[idx][0]
        value = full_statutes[statute_name]["value"]

        # check statute value
        if "min" in r["statutes"][statute_name]:
            boundary = parse(r["statutes"][statute_name]["min"])
            if not value >= boundary:
                failed.append(f"{indent}{statute_name}: Statute value below acceptable min ({value}<{boundary})")
        if "max" in r["statutes"][statute_name]:
            boundary = parse(r["statutes"][statute_name]["max"])
            if not value <= boundary:
                failed.append(f"{indent}{statute_name}: Statute value above acceptable max ({value}>{boundary})")

        # check statute constraints
        for c in ["proposal_threshold", "veto_interval", "implementation_delay", "max_delta"]:
            # Check against statute-specific boundaries if present, else check against defaults
            if c in r["statutes"][statute_name]:
                section = r["statutes"][statute_name][c]
            else:
                assert c in r["default_constraints"], f"Invalid constraint or missing from defaults: {c}"
                section = r["default_constraints"][c]

            if c == "proposal_threshold":
                if proposal_threshold is None or statute_name != bill_statute_name:
                    value = full_statutes[statute_name]["threshold_amount_to_propose"]
                else:
                    value = proposal_threshold
            elif c == "veto_interval":
                if veto_interval is None or statute_name != bill_statute_name:
                    value = full_statutes[statute_name][c]
                else:
                    value = veto_interval
            elif c == "implementation_delay":
                if implementation_delay is None or statute_name != bill_statute_name:
                    value = full_statutes[statute_name][c]
                else:
                    value = implementation_delay
            elif c == "max_delta":
                if max_delta is None or statute_name != bill_statute_name:
                    value = full_statutes[statute_name][c]
                else:
                    value = max_delta

            if "min" in section:
                boundary = parse(section["min"])
                if not value >= boundary:
                    failed.append(f"{indent}{statute_name}: {c.replace('_',' ')} below acceptable min ({value}<{boundary})")
            if "max" in section:
                boundary = parse(section["max"])
                if not value <= boundary:
                    failed.append(f"{indent}{statute_name}: {c.replace('_',' ')} above acceptable max ({value}>{boundary})")

    # Minimum debt, LP and initiator incentives
    relevant_statutes = [
        "VAULT_MINIMUM_DEBT",
        "VAULT_LIQUIDATION_PENALTY_BPS",
        "VAULT_INITIATOR_INCENTIVE_FLAT",
        "VAULT_INITIATOR_INCENTIVE_BPS",
    ]
    min_debt_byc = full_statutes["VAULT_MINIMUM_DEBT"]["value"] / 1000.0
    liquidation_penalty = full_statutes["VAULT_LIQUIDATION_PENALTY_BPS"]["value"] / 10_000.0
    initiator_incentive_flat_byc = full_statutes["VAULT_INITIATOR_INCENTIVE_FLAT"]["value"] / 1000.0
    initiator_incentive_relative = full_statutes["VAULT_INITIATOR_INCENTIVE_BPS"]["value"] / 10_000.0
    initiator_incentive_byc = initiator_incentive_flat_byc + initiator_incentive_relative * min_debt_byc
    remaining_liquidation_penalty = min_debt_byc * liquidation_penalty - initiator_incentive_byc
    if not remaining_liquidation_penalty > 0:
        indent = "" if bill_statute_name in relevant_statutes else "  "
        failed.append(f"{', '.join(relevant_statutes)}: Initiator Incentive can be larger than Liquidation Penalty")

    # Minimum auction price
    max_allowed_deviation = 0.01
    relevant_statutes = [
        "VAULT_AUCTION_TTL",
        "VAULT_AUCTION_STARTING_PRICE_FACTOR_BPS",
        "VAULT_AUCTION_PRICE_TTL",
        "VAULT_AUCTION_PRICE_DECREASE_BPS",
        "VAULT_AUCTION_MINIMUM_PRICE_FACTOR_BPS",
    ]
    auction_ttl = full_statutes["VAULT_AUCTION_TTL"]["value"]
    starting_price_factor = full_statutes["VAULT_AUCTION_STARTING_PRICE_FACTOR_BPS"]["value"] / 10_000.0 # of statutes price
    price_ttl = full_statutes["VAULT_AUCTION_PRICE_TTL"]["value"]
    decrease_factor = full_statutes["VAULT_AUCTION_PRICE_DECREASE_BPS"]["value"] / 10_000.0 # of starting price
    decrease = decrease_factor * starting_price_factor # of statutes price
    max_num_decreases = ceil(auction_ttl / price_ttl) - 1
    implicit_min_price = starting_price_factor - (max_num_decreases * decrease) # of statutes price
    min_price_factor = full_statutes["VAULT_AUCTION_MINIMUM_PRICE_FACTOR_BPS"]["value"] / 10_000.0 # of statutes price
    min_price = min_price_factor * starting_price_factor # of statutes price
    min_price_deviation = min_price - implicit_min_price # of statutes price
    if abs(min_price_deviation) > max_allowed_deviation: # devition of less than 1% is ok
        indent = "" if bill_statute_name in relevant_statutes else "  "
        failed.append(f"{', '.join(relevant_statutes)}: Implicit minimum auction price deviates by more than {max_allowed_deviation:.1f}% from Minimum Auction Price")

    if failed:
        if any(bill_statute_name in msg for msg in failed):
            print("Proposed bill has failed Statutes verification:")
        else:
            print("Statutes verification has failed:")
        for msg in failed:
            print(msg)
        return False

    print("Proposed bill has passed Statutes verification")
    return True
