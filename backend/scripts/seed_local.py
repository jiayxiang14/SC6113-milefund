"""Populate a local Anvil chain with demo campaigns in every lifecycle state.

Run against a fresh `anvil` with the contract deployed and the backend running with a raised
rate limit (the script sends far more requests per minute than a person would):

    RATE_LIMIT_PER_MINUTE=1000 .venv/bin/python app.py
    .venv/bin/python scripts/seed_local.py

It uses Anvil's well-known development accounts (never use them on a real network), signs
metadata exactly as the browser does, fast-forwards chain time and then checks the API.
"""

import json
import os
import sys
import time

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

API = os.getenv("API_URL", "http://127.0.0.1:5050")
RPC = os.getenv("RPC_URL", "http://127.0.0.1:8545")
MNEMONIC = "test test test test test test test test test test test junk"

Account.enable_unaudited_hdwallet_features()
accounts = [Account.from_mnemonic(MNEMONIC, account_path=f"m/44'/60'/0'/0/{i}") for i in range(6)]
creator, alice, bob, carol, dave = accounts[1:6]

w3 = Web3(Web3.HTTPProvider(RPC))
cfg = requests.get(f"{API}/api/config", timeout=10).json()
if cfg["chainId"] != 31337:
    sys.exit("Refusing to run: this script only targets a local Anvil chain (31337).")
contract = w3.eth.contract(address=cfg["contractAddress"], abi=cfg["abi"])
MIN = 60


def send(account, action, fn, campaign_id=None, value=0):
    tx = fn.build_transaction(
        {"from": account.address, "nonce": w3.eth.get_transaction_count(account.address), "value": value}
    )
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    requests.post(
        f"{API}/api/transactions",
        json={"hash": Web3.to_hex(tx_hash), "from": account.address, "action": action, "campaignId": campaign_id},
        timeout=10,
    )
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt["status"] == 1, f"{action} failed"
    return receipt


def publish(account, doc) -> str:
    content = json.dumps(doc)
    content_hash = Web3.to_hex(Web3.keccak(text=content))
    message = encode_defunct(text=f"MileFund metadata\nHash: {content_hash}")
    signature = Web3.to_hex(account.sign_message(message).signature)
    res = requests.post(f"{API}/api/metadata", json={"content": content, "signature": signature}, timeout=10)
    res.raise_for_status()
    assert res.json()["hash"] == content_hash
    return content_hash


def warp(seconds):
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])


def chain_now():
    return w3.eth.get_block("latest")["timestamp"]


def create(title, summary, description, category, goal_eth, milestones, funding=5 * MIN, voting=3 * MIN, window=30 * MIN):
    doc = {
        "type": "campaign",
        "author": creator.address.lower(),
        "createdAt": chain_now(),
        "title": title,
        "summary": summary,
        "description": description,
        "category": category,
        "milestones": [{"title": t, "description": d} for t, d, _ in milestones],
    }
    meta = publish(creator, doc)
    receipt = send(
        creator,
        "createCampaign",
        contract.functions.createCampaign(
            Web3.to_wei(goal_eth, "ether"), funding, voting, window, [p for _, _, p in milestones], meta
        ),
    )
    campaign_id = contract.events.CampaignCreated().process_receipt(receipt)[0]["args"]["campaignId"]
    print(f"  created campaign #{campaign_id}: {title}")
    return campaign_id


def back(cid, who, eth):
    send(who, "contribute", contract.functions.contribute(cid), cid, Web3.to_wei(eth, "ether"))


def submit(cid, index, summary, links=()):
    doc = {
        "type": "milestone-proof",
        "author": creator.address.lower(),
        "createdAt": chain_now(),
        "campaignId": cid,
        "milestoneIndex": index,
        "summary": summary,
        "links": list(links),
    }
    send(creator, "submitMilestone", contract.functions.submitMilestone(cid, publish(creator, doc)), cid)


def vote(cid, who, approve):
    send(who, "vote", contract.functions.vote(cid, approve), cid)


def tally(cid):
    warp(3 * MIN)
    send(dave, "finalizeMilestone", contract.functions.finalizeMilestone(cid), cid)


