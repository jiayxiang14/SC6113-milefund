"""Background worker that mirrors contract events into SQLite and tracks reported transactions."""

import json
import logging
import threading
import time

from web3 import Web3
from web3.exceptions import TransactionNotFound

from chain import Chain
from db import Database

log = logging.getLogger("milefund.indexer")

# Which event argument identifies the account that performed the action.
ACTOR_ARG = {
    "CampaignCreated": "creator",
    "Contributed": "backer",
    "Voted": "voter",
    "FundsWithdrawn": "creator",
    "Refunded": "backer",
}
AMOUNT_ARG = {
    "CampaignCreated": "goal",
    "Contributed": "amount",
    "Voted": "weight",
    "MilestoneFinalized": "releasedAmount",
    "FundsWithdrawn": "amount",
    "Refunded": "amount",
}
DROP_AFTER_SECONDS = 15 * 60


def _hex(value) -> str:
    return value.lower() if isinstance(value, str) else Web3.to_hex(value)


def _jsonable(value):
    if isinstance(value, bytes):
        return Web3.to_hex(value)
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53:
        return str(value)
    return value


class Indexer:
    def __init__(self, chain: Chain, db: Database, start_block: int, chunk: int, confirmations: int, interval: float):
        self.chain = chain
        self.db = db
        self.start_block = start_block
        self.chunk = chunk
        self.confirmations = confirmations
        self.interval = interval
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._block_times: dict[int, int] = {}
        self._events_by_topic = {}
        for item in chain.abi:
            if item.get("type") == "event":
                event = getattr(chain.contract.events, item["name"])()
                self._events_by_topic[_hex(event.topic)] = event

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="indexer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - keep the worker alive across RPC hiccups
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("indexer iteration failed: %s", self.last_error)
            self._stop.wait(self.interval)

    def sync_once(self) -> None:
        self.index_events()
        self.check_transactions()

    @property
    def last_indexed_block(self) -> int:
        return int(self.db.get_kv("last_indexed_block", str(self.start_block - 1)))

    # -- events ------------------------------------------------------------------

    def index_events(self) -> None:
        # Stay `confirmations` blocks behind the head so a shallow reorg cannot leave
        # phantom events in the database.
        head = self.chain.w3.eth.block_number - self.confirmations
        start = self.last_indexed_block + 1
        while start <= head:
            end = min(start + self.chunk - 1, head)
            logs = self.chain.w3.eth.get_logs(
                {"address": self.chain.address, "fromBlock": start, "toBlock": end}
            )
            rows = [row for row in (self._decode(lg) for lg in logs) if row]
            self.db.insert_events(rows, end)
            if rows:
                log.info("indexed %d events in blocks %d-%d", len(rows), start, end)
            start = end + 1

    def _decode(self, raw_log) -> dict | None:
        event = self._events_by_topic.get(_hex(raw_log["topics"][0]))
        if event is None:
            return None
        decoded = event.process_log(raw_log)
        args = dict(decoded["args"])
        name = decoded["event"]
        actor = args.get(ACTOR_ARG.get(name, ""))
        amount = args.get(AMOUNT_ARG.get(name, ""))
        return {
            "tx_hash": Web3.to_hex(decoded["transactionHash"]),
            "log_index": decoded["logIndex"],
            "block_number": decoded["blockNumber"],
            "block_time": self._block_time(decoded["blockNumber"]),
            "event": name,
            "campaign_id": args["campaignId"],
            "actor": actor.lower() if actor else None,
            "amount": str(amount) if amount is not None else None,
            "data": json.dumps({k: _jsonable(v) for k, v in args.items()}),
        }

    def _block_time(self, number: int) -> int:
        if number not in self._block_times:
            if len(self._block_times) > 5000:
                self._block_times.clear()
            self._block_times[number] = int(self.chain.w3.eth.get_block(number)["timestamp"])
        return self._block_times[number]

    # -- transactions ------------------------------------------------------------

    def check_transactions(self) -> None:
        now = int(time.time())
        for tx in self.db.pending_transactions():
            self._check_transaction(tx, now)

    def _check_transaction(self, row: dict, now: int) -> None:
        w3 = self.chain.w3
        try:
            tx = w3.eth.get_transaction(row["hash"])
        except TransactionNotFound:
            if now - row["submitted_at"] > DROP_AFTER_SECONDS:
                self.db.update_transaction(row["hash"], now, status="dropped")
            return

        # Reject reports that do not describe a call from the claimed sender to our contract,
        # so nobody can pollute another user's history with unrelated transactions.
        if tx["from"].lower() != row["sender"] or (tx["to"] or "").lower() != self.chain.address.lower():
            log.info("discarding mismatched transaction report %s", row["hash"])
            self.db.delete_transaction(row["hash"])
            return

        try:
            receipt = w3.eth.get_transaction_receipt(row["hash"])
        except TransactionNotFound:
            return  # still in the mempool

        if receipt["status"] == 1:
            self.db.update_transaction(
                row["hash"], now, status="confirmed", block_number=receipt["blockNumber"], gas_used=receipt["gasUsed"]
            )
        else:
            self.db.update_transaction(
                row["hash"],
                now,
                status="failed",
                block_number=receipt["blockNumber"],
                gas_used=receipt["gasUsed"],
                error=self.chain.revert_reason(tx, receipt["blockNumber"]),
            )
