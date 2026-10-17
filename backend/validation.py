"""Input validation for API requests and off-chain metadata documents."""

import json
import re
from urllib.parse import urlparse

from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")

MAX_CONTENT_BYTES = 8 * 1024
CATEGORIES = {"technology", "education", "community", "environment", "health", "arts", "other"}
TX_ACTIONS = {
    "createCampaign",
    "contribute",
    "finalizeFunding",
    "cancelCampaign",
    "submitMilestone",
    "vote",
    "finalizeMilestone",
    "failInactiveCampaign",
    "withdraw",
    "claimRefund",
}


class ValidationError(ValueError):
    pass


def require_address(value, field="address") -> str:
    if not isinstance(value, str) or not ADDRESS_RE.match(value):
        raise ValidationError(f"{field} must be a 0x-prefixed 20-byte hex address")
    return value.lower()


def require_hash(value, field="hash") -> str:
    if not isinstance(value, str) or not HASH_RE.match(value):
        raise ValidationError(f"{field} must be a 0x-prefixed 32-byte hex string")
    return value.lower()


def _text(doc: dict, key: str, lo: int, hi: int) -> str:
    value = doc.get(key)
    if not isinstance(value, str):
        raise ValidationError(f"'{key}' must be a string")
    stripped = value.strip()
    if not lo <= len(stripped) <= hi:
        raise ValidationError(f"'{key}' must be {lo}-{hi} characters")
    return stripped


def _int(doc: dict, key: str, lo: int, hi: int) -> int:
    value = doc.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValidationError(f"'{key}' must be an integer between {lo} and {hi}")
    return value


def _url(value) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise ValidationError("links must be strings of at most 300 characters")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("links must be absolute http(s) URLs")
    return value


def metadata_message(content_hash: str) -> str:
    """The exact text the author signs with MetaMask (EIP-191 personal_sign)."""
    return f"MileFund metadata\nHash: {content_hash}"


def content_hash(content: str) -> str:
    return Web3.to_hex(Web3.keccak(text=content))


def validate_metadata(content: str) -> dict:
    """Parse and validate a metadata document. The raw string (not a re-serialisation) is what
    gets hashed, so the browser and server always agree on the hash."""
    if not isinstance(content, str) or not content:
        raise ValidationError("content must be a non-empty JSON string")
    if len(content.encode()) > MAX_CONTENT_BYTES:
        raise ValidationError(f"content must be at most {MAX_CONTENT_BYTES} bytes")
    try:
        doc = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValidationError(f"content is not valid JSON: {e.msg}") from None
    if not isinstance(doc, dict):
        raise ValidationError("content must be a JSON object")

    doc["author"] = require_address(doc.get("author"), "author")
    _int(doc, "createdAt", 1_600_000_000, 10_000_000_000)
    kind = doc.get("type")

    if kind == "campaign":
        _text(doc, "title", 3, 80)
        _text(doc, "summary", 10, 200)
        _text(doc, "description", 20, 3000)
        if doc.get("category") not in CATEGORIES:
            raise ValidationError(f"'category' must be one of {sorted(CATEGORIES)}")
        milestones = doc.get("milestones")
        if not isinstance(milestones, list) or not 1 <= len(milestones) <= 5:
            raise ValidationError("'milestones' must be a list of 1-5 items")
        for m in milestones:
            if not isinstance(m, dict):
                raise ValidationError("each milestone must be an object")
            _text(m, "title", 3, 80)
            _text(m, "description", 10, 500)
    elif kind == "milestone-proof":
        _int(doc, "campaignId", 0, 2**63)
        _int(doc, "milestoneIndex", 0, 4)
        _text(doc, "summary", 20, 3000)
        links = doc.get("links", [])
        if not isinstance(links, list) or len(links) > 5:
            raise ValidationError("'links' must be a list of at most 5 URLs")
        for link in links:
            _url(link)
    else:
        raise ValidationError("'type' must be 'campaign' or 'milestone-proof'")
    return doc


def recover_signer(content_hash_: str, signature: str) -> str:
    if not isinstance(signature, str) or not SIGNATURE_RE.match(signature):
        raise ValidationError("signature must be a 65-byte hex string")
    try:
        signer = Account.recover_message(encode_defunct(text=metadata_message(content_hash_)), signature=signature)
    except Exception:  # noqa: BLE001 - malformed signatures raise various errors
        raise ValidationError("signature could not be verified") from None
    return signer.lower()
