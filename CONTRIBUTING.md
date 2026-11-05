# Contributing

Bug reports, documentation improvements and pull requests are welcome. By
submitting a contribution you agree to provide it under this repository's MIT
license; no separate contributor license agreement is required.

## Local checks

```sh
uv sync --frozen --all-groups
uv run --frozen ruff check .
uv run --frozen pytest
uv build
uvx bandit -q -r src
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/sellsy-mcp-runtime.txt
uvx pip-audit --no-deps --disable-pip -r /tmp/sellsy-mcp-runtime.txt
```

Tests use temporary storage and mocked HTTP. No Sellsy credentials are needed.
Do not call production endpoints in unit tests, publish raw OAuth errors, or add
real customer names, IDs, files, tokens or operational logs to fixtures.

For a business write, preserve all document rows, validate taxes/relations,
check the live fingerprint immediately before the request, persist the attempt,
and verify the result by reading it back. Do not introduce automatic write retries.

Keep a change focused and describe the user-visible problem and validation.
Add regression coverage when modifying authentication, permission checks,
pagination, money calculations, write preservation or concurrency.

The optional `scripts/check_sellsy_contract.py` compares public API schemas to a
reviewed baseline. It does not use credentials or call business endpoints. A
contract difference needs review; do not refresh hashes just to hide a failure.

## Before sharing an archive or release

Build from tracked files only. Run `gitleaks git --redact` and inspect the sdist and
wheel. Keep `.env`, `var/`, secrets, signed URLs and local reports outside Git and
container build contexts. Dependency updates must pass the full suite and audit.
Security-sensitive issues belong in the private reporting channel in SECURITY.md.
