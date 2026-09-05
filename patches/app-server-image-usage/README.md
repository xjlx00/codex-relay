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
