# Native data services: deployment handoff

Kafka 4.1.2, Spark 4.1.3, ZooKeeper 3.9.4 and Elasticsearch 8.19.23 run directly on Ubuntu 24.04 x86_64 EC2 under systemd. Kafka uses KRaft; ZooKeeper is for Spark HA. EC2 sizes, EBS volumes and network ownership remain unchanged. HAProxy/Filebeat auxiliary containers and the other web/database services retain their existing execution model. Docker is still required for those helpers; the four data daemons do not run in Docker.

The code provides an offline archive verifier/extractor, native configuration packager, host installer, protocol health checks, configuration rollback gate, driver admission/fencing primitives and Lifecycle stop/recovery stages. **This is not operational AWS acceptance.** `ready_for_aws` stays false until real host, secret, networking, application and restoration evidence exists. The Linux tooling-container probe is a parser/binary check; it does not boot systemd as PID1.

## Files and trust boundaries

| Input | Purpose |
| --- | --- |
| `native-releases.lock.json` | Five fixed native release URLs and publisher checksums; no latest-on-boot URL |
| `contracts/native-host.v1.json` | Architecture, persistence, resource and stop/recovery contract |
| Native manifest-v1 config artifact | Exact node/generation/config/unit/source hashes; binaries are separate |
| `/etc/mlo/host-binding.json` | Root-owned 0600 host identity, accepted manifest and release-lock hash, EBS UUID, secret filenames and helper image digests |
| `/etc/mlo/native-execution.json` | Control owner/generation, PostgreSQL lease, full Spark observer set, accepted driver specs and protected registry |
| Existing signed review/coordination inputs | Authorization for the exact SSM document, artifact S3 version and host cohort |

The small artifact uses the existing common TAR verifier and SSM entrypoint protocol. Native archives are deliberately outside its 128 MiB configuration-artifact limit. The host must receive these separately under `/var/lib/mlo/native-archives/`; each is rehashed before extraction. Publisher HTTPS checksums are pinned; detached-signature and complete OS/Python/connector acceptance are separate evidence and are not claimed by a successful download.

Extracted immutable releases live at `/opt/mlo/native/releases/<product>-<digest-prefix>`. Revision-specific configs live at `/etc/mlo-native/<generation>/<node>/<revision>`. Neither install nor rollback silently points at a new binary version. Native helpers are content-addressed. Existing directories without the expected receipt or persisted UID/GID are refused.

## Local preparation (no AWS)

Run from the infrastructure checkout, with its prepared Python environment:

```bash
export PYTHONPATH=aws-infra/runtime/src
aws-infra/runtime/.venv/bin/python -m mlo_runtime.native_releases fetch \
  --lock aws-infra/runtime/native-releases.lock.json \
  --cache .local/native-predeploy/archives \
  --output .local/native-predeploy/archives-receipt.json
```

`fetch` requires network access to the pinned publishers. `verify` uses the same arguments and works offline on an existing cache. Receipt/output paths must be new; never overwrite an earlier run to conceal a failure. An archive checksum is not a connector integration or host readiness result.

An example-only configuration artifact can be prepared with:

```bash
aws-infra/runtime/.venv/bin/python -m mlo_runtime.native_package \
  --inventory aws-infra/contracts/inventory.example.json \
  --revision native-review-v1 --fixture \
  --output .local/native-predeploy/native-fixture-v1.tgz
```

Fixture artifacts are refused on real hosts. A real artifact requires the Foundation-exported real inventory, a new revision and output under `.private/`, with no `--fixture`. It contains real host configuration and must not be published to GitHub. The separate accepted S3 object version/hash, manifest hash, helper image digests and fixed SSM document version/hash remain mandatory.

The `containers/native-tools.Dockerfile` image and `scripts/check-native-linux.py` are disposable **test tooling**. Build the image for `linux/amd64`. The probe has no network, privileges, Docker socket or host cgroup mount. It checks the pinned binaries and all nine hosts' unit syntax; it does not establish restart, failover or EBS retention acceptance.

## Before the first AWS instance

1. Rebuild/review Terraform plans from the current source. Previous Docker-era/source-hash plans and reviews are stale. Do not clear completed/pending receipts to bypass the mismatch.
2. Review the 48-hour service/52-hour billable budget and retain the current instance sizes until measurements justify a change.
3. Select the actual AMI/bootstrap process. A Canonical Ubuntu AMI lookup alone does not prove native packages, Python dependencies, helper Docker/Compose, AWS/SSM tools or offline boot readiness.
4. Prepare the offline native archives, OS packages, Python environment, complete Spark Kafka/S3A/JDBC/Delta connector closure and actual application artifact. The current Delta connector lock still lists unresolved dependencies; a binary version probe cannot remove that blocker.
5. Prepare PKI, secret delivery after reboot, EBS initialization/mount and initial-cluster procedures separately. Do not invent an EBS UUID before the volume exists.

