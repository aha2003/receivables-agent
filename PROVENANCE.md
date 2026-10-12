# Provenance

All code in this repository is original and written for this project; no code is copied from other repositories. All data is synthetic and fictional.

Every dependency is pinned in `uv.lock`. Licences below are SPDX identifiers taken from each package's distribution metadata (`License-Expression`, or the trove classifier where no expression is published) and normalised by hand. Update this file in the same PR as any dependency change.

## Runtime dependencies

None yet. Each runtime package of SPEC §16.1 is added, with its line here, in the step that first uses it (see docs/DECISIONS.md D-001).

## Development dependencies (direct)

Development packages of §16.1 not yet needed (`httpx`) are added in the step that first uses them (D-001).

| Package | Version | Licence | Purpose |
|---|---|---|---|
| hypothesis | 6.169.0 | MPL-2.0 | Property tests |
| pip-audit | 2.10.1 | Apache-2.0 | Dependency vulnerability scan |
| pre-commit | 4.6.2 | MIT | Local git hooks |
| pyright | 1.1.414 | MIT | Type checking (downloads Node.js and pyright from npm on first run) |
| pytest | 9.1.1 | MIT | Test runner |
| pytest-cov | 7.1.0 | MIT | Coverage |
| ruff | 0.17.0 | MIT | Lint, format, security rules |

## Development dependencies (transitive)

| Package | Version | Licence |
|---|---|---|
| boolean-py | 5.0 | BSD-2-Clause |
| cachecontrol | 0.14.4 | Apache-2.0 |
| certifi | 2026.7.22 | MPL-2.0 |
| cfgv | 3.5.0 | MIT |
| charset-normalizer | 3.5.2 | MIT |
| colorama | 0.4.6 | BSD-3-Clause (Windows only) |
| coverage | 7.16.2 | Apache-2.0 |
| cyclonedx-python-lib | 11.12.0 | Apache-2.0 |
| defusedxml | 0.7.1 | PSF-2.0 |
| distlib | 0.4.3 | PSF-2.0 |
| filelock | 4.1.1 | MIT |
| identify | 2.6.20 | MIT |
| idna | 3.20 | BSD-3-Clause |
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
| pyyaml | 6.0.3 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| rich | 15.0.0 | MIT |
| sortedcontainers | 2.4.0 | Apache-2.0 |
| tomli | 2.5.0 | MIT |
| tomli-w | 1.2.0 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT |
| virtualenv | 21.14.7 | MIT |

## Build backend and external tools

| Tool | Version | Licence | Use |
|---|---|---|---|
| uv / uv_build | 0.12.x | MIT OR Apache-2.0 | Environment, lockfile, build backend |
| actions/checkout | v5 | MIT | CI checkout |
| astral-sh/setup-uv | v7 | MIT | CI uv install |
| gitleaks (CLI binary, sha256-pinned) | 8.30.1 | MIT | CI secret scan over the full git history |
| Dependabot | GitHub service | n/a | Weekly update PRs for uv and GitHub Actions |
