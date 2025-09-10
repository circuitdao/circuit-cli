import asyncio
import httpx
import logging
import logging.config
import sys
from math import floor
from typing import Optional, Dict, Any

from chia.util.bech32m import encode_puzzle_hash
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    puzzle_hash_for_synthetic_public_key,
)
from chia_rs import PrivateKey, SpendBundle, Coin
from httpx import AsyncClient

from circuit_cli.utils import generate_ssks, sign_spends
from circuit_cli.persistence import DictStore

log = logging.getLogger(__name__)


def setup_console_logging():
    """Setup console-friendly logging for CLI usage.

    This function is safe to call multiple times and will NOT override
    an existing logging configuration (e.g., configured by the CLI based
    on --verbose). If no handlers are configured yet, it applies a sane
    default suitable for library usage.
    """
    root_logger = logging.getLogger()
    if root_logger.handlers:
        # Respect existing configuration set by the CLI.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("circuit_cli").setLevel(root_logger.level or logging.INFO)
        return

    # Fallback default (library context). Keep it simple and avoid duplicate handlers.
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(fmt="%(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("circuit_cli").setLevel(logging.INFO)


class APIError(Exception):
    """Custom exception for API-related errors."""

    def __init__(self, message: str, response: Optional[httpx.Response] = None):
        super().__init__(message)
        self.response = response


class CircuitRPCClient:
    """High-level asynchronous client for interacting with the Circuit RPC API.

    This client wraps HTTP requests to the Circuit API, handles spend bundle
    signing and submission, and provides convenience helpers for common
    workflows used by the CLI. It is designed for programmatic use as well as
    being driven by the circuit_cli entrypoints.

    Key features:
    - Manages a set of synthetic public/private keys derived from a master key.
    - Converts human-friendly amounts to on-chain units (mojos, MCAT, price units).
    - Standardizes payload construction and error handling.
    - Signs SpendBundles locally and submits them to the RPC service.
    - Optionally waits for transaction confirmation and can emit progress events.

    Notes:
    - Most methods correspond 1:1 with RPC endpoints and either fetch state
      or produce a SpendBundle to be signed and pushed.
    - Set "progress_handler" to a callable (sync or async) to receive progress
      updates during long-running operations like confirmations.
    - Use "set_fee_per_cost" to resolve dynamic fee presets (e.g. "fast").
    """

    def __init__(
        self,
        base_url: str,
        private_key: str,
        add_sig_data: str = None,
        fee_per_cost: int | str = "fast",
        client: Optional[AsyncClient] = None,
        key_count: int = 500,
        no_wait_for_tx: bool = False,
        dict_store_path: str = None,
    ):
        """Create a CircuitRPCClient.

        Args:
            base_url: Base URL of the Circuit API service.
            private_key: Hex-encoded master private key for deriving synthetic keys.
            add_sig_data: Optional additional domain-separation string added to signatures.
            fee_per_cost: Either a numeric fee-per-cost value or a preset name (e.g. "fast", "medium").
            client: Optional pre-configured httpx.AsyncClient to use instead of creating a new one.
            key_count: Number of synthetic keys to derive from the master key.
            no_wait_for_tx: If True, skip waiting for confirmation after submitting transactions.

        Notes:
            - If no private_key is provided, operations requiring signatures will fail.
            - Call set_fee_per_cost() before submitting transactions when using presets.
        """
        # Setup console-friendly logging
        setup_console_logging()

        if private_key:
            secret_key = PrivateKey.from_bytes(bytes.fromhex(private_key))
            synthetic_secret_keys = generate_ssks(secret_key, 0, key_count)
            synthetic_public_keys = [x.get_g1() for x in synthetic_secret_keys]
        else:
            log.warning(
                "** No master private key found. Set environment variable PRIVATE_KEY or use --private-key cmd line option. **"
            )
            synthetic_secret_keys = []
            synthetic_public_keys = []
        self.synthetic_secret_keys = synthetic_secret_keys
        self.synthetic_public_keys = synthetic_public_keys
        #if private_key:
        #    log.info("Wallet first 5 addresses:")
        #    log.info("Puzzle hash: %s", puzzle_hash_for_synthetic_public_key(synthetic_public_keys[0]))
        #    log.info(
        #        [encode_puzzle_hash(puzzle_hash_for_synthetic_public_key(x), "txch") for x in synthetic_public_keys[:5]]
        #    )

        self.consts = {
            "price_PRECISION": 1000000,  # Default 6 decimals
            "MOJOS": 1000000000000,  # Default XCH to mojo conversion
            "MCAT": 1000,  # Default CAT decimals
        }
        self.base_url = base_url
        # Use injected client if provided, otherwise create httpx.Client
        self.client = client if client is not None else httpx.AsyncClient(base_url=base_url, timeout=120)
        log.info(f"Using add_sig_data={add_sig_data}")
        self.add_sig_data = add_sig_data
        self._fee_per_cost = fee_per_cost
        self.fee_per_cost: float | None = None
        self.no_wait_for_confirmation = no_wait_for_tx
        log.info(f"Using no_wait_for_confirmation={no_wait_for_tx}")
        log.info(f"Using key_count={key_count}")
        log.info(f"Using dict_store_path={dict_store_path}")
        self.store = DictStore(dict_store_path)
        # Optional progress handler for streaming progress events
        # It can be a sync or async callable accepting a single dict argument
        self.progress_handler = None
        # When True, print each HTTP endpoint used to stderr (human-friendly tracing)
        self.show_endpoints: bool = False

    async def _emit_progress(self, event: Dict[str, Any]):
        """Emit a progress event to the configured handler if set."""
        handler = self.progress_handler
        if handler is None:
            return
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(event)
            else:
                handler(event)
        except Exception as e:
            log.debug(f"Progress handler raised exception: {e}")

    async def set_fee_per_cost(self) -> int:
        """Resolve and set fee_per_cost based on preset or explicit value.

        Returns:
            The resolved fee_per_cost value as an integer/float stored on self.fee_per_cost.
        """
        if self._fee_per_cost is None:
            self.fee_per_cost = 0
        elif self._fee_per_cost in ("fast", "medium"):
            response_data = await self._make_api_request("POST", "/statutes", {"full": False})
            fee_per_costs = response_data.get("fee_per_costs")
            self.fee_per_cost = fee_per_costs.get(self._fee_per_cost)
        else:
            self.fee_per_cost = float(self._fee_per_cost)
        log.info("Set fee_per_cost to: %s", self.fee_per_cost)

    @property
    def synthetic_pks_hex(self) -> list[str]:
        """Get synthetic public keys as hex strings - commonly used in API calls."""
        return [key.to_bytes().hex() for key in self.synthetic_public_keys]

    async def _make_api_request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any] | list:
        """Make a standardized API request with error handling.

        Args:
            method: HTTP method (e.g., "GET", "POST").
            endpoint: API endpoint path, starting with "/".
            json_data: Optional JSON body for POST requests.
            params: Optional query parameters for GET requests.

        Returns:
            Parsed JSON response as a dict or list.

        Raises:
            APIError: If the request fails or a network error occurs.
            ValueError: If an unsupported HTTP method is used.
        """
        try:
            log.info(f"Making request to {method} {endpoint} with params {params} and json_data {json_data}")
            # Optional human-friendly endpoint trace in text mode
            if getattr(self, "show_endpoints", False):
                try:
                    import sys as _sys

                    base = getattr(self, "base_url", "")
                    _sys.stderr.write(f"→ HTTP {method.upper()} {base}{endpoint}\n")
                    _sys.stderr.flush()
                except Exception:
                    pass
            if method.upper() == "GET":
                response = await self.client.get(endpoint, params=params)
            elif method.upper() == "POST":
                response = await self.client.post(endpoint, json=json_data)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if response.is_error:
                error_msg = f"API request failed: {method} {endpoint}"
                try:
                    error_detail = response.json().get("detail", str(response.content))
                    error_msg += f" - {error_detail}"
                except Exception:
                    error_msg += f" - Status: {response.status_code}, Content: {response.content}"

                log.error(error_msg)
                raise APIError(error_msg, response)

            return response.json()

        except httpx.RequestError as e:
            error_msg = f"Network error during {method} {endpoint}: {e}"
            log.exception(error_msg)
            raise APIError(error_msg)
        except Exception as e:
            if isinstance(e, APIError):
                raise
            error_msg = f"Unexpected error during {method} {endpoint}: {e}"
            log.exception(error_msg)
            raise APIError(error_msg)

    def _build_base_payload(self, **kwargs) -> Dict[str, Any]:
        """Build base payload with synthetic_pks and other common fields."""
        payload = {
            "synthetic_pks": self.synthetic_pks_hex,
        }
        payload.update(kwargs)
        return payload

    def _build_transaction_payload(self, endpoint_specific_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build payload for transaction endpoints.

        The resulting payload includes synthetic public keys and the resolved
        fee_per_cost value, merged with endpoint-specific fields provided.
        """
        base_payload = {
            "synthetic_pks": self.synthetic_pks_hex,
            "fee_per_cost": self.fee_per_cost,
        }
        base_payload.update(endpoint_specific_data)
        return base_payload

    def _convert_number(self, number: str | float | int | None, unit_type: str = None, ceil=False) -> int | None:
        """Convert a human-friendly number to on-chain units (if not already in on-chain units).

        Args:
            number: A str or float in human-friendly units, an int in on-chain units, or None.
            unit_type: Unit type to convert to. One of: "MOJOS", "MCAT", "PRICE".
            ceil: By default, converted numbers are rounded down, if ceil is True, converted number is rounded up.

        Returns:
            Integer number of base units according to the selected unit_type.

        Raises:
            ValueError: If an unknown unit_type is provided.
            TypeError: If number is of type other than str, float, int or None
        """
        if number is None:
            return None
        if isinstance(number, (str, float)):
            if unit_type == "MOJOS":
                if ceil:
                    return ceil(number * self.consts["MOJOS"])
                else:
                    return floor(number * self.consts["MOJOS"])
            elif unit_type == "MCAT":
                if ceil:
                    return ceil(number * self.consts["MCAT"])
                else:
                    return floor(number * self.consts["MCAT"])
            elif unit_type == "PRICE":
                if ceil:
                    return ceil(number * self.consts["price_PRECISION"])
                else:
                    return floor(number * self.consts["price_PRECISION"])
            elif unit_type is None:
                raise ValueError("Unit type must not be None when converting str or float to int")
            else:
                raise ValueError(f"Unknown unit type: {unit_type}")
        if isinstance(number, int):
            return number
        raise TypeError(f"Can only convert from str, float and int to int, got {type(number).__name__}")

    async def _process_transaction(
        self, endpoint: str, payload: Dict[str, Any], error_handling_info: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Process a transaction: make API call, sign bundle, push, and wait for confirmation.

        Args:
            endpoint: API endpoint
            payload: Request payload
            error_handling_info: Optional error handling information

        Returns:
            Response from sign_and_push
        """
        log.info("Processing transaction: %s", endpoint)
        # Make API request to get bundle
        response_data = await self._make_api_request("POST", endpoint, payload)

        # Extract bundle from response
        bundle_data = response_data.get("bundle")
        bundle = None
        if bundle_data:
            bundle = SpendBundle.from_json_dict(bundle_data)
        elif response_data.get("coin_spends"):
            bundle = SpendBundle.from_json_dict(response_data)
        if not bundle:
            raise APIError(f"No bundle in response from {endpoint}")
        log.info("Bundle extracted from response: %s", bundle.name().hex())
        # Sign and push transaction
        sig_response = await self.sign_and_push(bundle, error_handling_info)

        # Wait for confirmation
        signed_bundle = SpendBundle.from_json_dict(sig_response["bundle"])
        log.info("Waiting for confirmation of transaction ID %s", signed_bundle.name().hex())
        await self.wait_for_confirmation(signed_bundle)

        return sig_response

    async def _get_coin_name_if_needed(
        self, coin_name: Optional[str], endpoint: str, error_message: str = None, payload_extras=None
    ) -> str:
        """
        Helper method to get coin_name if not provided by querying the endpoint.

        Args:
            coin_name: The coin name if already provided
            endpoint: API endpoint to query for coin names
            error_message: Custom error message base (optional)

        Returns:
            The coin name to use
        """
        if coin_name:
            return coin_name

        # Query the endpoint to get available coins
        payload = self._build_base_payload()
        if payload_extras is not None:
            payload.update(payload_extras)
        data = await self._make_api_request("POST", endpoint, payload)
        base_error = error_message or "No coin found"
        assert len(data) > 0, base_error
        assert len(data) == 1, "More than one coin found. Must provide coin_name"
        return data[0]["name"]

    async def wait_for_confirmation(self, bundle: SpendBundle = None, blocks=None, stream: bool = False):
        """Wait until a transaction is confirmed or a number of blocks elapse.

        Args:
            bundle: Optional SpendBundle. If provided, waits for this transaction ID.
            blocks: Optional block count to wait for; if None, uses server defaults.
            stream: If True, yields progress events via progress_handler while waiting.

        Returns:
            A dict with receipt information if available or final status booleans.

        Notes:
            - When stream=True and bundle is provided, this becomes an async generator
              that yields progress events dicts until completion.
            - Otherwise, it returns True when done (backward compatible behavior).
        """
        if self.no_wait_for_confirmation:
            if stream:
                # stream a single completion event for consistency
                async def _gen():
                    ev = {"event": "skipped", "reason": "no_wait_for_confirmation", "done": True}
                    await self._emit_progress(ev)
                    yield ev

                return _gen()
            else:
                await self._emit_progress({"event": "skipped", "reason": "no_wait_for_confirmation", "done": True})
                return True
        if bundle is not None and isinstance(bundle, SpendBundle):
            tx_id = bundle.name().hex()

            async def _wait_gen():
                attempt = 0
                while True:
                    attempt += 1
                    if getattr(self, "show_endpoints", False):
                        try:
                            import sys as _sys

                            base = getattr(self, "base_url", "")
                            _sys.stderr.write(f"→ HTTP POST {base}/transactions/status\n")
                            _sys.stderr.flush()
                        except Exception:
                            pass
                    response = await self.client.post("/transactions/status", json={"bundle": bundle.to_json_dict()})
                    if response.status_code != 200:
                        content = None
                        try:
                            content = response.json()
                        except Exception:
                            content = response.text
                        # Yield an error event and raise to stop
                        ev = {
                            "event": "error",
                            "attempt": attempt,
                            "status_code": response.status_code,
                            "content": content,
                            "tx_id": tx_id,
                        }
                        await self._emit_progress(ev)
                        yield ev
                        response.raise_for_status()
                    data = response.json()
                    status = data.get("status")
                    # Include any extra fields to help the UI
                    ev = {"event": "poll", "attempt": attempt, "status": status, "tx_id": tx_id}
                    await self._emit_progress(ev)
                    yield ev
                    if status == "confirmed":
                        log.info("Transaction confirmed. ID %s", tx_id)
                        evc = {"event": "confirmed", "tx_id": tx_id, "done": True}
                        await self._emit_progress(evc)
                        yield evc
                        return
                    elif status == "failed":
                        evf = {"event": "failed", "tx_id": tx_id, "done": True}
                        await self._emit_progress(evf)
                        yield evf
                        raise ValueError(f"Transaction failed. ID {tx_id}")
                    log.info(f"Still waiting for confirmation of transaction ID {tx_id}")
                    await asyncio.sleep(5)

            if stream:
                return _wait_gen()
            else:
                # consume generator until it completes, returning boolean
                async for ev in _wait_gen():
                    if ev.get("event") in ("confirmed", "failed"):
                        # confirmed already returns before, failed raises; this is for completeness
                        pass
                return True
        elif blocks is not None:
            if stream:

                async def _blocks_gen():
                    # simulate block waiting progress events
                    total = int(blocks)
                    for i in range(total):
                        evs = {"event": "sleep", "remaining_blocks": total - i, "tx_id": None}
                        await self._emit_progress(evs)
                        yield evs
                        await asyncio.sleep(55)
                    evd = {"event": "done", "done": True}
                    await self._emit_progress(evd)
                    yield evd

                return _blocks_gen()
            else:
                # Emit a single done event after sleeping for compatibility
                await asyncio.sleep(blocks * 55)
                await self._emit_progress({"event": "done", "done": True})
                return True
        else:
            raise ValueError("Either bundle or number of blocks must be provided")

    async def sign_and_push(self, bundle: SpendBundle, error_handling_info: dict = None):
        """Sign a SpendBundle locally and push it to the RPC service.

        Args:
            bundle: The unsigned or partially signed SpendBundle to sign.
            error_handling_info: Optional dict with server-specific error handling hints.

        Returns:
            The server response JSON as a dict, typically including the signed bundle.

        Raises:
            APIError: If the push fails (non-2xx response).
        """
        signed_bundle = await sign_spends(
            bundle.coin_spends,
            self.synthetic_secret_keys,
            add_data=self.add_sig_data,
        )
        assert isinstance(signed_bundle, SpendBundle)
        log.info("Signed bundle: %s, pushing", signed_bundle.name().hex())
        response = await self.client.post(
            "/sign_and_push",
            json={
                "bundle_dict": signed_bundle.to_json_dict(),
                "signature": signed_bundle.aggregated_signature.to_bytes().hex(),
                "error_handling_info": error_handling_info,
            },
        )
        if response.is_error:
            error_msg = f"Error during transaction push: {response.content}"
            if error_handling_info is not None:
                error_msg += f" Error handling info: {error_handling_info}"
            raise APIError(error_msg, response)
        log.info("Transaction signed and broadcast. ID %s", signed_bundle.name().hex())
        return response.json()

    ### WALLET ###
    async def wallet_addresses(self, derivation_index: int, puzzle_hashes=False):
        payload = self._build_base_payload(
            derivation_index=derivation_index,
            include_puzzle_hashes=puzzle_hashes,
        )
        return await self._make_api_request("POST", "/wallet/addresses", payload)

    async def wallet_balances(self):
        """
        Get wallet balances for XCH, BYC, and CRT coins.

        Retrieves the current balance information for all supported coin types
        in the user's wallet. This includes XCH (Chia), BYC (stablecoin), and
        CRT (governance tokens) that are not currently in governance mode.

        Returns:
            dict: A dictionary containing balance information with keys:
                - xch_balance: XCH balance in mojos
                - byc_balance: BYC balance in mBYC
                - crt_balance: CRT balance in mCRT
                - total_coins: Total number of coins
                - pending_balance: Pending transactions balance

        Example:
            balances = client.wallet_balances()
            # Returns: {"xch_balance": 5000000000000, "byc_balance": 1500000, ...}
        """
        payload = self._build_base_payload()
        return await self._make_api_request("POST", "/balances", payload)

    async def wallet_coins(self, type=None):
        """
        Get detailed information about individual coins in the wallet.

        Retrieves information about individual coins owned by the wallet.
        By default, returns CRT coins not in governance mode, but can be
        filtered to show specific coin types.

        Args:
            type (str, optional): Filter coins by type. Options are:
                - "xch": XCH coins only
                - "byc": BYC coins only
                - "crt": CRT coins only
                - "all": All coin types
                - "gov": Governance coins only
                - "empty": Empty governance coins
                - "bill": Governance coins with bills
                - None: Default CRT coins not in governance mode

        Returns:
            dict: Contains coin information with keys:
                - coins: List of coin objects with details like coin_name, amount, etc.
                - total_count: Total number of coins
                - confirmed_count: Number of confirmed coins

        Example:
            coins_info = client.wallet_coins(type="xch")
            # Returns: {"coins": [...], "total_count": 5, "confirmed_count": 4}
        """
        payload = self._build_base_payload(coin_type=type)
        return await self._make_api_request("POST", "/coins", payload)

    async def wallet_toggle(self, coin_name: str, info=False):
        """
        Convert a CRT coin between plain mode and governance mode.

        Toggles a CRT coin between plain CRT mode and governance mode.
        If the coin is in governance mode, exits to plain CRT. If the coin
        is plain CRT, activates governance mode.

        Args:
            coin_name (str): The name/ID of the coin to toggle
            info (bool): If True, show information about toggling without executing.
                        Defaults to False.

        Returns:
            dict: Transaction result or information about the toggle operation

        Example:
            result = client.wallet_toggle("0xabc123...")
            # Toggles the coin between governance and plain modes
        """
        return await self.bills_toggle(coin_name, info)

    ### COLLATERAL VAULT ###
    async def vault_show(self):
        """
        Show information about the user's collateral vault.

        Displays comprehensive information about the user's vault including
        collateral amount, borrowed amount, health ratio, liquidation status,
        and other vault parameters.

        Returns:
            dict: Vault information containing:
                - vault_id: Unique identifier for the vault
                - collateral_amount: XCH collateral in mojos
                - borrowed_amount: BYC debt in mCAT units
                - is_healthy: Boolean indicating vault health
                - liquidation_ratio: Current liquidation ratio
                - stability_fee_rate: Current stability fee rate

        Example:
            vault_info = client.vault_show()
            # Returns: {"vault_id": "0x...", "collateral_amount": 10000000000000, ...}
        """
        payload = self._build_base_payload()
        return await self._make_api_request("POST", "/vault", payload)

    async def vault_deposit(self, amount):
        """
        Deposit XCH collateral into the vault.

        Adds XCH collateral to the user's vault, improving the collateral ratio
        and vault health. This allows for increased borrowing capacity.

        Args:
            amount (float): Amount of XCH to deposit as collateral

        Returns:
            dict: Transaction result with bundle and status information

        Example:
            result = client.vault_deposit(5.0)
            # Deposits 5 XCH as collateral into the vault
        """
        payload = self._build_transaction_payload({"amount": self._convert_number(amount, "MOJOS")})
        return await self._process_transaction("/vault/deposit", payload)

    async def vault_withdraw(self, amount):
        """
        Withdraw XCH collateral from the vault.

        Removes XCH collateral from the user's vault. This reduces the
        collateral ratio and may require repaying debt to maintain vault health.

        Args:
            amount (float): Amount of XCH collateral to withdraw

        Returns:
            dict: Transaction result with bundle and status information

        Example:
            result = client.vault_withdraw(2.0)
            # Withdraws 2 XCH collateral from the vault
        """
        payload = self._build_transaction_payload({"amount": self._convert_number(amount, "MOJOS")})
        return await self._process_transaction("/vault/withdraw", payload)

    async def vault_borrow(self, amount):
        """
        Borrow BYC stablecoin against vault collateral.

        Borrows BYC stablecoin against the XCH collateral in the vault.
        The amount that can be borrowed depends on the collateral ratio
        and current stability parameters.

        Args:
            amount (float): Amount of BYC to borrow

        Returns:
            dict: Transaction result with bundle and status information

        Example:
            result = client.vault_borrow(1000.0)
            # Borrows 1000 BYC against vault collateral
        """
        payload = self._build_transaction_payload({"amount": self._convert_number(amount, "MCAT")})
        return await self._process_transaction("/vault/borrow", payload)

    async def vault_repay(self, amount):
        """
        Repay BYC debt to the vault.

        Repays BYC debt to reduce the outstanding loan amount and improve
        the vault's collateral ratio and health status.

        Args:
            amount (float): Amount of BYC debt to repay

        Returns:
            dict: Transaction result with bundle and status information

        Example:
            result = client.vault_repay(500.0)
            # Repays 500 BYC of vault debt
        """
        payload = self._build_transaction_payload({"amount": self._convert_number(amount, "MCAT")})
        return await self._process_transaction("/vault/repay", payload)

    ### SAVINGS VAULT ###
    async def savings_show(self):
        """
        Show information about the user's savings vault.

        Displays information about BYC savings including total deposited amount,
        interest earned, and available withdrawal balance.

        Returns:
            dict: Savings information containing:
                - total_deposited: Total BYC deposited in mCAT units
                - interest_earned: Interest earned in mCAT units
                - available_to_withdraw: Amount available for withdrawal
                - interest_rate: Current interest rate

        Example:
            savings_info = client.savings_show()
            # Returns: {"total_deposited": 5000000, "interest_earned": 125000, ...}
        """
        payload = self._build_base_payload()
        return await self._make_api_request("POST", "/savings", payload)

    async def savings_deposit(self, amount, interest=None):
        response = self.client.post(
            "/savings/deposit",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "amount": self._convert_number(amount, "MCAT"),
                "treasury_withdraw_amount": self._convert_number(interest, "MCAT"),
                "fee_per_cost": self.fee_per_cost,
            },
        )
        if response.is_error:
            print(response.content)
            response.raise_for_status()
        data = response.json()
        if "message" in data.keys():
            return data
        bundle: SpendBundle = SpendBundle.from_json_dict(data["bundle"])
        sig_response = await self.sign_and_push(bundle)
        signed_bundle = SpendBundle.from_json_dict(sig_response["bundle"])
        await self.wait_for_confirmation(signed_bundle)
        return sig_response

    async def savings_withdraw(self, amount, interest=None):
        response = self.client.post(
            "/savings/withdraw",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "amount": self._convert_number(amount, "MCAT"),
                "treasury_withdraw_amount": self._convert_number(interest, "MCAT"),
                "fee_per_cost": self.fee_per_cost,
            },
        )
        if response.is_error:
            print(response.content)
            response.raise_for_status()
        data = response.json()
        if "message" in data.keys():
            return data
        bundle: SpendBundle = SpendBundle.from_json_dict(data["bundle"])
        sig_response = await self.sign_and_push(bundle)
        signed_bundle = SpendBundle.from_json_dict(sig_response["bundle"])
        await self.wait_for_confirmation(signed_bundle)
        return sig_response

    ### ANNOUNCERS ###
    async def announcer_show(self, approved=False, valid=False, penalizable=False, incl_spent=False):
        """List announcer coins with optional filters.

        Args:
            approved: Include only approved announcers.
            valid: Include only valid (not expired/invalidated) announcers.
            penalizable: Include announcers eligible for penalty.
            incl_spent: Include already spent coins in the results.

        Returns:
            A list of announcer coin records returned by the RPC service.
        """
        payload = self._build_base_payload(
            approved=approved, valid=valid, penalizable=penalizable, include_spent_coins=incl_spent
        )
        data = await self._make_api_request("POST", "/announcer", payload)
        assert isinstance(data, list)
        return data

    async def announcer_launch(self, price):
        """Create and activate a new announcer with the given price.

        This constructs the appropriate payload, requests a spend bundle from
        the server, signs it locally, submits it, and waits for confirmation.

        Args:
            price: The initial price value to register for the announcer.

        Returns:
            The sign_and_push response, typically including the signed bundle.
        """
        log.info("Launching announcer...")
        payload = self._build_transaction_payload(
            {"operation": "launch", "args": {"price": self._convert_number(price, "PRICE")}}
        )
        log.info(f"Launching announcer with price: {price}")
        try:
            return await self._process_transaction("/announcers/launch/", payload)
        finally:
            log.info("Launching announcer successful.")

    async def announcer_configure(
        self,
        coin_name,
        make_approvable=False,
        deposit=None,
        min_deposit=None,
        inner_puzzle_hash=None,
        price=None,
        ttl=None,
        cancel_deactivation=False,
        deactivate=False,
    ):
        """Update configuration parameters for an existing announcer.

        If coin_name is not provided, the client will query for a single
        announcer and use its coin name; otherwise the provided coin is used.

        Args:
            coin_name: Announcer coin name (optional; auto-detected if unique).
            make_approvable: Toggle whether the announcer can be approved.
            deactivate: Deactivate the announcer. None to keep current approval status
            deposit: New deposit amount (raw integer units expected by server).
            min_deposit: New minimum deposit threshold (raw integer units).
            inner_puzzle_hash: New inner puzzle hash to set.
            price: New price value to set.
            ttl: New price time-to-live value.

        Returns:
            The transaction result from processing the configuration update.
        """

        assert not (cancel_deactivation and deactivate), (
            "Cannot both deactivate and cancel deactivation at the same time"
        )

        coin_name = await self._get_coin_name_if_needed(coin_name, "/announcer", "No announcer found")

        # Build args using helper methods for amount conversions
        args = {
            "make_approvable": make_approvable,
            "new_deposit": self._convert_number(deposit, "MOJOS"),
            "new_min_deposit": self._convert_number(min_deposit, "MOJOS"),
            "new_inner_puzzle_hash": inner_puzzle_hash,
            "new_price": self._convert_number(price, "PRICE"),
            "new_price_ttl": self._convert_number(ttl),
            "cancel_deactivation": cancel_deactivation,
            "deactivate": deactivate,
        }

        payload = self._build_transaction_payload({"operation": "configure", "args": args})
        return await self._process_transaction(f"/announcers/{coin_name}/", payload)

    async def announcer_register(self, coin_name=None):
        """Register an announcer coin for participation.

        If coin_name is omitted and there is exactly one eligible announcer,
        it will be used automatically; otherwise an explicit coin_name is required.
        """
        coin_name = await self._get_coin_name_if_needed(
            coin_name, "/announcers", "No announcer found", payload_extras={"approved": True, "valid": True}
        )
        payload = self._build_transaction_payload({"operation": "register", "args": {}})
        return await self._process_transaction(f"/announcers/{coin_name}/", payload)

    async def announcer_exit(self, coin_name):
        """Exit (deactivate) a registered announcer coin.

        If coin_name is not provided and exactly one announcer exists, it will
        be selected automatically; otherwise pass the specific coin name.
        """
        coin_name = await self._get_coin_name_if_needed(coin_name, "/announcers", "No announcer found")
        payload = self._build_transaction_payload({"operation": "exit", "args": {}})
        return await self._process_transaction(f"/announcers/{coin_name}/", payload)

    async def announcer_update(self, price, coin_name=None, fee_coin=False):
        """Mutate an announcer by updating its price and optional fee coin attachment.

        If coin_name is omitted and exactly one announcer exists, it will be
        used automatically; otherwise specify which announcer to update.

        Args:
            price: New price value to set (integer units expected by server).
            coin_name: Optional announcer coin name to target.
            fee_coin: Whether to attach a fee coin for the update operation.
        """
        coin_name = await self._get_coin_name_if_needed(coin_name, "/announcer", "No announcer found")

        args = {
            "new_price": self._convert_number(price, "PRICE"),
            "attach_fee_coin": fee_coin,
        }
        payload = self._build_transaction_payload({"operation": "mutate", "args": args})
        return await self._process_transaction(f"/announcers/{coin_name}/", payload)

    ### UPKEEP ###
    async def upkeep_invariants(self):
        """Fetch protocol invariants from the RPC server.

        Returns a static snapshot of invariant checks useful for diagnostics.
        """
        return await self._make_api_request("GET", "/protocol/invariants")

    async def upkeep_state(
        self, vaults=False, surplus_auctions=False, recharge_auctions=False, treasury=False, bills=False
    ):
        """Fetch protocol state sections with optional filtering.

        If no specific section flags are provided, the full state is returned.
        """
        # If no option is specified, whole state is returned by default
        if not (vaults or surplus_auctions or recharge_auctions or treasury or bills):
            vaults = True
            surplus_auctions = True
            recharge_auctions = True
            treasury = True
            bills = True

        payload = {
            "vaults": vaults,
            "surplus_auctions": surplus_auctions,
            "recharge_auctions": recharge_auctions,
            "treasury": treasury,
            "bills": bills,
        }
        data = await self._make_api_request("POST", "/protocol/state", payload)
        clean_data = {k: v for k, v in data.items() if v is not None}
        return clean_data

    ## RPC server ##
    async def upkeep_rpc_sync(self):
        """Trigger a chain data sync on the RPC server."""
        return await self._make_api_request("POST", "/sync_chain_data")

    async def upkeep_rpc_status(self):
        """Fetch the health/status of the RPC server."""
        return await self._make_api_request("GET", "/health")

    async def upkeep_rpc_version(self):
        """Return the RPC server version string."""
        return await self._make_api_request("GET", "/rpc/version")

    ## Announcer ##
    async def upkeep_announcers_list(
        self, coin_name=None, approved=False, valid=False, penalizable=False, incl_spent=False
    ):
        """List announcers, optionally filtering and/or targeting a specific coin.

        Args:
            coin_name: Optional announcer coin name to show details for.
            approved: Filter by approved state.
            valid: Filter by validity state.
            penalizable: Filter by penalizable state.
            incl_spent: Include spent coins in the listing.
        """
        if coin_name:
            payload = {
                "coin_name": coin_name,
                "approved": approved,
                "valid": valid,
                "penalizable": penalizable,
                "include_spent_coins": incl_spent,
            }
            return await self._make_api_request("POST", f"/announcers/{coin_name}", payload)

        payload = {
            "approved": True,
            "valid": valid,
            "penalizable": penalizable,
            "include_spent_coins": incl_spent,
        }
        return await self._make_api_request("POST", "/announcers", payload)

    async def self_unlock(self):
        """Release the store lock."""
        self.store.unlock()

    async def upkeep_announcers_approve(self, coin_name, create_conditions=False, bill_coin_name=None, label=None):
        if create_conditions:
            assert bill_coin_name is None, "Cannot create custom conditions and implement bill at the same time"
            response = await self.client.post(
                f"/announcers/{coin_name}/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "operation": "govern",
                    "args": {"toggle_activation": True},
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if response.is_error:
                print(response.content)
                response.raise_for_status()
            data = response.json()
            if label:
                with self.store.lock():
                    self.store.set(f"proposals.values.{label}", data["announcements_to_vote_for"])
            return data
        else:
            if bill_coin_name is not None:
                if bill_coin_name.startswith("<") and bill_coin_name.endswith(">"):
                    label = bill_coin_name[1:-1]
                    bill_coin_name = self.store.get(f"proposals.propose.coins.{label}")

            assert bill_coin_name is not None, "Must specify bill to implement when not creating custom conditions"
            bill_response = await self.client.post(
                "/bills/implement",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "coin_name": bill_coin_name,
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if bill_response.is_error:
                print(bill_response.content)
                bill_response.raise_for_status()
            bill_response_dict = bill_response.json()
            implement_bundle = bill_response_dict["bundle"]
            statutes_mutation_spend = bill_response_dict["statutes_mutation_spend"]
            govern_response = await self.client.post(
                f"/announcers/{coin_name}/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "operation": "govern",
                    "args": {
                        "toggle_activation": True,
                        "implement_bundle": implement_bundle,
                        "statutes_mutation_spend": statutes_mutation_spend,
                    },
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if govern_response.is_error:
                print(govern_response.content)
                govern_response.raise_for_status()
            bundle = SpendBundle.from_json_dict(govern_response.json()["bundle"])
            sig_response = await self.sign_and_push(bundle)
            signed_bundle: SpendBundle = SpendBundle.from_json_dict(sig_response["bundle"])
            await self.wait_for_confirmation(signed_bundle)
            return sig_response

    async def upkeep_announcers_disapprove(self, coin_name, create_conditions=False, bill_coin_name=None):
        if create_conditions:
            assert bill_coin_name is None, "Cannot create custom conditions and implement bill at the same time"
            print(f"Generating custom conditions for bill to disapprove announcer {coin_name}")
            response = await self.client.post(
                f"/announcers/{coin_name}/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "operation": "govern",
                    "args": {"toggle_activation": False},
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if response.is_error:
                print(response.content)
                response.raise_for_status()
            return response.json()
        else:
            assert bill_coin_name is not None, "Must specify bill to implement when not creating custom conditions"
            print(f"Implementing bill {bill_coin_name} to disapprove announcer {coin_name}")
            bill_response = await self.client.post(
                "/bills/implement",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "coin_name": bill_coin_name,
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if bill_response.is_error:
                print(bill_response.content)
                bill_response.raise_for_status()
            bill_response_dict = bill_response.json()
            implement_bundle = bill_response_dict["bundle"]
            statutes_mutation_spend = bill_response_dict["statutes_mutation_spend"]
            govern_response = await self.client.post(
                f"/announcers/{coin_name}/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "operation": "govern",
                    "args": {
                        "toggle_activation": False,
                        "implement_bundle": implement_bundle,
                        "statutes_mutation_spend": statutes_mutation_spend,
                    },
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if govern_response.is_error:
                print(govern_response.content)
                govern_response.raise_for_status()
            bundle = SpendBundle.from_json_dict(govern_response.json()["bundle"])
            sig_response = await self.sign_and_push(bundle)
            signed_bundle: SpendBundle = SpendBundle.from_json_dict(sig_response["bundle"])
            await self.wait_for_confirmation(signed_bundle)
            return sig_response

    async def upkeep_announcers_penalize(self, coin_name=None):
        if coin_name is None:
            response = await self.client.post(
                "/announcers",
                json={
                    "synthetic_pks": [],
                    "penalizable": True,
                },
            )
            data = response.json()
            if len(data) == 0:
                return {"status": "failed", "error": "no penalizable announcer found"}
            coin_name = data[0]["name"]
        else:
            coin_name = coin_name

        response = await self.client.post(
            f"/announcers/{coin_name}/",
            json={
                "synthetic_pks": [],
                "operation": "penalize",
                "args": {},
                "fee_per_cost": self.fee_per_cost,
            },
        )
        if response.is_error:
            print(response.content)
            response.raise_for_status()
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json())
        sig_response = await self.sign_and_push(bundle)
        signed_bundle: SpendBundle = SpendBundle.from_json_dict(sig_response["bundle"])
        await self.wait_for_confirmation(signed_bundle)
        return sig_response

    ## Bills ##
    async def upkeep_bills_list(
        self,
        exitable=None,
        empty=None,
        non_empty=None,
        vetoable=None,
        enacted=None,
        in_implementation_delay=None,
        implementable=None,
        lapsed=None,
        statute_index=None,
        bill=None,
        incl_spent=False,
    ):
        """List governance bills with optional state filters.

        The filters correspond to server-side bill state predicates.
        """
        payload = {
            "synthetic_pks": [],
            "exitable": exitable,
            "empty": empty,
            "non_empty": non_empty,
            "vetoable": vetoable,
            "enacted": enacted,
            "in_implementation_delay": in_implementation_delay,
            "implementable": implementable,
            "lapsed": lapsed,
            "statute_index": statute_index,
            "bill": bill,
            "include_spent_coins": incl_spent,
        }
        return await self._make_api_request("POST", "/bills", payload)

    ## Registry ##
    async def upkeep_registry_show(self):
        """Return the on-chain registry state."""
        return await self._make_api_request("POST", "/registry", {})

    async def upkeep_registry_reward(self, target_puzzle_hash=None, info=False):
        """Distribute rewards from the registry to a target puzzle hash.

        Args:
            target_puzzle_hash: Optional destination for rewards; if omitted the
                default server behavior is used.
            info: If True, request only informational output without submitting a tx.
        """
        payload = self._build_base_payload(target_puzzle_hash=target_puzzle_hash, info=info)
        if info:
            return await self._make_api_request("POST", "/registry/distribute_rewards", payload)
        return await self._process_transaction("/registry/distribute_rewards", payload)

    ## Recharge auction ##
    async def upkeep_recharge_list(self):
        """List all active recharge auctions."""
        return await self._make_api_request("POST", "/recharge_auctions", {})

    async def upkeep_recharge_launch(self, create_conditions=False, bill_coin_name=None):
        if create_conditions:
            assert bill_coin_name is None, "Cannot create custom conditions and implement bill at the same time"
            print("Generating custom conditions for bill to launch recharge auction coin")
            response = await self.client.post(
                "/recharge_auctions/launch",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if response.is_error:
                print(response.content)
                response.raise_for_status()
            return response.json()
        else:
            assert bill_coin_name is not None, "Must specify bill to implement when not creating custom conditions"
            print(f"Implementing bill {bill_coin_name} to launch recharge auction coin")
            bill_response = await self.client.post(
                "/bills/implement",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "coin_name": bill_coin_name,
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if bill_response.is_error:
                print(bill_response.content)
                bill_response.raise_for_status()
            print("Got bill", bill_response.content)
            print("Launching recharge coin")
            implement_bundle = SpendBundle.from_json_dict(bill_response.json())
            response = await self.client.post(
                "/recharge_auctions/launch",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "implement_bundle": implement_bundle.to_json_dict(),
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if response.is_error:
                print(response.content)
                response.raise_for_status()
            bundle: SpendBundle = SpendBundle.from_json_dict(response.json()["bundle"])
            sig_response = await self.sign_and_push(bundle)
            signed_bundle: SpendBundle = SpendBundle.from_json_dict(sig_response["bundle"])
            await self.wait_for_confirmation(signed_bundle)
            return sig_response

    async def upkeep_recharge_start(self, coin_name):
        """Start a recharge auction for the specified auction coin."""
        payload = self._build_transaction_payload({"operation": "start", "args": {}})
        return await self._process_transaction(f"/recharge_auctions/{coin_name}/", payload)

    async def upkeep_recharge_bid(
        self,
        coin_name,
        amount=None,
        crt=None,
        target_puzzle_hash=None,
        info=False,
    ):
        """Place a bid in a recharge auction.

        Args:
            coin_name: The recharge auction coin to bid on.
            amount: Amount of BYC to bid (float; converted to mCAT units).
            crt: Optional CRT amount to include (float; converted to mCAT units).
            target_puzzle_hash: Optional target puzzle hash for proceeds.
            info: If True, return only informational data without broadcasting.
        """
        args = {
            "byc_amount": self._convert_number(amount, "MCAT"),
            "crt_amount": self._convert_number(crt, "MCAT", ceil=True),
            "target_puzzle_hash": target_puzzle_hash,
            "info": info,
        }
        print(f"{args=}")
        payload = self._build_transaction_payload({"operation": "bid", "args": args})

        if info:
            # For info requests, just make the API call without processing transaction
            return await self._make_api_request("POST", f"/recharge_auctions/{coin_name}/", payload)

        return await self._process_transaction(f"/recharge_auctions/{coin_name}/", payload)

    async def upkeep_recharge_settle(self, coin_name):
        """Settle a completed recharge auction, finalizing outcomes."""
        payload = self._build_transaction_payload({"operation": "settle", "args": {}})
        return await self._process_transaction(f"/recharge_auctions/{coin_name}/", payload)

    ## Surplus auction ##
    async def upkeep_surplus_list(self):
        """List all active surplus auctions."""
        return await self._make_api_request("POST", "/surplus_auctions", {})

    async def upkeep_surplus_start(self):
        """Start a surplus auction cycle on the protocol."""
        payload = self._build_transaction_payload({})
        return await self._process_transaction("/surplus_auctions/start", payload)

    async def upkeep_surplus_bid(self, coin_name, amount=None, target_puzzle_hash=None, info=None):
        """Place a bid in a surplus auction.

        Args:
            coin_name: The surplus auction coin to bid on.
            amount: Amount of BYC to bid (float; converted to mCAT units).
            target_puzzle_hash: Optional target puzzle hash for proceeds.
            info: If True, return only informational data without broadcasting.
        """
        if not info:
            assert amount is not None

        args = {
            "amount": self._convert_number(amount, "MCAT"),
            "target_puzzle_hash": target_puzzle_hash,
            "info": info,
        }
        payload = self._build_transaction_payload({"operation": "bid", "args": args})

        if info:
            # For info requests, just make the API call without processing transaction
            return await self._make_api_request("POST", f"/surplus_auctions/{coin_name}/", payload)

        return await self._process_transaction(f"/surplus_auctions/{coin_name}/", payload)

    async def upkeep_surplus_settle(self, coin_name: str):
        """Settle a completed surplus auction, finalizing distribution."""
        payload = self._build_transaction_payload({"operation": "settle", "args": {}})
        return await self._process_transaction(f"/surplus_auctions/{coin_name}/", payload)

    ## Treasury ##
    async def upkeep_treasury_show(self):
        """Return current treasury state and balances."""
        return await self._make_api_request("POST", "/treasury", {})

    async def upkeep_treasury_rebalance(self, info=False):
        """Rebalance treasury assets; optionally return only info.

        Args:
            info: If True, return whether rebalance can execute (no tx).
        """
        if info:
            treasury_data = await self._make_api_request("POST", "/treasury", {})
            return {"action_executable": treasury_data["can_rebalance"]}

        log.info("Rebalancing treasury")
        payload = self._build_transaction_payload({})
        sig_response = await self._process_transaction("/treasury/rebalance", payload)
        log.info("Treasury rebalanced")
        return sig_response

    async def upkeep_treasury_launch(self, SUCCESSOR_LAUNCHER_ID=None, create_conditions=False, bill_coin_name=None):
        if not SUCCESSOR_LAUNCHER_ID:
            response = await self.client.post(
                "/treasury",
                json={},
            )
            data = response.json()
            successor_launcher_id = data["treasury_coins"][0]["launcher_id"]
        else:
            successor_launcher_id = SUCCESSOR_LAUNCHER_ID

        if create_conditions:
            assert bill_coin_name is None, "Cannot create custom conditions and implement bill at the same time"
            print(
                f"Generating custom conditions for bill to launch treasury coin with {successor_launcher_id} "
                f"as successor launcher ID"
            )
            response = await self.client.post(
                "/treasury/launch",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "successor_launcher_id": successor_launcher_id,
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if response.is_error:
                print(response.content)
                response.raise_for_status()
            return response.json()
        else:
            assert bill_coin_name is not None, "Must specify bill to implement when not creating custom conditions"
            print(
                f"Implementing bill {bill_coin_name} to launch treasury coin with {successor_launcher_id} as "
                f"successor launcher ID"
            )
            bill_response = await self.client.post(
                "/bills/implement",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "coin_name": bill_coin_name,
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            print("Launching treasury coin with successor launcher ID", successor_launcher_id)
            implement_bundle = SpendBundle.from_json_dict(bill_response.json())
            response = await self.client.post(
                "/treasury/launch",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "implement_bundle": implement_bundle.to_json_dict(),
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            if response.is_error:
                print(response.content)
                response.raise_for_status()
            bundle: SpendBundle = SpendBundle.from_json_dict(response.json()["bundle"])
            sig_response = await self.sign_and_push(bundle)
            signed_bundle: SpendBundle = SpendBundle.from_json_dict(sig_response["bundle"])
            await self.wait_for_confirmation(signed_bundle)
            return sig_response

    ## Vaults ##
    async def upkeep_vaults_list(self, coin_name=None, seized=None, not_seized=None):
        if seized and not_seized:
            return []

        if not_seized:
            seized = False
        elif seized is None:
            seized = None

        if coin_name:
            response = await self.client.post(
                f"/vaults/{coin_name}/",
                json={
                    "seized": seized,
                },
            )
            return response.json()

        response = await self.client.post(
            "/vaults",
            json={
                "seized": seized,
            },
        )
        return response.json()

    async def upkeep_vaults_transfer(self, coin_name=None):
        if not coin_name:
            response = await self.client.post(
                "/vaults",
                json={
                    "seized": False,
                },
            )
            vaults = response.json()
            coin_name = max(vaults, key=lambda x: x["fees_to_transfer"])["name"]

        log.info("Transferring Stability Fees from collateral vault %s", coin_name)

        response = await self.client.post(
            "/vaults/transfer_stability_fees",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "vault_name": coin_name,
                "fee_per_cost": self.fee_per_cost,
            },
            headers={"Content-Type": "application/json"},
        )
        if response.is_error:  # response.status_code != 200:
            try:
                log.warning(response.json().get("detail"))
            except Exception:
                log.error(response.content)
            response.raise_for_status()
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json()["bundle"])
        sig_response = await self.sign_and_push(bundle)
        signed_bundle: SpendBundle = SpendBundle.from_json_dict(sig_response["bundle"])
        await self.wait_for_confirmation(signed_bundle)
        log.info(
            f"Stability Fees transferred ({response.json()['sf_transferred'] / self.consts['MCAT']:,.3f} BYC) from "
            f"collateral vault {coin_name}"
        )
        return sig_response

    async def upkeep_vaults_liquidate(self, coin_name, target_puzzle_hash=None):

        if not target_puzzle_hash:
            target_puzzle_hash = puzzle_hash_for_synthetic_public_key(self.synthetic_public_keys[0]).hex()

        response = await self.client.post(
            "/vaults/start_auction",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "vault_name": coin_name,
                "initiator_puzzle_hash": target_puzzle_hash,
                "fee_per_cost": self.fee_per_cost,
            },
            headers={"Content-Type": "application/json"},
        )
        if response.is_error:
            print(response.content)
            response.raise_for_status()
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json())
        sig_response = await self.sign_and_push(bundle)
        signed_bundle: SpendBundle = SpendBundle.from_json_dict(sig_response["bundle"])
        await self.wait_for_confirmation(signed_bundle)
        return sig_response

    async def upkeep_vaults_bid(self, coin_name, amount, max_bid_price=None, info=False):
        """
        Args:
            info: If True, return info about the prospective toggle without sending a tx.
        """
        if info:
            # For info requests, just make the API call without processing transaction
            if amount is None:
                response = await self.client.post(
                    f"/vaults/{coin_name}/",
                    json={},
                )
                data = response.json()
                owed_to_initiator = data.get("initiator_incentive_balance") or 0
                amount = data["debt_owed_to_vault"] + owed_to_initiator

            payload = self._build_base_payload(
                vault_name=coin_name,
                amount=self._convert_number(amount, "MCAT"),
                info=info,
            )
            return await self._make_api_request("POST", "/vaults/bid_auction", payload)

        assert amount is not None, "Must specify amount to bid"
        keeper_puzzle_hash = puzzle_hash_for_synthetic_public_key(self.synthetic_public_keys[0]).hex()

        response = await self.client.post(
            "/vaults/bid_auction",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "vault_name": coin_name,
                "amount": self._convert_number(amount, "MCAT"),
                "max_bid_price": self._convert_number(max_bid_price, "PRICE"),
                "target_puzzle_hash": keeper_puzzle_hash,
                "info": info,
                "fee_per_cost": self.fee_per_cost,
            },
            headers={"Content-Type": "application/json"},
        )
        if response.is_error:
            print(response.content)
            response.raise_for_status()
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json())
        sig_response = await self.sign_and_push(bundle)
        signed_bundle: SpendBundle = SpendBundle.from_json_dict(sig_response["bundle"])
        await self.wait_for_confirmation(signed_bundle)
        return sig_response

    async def upkeep_vaults_recover(self, coin_name):
        response = await self.client.post(
            "/vaults/recover_bad_debt",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "vault_name": coin_name,
                "fee_per_cost": self.fee_per_cost,
            },
            headers={"Content-Type": "application/json"},
        )
        if response.is_error:
            print(response.content)
            response.raise_for_status()
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json())
        sig_response = await self.sign_and_push(bundle)
        signed_bundle: SpendBundle = SpendBundle.from_json_dict(sig_response["bundle"])
        await self.wait_for_confirmation(signed_bundle)
        return sig_response

    ### BILLS ###
    async def bills_list(
        self,
        exitable=None,
        empty=None,
        non_empty=None,
        vetoable=None,
        enacted=None,
        in_implementation_delay=None,
        implementable=None,
        lapsed=None,
        statute_index=None,
        bill=None,
        incl_spent=False,
    ):
        """List governance bills with optional state filters.

        Mirrors upkeep_bills_list but uses base payload helper.
        """
        payload = self._build_base_payload(
            exitable=exitable,
            empty=empty,
            non_empty=non_empty,
            vetoable=vetoable,
            enacted=enacted,
            in_implementation_delay=in_implementation_delay,
            implementable=implementable,
            lapsed=lapsed,
            statute_index=statute_index,
            bill=bill,
            include_spent_coins=incl_spent,
        )
        return await self._make_api_request("POST", "/bills", payload)

    async def bills_toggle(self, coin_name, info=False):
        """Toggle a CRT coin between plain and governance modes.

        Args:
            coin_name: The CRT coin to toggle.
            info: If True, return info about the prospective toggle without sending a tx.
        """
        if info:
            # For info requests, just make the API call without processing transaction
            payload = self._build_base_payload(coin_name=coin_name, info=info)
            return await self._make_api_request("POST", "/coins/toggle_governance", payload)

        # For actual toggle operations, process as transaction
        payload = self._build_transaction_payload({"coin_name": coin_name, "info": info})
        response_data = await self._make_api_request("POST", "/coins/toggle_governance", payload)

        # Extract bundle and error handling info
        bundle = SpendBundle.from_json_dict(response_data["bundle"])
        error_handling_info = response_data.get("error_handling_info")

        # Sign and push transaction
        sig_response = await self.sign_and_push(bundle, error_handling_info)

        # Wait for confirmation
        signed_bundle = SpendBundle.from_json_dict(sig_response["bundle"])
        await self.wait_for_confirmation(signed_bundle)

        return sig_response

    async def bills_reset(self, coin_name):
        """Reset a bill on a governance coin back to empty state."""
        # Get coin name if not provided, using custom logic for empty bills
        if not coin_name:
            payload = self._build_base_payload(include_spent_coins=False, empty_only=True)
            data = await self._make_api_request("POST", "/bills", payload)
            assert len(data) > 0, "No bills found"
            assert len(data) == 1, "More than one bill found. Must specify governance coin name"
            coin_name = data["name"]

        # Process as transaction
        payload = self._build_transaction_payload({"coin_name": coin_name})
        return await self._process_transaction("/bills/reset", payload)

    async def bills_propose(
        self,
        index: int = None,
        value: str = None,
        coin_name: str = None,
        force: bool = False,
        proposal_threshold=None,
        veto_interval=None,
        implementation_delay=None,
        max_delta=None,
        skip_verify=False,
        label=None,
    ):
        assert index is not None, "Must specify Statute index (between -1 and 42 included)"

        # Get coin name if not provided, using custom logic for empty bills
        if coin_name is None:
            payload = self._build_base_payload(include_spent_coins=False, empty_only=True)
            data = await self._make_api_request("POST", "/bills", payload)
            assert len(data) > 0, "No governance coin with empty bill found"
            coin_name = data[0]["name"]

        if value is not None:
            if value.startswith("<") and value.endswith(">"):
                # this is a labelled value
                value_label = value[1:-1]
                value = self.store.get(f"proposals.values.{value_label}")
            if value.lower().startswith("0x"):
                value = value[2:]
        # Process as transaction
        payload = self._build_transaction_payload(
            {
                "coin_name": coin_name,
                "statute_index": index,
                "value": value,
                "value_is_program": False,
                "threshold_amount_to_propose": self._convert_number(proposal_threshold, "MCAT"),
                "veto_seconds": self._convert_number(veto_interval),
                "delay_seconds": self._convert_number(implementation_delay),
                "max_delta": self._convert_number(max_delta),
                "force": force,
                "verify": not skip_verify,
            }
        )
        tx_result = await self._process_transaction("/bills/propose", payload)
        bundle = SpendBundle.from_json_dict(tx_result["bundle"])
        new_proposal_coin: Coin
        for coin in bundle.additions():
            if coin.parent_coin_info == bytes.fromhex(coin_name):
                new_proposal_coin = coin
                break
        else:
            raise Exception("New proposal coin not found in returned bundle")
        if label:
            log.debug("Storing proposal coin %s with label %s", new_proposal_coin.name(), label)
            with self.store.transaction() as data:
                data[f"proposals.propose.coins.{label}"] = new_proposal_coin.name().hex()
        return tx_result

    async def bills_implement(self, coin_name=None): #, info=False):
        if coin_name is None: # or info:
            payload = self._build_base_payload(include_spent_coins=False, empty_only=False, non_empty_only=True)
            data: list = await self._make_api_request("POST", "/bills", payload)

            if coin_name:
                coins = [coin for coin in data if coin["name"] == coin_name]
            else:
                # LATER: verify that sorting works as intended when coins are human readable
                coins = sorted(data, key=lambda x: x["status"]["implementable_in"])
                assert len(coins) > 0, "There are no proposed bills"
                assert coins[0]["status"]["implementable_in"] <= 0, "No implementable bill found"
                coin_name = coins[0]["name"]
            #if info:
            #    return coins
        else:
            if coin_name.startswith("<") and coin_name.endswith(">"):
                label = coin_name[1:-1]
                coin_name = self.store.get(f"proposals.propose.coins.{label}")

        # Process as transaction
        payload = self._build_transaction_payload({"coin_name": coin_name})
        return await self._process_transaction("/bills/implement", payload)

    ### ORACLE ###
    async def oracle_show(self):
        """Return current oracle state and parameters from the RPC server."""
        return await self._make_api_request("POST", "/oracle", {})

    async def oracle_update(self, info=False):
        """Update on-chain oracle data.

        If info is True, returns informational output from the server about a
        potential update without broadcasting a transaction. Otherwise, builds,
        signs, submits, and waits for confirmation of the update transaction.
        """
        if info:
            # For info requests, just make the API call without processing transaction
            payload = self._build_base_payload(info=info)
            return await self._make_api_request("POST", "/oracle/update", payload)

        # For actual updates, process as transaction
        payload = self._build_transaction_payload({"info": info})
        return await self._process_transaction("/oracle/update", payload)

    ### STATUTES ###
    async def statutes_list(self, full=False):
        """List protocol statutes.

        Args:
            full: If True, return the full set of statute definitions and metadata.
        """
        payload = self._build_base_payload(full=full)
        return await self._make_api_request("POST", "/statutes", payload)

    async def statutes_update(self, info=False):
        """Update protocol statutes on-chain.

        If info is True, fetch informational data about a potential update
        (no transaction). Otherwise, create, sign, and submit the update
        transaction and wait for confirmation.
        """
        if info:
            # For info requests, just make the API call without processing transaction
            return await self._make_api_request("POST", "/statutes/info", {})

        # For actual updates, process as transaction
        payload = self._build_transaction_payload({})
        return await self._process_transaction("/statutes/update", payload)

    async def statutes_announce(self, *args):
        """Publish a statutes announcement transaction.

        Creates, signs, and submits the announce transaction which may be
        required after a statutes update, then waits for confirmation.
        """
        payload = self._build_transaction_payload({})
        return await self._process_transaction("/statutes/announce", payload)

    async def close(self):
        """Close the HTTP client with proper logging."""
        if hasattr(self, "client"):
            await self.client.aclose()
            log.info("HTTP client closed")
