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


    ### WALLET ###
    async def wallet_balances(self, human_readable=False):
        response = self.client.post(
            "/balances",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
            }
        )
        data = response.json()
        if human_readable:
            data["human_readable"] = True
        return data


    async def wallet_coins(self, type=""):
        coin_type = "" if type is None else type
        response = self.client.post(
            "/coins",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "coin_type": coin_type,
            }
        )
        return response.json()


    ### COLLATERAL VAULT ###
    async def vault_show(self, human_readable=False):
        response = self.client.post(
            "/vault",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
            },
        )
        data = response.json()
        if human_readable:
            data["human_readable"] = True
        return data


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


    ### SAVINGS VAULT ###
    async def savings_show(self, human_readable=False):
        response = self.client.post(
            "/savings",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
            },
        )
        data = response.json()
        if human_readable:
            data["human_readable"] = True
        return data


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


    ### ANNOUNCERS ###
    async def announcer_list(self, approved=False, all=False, valid=False, incl_spent=False, human_readable=False):
        if all:
            response = self.client.post(
                "/announcers/",
                json={
                    "synthetic_pks": [],
                    "approved": approved,
                    "valid_only": valid,
                    "include_spent_coins": incl_spent,
                },
            )
        else:
            response = self.client.post(
                "/announcers/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "approved": approved,
                    "valid_only": valid,
                    "include_spent_coins": incl_spent,
                },
            )
        data = response.json()
        if human_readable:
            for v in data:
                v["human_readable"] = True
        return data


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


    async def announcer_configure(self, COIN_NAME, deactivate=False, deposit=None, min_deposit=None, inner_puzzle_hash=None, price=None, ttl=None):
        if not COIN_NAME:
            response = self.client.post(
                "/announcers/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                },
            )
            data = response.json()
            assert len(data) > 0, "No announcer found"
            assert len(data) == 1, "More than one announcer found. Must provide COIN_NAME"
            coin_name = data[0]["name"]
        else:
            coin_name = COIN_NAME

        response = self.client.post(
            "/announcers/" + coin_name,
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "operation": "configure",
                "args": {
                    "deactivate": deactivate,
                    "new_deposit": deposit,
                    "new_min_deposit": min_deposit,
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


    async def announcer_exit(self, COIN_NAME):
        if not COIN_NAME:
            response = self.client.post(
                "/announcers/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                },
            )
            data = response.json()
            assert len(data) > 0, "No announcer found"
            assert len(data) == 1, "More than one announcer found. Must provide COIN_NAME"
            coin_name = data[0]["name"]
        else:
            coin_name = COIN_NAME

        response = self.client.post(
            "/announcers/" + coin_name,
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "operation": "exit",
                "args": {
                    "melt": True,
                },
                "fee_per_cost": self.fee_per_cost,
            },
        )
        data = response.json()
        #print("Got bundle, signing and pushing", data)
        bundle: SpendBundle = SpendBundle.from_json_dict(data)
        sig_response = await self.sign_and_push(bundle)
        return sig_response


    async def announcer_update(self, PRICE, COIN_NAME: str = None):
        if not COIN_NAME:
            response = self.client.post(
                "/announcers/",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                },
            )
            data = response.json()
            assert len(data) > 0, "No announcer found"
            assert len(data) == 1, "More than one announcer found. Must specify COIN_NAME"
            coin_name = data[0]["name"]
        else:
            coin_name = COIN_NAME

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


    async def announcer_govern(self, COIN_NAME, approve=False, disapprove=False, create_conditions=False, enact_bill_name=None):
        announcer_name = COIN_NAME
        # cannot approve and disapprove at the same time
        if (not approve and not disapprove) or (approve and disapprove):
            raise ValueError("Announcer must be either approved or disapproved")
        action = "approve" if approve else "disapprove"
        if create_conditions:
            assert enact_bill_name is None, "Cannot create custom conditions and enact bill at the same time"
            print(f"Generating custom conditions for bill to govern ({action}) announcer {announcer_name}")
            response = self.client.post(
                "/announcers/%s" % announcer_name,
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "operation": "govern",
                    "args": {"toggle_activation": approve, "enact_bundle": None},
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            bundle = response.json()
            return bundle
        else:
            assert enact_bill_name is not None, "Must specify bill to enact when not creating custom conditions"
            print(f"Enacting bill {enact_bill_name} to govern ({action}) announcer {announcer_name}")
            bill_coin_name = enact_bill_name
            bill_response = self.client.post(
                "/bills/enact",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "coin_name": bill_coin_name,
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            #print("Got bill", bill_response.content)
            print("Governing announcer", announcer_name)
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


    ### UPKEEP ###
    async def upkeep_info(self):
        response = self.client.get("/protocol/info")
        return response.json()

    async def upkeep_state(self):
        response = self.client.get("/protocol/state")
        return response.json()

    async def upkeep_rpc_sync(self):
        response = self.client.post("/sync_chain_data")
        return response.json()

    async def upkeep_rpc_version(self):
        response = self.client.get("/rpc/version")
        return response.json()

    ## Treasury ##
    async def upkeep_treasury_show(self, human_readable=False):
        response = self.client.post(
            "/treasury",
            json={
                "human_readable": human_readable,
            },
        )
        data = response.json()
        return data

    async def upkeep_treasury_rebalance(self, info=False):
        response = self.client.post(
            "/treasury",
            json={
                "human_readable": False,
            },
        )
        data = response.json()

        if info:
            return {"action_executable": data["can_rebalance"]}

        if not data["can_rebalance"]:
            return {"status": "failed", "error": "Rebalance threshold not reached"}

        response = self.client.post(
            "/treasury/rebalance",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "fee_per_cost": self.fee_per_cost,
            },
        )
        if response.is_error:
            print(response.content)
            response.raise_for_status()
        bundle = response.json()
        if bundle.get("detail"):
            return bundle
        #print("Got bundle, signing and pushing", bundle)
        signed_bundle_json = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
        await self.wait_for_confirmation(SpendBundle.from_json_dict(signed_bundle_json["bundle"]))
        return {"status": "confirmed"}


    async def upkeep_treasury_launch(self, SUCCESSOR_LAUNCHER_ID=None, create_conditions=False, enact_bill_name=None):

        if not SUCCESSOR_LAUNCHER_ID:
            response = self.client.post(
                "/treasury",
                json={
                    "human_readable": False
                },
            )
            data = response.json()
            successor_launcher_id = data["treasury_coins"][0]["launcher_id"]
        else:
            successor_launcher_id = SUCCESSOR_LAUNCHER_ID

        if create_conditions:
            assert enact_bill_name is None, "Cannot create custom conditions and enact bill at the same time"
            print(f"Generating custom conditions for bill to launch treasury coin with {successor_launcher_id} as successor launcher ID")
            response = self.client.post(
                "/treasury/launch",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "successor_launcher_id": successor_launcher_id,
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            bundle = response.json()
            return bundle
        else:
            assert enact_bill_name is not None, "Must specify bill to enact when not creating custom conditions"
            print(f"Enacting bill {enact_bill_name} to launch treasury coin with {successor_launcher_id} as successor launcher ID")
            bill_coin_name = enact_bill_name
            bill_response = self.client.post(
                "/bills/enact",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "coin_name": bill_coin_name,
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            #print("Got bill", bill_response.content)
            print("Launching terasury coin with successor launcher ID", successor_launcher_id)
            bundle_dict = bill_response.json()
            enact_bundle = SpendBundle.from_json_dict(bundle_dict)
            response = self.client.post(
                "/treasury/launch",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "enact_bundle": enact_bundle.to_json_dict(),
                    "fee_per_cost": self.fee_per_cost,
                },
            )
            propose_result = response.json()
            #print("Got bundle, signing and pushing", propose_result)
            unsigned_bundle = SpendBundle.from_json_dict(propose_result["bundle"])
            resp_data = await self.sign_and_push(unsigned_bundle)
            return resp_data


    ## Vaults ##
    async def upkeep_vaults_show(self, human_readable=False):
        response = self.client.get("/vaults")
        data = response.json()
        if human_readable:
            for v in data:
                v["human_readable"] = True
        return data

    async def upkeep_vaults_transfer(self, COIN_NAME):
        response = self.client.post(
            "/vaults/transfer_stability_fees",
            json={
                "vault_name": COIN_NAME,
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
        #print("Got bundle, signing and pushing", bundle)
        signed_bundle_json = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
        await self.wait_for_confirmation(SpendBundle.from_json_dict(signed_bundle_json["bundle"]))
        return {"status": "confirmed"}

    async def upkeep_vaults_auction(self, COIN_NAME: str, start=False, bid_amount=None):

        assert (not start and bid_amount is not None) or (start and bid_amount is None), "Cannot (re-)start auction and submit bid at same time"
        keeper_puzzle_hash = puzzle_hash_for_synthetic_public_key(self.synthetic_public_keys[0]).hex()

        if start:
            response = self.client.post(
                "/vaults/start_auction",
                json={
                    "synthetic_pks": [],
                    "vault_name": COIN_NAME,
                    "initiator_puzzle_hash": keeper_puzzle_hash,
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
            #print("Got bundle, signing and pushing", bundle)
            signed_bundle_json = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
            await self.wait_for_confirmation(SpendBundle.from_json_dict(signed_bundle_json["bundle"]))
            return {"status": "confirmed"}
        else:
            response = self.client.post(
                "/vaults/bid_auction",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "vault_name": COIN_NAME,
                    "amount": bid_amount,
                    "max_bid_price": None, # TODO: get from command line
                    "target_puzzle_hash": keeper_puzzle_hash,
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
            #print("Got bundle, signing and pushing", bundle)
            signed_bundle_json = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
            await self.wait_for_confirmation(SpendBundle.from_json_dict(signed_bundle_json["bundle"]))
            return {"status": "confirmed"}


    ### BILLS ###
    async def bills_list(self, all=False, empty_only=False, non_empty_only=False, human_readable=False, incl_spent=False):

        assert not (empty_only and  non_empty_only), "Cannot request both only empty and only non-empty governance coins"

        response = self.client.post(
            "/bills",
            json={
                "synthetic_pks": [] if all else [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "include_spent_coins": incl_spent,
                "empty_only": empty_only,
                "non_empty_only": non_empty_only,
                "human_readable": human_readable,
            },
            headers={"Content-Type": "application/json"},
        )
        data = response.json()
        if human_readable:
            for b in data:
                b["human_readable"] = True
        return data


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
        if not COIN_NAME:
            response = self.client.post(
                "/bills",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "include_spent_coins": incl_spent,
                    "empty_only": empty_only,
                },
                headers={"Content-Type": "application/json"},
            )
            data = response.json()
            assert len(data) > 0, "No bills found"
            assert len(data) == 1, "More than one bill found. Must specify governance coin name"
            coin_name = data[0]["name"]
        else:
            coin_name = COIN_NAME

        response = self.client.post(
            "/bills/reset",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "coin_name": coin_name,
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle = response.json()
        sig_response = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
        return sig_response


    async def bills_propose(
            self, INDEX: int = None, VALUE: str = None, coin_name: str = None, force: bool = False,
            proposal_threshold: int = None, veto_seconds: int = None, delay_seconds: int = None, max_delta: int = None
    ):
        assert INDEX is not None, "Must specify Statute index (between -1 and 42 included)"
        if coin_name is None:
            response = self.client.post(
                "/bills",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "include_spent_coins": False,
                    "empty_only": True,
                },
                headers={"Content-Type": "application/json"},
            )
            data = response.json()
            assert len(data) > 0, "No governance coin with empty bill found"
            coin_name = data[0]["name"]

        response = self.client.post(
            "/bills/new",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "coin_name": coin_name,
                "statute_index": INDEX,
                "value": VALUE,
                "threshold_amount_to_propose": proposal_threshold,
                "veto_seconds": veto_seconds,
                "delay_seconds": delay_seconds,
                "max_delta": max_delta,
                "force": force,
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


    async def bills_enact(self, COIN_NAME: str = None, info: bool = False, human_readable: bool = False):

        if COIN_NAME is None:
            response = self.client.post(
                "/bills",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "include_spent_coins": False,
                    "empty_only": False,
                    "non_empty_only": True,
                    "human_readable": human_readable,
                },
                headers={"Content-Type": "application/json"},
            )
            data = response.json()
            coins = sorted(data, key=lambda x: x["time_left_until_enactable"])
            if not info:
                assert len(coins) > 0, "There are no proposed bills"
                assert coins[0]["time_left_until_enactable"] == 0, "No enactable bill found"
            coin_name = coins[0]["name"]
        else:
            coin_name = COIN_NAME

        if info:
            if human_readable:
                for c in coins:
                    c["human_readable"] = True
            return coins

        response = self.client.post(
            "/bills/enact",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "coin_name": coin_name,
                "fee_per_cost": self.fee_per_cost,
            },
        )
        print("Got bundle, enacting bill")
        bundle = response.json()
        if response.status_code != 200:
            print(response.content)
            response.raise_for_status()
        sig_response = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
        return sig_response


    ### RESOLVER COINS ###
    async def resolver_list(self, all=False, usable_only=False, non_usable_only=False, human_readable=False):

        assert not (usable_only and  non_usable_only), "Cannot request both only usable and only non-usable resolver coins"
        assert not all, "Option --all not currently implemented"

        response = self.client.post(
            "/coins/outlier_resolver",
            json={
                "synthetic_pks": [] if all else [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "usable_only": usable_only,
                "non_usable_only": non_usable_only,
            },
            headers={"Content-Type": "application/json"},
        )
        data = response.json()
        if human_readable:
            for v in data:
                v["human_readable"] = True
        return data


    async def resolver_toggle(self, COIN_NAME: str):
        response = self.client.post(
            "/coins/create_outlier_resolver",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "coin_name": COIN_NAME,
                "fee_per_cost": self.fee_per_cost,
            },
        )
        bundle = response.json()
        sig_response = await self.sign_and_push(SpendBundle.from_json_dict(bundle))
        return sig_response


    ### ORACLE ###
    async def oracle_show(self):
        response = self.client.get("/oracle")
        return response.json()


    async def oracle_update(self, info=False):
        response = self.client.post(
            "/oracle/",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "info": info,
                "fee_per_cost": self.fee_per_cost,
            },
        )
        data = response.json()
        if info:
            return data
        try:
            bundle = SpendBundle.from_json_dict(data)
            return await self.sign_and_push(bundle)
        except:
            raise ValueError("Failed to update oracle")


    async def oracle_outlier_accept(self, COIN_NAME: str = None):

        if not COIN_NAME:
            response = self.client.post(
                "/coins/outlier_resolver",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "usable_only": True,
                },
                headers={"Content-Type": "application/json"},
            )
            data = response.json()
            pprint(f"{data=}")
            assert len(data) > 0, "No usable resolver coins found"
            assert len(data) == 1, "More than one usable resolver coin found. Must specify resolver coin name"
            coin_name = data[0]["name"]
        else:
            coin_name = COIN_NAME

        response = self.client.post(
            "/oracle/resolution/",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "resolver_crt_coin_name": coin_name,
                "decision": True,
                "fee_per_cost": self.fee_per_cost,
            },
        )
        data = response.json()
        try:
            bundle = SpendBundle.from_json_dict(data)
            return await self.sign_and_push(bundle)
        except:
            raise ValueError("Failed to vote to accept an outlier")


    async def oracle_outlier_reject(self, COIN_NAME: str = None, ban_list: list[str] = None):

        if not COIN_NAME:
            response = self.client.post(
                "/coins/outlier_resolver",
                json={
                    "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                    "usable_only": True,
                },
                headers={"Content-Type": "application/json"},
            )
            data = response.json()
            assert len(data) > 0, "No usable resolver coins found"
            assert len(data) == 1, "More than one usable resolver coin found. Must specify resolver coin name"
            coin_name = data[0]["name"]
        else:
            coin_name = COIN_NAME

        response = self.client.post(
            "/oracle/resolution/",
            json={
                "synthetic_pks": [key.to_bytes().hex() for key in self.synthetic_public_keys],
                "resolver_crt_coin_name": coin_name,
                "decision": False,
                "temp_ban_list": ban_list,
                "fee_per_cost": self.fee_per_cost,
            },
        )
        data = response.json()
        try:
            bundle = SpendBundle.from_json_dict(data)
            return await self.sign_and_push(bundle)
        except:
            raise ValueError("Failed to vote to reject an outlier")


    async def oracle_outlier_resolve(self):

        response = self.client.post(
            "/oracle/resolution/apply",
            json={"fee_per_cost": self.fee_per_cost},
        )
        data = response.json()
        try:
            bundle = SpendBundle.from_json_dict(data)
            return await self.sign_and_push(bundle)
        except:
            raise ValueError("Failed to resolve price outlier")


    ### STATUTES ###
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


    async def statutes_update(self, info=False):
        if info:
            response = self.client.post(
                "/statutes/info/",
            )
            return response.json()

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


    def close(self):
        self.client.close()
