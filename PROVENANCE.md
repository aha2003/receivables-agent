# Provenance

All code in this repository is original and written for this project; no code is copied from other repositories. All data is synthetic and fictional.

Every dependency is pinned in `uv.lock`. Licences below are SPDX identifiers taken from each package's distribution metadata (`License-Expression`, or the trove classifier where no expression is published) and normalised by hand. Update this file in the same PR as any dependency change.

## Runtime dependencies (direct)

Each runtime package of SPEC §16.1 is added, with its line here, in the step that first uses it (see docs/DECISIONS.md D-001).

| Package | Version | Licence | Purpose | Added in |
|---|---|---|---|---|
| alembic | 1.20.0 | MIT | Schema migrations | S1.04 |
| anthropic | 1.14.0 | MIT | Official Claude SDK, imported only by `adapters/llm/anthropic_client.py` (§3.1, D-032) | S3.04 |
| argon2-cffi | 25.1.0 | MIT | Argon2id password hashing | S1.11 |
| fastapi | 0.143.0 | MIT | HTTP API and pages | S1.11 |
| itsdangerous | 2.2.0 | BSD-3-Clause | Signed session cookie | S1.11 |
| jinja2 | 3.1.6 | BSD-3-Clause | Server-rendered templates | S1.11 |
| pydantic-settings | 2.15.0 | MIT | Environment settings | S1.04 |
| pyyaml | 6.0.3 | MIT | config.yaml loader (BaseLoader, strings only) | S1.13 |
| python-multipart | 0.0.32 | Apache-2.0 | Login form parsing | S1.11 |
| sqlalchemy | 2.1.4 | MIT | Database access (SQLite) | S1.04 |
| structlog | 26.1.0 | MIT OR Apache-2.0 | JSON logs with a PII-scrubbing processor (§3.1, NFR-07) | S3.03 |
| uvicorn | 0.54.0 | BSD-3-Clause | ASGI server | S1.11 |

## Runtime dependencies (transitive)

| Package | Version | Licence |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| argon2-cffi-bindings | 26.1.0 | MIT |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.0 | BSD-3-Clause |
| docstring-parser | 0.18.0 | MIT (installed by anthropic) |
| h11 | 0.16.0 | MIT |
| httpcore2 | 2.13.1 | BSD-3-Clause (installed by anthropic via httpx2) |
| httpx2 | 2.13.1 | BSD-3-Clause (installed by anthropic; its HTTP client) |
| idna | 3.20 | BSD-3-Clause |
| jiter | 0.17.0 | MIT (installed by anthropic) |
| mako | 1.4.3 | MIT |
| markupsafe | 3.0.4 | BSD-3-Clause |
| opentelemetry-api | 1.45.1 | Apache-2.0 (installed by FastAPI and anthropic; no telemetry is configured or exported) |
| pycparser | 3.11 | BSD-3-Clause |
| pydantic | 2.14.1 | MIT |
| pydantic-core | 2.50.1 | MIT |
| python-dotenv | 1.2.4 | BSD-3-Clause (installed by pydantic-settings; the app reads no `.env` file) |
| sniffio | 1.3.1 | MIT OR Apache-2.0 (installed by anthropic) |
| starlette | 1.8.0 | BSD-3-Clause |
| truststore | 0.10.5 | MIT (installed by httpx2; system certificate store) |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |

Locked but not installed on any supported platform: `httpx2-jsfetch` (only for `sys_platform == 'emscripten'`).

## Development dependencies (direct)

