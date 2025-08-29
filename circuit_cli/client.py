import asyncio
import httpx
import logging
import logging.config
import sys
from math import ceil, floor
from typing import Optional, Dict, Any

from chia.util.bech32m import encode_puzzle_hash
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    puzzle_hash_for_synthetic_public_key,
)
from chia_rs import PrivateKey, SpendBundle
from httpx import AsyncClient

from circuit_cli.utils import generate_ssks, sign_spends

log = logging.getLogger(__name__)


def setup_console_logging():
    """Setup console-friendly logging for CLI usage."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("circuit_cli").setLevel(logging.INFO)


class APIError(Exception):
    """Custom exception for API-related errors."""

    def __init__(self, message: str, response: Optional[httpx.Response] = None):
        super().__init__(message)
        self.response = response


class CircuitRPCClient:
    # TODO: add support for estimated fees for tx to be included in next block
    def __init__(
        self,
        base_url: str,
        private_key: str,
        add_sig_data: str = None,
        fee_per_cost: int = 0,
        client: Optional[AsyncClient] = None,
        key_count: int = 500,
        no_wait_for_tx: bool = False,
    ):
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
        if private_key:
            log.info("Wallet first 5 addresses:")
            log.info(
                [encode_puzzle_hash(puzzle_hash_for_synthetic_public_key(x), "txch") for x in synthetic_public_keys[:5]]
            )
        self.consts = {
            "PRICE_PRECISION": 1000000,  # Default 6 decimals
            "MOJOS": 1000000000000,  # Default XCH to mojo conversion
            "MCAT": 1000,  # Default CAT decimals
        }
        self.base_url = base_url
        # Use injected client if provided, otherwise create httpx.Client
        self.client = client if client is not None else httpx.AsyncClient(base_url=base_url, timeout=120)
        log.info(f"Using add_sig_data={add_sig_data}")
        self.add_sig_data = add_sig_data
        self.fee_per_cost = fee_per_cost
        self.no_wait_for_confirmation = no_wait_for_tx

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
    ) -> Dict[str, Any]:
        """
        Make a standardized API request with error handling.

        Args:
            method: HTTP method (GET, POST)
            endpoint: API endpoint
            json_data: JSON payload for POST requests
            params: Query parameters for GET requests

        Returns:
            Response JSON data

        Raises:
            APIError: If the request fails
        """
        try:
            log.info(f"Making request to {method} {endpoint} with params {params} and json_data {json_data}")
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
        """Build payload for transaction endpoints."""
        base_payload = {
            "synthetic_pks": self.synthetic_pks_hex,
            "fee_per_cost": self.fee_per_cost,
        }
        base_payload.update(endpoint_specific_data)
        return base_payload

    def _convert_amount(self, amount: float, currency_type: str = "MOJOS") -> int:
        """Convert amount to appropriate units."""
        if currency_type == "MOJOS":
            return floor(amount * self.consts["MOJOS"])
        elif currency_type == "MCAT":
            return floor(amount * self.consts["MCAT"])
        elif currency_type == "PRICE":
            return int(amount * self.consts["PRICE_PRECISION"])
        else:
            raise ValueError(f"Unknown currency type: {currency_type}")

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
            bundle = SpendBundle.from_json_dict(bundle_data["bundle"])
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

    async def _get_coin_name_if_needed(self, coin_name: Optional[str], endpoint: str, error_message: str = None) -> str:
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
        data = await self._make_api_request("POST", endpoint, payload)

        base_error = error_message or "No coin found"
        assert len(data) > 0, base_error
        assert len(data) == 1, "More than one coin found. Must provide coin_name"
        return data["name"]

    async def wait_for_confirmation(self, bundle: SpendBundle = None, blocks=None):
        if self.no_wait_for_confirmation:
            return True
        if bundle is not None and isinstance(bundle, SpendBundle):
            while True:
                response = await self.client.post("/transactions/status", json={"bundle": bundle.to_json_dict()})
                if response.status_code != 200:
                    print(response.content)
                    response.raise_for_status()
                data = response.json()
                if data["status"] == "confirmed":
                    log.info("Transaction confirmed. ID %s", bundle.name().hex())
                    return True
                elif data["status"] == "failed":
                    raise ValueError(f"Transaction failed. ID {bundle.name().hex()}")
                log.info(f"Still waiting for confirmation of transaction ID {bundle.name().hex()}")
                await asyncio.sleep(5)
        elif blocks is not None:
            await asyncio.sleep(blocks * 55)
            return True
        else:
            raise ValueError("Either bundle or number of blocks must be provided")

    async def sign_and_push(self, bundle: SpendBundle, error_handling_info: dict = None):
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
        response.raise_for_status()
        log.info("Transaction signed and broadcast. ID %s", signed_bundle.name().hex())
        return response.json()

    ### WALLET ###
    async def wallet_balances(self, human_readable=False):
        """
        Get wallet balances for XCH, BYC, and CRT coins.

        Retrieves the current balance information for all supported coin types
        in the user's wallet. This includes XCH (Chia), BYC (stablecoin), and
        CRT (governance tokens) that are not currently in governance mode.

        Args:
            human_readable (bool): If True, format numbers in human-readable format.
                                 Defaults to False.

        Returns:
            dict: A dictionary containing balance information with keys:
                - xch_balance: XCH balance in mojos
                - byc_balance: BYC balance in mCAT units
                - crt_balance: CRT balance
                - total_coins: Total number of coins
                - pending_balance: Pending transactions balance

        Example:
            balances = client.wallet_balances()
            # Returns: {"xch_balance": 5000000000000, "byc_balance": 1500000, ...}
        """
        payload = self._build_base_payload(human_readable=human_readable)
        return await self._make_api_request("POST", "/balances", payload)

    async def wallet_coins(self, type=None, human_readable=False):
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
            human_readable (bool): Format numbers in human-readable format.

        Returns:
            dict: Contains coin information with keys:
                - coins: List of coin objects with details like coin_name, amount, etc.
                - total_count: Total number of coins
                - confirmed_count: Number of confirmed coins

        Example:
            coins_info = client.wallet_coins(type="xch")
            # Returns: {"coins": [...], "total_count": 5, "confirmed_count": 4}
        """
        payload = self._build_base_payload(coin_type=type, human_readable=human_readable)
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
    async def vault_show(self, human_readable=False):
        """
        Show information about the user's collateral vault.

        Displays comprehensive information about the user's vault including
        collateral amount, borrowed amount, health ratio, liquidation status,
        and other vault parameters.

        Args:
            human_readable (bool): Format numbers in human-readable format.
                                 Defaults to False.

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
        payload = self._build_base_payload(human_readable=human_readable)
        return await self._make_api_request("POST", "/vault", payload)

    async def vault_deposit(self, amount: float):
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
        payload = self._build_transaction_payload({"amount": self._convert_amount(amount, "MOJOS")})
        return await self._process_transaction("/vault/deposit", payload)

    async def vault_withdraw(self, amount: float):
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
        payload = self._build_transaction_payload({"amount": self._convert_amount(amount, "MOJOS")})
        return await self._process_transaction("/vault/withdraw", payload)

    async def vault_borrow(self, amount: float):
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
        payload = self._build_transaction_payload({"amount": self._convert_amount(amount, "MCAT")})
        return await self._process_transaction("/vault/borrow", payload)

    async def vault_repay(self, amount: float):
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
        payload = self._build_transaction_payload({"amount": self._convert_amount(amount, "MCAT")})
        return await self._process_transaction("/vault/repay", payload)

    ### SAVINGS VAULT ###
    async def savings_show(self, human_readable=False):
        """
        Show information about the user's savings vault.

        Displays information about BYC savings including total deposited amount,
        interest earned, and available withdrawal balance.

        Args:
            human_readable (bool): Format numbers in human-readable format.
                                 Defaults to False.

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
        payload = self._build_base_payload(human_readable=human_readable)
        return await self._make_api_request("POST", "/savings", payload)

    async def savings_deposit(self, amount: float):
        """
        Deposit BYC into the savings vault to earn interest.

        Deposits BYC stablecoin into the savings vault where it will
        earn interest over time based on the current savings rate.

        Args:
            amount (float): Amount of BYC to deposit into savings

        Returns:
            dict: Transaction result with bundle and status information

        Example:
            result = client.savings_deposit(1000.0)
            # Deposits 1000 BYC into savings to earn interest
        """
        payload = self._build_transaction_payload({"amount": self._convert_amount(amount, "MCAT")})
        return await self._process_transaction("/savings/deposit", payload)

    async def savings_withdraw(self, amount: float):
        """
        Withdraw BYC from the savings vault.

        Withdraws BYC from the savings vault, including both principal
        and any accrued interest up to the available balance.

        Args:
            amount (float): Amount of BYC to withdraw from savings

        Returns:
            dict: Transaction result with bundle and status information

        Example:
            result = client.savings_withdraw(500.0)
            # Withdraws 500 BYC from savings including interest
        """
        payload = self._build_transaction_payload({"amount": self._convert_amount(amount, "MCAT")})
        return await self._process_transaction("/savings/withdraw", payload)

    ### ANNOUNCERS ###
    async def announcer_show(self, approved=False, valid=False, penalizable=False, incl_spent=False):
        """Show announcer information using DRY helper methods."""
        payload = self._build_base_payload(
            approved=approved, valid=valid, penalizable=penalizable, include_spent_coins=incl_spent
        )
        data = await self._make_api_request("POST", "/announcer", payload)
        assert isinstance(data, list)
        return data

    async def announcer_launch(self, price, units=False):
        """Launch announcer using DRY transaction processing."""
        log.info("Launching announcer...")
        price = price if units else self._convert_amount(price, "PRICE")
        payload = self._build_transaction_payload({"operation": "launch", "args": {"price": price}})
        log.info(f"Launching announcer with price: {price}")
        try:
            return await self._process_transaction("/announcers/launch/", payload)
        finally:
            log.info("Launching announcer successful.")

    async def announcer_configure(
        self,
        coin_name,
        make_approvable=False,
        deactivate=False,
        deposit=None,
        min_deposit=None,
        inner_puzzle_hash=None,
        price=None,
        ttl=None,
        units=False,
    ):
        """Configure announcer using DRY helper methods."""
        coin_name = await self._get_coin_name_if_needed(coin_name, "/announcer", "No announcer found")

        # Build args using helper methods for amount conversions
        args = {
            "deactivate": deactivate,
            "make_approvable": make_approvable,
            "new_deposit": (deposit if units else ceil(deposit * self.consts["MOJOS"]))
            if deposit is not None
            else deposit,
            "new_min_deposit": (min_deposit if units else ceil(min_deposit * self.consts["MOJOS"]))
            if min_deposit is not None
            else min_deposit,
            "new_inner_puzzle_hash": inner_puzzle_hash,
            "new_price": (price if units else self._convert_amount(price, "PRICE")) if price is not None else price,
            "new_price_ttl": ttl if ttl is not None else ttl,
        }

        payload = self._build_transaction_payload({"operation": "configure", "args": args})
        return await self._process_transaction(f"/announcers/{coin_name}/", payload)

    async def announcer_register(self, coin_name: str = None):
        """Register announcer using DRY helper methods."""
        coin_name = await self._get_coin_name_if_needed(coin_name, "/announcers", "No announcer found")
        payload = self._build_transaction_payload({"operation": "register", "args": {}})
        return await self._process_transaction(f"/announcers/{coin_name}/", payload)

    async def announcer_reward(self, coin_name: str = None):
        """Claim announcer reward using DRY helper methods."""
        coin_name = await self._get_coin_name_if_needed(coin_name, "/announcers", "No announcer found")
        payload = self._build_transaction_payload({"operation": "register", "args": {}})
        return await self._process_transaction(f"/announcers/{coin_name}/", payload)

    async def announcer_exit(self, coin_name):
        """Exit announcer using DRY helper methods."""
        coin_name = await self._get_coin_name_if_needed(coin_name, "/announcers", "No announcer found")
        payload = self._build_transaction_payload({"operation": "exit", "args": {}})
        return await self._process_transaction(f"/announcers/{coin_name}/", payload)

    async def announcer_update(self, PRICE, coin_name: str = None, fee_coin=False, units=False):
        """Update announcer price using DRY helper methods."""
        coin_name = await self._get_coin_name_if_needed(coin_name, "/announcer", "No announcer found")

        args = {
            "new_price": PRICE if units else self._convert_amount(PRICE, "PRICE"),
            "attach_fee_coin": fee_coin,
        }
        payload = self._build_transaction_payload({"operation": "mutate", "args": args})
        return await self._process_transaction(f"/announcers/{coin_name}/", payload)

    ### UPKEEP ###
    async def upkeep_invariants(self):
        """Show protocol invariants using DRY helper methods."""
        return await self._make_api_request("GET", "/protocol/invariants")

    async def upkeep_state(
        self, vaults=False, surplus_auctions=False, recharge_auctions=False, treasury=False, bills=False
    ):
        """Show protocol state using DRY helper methods."""
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
        """Sync RPC server using DRY helper methods."""
        return await self._make_api_request("POST", "/sync_chain_data")

    async def upkeep_rpc_status(self):
        """Get RPC server status using DRY helper methods."""
        return await self._make_api_request("GET", "/health")

    async def upkeep_rpc_version(self):
        """Get RPC server version using DRY helper methods."""
        return await self._make_api_request("GET", "/rpc/version")

    ## Announcer ##
    async def upkeep_announcers_list(
        self, coin_name=None, approved=False, valid=False, penalizable=False, incl_spent=False
    ):
        """List announcers using DRY helper methods."""
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

    async def upkeep_announcers_approve(
        self, coin_name, create_conditions=False, bill_coin_name=None, govern_bundle=None
    ):
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
            return response.json()
        else:
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
        """List bills using DRY helper methods."""
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
        """Show registry using DRY helper methods."""
        return await self._make_api_request("POST", "/registry", {})

    async def upkeep_registry_reward(self, target_puzzle_hash=None, info=False):
        """Distribute registry rewards using DRY helper methods."""
        payload = self._build_base_payload(target_puzzle_hash=target_puzzle_hash, info=info)
        # TODO: don't we need to sign and broadcast this transaction?!
        return await self._make_api_request("POST", "/registry/distribute_rewards", payload)

    ## Recharge auction ##
    async def upkeep_recharge_list(self):
        """List recharge auctions using DRY helper methods."""
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

    async def upkeep_recharge_start(self, coin_name: str):
        """Start recharge auction using DRY helper methods."""
        payload = self._build_transaction_payload({"operation": "start", "args": {}})
        return await self._process_transaction(f"/recharge_auctions/{coin_name}/", payload)

    async def upkeep_recharge_bid(
        self,
        coin_name: str,
        BYC_amount: float | None = None,
        crt: float | None = None,
        target_puzzle_hash: str | None = None,
        info=False,
    ):
        """Bid in recharge auction using DRY helper methods."""
        args = {
            "byc_amount": self._convert_amount(BYC_amount, "MCAT") if BYC_amount else None,
            "crt_amount": ceil(crt * self.consts["MCAT"]) if crt else None,
            "target_puzzle_hash": target_puzzle_hash,
            "info": info,
        }
        payload = self._build_transaction_payload({"operation": "bid", "args": args})

        if info:
            # For info requests, just make the API call without processing transaction
            return await self._make_api_request("POST", f"/recharge_auctions/{coin_name}/", payload)

        return await self._process_transaction(f"/recharge_auctions/{coin_name}/", payload)

    async def upkeep_recharge_settle(self, coin_name: str):
        """Settle recharge auction using DRY helper methods."""
        payload = self._build_transaction_payload({"operation": "settle", "args": {}})
        return await self._process_transaction(f"/recharge_auctions/{coin_name}/", payload)

    ## Surplus auction ##
    async def upkeep_surplus_list(self):
        """List surplus auctions using DRY helper methods."""
        return await self._make_api_request("POST", "/surplus_auctions", {})

    async def upkeep_surplus_start(self):
        """Start surplus auction using DRY helper methods."""
        payload = self._build_transaction_payload({})
        return await self._process_transaction("/surplus_auctions/start", payload)

    async def upkeep_surplus_bid(self, coin_name: str, amount: float = None, target_puzzle_hash=None, info=None):
        """Bid in surplus auction using DRY helper methods."""
        if not info:
            assert amount is not None

        args = {
            "amount": self._convert_amount(amount, "MCAT") if amount else None,
            "target_puzzle_hash": target_puzzle_hash,
            "info": info,
        }
        payload = self._build_transaction_payload({"operation": "bid", "args": args})

        if info:
            # For info requests, just make the API call without processing transaction
            return await self._make_api_request("POST", f"/surplus_auctions/{coin_name}/", payload)

        return await self._process_transaction(f"/surplus_auctions/{coin_name}/", payload)

    async def upkeep_surplus_settle(self, coin_name: str):
        """Settle surplus auction using DRY helper methods."""
        payload = self._build_transaction_payload({"operation": "settle", "args": {}})
        return await self._process_transaction(f"/surplus_auctions/{coin_name}/", payload)

    ## Treasury ##
    async def upkeep_treasury_show(self):
        """Show treasury using DRY helper methods."""
        return await self._make_api_request("POST", "/treasury", {})

    async def upkeep_treasury_rebalance(self, info=False):
        """Rebalance treasury using DRY helper methods."""
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

    async def upkeep_vaults_liquidate(self, coin_name: str):
        keeper_puzzle_hash = puzzle_hash_for_synthetic_public_key(self.synthetic_public_keys[0]).hex()

        response = await self.client.post(
            "/vaults/start_auction",
            json={
                "synthetic_pks": [],
                "vault_name": coin_name,
                "initiator_puzzle_hash": keeper_puzzle_hash,
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

    async def upkeep_vaults_bid(self, coin_name: str, amount: float):
        keeper_puzzle_hash = puzzle_hash_for_synthetic_public_key(self.synthetic_public_keys[0]).hex()

        response = await self.client.post(
            "/vaults/bid_auction",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "vault_name": coin_name,
                "amount": floor(amount * self.consts["MCAT"]),
                "max_bid_price": None,  # TODO: get from command line
                "target_puzzle_hash": keeper_puzzle_hash,
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

    async def upkeep_vaults_recover(self, coin_name: str):
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
        """List bills using DRY helper methods."""
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

    async def bills_toggle(self, coin_name: str, info=False):
        """Toggle governance mode of a coin using DRY helper methods."""
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

    async def bills_reset(self, coin_name: str):
        """Reset a bill using DRY helper methods."""
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
        INDEX: int = None,
        VALUE: str = None,
        coin_name: str = None,
        force: bool = False,
        proposal_threshold: int = None,
        veto_interval: int = None,
        implementation_delay: int = None,
        max_delta: int = None,
    ):
        """Propose a bill using DRY helper methods."""
        assert INDEX is not None, "Must specify Statute index (between -1 and 42 included)"

        # Get coin name if not provided, using custom logic for empty bills
        if coin_name is None:
            payload = self._build_base_payload(include_spent_coins=False, empty_only=True)
            data = await self._make_api_request("POST", "/bills", payload)
            assert len(data) > 0, "No governance coin with empty bill found"
            coin_name = data["name"]

        # Process as transaction
        payload = self._build_transaction_payload(
            {
                "coin_name": coin_name,
                "statute_index": INDEX,
                "value": VALUE,
                "value_is_program": False,
                "threshold_amount_to_propose": proposal_threshold,
                "veto_seconds": veto_interval,
                "delay_seconds": implementation_delay,
                "max_delta": max_delta,
                "force": force,
            }
        )
        return await self._process_transaction("/bills/propose", payload)

    async def bills_implement(self, coin_name: str = None, info: bool = False):
        """Implement a bill using DRY helper methods."""
        coins = []
        if coin_name is None or info:
            payload = self._build_base_payload(include_spent_coins=False, empty_only=False, non_empty_only=True)
            data = await self._make_api_request("POST", "/bills", payload)

            if coin_name:
                coins = [coin for coin in data if coin["name"] == coin_name]
            else:
                # LATER: verify that sorting works as intended when coins are human readable
                coins = sorted(data, key=lambda x: x["status"]["implementable_in"])
                assert len(coins) > 0, "There are no proposed bills"
                assert coins[0]["status"]["implementable_in"] <= 0, "No implementable bill found"
                coin_name = coins[0]["name"]

        if info:
            return coins

        # Process as transaction
        payload = self._build_transaction_payload({"coin_name": coin_name})
        return await self._process_transaction("/bills/implement", payload)

    ### ORACLE ###
    async def oracle_show(self):
        """Show oracle information using DRY helper methods."""
        return await self._make_api_request("POST", "/oracle", {})

    async def oracle_update(self, info=False):
        """Update oracle using DRY helper methods."""
        if info:
            # For info requests, just make the API call without processing transaction
            payload = self._build_base_payload(info=info)
            return await self._make_api_request("POST", "/oracle/update", payload)

        # For actual updates, process as transaction
        payload = self._build_transaction_payload({"info": info})
        return await self._process_transaction("/oracle/update", payload)

    ### STATUTES ###
    async def statutes_list(self, full=False):
        """List statutes using DRY helper methods."""
        payload = self._build_base_payload(full=full)
        return await self._make_api_request("POST", "/statutes", payload)

    async def statutes_update(self, info=False):
        """Update statutes using DRY helper methods."""
        if info:
            # For info requests, just make the API call without processing transaction
            return await self._make_api_request("POST", "/statutes/info", {})

        # For actual updates, process as transaction
        payload = self._build_transaction_payload({})
        return await self._process_transaction("/statutes/update", payload)

    async def statutes_announce(self, *args):
        """Announce statutes using DRY helper methods."""
        payload = self._build_transaction_payload({})
        return await self._process_transaction("/statutes/announce", payload)

    async def close(self):
        """Close the HTTP client with proper logging."""
        if hasattr(self, "client"):
            await self.client.aclose()
            log.info("HTTP client closed")
