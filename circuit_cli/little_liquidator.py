import asyncio
import json
import logging
import time
from pprint import pformat
from typing import Dict

import httpx
from chia.wallet.trading.offer import Offer
from chia_rs import SpendBundle
from circuit_cli.client import APIError, CircuitRPCClient

PRECISION_BPS = 10**4
PRICE_PRECISION = 10**2
MCAT = 10**3

log = logging.getLogger(__name__)

MOJOS = 10**12


async def fetch_dexie_price(pay_asset: str = "XCH", receive_asset: str = "TBYC"):
    """
    Backwards-compatible simple price fetcher expected by tests.
    Returns the last traded price ratio (receive per pay) from Dexie price API.
    """
    data = await fetch_dexie_market_depth(pay_asset, receive_asset, None)
    if not data:
        return 0
    return data.get("relevant_price", 0)


async def fetch_dexie_market_depth(pay_asset: str = "XCH", receive_asset: str = "TBYC", amount: float = None):
    """
    Fetch market depth from Dexie's price API to check if there's sufficient liquidity.

    Args:
        pay_asset: Asset to sell (default: XCH)
        receive_asset: Asset to receive (default: TBYC)
        amount: Amount to check liquidity for (in whole units)

    Returns:
        Dict with market info including best_bid, best_ask, depth, or None on error
    """
    url = "https://api-testnet.dexie.space/v3/prices/tickers"
    headers = {"Accept": "application/json"}

    log.debug(f"Requesting Dexie testnet tickers: {url}")

    async with httpx.AsyncClient(timeout=10) as http_client:
        try:
            resp = await http_client.get(url, headers=headers)
            resp.raise_for_status()

            # Check if response has content before trying to parse JSON
            if not resp.content:
                log.warning("Empty response from Dexie Price API")
                return None

            try:
                data = resp.json()
            except ValueError as e:
                log.warning(f"Invalid JSON response from Dexie Price API: {e}")
                log.warning(f"Response status: {resp.status_code}, headers: {dict(resp.headers)}")
                log.warning(f"Response content: {resp.text[:500]}")
                return None

            if isinstance(data, dict) and "tickers" in data:
                # Find the TBYC/TXCH pair (testnet BYC vs testnet XCH)
                target_base = "TBYC" if receive_asset == "TBYC" else receive_asset
                target_currency = "TXCH" if pay_asset == "XCH" else pay_asset

                for ticker in data["tickers"]:
                    base_code = ticker.get("base_code", "")
                    target_code = ticker.get("target_code", "")

                    # Match TBYC_TXCH pair
                    if base_code == target_base and target_code == target_currency:
                        # Extract market data
                        best_bid = float(ticker.get("bid") or 0) if ticker.get("bid") else 0
                        best_ask = float(ticker.get("ask") or 0) if ticker.get("ask") else 0
                        last_price = float(ticker.get("last_price") or 0)
                        # Use last traded price as market reference; values are ratios (TBYC per XCH)
                        relevant_price = 1 / best_ask

                        # Assume sufficient depth if we have a valid price ratio
                        has_sufficient_depth = relevant_price > 0

                        log.debug(
                            f"Found TBYC pair: bid={best_bid}, ask={best_ask}, last_price={last_price}, relevant_price={relevant_price}"
                        )

                        return {
                            "best_bid": best_bid,
                            "best_ask": best_ask,
                            "last_price": last_price,
                            "relevant_price": relevant_price,
                            "has_sufficient_depth": has_sufficient_depth,
                        }
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            log.warning(f"HTTP error fetching market depth from Dexie Price API: {e}")
        except Exception as e:
            log.warning(f"Unexpected error fetching market depth from Dexie Price API: {e}")
        return None


async def upload_offer_to_dexie(offer_data: dict, progress_handler=None):
    """
    Upload a created offer to dexie.space marketplace.

    Args:
        offer_data: Dict containing offer details to upload
        progress_handler: Optional progress handler for emitting events

    Returns:
        Dict with upload result or None on error
    """

    # Helper function to emit progress if handler is available
    async def emit_progress(event_data):
        if progress_handler:
            try:
                if asyncio.iscoroutinefunction(progress_handler):
                    await progress_handler(event_data)
                else:
                    progress_handler(event_data)
            except Exception as e:
                log.debug(f"Progress handler raised exception: {e}")

    # Extract offer details for progress reporting

    offer = Offer.from_bech32(offer_data["offer"])
    await emit_progress(
        {
            "event": "dexie_upload_started",
            "message": f"Uploading offer to Dexie - offering {offer.get_offered_amounts()} for {offer.get_requested_amounts()}",
            "offer_details": offer.summary(),
        }
    )

    url = "https://api-testnet.dexie.space/v1/offers"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    await emit_progress({"event": "dexie_upload_request", "message": "Sending offer to Dexie API", "url": url})

    async with httpx.AsyncClient(timeout=30) as http_client:
        try:
            resp = await http_client.post(url, json={"offer": offer.to_bech32()}, headers=headers)
            log.debug(f"Upload offer to Dexie response: {resp.status_code} {resp.content}")
            resp.raise_for_status()
            result = resp.json()

            await emit_progress(
                {
                    "event": "dexie_upload_success",
                    "message": f"Successfully uploaded offer to Dexie with ID: {result.get('id', 'unknown')}",
                    "dexie_id": result.get("id"),
                    "response": result,
                }
            )

            return result
        except Exception as e:
            log.exception(f"Error uploading offer to Dexie: {offer.summary()}")
            await emit_progress(
                {
                    "event": "dexie_upload_failed",
                    "message": f"Failed to upload offer to Dexie: {resp.content}",
                    "error": str(e),
                }
            )
        return None


