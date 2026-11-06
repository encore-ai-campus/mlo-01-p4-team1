"""inspect/plan/execute/resume/verify: synthetic inputs and local adapters only."""

import argparse
import json
import os
from pathlib import Path
import sys

from .adapter import Simulator, initialize
from .cli_io import InputSnapshots, OutputReservation, safe_error
from .engine import execute, verify
from .journal import atomic, authority_key
from .model import BASE, Refused, need, read, validate_bundle
from .planner import plan, validate_plan


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo arbitrary argv values, including a pasted secret.
        value = safe_error(Refused("invalid CLI arguments", phase="arguments"), "arguments", None)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)


def guard():
    # No CLI path can invoke AWS/Terraform/shell or even open a network socket.
    hook = str(BASE / "scripts/stateful-hook.py")

    def audit(event, args):
        if event.startswith("socket."):
            raise Refused("network is forbidden in lifecycle CLI")
        if event in {"os.system", "os.exec", "os.posix_spawn"}:
            raise Refused("external command is forbidden")
        if event == "subprocess.Popen":
            argv = args[1]
            need(args[0] == hook and isinstance(argv, (list, tuple)) and argv[0] == hook
                 and len(argv) in (7, 11) and argv[1] == "--operation"
                 and argv[2] in {"quiesce", "backup", "copy", "verify-copy", "verify-restore", "stop", "observe-stopped"}
                 and argv[3] == "--node" and argv[4] in {"postgresql", "mongodb"}
                 and argv[5] == "--generation", "unexpected process/argv refused")
    sys.addaudithook(audit)
    for key in list(os.environ):
        if key.startswith(("AWS_", "TF_")) or key.lower().endswith("_proxy"):
            del os.environ[key]
    os.environ.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull,
                      AWS_EC2_METADATA_DISABLED="true", PATH=str(Path(sys.executable).parent) + ":/usr/bin:/bin")


def main():
    guard()
    parser = SafeParser(description=__doc__)
    parser.add_argument("operation", choices=["inspect", "plan", "lab-init", "execute", "resume", "verify", "observe"])
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--mode", choices=["retain-data", "purge-data"], default="retain-data")
    parser.add_argument("--adapter", choices=["local-simulator"], default="local-simulator")
    for name in ("plan", "lab", "approval", "evidence", "authority-key", "output", "reconciliation"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--step")
    parser.add_argument("--fault-step")
    parser.add_argument("--fault", choices=["before", "timeout", "crash"])
    args = parser.parse_args()
    output = OutputReservation(args.output)
    operation_complete = False
    try:
        snapshots = InputSnapshots((args.bundle, args.plan, args.approval, args.evidence,
                                    args.authority_key, args.reconciliation))
        output.reserve()
        bundle = read(args.bundle)
        targets = validate_bundle(bundle)
        if args.operation == "inspect":
            result = {"ready_for_aws": False, "targets": sorted(targets), "unknown": [],
                      "untracked_data": bundle["binding"]["untracked_data"],
                      "backend_preserved": [k for k, r in targets.items() if r["kind"] == "backend"]}
        elif args.operation == "plan":
            result = plan(bundle, args.mode)
        elif args.operation == "lab-init":
            need(args.lab is not None, "--lab required")
            initialize(args.lab, bundle)
            result = {"ready_for_aws": False, "initialized": True}
        else:
            need(args.plan is not None and args.lab is not None, "--plan and --lab required")
            reviewed = read(args.plan)
            if args.operation == "observe":
                validate_plan(reviewed, bundle, reporting=True)
                step = next((s for s in reviewed["steps"] if s["id"] == args.step), None)
                need(step is not None, "unknown step")
                result = Simulator(args.lab, bundle).observe(step)
            else:
                need(args.authority_key is not None, "--authority-key required")
                key = authority_key(args.authority_key)
                if args.operation == "verify":
                    result = verify(reviewed, bundle, args.lab, key)
                else:
                    need(args.approval is not None and args.evidence is not None, "approval and evidence required")
                    result = execute(reviewed, bundle, args.lab, read(args.approval), read(args.evidence), key,
                                     resume=args.operation == "resume",
                                     reconciliations=read(args.reconciliation) if args.reconciliation else (),
                                     fault=[args.fault_step, args.fault] if args.fault else None,
                                     input_guard=snapshots.check)
        operation_complete = True
        print(json.dumps(result, sort_keys=True))
        output.publish(result)
        return 3 if result.get("complete") is False else 0
    except Exception as exc:
        print(json.dumps(safe_error(exc, args.operation, args.lab, reporting=operation_complete),
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        output.close()


if __name__ == "__main__":
    raise SystemExit(main())
