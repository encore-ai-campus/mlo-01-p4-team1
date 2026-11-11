"""Native data-host stop/recovery stages after common shutdown preservation proof."""
from pathlib import Path
import sys
from .journal import atomic
from .model import ROOT, need, digest
sys.path.insert(0, str(ROOT / "aws-infra/contracts"))


def stop_native(inventory, targets, backends, guard, evidence, issuers, directory):
    from infra_common.coordination import require_operation
    from infra_common.evidence import validate_shutdown
    wanted = {n["name"] for n in inventory["nodes"] if n["role"] in {"kafka", "spark", "elasticsearch"}}
    need(set(targets) == set(backends) == wanted and len(wanted) == 9, "complete native nine-host set required")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = directory / "native-shutdown.json"
    need(not journal.exists(), "native shutdown journal exists; reconcile before retry")
    token = require_operation(guard, "destroy")
    state = {"schema_version": "native-shutdown-v1", "generation": inventory["generation"],
             "operation_id": token["operation_id"], "epoch": token["epoch"],
             "completed": [], "pending": None, "complete": False, "ready_for_aws": False}
    atomic(journal, state)
    # All workers/masters stop before *any* ZooKeeper member. A per-host stop
    # loop would tear down the quorum while another Spark host still runs.
    phases = [
        {"mlo-kafka.service", "mlo-elasticsearch.service"},
        {"mlo-spark-worker.service"},
        {"mlo-spark-master.service"},
        {"mlo-zookeeper.service"},
    ]
    for names in phases:
        for name in sorted(wanted):
            node, backend = targets[name], backends[name]
            units = {k: v for k, v in node["units"].items() if k in names}
            if not units:
                continue
            current = require_operation(guard, "destroy")
            need(current["operation_id"] == state["operation_id"] and current["epoch"] == state["epoch"], "native stop operation changed")
            proof = evidence() if callable(evidence) else evidence
            validate_shutdown(proof, inventory, current, issuers)
            need((backend.binding["node"], backend.binding["generation"]) == (name, inventory["generation"]), "native backend identity mismatch")
            if not inventory["example_only"]:
                actual = next(n["instance_id"] for n in inventory["nodes"] if n["name"] == name)
                need(backend.binding.get("instance_id") == actual, "native backend instance mismatch")
            backend.mount()
            state["pending"] = {"node": name, "units": sorted(units), "evidence_sha256": digest(proof)}
            atomic(journal, state)
            backend.stop({**node, "units": units})
            backend.stopped(units)
            state["completed"].append(state["pending"])
            state["pending"] = None
            atomic(journal, state)
    require_operation(guard, "destroy")
    state["complete"] = True
    atomic(journal, state)
    return state


def recover_native(targets, backends, guard, verify_preservation):
    """Start coordination and all daemons, check all health, keep writers closed.

    No driver is admitted here. A fresh lease and the separate maintenance
    barrier must authorize writer admission after data/backup verification.
    """
    from infra_common.coordination import require_operation
    need(set(targets) == set(backends) and len(targets) == 9, "complete native recovery cohort required")
    require_operation(guard, "deploy")
    need(verify_preservation() is True, "native restore/preservation verification required")
    for name in targets:
        need(backends[name].binding["node"] == name and backends[name].binding["generation"] == targets[name]["generation"], "native recovery binding mismatch")
        backends[name].mount()
    for names in ({"mlo-zookeeper.service"}, {"mlo-spark-master.service"}, {"mlo-spark-worker.service"}, {"mlo-kafka.service", "mlo-elasticsearch.service"}):
        for name, node in sorted(targets.items()):
            require_operation(guard, "deploy")
            units = {k: v for k, v in node["units"].items() if k in names}
            if units:
                backends[name].start({**node, "units": units})
    health = {}
    for name, node in targets.items():
        require_operation(guard, "deploy")
        health[name] = backends[name].health(node)
        need(health[name].get("ready") is True, "native recovery health failed; writers remain blocked")
    need(verify_preservation() is True, "native preservation changed during recovery")
    return {"schema_version": "native-recovery-v1", "health": health, "writers_admitted": False, "ready_for_aws": False}