No AWS resource, new AMI/snapshot, S3 upload or SSM command is needed for the local steps above. Actual AMI boot, instance profile/SSM registration, EBS UUID and cross-AZ communication require the later controlled AWS/host acceptance stage.

## Host install and cohort activation

Use the existing signed controller/SSM artifact route. The entrypoint accepts the unchanged operations `install`, `deploy`, `health`, `rollback`.

- `install`: verify identity, every config file, accepted release lock, mounted EBS UUID, secrets, persistent identity and offline archives; stage the release/config without starting units.
- `deploy`: requires an already staged revision. Install/verify the fixed units; start the accepted proxy/Filebeat helpers and foreground daemons; require authenticated protocol readiness before enable-on-boot and a success receipt.
- `health`: check the installed revision, units, helper health and authenticated Kafka/Spark/ES responses. ES additionally observes three nodes and the retained cluster UUID after first bootstrap.
- `rollback`: configuration-only rollback to a staged previous revision using exactly the same binary release set. A fresh common signed shutdown proof is required. Binary downgrades/data migrations need a separate reviewed procedure.

Initial quorum activation must start the three-member cohorts concurrently; a sequential deploy-and-wait of only the first Kafka/ES member cannot pass quorum health. Stage all members first. Prepare Gateway/proxy, PostgreSQL/MongoDB and observation collector dependencies before declaring application readiness. Do not mark Filebeat forwarding healthy before the collector exists.

Native install never runs `mkfs` or Kafka storage format. Kafka `meta.properties` must already match the reviewed KRaft cluster UUID and node ID. ZooKeeper myid must retain the correct member ID. ES first bootstrap is explicitly separate from recovery; retain its observed cluster UUID and remove `initial_master_nodes` for later accepted starts. Do not reuse a first-bootstrap binding on populated storage.

Every daemon start checks the protected host binding and exact EBS mount UUID through a privileged pre-start guard. A missing EBS mount/secret must prevent startup. Dedicated non-login UID/GID 2000 owns only the accepted data directories; root owns binaries/config and original secret files. Service credentials are delivered with systemd `LoadCredential`. Journald remains bounded by the existing host role.

## Control drivers and failover

Native driver admission checks the maintenance barrier and the current PostgreSQL lease before/after starting a static epoch-specific unit. The protected registry is written before start. Driver units use `Restart=no`, `KillMode=control-group` and private runtime credentials; a stale owner must not be resurrected by systemd. Batch query registry keys include a unique immutable run suffix; never reuse an application admission ID to submit a second batch.

`native_fence` implements the host-local fixed start/stop/observe protocol. A separately accepted, checksum-bound remote adapter must target the exact old Control instance through SSM. Native supervisor bindings set `execution_backend=native-systemd-v1`; a legacy Docker receipt is refused. Missing registry for a previous lease, unregistered driver units, different owner/generation/epoch, partial observers or a populated nested cgroup block takeover.

For common wire-v1 compatibility the historical `containers_stopped` field means execution stopped; native receipts additionally identify their backend and observed native units. No Docker observation is used as native stop evidence. `SystemdSparkExecution` reuses only the authenticated Spark JSON observation logic.

Control Compose web/observation deployment requires an explicitly checksum-bound protected native execution contract and matching supervisor backend. A JSON `ready` flag alone does not enable it. Preparing those contracts does not prove real remote fencing, continuous driver artifacts or their connector closure.

## Stop, recovery and unresolved acceptance

Lifecycle `native.stop_native` requires the complete nine-host set, common operation identity and fresh signed admission/flush/idle preservation evidence. It journals each pending/completed effect. Order: Kafka/ES -> all Spark workers -> all Spark masters -> all ZooKeeper members, after admission closure, writer fencing and checkpoint/backup preservation. The native signed issuer/remote routing must observe real hosts, including the stopped-unit alternative once the Spark observer services are down; lab signatures cannot authorize a real inventory.

Recovery verifies retained releases/mounts/preservation, then starts ZooKeeper -> Spark masters -> Spark workers -> Kafka/ES and checks every host. It never admits writers. A fresh lease/maintenance-barrier decision and verified application state are separate.

