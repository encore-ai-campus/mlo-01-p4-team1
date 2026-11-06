"""Consume the shared checker rather than duplicating its schemas."""

from pathlib import Path
import sys

RUNTIME = Path(__file__).resolve().parents[2]
ROOT = RUNTIME.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_preparation import (  # noqa: E402
    ContractError,
    read_json,
    require,
    validate_inventory,
    validate_services,
    validate_runtime_plan,
    node_services,
)


def load_inventory(path):
    inventory = validate_inventory(read_json(path))
    services = validate_services(
        read_json(ROOT / "aws-infra/contracts/services.v1.json"), inventory
    )
    return inventory, services
