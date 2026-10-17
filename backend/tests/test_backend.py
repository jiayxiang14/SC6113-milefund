"""Backend tests that run without a blockchain node: validation, signatures and the REST API
(using a stub in place of the RPC-backed Chain)."""

import json
import sys
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402
from chain import Chain  # noqa: E402
from config import BACKEND_DIR, Config  # noqa: E402
from validation import ValidationError, content_hash, metadata_message, recover_signer, validate_metadata  # noqa: E402

CREATOR = Account.create()
OTHER = Account.create()
CONTRACT = "0x5FbDB2315678afecb367f032d93F642f64180aa3"


def campaign_doc(**overrides) -> dict:
    doc = {
        "type": "campaign",
        "author": CREATOR.address.lower(),
        "createdAt": 1_790_000_000,
        "title": "Solar kiosks",
        "summary": "Solar phone charging kiosks for students.",
        "description": "Design, pilot and install three solar charging kiosks.",
        "category": "environment",
        "milestones": [{"title": "Prototype", "description": "One working kiosk."}],
    }
    doc.update(overrides)
    return doc


def sign(account, content: str) -> str:
    message = encode_defunct(text=metadata_message(content_hash(content)))
    return Web3.to_hex(account.sign_message(message).signature)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_valid_campaign_document_passes():
    doc = validate_metadata(json.dumps(campaign_doc()))
    assert doc["title"] == "Solar kiosks"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"title": "ab"}, "title"),
        ({"summary": "short"}, "summary"),
        ({"category": "gambling"}, "category"),
        ({"milestones": []}, "milestones"),
        ({"milestones": [{"title": "x" * 81, "description": "One working kiosk."}]}, "title"),
        ({"author": "0x1234"}, "author"),
        ({"createdAt": "yesterday"}, "createdAt"),
        ({"type": "invoice"}, "type"),
    ],
)
def test_invalid_campaign_documents_are_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        validate_metadata(json.dumps(campaign_doc(**overrides)))


def test_proof_links_must_be_http():
    doc = {
        "type": "milestone-proof",
        "author": CREATOR.address.lower(),
        "createdAt": 1_790_000_000,
        "campaignId": 0,
        "milestoneIndex": 0,
        "summary": "Prototype finished and load tested for 48 hours.",
        "links": ["javascript:alert(1)"],
    }
    with pytest.raises(ValidationError, match="http"):
        validate_metadata(json.dumps(doc))
    doc["links"] = ["https://example.org/report"]
    validate_metadata(json.dumps(doc))


def test_oversized_and_malformed_content_rejected():
    with pytest.raises(ValidationError, match="bytes"):
        validate_metadata(json.dumps(campaign_doc(description="x" * 9000)))
    with pytest.raises(ValidationError, match="JSON"):
        validate_metadata("{not json")
    with pytest.raises(ValidationError, match="object"):
        validate_metadata("[1, 2]")


def test_signature_recovers_author():
    content = json.dumps(campaign_doc())
    assert recover_signer(content_hash(content), sign(CREATOR, content)) == CREATOR.address.lower()
    with pytest.raises(ValidationError):
        recover_signer(content_hash(content), "0xdeadbeef")


# ---------------------------------------------------------------------------
# API with a stubbed chain
# ---------------------------------------------------------------------------


