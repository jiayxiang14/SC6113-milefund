"""MileFund Flask backend: REST API, event indexer and static frontend host."""

import json
import logging
import threading
import time
from collections import defaultdict, deque

from flask import Flask, jsonify, request
from requests.exceptions import RequestException
from web3.exceptions import ContractLogicError

from chain import ZERO_HASH, Chain, campaign_phase
from config import KNOWN_NETWORKS, PROJECT_DIR, Config
from db import Database
from indexer import Indexer
from validation import (
    TX_ACTIONS,
    ValidationError,
    content_hash,
    recover_signer,
    require_address,
    require_hash,
    validate_metadata,
)

log = logging.getLogger("milefund")


class RateLimiter:
    """Sliding one-minute window per client IP for write endpoints."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _wei(value: int) -> str:
    # Wei amounts exceed JavaScript's safe integer range, so they travel as strings.
    return str(value)


def create_app(config: Config | None = None, chain: Chain | None = None, start_indexer: bool = True) -> Flask:
    config = config or Config.from_env()
    app = Flask(__name__, static_folder=str(PROJECT_DIR / "frontend"), static_url_path="")
    app.json.sort_keys = False

    chain = chain or Chain(config.rpc_url, config.abi_path)
    chain_id = chain.chain_id()
    config.resolve_deployment(chain_id)
    chain.bind(config.contract_address)
    db = Database(config.database_path)
    limiter = RateLimiter(config.rate_limit_per_minute)
    network_name, default_explorer = KNOWN_NETWORKS.get(chain_id, (f"Chain {chain_id}", ""))
    explorer = config.explorer_url if config.explorer_url is not None else default_explorer
    constants = chain.constants()

    indexer = Indexer(chain, db, config.start_block, config.index_chunk, config.confirmations, config.index_interval)
    app.extensions["milefund"] = {"chain": chain, "db": db, "indexer": indexer, "config": config}
    if start_indexer:
        indexer.start()
    log.info("serving %s on %s (chain %d)", chain.address, network_name, chain_id)

    # -- helpers -----------------------------------------------------------------

    def load_campaign(campaign_id: int):
        if campaign_id < 0 or campaign_id >= chain.campaign_count():
            raise ApiError(404, f"Campaign {campaign_id} does not exist")
        return chain.campaign(campaign_id), chain.milestones(campaign_id)

    def verified_metadata(hash_: str, kind: str, author: str) -> dict | None:
        """Return the stored document only if it matches the on-chain hash and was written by
        the on-chain author. The hash is recomputed so a tampered database row is caught."""
        if hash_ == ZERO_HASH:
            return None
        row = db.get_metadata(hash_)
        if not row or row["kind"] != kind or row["author"] != author:
            return None
        if content_hash(row["content"]) != hash_.lower():
            log.error("metadata %s failed its integrity check", hash_)
            return None
        return {"content": row["content"], "document": json.loads(row["content"])}

    def summarize(c: dict, milestones: list[dict], now: int) -> dict:
        meta = verified_metadata(c["metadataHash"], "campaign", c["creator"])
        doc = meta["document"] if meta else {}
        return {
            "id": c["id"],
            "creator": c["creator"],
            "title": doc.get("title"),
            "summary": doc.get("summary"),
            "category": doc.get("category"),
            "state": c["state"],
            "phase": campaign_phase(c, milestones, now),
            "goal": _wei(c["goal"]),
            "totalRaised": _wei(c["totalRaised"]),
            "deadline": c["deadline"],
            "backerCount": c["backerCount"],
            "milestoneCount": c["milestoneCount"],
            "currentMilestone": c["currentMilestone"],
            "metadataVerified": meta is not None,
        }

    # -- error handling ----------------------------------------------------------

    @app.errorhandler(ApiError)
    def handle_api_error(e: ApiError):
        return jsonify(error=e.message), e.status

    @app.errorhandler(ValidationError)
    def handle_validation_error(e: ValidationError):
        return jsonify(error=str(e)), 400

    @app.errorhandler(RequestException)
    def handle_rpc_error(e):
        log.warning("RPC error: %s", e)
        return jsonify(error="The blockchain node is unreachable. Please try again shortly."), 503

    @app.errorhandler(ContractLogicError)
    def handle_contract_error(e):
        return jsonify(error=f"Contract call failed: {chain.decode_error(getattr(e, 'data', None)) or e}"), 400

    @app.errorhandler(404)
    def handle_404(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found"), 404
        return e

    @app.after_request
    def security_headers(resp):
        # Scripts may only come from this origin, which blocks injected third-party code.
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def rate_limited():
        if not limiter.allow(f"{request.remote_addr}:{request.endpoint}"):
            raise ApiError(429, "Too many requests. Please wait a minute and try again.")

    def json_body() -> dict:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "Request body must be a JSON object")
        return body

    # -- routes ------------------------------------------------------------------

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/config")
    def get_config():
        return jsonify(
            chainId=chain_id,
            networkName=network_name,
            contractAddress=chain.address,
            explorerUrl=explorer,
            constants=constants,
            abi=chain.abi,
        )

    @app.get("/api/health")
    def health():
        head = chain.w3.eth.block_number
        return jsonify(
            ok=indexer.last_error is None,
            chainHead=head,
            lastIndexedBlock=indexer.last_indexed_block,
            indexerError=indexer.last_error,
        )

    @app.get("/api/campaigns")
    def list_campaigns():
        limit = min(max(request.args.get("limit", 50, type=int), 1), 100)
        count = chain.campaign_count()
        now = chain.now()
        items = []
        for cid in range(count - 1, max(count - 1 - limit, -1), -1):
            c, ms = chain.campaign(cid), chain.milestones(cid)
            items.append(summarize(c, ms, now))
        return jsonify(campaigns=items, total=count, now=now)

    @app.get("/api/campaigns/<int:campaign_id>")
    def get_campaign(campaign_id: int):
        c, milestones = load_campaign(campaign_id)
        now = chain.now()
        meta = verified_metadata(c["metadataHash"], "campaign", c["creator"])
        doc = meta["document"] if meta else {}
        described = doc.get("milestones", [])

        ms_out = []
        for m in milestones:
            i = m["index"]
            proof = verified_metadata(m["proofHash"], "milestone-proof", c["creator"])
            proof_doc = proof["document"] if proof else None
            if proof_doc and (proof_doc["campaignId"] != campaign_id or proof_doc["milestoneIndex"] != i):
                proof = proof_doc = None
            ms_out.append(
                {
                    "index": i,
                    "title": described[i]["title"] if i < len(described) else None,
                    "description": described[i]["description"] if i < len(described) else None,
                    "percentageBps": m["percentageBps"],
                    "state": m["state"],
                    "attempts": m["attempts"],
                    "voteDeadline": m["voteDeadline"],
                    "yesWeight": _wei(m["yesWeight"]),
                    "noWeight": _wei(m["noWeight"]),
                    "proofHash": m["proofHash"],
                    "proof": proof_doc,
                    "proofContent": proof["content"] if proof else None,
                }
            )

        body = summarize(c, milestones, now)
        body.update(
            description=doc.get("description"),
            metadataHash=c["metadataHash"],
            metadataContent=meta["content"] if meta else None,
            milestonesMatchChain=len(described) == c["milestoneCount"],
            totalReleased=_wei(c["totalReleased"]),
            totalWithdrawn=_wei(c["totalWithdrawn"]),
            withdrawable=_wei(c["totalReleased"] - c["totalWithdrawn"]),
            votingDuration=c["votingDuration"],
            milestoneWindow=c["milestoneWindow"],
            phaseStart=c["phaseStart"],
            submissionDeadline=c["phaseStart"] + c["milestoneWindow"] if c["state"] == "Active" else None,
            milestones=ms_out,
            now=now,
        )

        viewer = request.args.get("viewer")
        if viewer:
            v = chain.viewer(c, milestones, require_address(viewer, "viewer"))
            body["viewer"] = {
                **v,
                "contribution": _wei(v["contribution"]),
                "refundable": _wei(v["refundable"]),
            }
        return jsonify(body)

    @app.get("/api/campaigns/<int:campaign_id>/activity")
    def campaign_activity(campaign_id: int):
        load_campaign(campaign_id)
        return jsonify(events=[_event_out(e) for e in db.campaign_events(campaign_id)])

    @app.get("/api/history/<address>")
    def history(address: str):
        addr = require_address(address)
        return jsonify(
            address=addr,
            transactions=db.sender_transactions(addr),
            events=[_event_out(e) for e in db.actor_events(addr)],
        )

    @app.post("/api/metadata")
    def post_metadata():
        rate_limited()
        body = json_body()
        content = body.get("content")
        doc = validate_metadata(content)
        hash_ = content_hash(content)
        signer = recover_signer(hash_, body.get("signature"))
        if signer != doc["author"]:
            raise ApiError(403, "Signature does not match the document author")
        created = db.insert_metadata(hash_, doc["type"], signer, content, body["signature"], int(time.time()))
        return jsonify(hash=hash_, created=created), 201 if created else 200

    @app.get("/api/metadata/<hash_>")
    def get_metadata(hash_: str):
        row = db.get_metadata(require_hash(hash_))
        if not row:
            raise ApiError(404, "Metadata not found")
        return jsonify(hash=row["hash"], kind=row["kind"], author=row["author"], content=row["content"])

    @app.post("/api/transactions")
    def post_transaction():
        rate_limited()
        body = json_body()
        tx_hash = require_hash(body.get("hash"), "hash")
        sender = require_address(body.get("from"), "from")
        action = body.get("action")
        if action not in TX_ACTIONS:
            raise ApiError(400, f"action must be one of {sorted(TX_ACTIONS)}")
        campaign_id = body.get("campaignId")
        if campaign_id is not None and (not isinstance(campaign_id, int) or campaign_id < 0):
            raise ApiError(400, "campaignId must be a non-negative integer")
        db.insert_transaction(tx_hash, sender, action, campaign_id, int(time.time()))
        return jsonify(db.get_transaction(tx_hash)), 202

    @app.get("/api/transactions/<tx_hash>")
    def get_transaction(tx_hash: str):
        row = db.get_transaction(require_hash(tx_hash))
        if not row:
            raise ApiError(404, "Transaction not tracked")
        return jsonify(row)

    return app


def _event_out(row: dict) -> dict:
    return {
        "txHash": row["tx_hash"],
        "blockNumber": row["block_number"],
        "time": row["block_time"],
        "event": row["event"],
        "campaignId": row["campaign_id"],
        "actor": row["actor"],
        "amount": row["amount"],
        "args": json.loads(row["data"]),
    }


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    create_app().run(host="127.0.0.1", port=int(os.getenv("PORT", "5050")), debug=False)
