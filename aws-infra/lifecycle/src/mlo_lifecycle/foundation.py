"""Consume all seven Foundation snapshot exports without narrowing to 81 fixtures."""

import sys
from .model import ROOT, digest, need

sys.path.insert(0, str(ROOT / "aws-infra/foundation/scripts"))
from lifecycle_export import sha, audit_removal


def review_snapshots(snapshots, mode="retain-data"):
    need(
        set(snapshots)
        == {
            "encore1",
            "encore2",
            "encore3",
            "encore4",
            "encore5",
            "connectivity",
            "bootstrap",
        },
        "all Foundation roots required",
    )
    need(mode in {"retain-data", "purge-data"}, "unknown retention policy")
    targets = {}
    generations = set()
    sources = set()
    for root, snapshot in snapshots.items():
        need(
            snapshot["root"] == root
            and sha({k: v for k, v in snapshot.items() if k != "sha256"})
            == snapshot["sha256"],
            "Foundation export digest/root mismatch",
        )
        need(not snapshot["unknown"], "unclassified Foundation resource")
        generations.add(snapshot["binding"]["generation"])
        sources.add(snapshot["source"])
        for address, resource in snapshot["resources"].items():
            need(
                resource["kind"] is not None and resource["address"] == address,
                "unknown address/classification",
            )
            targets[root + ":" + address] = {
                **resource,
                "root": root,
                "dependencies": [root + ":" + d for d in resource["dependencies"]],
            }
    need(len(generations) == 1 and len(sources) == 1, "mixed generation/provenance")
    need(
        sum(r["kind"] == "compute" for r in targets.values()) == 16,
        "Foundation full-set compute count mismatch",
    )
    retained = {
        k
        for k, r in targets.items()
        if r["kind"] == "backend" or mode == "retain-data" and r["kind"] == "data"
    }
    changed = True
    while changed:
        before = set(retained)
        for key in list(retained):
            retained.update(targets[key]["dependencies"])
        # Keep encryption/versioning/public-access/ownership/policy resources of retained buckets.
        retained.update(
            k
            for k, r in targets.items()
            if r["kind"] == "data-control" and set(r["dependencies"]) & retained
        )
        changed = before != retained
    deleted = set(targets) - retained
    need(
        not deleted & retained and deleted | retained == set(targets),
        "whole-set retention inequality",
    )
    return {
        "schema_version": "foundation-retention-review-v1",
        "ready_for_aws": False,
        "execution_enabled": False,
        "source": next(iter(sources)),
        "generation": next(iter(generations)),
        "mode": mode,
        "targets": targets,
        "delete": sorted(deleted),
        "retain": sorted(retained),
        "protection_release": sorted(k for k in deleted if targets[k]["protected"]),
        "snapshot_digests": {r: s["sha256"] for r, s in snapshots.items()},
        "blockers": [
            "reviewed saved plan and exact configuration/protection release",
            "fresh live identity/state/provider observations",
            "remote backup and shutdown acceptance",
        ],
    }


def review_root_plan(review, snapshots, root, candidate, saved_digest):
    need(
        review["snapshot_digests"] == {r: s["sha256"] for r, s in snapshots.items()},
        "review snapshot changed",
    )

    def addresses(field):
        return [k.split(":", 1)[1] for k in review[field] if k.startswith(root + ":")]

    return audit_removal(
        snapshots[root],
        candidate,
        delete=addresses("delete"),
        retain=addresses("retain"),
        protection_release=addresses("protection_release"),
        saved_plan_sha256=saved_digest,
    )
