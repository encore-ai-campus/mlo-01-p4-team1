#!/usr/bin/env python3
"""Offline shared-contract checks. This module never invokes AWS tools or SDKs."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "aws-infra/contracts"


class ContractError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ContractError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_object)


def validate_schema(data, filename):
    schema = read_json(CONTRACTS / filename)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = list(validator.iter_errors(data))
    if errors:
        e = errors[0]
        location = "/".join(map(str, e.absolute_path)) or "<root>"
        # Do not echo the failed value: callers may check a private inventory.
        raise ContractError(f"{filename}: {location}: {e.validator} failed")


def validate_inventory(data):
    validate_schema(data, "inventory.schema.json")
    topology = read_json(CONTRACTS / "topology.v1.json")
    expected = {n["name"]: n for n in topology["nodes"]}
    names = [n["name"] for n in data["nodes"]]
    require(len(set(names)) == len(names), "duplicate node names")
    require(set(names) == set(expected), "node set differs from approved topology")
    require(len(set(data["az_ids"].values())) == 3, "three distinct physical AZ IDs required")
    networks = {}
    for account, details in data["accounts"].items():
        try:
            net = ipaddress.ip_network(details["cidr"], strict=True)
        except ValueError as exc:
            raise ContractError(f"invalid VPC network for {account}") from exc
        private_blocks = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
        require(net.version == 4 and any(net.subnet_of(block) for block in private_blocks), f"RFC1918 IPv4 VPC required for {account}")
        for other, network in networks.items():
            require(not net.overlaps(network), f"overlapping VPCs: {account}/{other}")
        networks[account] = net
    for field in ("vpc_id", "s3_endpoint_id"):
        values = [v[field] for v in data["accounts"].values()]
        require(len(set(values)) == 5, f"duplicate {field} across accounts")
    instance_ids, addresses = set(), set()
    for node in data["nodes"]:
        for key, value in expected[node["name"]].items():
            require(node[key] == value, f"{node['name']}: unexpected {key}")
        require(node["az_id"] == data["az_ids"][node["az_slot"]], f"{node['name']}: AZ mapping mismatch")
        address = ipaddress.ip_address(node["private_ip"])
        require(address in networks[node["account"]], f"{node['name']}: IP outside its VPC")
        require(address not in addresses, "duplicate private IP")
        require(node["instance_id"] not in instance_ids, "duplicate instance ID")
        require(node["runtime_role_ref"].startswith("role-ref:"), "logical role-ref required")
        addresses.add(address)
        instance_ids.add(node["instance_id"])
    pairs = [tuple(sorted(p)) for p in data["peering_pairs"]]
    require(len(set(pairs)) == len(pairs), "duplicate or reversed duplicate peering")
    require(set(pairs) == {tuple(p) for p in topology["peering_pairs"]}, "missing direct peering")
    buckets = list(data["storage"].values())
    require(len(set(buckets)) == len(buckets), "storage purposes must have separate bucket refs")
    return data


def node_services(node, services):
    return sorted(
        name for name, spec in services.items()
        if node["role"] in spec["roles"] and ("nodes" not in spec or node["name"] in spec["nodes"])
    )


def port_interval(spec):
    require(("port" in spec) != ("port_range" in spec), "exactly one port/port_range required")
    low, high = (spec["port"], spec["port"]) if "port" in spec else spec["port_range"]
    require(type(low) is int and type(high) is int and 1 <= low <= high <= 65535, "invalid port range")
    return low, high


def validate_services(specification, inventory):
    require(specification["schema_version"] == "1.0.0", "service contract version mismatch")
    roles = {n["role"] for n in inventory["nodes"]}
    services = specification["services"]
    for name, spec in services.items():
        require(set(spec["roles"]) <= roles and bool(spec["roles"]), f"unknown role in {name}")
        require(spec["protocol"] == "tcp", f"unexpected protocol in {name}")
        require(spec["exposure"] in {"private", "host-and-container-bridge"}, f"public service exposure: {name}")
        port_interval(spec)
        if "nodes" in spec:
            allowed = {n["name"] for n in inventory["nodes"] if n["role"] in spec["roles"]}
            require(set(spec["nodes"]) <= allowed, f"invalid node selector in {name}")
    for node in inventory["nodes"]:
        bindings = []
        for name in node_services(node, services):
            low, high = port_interval(services[name])
            for previous, previous_low, previous_high in bindings:
                require(high < previous_low or previous_high < low, f"port collision on {node['name']}: {previous}/{name}")
            bindings.append((name, low, high))
    for flow in specification["flows"]:
        target = flow["destination_service"]
        require(target in services, "flow has unknown destination service")
        require(bool(flow["source_roles"]) and set(flow["source_roles"]) <= roles | {"alb"}, "flow has unknown source")
        require(services[target]["exposure"] == "private", "host proxy is not a cross-VPC SG destination")
    return specification


def validate_runtime_plan(plan, inventory, specification):
    validate_schema(plan, "runtime-plan.schema.json")
    for key in ("example_only", "environment", "generation"):
        require(plan[key] == inventory[key], f"runtime plan {key} mismatch")
    nodes = {n["name"]: n for n in inventory["nodes"]}
    names = [p["node"] for p in plan["node_plans"]]
    require(len(set(names)) == len(names) and set(names) == set(nodes), "runtime node set mismatch")
    for p in plan["node_plans"]:
        node = nodes[p["node"]]
        require(p["role"] == node["role"], f"runtime role mismatch: {p['node']}")
        require(sorted(p["services"]) == node_services(node, specification["services"]), f"runtime services mismatch: {p['node']}")


def normalized_path(path):
    p = PurePosixPath(path)
    require(not p.is_absolute() and ".." not in p.parts and "\\" not in path, "unsafe ownership path")
    return str(p)


def validate_ownership(specification):
    files, prefixes = {}, []
    for owner, rule in specification["owners"].items():
        for raw in rule["files"]:
            path = normalized_path(raw)
            require(path not in files, f"duplicate exact ownership: {path}")
            files[path] = owner
        for prefix in rule["prefixes"]:
            require(prefix.endswith("/"), "ownership prefix must end with slash")
            normalized_path(prefix)
            for previous in prefixes:
                require(not prefix.startswith(previous) and not previous.startswith(prefix), "overlapping ownership prefixes")
            prefixes.append(prefix)


def owner_of(path, specification):
    path = normalized_path(path)
    for owner, rule in specification["owners"].items():
        if path in rule["files"]:
            return owner
    owners = [owner for owner, rule in specification["owners"].items() if any(path.startswith(p) for p in rule["prefixes"])]
    require(len(owners) <= 1, f"ambiguous ownership: {path}")
    return owners[0] if owners else None


def check_owner_paths(owner, paths, specification):
    for path in paths:
        require(owner_of(path, specification) == owner, f"{owner} does not own {path}")
        candidate = ROOT / path
        require(candidate.resolve().is_relative_to(ROOT), f"path escapes worktree: {path}")
        require(candidate.resolve() == candidate.absolute(), f"symlink ownership shortcut: {path}")


def git_paths(*args):
    p = subprocess.run(["git", *args], cwd=ROOT, text=False, capture_output=True, check=False)
    require(p.returncode == 0, "git read failed; verify the baseline commit")
    return [value.decode() for value in p.stdout.split(b"\0") if value]


def public_paths():
    return sorted(set(git_paths("ls-files", "-c", "-o", "--exclude-standard", "-z")))


def check_documents_and_paths(ownership):
    for path in public_paths():
        require(owner_of(path, ownership) is not None, f"unassigned repository path: {path}")
        file = ROOT / path
        require(file.resolve().is_relative_to(ROOT), f"repository symlink escapes root: {path}")
        if file.suffix != ".md" or not file.is_file():
            continue
        content = file.read_text()
        require(content.endswith("\n"), f"missing final newline: {path}")
        require(len(re.findall(r"^```", content, re.M)) % 2 == 0, f"unclosed code fence: {path}")
        for raw in re.findall(r"\[[^\]]*\]\(([^)]+)\)", content):
            target = raw.strip("<>").split("#", 1)[0]
            if not target or re.match(r"[a-z]+://", target):
                continue
            require((file.parent / target).exists(), f"broken Markdown link: {path}: {target}")


def run_pipeline(root=ROOT):
    producer = root / "aws-infra/foundation/scripts/render-fixture.sh"
    consumer = root / "aws-infra/runtime/scripts/render-fixture.sh"
    require(producer.is_file() and consumer.is_file(), "NOT READY: both role render-fixture.sh implementations are required")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull, AWS_EC2_METADATA_DISABLED="true")
    with tempfile.TemporaryDirectory(prefix="mlo-p4-contract-") as directory:
        inventory_path = Path(directory) / "inventory.json"
        plan_path = Path(directory) / "runtime-plan.json"
        for command in (["bash", str(producer), "--output", str(inventory_path)], ["bash", str(consumer), "--inventory", str(inventory_path), "--output", str(plan_path)]):
            p = subprocess.run(command, cwd=root, env=env, check=False)
            require(p.returncode == 0, "producer/consumer implementation failed")
        inventory = validate_inventory(read_json(inventory_path))
        require(inventory["example_only"], "pipeline requires an example-only inventory")
        services = validate_services(read_json(CONTRACTS / "services.v1.json"), inventory)
        validate_runtime_plan(read_json(plan_path), inventory, services)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=CONTRACTS / "inventory.example.json")
    parser.add_argument("--runtime-plan", type=Path)
    parser.add_argument("--owner", choices=["A", "B", "C"])
    parser.add_argument("--base")
    parser.add_argument("--pipeline", action="store_true")
    args = parser.parse_args()
    if bool(args.owner) != bool(args.base):
        parser.error("--owner and --base must be supplied together")
    try:
        inventory = validate_inventory(read_json(args.inventory))
        services = validate_services(read_json(CONTRACTS / "services.v1.json"), inventory)
        plan = args.runtime_plan or (CONTRACTS / "runtime-plan.example.json" if args.inventory == CONTRACTS / "inventory.example.json" else None)
        if plan:
            validate_runtime_plan(read_json(plan), inventory, services)
        ownership = read_json(CONTRACTS / "ownership.v1.json")
        validate_ownership(ownership)
        check_documents_and_paths(ownership)
        if args.owner:
            paths = set(git_paths("diff", "--name-only", "-z", args.base, "--"))
            paths.update(git_paths("ls-files", "--others", "--exclude-standard", "-z"))
            check_owner_paths(args.owner, sorted(paths), ownership)
        if args.pipeline:
            run_pipeline()
        print("PASS: shared contracts, 16-node fixture, topology, services, ownership and document links")
        return 0
    except (ContractError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