def main():
    print("Seeding demo data…")

    # 1. Completed campaign, including one rejected-then-resubmitted milestone.
    a = create(
        "Solar charging kiosks for the campus",
        "Three solar-powered phone charging kiosks for students, delivered in stages.",
        "We will design, pilot and install three off-grid solar charging kiosks around the campus. "
        "Each stage is released only after backers review photos, test reports and the bill of materials.",
        "environment",
        1.0,
        [
            ("Prototype kiosk", "One working kiosk with a 200 W panel and a test report.", 3000),
            ("Pilot at the library", "Kiosk installed at the library for a two-week usage pilot.", 3000),
            ("Full roll-out", "All three kiosks installed with a maintenance plan.", 4000),
        ],
    )
    back(a, alice, 0.6)
    back(a, bob, 0.3)
    back(a, carol, 0.2)
    warp(5 * MIN)
    send(dave, "finalizeFunding", contract.functions.finalizeFunding(a), a)
    submit(a, 0, "The prototype is built and passed a 48-hour load test. Photos and the test log are linked.", ["https://example.org/kiosk/prototype"])
    vote(a, alice, True)
    vote(a, bob, True)
    tally(a)
    send(creator, "withdraw", contract.functions.withdraw(a), a)
    submit(a, 1, "Kiosk installed at the library entrance. Usage data will follow next week.")
    vote(a, alice, False)
    vote(a, carol, True)
    tally(a)  # rejected: no usage data yet
    submit(a, 1, "Two-week pilot finished: 412 charging sessions logged. Usage dashboard linked.", ["https://example.org/kiosk/pilot-report"])
    vote(a, alice, True)
    vote(a, bob, True)
    tally(a)
    submit(a, 2, "All three kiosks installed (library, canteen, sports hall). Maintenance plan attached.", ["https://example.org/kiosk/rollout"])
    vote(a, alice, True)
    vote(a, carol, True)
    tally(a)
    send(creator, "withdraw", contract.functions.withdraw(a), a)

    # 2. Failed campaign: goal not reached, backers refunded.
    b = create(
        "Community darkroom",
        "Rent and equip a shared film darkroom for local photographers.",
        "A shared darkroom with two enlargers and chemistry for community use, funded by its members.",
        "arts",
        2.0,
        [("Lease and fit-out", "Sign the lease and install ventilation and sinks.", 6000), ("Equipment", "Buy enlargers, trays and safelights.", 4000)],
    )
    back(b, bob, 0.25)
    back(b, carol, 0.1)
    warp(5 * MIN)
    send(dave, "finalizeFunding", contract.functions.finalizeFunding(b), b)
    send(bob, "claimRefund", contract.functions.claimRefund(b), b)

    # 3. Active campaign with a milestone vote currently open.
    c = create(
        "Open-source exam timetable planner",
        "A web tool that builds clash-free exam timetables for student societies.",
        "We will ship a constraint-solver based timetable planner, open-sourced under MIT, with a hosted demo for the university.",
        "technology",
        0.5,
        [("Solver and CLI", "Constraint solver with a command-line interface and test suite.", 5000), ("Web interface and hosting", "Browser UI and a hosted public instance.", 5000)],
        voting=60 * MIN,
    )
    back(c, alice, 0.3)
    back(c, dave, 0.25)
    warp(5 * MIN)
    send(dave, "finalizeFunding", contract.functions.finalizeFunding(c), c)
    submit(c, 0, "Solver released with 94% test coverage. It schedules 1,200 exams in under 3 seconds.", ["https://github.com/example/timetable-solver"])
    vote(c, dave, True)

    # 4. Campaign that is still raising funds.
    d = create(
        "Peer tutoring micro-grants",
        "Small grants that pay senior students to run weekly maths tutoring sessions.",
        "Funds pay tutors per verified session. Each term is a milestone, released after attendance records are published.",
        "education",
        0.8,
        [("Term 1", "Twelve weekly sessions with published attendance.", 5000), ("Term 2", "Twelve more sessions plus a feedback survey.", 5000)],
        funding=3 * 24 * 60 * MIN,
    )
    back(d, bob, 0.15)

    print("Waiting for the indexer…")
    time.sleep(3)
    verify(a, b, c, d)


def verify(a, b, c, d):
    get = lambda path: requests.get(f"{API}{path}", timeout=10).json()  # noqa: E731
    states = {x["id"]: (x["state"], x["phase"], x["metadataVerified"]) for x in get("/api/campaigns")["campaigns"]}
    print("Campaign states:", states)
    assert states[a] == ("Completed", "completed", True)
    assert states[b] == ("Failed", "failed", True)
    assert states[c] == ("Active", "voting", True)
    assert states[d] == ("Funding", "funding", True)

    detail = get(f"/api/campaigns/{a}?viewer={alice.address}")
    assert [m["state"] for m in detail["milestones"]] == ["Approved"] * 3
    assert detail["milestones"][1]["attempts"] == 2
    assert all(m["proof"] for m in detail["milestones"])
    assert detail["withdrawable"] == "0"

    failed = get(f"/api/campaigns/{b}?viewer={carol.address}")
    assert failed["viewer"]["refundable"] == str(Web3.to_wei(0.1, "ether"))

    history = get(f"/api/history/{alice.address}")
    assert history["transactions"] and all(t["status"] == "confirmed" for t in history["transactions"]), history["transactions"]
    assert any(e["event"] == "Voted" for e in history["events"])
    activity = get(f"/api/campaigns/{a}/activity")["events"]
    print(f"Indexed {len(activity)} events for campaign #{a}; alice has {len(history['transactions'])} tracked transactions.")
    print("All checks passed.")


if __name__ == "__main__":
    main()
