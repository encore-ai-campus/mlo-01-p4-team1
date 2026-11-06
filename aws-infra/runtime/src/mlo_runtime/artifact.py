"""Runtime compatibility wrapper around the one common artifact verifier."""
import argparse
import sys
from .contracts import ROOT, ContractError

sys.path.insert(0, str(ROOT / "aws-infra/contracts"))


def inspect_artifact(archive, expected_sha256, environment, generation, node,
                     destination, expected_manifest_sha256=None):
    from artifact_verifier import inspect_artifact as shared_inspect
    try:
        return shared_inspect(archive, expected_sha256, environment, generation,
                              node, destination, expected_manifest_sha256)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ["archive", "sha256", "manifest-sha256", "environment", "generation", "node", "destination"]:
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args()
    inspect_artifact(args.archive, args.sha256, args.environment, args.generation,
                     args.node, args.destination, args.manifest_sha256)


if __name__ == "__main__":
    main()
