"""Sanitized observation transport/configuration; business events come from apps."""

from copy import deepcopy
import datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

import yaml

from .operations import UnsafeTransition, local_lock

DATASETS = ("mlo.pipeline", "mlo.ads", "mlo.security", "mlo.service")
STATES = (
    "QUEUED",
    "STARTED",
    "SENT",
    "ACKNOWLEDGED",
    "COMMITTED",
    "SEARCHABLE",
    "PUBLISHED",
    "RETRYING",
    "FAILED",
    "QUARANTINED",
    "SUCCEEDED",
    "STALE",
    "STOPPED",
    "HEARTBEAT",
)
FIELDS = {
    "event": ("id", "dataset", "action", "outcome", "duration"),
    "service": ("name",),
    "host": ("name",),
    "trace": ("id",),
    "pipeline": (
        "name",
        "run_id",
        "stage",
        "state",
        "attempt",
        "release_id",
        "batch_id",
        "records_in",
        "records_out",
        "records_quarantined",
        "source_ref",
        "target_ref",
        "business_event_id",
    ),
    "error": ("code", "retryable"),
}
SLUG = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}\Z")
REF = re.compile(r"(?:raw|lake|es|queue|worker|archive)/[a-z0-9_./-]{1,180}\Z")


def _require(value):
    if not value:
        raise UnsafeTransition("invalid sanitized observation envelope")


def sanitize(source):
    """Rebuild only the small typed envelope; never persist arbitrary messages."""
    _require(isinstance(source, dict))
    out = {"@timestamp": source.get("@timestamp")}
    for group, names in FIELDS.items():
        if isinstance(source.get(group), dict):
            out[group] = {
                k: deepcopy(source[group][k]) for k in names if k in source[group]
            }
    try:
        stamp = datetime.datetime.fromisoformat(
            out["@timestamp"].replace("Z", "+00:00")
        )
        _require(stamp.tzinfo is not None)
        event, pipeline = out["event"], out["pipeline"]
        _require(str(uuid.UUID(event["id"])) == event["id"])
        _require(str(uuid.UUID(pipeline["run_id"])) == pipeline["run_id"])
        _require(
            event["dataset"] in DATASETS
            and event["outcome"] in {"success", "failure", "unknown"}
        )
        _require(pipeline["state"] in STATES)
        for value in (
            event["action"],
            out["service"]["name"],
            out["host"]["name"],
            pipeline["name"],
            pipeline["stage"],
        ):
            _require(isinstance(value, str) and SLUG.fullmatch(value))
        for key in (
            "attempt",
            "batch_id",
            "records_in",
            "records_out",
            "records_quarantined",
        ):
            if key in pipeline:
                _require(
                    type(pipeline[key]) is int
                    and pipeline[key] >= (1 if key == "attempt" else 0)
                )
        _require("attempt" in pipeline)
        if "duration" in event:
            _require(type(event["duration"]) is int and event["duration"] >= 0)
        for key in ("source_ref", "target_ref"):
            if key in pipeline:
                _require(
                    isinstance(pipeline[key], str)
                    and REF.fullmatch(pipeline[key])
                    and ".." not in pipeline[key]
                    and "//" not in pipeline[key]
                )
        if "release_id" in pipeline:
            _require(SLUG.fullmatch(pipeline["release_id"]))
        if "business_event_id" in pipeline:
            _require(
                str(uuid.UUID(pipeline["business_event_id"]))
                == pipeline["business_event_id"]
            )
        if "trace" in out:
            _require(re.fullmatch(r"[a-f0-9]{32}", out["trace"]["id"]))
        if "error" in out:
            _require(
                re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", out["error"]["code"])
                and type(out["error"]["retryable"]) is bool
            )
        if pipeline["state"] in {"FAILED", "QUARANTINED"}:
            _require("error" in out and event["outcome"] == "failure")
        if pipeline["state"] in {"SEARCHABLE", "PUBLISHED", "SUCCEEDED"}:
            _require(event["outcome"] == "success")
        cloud = source["cloud"]
        _require(
            cloud["provider"] == "aws"
            and cloud["region"] == "ap-northeast-2"
            and cloud["account"]["name"] in {f"encore{i}" for i in range(1, 6)}
        )
        out["cloud"] = {
            "provider": "aws",
            "region": "ap-northeast-2",
            "account": {"name": cloud["account"]["name"]},
        }
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise UnsafeTransition("invalid sanitized observation envelope") from exc
    _require(len(json.dumps(out).encode()) <= 4096)
    return out


