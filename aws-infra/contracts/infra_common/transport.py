"""Accepted regional STS routes; explicit proxy selection, never TLS interception."""

import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .coordination import require

REGION = "ap-northeast-2"
STS_ENDPOINT = "https://sts.ap-northeast-2.amazonaws.com"


def metadata_provider():
    """IMDSv2 only, with a direct HTTP session independent of ambient proxies.

    In pinned botocore 1.42.30, IMDSFetcher's env argument controls disabling,
    while its HTTP session still reads proxies from os.environ. Replace that
    session explicitly; never change global proxy settings for other threads.
    """
    import botocore
    from botocore.credentials import InstanceMetadataProvider
    from botocore.httpsession import URLLib3Session
    from botocore.utils import InstanceMetadataFetcher

    require(
        botocore.__version__ == "1.42.30",
        "review pinned metadata SDK adapter before upgrading",
    )
    fetcher = InstanceMetadataFetcher(
        timeout=2,
        num_attempts=1,
        env={"AWS_EC2_METADATA_DISABLED": "false"},
        config={
            "ec2_metadata_v1_disabled": True,
            "ec2_metadata_service_endpoint_mode": "ipv4",
        },
    )
    fetcher._session.close()
    fetcher._session = URLLib3Session(timeout=2, proxies={})
    return InstanceMetadataProvider(iam_role_fetcher=fetcher)


def routes(spec, scope):
    """Validate before credentials or clients can be discovered.

    Gateways have public egress. Every other host uses the accepted private
    Gateway addresses directly, so first install has no host-HAProxy dependency.
    """
    node = spec.get("node")
    account = spec.get("account_alias")
    expected = spec.get("instance_role_name")
    require(account in scope["account_ids"], "exact host account required")
    require(
        isinstance(node, str) and re.fullmatch(r"[a-z][a-z0-9-]{1,63}", node),
        "exact host node required",
    )
    topology = json.loads(
        (Path(__file__).resolve().parents[1] / "topology.v1.json").read_text()
    )
    nodes = {n["name"]: n for n in topology["nodes"]}
    require(
        node in nodes and nodes[node]["account"] == account,
        "node/account topology mismatch",
    )
    require(expected == "p4-runtime-" + node, "runtime role must match host node")
    route = spec.get("sts_route", {})
    require(isinstance(route, dict), "STS route required")
    if node in {"gateway-a", "gateway-b"}:
        require(
            account == "encore5" and route == {"mode": "regional-direct"},
            "Gateway STS route mismatch",
        )
        return [{}]
    require(
        set(route) == {"mode", "gateway_addresses"}
        and route["mode"] == "gateway-connect",
        "private host requires Gateway CONNECT",
    )
    addresses = route["gateway_addresses"]
    require(
        isinstance(addresses, list)
        and len(addresses) == 2
        and len(set(addresses)) == 2,
        "two exact Gateway addresses required",
    )
    result = []
    for address in addresses:
        require(isinstance(address, str), "invalid Gateway address")
        parsed = urlsplit(address)
        require(
            parsed.scheme == "http"
            and parsed.port == 3128
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment,
            "invalid Gateway proxy URL",
        )
        try:
            ip = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError(
                "Gateway proxy must use an accepted private IPv4 address"
            ) from exc
        require(
            ip.version == 4
            and any(
                ip in ipaddress.ip_network(cidr)
                for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            ),
            "Gateway proxy must be RFC1918 IPv4",
        )
        result.append({"https": address})
    return result


def verify_host(session, spec, scope):
    """Only GetCallerIdentity is retried across approved CONNECT paths."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import (
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ProxyConnectionError,
        ReadTimeoutError,
    )

    approved = routes(spec, scope)
    transient = (
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ProxyConnectionError,
        ReadTimeoutError,
    )
    for index, proxies in enumerate(approved):
        client = boto3.Session(botocore_session=session).client(
            "sts",
            region_name=REGION,
            endpoint_url=STS_ENDPOINT,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"total_max_attempts": 1, "mode": "standard"},
                proxies=proxies,
            ),
        )
        try:
            identity = client.get_caller_identity()
        except transient:
            if index + 1 == len(approved):
                raise
            continue
        finally:
            client.close()
        account = scope["account_ids"][spec["account_alias"]]
        require(
            identity["Account"] == account
            and re.fullmatch(
                r"arn:aws:sts::"
                + account
                + r":assumed-role/"
                + re.escape(spec["instance_role_name"])
                + r"/[A-Za-z0-9+=,.@_-]+",
                identity["Arn"],
            ),
            "instance credential identity mismatch",
        )
        return identity
    raise ValueError("no approved STS route")
