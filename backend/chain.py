"""Read-only access to the MilestoneCrowdfund contract via web3.py.

The backend never holds a private key: every state-changing transaction is signed by the
user's MetaMask wallet in the browser. This module only reads state and decodes logs.
"""

import json
import threading
import time

from eth_utils import function_signature_to_4byte_selector
from web3 import Web3
from web3.exceptions import ContractCustomError, ContractLogicError

CAMPAIGN_STATES = ["Funding", "Active", "Completed", "Failed"]
MILESTONE_STATES = ["Pending", "Voting", "Approved", "Rejected"]
ZERO_HASH = "0x" + "00" * 32


def _struct(values, components) -> dict:
    return {comp["name"]: val for comp, val in zip(components, values)}


def _abi_type(param) -> str:
    if param["type"].startswith("tuple"):
        inner = ",".join(_abi_type(c) for c in param["components"])
        return f"({inner}){param['type'][5:]}"
    return param["type"]


class Chain:
    def __init__(self, rpc_url: str, abi_path: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        with open(abi_path) as f:
            self.abi = json.load(f)
        self.contract = None
        self._outputs = {
            item["name"]: item["outputs"] for item in self.abi if item.get("type") == "function"
        }
        # 4-byte selector -> custom error name, used to explain failed transactions.
        self.error_selectors = {}
        for item in self.abi:
            if item.get("type") == "error":
                sig = f"{item['name']}({','.join(_abi_type(i) for i in item['inputs'])})"
                self.error_selectors["0x" + function_signature_to_4byte_selector(sig).hex()] = item["name"]
        self._head_cache = (0.0, None)
        self._lock = threading.Lock()

    def bind(self, address: str) -> None:
        self.contract = self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=self.abi)

    @property
    def address(self) -> str:
        return self.contract.address

    def chain_id(self) -> int:
        return self.w3.eth.chain_id

    def latest_block(self) -> dict:
        """Latest block, cached for two seconds so a burst of API calls makes one RPC request."""
        with self._lock:
            ts, block = self._head_cache
            if block is None or time.time() - ts > 2:
                block = self.w3.eth.get_block("latest")
                self._head_cache = (time.time(), block)
            return block

    def now(self) -> int:
        """Chain time. Using the chain's clock rather than the server's keeps the UI correct on
        local nodes whose time has been fast-forwarded."""
        return int(self.latest_block()["timestamp"])

    # -- contract reads ----------------------------------------------------------

    def campaign_count(self) -> int:
        return self.contract.functions.campaignCount().call()

    def campaign(self, campaign_id: int) -> dict:
        raw = self.contract.functions.getCampaign(campaign_id).call()
        c = _struct(raw, self._outputs["getCampaign"][0]["components"])
        c["id"] = campaign_id
        c["creator"] = c["creator"].lower()
        c["state"] = CAMPAIGN_STATES[c["state"]]
        c["metadataHash"] = Web3.to_hex(c["metadataHash"])
        return c

    def milestones(self, campaign_id: int) -> list[dict]:
        raw = self.contract.functions.getMilestones(campaign_id).call()
        comps = self._outputs["getMilestones"][0]["components"]
        out = []
        for i, values in enumerate(raw):
            m = _struct(values, comps)
            m["index"] = i
            m["state"] = MILESTONE_STATES[m["state"]]
            m["proofHash"] = Web3.to_hex(m["proofHash"])
            out.append(m)
        return out

    def viewer(self, campaign: dict, milestones: list[dict], address: str) -> dict:
        addr = Web3.to_checksum_address(address)
        cid = campaign["id"]
        fn = self.contract.functions
        current = milestones[min(campaign["currentMilestone"], len(milestones) - 1)]
        return {
            "address": address.lower(),
            "isCreator": address.lower() == campaign["creator"],
            "contribution": fn.contributions(cid, addr).call(),
            "refundable": fn.refundableAmount(cid, addr).call(),
            "hasVotedCurrent": fn.hasVoted(cid, current["index"], current["attempts"], addr).call(),
        }

    def withdrawable(self, campaign_id: int) -> int:
        return self.contract.functions.withdrawableAmount(campaign_id).call()

    def constants(self) -> dict:
        fn = self.contract.functions
        return {
            "quorumBps": fn.QUORUM_BPS().call(),
            "maxMilestones": fn.MAX_MILESTONES().call(),
            "maxAttempts": fn.MAX_ATTEMPTS().call(),
            "minDuration": fn.MIN_DURATION().call(),
            "maxFundingDuration": fn.MAX_FUNDING_DURATION().call(),
            "maxVotingDuration": fn.MAX_VOTING_DURATION().call(),
            "maxMilestoneWindow": fn.MAX_MILESTONE_WINDOW().call(),
        }

    # -- errors ------------------------------------------------------------------

    def decode_error(self, data) -> str | None:
        if not data:
            return None
        if isinstance(data, bytes):
            data = Web3.to_hex(data)
        return self.error_selectors.get(str(data)[:10].lower())

    def revert_reason(self, tx: dict, block_number: int) -> str:
        """Best-effort reason for a failed transaction: replay it against the parent block."""
        call = {"from": tx["from"], "to": tx["to"], "data": tx["input"], "value": tx["value"]}
        try:
            self.w3.eth.call(call, block_number - 1)
        except ContractCustomError as e:
            return self.decode_error(e.data) or "Reverted with a custom error"
        except ContractLogicError as e:
            return self.decode_error(getattr(e, "data", None)) or str(e)
        except Exception:  # noqa: BLE001 - the node may not support historical calls
            pass
        return "Transaction reverted"


def campaign_phase(c: dict, milestones: list[dict], now: int) -> str:
    """A finer-grained status than the on-chain state, telling the UI which action is due."""
    if c["state"] == "Funding":
        return "funding" if now < c["deadline"] else "funding-ended"
    if c["state"] != "Active":
        return c["state"].lower()
    m = milestones[c["currentMilestone"]]
    if m["state"] == "Voting":
        return "voting" if now < m["voteDeadline"] else "vote-ended"
    if now > c["phaseStart"] + c["milestoneWindow"]:
        return "creator-inactive"
    return "revising" if m["state"] == "Rejected" else "in-progress"
