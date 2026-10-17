# MileFund: Milestone-Based Crowdfunding DApp

MileFund is a decentralized crowdfunding application on Ethereum. Contributions are locked in a
smart contract and released to the project creator **one milestone at a time**, and only after
backers approve each milestone in a contribution-weighted vote. If a campaign fails (goal missed,
cancelled, creator inactive, or a milestone rejected too many times), backers reclaim the
unreleased funds pro rata.

| Layer | Technology |
|---|---|
| Smart contract | Solidity 0.8.26, OpenZeppelin `ReentrancyGuard` |
| Contract tooling and tests | Foundry (forge, anvil, cast) |
| Backend | Python 3.13, Flask 3, web3.py 8, SQLite |
| Frontend | HTML, CSS, vanilla JavaScript, ethers.js 6 |
| Wallet | MetaMask (EIP-1193 / EIP-191 signatures) |
| Network | Ethereum Sepolia testnet (or a local Anvil chain) |

## Features

- **Campaign creation** with 1–5 milestones whose fund shares add up to 100%.
- **Contributions** in ETH during a funding period; a contribution becomes the backer's voting weight.
- **Milestone governance:** the creator submits proof of delivery; backers vote; the milestone passes when
  approvals outweigh rejections **and** at least 20% of the raised funds vote (quorum).
- **Resubmission:** a rejected milestone can be resubmitted up to 3 attempts in total.
- **Automatic failure paths:** goal not met, creator cancels, creator misses the submission window,
  or attempts are exhausted. Anyone can trigger the transition.
- **Pro-rata refunds** of every wei not yet released to the creator (pull payments).
- **Tamper-evident off-chain data:** descriptions and proofs are stored by the backend, but their
  keccak256 hash is recorded on-chain. The browser recomputes the hash and compares it with the chain.
- **Signed uploads:** metadata uploads must be signed in MetaMask by the author (EIP-191).
- **Transaction status and history:** pending → confirmed / failed (with the decoded revert reason),
  plus an indexed log of all on-chain events per campaign and per user.

## Architecture

```
 Browser (HTML/CSS/JS + ethers.js)
   │  1. sign metadata (personal_sign) ──────────────►  Flask backend  ── SQLite
   │  2. send transaction, signed in MetaMask                │  ▲         (events, metadata,
   ▼                                                         │  │          tx status)
 MilestoneCrowdfund.sol on Sepolia ── 3. emits events ───────┘  │
   ▲                                     (indexer thread polls eth_getLogs)
   └──────── 4. backend reads live state (web3.py) ────────────┘
```

The backend **never holds a private key**. All state changes are signed in the user's wallet.

### Campaign lifecycle

```
Funding ──deadline, goal met──► Active ──last milestone approved──► Completed
   │                              │
   │ goal missed / cancelled      │ creator inactive / attempts exhausted
   ▼                              ▼
 Failed ◄─────────────────────────┘    (backers claim pro-rata refunds)

Milestone: Pending ──submit──► Voting ──tally──► Approved
                                  │
                                  └──► Rejected ──resubmit──► Voting
```

## Repository layout

```
src/MilestoneCrowdfund.sol        smart contract
test/MilestoneCrowdfund.t.sol     Foundry unit, reentrancy and fuzz tests (38 tests)
script/Deploy.s.sol               deployment script (writes deployments/<chainId>.json)
backend/app.py                    Flask app and REST API
backend/chain.py                  read-only contract access (web3.py)
backend/indexer.py                event indexer and transaction-status tracker
backend/validation.py             request and metadata validation, signature recovery
backend/db.py                     SQLite schema and queries
backend/abi/                      contract ABI used by the backend and frontend
backend/tests/                    pytest suite (18 tests)
backend/scripts/seed_local.py     creates demo campaigns on Anvil and checks the API end to end
frontend/                         single-page web app served by Flask
```

## Prerequisites

