"""Fixed-argv adapter for planned DB stops; operation hooks are accepted inputs."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import argparse

from .operations import UnsafeTransition


class CommandStatefulBackend:
    """Execute one hash-bound hook with immutable node/generation binding.

    The hook implements DB-specific quiesce/dump/copy/restore operations. It is
    part of the accepted artifact, not a shell command from an API request.
    Receipts carry locations/digests only; credentials stay in protected files.
    """

    def __init__(self, binding, folder):
        self.binding, self.folder = binding, Path(folder)

    def _run(self, node, action, previous=None):
        if node != self.binding["node"] or not re.fullmatch(r"[A-Za-z0-9_.-]+", node):
            raise UnsafeTransition("stateful target identity mismatch")
        hook = Path(self.binding["hook"])
        if (
            not hook.is_absolute()
            or hook.is_symlink()
            or hashlib.sha256(hook.read_bytes()).hexdigest() != self.binding["sha256"]
        ):
            raise UnsafeTransition("stateful adapter checksum mismatch")
        argv = [
            str(hook),
            "--operation",
            action,
            "--node",
            node,
            "--generation",
            self.binding["generation"],
        ]
        if previous:
            argv += [
                "--backup-sha256",
                previous["sha256"],
                "--backup-location",
                previous["location"],
            ]
        result = subprocess.run(argv, check=True, capture_output=True, timeout=900)
        if len(result.stdout) > 65536:
            raise UnsafeTransition("stateful receipt exceeds bound")
        value = json.loads(result.stdout)
        if (
            value.get("node") != node
            or value.get("generation") != self.binding["generation"]
            or value.get("operation") != action
        ):
            raise UnsafeTransition("stateful operation receipt mismatch")
        return value

    def quiesce(self, node):
        return self._run(node, "quiesce")

    def backup(self, node):
        return self._run(node, "backup")

    def copy_backup(self, node, archive):
        return self._run(node, "copy", archive)

    def verify_copy(self, node, copied):
        return self._run(node, "verify-copy", copied)

    def verify_restore(self, node, copied):
        return self._run(node, "verify-restore", copied)

    def stop(self, node):
        return self._run(node, "stop")

    def observe_stopped(self, node):
        return self._run(node, "observe-stopped").get("stopped") is True


def main():
    from .operations import stop_stateful

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binding", type=Path, default=Path("/etc/mlo/stateful-stop.json")
    )
    parser.add_argument("--confirm-planned-stop")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.binding.is_symlink() or args.binding.stat().st_mode & 0o077:
        raise UnsafeTransition("stateful binding must be protected 0600")
    binding = json.loads(args.binding.read_text())
    if (
        binding.get("ready") is not True
        or binding.get("example_only") is not False
        or (not args.validate_only and args.confirm_planned_stop != binding["node"])
    ):
        raise UnsafeTransition("unaccepted planned stop binding/confirmation")
    backend = CommandStatefulBackend(binding, binding["receipt_directory"])
    if args.validate_only:
        hook = Path(binding["hook"])
        if (
            not hook.is_absolute()
            or hook.is_symlink()
            or hashlib.sha256(hook.read_bytes()).hexdigest() != binding["sha256"]
        ):
            raise UnsafeTransition("stateful adapter checksum mismatch")
        print(
            json.dumps(
                {
                    "validated": True,
                    "node": binding["node"],
                    "scope": "binding only; backup/restore not executed",
                }
            )
        )
        return
    stop_stateful(binding["node"], backend)


if __name__ == "__main__":
    main()