class BoundedSpool:
    """Fail with backpressure at capacity; never silently delete unarchived logs.

    The app exporter must retain its cursor when append raises. Filebeat registry
    transport ACK is separate from ES visibility/archive acceptance.
    """

    def __init__(self, folder, max_bytes=64 * 1024 * 1024, max_files=16):
        if type(max_bytes) is not int or max_bytes < 4096 or not 1 <= max_files <= 1024:
            raise UnsafeTransition("bounded spool capacity required")
        self.folder, self.max_bytes, self.max_files = Path(folder), max_bytes, max_files
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    def append(self, event):
        line = (
            json.dumps(sanitize(event), sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
        with local_lock(self.folder / "append.lock"):
            files = sorted(self.folder.glob("*.ndjson"))
            if any(p.is_symlink() for p in files):
                raise UnsafeTransition("observation spool symlink refused")
            used = sum(p.stat().st_size for p in files)
            if used + len(line) > self.max_bytes:
                raise UnsafeTransition(
                    "observation spool pressure; retain exporter cursor"
                )
            current = files[-1] if files else self.folder / "00000000.ndjson"
            if (
                current.exists()
                and current.stat().st_size + len(line)
                > self.max_bytes // self.max_files
            ):
                if len(files) >= self.max_files:
                    raise UnsafeTransition(
                        "observation spool file bound reached; archive required"
                    )
                current = self.folder / f"{int(files[-1].stem) + 1:08d}.ndjson"
            with current.open("ab") as stream:
                current.chmod(0o600)
                stream.write(line)
                stream.flush()
                import os

                os.fsync(stream.fileno())
        return {"event_id": event["event"]["id"], "state": "QUEUED", "bytes": len(line)}


def pressure(used, capacity, last_progress, *, now=None, stale_after=120):
    now = time.time() if now is None else now
    if (
        not 0 <= used <= capacity
        or capacity <= 0
        or last_progress > now
        or stale_after <= 0
    ):
        raise UnsafeTransition("invalid observation pressure sample")
    return {
        "queue_ratio": used / capacity,
        "pressure": used >= capacity * 0.8,
        "state": "STALE" if now - last_progress >= stale_after else "HEARTBEAT",
    }


def archive_spool(source, destination, *, capacity=64 * 1024 * 1024):
    """Bounded, sanitized local archive handoff. S3 transfer is an external binding.

    Never remove the source based on a transport ACK or an unverified archive.
    A consumer must accept the checksum and its own restoration test separately.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = []
    size = 0
    for path in sorted(Path(source).glob("*.ndjson")):
        _require(not path.is_symlink())
        with path.open("rb") as stream:
            for line in stream:
                _require(len(line) <= 8192)
                item = (
                    json.dumps(
                        sanitize(json.loads(line)),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )
                size += len(item)
                _require(size <= 16 * 1024 * 1024)
                lines.append(item)
    compressed = gzip.compress(b"".join(lines), mtime=0)
    checksum = hashlib.sha256(compressed).hexdigest()
    target = destination / (checksum + ".ndjson.gz")
    with local_lock(destination / "archive.lock"):
        if not target.exists():
            if (
                sum(p.stat().st_size for p in destination.glob("*.ndjson.gz"))
                + len(compressed)
                > capacity
            ):
                raise UnsafeTransition(
                    "sanitized archive capacity reached; accepted external transfer required"
                )
            with target.open("xb") as stream:
                target.chmod(0o600)
                stream.write(compressed)
                stream.flush()
                import os

                os.fsync(stream.fileno())
        _require(hashlib.sha256(target.read_bytes()).hexdigest() == checksum)
    return {
        "path": str(target),
        "sha256": checksum,
        "records": len(lines),
        "source_retained": True,
        "s3_transfer": "not_run",
        "ready_for_aws": False,
    }


def filebeat_config(collector):
    return {
        "filebeat.inputs": [
            {
                "type": "filestream",
                "id": "mlo-sanitized-v02",
                "paths": ["/spool/*.ndjson"],
                "message_max_bytes": 8192,
                "prospector.scanner.fingerprint.length": 64,
                "parsers": [{"ndjson": {"target": "", "overwrite_keys": True}}],
            }
        ],
        "processors": [
            {
                "script": {
                    "lang": "javascript",
                    "id": "sanitized-envelope-v02",
                    "source": r"""
function process(event) {
  try {
  var fields = %s;
  var result = {"@timestamp": event.Get("@timestamp")};
  var cloud = event.Get("cloud");
  if (cloud && cloud.provider === "aws" && cloud.region === "ap-northeast-2" &&
      cloud.account && /^encore[1-5]$/.test(cloud.account.name)) {
    result.cloud = {"provider": "aws", "region": "ap-northeast-2", "account": {"name": cloud.account.name}};
  } else { event.Cancel(); return; }
  var data = event.Get("event");
  var pipe = event.Get("pipeline");
  if (!data || !pipe || %s.indexOf(data.dataset) < 0 || %s.indexOf(pipe.state) < 0 ||
      !/^[a-f0-9-]{36}$/.test(data.id) || !/^[a-f0-9-]{36}$/.test(pipe.run_id)) {
    event.Cancel(); return;
  }
  Object.keys(fields).forEach(function(group) {
    var src = event.Get(group);
    if (src) { result[group] = {}; fields[group].forEach(function(k) {
      if (src[k] !== undefined) result[group][k] = src[k];
    }); }
  });
  var safe = /^[a-z0-9][a-z0-9_.-]{0,79}$/;
  if (!result.service || !result.host || !safe.test(result.service.name) || !safe.test(result.host.name) ||
      !safe.test(data.action) || !safe.test(pipe.name) || !safe.test(pipe.stage)) { event.Cancel(); return; }
  ["source_ref", "target_ref"].forEach(function(k) {
    if (pipe[k] !== undefined && !/^(raw|lake|es|queue|worker|archive)\/[a-z0-9_./-]{1,180}$/.test(pipe[k])) event.Cancel();
  });
  if (["success", "failure", "unknown"].indexOf(data.outcome) < 0 ||
      (pipe.release_id !== undefined && !safe.test(pipe.release_id)) ||
      (pipe.business_event_id !== undefined && !/^[a-f0-9-]{36}$/.test(pipe.business_event_id))) {
    event.Cancel(); return;
  }
  ["attempt", "batch_id", "records_in", "records_out", "records_quarantined"].forEach(function(k) {
    if (pipe[k] !== undefined && (typeof pipe[k] !== "number" || Math.floor(pipe[k]) !== pipe[k] || pipe[k] < 0)) event.Cancel();
  });
  if (result.error && (!/^[A-Z][A-Z0-9_]{1,79}$/.test(result.error.code) || typeof result.error.retryable !== "boolean")) {
    event.Cancel(); return;
  }
  Object.keys(event.Get()).forEach(function(k) { event.Delete(k); });
  Object.keys(result).forEach(function(k) { if (result[k] !== null) event.Put(k, result[k]); });
  } catch (e) { event.Cancel(); }
}
"""
                    % (json.dumps(FIELDS), json.dumps(DATASETS), json.dumps(STATES)),
                }
            }
        ],
        "path.data": "/data/filebeat",
        "filebeat.registry.flush": "0s",
        "queue.disk": {
            "path": "/data/filebeat/diskqueue",
            "max_size": "128MB",
            "segment_size": "16MB",
        },
        "output.logstash": {
            "hosts": [collector + ":5044"],
            "pipelining": 0,
            "bulk_max_size": 32,
            "backoff.init": "1s",
            "backoff.max": "10s",
            "ssl.certificate_authorities": ["/run/mlo/secrets/ca.crt"],
            "ssl.certificate": "/run/mlo/secrets/client.crt",
            "ssl.key": "/run/mlo/secrets/client.key",
            "ssl.verification_mode": "full",
        },
        "http.enabled": True,
        "http.host": "127.0.0.1",
        "http.port": 5066,
        "logging.level": "warning",
        "logging.to_files": False,
        "logging.to_stderr": True,
        "monitoring.enabled": False,
    }


def logstash_settings(host):
    return {
        "node.name": "mlo-control-b",
        "api.http.host": "127.0.0.1",
        "api.http.port": 9600,
        "pipeline.workers": 1,
        "pipeline.batch.size": 32,
        "pipeline.ecs_compatibility": "v8",
        "queue.type": "persisted",
        "path.data": "/data/logstash",
        "path.queue": "/data/logstash/queue",
        "queue.max_bytes": "128mb",
        "queue.page_capacity": "16mb",
        "queue.checkpoint.writes": 1,
        "dead_letter_queue.enable": True,
        "dead_letter_queue.max_bytes": "32mb",
        "log.level": "warn",
        "config.support_escapes": True,
        "xpack.monitoring.enabled": False,
    }


def logstash_pipeline(host, es_hosts):
    # Event#to_hash is rebuilt so Beats metadata, original/message/request do not
    # enter ES. Only source-sanitized input is permitted into the durable PQ.
    fields = json.dumps(FIELDS)
    return """input { beats {
  host => "%s" port => 5044 ssl_enabled => true
  ssl_certificate_authorities => ["/run/mlo/secrets/ca.crt"]
  ssl_certificate => "/run/mlo/secrets/server.crt" ssl_key => "/run/mlo/secrets/server-pkcs8.key"
  ssl_client_authentication => "required"
} }
filter {
  ruby { code => '
    allowed = JSON.parse(%%q(%s))
    keep = { "@timestamp" => event.get("@timestamp") }
    allowed.each do |group, fields|
      value = event.get(group)
      if value.is_a?(Hash)
        keep[group] = value.select { |k, v| fields.include?(k) }
      end
    end
    cloud = event.get("cloud")
    if cloud.is_a?(Hash) && cloud["account"].is_a?(Hash)
      keep["cloud"] = { "provider" => "aws", "region" => "ap-northeast-2", "account" => { "name" => cloud["account"]["name"] } }
    end
    event.to_hash.keys.each { |k| event.remove(k) }
    keep.each { |k, v| event.set(k, v) }
  ' }
  if [event][dataset] not in ["mlo.pipeline", "mlo.ads", "mlo.security", "mlo.service"] or
     [event][id] !~ /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/ or
     [pipeline][state] not in %s { drop {} }
}
output { elasticsearch {
  hosts => %s user => "${MLO_LOG_WRITER}" password => "${MLO_LOG_PASSWORD}"
  ssl_certificate_authorities => ["/run/mlo/secrets/ca.crt"] ssl_verification_mode => "full"
  index => "logs-%%{[event][dataset]}-%%{+yyyy.MM.dd}"
  document_id => "%%{[event][id]}" action => "index" ilm_enabled => false manage_template => false data_stream => false
} }
""" % (host, fields, json.dumps(STATES), json.dumps(es_hosts))


def kibana_config(host, es_hosts):
    return {
        "server.host": host,
        "server.port": 5601,
        "server.ssl.enabled": True,
        "server.ssl.certificate": "/run/mlo/secrets/server.crt",
        "server.ssl.key": "/run/mlo/secrets/server.key",
        "elasticsearch.hosts": es_hosts,
        "elasticsearch.ssl.certificateAuthorities": ["/run/mlo/secrets/ca.crt"],
        "elasticsearch.ssl.verificationMode": "full",
        "elasticsearch.username": "kibana_system",
        # Password/encryption keys are valid setting names in kibana.keystore,
        # provisioned outside this renderer; never synthetic production values.
        "xpack.security.secureCookies": True,
        "xpack.security.authc.providers": {"basic.basic1": {"order": 0}},
        "telemetry.enabled": False,
        "map.includeElasticMapsService": False,
        "newsfeed.enabled": False,
        "logging.root.level": "warn",
    }


def configs(node, inventory):
    collector = next(
        n["private_ip"] for n in inventory["nodes"] if n["name"] == "control-b"
    )
    files = {
        "filebeat.yml": yaml.safe_dump(filebeat_config(collector), sort_keys=False)
    }
    if node["name"] == "control-b":
        hosts = [
            "https://" + n["private_ip"] + ":9200"
            for n in inventory["nodes"]
            if n["role"] == "elasticsearch"
        ]
        files.update(
            {
                "logstash.yml": yaml.safe_dump(logstash_settings(collector)),
                "logstash.conf": logstash_pipeline(collector, hosts),
                "kibana.yml": yaml.safe_dump(kibana_config(collector, hosts)),
            }
        )
    return files


def security_artifacts():
    indices = ["logs-" + d + "-*" for d in DATASETS]
    return {
        "writer": {
            "cluster": ["monitor"],
            "indices": [{"names": indices, "privileges": ["create_index", "index"]}],
        },
        "reader": {
            "cluster": [],
            "indices": [
                {"names": indices, "privileges": ["read", "view_index_metadata"]}
            ],
        },
        "template": {
            "index_patterns": indices,
            # Override ES built-in logs-* data-stream templates for this private,
            # stable-ID projection. Otherwise index operations are rejected.
            "priority": 501,
            "template": {
                "settings": {"number_of_shards": 1},
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "@timestamp": {"type": "date"},
                        **{
                            g: {
                                "properties": {
                                    f: {
                                        "type": (
                                            "long"
                                            if f
                                            in {
                                                "duration",
                                                "attempt",
                                                "batch_id",
                                                "records_in",
                                                "records_out",
                                                "records_quarantined",
                                            }
                                            else (
                                                "boolean"
                                                if f == "retryable"
                                                else "keyword"
                                            )
                                        )
                                    }
                                    for f in names
                                }
                            }
                            for g, names in FIELDS.items()
                        },
                        "cloud": {
                            "properties": {
                                "provider": {"type": "keyword"},
                                "region": {"type": "keyword"},
                                "account": {
                                    "properties": {"name": {"type": "keyword"}}
                                },
                            }
                        },
                    },
                },
            },
        },
        "kibana_operator": {
            "elasticsearch": {
                "cluster": [],
                "indices": [
                    {"names": indices, "privileges": ["read", "view_index_metadata"]}
                ],
            },
            "kibana": [{"base": ["read"], "feature": {}, "spaces": ["observations"]}],
        },
    }


def dashboard():
    objects = [
        {
            "type": "index-pattern",
            "id": "mlo-observations-v02",
            "attributes": {"title": "logs-mlo.*-*", "timeFieldName": "@timestamp"},
        }
    ]
    panels, references = [], []
    for i, (title, query) in enumerate(
        [
            (
                "Pipeline failures and retries",
                "pipeline.state: (FAILED or QUARANTINED or RETRYING)",
            ),
            ("Heartbeat and no progress", "pipeline.state: (HEARTBEAT or STALE)"),
            (
                "Transfer versus searchable release",
                "pipeline.state: (SENT or ACKNOWLEDGED or COMMITTED or SEARCHABLE or PUBLISHED)",
            ),
            ("Ads observation freshness", 'event.dataset: "mlo.ads"'),
        ]
    ):
        sid = "mlo-observation-panel-" + str(i)
        objects.append(
            {
                "type": "search",
                "id": sid,
                "attributes": {
                    "title": title,
                    "columns": [
                        "host.name",
                        "pipeline.state",
                        "pipeline.run_id",
                        "pipeline.release_id",
                        "error.code",
                    ],
                    "sort": [["@timestamp", "desc"]],
                    "kibanaSavedObjectMeta": {
                        "searchSourceJSON": json.dumps(
                            {
                                "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
                                "query": {"language": "kuery", "query": query},
                                "filter": [],
                            }
                        )
                    },
                },
                "references": [
                    {
                        "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                        "type": "index-pattern",
                        "id": "mlo-observations-v02",
                    }
                ],
            }
        )
        references.append({"name": sid, "type": "search", "id": sid})
        panels.append(
            {
                "panelIndex": str(i),
                "type": "search",
                "panelRefName": sid,
                "embeddableConfig": {},
                "gridData": {
                    "x": (i % 2) * 24,
                    "y": (i // 2) * 15,
                    "w": 24,
                    "h": 15,
                    "i": str(i),
                },
                "version": "8.19.23",
            }
        )
    objects.append(
        {
            "type": "dashboard",
            "id": "mlo-observations-v02",
            "attributes": {
                "title": "MLO transport and release observations",
                "description": "Synthetic acceptance; authoritative business ledgers remain PG/Mongo/S3.",
                "panelsJSON": json.dumps(panels),
                "optionsJSON": '{"useMargins":true}',
                "timeRestore": False,
                "kibanaSavedObjectMeta": {
                    "searchSourceJSON": '{"query":{"language":"kuery","query":""},"filter":[]}'
                },
            },
            "references": references,
        }
    )
    return "\n".join(json.dumps(obj) for obj in objects) + "\n"