- [Foundry](https://book.getfoundry.sh/getting-started/installation) (`forge`, `anvil`, `cast`)
- Python 3.11+
- The MetaMask browser extension

```bash
git clone --recursive <repo-url> milefund && cd milefund   # or: forge install
forge build
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
```

## Run locally (Anvil)

```bash
# Terminal 1: local chain (prints 10 funded test accounts and their private keys)
anvil

# Terminal 2: deploy with Anvil's first test account
forge script script/Deploy.s.sol --rpc-url anvil --broadcast \
  --private-key 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80

# Terminal 3: backend + frontend on http://127.0.0.1:5050
cd backend && .venv/bin/python app.py
```

Optional demo data (campaigns in every state):

```bash
cd backend
RATE_LIMIT_PER_MINUTE=1000 .venv/bin/python app.py   # restart with a higher limit while seeding
.venv/bin/python scripts/seed_local.py
```

**MetaMask setup for Anvil:** add a network with RPC `http://127.0.0.1:8545` and chain ID `31337`
(the app also offers to add it), then import one of Anvil's test private keys. These keys are
public: never use them on a real network. If you restart Anvil, use *Settings → Advanced → Clear
activity tab data* in MetaMask to reset its nonce cache.

## Deploy to Sepolia

1. Get Sepolia ETH from a faucet for your deployer account.
2. Store the deployer key in Foundry's encrypted keystore (it never goes in a file in the repo):
   ```bash
   cast wallet import milefund-deployer --interactive
   ```
3. Configure and deploy (`--verify` publishes the source on Etherscan):
   ```bash
   cp .env.example .env    # set SEPOLIA_RPC_URL and ETHERSCAN_API_KEY
   source .env
   forge script script/Deploy.s.sol --rpc-url sepolia --account milefund-deployer --broadcast --verify
   ```
   This writes `deployments/11155111.json` (address and start block).
4. Run the backend against Sepolia:
   ```bash
   cd backend && cp .env.example .env
   # set RPC_URL to the Sepolia endpoint and CONFIRMATIONS=2
   .venv/bin/python app.py
   ```

### Hosting (Render)

`render.yaml` is a Render Blueprint. After deploying to Sepolia and committing
`deployments/11155111.json`: Render dashboard → **New → Blueprint** → select this repository.
The service installs `backend/requirements.txt` and starts
`gunicorn --chdir backend -w 1 --threads 8 "app:create_app()"` (a single worker so only one
indexer thread runs).

On the free plan the disk is ephemeral and the service sleeps after 15 minutes idle. Chain events
are re-indexed automatically on start-up, but uploaded descriptions and proofs are lost on each
restart unless a persistent disk is attached (paid plan; set `DATABASE_PATH` to a file on the disk).

## Testing

```bash
forge test                     # 38 contract tests incl. fuzz tests (512 runs each)
forge test --gas-report        # gas usage per function
forge coverage                 # 100% line / 94.8% statement / 77.6% branch coverage
cd backend && .venv/bin/python -m pytest -q tests   # 18 backend tests, no chain needed
```

The contract tests cover the happy path, every revert condition, weighted voting, quorum,
resubmission, inactivity failure, pro-rata refunds after partial release, a reentrancy attack
contract, and fuzzed invariants (refunds + payouts never exceed funds raised; full release sums to
exactly `totalRaised`). The backend tests cover input validation, signature checks, tamper
detection, rate limiting and security headers. `seed_local.py` is an end-to-end check against a
live Anvil chain.

## REST API

| Method | Path | Description |
|---|---|---|
| GET | `/api/config` | chain ID, contract address, ABI, contract constants |
| GET | `/api/health` | chain head and indexer progress |
| GET | `/api/campaigns` | campaign list (live chain state + verified metadata) |
| GET | `/api/campaigns/<id>?viewer=<addr>` | full campaign, milestones, proofs, viewer's position |
| GET | `/api/campaigns/<id>/activity` | indexed events for a campaign |
| GET | `/api/history/<addr>` | a user's tracked transactions and events |
| POST | `/api/metadata` | upload a signed description or proof `{content, signature}` |
| GET | `/api/metadata/<hash>` | fetch a stored document |
| POST | `/api/transactions` | report a sent transaction for status tracking |
| GET | `/api/transactions/<hash>` | status of a tracked transaction |

## Security measures

| Concern | Measure |
|---|---|
| Authentication / authorization | `onlyCreator` modifier; votes restricted to backers; metadata uploads require an EIP-191 signature from the author; the backend only shows documents whose author is the on-chain creator. |
| Reentrancy | Pull payments, checks-effects-interactions ordering, and OpenZeppelin `nonReentrant` on `withdraw` / `claimRefund` (covered by an attack test). |
| Vote manipulation | Contributions are frozen once funding ends, so weights cannot be bought mid-vote; one vote per backer per attempt. |
| Input validation | Custom errors for every invalid input on-chain; mirrored validation in the frontend and backend (lengths, ranges, URL schemes, address/hash formats, payload size). |
| Private keys | Keys stay in MetaMask; the backend holds none; deployment uses Foundry's encrypted keystore. |
| Data integrity | Only keccak256 hashes go on-chain; the browser recomputes hashes and can cross-check them against the chain via the wallet. |
| Web security | Strict Content-Security-Policy (scripts from own origin only), user content rendered as text (no `innerHTML`), per-IP rate limiting, `nosniff`, `no-referrer`. |
| Forged history | Reported transactions are checked against the chain (sender and target contract) and discarded if they do not match. |
| Gas | Storage packing (campaign struct fits in 5 slots), custom errors instead of revert strings, `calldata` arrays, bounded loops (≤ 5 milestones), long text kept off-chain. |
| Privacy | Only public addresses and voluntarily published text are stored; no personal data is collected. |

## Limitations

- A creator could back their own campaign from a second address and vote with that stake.
- Voting weight is proportional to money contributed, so large backers dominate (plutocratic voting).
- Rounding in pro-rata refunds can leave a few wei in the contract.
- Off-chain metadata depends on the backend being available (integrity is verifiable, availability is not).
- The indexer polls one RPC endpoint; a production system would use a dedicated indexer.
