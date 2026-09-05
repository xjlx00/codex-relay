# Isolated app-server image usage patch

Upstream: OpenAI/Codex `52e73e3a548ae5310c7765995b9803dd538b82b0`.

This patch preserves the Images response's raw `usage` in structured image
completion items. It does not introduce an upstream HTTP proxy, modify image
requests, or include any subscription credentials. Missing usage remains null.

The CI workflow builds and tests an isolated Linux binary. The production relay
must not be switched until a live subscription image probe confirms the native
Images endpoint supplies the expected token usage.

The patch covers response parsing, image lifecycle propagation, nullable
serialization and compatibility with older saved items. Generated schema and
the final formatted patch are included with the build artifact.

Live verification succeeded on 2026-09-06 (Asia/Shanghai), using one image and
the existing ChatGPT Pro subscription. The native image endpoint returned 60
input tokens, 229 output tokens and 289 total tokens, including both modality
breakdowns. See [verification.md](verification.md) and [live-report.json](live-report.json).

`image-usage.patch` is the small source patch used by CI before schema generation.
`complete-source.patch` is the final formatted patch including generated JSON,
TypeScript and precomputed schemas. Apply either patch to the pinned upstream
source, not both. `UPSTREAM_LICENSE` accompanies the upstream-derived patch.

The isolated Linux binary is stored in the CI artifact and on the VPS under
`/opt/codex-relay-v2/experiments/image-usage-52e73e3/`. Production still uses
the official 0.153.2 binary. Gateway forwarding and per-user image accounting
have not been enabled by this verification.