class StubChain(Chain):
    """Serves one campaign from memory instead of an RPC node."""

    def __init__(self, metadata_hash: str):
        super().__init__("http://127.0.0.1:1", str(BACKEND_DIR / "abi" / "MilestoneCrowdfund.json"))
        self.metadata_hash = metadata_hash

    def chain_id(self):
        return 31337

    def constants(self):
        return {"quorumBps": 2000, "maxMilestones": 5, "maxAttempts": 3, "minDuration": 120}

    def now(self):
        return 1_790_000_100

    def campaign_count(self):
        return 1

    def campaign(self, campaign_id):
        return {
            "id": campaign_id,
            "creator": CREATOR.address.lower(),
            "deadline": 1_790_000_500,
            "milestoneCount": 1,
            "currentMilestone": 0,
            "state": "Funding",
            "goal": 10**18,
            "totalRaised": 4 * 10**17,
            "totalReleased": 0,
            "totalWithdrawn": 0,
            "votingDuration": 180,
            "milestoneWindow": 1800,
            "phaseStart": 0,
            "backerCount": 2,
            "metadataHash": self.metadata_hash,
        }

    def milestones(self, campaign_id):
        return [
            {"index": 0, "percentageBps": 10000, "state": "Pending", "attempts": 0, "voteDeadline": 0,
             "yesWeight": 0, "noWeight": 0, "proofHash": "0x" + "00" * 32}
        ]


@pytest.fixture
def setup(tmp_path):
    content = json.dumps(campaign_doc())
    config = Config.from_env()
    config.contract_address = CONTRACT
    config.database_path = str(tmp_path / "test.db")
    config.rate_limit_per_minute = 5
    app = create_app(config, chain=StubChain(content_hash(content)), start_indexer=False)
    return app, app.test_client(), content


def test_metadata_upload_and_campaign_verification(setup):
    app, client, content = setup
    res = client.post("/api/metadata", json={"content": content, "signature": sign(CREATOR, content)})
    assert res.status_code == 201
    assert res.get_json()["hash"] == content_hash(content)

    again = client.post("/api/metadata", json={"content": content, "signature": sign(CREATOR, content)})
    assert again.status_code == 200 and again.get_json()["created"] is False

    detail = client.get("/api/campaigns/0").get_json()
    assert detail["metadataVerified"] is True
    assert detail["title"] == "Solar kiosks"
    assert detail["phase"] == "funding"
    assert detail["goal"] == str(10**18)  # wei travels as a string


def test_metadata_from_wrong_signer_is_rejected(setup):
    _, client, content = setup
    res = client.post("/api/metadata", json={"content": content, "signature": sign(OTHER, content)})
    assert res.status_code == 403


def test_tampered_metadata_row_fails_verification(setup):
    app, client, content = setup
    client.post("/api/metadata", json={"content": content, "signature": sign(CREATOR, content)})
    db = app.extensions["milefund"]["db"]
    with db.connect() as conn:
        conn.execute("UPDATE metadata SET content = replace(content, 'Solar', 'Scam!')")
    detail = client.get("/api/campaigns/0").get_json()
    assert detail["metadataVerified"] is False
    assert detail["title"] is None


def test_input_validation_errors(setup):
    _, client, _ = setup
    assert client.get("/api/campaigns/7").status_code == 404
    assert client.get("/api/history/not-an-address").status_code == 400
    assert client.post("/api/metadata", data="plain text").status_code == 400
    bad_tx = client.post("/api/transactions", json={"hash": "0x12", "from": CREATOR.address, "action": "vote"})
    assert bad_tx.status_code == 400
    bad_action = client.post(
        "/api/transactions", json={"hash": "0x" + "ab" * 32, "from": CREATOR.address, "action": "drainFunds"}
    )
    assert bad_action.status_code == 400


def test_transaction_report_is_tracked_as_pending(setup):
    _, client, _ = setup
    tx_hash = "0x" + "cd" * 32
    res = client.post("/api/transactions", json={"hash": tx_hash, "from": CREATOR.address, "action": "contribute", "campaignId": 0})
    assert res.status_code == 202 and res.get_json()["status"] == "pending"
    history = client.get(f"/api/history/{CREATOR.address}").get_json()
    assert [t["hash"] for t in history["transactions"]] == [tx_hash]


def test_rate_limit_and_security_headers(setup):
    _, client, _ = setup
    codes = [client.post("/api/metadata", json={}).status_code for _ in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429
    res = client.get("/api/config")
    assert "script-src 'self'" in res.headers["Content-Security-Policy"]
    assert res.headers["X-Content-Type-Options"] == "nosniff"
