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
    parser.add_argument("operation", choices=["inspect", "plan", "lab-init", "execute", "resume", "verify", "observe",
                                              "restore-track2", "resume-track2", "observe-track2", "verify-track2"])
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--mode", choices=["retain-data", "purge-data"], default="retain-data")
    parser.add_argument("--adapter", choices=["local-simulator"], default="local-simulator")
    for name in ("plan", "lab", "approval", "evidence", "authority-key", "output", "reconciliation", "recovery-approval"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--step")
    parser.add_argument("--fault-step")
    parser.add_argument("--fault", choices=["before", "timeout", "crash"])
    parser.add_argument("--rollback-release", action="store_true")
    args = parser.parse_args()
    output = OutputReservation(args.output)
    operation_complete = False
    try:
        snapshots = InputSnapshots((args.bundle, args.plan, args.approval, args.evidence,
                                    args.authority_key, args.reconciliation, args.recovery_approval))
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
            if args.operation in {"observe", "observe-track2"}:
                validate_plan(reviewed, bundle, reporting=True)
                if args.operation == "observe-track2":
                    from .recovery import observe
                    need("track2" in bundle, "Track2 bundle required")
                    result = observe(args.lab, bundle["track2"], args.step, rollback=args.rollback_release)
                else:
                    step = next((s for s in reviewed["steps"] if s["id"] == args.step), None)
                    need(step is not None, "unknown step")
                    result = Simulator(args.lab, bundle).observe(step)
            else:
                need(args.authority_key is not None, "--authority-key required")
                key = authority_key(args.authority_key)
                if args.operation == "verify-track2":
                    from .recovery import verify_recovery
                    result = verify_recovery(reviewed, bundle, args.lab, key, rollback=args.rollback_release)
                elif args.operation == "verify":
                    result = verify(reviewed, bundle, args.lab, key)
                else:
                    need(args.approval is not None and args.evidence is not None, "approval and evidence required")
                    if args.operation in {"restore-track2", "resume-track2"}:
                        from .recovery import recover
                        need(args.recovery_approval is not None, "separate recovery approval required")
                        result = recover(reviewed, bundle, args.lab, read(args.approval), read(args.evidence), key,
                                         read(args.recovery_approval), rollback=args.rollback_release,
                                         resume=args.operation == "resume-track2",
                                         reconciliations=read(args.reconciliation) if args.reconciliation else (),
                                         fault=[args.fault_step, args.fault] if args.fault else None,
                                         input_guard=snapshots.check)
                    else:
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
        error_lab = (args.lab / "track2-recovery" if args.lab is not None
                     and args.operation in {"restore-track2", "resume-track2", "observe-track2", "verify-track2"}
                     else args.lab)
        print(json.dumps(safe_error(exc, args.operation, error_lab, reporting=operation_complete),
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        output.close()


if __name__ == "__main__":
    raise SystemExit(main())
