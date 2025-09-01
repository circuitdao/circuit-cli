import asyncio
import json
import random
import time
import statistics
import aiohttp
import math
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"
# We will now use multiple pairs to construct the XCH-USD price
PAIRS = ["XCH-USDT", "XCH-USD"]
# Subscribe to trades and order book for all pairs
TRADE_CH = {"op": "subscribe", "args": [{"channel": "trades", "instId": p} for p in PAIRS]}
BOOK_CH = {"op": "subscribe", "args": [{"channel": "books5", "instId": p} for p in PAIRS]}

# Coinbase for USDT-USD conversion
# Using Coinbase v2 public API for spot prices, as v3 requires authentication
COINBASE_API_URL = "https://api.coinbase.com/v2"
USDT_USD_PAIR = "USDT-USD"


class Oracle:
    def __init__(self, window_sec=5, min_notional=10):
        self.window = window_sec
        self.min_notional = min_notional
        self.trades = []  # list of (ts_ms, px, qty)
        self.last_pub = 0
        self.last_trade_ts = 0
        self.last_price = float("nan")
        self.usdt_usd_price = float("nan")
        self.subscribers = []  # List of asyncio.Queue for event subscribers

    def set_usdt_usd_price(self, price):
        if price and price > 0:
            self.usdt_usd_price = price

    def add_trade(self, instId, ts_ms, px, qty):
        # Convert price to USD if necessary
        usd_px = px
        if instId.endswith("-USDT"):
            if not math.isnan(self.usdt_usd_price):
                usd_px = px * self.usdt_usd_price
            else:
                # Can't convert, so we skip this trade
                return

        if usd_px * qty < self.min_notional:
            return
        now = int(time.time() * 1000)
        self.trades.append((ts_ms, usd_px, qty))
        # drop old
        cutoff = now - self.window * 1000
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.pop(0)
        self.last_trade_ts = ts_ms

    def compute(self, fallback_mid=None):
        now = int(time.time() * 1000)
        # stale?
        stale = (now - self.last_trade_ts) > 5000
        price = None
        meta = {"stale": stale, "window": self.window, "trades": len(self.trades)}
        if self.trades:
            vol = sum(q for _, _, q in self.trades)
            if vol > 0:
                vwap = sum(px * q for _, px, q in self.trades) / vol
                # outlier trim (simple): drop if far from median of trade prices
                med = statistics.median([px for _, px, _ in self.trades])
                if abs(vwap - med) / med > 0.03 and len(self.trades) > 4:
                    # trim extremes
                    prices = sorted([px for _, px, _ in self.trades])
                    core = prices[len(prices) // 10 : -len(prices) // 10 or None]
                    vwap = sum(p * q for (_, p, q) in self.trades if p in core) / sum(
                        q for (_, p, q) in self.trades if p in core
                    )
                price = vwap
        if price is None:
            price = fallback_mid if fallback_mid is not None else self.last_price
            meta["degraded"] = True
        self.last_price = price
        meta["ts"] = now
        return price, meta

    def subscribe(self):
        """Allows a consumer to subscribe to price updates.

        Returns an asyncio.Queue that will receive price update events.
        """
        q = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """Removes a subscriber queue."""
        try:
            self.subscribers.remove(q)
        except ValueError:
            # This can happen if a task is cancelled and tries to unsubscribe
            # after the list has already been cleared, for example.
            pass

    async def publish_update(self, price, meta):
        """Publishes the latest price update to all subscribers."""
        event = {
            "type": "price_update",
            "price": price,
            "meta": meta,
            "usdt_usd_price": self.usdt_usd_price,
        }
        for q in self.subscribers:
            await q.put(event)


async def usdt_price_fetcher(oracle: Oracle):
    """Fetches USDT-USD price from Coinbase and updates the oracle."""
    base_delay = 5.0
    max_delay = 60.0
    delay = base_delay

    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"{COINBASE_API_URL}/prices/{USDT_USD_PAIR}/spot"
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        price = float(data["data"]["amount"])
                        oracle.set_usdt_usd_price(price)
                        delay = base_delay  # Reset backoff delay on success
                        await asyncio.sleep(15)  # Wait for the normal interval
                        continue

                    # Handle non-200 server responses
                    body = await response.text()
                    logging.warning(
                        "Failed to fetch USDT-USD price from Coinbase (Status: %s). Retrying in %.2fs. Body: %s",
                        response.status,
                        delay,
                        body,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.error("Error fetching USDT-USD price: %s. Retrying in %.2fs.", e, delay)

        await asyncio.sleep(delay)
        delay = min(max_delay, delay * 1.5 + random.uniform(0, 1))  # Exponential backoff with jitter


async def okx_ws(oracle: Oracle):
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(OKX_WS, heartbeat=15) as ws:
                    await ws.send_json(TRADE_CH)
                    await ws.send_json(BOOK_CH)
                    book_mids = {}
                    logging.info("Connected to OKX WebSocket and subscribed to channels.")
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        if "event" in data:
                            if data["event"] == "subscribe":
                                logging.info("Successfully subscribed to %s", data["arg"])
                            elif data["event"] == "error":
                                logging.error("Subscription error: %s", data["msg"])
                            continue
                        if "arg" not in data:
                            continue

                        ch = data["arg"]["channel"]
                        instId = data["arg"]["instId"]

                        if ch == "trades" and "data" in data:
                            for t in data["data"]:
                                px = float(t["px"])
                                sz = float(t["sz"])
                                ts = int(t["ts"])
                                oracle.add_trade(instId, ts, px, sz)
                        elif ch == "books5":
                            if data.get("data"):
                                d = data["data"][0]
                                bids = d.get("bids")
                                asks = d.get("asks")
                                if bids and asks and len(bids) > 0 and len(asks) > 0:
                                    best_bid = float(bids[0][0])
                                    best_ask = float(asks[0][0])
                                    mid = (best_bid + best_ask) / 2.0

                                    # Use volume at the top of the book for weighting
                                    top_bid_vol = float(bids[0][1])
                                    top_ask_vol = float(asks[0][1])
                                    book_top_vol = top_bid_vol + top_ask_vol

                                    if instId.endswith("-USDT"):
                                        if not math.isnan(oracle.usdt_usd_price):
                                            book_mids[instId] = (
                                                mid * oracle.usdt_usd_price,
                                                book_top_vol,
                                            )
                                        elif instId in book_mids:
                                            # remove stale price if conversion not possible
                                            del book_mids[instId]
                                    else:  # -USD
                                        book_mids[instId] = (mid, book_top_vol)
                        # publish every 1s
                        now = time.time()
                        if now - oracle.last_pub >= 1.0:
                            fallback_mid = None
                            if book_mids:
                                # Calculate a volume-weighted mid-price for fallback
                                total_book_volume = sum(vol for _, vol in book_mids.values())
                                if total_book_volume > 0:
                                    weighted_sum = sum(px * vol for px, vol in book_mids.values())
                                    fallback_mid = weighted_sum / total_book_volume
                                else:
                                    # Fallback to simple average if no volume
                                    prices = [px for px, _ in book_mids.values()]
                                    if prices:
                                        fallback_mid = statistics.mean(prices)
                            price, meta = oracle.compute(fallback_mid=fallback_mid)

                            # Publish event to any subscribers
                            await oracle.publish_update(price, meta)

                            price_str = (
                                f"{price:.2f}" if isinstance(price, (int, float)) and not math.isnan(price) else "None"
                            )
                            usdt_price_str = (
                                f"{oracle.usdt_usd_price:.4f}" if not math.isnan(oracle.usdt_usd_price) else "None"
                            )
                            logging.info(
                                "XCH-USD oracle=%s trades=%d meta=%s | USDT-USD=%s",
                                price_str,
                                meta["trades"],
                                meta,
                                usdt_price_str,
                            )
                            oracle.last_pub = now
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.error("OKX WebSocket connection error: %s. Reconnecting...", e)
            await asyncio.sleep(2 + 3 * random.random())


async def price_subscriber(oracle: Oracle):
    """An example task that subscribes to and prints price updates."""
    q = oracle.subscribe()
    logging.info("[Subscriber] Task started, waiting for price updates.")
    try:
        while True:
            update = await q.get()
            price = update.get("price")
            price_str = f"{price:.2f}" if isinstance(price, (int, float)) and not math.isnan(price) else "None"
            logging.info("[Subscriber] Received price update: %s", price_str)
            q.task_done()  # Acknowledge the item has been processed
    except asyncio.CancelledError:
        logging.info("[Subscriber] Task cancelled.")
    finally:
        oracle.unsubscribe(q)
        logging.info("[Subscriber] Unsubscribed.")


async def main():
    oracle = Oracle()
    # Create a subscriber task to demonstrate event handling
    subscriber_task = asyncio.create_task(price_subscriber(oracle))

    core_tasks = asyncio.gather(okx_ws(oracle), usdt_price_fetcher(oracle))

    try:
        await core_tasks
    finally:
        # On exit, ensure the subscriber task is cancelled and cleaned up
        subscriber_task.cancel()
        await asyncio.gather(subscriber_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\nExiting.")
