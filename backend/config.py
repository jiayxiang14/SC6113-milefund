"""Runtime configuration loaded from environment variables (and an optional .env file)."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent

load_dotenv(BACKEND_DIR / ".env")

KNOWN_NETWORKS = {
    1: ("Ethereum Mainnet", "https://etherscan.io"),
    11155111: ("Sepolia", "https://sepolia.etherscan.io"),
    31337: ("Anvil (local)", ""),
}


@dataclass
class Config:
    rpc_url: str
    contract_address: str | None
    start_block: int
    database_path: str
    index_interval: float
    index_chunk: int
    confirmations: int
    rate_limit_per_minute: int
    abi_path: str
    deployments_dir: str
    explorer_url: str | None

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            rpc_url=os.getenv("RPC_URL", "http://127.0.0.1:8545"),
            contract_address=os.getenv("CONTRACT_ADDRESS") or None,
            start_block=int(os.getenv("START_BLOCK", "0")),
            database_path=os.getenv("DATABASE_PATH", str(BACKEND_DIR / "milefund.db")),
            index_interval=float(os.getenv("INDEX_INTERVAL", "4")),
            index_chunk=int(os.getenv("INDEX_CHUNK", "2000")),
            confirmations=int(os.getenv("CONFIRMATIONS", "0")),
            rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")),
            abi_path=os.getenv("ABI_PATH", str(BACKEND_DIR / "abi" / "MilestoneCrowdfund.json")),
            deployments_dir=os.getenv("DEPLOYMENTS_DIR", str(PROJECT_DIR / "deployments")),
            explorer_url=os.getenv("EXPLORER_URL") or None,
        )

    def resolve_deployment(self, chain_id: int) -> None:
        """Fill in the contract address and start block from deployments/<chainId>.json
        (written by script/Deploy.s.sol) unless they were set explicitly."""
        if self.contract_address:
            return
        path = Path(self.deployments_dir) / f"{chain_id}.json"
        if not path.exists():
            raise RuntimeError(
                f"No CONTRACT_ADDRESS configured and {path} does not exist. "
                "Deploy the contract first (see README)."
            )
        data = json.loads(path.read_text())
        self.contract_address = data["address"]
        if self.start_block == 0:
            self.start_block = int(data.get("startBlock", 0))
