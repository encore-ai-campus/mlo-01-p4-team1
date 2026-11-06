# Runtime execution contract v1 — remediation decision

This is the common coordinator's contract for the 2026-10-30 revision. Existing inventory-v1 and runtime-plan-v1 remain compatible; AWS readiness remains false until real acceptance.

## Archive and delivery

- Format: gzip TAR, suffix `.tar.gz`, canonical bounded regular-file members only. No directory entries, links, devices, absolute paths, backslashes, duplicate or parent paths. Maximum 32 MiB/member and 128 MiB expanded archive.
- Exactly one `manifest.json`. Manifest fields: `schema_version` = `1.0.0`, `artifact_version`, `environment`, `generation`, `platform` = `linux/amd64`, `example_only` = false, `target_nodes`, `node_roles`, `operations`, `files`, `image_digests`, `config_hashes`, `required_secrets`.
- `operations` contains only install/deploy/health/rollback; `files` maps every other archive member to its SHA256; `config_hashes` is a verified subset of files. `node_roles` and `required_secrets` use target node names. Image values must include immutable `@sha256:<64hex>` references.
- Delivery binding: artifact `{key, version, sha256, manifest_sha256}`; bucket comes from the existing transfer bucket binding. Key begins `artifacts/` and ends `.tar.gz`. S3 version is immutable. Archive and manifest SHA256 are external trusted inputs, not self-authentication.
- Common standalone verifier: `aws-infra/contracts/artifact_verifier.py`. CLI: `--archive --sha256 --manifest-sha256 --environment --generation --node --destination`. Library: `inspect_artifact(archive, expected_sha256, environment, generation, node, destination, expected_manifest_sha256=None)`; dispatcher always supplies the manifest hash. Successful return is the manifest; extraction happens only after complete verification into a new directory. No asserts for security decisions.
- Fixed executable: `bin/runtime-entrypoint.py`, invoked with `python3`, never arbitrary command input. Interface: `--operation install|deploy|health|rollback --environment <env> --generation <gen> --node <name> --binding <protected host-binding.json>`. Default production host binding: `/etc/mlo/host-binding.json`; local fixture override only through explicit lab binding.
- Foundation dispatcher embeds the trusted common verifier, downloads the exact S3 object version, derives/validates current EC2 instance and node from IMDSv2 plus its compiled instance-ID mapping, verifies both hashes and target metadata, then invokes the fixed runtime entrypoint. Its receipt records success/failure without secrets.
- B packager includes its runtime package plus selected node bundles. Installer stages a new release, validates host/mount/images/secrets, applies accepted Compose/systemd through a controlled backend, verifies health, and atomically switches current only on success. Idempotency, serialization, old-release preservation, rollback and receipts are required. Do not format storage or execute arbitrary shell from manifest.

## SSM transfer authentication

- Preserve original named AWS login profiles as source credentials; pin CLI `2.37.10`. Use an isolated credential_process bridge when Terraform/provider/SDK cannot directly consume login_session. The bridge invokes `aws configure export-credentials --profile <source> --format process` with original config paths restored; credentials remain in memory/stdout to the SDK consumer and are never logged.
- Each account gets a distinct controller role `p4-controller-encoreN`. B's SSM profile name is `p4-controller-encoreN`; generated isolated config defines that role profile with `source_profile=encoreN` and the exact controller role ARN.
- amazon.aws.aws_ssm 11.4.0 uses `<instance-id>/...` S3 keys and has no configurable prefix option. Account separation therefore uses each account's set of exact instance-ID prefixes, rather than an invented plugin prefix setting. Artifact keys live separately under `artifacts/`.
- Controller identity, bucket and each endpoint policy allow the necessary Get/Put/Delete/List/GetBucketLocation only for the transfer bucket and that controller's instance-prefix set. Runtime instance roles do not gain temporary transfer writes. Cross-account grants and explicit negative tests are required.

## Execution and model boundary

- Use existing PostgreSQL for a transactional lease backend; no new managed service. Generation/epoch/owner CAS, renew failure fencing, stale evidence refusal, all-master/worker observation and old-host remote fencing must remain fail closed.
- Remote execution adapters use fixed argv protocols plus validated target/generation and receipts, not free-form shell. Actual SSM/tailnet/host credentials remain acceptance inputs.
- Implement a local-testable Gateway relay forwarding to a fake home API over TLS. Home request API preserves request ID/body/deadline and has submit/result/cancel/status semantics; dedup is the home API's responsibility, not a promise made by retrying the Control client. Test accepted-but-disconnected requests and observable slot release separately. Do not implement a real model or change the home PC.
