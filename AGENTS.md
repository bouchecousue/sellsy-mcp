# Contributing with coding agents

- Use `uv`; run commands from this repository root.
- This is the standalone community server. Never copy business exports, production
  credentials, private operational reports or databases into this repository.
- Tests must use mocked HTTP and synthetic customer identities. Never use a live
  Sellsy account as a substitute for tests, or replay an uncertain business write.
- Keep read-only defaults, per-user OAuth isolation, explicit write consent,
  prepare/apply manifests, full-row preservation and readback verification.
- Do not weaken scope checks to repair a client connection.
- Run `uv run --frozen ruff check .`, `uv run --frozen pytest`, and the dependency
  audit described in CONTRIBUTING.md for runtime changes. Build/test the container
  when changing deployment files.
- Update the configuration and security documentation when OAuth, storage or
  network behavior changes. Do not claim an untested client or live write works.