Failed/crashed activation leaves a pending receipt and stopped services. It cannot replay or auto-restart the old writer. Inspect actual state and reconcile with an independent reviewed record before retry; do not delete the journal or flip a boolean. Native data service stop does not itself stop EC2 or authorize Terraform destroy.

Before real service deployment still require: actual Linux PID1/systemd restart tests, EBS data survival and backup restoration, remote SSM/lease fencing, helper observation transport, full application/connector acceptance, real inventory/PKI/secrets and 48-hour growth/capacity measurements. Track2 application/stop-hook acceptance remains owned by the application team and stays blocked until supplied.


## Native connector closure v2 - 2026-11-10 local continuation

Local candidate only; `ready_for_aws=false`. The previous `connectors.lock.json`
remains a legacy Delta-only intake record, not an accepted production lock.

- `native-connectors.lock.json` (v2) pins 286 unmodified Spark 4.1.3
  distribution JARs and 15 unique additional JARs by SHA-256. The original
  16-extra intake contained `lz4-java-1.8.0.jar` twice (also in Spark);
  the standalone duplicate was removed without replacing Spark's identical
  pinned JAR. The historical intake and SHA-1 publisher evidence remain in
  ignored `.local/`; publisher SHA-1 is not a runtime acceptance signature.
- `mlo_runtime.native_connectors` verifies exact filenames and SHA-256,
  rejects duplicates, symlinks and unknown JARs, and stages an offline
  content-addressed bundle under `/opt/mlo/native/connectors/spark-<id>/jars`.
  The effective Spark runtime ID is derived from the Spark publisher SHA-512
  plus the connector lock SHA-256. No Maven/`--packages` downloads are allowed.
- Native packaging includes the lock under manifest-v1's SHA-protected `files`;
  no incompatible manifest field was added. The production host binding must
  independently pin `native_connector_lock_sha256` alongside the publisher
  release lock, and the trusted cache must contain exactly the approved 15
  extra JARs under `/var/lib/mlo/native-connectors` before offline `install`.
- Spark master/worker configs carry identical `spark.driver.extraClassPath`
  and `spark.executor.extraClassPath` to the approved bundle. Their immutable
  systemd units run a content-addressed `native_connector_guard.py` with
  `ExecStartPre=+` after the EBS mount guard. It checks both locks and all
  301 JAR hashes before **every** daemon start, including automatic restarts.
- Control `NativeAdmission` requires separately staged, protected connector
  lock, Spark/Java release digests, the same classpath and proven source
  archive SHA-256. A production driver spec must include the approved
  `application_artifacts` list and an exact `--py-files` set; no inline Spark
  authentication secret, `--packages`, `--conf`, unpinned JARs, runtime Ivy or
  unchecked ZIPs are permitted. The static unit passes the accepted spec hash
  to `native_driver_launch.py`, which checks it again before exec.

**Unresolved:** A read-only direct-POM audit (15 Maven POMs) found differences
between Delta's declared Jackson/RoaringBitmap/SLF4J versions and Spark's
provided copies. `jackson-datatype-jdk8` and Unity Catalog client/Hadoop
artifacts are not in the 301 JARs; their requirement/compatibility needs
explicit resolution. The review does **not** resolve transitive dependencies,
ABI conflicts or runtime integration. Java 21 core classloading success is
not a Kafka/S3A/PostgreSQL/Delta functional test.

**Application handoff:** the pinned team `develop` snapshot has
`app/pipeline/weather/submit_spark.py` requiring an inline
`spark.authenticate.secret`, `spark.network.crypto.enabled=true`, all JARs
under `SPARK_HOME/jars`, and a `--properties-file` override. This conflicts
with the Native secret-file + TLS RPC and separate approved connector dir.
Prefer a reviewed direct `spark_job.py` invocation with a SHA-pinned
`--py-files` application ZIP after the team approves that entrypoint; do not
merge or weaken the team's wrapper automatically. The Control staging and
supervisor contract for this application are not yet accepted.

**Host evidence still required:** offline OS/Python closure, root-protected
Control artifact/connector staging, actual PID1 restart/reboot, EBS identity
and retained data, PKI/secret resupply, real IAM instance profiles and SSM,
remote fencing, quorum failover, backups and 48-hour load. The Canonical
Ubuntu 24.04 AMD64 AMI lookup is a candidate listing, not boot acceptance.
Do not regenerate/use an old Terraform saved plan or set `ready_for_aws=true`
from the local checks alone.


