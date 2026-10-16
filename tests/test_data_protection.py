"""The data-protection statement and PROVENANCE.md stay true (SPEC §20.1, §20.2; C1.04; D-046)."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATEMENT_DOC = (ROOT / "docs" / "DATA_PROTECTION.md").read_text(encoding="utf-8")
PROVENANCE = (ROOT / "PROVENANCE.md").read_text(encoding="utf-8")

# SPEC §20.2, whose sentence 3 was reworded to match the code (D-046, option 1a).
STATEMENT = (
    "The competition prototype uses a fictional, seeded receivables dataset. It does not "
    "require or contain real company ledgers or customer correspondence. Financial "
    "calculations are deterministic; LLM output cannot change ledger amounts. Agent actions "
    "are limited to a fixed tool registry, an autonomy level and a kill switch; every message "
    "the agent sends to a customer also passes a deterministic policy engine. Outbound "
    "messages are written to a local sandbox outbox only. Inbound message content is treated "
    "as untrusted input and tested against instruction-injection attempts. Logs and demo "
    "artefacts are designed to exclude secrets and personal data. Simulator outcomes are "
    "illustrative and are not presented as evidence of real-world collections improvement."
)


def test_the_published_statement_is_the_specs() -> None:
    block = STATEMENT_DOC.split("<!-- STATEMENT:START -->", 1)[1]
    assert block.split("<!-- STATEMENT:END -->")[0].strip() == STATEMENT
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    assert spec.split("### 20.2", 1)[1].split("“", 1)[1].split("”", 1)[0] == STATEMENT


def test_every_piece_of_evidence_exists() -> None:
    table = STATEMENT_DOC.split("## Evidence for each sentence", 1)[1]
    rows = [r for r in table.splitlines() if re.match(r"^\| \d \|", r)]
    assert len(rows) == 7
    for row in rows:
        evidence = row.rstrip(" |").rsplit("|", 1)[1]
        paths = re.findall(r"`([^`]+)`", evidence)
        assert paths, row
        missing = [p for p in paths if not (ROOT / p).exists()]
        assert missing == [], (row[:6], missing)


DOCS = ["README.md", "docs/DEMO.md", "docs/ARCHITECTURE.md", "docs/DATA_PROTECTION.md"]


def prose_sentences(path: str) -> list[str]:
    """The prose of a document as sentences (code blocks and diagrams left out)."""
    raw = re.sub(r"```.*?```", "", (ROOT / path).read_text(encoding="utf-8"), flags=re.S)
    return re.split(r"(?<=[.!?])\s", re.sub(r"\s+", " ", raw))


@pytest.mark.parametrize("path", DOCS)
def test_no_document_claims_redaction_makes_real_data_safe(path: str) -> None:
    for sentence in prose_sentences(path):
        if re.search(r"redact", sentence, re.I) and re.search(r"\bsafe\b", sentence, re.I):
            # The negation must govern "safe": "does not make … safe", "never … safe".
            assert re.search(r"\b(?:not|never)\b[^.]*\bsafe\b", sentence, re.I), sentence


def test_provenance_lists_every_locked_package_at_its_locked_version() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    locked = {p["name"]: p["version"] for p in lock["package"] if p["name"] != project["name"]}
    listed = dict(re.findall(r"^\| ([a-z0-9][a-z0-9._-]*) \| ([0-9][^ |]*) \|", PROVENANCE, re.M))
    # A locked package for another platform only may be named in the platform note instead of
    # a row, and only if every dependency on it in uv.lock carries a platform marker.
    note = next(line for line in PROVENANCE.splitlines() if line.startswith("Locked but not"))
    for name in (n for n in locked if n not in listed):
        assert f"`{name}`" in note, name
        uses = [d for p in lock["package"] for d in p.get("dependencies", []) if d["name"] == name]
        assert uses and all("sys_platform" in d.get("marker", "") for d in uses), name
    assert sorted(set(listed) - set(locked)) == []  # no row for a package that is not locked
    assert {n: (listed[n], locked[n]) for n in listed if listed[n] != locked[n]} == {}


SPDX_USED = {"MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "MPL-2.0", "PSF-2.0"}


def test_provenance_has_an_spdx_licence_for_every_row() -> None:
    rows = re.findall(r"^\| ([a-z0-9][a-z0-9._-]*) \| [0-9][^ |]* \| ([^|]+?) \|", PROVENANCE, re.M)
    assert len(rows) >= 80
    for name, licence in rows:
        expression = licence.split(" (", 1)[0]  # a bracketed note may follow
        assert set(re.split(r"\s+(?:OR|AND)\s+", expression)) <= SPDX_USED, (name, licence)


OVERCLAIM = (
    r"(?:every|all)\b[^.;]{0,40}?\b(?:actions?|side[- ]effects?|steps?)\b[^.;]*?"
    r"(?:pass(?:es)?|go(?:es)? through|run through)[^.;]*?policy engine"
    r"|policy engine[^.;]*?between[^.;]*?every (?:side[- ]effect|action|step)"
)


@pytest.mark.parametrize("path", DOCS)
def test_no_document_claims_every_action_passes_the_policy_engine(path: str) -> None:
    # Only the messages the agent sends pass evaluate_send (SPEC §18.5, §18.6 step 5; D-046).
    assert [s for s in prose_sentences(path) if re.search(OVERCLAIM, s, re.I)] == []


@pytest.mark.parametrize(
    "claim",
    ["Every action passes a deterministic policy engine.",
     "every agent action passes the policy engine",
     "All actions pass through the policy engine.",
     "every side effect goes through the policy engine",
     "A policy engine sits between the LLM and every side effect."],
)  # fmt: skip
def test_the_overclaim_check_catches_the_known_phrasings(claim: str) -> None:
    assert re.search(OVERCLAIM, claim, re.I)
