"""
JSON formatter for Circuit CLI responses.

This module provides human-readable formatting for JSON responses from CircuitRPCClient,
with intelligent formatting based on field names and value types.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Union


class CircuitJSONFormatter:
    """Formats JSON responses from CircuitRPCClient in a human-readable way."""

    def __init__(self, use_color: bool | None = None):
        # Common field patterns for different types of values
        self.amount_patterns = [
            r".*amount.*",
            r".*balance.*",
            r".*deposit.*",
            r".*fee.*",
            r".*collateral.*",
            r".*debt.*",
            r".*borrowed.*",
            r".*value.*",
            r".*price.*",
            r".*min_.*",
            r".*max_.*",
            r".*threshold.*",
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

        self.timestamp_patterns = [
            r".*timestamp.*",
            r".*time.*",
            r".*created_at.*",
            r".*updated_at.*",
            r".*expires.*",
            r".*deadline.*",
        ]

        self.hex_patterns = [r".*_hex$", r".*hash$", r".*signature.*", r".*pubkey.*", r".*public_key.*"]

        # Mojos per XCH (standard Chia conversion)
        self.MOJOS_PER_XCH = 1000000000000
        self.MCAT_PRECISION = 1000
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

    def _format_list(self, data: List[Any], indent: int = 0) -> str:
        """Format a list with appropriate formatting."""
        if not data:
            return "[]"

        lines = []
        prefix = "  " * indent
        bullet = "•" if True else "-"

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

        # Handle nested structures
        if isinstance(value, dict):
            return self._format_dict(value, indent)
        elif isinstance(value, list):
            return self._format_list(value, indent)

        # Handle primitive values with context-aware formatting
        key_lower = key.lower()

        # Check for amount/balance fields
        if self._matches_pattern(key_lower, self.amount_patterns) and isinstance(value, (int, float)):
            return self._format_amount(key_lower, value)

        # Check for address/hash fields
        if self._matches_pattern(key_lower, self.address_patterns) and isinstance(value, str):
            return self._format_address(value)

        # Check for timestamp fields
        if self._matches_pattern(key_lower, self.timestamp_patterns) and isinstance(value, (int, float)):
            return self._format_timestamp(value)

        # Check for hex fields
        if self._matches_pattern(key_lower, self.hex_patterns) and isinstance(value, str):
            return self._format_hex(value)

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
        if isinstance(value, str):
            # Truncate very long strings
            if len(value) > 100:
                return f"{value[:50]}...{value[-47:]}"

        return str(value)

    def _format_amount(self, key: str, value: Union[int, float]) -> str:
        """Format amount fields with appropriate units."""
        if "price" in key:
            # Prices are typically in USD with precision
            return f"${value:.6f}".rstrip("0").rstrip(".")
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
        elif "balance" in key or "amount" in key or "collateral" in key:
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

    def _format_address(self, value: str) -> str:
        """Format address/hash fields."""
        return value

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
            "Mcat": "mCAT",
            "Pubkey": "Public Key",
            "Coinname": "Coin Name",
            "Puzzlehash": "Puzzle Hash",
        }

        for old, new in replacements.items():
            formatted = formatted.replace(old, new)

        # Bold common identifier-like keys at root level will be done in _format_dict
        return formatted

    def _sort_keys(self, keys: List[str]) -> List[str]:
        """Sort keys to put most important ones first."""
        priority_keys = ["name", "id", "status", "balance", "amount", "address"]

        sorted_keys = []
        remaining_keys = list(keys)

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