## Team feature/infra -> develop -> operator deployment readiness

The intended final Git workflow is `feature/infra` commit, reviewed PR into
`develop`, and **only after merge** a newly pinned application/infrastructure
snapshot and operator-controlled AWS deployment. The reviewed local baseline
`feat/2-infra-manual-launch` and `feature/infra` had the same base commit when
checked; do not force-push/reset or copy an older app tree into the feature
branch. Commit, push and PR are approval-controlled steps, not part of this
local acceptance run.

### Shared app immutable review (2026-11-10)

The latest checked `origin/develop` was `c4d6fe7e96af7e2ebfe3083a26e7093198b3782a`.
`mlo_runtime.native_app` uses *only Git blobs*, never checks out or merges that
revision, producing a deterministic `pipeline-reviewed.zip`, separate weather
and feedback driver scripts, and SHA-256 per source. It excludes the weather
`submit_spark.py` wrapper because its inline `spark.authenticate.secret`,
`spark.network.crypto.enabled=true`, `--properties-file`, and single-JAR
location are incompatible with the Native secret-file+TLS RPC+separate bundle.
No security check is disabled to achieve compatibility. The weather code ZIP
imports its modules and location mapping from an isolated Python path; the
feedback script additionally depends on Django, `backend.common.events`, and
a pinned offline Python/DB runtime not yet accepted for Control.

Local-only `mlo_runtime.native_control_stage` creates a content-addressed
Control **candidate** under a disposable `mlo-control-lab-*` root; it never
installs at `/`, changes an accepted host, enables a unit or modifies credentials.
It checks the reviewed ZIP/entrypoint hashes, verifies staged receipts and
fails on extra/altered files. A production Control installer, spec acceptance,
locked Python environment, PKI and PostgreSQL lease supervisor remain blocked.
The future direct Native invocation contract is `spark-submit --master
spark://<accepted master list> --deploy-mode client --py-files
/opt/mlo/current/apps/<accepted release>/pipeline-reviewed.zip
/opt/mlo/current/apps/<accepted release>/weather-stream-spark_job.py` with
all security, TLS, identity, checkpoint and driver classpath settings in a
hash-pinned `spark-defaults.conf` (not `--conf` runtime overrides). This is a
review **contract**, not an executable configuration without real endpoints,
identities, EBS UUIDs and protected secret files. Any feedback writer must
also prove its Django/PostgreSQL dependency closure and lease fencing.

### Local verification and remaining host tests

- Delta 4.3.1 + Spark 4.1.3 **local filesystem** round-trip (4 rows) passed
  in Ubuntu 24.04 AMD64 with outbound network disabled. A second execution
  passed using connector classpath from `spark-defaults.conf` alone, without
  `--driver-class-path`/`--conf`/Maven. Neither is an S3A/Kafka/Delta-on-S3
  integration acceptance. Maven direct POM mismatches for legacy Delta's
  Unity Catalog/Jackson dependencies remain unapproved for UC functionality;
  current team pipeline code has no Unity Catalog entrypoint.
- Ephemeral local CA/client/server certificate test established mTLS,
  PKCS#12 certificate/key loading and refusal of a missing client identity.
  Production CA, SAN identities, revocation and `systemd LoadCredential`
  resupply remain unverified.
- Runtime unit tests and isolated Control staging tamper tests are local only.
  Linux `systemd-analyze verify` tests generated units, **not** actual PID1
  reboot, restart or crash recovery. There are no live EC2/EBS volumes in
  the operator accounts to prove EBS UUID or retained state.
- Read-only AWS CLI checks through the existing `ProjectInfraRole` profiles
  show zero EC2/online SSM managed targets. The locked `p4-runtime-artifact`
  SSM Document could not be described with that role at this time. Do not
  substitute a general-purpose shell document, invent instance IDs, execute
  SendCommand or bypass the immutable document-and-artifact review policy.
- The prior saved Terraform plan's source hash **does not match** the current
  code. Re-run preflight and bootstrap-plan in a **new** private review
  generation after all code and app locks are signed off. Do not delete or
  reuse prior receipts or plans.

**Gate:** full actual-host install, EBS retention, SSM/document permissions,
Control driver PKI/Python/feedback dependencies, PostgreSQL lease failover,
backup/restore, observation transmission, and 48-hour soak must pass before
`ready_for_aws=true` or any real deployment. Keep at false until then.


### systemd user/credential separation audit