| Package | Version | Licence | Purpose |
|---|---|---|---|
| httpx | 0.28.1 | BSD-3-Clause | FastAPI test client (added in S1.11) |
| hypothesis | 6.169.0 | MPL-2.0 | Property tests |
| pip-audit | 2.10.1 | Apache-2.0 | Dependency vulnerability scan |
| pre-commit | 4.7.0 | MIT | Local git hooks |
| pyright | 1.1.414 | MIT | Type checking (downloads Node.js and pyright from npm on first run) |
| pytest | 9.1.1 | MIT | Test runner |
| pytest-cov | 7.1.0 | MIT | Coverage |
| types-pyyaml | 6.0.12.20260906 | Apache-2.0 | Type stubs for pyyaml (pyright) |
| ruff | 0.17.1 | MIT | Lint, format, security rules |

## Development dependencies (transitive)

| Package | Version | Licence |
|---|---|---|
| boolean-py | 5.0 | BSD-2-Clause |
| cachecontrol | 0.14.4 | Apache-2.0 |
| certifi | 2026.10.19 | MPL-2.0 |
| cfgv | 3.5.0 | MIT |
| charset-normalizer | 3.5.2 | MIT |
| colorama | 0.4.6 | BSD-3-Clause (Windows only) |
| coverage | 7.16.2 | Apache-2.0 |
| cyclonedx-python-lib | 11.12.0 | Apache-2.0 |
| defusedxml | 0.7.1 | PSF-2.0 |
| distlib | 0.4.3 | PSF-2.0 |
| filelock | 4.1.1 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| identify | 2.6.20 | MIT |
| iniconfig | 2.3.1 | MIT |
| license-expression | 30.4.4 | Apache-2.0 |
| markdown-it-py | 4.2.0 | MIT |
| mdurl | 0.1.2 | MIT |
| msgpack | 1.2.3 | Apache-2.0 |
| nodeenv | 1.11.0 | BSD-3-Clause |
| packageurl-python | 0.17.6 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pip | 26.2.1 | MIT |
| pip-api | 0.0.35 | Apache-2.0 |
| pip-requirements-parser | 32.0.1 | MIT |
| platformdirs | 4.13.0 | MIT |
| pluggy | 1.6.0 | MIT |
| py-serializable | 2.1.0 | Apache-2.0 |
| pygments | 2.21.0 | BSD-2-Clause |
| pyparsing | 3.3.3 | MIT |
| python-discovery | 1.6.2 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| rich | 15.0.0 | MIT |
| sortedcontainers | 2.4.0 | Apache-2.0 |
| tomli | 2.5.0 | MIT |
| tomli-w | 1.2.0 | MIT |
| urllib3 | 2.8.0 | MIT |
| virtualenv | 21.14.7 | MIT |

## Vendored assets (served from `src/receivables/web/static/vendor/`, never from a CDN)

| Asset | Version | Licence | Source | SHA-256 |
|---|---|---|---|---|
| htmx (`htmx.min.js`) | 2.0.10 | 0BSD (Zero-Clause BSD) | https://github.com/bigskysoftware/htmx/releases/download/v2.0.10/htmx.min.js | `71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de` |
| htmx licence (`LICENSE`) | 2.0.10 | 0BSD | https://github.com/bigskysoftware/htmx/releases/download/v2.0.10/LICENSE | `d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38` |

Downloaded from the official GitHub release with the maintainer's approval (S1.13). `tests/api/test_web_security.py` checks every vendored file against these hashes. htmx runs with `allowEval`, `allowScriptTags` and indicator styles disabled (CSP-compatible, D-017).

## Build backend and external tools

| Tool | Version | Licence | Use |
|---|---|---|---|
| uv / uv_build | 0.12 or 0.13 (`pyproject.toml` allows `uv_build>=0.12,<0.14`; CI installs the latest uv) | MIT OR Apache-2.0 | Environment, lockfile, build backend |
| actions/checkout | v7 | MIT | CI checkout (bumped from v5 by Dependabot #67) |
| astral-sh/setup-uv | v7 | MIT | CI uv install |
| gitleaks (CLI binary, sha256-pinned) | 8.30.1 | MIT | CI secret scan over the full git history |
| Dependabot | GitHub service | n/a | Weekly update PRs for uv and GitHub Actions |
