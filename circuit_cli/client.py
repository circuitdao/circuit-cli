import asyncio
from math import floor

import httpx
from chia.types.blockchain_format.coin import Coin
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import encode_puzzle_hash
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    puzzle_hash_for_synthetic_public_key,
)
from chia_rs import PrivateKey

from circuit_cli.utils import generate_ssks, sign_spends

MOJOS = 10**12
MBYC = 10**3

from pprint import pprint

class CircuitRPCClient:
    # TODO: add support for fees across all methods
    def __init__(self, base_url: str, private_key: str, add_sig_data: str = None, fee_per_cost: int = 0):
        if private_key:
            secret_key = PrivateKey.from_bytes(bytes.fromhex(private_key))
            synthetic_secret_keys = generate_ssks(secret_key, 0, 500)
            synthetic_public_keys = [x.get_g1() for x in synthetic_secret_keys]
        else:
            synthetic_secret_keys = []
            synthetic_public_keys = []
        self.synthetic_secret_keys = synthetic_secret_keys
        self.synthetic_public_keys = synthetic_public_keys
        #print([encode_puzzle_hash(puzzle_hash_for_synthetic_public_key(x), "txch") for x in synthetic_public_keys[:5]])
        self.base_url = base_url
        self.client = httpx.Client(base_url=base_url, timeout=120)
        self.add_sig_data = add_sig_data
        self.fee_per_cost = fee_per_cost

    async def wait_for_confirmation(self, bundle: SpendBundle = None, blocks=None):
        if bundle is not None and isinstance(bundle, SpendBundle):
            while True:
                response = self.client.post("/transactions/status", json={"bundle": bundle.to_json_dict()})
                print(response.content)
                if response.status_code != 200:
                    response.raise_for_status()
                data = response.json()
                if data["status"] == "confirmed":
                    return True
                elif data["status"] == "failed":
                    raise ValueError("Transaction failed")
                print("Still waiting for confirmation...")
                await asyncio.sleep(5)
        elif blocks is not None:
            await asyncio.sleep(blocks * 55)
        else:
            raise ValueError("Either bundle or blocks must be provided")

    async def sign_and_push(self, bundle: SpendBundle):
        #print("USING ADDITIONAL SIGNATURE DATA", self.add_sig_data)
        signed_bundle = await sign_spends(
            bundle.coin_spends,
            self.synthetic_secret_keys,
            add_data=self.add_sig_data,
        )
        assert isinstance(signed_bundle, SpendBundle)
        response = self.client.post(
            "/sign_and_push",
            json={
                "bundle_dict": signed_bundle.to_json_dict(),
                "signature": signed_bundle.aggregated_signature.to_bytes().hex(),
            },
            #timeout=30, # LATER: needed?
        )
        json_resp = response.json()
        #print("Got response from sign and push", response.status_code, json_resp)
        if response.status_code != 200:
            print("Error from sign_and_push:", response.content)
            response.raise_for_status()
        #print("Returning signed bundle")
        return json_resp

    async def wallet_balances(self):
        response = self.client.post(
            "/balances", json={"synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys]}
        )
        return response.json()

    async def wallet_coins(self):
        response = self.client.post(
            "/coins", json={"synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys]}
        )
        return response.json()

    async def vault_show(self):
        response = self.client.post(
            "/vault",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
            },
        )
        return response.json()

    async def vault_deposit(self, AMOUNT: float):
        response = self.client.post(
            "/vault/deposit",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "amount": floor(AMOUNT * MOJOS),
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json()["bundle"])
        sig_response = await self.sign_and_push(bundle)
        return sig_response

    async def vault_withdraw(self, AMOUNT: float):
        response = self.client.post(
            "/vault/withdraw",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "amount": floor(AMOUNT * MOJOS),
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json()["bundle"])
        sig_response = await self.sign_and_push(bundle)
        return sig_response

    async def vault_borrow(self, AMOUNT: float):
        response = self.client.post(
            "/vault/borrow",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "amount": floor(AMOUNT * MBYC),
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json()["bundle"])
        sig_response = await self.sign_and_push(bundle)
        return sig_response

    async def vault_repay(self, AMOUNT: float):
        response = self.client.post(
            "/vault/repay",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "amount": floor(AMOUNT * MBYC),
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json()["bundle"])
        sig_response = await self.sign_and_push(bundle)
        return sig_response


    async def savings_show(self):
        response = self.client.post(
            "/savings",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
            },
        )
        return response.json()


    async def savings_deposit(self, AMOUNT: float):
        response = self.client.post(
            "/savings/deposit",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "amount": floor(AMOUNT * MBYC),
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json()["bundle"])
        sig_response = await self.sign_and_push(bundle)
        return sig_response


    async def savings_withdraw(self, AMOUNT: float):
        response = self.client.post(
            "/savings/withdraw",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "amount": floor(AMOUNT * MBYC),
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json()["bundle"])
        sig_response = await self.sign_and_push(bundle)
        return sig_response


    async def announcer_list(self, all=False, incl_spent=False):
        if all:
            response = self.client.post(
                "/announcers/",
                json={
                    "synthetic_pks": [],
                    "include_spent_coins": incl_spent,
                },
            )
        else:
            response = self.client.post(
                "/announcers/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "include_spent_coins": incl_spent,
                },
            )
        return response.json()

    async def announcer_launch(self, price):
        response = self.client.post(
            "/announcers/launch",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "operation": "launch",
                "args": {"price": price},
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle_json = response.json()
        bundle: SpendBundle = SpendBundle.from_json_dict(bundle_json)
        sig_response = await self.sign_and_push(bundle)
        signed_bundle = SpendBundle.from_json_dict(sig_response["bundle"])
        await self.wait_for_confirmation(signed_bundle)
        return sig_response

    async def announcer_configure(self, coin_name, amount=None, inner_puzzle_hash=None, price=None, ttl=None):
        if not coin_name:
            response = self.client.post(
                "/announcers/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                },
            )
            data = response.json()
            assert len(data) > 0, "No announcer found"
            assert len(data) == 1, "More than one announcer found. Use --coin-name to specify one"
            coin_name = data[0]["name"]
        else:
            coin_name = coin_name

        response = self.client.post(
            "/announcers/" + coin_name,
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "operation": "configure",
                "args": {
                    "new_amount": amount,
                    "new_inner_puzzle_hash": inner_puzzle_hash,
                    "new_price": price,
                    "new_ttl": ttl,
                },
                "fee_per_cost": self.fee_per_cost,
            },
        )
        data = response.json()
        #print("Got bundle, signing and pushing", data)
        bundle: SpendBundle = SpendBundle.from_json_dict(data)
        sig_response = await self.sign_and_push(bundle)
        return sig_response

    async def announcer_update(self, coin_name, PRICE):
        if not coin_name:
            response = self.client.post(
                "/announcers/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                },
            )
            data = response.json()
            assert len(data) > 0, "No announcer found"
            assert len(data) == 1, "More than one announcer found. Use --coin-name to specify one"
            coin_name = data[0]["name"]
        else:
            coin_name = coin_name

        response = self.client.post(
            "/announcers/" + coin_name,
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "operation": "mutate",
                "args": {
                    "new_price": PRICE,
                },
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle: SpendBundle = SpendBundle.from_json_dict(response.json())
        sig_response = await self.sign_and_push(bundle)
        return sig_response

    async def upkeep_sync(self):
        response = self.client.post("/sync_chain_data")
        return response.json()

    async def upkeep_vaults(self):
        response = self.client.get("/vaults")
        return response.json()

    async def upkeep_transfer_sf(self, vault_id):
        response = self.client.post(
            "/vaults/transfer_stability_fees",
            json={
                "vault_name": vault_id,
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "fee_per_cost": self.fee_per_cost,
            },
            headers={"Content-Type": "application/json"},
        )
        if response.is_error:
            print(response.content)
            response.raise_for_status()
        bundle = response.json()
        if bundle.get("detail"):
            return bundle
        print("Got bundle, signing and pushing", bundle)
        signed_bundle_json = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
        await self.wait_for_confirmation(SpendBundle.from_json_dict(signed_bundle_json["bundle"]))
        return {"status": "confirmed"}

    async def bills_list(self, all=False, empty_only=False, incl_spent=False):
        if all:
            response = self.client.post(
                "/bills",
                json={
                    "synthetic_pks": [],
                    "include_spent_coins": incl_spent,
                    "empty_only": empty_only,
                },
            )
        else:
            response = self.client.post(
                "/bills",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "include_spent_coins": incl_spent,
                    "empty_only": empty_only,
                },
                headers={"Content-Type": "application/json"},
            )
        return response.json()

    async def bills_toggle(self, COIN_NAME: str):
        response = self.client.post(
            "/coins/toggle_governance",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "coin_name": COIN_NAME,
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle = response.json()
        sig_response = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
        return sig_response

    async def bills_reset(self, COIN_NAME: str):
        response = self.client.post(
            "/bills/reset",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "coin_name": COIN_NAME,
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle = response.json()
        sig_response = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
        return sig_response


    async def bills_propose(
        self,
        COIN_NAME,
        value,
        threshold_amount_to_propose,
        veto_seconds,
        delay_seconds,
        max_delta,
        statute_index,
    ):
        response = self.client.post(
            "/bills/new",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "coin_name": COIN_NAME,
                "value": value,
                "threshold_amount_to_propose": threshold_amount_to_propose,
                "veto_seconds": veto_seconds,
                "delay_seconds": delay_seconds,
                "max_delta": max_delta,
                "statute_index": statute_index,
                "fee_per_cost": self.fee_per_cost,
            },
        )
        print("Got bundle, posting new bill")
        bundle = response.json()
        if response.status_code != 200:
            print(response.content)
            response.raise_for_status()
        sig_response = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
        return sig_response

    async def oracle_show(self):
        response = self.client.post(
            "/oracle/announce/",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
            },
        )
        data = response.json()
        coin = Coin.from_json_dict(data["bundle"]["coin_spends"][0]["coin"])
        data["name"] = coin.name().hex()
        del data["bundle"]
        return data

    async def oracle_update(self):
        response = self.client.post(
            "/oracle/",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "fee_per_cost": self.fee_per_cost,
            },
        )
        data = response.json()
        try:
            bundle = SpendBundle.from_json_dict(data)
            return await self.sign_and_push(bundle)
        except:
            print("Failed to update oracle", data)
            raise ValueError("Failed to update oracle")

    #async def protocol_statutes(self):
    #    response = self.client.get("/statutes")
    #    return response.json()

    async def statutes_list(self, full=False):
        response = self.client.post(
            "/statutes",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "full": full,
                #"fee_per_cost": self.fee_per_cost,
            },
        )
        data = response.json()
        return data

    async def statutes_update_price(self, *args):
        response = self.client.post(
            "/statutes/price/",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "fee_per_cost": self.fee_per_cost,
            },
        )
        try:
            data = response.json()
        except:
            error = response.content
            raise ValueError("Failed to parse response: %s" % error)
        try:
            bundle = SpendBundle.from_json_dict(data)
            return await self.sign_and_push(bundle)
        except:
            print("Failed to update statutes", data)
            raise ValueError("Failed to update statutes")

    async def statutes_announce(self, *args):
        response = self.client.post(
            "/statutes/announce/",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "fee_per_cost": self.fee_per_cost,
            },
        )
        try:
            data = response.json()
        except:
            error = response.content
            raise ValueError("Failed to parse response: %s" % error)
        try:
            bundle = SpendBundle.from_json_dict(data["bundle"])
            print("Announcing statutes", bundle)
            return await self.sign_and_push(bundle)
        except:
            print("Failed to announce statutes", data)
            raise ValueError("Failed to announce statutes")

    async def announcer_propose(self, COIN_NAME, approve, bill_name=None, no_bundle=True, enact=False):
        announcer_name = COIN_NAME
        #print("Enacting bill", enact, bill_name)
        if enact:
            bill_coin_name = bill_name
            bill_response = self.client.post(
                "/bills/enact",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "coin_name": bill_coin_name,
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            #print("Got bill, proposing announcer", bill_response.content)
            bundle_dict = bill_response.json()
            enact_bundle = SpendBundle.from_json_dict(bundle_dict)
            response = self.client.post(
                "/announcers/%s" % announcer_name,
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "operation": "govern",
                    "args": {
                        "toggle_activation": approve,
                        "enact_bundle": enact_bundle.to_json_dict(),
                    },
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            propose_result = response.json()
            #print("Got bundle, signing and pushing", propose_result)
            unsigned_bundle = SpendBundle.from_json_dict(propose_result["bundle"])
            resp_data = await self.sign_and_push(unsigned_bundle)
            return resp_data
        else:
            print("Proposing announcer", announcer_name)
            response = self.client.post(
                "/announcers/%s" % announcer_name,
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "operation": "govern",
                    "args": {"toggle_activation": approve, "no_bundle": no_bundle},
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            bundle = response.json()
            return bundle

    def close(self):
        self.client.close()