The first locally rendered Control driver unit used `User=mlo` but passed the
root-protected `0600` driver-spec path directly to the launcher. The `mlo`
service account could not read it. The revised unit keeps the source
root-protected and adds `LoadCredential=driver-spec:<approved spec path>`;
`ExecStart` supplies the systemd `%d/driver-spec` credential path to the
launcher with the reviewed SHA-256. It also delivers root-owned Spark defaults
via `LoadCredential=driver-defaults:<approved defaults path>` and verifies their
SHA-256 immediately before execution. Application source and helper code stay
content-addressed and readable; secrets never become source files.
The static `%d`/LoadCredential unit form passed the Ubuntu 24.04 `systemd-analyze`
syntax parser and the launcher tests prove changed specs/defaults are refused
before `os.execv`. This **does not** prove an actual PID1 restart or successful
systemd credential copy on the production AMI; retain the host acceptance gate.

For the eventual approved AWS flow, the Terraform controller_deployment grants
are gated by real instance IDs and enabled artifact resources. The currently
usable `ProjectInfraRole` profiles cannot describe the `p4-runtime-artifact`
SSM Document. This is not a reason to switch silently to a legacy admin role
or the unrestricted `AWS-RunShellScript` document. Confirm the intended
controller role, exact document version/hash and Native host entrypoint from
reviewed Terraform outputs before any real `send-command`.


## Latest develop feedback acceptance scope (2026-11-10)

The pinned public `develop` SHA `c4d6fe7e96af7e2ebfe3083a26e7093198b3782a`
was independently checked out in a clean worktree. The new local application
review schema `native-shared-app-review-v2` packages **271** Git-blobs from
`app/pipeline` and `app/backend`, including `backend/common/events.py`, Django
settings/models and `backend/requirements.lock`. It intentionally excludes
`app/pipeline/weather/submit_spark.py`, which uses inline Spark auth and a
runtime `--properties-file` override conflicting with the Native security
contract. The release contains only the fixed source SHA and never reads
uncommitted application files from `feature/infra` or another workspace.

The manifest declares `runtime_feature_scope` with mandatory Spark Kafka
streaming, S3A checkpoint support, PostgreSQL feedback, and weather->ES
HTTP. It explicitly **excludes Unity Catalog client/Hadoop and Delta
Unity Catalog kernel** from deployment acceptance. Source scanning rejects
new Delta/Unity Catalog API use until separately reviewed; the optional
Delta local-file roundtrip is not evidence of Unity Catalog compatibility.
The canonical 301-JAR Spark bundle remains pinned and distinct from any
implicit Maven/`--packages` dependency resolver.

Native Control local staging preserves deterministic archive and entrypoint
SHA-256, checks the bundled Python requirements lock, and refuses mismatched
receipts, altered archives or unexpected files. The ZIP imports Django and
`backend.common.events` in a no-network Linux test container; the observed
Python 3.14.10 environment had matching Django 6.1.2, psycopg 3.3.6,
psycopg-binary 3.3.6, Kafka Python 2.3.2, djangorestframework 3.18.3,
cryptography 50.0.2 and argon2-cffi 25.1.0. The target Ubuntu 24.04 has
**Python 3.12**, so that import smoke is **not** an accepted Ubuntu host
Python package closure. The minimal Ubuntu 24.04 amd64 tool image lacks pip
and `python3.12-venv`; `/usr/bin/python3 -m venv` failed. The full locked
Linux/amd64 CPython 3.12 wheelhouse, OS package bootstrap and hash-verified
Control installation are not yet supplied or tested.

The PostgreSQL/Django feedback writer also requires actual schema migrations,
TLS DB connectivity, permissions, guaranteed source event/Outbox consistency,
a validated Kafka consumer/Structured Streaming pipeline, and PostgreSQL
lease/cgroup fencing acceptance. Importing modules in a disposable container
is not evidence of transaction durability or safe remote takeover.

Lifecycle offline `pg_isready` could falsely mark the postgres container
ready before `POSTGRES_DB` initialization. The *test harness* now requires
`psql -d mlo_lifecycle -tAc 'SELECT 1'` before touching that database. This
changes neither production service configuration nor cleanup/protection
rules. The earlier full Lifecycle run failed that race and one recovery case;
a targeted rerun passed both, with complete integration re-verification
tracked separately.