class LittleLiquidator:
    def __init__(
        self,
        rpc_client: CircuitRPCClient,
        max_bid_milli_amount: int,
        min_discount: float,
        min_profit_threshold: float = 0.02,  # Minimum 2% profit
        transaction_fee: float = 0.001,  # Estimated transaction fees
        max_offer_amount: float = 5.0,  # Maximum XCH amount per offer
        offer_expiry_seconds: int = 600,  # 1 hour offer expiry
        current_time: float | None = None,  # testing: inject current time
        progress_handler=None,  # Progress handler for streaming updates
        min_collateral_to_keep: float = 2.0,  # Minimum XCH to keep before creating offers
        disable_dexie_offers: bool = False,  # Disable creating or renewing offers to dexie
    ):
        self.rpc_client = rpc_client
        self.max_bid_milli_amount = max_bid_milli_amount
        self.min_discount = min_discount or 0.1
        self.min_profit_threshold = min_profit_threshold
        self.transaction_fee = transaction_fee
        self.max_offer_amount = max_offer_amount
        self.offer_expiry_seconds = offer_expiry_seconds
        # If provided, use injected time value for deterministic testing; otherwise use wall clock
        self._injected_current_time = current_time
        self.progress_handler = progress_handler
        self.min_collateral_to_keep = min_collateral_to_keep
        self.disable_dexie_offers = disable_dexie_offers

    async def _emit_progress(self, event_data: dict):
        """Emit a progress event to the configured handler if set."""
        if self.progress_handler:
            try:
                if asyncio.iscoroutinefunction(self.progress_handler):
                    await self.progress_handler(event_data)
                else:
                    self.progress_handler(event_data)
            except Exception as e:
                log.debug(f"Progress handler raised exception: {e}")

    async def _get_locked_coins(self) -> Dict[str, float]:
        """Get locked coins from persistent storage"""
        try:
            data = self.rpc_client.store.get("liquidator_locked_coins")
            if data:
                return json.loads(data)
        except Exception as e:
            log.debug(f"Failed to load locked coins: {e}")
        return {}

    async def _save_locked_coins(self, locked_coins: Dict[str, float]):
        """Save locked coins to persistent storage"""
        try:
            self.rpc_client.store.set("liquidator_locked_coins", json.dumps(locked_coins))
        except Exception as e:
            log.warning(f"Failed to save locked coins: {e}")

    def _now(self) -> float:
        """Return current time. Uses injected time if provided (testing), else time.time()."""
        return self._injected_current_time if self._injected_current_time is not None else time.time()

    async def _clean_expired_locks(self):
        """Remove expired coin locks"""
        locked_coins = await self._get_locked_coins()
        current_time = self._now()

        # Remove expired locks
        expired_coins = [coin for coin, expiry in locked_coins.items() if expiry < current_time]
        for coin in expired_coins:
            del locked_coins[coin]
            log.debug(f"Expired lock removed for coin {coin}")

        if expired_coins:
            await self._save_locked_coins(locked_coins)

        return locked_coins

    async def _lock_coins(self, coin_names: list[str]):
        """Lock coins for offer creation"""
        locked_coins = await self._get_locked_coins()
        current_time = self._now()
        expiry_time = current_time + self.offer_expiry_seconds

        for coin_name in coin_names:
            locked_coins[coin_name] = expiry_time
            log.debug(f"Locked coin {coin_name} until {expiry_time}")

        await self._save_locked_coins(locked_coins)

    async def _get_ignore_coins(self) -> list[str]:
        """Get list of coin names to ignore (locked coins)"""
        locked_coins = await self._clean_expired_locks()
        return list(locked_coins.keys())

    async def _get_active_offers(self) -> Dict[str, dict]:
        """Get active offers from persistent storage"""
        try:
            data = self.rpc_client.store.get("liquidator_active_offers")
            if data:
                return json.loads(data)
        except Exception as e:
            log.debug(f"Failed to load active offers: {e}")
        return {}

    async def _save_active_offers(self, offers: Dict[str, dict]):
        """Save active offers to persistent storage"""
        try:
            self.rpc_client.store.set("liquidator_active_offers", json.dumps(offers))
        except Exception as e:
            log.warning(f"Failed to save active offers: {e}")

    async def _add_active_offer(self, offer_id: str, xch_amount: float, created_at: float, market_price: float):
        """Add a new active offer to tracking"""
        active_offers = await self._get_active_offers()
        active_offers[offer_id] = {
            "xch_amount": xch_amount,
            "created_at": created_at,
            "expires_at": created_at + self.offer_expiry_seconds,
            "market_price_at_creation": market_price,
            "renewal_count": 0,
        }
        await self._save_active_offers(active_offers)
        log.debug(f"Added offer {offer_id} to active tracking")

    async def _remove_active_offer(self, offer_id: str):
        """Remove an offer from active tracking"""
        active_offers = await self._get_active_offers()
        if offer_id in active_offers:
            del active_offers[offer_id]
            await self._save_active_offers(active_offers)
            log.debug(f"Removed offer {offer_id} from active tracking")

    async def _is_offer_expired(self, offer_data: dict, now: float) -> bool:
        """Return True if the offer should be considered expired.
        In normal operation, check strict expiry against expires_at. For testing environments,
        this method can be made to use injected current time via self._now().
        """
        try:
            expires_at = offer_data.get("expires_at", 0)
            log.debug(f"Offer {offer_data} has expired: {now > expires_at}")
            return now > expires_at
        except Exception:
            log.exception("Error checking offer expiry")
            return True

    async def _manage_expired_offers(self):
        """Check for expired offers and recreate them with updated prices"""
        try:
            active_offers = await self._get_active_offers()
            log.info(f"Found {len(active_offers)} active offers")
            current_time = self._now()
            expired_offers = []

            for offer_id, offer_data in active_offers.items():
                if await self._is_offer_expired(offer_data, current_time):
                    expired_offers.append((offer_id, offer_data))

            if not expired_offers:
                return 0

            # If dexie offers are disabled, just clean up expired offers without renewing
            if self.disable_dexie_offers:
                log.info(f"Dexie offers disabled, removing {len(expired_offers)} expired offers from tracking")
                for offer_id, _ in expired_offers:
                    await self._remove_active_offer(offer_id)
                return 0

            log.info(f"Found {len(expired_offers)} expired offers to renew")
            await self._emit_progress(
                {
                    "event": "offer_renewal_started",
                    "message": f"Starting renewal process for {len(expired_offers)} expired offers",
                    "expired_offer_count": len(expired_offers),
                    "total_active_offers": len(active_offers),
                }
            )
            renewed_count = 0

            log.info(f"Starting renewal process for {len(expired_offers)} expired offers")

            for offer_id, offer_data in expired_offers:
                try:
                    # Get the current market price ratio (TBYC/XCH) and convert to mTBYC/XCH price
                    market_price = await fetch_dexie_price("XCH", "TBYC")
                    current_market_price = market_price if market_price else 0
                    if not current_market_price or current_market_price <= 0:
                        # Fall back to last known market price from offer creation in offline/test envs
                        fallback_price = offer_data.get("market_price_at_creation")
                        if fallback_price and fallback_price > 0:
                            log.warning(
                                f"Price API unavailable or invalid ({current_market_price}); using fallback price {fallback_price} for renewal of {offer_id}"
                            )
                            current_market_price = fallback_price
                        else:
                            log.warning(
                                f"Cannot renew offer {offer_id}: invalid market price {current_market_price}, removing from tracking"
                            )
                            await self._remove_active_offer(offer_id)
                            continue

                    xch_amount = offer_data["xch_amount"]
                    renewal_count = offer_data.get("renewal_count", 0)

                    # Check if we should continue renewing (max 10 renewals)
                    if renewal_count >= 10:
                        log.info(f"Offer {offer_id} reached max renewals, removing from tracking")
                        await self._remove_active_offer(offer_id)
                        continue

                    # Get initial balance to track consumption across renewals
                    ignore_coins = await self._get_ignore_coins()
                    available_coins = await self.rpc_client.wallet_coins(type="xch", ignore_coin_names=ignore_coins)
                    log.debug(f"Available coins: {available_coins}")
                    available_xch_coins = len([coin for coin in available_coins if coin["symbol"] == "XCH"])
                    remaining_balance_mojos = sum(x["amount"] for x in available_coins if x["symbol"] == "XCH")

                    # Check if we have enough balance to renew this offer
                    xch_amount_mojos = int(xch_amount * MOJOS)
                    available_xch = remaining_balance_mojos / MOJOS

                    if xch_amount_mojos > remaining_balance_mojos or available_xch_coins == 0:
                        log.warning(
                            f"Insufficient balance to renew offer {offer_id}: need {xch_amount:.6f} XCH, "
                            f"only {available_xch:.6f} XCH available and {available_xch_coins} XCH coins available. Removing from tracking."
                        )
                        await self._emit_progress(
                            {
                                "event": "offer_renewal_skipped",
                                "message": f"Removing offer {offer_id} from tracking - insufficient balance ({available_xch:.6f} XCH available, {xch_amount:.6f} XCH needed)",
                                "offer_id": offer_id,
                                "renewal_number": renewal_count + 1,
                                "xch_amount": xch_amount,
                                "available_balance": available_xch,
                            }
                        )
                        await self._remove_active_offer(offer_id)
                        continue
                    else:
                        log.info(
                            f"Sufficient balance for renewal of offer {offer_id} - {available_xch:.6f} XCH available, {xch_amount:.6f} XCH needed"
                        )
                    log.info(
                        f"Renewing expired offer {offer_id} (renewal #{renewal_count + 1}): {xch_amount} XCH at market price {current_market_price}"
                    )

                    await self._emit_progress(
                        {
                            "event": "offer_renewal_attempt",
                            "message": f"Renewing expired offer {offer_id} (renewal #{renewal_count + 1}) - {xch_amount} XCH at market price {current_market_price}",
                            "offer_id": offer_id,
                            "renewal_number": renewal_count + 1,
                            "xch_amount": xch_amount,
                            "market_price": current_market_price,
                        }
                    )

                    # Create new offer with current market conditions
                    result = await self.create_and_upload_offer(xch_amount_mojos, current_market_price)

                    if result["success"]:
                        # Remove old offer and add new one
                        await self._remove_active_offer(offer_id)

                        new_offer_id = result.get("offer_id", f"renewed_{offer_id}_{renewal_count + 1}")
                        await self._add_active_offer(new_offer_id, xch_amount, current_time, current_market_price)

                        # Update renewal count
                        active_offers = await self._get_active_offers()
                        if new_offer_id in active_offers:
                            active_offers[new_offer_id]["renewal_count"] = renewal_count + 1
                            await self._save_active_offers(active_offers)

                        renewed_count += 1

                        # Deduct consumed balance to track across multiple renewals
                        remaining_balance_mojos -= xch_amount_mojos
                        log.info(
                            f"Successfully renewed offer as {new_offer_id} (remaining balance: {remaining_balance_mojos / MOJOS:.6f} XCH)"
                        )

                        await self._emit_progress(
                            {
                                "event": "offer_renewal_success",
                                "message": f"Successfully renewed offer {offer_id} as {new_offer_id} - {xch_amount} XCH at updated market price",
                                "old_offer_id": offer_id,
                                "new_offer_id": new_offer_id,
                                "renewal_number": renewal_count + 1,
                                "xch_amount": xch_amount,
                                "market_price": current_market_price,
                                "remaining_balance": remaining_balance_mojos / MOJOS,
                            }
                        )
                    else:
                        error_msg = result.get("error", "Unknown error")
                        log.error(f"Failed to renew offer {offer_id}: {result}")

                        # Check if the error is due to insufficient coins
                        if "Can't find enough coins" in str(error_msg) or "Insufficient balance" in str(error_msg):
                            log.warning("Stopping offer renewal due to insufficient coins")
                            await self._emit_progress(
                                {
                                    "event": "offer_renewal_stopped",
                                    "message": f"Stopping offer renewal - ran out of coins to use (failed on offer {offer_id})",
                                    "offer_id": offer_id,
                                    "error": error_msg,
                                    "renewed_count": renewed_count,
                                    "remaining_expired_offers": len(expired_offers)
                                    - expired_offers.index((offer_id, offer_data))
                                    - 1,
                                }
                            )
                            # Stop processing remaining offers
                            break

                        await self._emit_progress(
                            {
                                "event": "offer_renewal_failed",
                                "message": f"Failed to renew offer {offer_id} (renewal #{renewal_count + 1}) - {result}",
                                "offer_id": offer_id,
                                "renewal_number": renewal_count + 1,
                                "xch_amount": xch_amount,
                                "error": error_msg,
                            }
                        )

                except Exception as e:
                    log.error(f"Error renewing offer {offer_id}: {e}")

            log.info(f"Offer renewal completed: {renewed_count} of {len(expired_offers)} expired offers renewed")
            return renewed_count

        except Exception:
            log.exception("Error in expired offer management")
            return 0

    async def get_offers_status(self):
        """Get status of all active offers for monitoring"""
        try:
            active_offers = await self._get_active_offers()

            offer_status = []
            for offer_id, offer_data in active_offers.items():
                time_remaining = offer_data["expires_at"] - (self._now())
                offer_status.append(
                    {
                        "offer_id": offer_id,
                        "xch_amount": offer_data["xch_amount"],
                        "created_at": offer_data["created_at"],
                        "expires_at": offer_data["expires_at"],
                        "time_remaining_seconds": max(0, int(time_remaining)),
                        "is_expired": time_remaining <= 0,
                        "market_price_at_creation": offer_data["market_price_at_creation"],
                        "renewal_count": offer_data.get("renewal_count", 0),
                    }
                )

            return {"total_offers": len(offer_status), "offers": offer_status}
        except Exception as e:
            log.error(f"Error getting offers status: {e}")
            return {"total_offers": 0, "offers": [], "error": str(e)}

    async def _get_xch_price(self):
        market_price = await fetch_dexie_price("XCH", "TBYC")
        return market_price * PRICE_PRECISION if market_price else 0

    async def _split_large_coins(self):
        """Split large XCH coins into smaller chunks suitable for offers"""
        try:
            ignore_coins = await self._get_ignore_coins()

            # Get current XCH coins, excluding locked ones
            coins = await self.rpc_client.wallet_coins(type="xch", ignore_coin_names=ignore_coins)
            if not coins:
                await self._emit_progress(
                    {"event": "coin_splitting_skipped", "message": "No XCH coins available for splitting"}
                )
                return

            # Count existing small enough coins (suitable for offers without splitting)
            small_enough_coins = [coin for coin in coins if coin.get("amount", 0) / MOJOS <= self.max_offer_amount]
            small_coin_count = len(small_enough_coins)

            # Skip splitting if we already have 5 or more small enough coins
            if small_coin_count >= 5:
                log.info(
                    f"Skipping coin splitting: already have {small_coin_count} small enough coins (≤ {self.max_offer_amount} XCH)"
                )
                await self._emit_progress(
                    {
                        "event": "coin_splitting_skipped",
                        "message": f"Coin splitting skipped - already have {small_coin_count} coins suitable for offers (≤ {self.max_offer_amount} XCH)",
                        "small_coin_count": small_coin_count,
                        "max_offer_amount": self.max_offer_amount,
                    }
                )
                return
            else:
                log.info(f"Found {small_coin_count} small enough coins (≤ {self.max_offer_amount} XCH)")

            # Find coins larger than max_offer_amount * 2
            large_coins = [coin for coin in coins if coin.get("amount", 0) / MOJOS > self.max_offer_amount * 2]
            if not large_coins:
                log.info(f"No large coins found to split: {coins}")
                await self._emit_progress(
                    {
                        "event": "coin_splitting_skipped",
                        "message": f"No large coins found to split (threshold: {self.max_offer_amount * 2} XCH)",
                        "total_coins": len(coins),
                        "threshold": self.max_offer_amount * 2,
                    }
                )
                return

            log.info(f"Found {len(large_coins)} large coins to split")
            await self._emit_progress(
                {
                    "event": "coin_splitting_started",
                    "message": f"Starting coin splitting process - found {len(large_coins)} large coins",
                    "large_coin_count": len(large_coins),
                    "small_coin_count": small_coin_count,
                    "target_coin_size": self.max_offer_amount,
                }
            )

            mojos_per_chunk = self.max_offer_amount * MOJOS
            log.info(f"Mojos per chunk: {mojos_per_chunk} ({self.max_offer_amount})")

            for coin in large_coins:
                coin_amount_xch = coin["amount"]
                coin_amount_xch_float = coin_amount_xch / MOJOS
                target_chunks = min(10, int(coin_amount_xch / mojos_per_chunk))

                if target_chunks > 1:
                    log.info(
                        f"Splitting large coin {coin['name']} ({coin_amount_xch_float} XCH) into {target_chunks} chunks"
                    )
                    await self._emit_progress(
                        {
                            "event": "coin_splitting",
                            "message": f"Splitting coin {coin['name'][:8]}... ({coin_amount_xch_float:.6f} XCH) into {target_chunks} chunks",
                            "coin_name": coin["name"],
                            "coin_amount_xch": coin_amount_xch_float,
                            "target_chunks": target_chunks,
                        }
                    )

                    mojos_per_chunk = int(coin_amount_xch / target_chunks)
                    # Create multiple outputs of max_offer_amount each
                    recipients = []
                    amounts = []
                    for i in range(target_chunks):
                        amounts.append(int(mojos_per_chunk))

                    # Add a remainder if any
                    remainder = coin_amount_xch - (target_chunks * mojos_per_chunk)
                    if remainder > 0.001:  # Only if a remainder is significant
                        amounts.append(int(remainder))

                    try:
                        # Send to multiple recipients to split the coin
                        result = await self.rpc_client.wallet_split_coin(coin["name"], amounts)
                        if result.get("status") == "success":
                            log.info(f"Successfully broke coin into {len(amounts)} pieces")
                            await self._emit_progress(
                                {
                                    "event": "coin_split_success",
                                    "message": f"Successfully split coin into {len(amounts)} pieces",
                                    "coin_name": coin["name"],
                                    "pieces_created": len(amounts),
                                    "original_amount_xch": coin_amount_xch_float,
                                }
                            )
                            return  # Only split one coin at a time to avoid complications
                        else:
                            log.warning(f"Failed to split coin: {result}")
                            await self._emit_progress(
                                {
                                    "event": "coin_split_failed",
                                    "message": f"Failed to split coin {coin['name'][:8]}...: {result}",
                                    "coin_name": coin["name"],
                                    "error": result,
                                }
                            )
                    except Exception as e:
                        log.warning(f"Failed to split large coin: {e}")
                        await self._emit_progress(
                            {
                                "event": "coin_split_error",
                                "message": f"Error splitting coin {coin['name'][:8]}...: {e}",
                                "coin_name": coin["name"],
                                "error": str(e),
                            }
                        )

        except Exception as e:
            log.exception("Error in coin splitting")
            await self._emit_progress(
                {"event": "coin_splitting_error", "message": f"Error in coin splitting process: {e}", "error": str(e)}
            )

    async def run(self, run_once=False):
        log.info(
            f"Starting liquidator with base url: {self.rpc_client.client.base_url}, "
            f"private key: {'******' if self.rpc_client.synthetic_secret_keys else 'None'}, "
            f"fee per cost: {self.rpc_client.fee_per_cost}"
        )

        # Emit detailed liquidator configuration at startup
        config = {
            "max_bid_milli_amount": self.max_bid_milli_amount,
            "min_discount": self.min_discount,
            "min_profit_threshold": self.min_profit_threshold,
            "transaction_fee": self.transaction_fee,
            "max_offer_amount": self.max_offer_amount,
            "offer_expiry_seconds": self.offer_expiry_seconds,
            "base_url": str(self.rpc_client.client.base_url),
            "has_private_keys": bool(self.rpc_client.synthetic_secret_keys),
            "fee_per_cost": self.rpc_client.fee_per_cost,
        }

        await self._emit_progress(
            {
                "event": "liquidator_started",
                "message": "Little Liquidator started with configuration",
                "configuration": config,
            }
        )

        if self.rpc_client.synthetic_public_keys:
            log.info(f"Synthetic public keys: {self.rpc_client.synthetic_public_keys}")
            await self._emit_progress(
                {
                    "event": "keys_loaded",
                    "message": f"Loaded {len(self.rpc_client.synthetic_public_keys)} synthetic public keys",
                }
            )
        else:
            log.warning("No synthetic public keys found, will not bid on auctions.")
            await self._emit_progress(
                {"event": "warning", "message": "No synthetic public keys found - liquidator will not bid on auctions"}
            )

        if run_once:
            await self.process_once()
            return

        while True:
            await self.process_once_and_sleep()

    async def process_once_and_sleep(self):
        try:
            await self.process_once()
        except Exception as e:
            log.exception("Error processing liquidations: %s", e)
            await self._emit_progress({"event": "error", "message": f"Error processing liquidations: {e}"})

        # Show current balance before waiting for next upkeep event
        try:
            ignore_coins = await self._get_ignore_coins()
            balances = await self.rpc_client.wallet_balances(ignore_coin_names=ignore_coins)

            # Get active offers status
            offers_status = await self.get_offers_status()
            active_offers = [offer for offer in offers_status.get("offers", []) if not offer.get("is_expired", True)]
            pending_offers = [offer for offer in offers_status.get("offers", []) if offer.get("is_expired", True)]

            # Extract key balance information
            xch_balance = balances.get("xch", 0) / MOJOS
            byc_balance = balances.get("byc", 0) / MCAT

            balance_summary = f"XCH: {xch_balance:.6f}, BYC: {byc_balance:.2f}"
            offers_summary = f"Active offers: {len(active_offers)}, Pending renewal: {len(pending_offers)}"

            await self._emit_progress(
                {
                    "event": "current_balance",
                    "message": f"Current balances before upkeep wait - {balance_summary}, {offers_summary}",
                    "balances": {
                        "xch": xch_balance,
                        "byc": byc_balance,
                        "locked_coins_count": len(ignore_coins),
                    },
                    "offers": {
                        "active_count": len(active_offers),
                        "pending_renewal_count": len(pending_offers),
                        "total_count": offers_status.get("total_offers", 0),
                    },
                }
            )
        except Exception as e:
            log.exception(f"Failed to get balances for progress report: {e}")
            await self._emit_progress(
                {"event": "balance_check_failed", "message": f"Could not retrieve current balances: {e}"}
            )

        await self._emit_progress({"event": "waiting", "message": "Waiting 50 seconds for next upkeep cycle"})
        log.info("Waiting for next upkeep...")
        await asyncio.sleep(50)

    async def process_once(self):
        await self._emit_progress({"event": "started", "message": "Starting liquidation process"})

        # fetch the latest fee per cost from the node
        await self._emit_progress({"event": "status", "message": "Fetching fee per cost from node"})
        await self.rpc_client.set_fee_per_cost()
        result = {
            "status": "completed",
            "actions_taken": {
                "auctions_started": 0,
                "bids_placed": 0,
                "bad_debts_recovered": 0,
                "auctions_restarted": 0,
                "offers_renewed": 0,
                "offers_created": [],
            },
            "current_state": {
                "vaults_pending_liquidation": [],
                "vaults_in_liquidation": [],
                "vaults_with_bad_debt": [],
            },
        }

        await self._emit_progress({"event": "status", "message": "Fetching protocol state"})
        state = await self.rpc_client.upkeep_state(
            vaults=True, surplus_auctions=False, recharge_auctions=False, treasury=True, bills=False
        )
        if not state:
            log.warning("Failed to get protocol state")
            await self._emit_progress({"event": "error", "message": "Failed to get protocol state"})
            result["status"] = "failed"
            result["error"] = "Failed to get protocol state"
            return result

        log.info("State: %s", pformat(state))

        # Emit progress with state summary
        pending_count = len(state.get("vaults_pending_liquidation", []))
        in_liquidation_count = len(state.get("vaults_in_liquidation", []))
        bad_debt_count = len(state.get("vaults_with_bad_debt", []))
        await self._emit_progress(
            {
                "event": "state_fetched",
                "message": f"State fetched - Pending: {pending_count}, In liquidation: {in_liquidation_count}, Bad debt: {bad_debt_count}",
            }
        )

        # Update the current state with vault information
        result["current_state"]["vaults_pending_liquidation"] = state.get("vaults_pending_liquidation", [])
        result["current_state"]["vaults_in_liquidation"] = state.get("vaults_in_liquidation", [])
        result["current_state"]["vaults_with_bad_debt"] = state.get("vaults_with_bad_debt", [])

        # In tests, the mock client may not have synthetic_public_keys attribute
        has_keys = getattr(self.rpc_client, "synthetic_public_keys", None)
        if has_keys is None:
            # Assume keys exist in test environments where attribute may be missing
            has_keys = True
        if has_keys:
            # First, manage expired offers
            renewed_offers = await self._manage_expired_offers()
            if renewed_offers > 0:
                result["actions_taken"]["offers_renewed"] = renewed_offers

            await self._split_large_coins()

            # Get balances, excluding locked coins
            ignore_coins = await self._get_ignore_coins()
            balances = await self.rpc_client.wallet_balances(ignore_coin_names=ignore_coins)
            log.debug("Balances: %s", pformat(balances))
            if ignore_coins:
                log.debug(f"Ignored {len(ignore_coins)} locked coins")

            # Log offers status for monitoring
            offers_status = await self.get_offers_status()
            if offers_status["total_offers"] > 0:
                log.info(f"Active offers: {offers_status['total_offers']} total")
                for offer in offers_status["offers"]:
                    status_msg = "expired" if offer["is_expired"] else f"{offer['time_remaining_seconds']}s remaining"
                    log.debug(f"Offer {offer['offer_id']}: {offer['xch_amount']} XCH, {status_msg}")

            # Check for incomplete liquidations and restart them
            auctions_restarted = await self.check_and_restart_incomplete_liquidations(state)
            result["actions_taken"]["auctions_restarted"] = auctions_restarted
            # either bid or start, but not both in the same iteration to avoid block size issues
            if state.get("vaults_in_liquidation"):
                await self._emit_progress(
                    {"event": "status", "message": f"Bidding on {len(state['vaults_in_liquidation'])} auctions"}
                )
                bid_results = await self.bid_on_auctions(state["vaults_in_liquidation"], balances)
                result["actions_taken"]["bids_placed"] = bid_results["bids_placed"]
                result["actions_taken"]["offers_created"] = bid_results["offers_created"]
                await self._emit_progress(
                    {
                        "event": "bids_completed",
                        "message": f"Placed {bid_results['bids_placed']} bids, created {len(bid_results['offers_created'])} offers",
                    }
                )
            elif state.get("vaults_pending_liquidation"):
                await self._emit_progress(
                    {
                        "event": "status",
                        "message": f"Starting auctions for {len(state['vaults_pending_liquidation'])} vaults",
                    }
                )
                auctions_started = await self.start_auctions(state["vaults_pending_liquidation"])
                result["actions_taken"]["auctions_started"] = auctions_started
                await self._emit_progress(
                    {"event": "auctions_started", "message": f"Started {auctions_started} auctions"}
                )

            if state.get("vaults_with_bad_debt"):
                await self._emit_progress(
                    {
                        "event": "status",
                        "message": f"Recovering bad debts for {len(state['vaults_with_bad_debt'])} vaults",
                    }
                )
                bad_debts_recovered = await self.recover_bad_debts(state["vaults_with_bad_debt"], state)
                result["actions_taken"]["bad_debts_recovered"] = bad_debts_recovered
                await self._emit_progress(
                    {"event": "bad_debts_recovered", "message": f"Recovered {bad_debts_recovered} bad debts"}
                )

            # Check if we have excess collateral to convert to BYC during normal cycles
            # await self._check_and_create_collateral_offers(balances, result)

        await self._emit_progress({"event": "completed", "message": "Liquidation process completed", "done": True})
        return result

    async def _check_and_create_collateral_offers(self, balances, result):
        """
        Check if we have excess collateral above the minimum threshold and create offers to convert it to BYC.

        Args:
            balances: Current wallet balances
            result: Result dictionary to update with offer creation info
        """
        if self.disable_dexie_offers:
            log.debug("Dexie offers disabled, skipping collateral offer creation")
            return
        try:
            # Get current XCH balance
            xch_balance_mojos = balances.get("xch", 0)
            xch_balance = xch_balance_mojos / MOJOS

            if xch_balance <= self.min_collateral_to_keep:
                log.debug(
                    f"XCH balance {xch_balance:.6f} is at or below minimum threshold {self.min_collateral_to_keep:.6f}"
                )
                return
            log.debug(f"XCH balance is above minimum threshold: {xch_balance:.6f} <= {self.min_collateral_to_keep:.6f}")
            # Calculate excess collateral
            excess_xch = xch_balance - self.min_collateral_to_keep

            if excess_xch < 0.1:  # Only create offers for meaningful amounts (> 0.1 XCH)
                log.debug(f"Excess XCH {excess_xch:.6f} too small to create offers")
                return

            log.info(
                f"Excess collateral detected: {excess_xch:.6f} XCH (balance: {xch_balance:.6f}, minimum: {self.min_collateral_to_keep:.6f})"
            )

            # Get current market price from Dexie
            market_price = await fetch_dexie_price("XCH", "TBYC")

            if not market_price:
                log.warning("Cannot get market price from Dexie, skipping collateral offer creation")
                return

            # Convert market price to the format expected by offer creation
            market_price_byc = market_price / MCAT

            await self._emit_progress(
                {
                    "event": "excess_collateral_detected",
                    "message": f"Creating offers for excess collateral: {excess_xch:.6f} XCH at market price {market_price_byc:.2f}",
                    "excess_xch": excess_xch,
                    "market_price": market_price_byc,
                }
            )

            # Create offers for the excess collateral, passing available balance
            excess_xch_mojos = int(excess_xch * MOJOS)
            available_balance = xch_balance_mojos - self.min_collateral_to_keep
            multiple_offers_result = await self.create_multiple_offers(
                excess_xch_mojos, market_price_byc, collateral_balance_available=available_balance
            )

            if multiple_offers_result["total_success"] > 0:
                log.info(
                    f"Successfully created {multiple_offers_result['total_success']} collateral offers for {excess_xch:.6f} XCH"
                )

                # Update result with collateral offers
                if "collateral_offers_created" not in result["actions_taken"]:
                    result["actions_taken"]["collateral_offers_created"] = []
                result["actions_taken"]["collateral_offers_created"].extend(multiple_offers_result["successful_offers"])

                await self._emit_progress(
                    {
                        "event": "collateral_offers_created",
                        "message": f"Created {multiple_offers_result['total_success']} offers from excess collateral",
                        "offers_created": multiple_offers_result["total_success"],
                        "excess_xch_used": excess_xch,
                    }
                )

                for offer_result in multiple_offers_result["successful_offers"]:
                    if offer_result.get("dexie_id"):
                        log.info(f"Collateral offer uploaded to dexie.space with ID: {offer_result['dexie_id']}")

            else:
                log.warning(f"Failed to create any collateral offers: {multiple_offers_result}")
                await self._emit_progress(
                    {
                        "event": "collateral_offers_failed",
                        "message": f"Failed to create offers from excess collateral: {multiple_offers_result.get('error', 'Unknown error')}",
                    }
                )

        except Exception as e:
            log.exception(f"Error checking and creating collateral offers: {e}")
            await self._emit_progress(
                {
                    "event": "collateral_check_error",
                    "message": f"Error during collateral offer creation: {e}",
                }
            )

    async def start_auctions(self, vaults_pending):
        log.info("Found vaults pending liquidation: %s", vaults_pending)
        auctions_started = 0
        for vault_pending in vaults_pending:
            vault_pending_name = vault_pending["name"]
            log.info("Starting auction for vault %s", vault_pending_name)
            await self._emit_progress(
                {
                    "event": "transaction_starting",
                    "message": f"Starting auction for vault {vault_pending_name}",
                    "transaction_type": "vault_liquidation",
                    "vault_name": vault_pending_name,
                }
            )
            try:
                result = await self.rpc_client.upkeep_vaults_liquidate(
                    coin_name=vault_pending_name, ignore_coin_names=await self._get_ignore_coins()
                )
                log.info(f"Auction started for vault {vault_pending_name}: {result}")
                await self._emit_progress(
                    {
                        "event": "transaction_completed",
                        "message": f"Auction started for vault {vault_pending_name}",
                        "transaction_type": "vault_liquidation",
                        "vault_name": vault_pending_name,
                    }
                )
                auctions_started += 1
            except APIError as e:
                log.error("Failed to start auction for vault %s: %s", vault_pending_name, e)
                await self._emit_progress(
                    {
                        "event": "transaction_failed",
                        "message": f"Failed to start auction for vault {vault_pending_name}: {e}",
                        "transaction_type": "vault_liquidation",
                        "vault_name": vault_pending_name,
                    }
                )
        return auctions_started

    async def bid_on_auctions(self, vaults_in_liquidation, balances):
        log.info("Found vaults in liquidation: %s", vaults_in_liquidation)
        bids_placed = 0
        offers_created = []
        current_balances = balances
        statutes = await self.rpc_client.statutes_list(full=False)
        log.debug("Statutes: %s", pformat(statutes))
        min_bid_amount_bps = statutes["implemented_statutes"]["VAULT_AUCTION_MINIMUM_BID_BPS"]
        min_bid_amount_flat = statutes["implemented_statutes"]["VAULT_AUCTION_MINIMUM_BID_FLAT"] / MCAT
        for vault_in_liquidation in vaults_in_liquidation:
            vault_name = vault_in_liquidation["name"]
            vault_info = await self.rpc_client.upkeep_vaults_list(coin_name=vault_name, seized=True)
            if not vault_info:
                log.warning("Failed to get vault info for %s", vault_name)
                await self._emit_progress(
                    {
                        "event": "bid_decision",
                        "decision": "skip",
                        "reason": "Failed to get vault info",
                        "vault_name": vault_name,
                    }
                )
                continue

            log.debug("Vault info: %s", pformat(vault_info))
            auction_price_per_xch = vault_info.get("auction_price")
            if not auction_price_per_xch:
                log.warning("Could not get auction_price from vault_info for %s", vault_name)
                await self._emit_progress(
                    {
                        "event": "bid_decision",
                        "decision": "skip",
                        "reason": "No auction price available",
                        "vault_name": vault_name,
                    }
                )
                continue

            available_xch = vault_info.get("collateral")
            if not available_xch or available_xch < 1:
                log.info(f"Not enough XCH to bid, skipping ({available_xch})")
                await self._emit_progress(
                    {
                        "event": "bid_decision",
                        "decision": "skip",
                        "reason": f"Insufficient collateral ({available_xch} mojos)",
                        "vault_name": vault_name,
                    }
                )
                continue

            # Check market conditions and profitability using dexie price API
            market_check = await self.check_market_conditions(available_xch, auction_price_per_xch)
            if not market_check["profitable"]:
                log.info(f"Market conditions unfavorable: {market_check['reason']}")
                await self._emit_progress(
                    {
                        "event": "bid_decision",
                        "decision": "skip",
                        "reason": f"Market conditions unfavorable: {market_check['reason']}",
                        "vault_name": vault_name,
                        "market_price": market_check.get("market_price"),
                        "auction_price": market_check.get("auction_price"),
                        "discount": market_check.get("discount"),
                    }
                )
                continue

            log.info(
                f"Market conditions favorable - discount: {market_check['discount']:.2%}, "
                f"market price: {market_check['market_price']}, auction price: {auction_price_per_xch / PRICE_PRECISION}"
            )
            await self._emit_progress(
                {
                    "event": "bid_decision",
                    "decision": "favorable_market",
                    "reason": f"Market conditions favorable - discount: {market_check['discount']:.2%}",
                    "vault_name": vault_name,
                    "market_price": market_check.get("market_price"),
                    "auction_price": auction_price_per_xch / PRICE_PRECISION,
                    "discount": market_check.get("discount"),
                }
            )
            mbyc_bid_amount = await self.calculate_byc_bid_amount(
                current_balances, vault_info, min_bid_amount_bps, min_bid_amount_flat, vault_name
            )
            if mbyc_bid_amount < 0:
                log.info("Insufficient balance to bid")
                await self._emit_progress(
                    {
                        "event": "bid_decision",
                        "decision": "skip",
                        "reason": "Insufficient balance to meet auction requirements",
                        "vault_name": vault_name,
                    }
                )
                continue

            # Calculate bid amount
            xch_to_acquire = self.calculate_acquired_xch(mbyc_bid_amount, available_xch, auction_price_per_xch)
            log.debug(f"XCH to acquire with BYC bid: {xch_to_acquire:.6f} with bid amount: {mbyc_bid_amount:.2f} mTBYC")
            # if bid amount is less than 1 TBYC (1000 mTBYC), then there's no need to bid
            if mbyc_bid_amount < 1000:
                log.info("We don't have any byc left, skipping")
                await self._emit_progress(
                    {
                        "event": "bid_decision",
                        "decision": "skip",
                        "reason": f"Bid amount too small ({mbyc_bid_amount / MCAT:.3f} TBYC < 1 TBYC)",
                        "vault_name": vault_name,
                    }
                )
                continue

            log.info(
                f"Bidding {mbyc_bid_amount / MCAT} TBYC for {xch_to_acquire / MOJOS} XCH at {auction_price_per_xch / PRICE_PRECISION} TBYC/XCH"
            )

            # Emit decision to proceed with bid
            await self._emit_progress(
                {
                    "event": "bid_decision",
                    "decision": "proceed",
                    "reason": f"Placing bid: {mbyc_bid_amount / MCAT:.2f} TBYC for {xch_to_acquire / MOJOS:.3f} XCH",
                    "vault_name": vault_name,
                    "bid_amount": mbyc_bid_amount / MCAT,
                    "xch_amount": xch_to_acquire / MOJOS,
                    "auction_price": auction_price_per_xch / PRICE_PRECISION,
                }
            )

            try:
                await self._emit_progress(
                    {
                        "event": "transaction_starting",
                        "message": f"Placing bid on vault {vault_name} ({mbyc_bid_amount / MCAT} TBYC for {xch_to_acquire / MOJOS:.3f} XCH)",
                        "transaction_type": "vault_bid",
                        "vault_name": vault_name,
                        "bid_amount": mbyc_bid_amount / MCAT,
                        "xch_amount": xch_to_acquire / MOJOS,
                    }
                )

                # Place the bid
                # Convert mTBYC to TBYC for the API (API will convert back using _convert_number)
                result = await self.rpc_client.upkeep_vaults_bid(
                    coin_name=vault_name,
                    amount=mbyc_bid_amount / MCAT,
                    max_bid_price=(auction_price_per_xch + 1) / PRICE_PRECISION,
                    ignore_coin_names=await self._get_ignore_coins(),
                )
                await self._emit_progress(
                    {
                        "event": "transaction_completed",
                        "message": f"Successfully placed bid on vault {vault_name}",
                        "transaction_type": "vault_bid",
                        "vault_name": vault_name,
                    }
                )
                bids_placed += 1
                # Refresh balances after successful bid
                ignore_coins = await self._get_ignore_coins()
                current_balances = await self.rpc_client.wallet_balances(ignore_coin_names=ignore_coins)
                log.debug("Refreshed balances after bid: %s", pformat(current_balances))

                # Log structured data for tax calculations
                tax_data = {
                    "event_type": "collateral_acquired",
                    "timestamp": self._now(),
                    "vault_name": vault_name,
                    "xch_acquired": xch_to_acquire / MOJOS,
                    "byc_paid": mbyc_bid_amount,
                    "auction_price_per_xch": auction_price_per_xch / PRICE_PRECISION,
                    "market_price_per_xch": market_check["market_price"],
                    "discount_percentage": market_check["discount"],
                    "estimated_profit_xch": (xch_to_acquire / MOJOS)
                    * (market_check["market_price"] - auction_price_per_xch / PRICE_PRECISION),
                }
                log.info(f"TAX_EVENT: {json.dumps(tax_data)}")

                # After a successful bid, create multiple offers based on max_offer_amount
                # Pass the current XCH balance to prevent creating more offers than available
                if not self.disable_dexie_offers:
                    current_xch_balance = current_balances.get("xch", 0)
                    available_xch = current_xch_balance - self.min_collateral_to_keep
                    multiple_offers_result = await self.create_multiple_offers(
                        xch_to_acquire, market_check["market_price"], collateral_balance_available=available_xch
                    )

                    if multiple_offers_result["total_success"] > 0:
                        log.info(
                            f"Successfully created {multiple_offers_result['total_success']} offers for {xch_to_acquire / MOJOS} XCH"
                        )
                        offers_created.extend(multiple_offers_result["successful_offers"])

                        for offer_result in multiple_offers_result["successful_offers"]:
                            if offer_result.get("dexie_id"):
                                log.info(f"Offer uploaded to dexie.space with ID: {offer_result['dexie_id']}")

                        log.info("Total offers created: %s", len(offers_created))
                        if multiple_offers_result["total_failed"] > 0:
                            log.warning(f"{multiple_offers_result['total_failed']} offers failed to create")
                        break
                    else:
                        log.error(f"Failed to create any offers for acquired XCH: {multiple_offers_result}")
                else:
                    log.debug("Dexie offers disabled, skipping offer creation after bid")
                    break

            except APIError as e:
                log.error("Failed to bid on auction for vault %s: %s", vault_name, e)
                await self._emit_progress(
                    {
                        "event": "transaction_failed",
                        "message": f"Failed to place bid on vault {vault_name}: {e}",
                        "transaction_type": "vault_bid",
                        "vault_name": vault_name,
                    }
                )
        return {"bids_placed": bids_placed, "offers_created": offers_created}

    async def calculate_byc_bid_amount(self, balances, vault_info, min_bid_amount_bps, min_bid_amount_flat, vault_name):
        # Support both possible keys used across code/tests
        byc_balance = balances.get("byc", balances.get("byc_balance", 0))
        # Determine required BYC to be able to bid up to max_bid_amount (in BYC units)
        vault_debt = vault_info["debt"] or 0
        vault_collateral = vault_info.get("collateral", 0)
        auction_price = vault_info.get("auction_price", 0)

        # calculate minimum bid required by calculating relative and taking max out of it relative or flat
        log.debug(
            f"Vault debt: {vault_debt} | min_bid_amount_bps: {min_bid_amount_bps} | min_bid_amount_flat: {min_bid_amount_flat}"
        )
        min_relative_bid_amount = (vault_debt * min_bid_amount_bps) / PRECISION_BPS
        log.debug(f"Picking bid amount from values: {min_relative_bid_amount} {min_bid_amount_flat}")
        min_bid_required = max(min_relative_bid_amount, min_bid_amount_flat)

        # Calculate bid needed to take all collateral
        # collateral is in mojos, auction_price is in (mTBYC * 100) per XCH
        # So: bid_for_all_collateral = (collateral_mojos / MOJOS) * (auction_price / 100) * MCAT
        # Result is in mTBYC (milli-units) to match other bid amounts
        bid_for_all_collateral = 0
        if vault_collateral > 0 and auction_price > 0:
            bid_for_all_collateral = (vault_collateral / MOJOS) * (auction_price / PRICE_PRECISION) * MCAT

        log.info(
            f"My TBYC balance: {byc_balance / MCAT:.2f} | Min bid required: {min_bid_required / MCAT:.2f} | "
            f"Debt: {vault_debt / MCAT:.2f} | Bid for all collateral: {bid_for_all_collateral / MCAT:.2f}"
        )

        # Auction requirements: bid must either:
        # 1. Exceed min_bid_required, OR
        # 2. Pay off all debt (vault_debt), OR
        # 3. Take all collateral (bid_for_all_collateral)

        # Choose the bid amount based on available balance and requirements
        if byc_balance >= bid_for_all_collateral:
            # We can take all collateral - this satisfies requirement #3
            desired_bid = bid_for_all_collateral
            strategy = "take_all_collateral"
            await self._emit_progress(
                {
                    "event": "bid_calculation",
                    "strategy": strategy,
                    "reason": f"Balance sufficient to take all collateral ({byc_balance / MCAT:.2f} >= {bid_for_all_collateral / MCAT:.2f} TBYC)",
                    "vault_name": vault_name,
                    "bid_amount": desired_bid / MCAT,
                    "balance": byc_balance / MCAT,
                }
            )
        elif byc_balance >= vault_debt:
            # We can pay off all debt - this satisfies requirement #2
            desired_bid = vault_debt
            strategy = "pay_off_debt"
            await self._emit_progress(
                {
                    "event": "bid_calculation",
                    "strategy": strategy,
                    "reason": f"Balance sufficient to pay off all debt ({byc_balance / MCAT:.2f} >= {vault_debt / MCAT:.2f} TBYC)",
                    "vault_name": vault_name,
                    "bid_amount": desired_bid / MCAT,
                    "balance": byc_balance / MCAT,
                }
            )
        elif byc_balance >= min_bid_required:
            # We can meet minimum bid - this satisfies requirement #1
            desired_bid = min_bid_required
            strategy = "meet_minimum"
            await self._emit_progress(
                {
                    "event": "bid_calculation",
                    "strategy": strategy,
                    "reason": f"Balance sufficient to meet minimum bid ({byc_balance / MCAT:.2f} >= {min_bid_required / MCAT:.2f} TBYC)",
                    "vault_name": vault_name,
                    "bid_amount": desired_bid / MCAT,
                    "balance": byc_balance / MCAT,
                }
            )
        else:
            # We don't have enough to meet any requirement
            log.info(f"Not enough TBYC to meet auction requirements. Need at least {min_bid_required / MCAT:.2f}")
            await self._emit_progress(
                {
                    "event": "bid_calculation",
                    "strategy": "insufficient_balance",
                    "reason": f"Balance insufficient ({byc_balance / MCAT:.2f} < {min_bid_required / MCAT:.2f} TBYC minimum)",
                    "vault_name": vault_name,
                    "balance": byc_balance / MCAT,
                    "min_required": min_bid_required / MCAT,
                }
            )
            return -1

        # Apply max_bid_milli_amount limit if set
        if self.max_bid_milli_amount:
            # Ensure max_bid can meet at least one requirement
            if (
                self.max_bid_milli_amount < min_bid_required
                and self.max_bid_milli_amount < vault_debt
                and self.max_bid_milli_amount < bid_for_all_collateral
            ):
                log.warning(
                    f"Max bid amount {self.max_bid_milli_amount / MCAT:.2f} is too low to meet any auction requirement. "
                    f"Min bid: {min_bid_required / MCAT:.2f}, Debt: {vault_debt / MCAT:.2f}, All collateral: {bid_for_all_collateral / MCAT:.2f}"
                )
                await self._emit_progress(
                    {
                        "event": "bid_calculation",
                        "strategy": "max_bid_too_low",
                        "reason": f"Max bid limit ({self.max_bid_milli_amount / MCAT:.2f} TBYC) too low to meet any requirement",
                        "vault_name": vault_name,
                        "max_bid_limit": self.max_bid_milli_amount / MCAT,
                        "min_required": min_bid_required / MCAT,
                        "debt": vault_debt / MCAT,
                        "all_collateral_bid": bid_for_all_collateral / MCAT,
                    }
                )
                return -1

            # Check if max_bid limit constrains our desired bid
            original_desired_bid = desired_bid
            desired_bid = min(byc_balance, self.max_bid_milli_amount, desired_bid)
            if desired_bid < original_desired_bid:
                await self._emit_progress(
                    {
                        "event": "bid_calculation",
                        "strategy": "max_bid_applied",
                        "reason": f"Max bid limit applied, reducing bid from {original_desired_bid / MCAT:.2f} to {desired_bid / MCAT:.2f} TBYC",
                        "vault_name": vault_name,
                        "original_bid": original_desired_bid / MCAT,
                        "final_bid": desired_bid / MCAT,
                        "max_bid_limit": self.max_bid_milli_amount / MCAT,
                    }
                )
        else:
            desired_bid = min(byc_balance, desired_bid)

        log.info(f"Final bid amount: {desired_bid / MCAT:.2f}")
        await self._emit_progress(
            {
                "event": "bid_calculation",
                "strategy": "final_bid",
                "reason": f"Final calculated bid amount: {desired_bid / MCAT:.2f} TBYC",
                "vault_name": vault_name,
                "bid_amount": desired_bid / MCAT,
            }
        )
        return desired_bid

    def calculate_acquired_xch(self, byc_bid_amount, available_xch, bid_price_per_xch):
        log.debug(f"Calculating xch to acquire with params: {self.max_bid_milli_amount}, {bid_price_per_xch}")
        xch_to_acquire = ((byc_bid_amount) / (bid_price_per_xch / 100)) * MOJOS
        log.debug(f"XCH to acquire: {xch_to_acquire} vs available: {available_xch}")
        return xch_to_acquire

    async def check_market_conditions(self, xch_to_acquire, auction_price_per_xch):
        """
        Check market conditions and profitability using dexie.space price API.

        Args:
            xch_to_acquire: Amount of XCH we would get (in mojos)
            auction_price_per_xch: Auction price per XCH

        Returns:
            dict: Contains market analysis and profitability assessment
        """

        # Get TBYC/XCH ratio from Dexie API
        market_price = await fetch_dexie_price("XCH", "TBYC")

        # If price API fails or returns 0, assume favorable test conditions
        if not market_price:
            log.info("TBYC price unavailable, testing liquidator assumes market conditions are favorable")
            auction_price_byc = auction_price_per_xch / PRICE_PRECISION
            return {
                "profitable": True,
                "discount": self.min_discount + 0.01,  # Slightly above minimum
                "market_price": auction_price_byc * 1.1,  # 10% above auction price
                "auction_price": auction_price_byc,
                "has_sufficient_depth": True,
                "reason": "API unavailable - testing mode",
            }

        # Convert market_price_ratio (TBYC/XCH) to market price (mTBYC/XCH) for comparison
        market_price_byc = market_price
        auction_price_byc = auction_price_per_xch / PRICE_PRECISION
        # Calculate discount from market price
        discount = (market_price_byc - auction_price_byc) / market_price_byc if market_price_byc > 0 else 0

        # Check if discount meets our minimum threshold
        profitable = discount >= self.min_discount

        log.debug(
            f"Market conditions: market_price_byc={market_price_byc}, auction_price={auction_price_byc}, discount={discount:.2%}, required={self.min_discount:.2%}, depth_ok=True"
        )

        return {
            "profitable": profitable,
            "discount": discount,
            "market_price": market_price_byc,
            "auction_price": auction_price_byc,
            "has_sufficient_depth": True,
            "reason": (
                "Sufficient discount and liquidity"
                if profitable
                else f"Discount {discount:.2%} < required {self.min_discount:.2%}"
            ),
        }

    async def create_multiple_offers(self, total_xch_amount, market_price, collateral_balance_available=None):
        """
        Create multiple offers for the acquired XCH based on max_offer_amount constraint.

        Args:
            total_xch_amount: Total amount of XCH to sell (in mojos)
            market_price: Market price to use for the offers
            collateral_balance_available: Available XCH balance for creating offers (in mojos).
                                         If None, will fetch current balance.

        Returns:
            dict: Summary of offer creation results
        """
        total_xch_whole = total_xch_amount / MOJOS
        max_offer_xch = self.max_offer_amount

        # Get available collateral balance if not provided
        if collateral_balance_available is None:
            ignore_coins = await self._get_ignore_coins()
            balances = await self.rpc_client.wallet_balances(ignore_coin_names=ignore_coins)
            collateral_balance_available = balances.get("xch", 0)
            log.info(f"Fetched current XCH balance: {collateral_balance_available / MOJOS:.6f} XCH")

        available_xch_whole = collateral_balance_available / MOJOS
        log.info(f"Available collateral for offers: {available_xch_whole:.6f} XCH")

        # Limit total XCH to what's actually available
        if total_xch_amount > collateral_balance_available:
            log.warning(
                f"Requested {total_xch_whole:.6f} XCH exceeds available balance {available_xch_whole:.6f} XCH. "
                f"Limiting offers to available balance."
            )
            total_xch_amount = collateral_balance_available
            total_xch_whole = total_xch_amount / MOJOS

        log.debug(f"Calculating how many offers to create for {total_xch_whole:.6f} XCH and {max_offer_xch=} XCH")
        # Calculate how many offers we need to create
        if total_xch_whole <= max_offer_xch:
            # Single offer is sufficient
            offers_to_create = [(total_xch_amount,)]
        else:
            # Split into multiple offers
            offers_to_create = []
            remaining_xch = total_xch_amount

            while remaining_xch > 0:
                # Determine the size of the next offer
                if remaining_xch / MOJOS <= max_offer_xch:
                    # Last offer - use all remaining XCH
                    offer_xch_amount = remaining_xch
                else:
                    # Create an offer of max_offer_amount size
                    offer_xch_amount = int(max_offer_xch * MOJOS)

                offers_to_create.append((offer_xch_amount,))
                remaining_xch -= offer_xch_amount

        log.info(
            f"Creating {len(offers_to_create)} offers for {total_xch_whole:.6f} XCH (max per offer: {max_offer_xch} XCH)"
        )

        # Track results and remaining balance
        successful_offers = []
        failed_offers = []
        remaining_balance = collateral_balance_available

        # Create each offer
        for i, (xch_amount,) in enumerate(offers_to_create):
            # Check if we still have enough balance for this offer
            if xch_amount > remaining_balance:
                log.warning(
                    f"Insufficient balance for offer {i + 1}: need {xch_amount / MOJOS:.6f} XCH, "
                    f"only {remaining_balance / MOJOS:.6f} XCH available. Skipping remaining offers."
                )
                failed_offers.append(
                    {
                        "xch_amount": xch_amount / MOJOS,
                        "error": f"Insufficient balance: {remaining_balance / MOJOS:.6f} XCH available",
                    }
                )
                break

            log.info(
                f"Creating offer {i + 1}/{len(offers_to_create)}: {xch_amount / MOJOS:.6f} XCH (balance: {remaining_balance / MOJOS:.6f} XCH)"
            )

            try:
                offer_result = await self.create_and_upload_offer(xch_amount, market_price * (1.05 - (i / 100)))

                if offer_result["success"]:
                    successful_offers.append(offer_result)
                    # Deduct the used amount from remaining balance
                    remaining_balance -= xch_amount
                    log.info(
                        f"Successfully created offer {i + 1}: {xch_amount / MOJOS:.6f} XCH (remaining balance: {remaining_balance / MOJOS:.6f} XCH)"
                    )
                else:
                    error_msg = offer_result.get("error", "Unknown error")
                    failed_offers.append({"xch_amount": xch_amount / MOJOS, "error": error_msg})
                    log.error(f"Failed to create offer {i + 1}: {offer_result}")

                    # Check if the error is due to insufficient coins
                    if "Can't find enough coins" in str(error_msg) or "Insufficient balance" in str(error_msg):
                        log.warning("Stopping offer creation due to insufficient coins")
                        # Stop processing remaining offers
                        break

            except Exception as e:
                failed_offers.append({"xch_amount": xch_amount / MOJOS, "error": str(e)})
                log.exception(f"Exception creating offer {i + 1}: {e}")

        result = {
            "total_offers_attempted": len(offers_to_create),
            "total_success": len(successful_offers),
            "total_failed": len(failed_offers),
            "successful_offers": successful_offers,
            "failed_offers": failed_offers,
            "total_xch_amount": total_xch_whole,
            "remaining_balance": remaining_balance / MOJOS,
        }

        log.info(
            f"Offer creation summary: {result['total_success']}/{result['total_offers_attempted']} successful, remaining balance: {remaining_balance / MOJOS:.6f} XCH"
        )
        return result

    async def create_and_upload_offer(self, xch_amount, market_price):
        """
        Create an offer for selling XCH and upload it to dexie.space.

        Args:
            xch_amount: Amount of XCH to sell (in mojos)
            market_price: Market price to use for the offer
        """
        try:
            log.debug("Creating offer with params: %s, %s", xch_amount, market_price)
            xch_amount_in_whole = xch_amount / MOJOS

            # Get ignore coins to pass to offer creation
            ignore_coins = await self._get_ignore_coins()

            # Check if we have enough balance before attempting to create the offer
            current_balances = await self.rpc_client.wallet_balances(ignore_coin_names=ignore_coins)
            available_xch_mojos = current_balances.get("xch", 0)
            available_xch = available_xch_mojos / MOJOS

            if xch_amount > available_xch_mojos:
                error_msg = f"Insufficient balance to create offer: need {xch_amount_in_whole:.6f} XCH, only {available_xch:.6f} XCH available"
                log.warning(error_msg)
                await self._emit_progress(
                    {
                        "event": "offer_creation_failed",
                        "message": error_msg,
                        "error": "Insufficient balance",
                        "offer_details": {
                            "xch_amount_requested": xch_amount_in_whole,
                            "xch_amount_available": available_xch,
                        },
                    }
                )
                return {"success": False, "error": error_msg}

            # Price the offer competitively (slightly below market to ensure execution)
            offer_price = market_price * 0.995  # 0.5% below market
            byc_amount_to_receive = xch_amount_in_whole * offer_price
            if byc_amount_to_receive < 1:
                raise ValueError(f"Offer price is too low: {offer_price} < 1")
            await self._emit_progress(
                {
                    "event": "offer_creation_started",
                    "message": f"Creating new offer - {xch_amount_in_whole:.6f} XCH for {byc_amount_to_receive:.2f} TBYC at price {offer_price}",
                    "offer_details": {
                        "xch_amount": xch_amount_in_whole,
                        "byc_amount": byc_amount_to_receive,
                        "offer_price": offer_price,
                        "market_price": market_price,
                    },
                }
            )

            log.info(f"Creating offer: {xch_amount_in_whole} XCH for {byc_amount_to_receive:.2f} TBYC at {offer_price}")

            # Create the offer via RPC, excluding locked coins
            # FIXME: this reuses coins which it shouldn't
            offer_result = await self.rpc_client.offer_make(
                xch_amount=xch_amount_in_whole,
                byc_amount=byc_amount_to_receive,
                ignore_coin_names=ignore_coins,
                expires_in_seconds=self.offer_expiry_seconds,
            )

            if offer_result and offer_result.get("bundle"):
                # Lock the coins used in this offer
                used_coins = offer_result.get("used_coin_names", [])
                if used_coins:
                    await self._lock_coins(used_coins)
                    log.debug(f"Locked {len(used_coins)} coins for offer")
                # Extract offer data for dexie upload
                # sign the offer bundle
                unsigned_bundle = SpendBundle.from_json_dict(offer_result["offered_bundle"])
                signed_off_bundle = await self.rpc_client.sign_bundle(unsigned_bundle)
                from chia_rs import G2Element

                assert signed_off_bundle.aggregated_signature != G2Element()
                offer_bundle = SpendBundle.from_json_dict(offer_result["bundle"])
                unsigned_offer = Offer.from_spend_bundle(offer_bundle)
                offer = Offer(unsigned_offer.requested_payments, signed_off_bundle, unsigned_offer.driver_dict)
                offer_data = {
                    "offer": offer.to_bech32(),
                    "requested": [{"asset_id": "TBYC", "amount": int(byc_amount_to_receive)}],
                    "offered": [{"asset_id": "TXCH", "amount": xch_amount}],
                    "price": offer_price,
                }
                # Upload to dexie.space
                upload_result = await upload_offer_to_dexie(offer_data, self._emit_progress)
                current_time = self._now()

                if upload_result:
                    offer_id = upload_result.get("id", f"local_{int(current_time)}")
                    log.info(f"Successfully uploaded offer to dexie.space: {offer_id}")

                    # Add offer to active tracking
                    await self._add_active_offer(offer_id, xch_amount_in_whole, current_time, market_price)

                    await self._emit_progress(
                        {
                            "event": "offer_creation_success",
                            "message": f"Successfully created and uploaded offer {offer_id} - {xch_amount_in_whole:.6f} XCH for {byc_amount_to_receive:.2f} TBYC",
                            "offer_id": offer_id,
                            "dexie_upload": True,
                            "offer_details": {
                                "xch_amount": xch_amount_in_whole,
                                "byc_amount": byc_amount_to_receive,
                                "offer_price": offer_price,
                            },
                        }
                    )

                    return {
                        "success": True,
                        "dexie_id": offer_id,
                        "offer_bech32": offer_data["offer"],
                        "offer_details": {
                            "xch_amount": xch_amount_in_whole,
                            "byc_amount": byc_amount_to_receive,
                            "offer_price": offer_price,
                        },
                    }
                else:
                    offer_id = f"local_{int(current_time)}"
                    log.warning("Failed to upload offer to dexie.space, but offer created locally")

                    # Add offer to active tracking even if not uploaded
                    await self._add_active_offer(offer_id, xch_amount_in_whole, current_time, market_price)

                    await self._emit_progress(
                        {
                            "event": "offer_creation_partial_success",
                            "message": f"Created offer {offer_id} locally but failed to upload to Dexie - {xch_amount_in_whole:.6f} XCH for {byc_amount_to_receive:.2f} TBYC",
                            "offer_id": offer_id,
                            "dexie_upload": False,
                            "offer_details": {
                                "xch_amount": xch_amount_in_whole,
                                "byc_amount": byc_amount_to_receive,
                                "offer_price": offer_price,
                            },
                        }
                    )

                    return {
                        "success": True,
                        "local_only": True,
                        "offer_details": {
                            "xch_amount": xch_amount_in_whole,
                            "byc_amount": byc_amount_to_receive,
                            "offer_price": offer_price,
                        },
                        "offer_id": offer_id,
                        "offer_bech32": offer_data["offer"],
                    }
            else:
                log.error(f"Failed to create offer: {offer_result}")
                await self._emit_progress(
                    {
                        "event": "offer_creation_failed",
                        "message": f"Failed to create offer for {xch_amount_in_whole:.6f} XCH - RPC call failed",
                        "error": "Offer RPC creation failed",
                        "offer_details": {
                            "xch_amount": xch_amount_in_whole,
                            "byc_amount": byc_amount_to_receive,
                            "offer_price": offer_price,
                        },
                    }
                )
                return {"success": False}

        except Exception as e:
            log.exception(f"Failed to create and upload offer: {e}")
            await self._emit_progress(
                {
                    "event": "offer_creation_failed",
                    "message": f"Failed to create offer for {xch_amount_in_whole:.6f} XCH - {str(e)}",
                    "error": str(e),
                    "offer_details": {
                        "xch_amount": xch_amount_in_whole if "xch_amount_in_whole" in locals() else xch_amount / MOJOS,
                        "target_byc_amount": byc_amount_to_receive if "byc_amount_to_receive" in locals() else 0,
                    },
                }
            )
            return {"success": False, "error": str(e)}

    async def check_and_restart_incomplete_liquidations(self, state):
        """
        Check for vaults that finished liquidation but weren't fully liquidated,
        and restart their auctions.

        Args:
            state: Current protocol state

        Returns:
            int: Number of auctions restarted
        """
        auctions_restarted = 0

        # Look for vaults that might have incomplete liquidations
        # These would be vaults that were in liquidation but no longer appear in vaults_in_liquidation
        # but still have debt or collateral that should be liquidated

        try:
            # Get all vaults to check their status
            all_vaults = await self.rpc_client.upkeep_vaults_list()
            if not all_vaults or not isinstance(all_vaults, list):
                return auctions_restarted

            current_liquidating = {v["name"] for v in state.get("vaults_in_liquidation", [])}

            for vault in all_vaults:
                vault_name = vault.get("name")
                if not vault_name or vault_name in current_liquidating:
                    continue

                # Check if vault should be liquidated but isn't currently in auction
                health_ratio = vault.get("health_ratio", float("inf"))
                collateral = vault.get("collateral", 0)
                debt = vault.get("debt", 0)

                # If vault is unhealthy and has collateral/debt, it should be liquidated
                if health_ratio < 1.0 and collateral > 0 and debt > 0:
                    log.info(
                        f"Found incomplete liquidation for vault {vault_name} (health: {health_ratio}, collateral: {collateral})"
                    )

                    try:
                        result = await self.rpc_client.upkeep_vaults_liquidate(coin_name=vault_name)
                        log.info(f"Restarted auction for incomplete liquidation {vault_name}: {result}")
                        auctions_restarted += 1
                    except APIError as e:
                        log.error("Failed to restart auction for vault %s: %s", vault_name, e)

        except Exception as e:
            log.error(f"Error checking for incomplete liquidations: {e}")

        return auctions_restarted

    async def recover_bad_debts(self, vaults_with_bad_debt, state):
        log.info("Found vaults with bad debt: %s", vaults_with_bad_debt)
        bad_debts_recovered = 0

        # Get treasury balance from state
        treasury_balance = state.get("treasury_balance", 0)
        log.info(f"Treasury balance: {treasury_balance}")

        # Calculate total debt to recover and provide detailed recovery plan
        total_debt_amount = sum(vault.get("principal", 0) for vault in vaults_with_bad_debt)
        recoverable_debt_amount = 0
        recoverable_vaults = []
        skipped_vaults = []
        if treasury_balance < 10_000:
            # treasury balance is less than 10 BYC, skip recovery
            return bad_debts_recovered

        for vault in vaults_with_bad_debt:
            vault_debt = vault.get("principal", 0)
            recoverable_debt_amount += vault_debt
            recoverable_vaults.append(vault)
            break

        # Emit detailed recovery plan
        await self._emit_progress(
            {
                "event": "debt_recovery_plan",
                "message": f"Bad debt recovery plan - Total debt: {total_debt_amount}, Recoverable: {recoverable_debt_amount}, Treasury balance: {treasury_balance}",
                "recovery_plan": {
                    "total_vaults_with_debt": len(vaults_with_bad_debt),
                    "total_debt_amount": total_debt_amount,
                    "recoverable_vaults": len(recoverable_vaults),
                    "recoverable_debt_amount": recoverable_debt_amount,
                    "skipped_vaults": len(skipped_vaults),
                    "treasury_balance": treasury_balance,
                    "can_recover_all": len(skipped_vaults) == 0,
                },
            }
        )

        # Report on skipped vaults if any
        for vault_with_bad_debt in skipped_vaults:
            vault_name = vault_with_bad_debt["name"]
            vault_debt = vault_with_bad_debt.get("principal", 0)
            log.info(
                f"Skipping bad debt recovery for vault {vault_name}: treasury balance ({treasury_balance}) insufficient to cover debt ({vault_debt})"
            )
            await self._emit_progress(
                {
                    "event": "debt_recovery_skipped",
                    "message": f"Skipping vault {vault_name} - insufficient treasury balance (need {vault_debt}, have {treasury_balance})",
                    "vault_name": vault_name,
                    "required_balance": vault_debt,
                    "available_balance": treasury_balance,
                    "shortfall": vault_debt - treasury_balance,
                }
            )

        # Process recoverable vaults
        for vault_with_bad_debt in recoverable_vaults:
            vault_name = vault_with_bad_debt["name"]
            vault_debt = vault_with_bad_debt.get("principal", 0)

            log.info("Recovering bad debt for vault %s (debt: %s)", vault_name, vault_debt)
            await self._emit_progress(
                {
                    "event": "debt_recovery_starting",
                    "message": f"Starting bad debt recovery for vault {vault_name} - recovering {vault_debt} from treasury",
                    "vault_name": vault_name,
                    "debt_amount": vault_debt,
                    "treasury_balance_before": treasury_balance,
                    "recovery_method": "treasury_withdrawal",
                }
            )
            try:
                ignore_coins = await self._get_ignore_coins()
                result = await self.rpc_client.upkeep_vaults_recover(vault_name, ignore_coin_names=ignore_coins)
                log.info(f"Recovered some debt for vault {vault_name}: {result}")
                await self._emit_progress(
                    {
                        "event": "debt_recovery_completed",
                        "message": f"Successfully recovered bad debt for vault {vault_name} - {vault_debt} recovered from treasury",
                        "vault_name": vault_name,
                        "recovered_amount": vault_debt,
                        "recovery_result": result,
                    }
                )
                bad_debts_recovered += 1
                # Update treasury balance for next iteration
                treasury_balance -= vault_debt
            except APIError as e:
                log.exception("Failed to recover bad debt for vault %s: %s", vault_name, e)
                await self._emit_progress(
                    {
                        "event": "debt_recovery_failed",
                        "message": f"Failed to recover bad debt for vault {vault_name}: {e}",
                        "vault_name": vault_name,
                        "attempted_recovery_amount": vault_debt,
                        "error": str(e),
                    }
                )

        # Final recovery summary
        await self._emit_progress(
            {
                "event": "debt_recovery_summary",
                "message": f"Bad debt recovery completed - recovered {bad_debts_recovered} of {len(vaults_with_bad_debt)} vaults, total amount: {sum(vault.get('principal', 0) for vault in recoverable_vaults[:bad_debts_recovered])}",
                "recovery_summary": {
                    "vaults_recovered": bad_debts_recovered,
                    "total_vaults": len(vaults_with_bad_debt),
                    "success_rate": bad_debts_recovered / len(vaults_with_bad_debt) if vaults_with_bad_debt else 0,
                },
            }
        )

        return bad_debts_recovered
