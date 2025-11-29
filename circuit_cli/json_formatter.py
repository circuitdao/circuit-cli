"""
JSON formatter for Circuit CLI responses.

This module provides human-readable formatting for JSON responses from CircuitRPCClient,
with intelligent formatting based on field names and value types.
"""

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Union

PRICE_PRECISION = 100

class CircuitJSONFormatter:
    """Formats JSON responses from CircuitRPCClient in a human-readable way."""

    def __init__(self, use_color: bool | None = None):
        # Common field patterns for different types of values
        self.amount_patterns = [
            #r".*amount.*",
            #r".*_balance.*",
            r"^(?!.*\b(min_deposit)\b).*deposit.*",
            #r".*value.*",
            #r".*min_.*",
            #r".*max_.*",
            r"^(?!.*\b(threshold_amount_to_propose)\b).*threshold.*",
        ]

        self.byc_patterns = [
            r".*borrow.*",
            r".*byc.*",
            r"^(?!.*\b(in_bad_debt)\b).*debt.*",
            r".*principal.*",
            r"^recharge_auction_(minimum|maximum)_bid$",
            r".*repay.*",
            r"^(?!.*df.*).*stability_fee.*", #r".*stability_fee.*",
            r"^surplus_auction_lot$",
            r".*treasury_(delta|minimum|maximum).*",
            r"^vault_auction_minimum_bid_flat$",
            r"^vault_initiator_incentive_flat_fee$",
            r"^(min|max)_coin_amount$",
            r"^treasury_balance$",
        ]

        self.crt_patterns = [
            r"^threshold_amount_to_propose$",
            r"^announcer_rewards_per_interval$",
            r"^(?!(.*crt_price.*)).*crt.*",
        ]

        self.xch_patterns = [
            r".*min_deposit.*",
            r"^(?!.*\b(collateral_ratio)\b).*collateral.*",
            r"^(?!.*(stability|initiator).*).*fee.*",
            r"^(?!.*\b(accrued_interest_withdrawable)\b).*withdraw.*",
            r".*xch.*",
        ]

        self.address_patterns = [
            r".*address.*",
            r".*puzzle_hash.*",
            r".*coin_name.*",
            r".*name$",
            r".*launcher_id.*",
            r".*coin_id.*",
            r".*parent.*",
        ]

        self.price_patterns = [
            r"^(?!(.*PRICE_PRECISION.*|.*crt_price.*|.*price_ttl.*|.*price_update.*|.*price_delay.*|.*price_factor.*|.*_bps$)).*price.*",
        ]

        self.crt_price_patterns = [
            r".*crt_price.*",
        ]

        self.price_info_patterns = [
            r".*price_info.*",
        ]

        self.timestamp_patterns = [
            r".*cutoff.*",
            r".*timestamp.*",
            r".*time.*",
            #r".*created_at.*",
            #r".*updated_at.*",
            #r".*expires_at.*",
            r"^\blast_price_update\b$",
            r"^(?!.*(stability|initiator|distributable).*).*_at$",
            r".*deadline.*",
        ]

        self.timeperiod_patterns = [
            r".*_in$",
            r".*delay.*",
            r"^(?!(.*rewards_.*|^claim_.*|.*_bps$)).*interval.*",
            r".*time_(until|to).*",
            r"^(?!(.*settle.*)).*ttl.*",
        ]

        self.hex_patterns = [
            r".*_hex$",
            r".*hash$",
            r".*signature.*",
            r".*pubkey.*",
            r".*public_key.*"
        ]

        self.ratio_patterns = [
            r".*ratio.*",
        ]

        self.pct_patterns = [
            r".*pct.*",
        ]

        self.bps_patterns = [
            r".*bps.*",
            r"^vault_auction_starting_price_factor$",
        ]

        #self.bool_patterns = [
        #    r".*can_be.*",
        #    r".*is_.*",
        #]

        # Mojos per XCH (standard Chia conversion)
        self.MOJOS_PER_XCH = 1_000_000_000_000
        self.MCAT_PRECISION = 1_000
        # Color support (TTY-aware unless explicitly set)
        try:
            import os, sys
            no_color = os.environ.get("CIRCUIT_CLI_NO_COLOR")
            isatty = sys.stdout.isatty()
        except Exception:
            no_color = "1"
            isatty = False
        if use_color is None:
            self.use_color = (not no_color) and isatty
        else:
            self.use_color = bool(use_color)
        self.colors = {
            "ok": "\x1b[32m" if self.use_color else "",
            "warn": "\x1b[33m" if self.use_color else "",
            "err": "\x1b[31m" if self.use_color else "",
            "info": "\x1b[36m" if self.use_color else "",
            "dim": "\x1b[2m" if self.use_color else "",
            "bold": "\x1b[1m" if self.use_color else "",
            "reset": "\x1b[0m" if self.use_color else "",
        }

    def format_response(self, data: Any, indent: int = 0) -> str:
        """Format a response with appropriate human-readable formatting."""
        if isinstance(data, dict):
            return self._format_dict(data, indent)
        elif isinstance(data, list):
            return self._format_list(data, indent)
        else:
            return self._format_value("", data, indent)

    def _format_dict(self, data: Dict[str, Any], indent: int = 0) -> str:
        """Format a dictionary with intelligent field formatting."""
        if not data:
            return "{}"

        lines = []
        prefix = "  " * indent

        # Sort keys to put important ones first
        sorted_keys = self._sort_keys(data.keys())

        # Add a subtle section header spacing at root level
        if indent == 0 and len(sorted_keys) > 1:
            lines.append("")

        for key in sorted_keys:
            value = data[key]
            formatted_key = self._format_key_name(key)
            formatted_value = self._format_value(key, value, indent + 1)

            if isinstance(value, (dict, list)) and value:
                # Add a blank line before nested objects for readability
                if lines and lines[-1] != "":
                    lines.append("")
                # Colorize section headers (keys) when at root level
                header = formatted_key
                if indent == 0 and self.use_color:
                    C = self.colors
                    header = f"{C['bold']}{formatted_key}{C['reset']}"
                lines.append(f"{prefix}{header}:")
                lines.append(formatted_value)
            else:
                # Emphasize certain scalar statuses
                val = formatted_value
                if isinstance(value, str) and key.lower() == "status":
                    C = self.colors
                    if value.lower() in ("confirmed", "success", "ok"):
                        icon = f"{C['ok']}✔{C['reset']} " if self.use_color else "✔ "
                        val = f"{icon}{C['ok']}{value.upper()}{C['reset']}" if self.use_color else value.upper()
                    elif value.lower() in ("failed", "error"):
                        icon = f"{C['err']}✖{C['reset']} " if self.use_color else "✖ "
                        val = f"{icon}{C['err']}{value.upper()}{C['reset']}" if self.use_color else value.upper()
                lines.append(f"{prefix}{formatted_key}: {val}")

        # Trailing newline separation for root level blocks
        if indent == 0:
            lines.append("")
        return "\n".join(lines)

    def _format_list(self, data: List[Any], indent: int = 0, key_lower: str = None) -> str:
        """Format a list with appropriate formatting."""
        if not data:
            # returned empty list
            return "\nNo results."

        lines = []
        prefix = "  " * indent
        bullet = "•" if True else "-"

        # LATER: uncomment below if we change (full_)implemented_statutes from dict to list of lists (idx, name, value)
        #if key_lower is not None and "implemented_statutes" in key_lower:
        #    for i, name, value in data:
        #        idx = f"[{i:02d}]"
        #        if self.use_color and self.colors.get('dim'):
        #            idx = f"{self.colors['dim']}{idx}{self.colors['reset']}"
        #        if key_lower == "implemented_statutes":
        #            lines.append(f"{prefix}{idx} - {name}: {self._format_value(name, value)}")
        #        elif key_lower == "full_implemented_statutes":
        #            lines.append(f"{prefix}{idx} - {name}:")
        #            lines.append(f"{self._format_value(name, value, indent = indent + 2)}")
        #    return "\n".join(lines)

        if key_lower is not None and self._matches_pattern(key_lower, self.price_info_patterns):
            item = data
            if isinstance(item[0], list):
                for idx in range(len(item)):
                    formatted_price_info = self._format_price_info(key_lower, item[idx])
                    lines.append(f"{prefix}{idx}: {formatted_price_info[0]}")
                    lines.append(f"{prefix}   {formatted_price_info[1]}")
            else:
                formatted_price_info = self._format_price_info(key_lower, item)
                lines.append(f"{prefix} {formatted_price_info[0]}")
                lines.append(f"{prefix} {formatted_price_info[1]}")
            return "\n".join(lines)

        for i, item in enumerate(data):
            idx = f"[{i}]"
            if self.use_color and self.colors.get('dim'):
                idx = f"{self.colors['dim']}{idx}{self.colors['reset']}"
            if isinstance(item, dict):
                lines.append(f"{prefix}{idx}:")
                lines.append(self._format_dict(item, indent + 1))
            else:
                formatted_item = self._format_value("", item, indent)
                lines.append(f"{prefix}{idx}: {formatted_item}")

        return "\n".join(lines)

    def _format_value(self, key: str, value: Any, indent: int = 0) -> str:
        """Format a single value based on its type and key name."""
        if value is None:
            return "null"

        # Handle primitive values with context-aware formatting
        key_lower = key.lower()

        # Handle nested structures
        if isinstance(value, dict):
            return self._format_dict(value, indent)
        elif isinstance(value, list):
            return self._format_list(value, indent, key_lower)

        # Check for amount/balance fields
        if self._matches_pattern(key_lower, self.amount_patterns) and isinstance(value, (int, float)):
            #print(f"{key_lower} is amount")
            return self._format_amount(key_lower, value)

        if self._matches_pattern(key_lower, self.byc_patterns) and isinstance(value, (int, float)):
            #print(f"{key_lower} is byc")
            return self._format_byc_amount(key_lower, value)

        if self._matches_pattern(key_lower, self.crt_patterns) and isinstance(value, (int, float)):
            #print(f"{key_lower} is crt")
            return self._format_crt_amount(key_lower, value)

        if self._matches_pattern(key_lower, self.xch_patterns) and isinstance(value, (int, float)):
            #print(f"{key_lower} is xch")
            return self._format_xch_amount(key_lower, value)

        # Check for price fields
        if self._matches_pattern(key_lower, self.price_patterns) and isinstance(value, (int, float)):
            #print(f"{key_lower} is price")
            return self._format_price(key_lower, value)

        # Check for crt price fields
        if self._matches_pattern(key_lower, self.crt_price_patterns) and isinstance(value, int):
            #print(f"{key_lower} is crt price")
            return self._format_crt_price(key_lower, value)

        # Check for address/hash fields
        if self._matches_pattern(key_lower, self.address_patterns) and isinstance(value, str):
            #print(f"{key_lower} is address")
            return self._format_address(value)

        # Check for time period fields
        if self._matches_pattern(key_lower, self.timeperiod_patterns) and isinstance(value, (int, float)):
            #print(f"{key_lower} is time period")
            return self._format_timeperiod(value)

        # Check for timestamp fields
        if self._matches_pattern(key_lower, self.timestamp_patterns) and isinstance(value, (int, float)):
            #print(f"{key_lower} is timestamp")
            return self._format_timestamp(value)

        # Check for hex fields
        if self._matches_pattern(key_lower, self.hex_patterns) and isinstance(value, str):
            #print(f"{key_lower} is hex")
            return self._format_hex(value)

        # Check for ratio fields
        if self._matches_pattern(key_lower, self.ratio_patterns) and isinstance(value, float):
            #print(f"{key_lower} is ratio")
            return self._format_ratio(value)

        # Check for percentage points fields
        if self._matches_pattern(key_lower, self.pct_patterns) and isinstance(value, int):
            #print(f"{key_lower} is percentage points")
            return self._format_pct(value)

        # Check for basis points fields
        if self._matches_pattern(key_lower, self.bps_patterns) and isinstance(value, int):
            #print(f"{key_lower} is basis points")
            return self._format_bps(value)

        #print(f"{key_lower} is of some other type")

        # Handle boolean values
        if isinstance(value, bool):
            if self.use_color:
                C = self.colors
                return f"{C['ok']}✓{C['reset']}" if value else f"{C['err']}✗{C['reset']}"
            return "✓" if value else "✗"

        # Handle large integers (potential amounts in raw form)
        if isinstance(value, int) and value > 1000000:
            # If it looks like mojos, convert to XCH
            if value % 1000000000000 == 0 or value > 1000000000000:
                xch_value = value / self.MOJOS_PER_XCH
                if xch_value >= 0.001:  # Only show if >= 0.001 XCH
                    return f"{value:,} ({xch_value:.6f} XCH)"

        # Handle regular numbers with formatting
        if isinstance(value, (int, float)):
            if isinstance(value, int) and abs(value) >= 1000:
                return f"{value:,}"
            elif isinstance(value, float):
                return f"{value:.6f}".rstrip("0").rstrip(".")

        # Handle strings
        do_not_truncate = [
            "approval_mod_hashes_serialized",
            "statutes_struct_serialized",
        ]
        if isinstance(value, str):
            # Truncate very long strings
            if len(value) > 100 and key_lower not in do_not_truncate:
                return f"{value[:50]}...{value[-47:]}"

        return str(value)

    def _format_amount(self, key: str, value: Union[int, float]) -> str:
        """Format amount fields with appropriate units."""
        if any(term in key for term in ["stability_fee", "principal", "debt", "borrow"]):
            aux = f"({value/self.MCAT_PRECISION:.3f} BYC)"
            if self.use_color and self.colors.get('dim'):
                aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
            return f"{value:,} mBYC {aux}"
        elif any(term in key for term in ["fee", "cost"]):
            # Fees are typically in mojos
            if value >= self.MOJOS_PER_XCH:
                xch_value = value / self.MOJOS_PER_XCH
                aux = f"({xch_value:.6f} XCH)"
                if self.use_color and self.colors.get('dim'):
                    aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
                return f"{value:,} mojos {aux}"
            else:
                return f"{value:,} mojos"
        elif "balance" in key or "amount" in key or "collateral" or "deposit" in key:
            # Check if it's likely XCH (mojos) or CAT tokens
            if value >= 1000000000:  # Likely mojos
                xch_value = value / self.MOJOS_PER_XCH
                aux = f"({xch_value:.6f} XCH)"
                if self.use_color and self.colors.get('dim'):
                    aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
                return f"{value:,} mojos {aux}"
            elif value >= 1000:  # Likely mCAT
                cat_value = value / self.MCAT_PRECISION
                aux = f"({cat_value:.3f} CAT)"
                if self.use_color and self.colors.get('dim'):
                    aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
                return f"{value:,} mCAT {aux}"
            else:
                return f"{value:,}"
        else:
            # Generic number formatting
            if isinstance(value, int) and abs(value) >= 1000:
                return f"{value:,}"
            else:
                return str(value)

    def _format_byc_amount(self, key: str, value: Union[int, float]) -> str:
        """Format BYC amount fields with appropriate units."""
        aux = f"({value/self.MCAT_PRECISION:.3f} BYC)"
        if self.use_color and self.colors.get('dim'):
            aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
        return f"{value:,} mBYC {aux}"

    def _format_crt_amount(self, key: str, value: Union[int, float]) -> str:
        """Format CRT amount fields with appropriate units."""
        aux = f"({value/self.MCAT_PRECISION:.3f} CRT)"
        if self.use_color and self.colors.get('dim'):
            aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
        return f"{value:,} mCRT {aux}"

    def _format_xch_amount(self, key: str, value: Union[int, float]) -> str:
        """Format XCH amount fields with appropriate units."""
        aux = f"({value/self.MOJOS_PER_XCH:.12f} XCH)"
        if self.use_color and self.colors.get('dim'):
            aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
        return f"{value:,} mojos {aux}"

    def _format_price(self, key: str, value: Union[int, float]) -> str:
        """Format price fields with appropriate units."""
        aux = f"({value/PRICE_PRECISION:.2f} XCH/USD)"
        if self.use_color and self.colors.get('dim'):
            aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
        return f"{value:,} XCH/¢USD {aux}"

    def _format_crt_price(self, key: str, value: Union[int, float]) -> str:
        """Format CRT price fields with appropriate units."""
        aux = f"({value/10**10:.10f} CRT/BYC)"
        if self.use_color and self.colors.get('dim'):
            aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
        return f"{value:,} dekaCRT/nanoBYC {aux}"

    def _format_price_info(self, key: str, value: tuple[int, int]) -> str:
        """Format price info fields with appropriate units."""
        #print(f"{value=}")
        return self._format_price(key, value[0]), self._format_timestamp(value[1])

    def _format_address(self, value: str) -> str:
        """Format address/hash fields."""
        return value

    def _format_timeperiod(self, value: Union[int, float]) -> str:
        value = int(value) # drop fractional seconds
        abs_value = abs(value)
        sign = "" if value >= 0 else "-"
        aux = f"({sign}{timedelta(seconds=abs_value)})"
        if self.use_color and self.colors.get('dim'):
            aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
        return f"{value} seconds {aux}"

    def _format_timestamp(self, value: Union[int, float]) -> str:
        """Format timestamp fields."""
        try:
            # Assume Unix timestamp
            dt = datetime.fromtimestamp(value)
            human = dt.strftime('%Y-%m-%d %H:%M:%S')
            if self.use_color and self.colors.get('dim'):
                return f"{int(value)} {self.colors['dim']}({human}){self.colors['reset']}"
            else:
                return f"{int(value)} ({human})"
        except (ValueError, OSError, OverflowError, TypeError):
            # If not a valid timestamp, return as is
            return str(value)

    def _format_hex(self, value: str) -> str:
        """Format hex fields."""
        if len(value) > 20:
            return f"{value[:10]}...{value[-10:]}"
        return value

    def _format_ratio(self, value: float) -> str:
        """Format ratio fields."""
        aux = f"({100*value:.2f}%)"
        if self.use_color and self.colors.get('dim'):
            aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
        return f"{value} {aux}"

    def _format_pct(self, value: int) -> str:
        """Format percentage points fields."""
        aux = f"({value}%)"
        if self.use_color and self.colors.get('dim'):
            aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
        return f"{value:,} pct {aux}"

    def _format_bps(self, value: int) -> str:
        """Format basis points fields."""
        aux = f"({value/100.0}%)"
        if self.use_color and self.colors.get('dim'):
            aux = f"{self.colors['dim']}{aux}{self.colors['reset']}"
        return f"{value:,} bps {aux}"

    def _format_key_name(self, key: str) -> str:
        """Format key names to be more readable."""
        # Convert snake_case to Title Case
        formatted = key.replace("_", " ").title()

        # Handle common abbreviations
        replacements = {
            "Id": "ID",
            "Ttl": "TTL",
            "Url": "URL",
            "Api": "API",
            "Rpc": "RPC",
            "Xch": "XCH",
            "Byc": "BYC",
            "Crt": "CRT",
            "Mcat": "mCAT",
            "Pubkey": "Public Key",
            "Coinname": "Coin Name",
            "Puzzlehash": "Puzzle Hash",
            "M Of N": "M-of-N",
        }

        for old, new in replacements.items():
            formatted = formatted.replace(old, new)

        # Bold common identifier-like keys at root level will be done in _format_dict
        return formatted

    def _sort_keys(self, keys: List[str]) -> List[str]:
        """Sort keys to put most important ones first."""
        priority_keys = ["name", "id", "status", "balance", "amount", "address"]
        constraint_keys = ["threshold_amount_to_propose", "veto_interval", "implementation_delay", "max_delta"]

        sorted_keys = []
        remaining_keys = list(keys)

        if all(key in keys for key in constraint_keys):
            # we are dealing with a statute
            sorted_keys=[]
            if "proposal_times" in keys:
                sorted_keys.append("proposal_times")
            if "statute_index" in keys:
                sorted_keys.append("statute_index")
            if "statute_name" in keys:
                sorted_keys.append("statute_name")
            if "value" in keys:
                sorted_keys.append("value")
            sorted_keys.extend(constraint_keys)
            return sorted_keys

        # Add priority keys first
        for priority_key in priority_keys:
            for key in keys:
                if priority_key in key.lower() and key in remaining_keys:
                    sorted_keys.append(key)
                    remaining_keys.remove(key)

        # Add remaining keys alphabetically
        sorted_keys.extend(sorted(remaining_keys))
        return sorted_keys

    def _matches_pattern(self, text: str, patterns: List[str]) -> bool:
        """Check if text matches any of the given regex patterns."""
        return any(re.match(pattern, text, re.IGNORECASE) for pattern in patterns)


def format_circuit_response(data: Any, use_color: bool | None = None) -> str:
    """Convenience function to format a Circuit response.
    If use_color is None, auto-detect TTY. Pass True/False to force behavior.
    """
    formatter = CircuitJSONFormatter(use_color=use_color)
    return formatter.format_response(data)


# Example usage and test function
def test_formatter():
    """Test the formatter with sample data."""
    sample_data = {
        "wallet_balances": {
            "xch_balance": 5000000000000,  # 5 XCH in mojos
            "byc_balance": 1500000,  # 1500 BYC in mCAT
            "fee_amount": 1000000,  # fee in mojos
        },
        "vault_info": {
            "coin_name": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            "collateral_amount": 10000000000000,
            "borrowed_amount": 500000,
            "is_active": True,
            "created_timestamp": 1693459200,
        },
        "announcer_list": [
            {
                "announcer_id": "0xabcd1234",
                "price": 25.50,
                "is_approved": True,
                "deposit_amount": 1000000000000,
            }
        ],
    }

    formatter = CircuitJSONFormatter()
    print("=== Formatted Output ===")
    print(formatter.format_response(sample_data))


if __name__ == "__main__":
    test_formatter()