The Foundation mock provider/16-EC2/10-peering test runner and Runtime unit,
Ansible syntax/lint checks pass in local workspaces. They do **not** authorize
AWS provisioning, provide real EBS UUIDs or validate SSM managed hosts.
Any unreviewed/expired saved Terraform plan remains ineligible. Keep
`ready_for_aws=false`, stage no deployment artifacts to S3, issue no remote
SSM commands and do not sign operational approval or commit/push/PR until
the explicit Git handoff and review stage.


## Control feedback CPython 3.12 and fail-closed group admission (2026-11-10)

`aws-infra/runtime/native-feedback-python.lock.json` pins **26** wheel files
for Linux AMD64 CPython 3.12, their original backend requirements lock SHA-256,
exact packages/versions and each wheel's SHA-256. The 37.92 MB wheel payloads
are **ignored local artifacts**, never added to Git. The standard-library
`mlo_runtime.native_python` verifier rejects modified, unknown, extra, missing,
wrong-ABI/wrong-platform and mismatched-application wheels. It can emit a
complete `--require-hashes` pip requirements file in a new ignored directory.
`native_app` schema `native-shared-app-review-v3` binds the wheel lock digest
to the deterministic Git-object ZIP (271 backend/pipeline source files) from
the pinned current team develop review. `native_control_stage` checks the
same digest before creating any **lab-only** Control application candidate.

The local Ubuntu 24.04 x86_64 tool image originally lacked `python3.12-venv`
and `python3-pip`. A **disposable local Docker test image** installed these
OS packages, then validated in an outbound-network-disabled container:

- Python 3.12.3, `pip install --no-index --require-hashes` for all 26 pinned
  wheels, `pip check`, Django 6.1.2 setup, psycopg 3.3.6, and imports from the
  unchanged source ZIP. Every installed non-bootstrap package matched the
  public team's `backend/requirements.lock`.
- On an isolated private Docker network with no published ports, a separately
  created disposable PostgreSQL 16.11 instance accepted real SQL transactions.
  The actual `pipeline.feedback.process.apply_record` path persisted two Kafka
  offset rows and one projection, ignored replays, and refused a changed
  payload at an existing offset. This was a **synthetic test schema**, not
  a production migration, production TLS, real Kafka stream or AWS database.
- The existing local PostgreSQL supervisor integration exercised five real
  database CAS, stale lease, two-process takeover, self-fence on database
  failure, and dump/restore test cases. Its executor adapters are local test
  processes, **not actual remote systemd/SSM/3-AZ**.

Production Native `verify_driver_connectors` now requires a separately
source-pinned `native_feedback_python_lock_sha256` binding and a root-protected
`/etc/mlo/native-feedback-python.lock.json` with that hash; it refuses an
unreviewed or changed Python executable. Its required immutable Python path is
`/opt/mlo/native/python/feedback-<lock SHA first 16>/venv/bin/python3.12`
(with CPython installed using `venv --copies`, to avoid executable symlinks).
The approved static driver launcher verifies the executable SHA again and
sets `PYSPARK_PYTHON` **and** `PYSPARK_DRIVER_PYTHON` to that exact path before
`spark-submit`. Workers must have the same path and wheel closure for any
Python executor tasks. Current AWS hosts do not exist and this future
production Python release has **not** been staged/accepted on them.

`native_fence.dispatch` previously admitted continuous drivers sequentially
without group self-fencing if the second `NativeAdmission.admit` failed. The
new group start requires exactly the weather and feedback drivers; on partial
start/failure it calls `SystemdSparkExecution.stop_group` for the current
lease/epoch, **never returns an accepted receipt**, and keeps uncertain
registries/takeover blocked. The local fault-injection tests cover failure
before first start, failure after first, uncertain stop, and complete success.
No test claims a successful production remote stop or systemd PID1 reboot.

The 2026-11-10 local verification environment also has a **new independent**
Python 3.14.8 venv in ignored `.local/` with pinned runtime and common
requirements installed and `pip check` passing; it does not rely on the
older `aws-infra/runtime/.venv` symlink into the private developer tree.

**Still blocked before AWS runtime acceptance:** durable install of the
root-protected wheelhouse and Python executable on Control A/B and every
Spark worker; actual Python/PKI credential re-supply after host reboot;
PostgreSQL server-side TLS, correct migrations/privileges, and real lease
fencing under PID1; EBS mount UUID and retained-state testing; accepted SSM
Document/IAM and monitored online EC2; Spark master/workers quorum,
Kafka/S3A checkpoint/ES end-to-end, backup/restore and 48h operations.
Keep `ready_for_aws=false`; no S3 artifact upload, SSM dispatch or Terraform
apply until reviewed operator authorization.
