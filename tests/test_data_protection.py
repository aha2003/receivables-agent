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
    "to a customer also passes a deterministic policy engine. Outbound messages are written to "
    "a local sandbox outbox only. Inbound message content is treated as untrusted input and "
    "tested against instruction-injection attempts. Logs and demo artefacts are designed to "
    "exclude secrets and personal data. Simulator outcomes are illustrative and are not "
    "presented as evidence of real-world collections improvement."
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


@pytest.mark.parametrize(
    "path", ["README.md", "docs/DEMO.md", "docs/ARCHITECTURE.md", "docs/DATA_PROTECTION.md"]
)
def test_no_document_claims_redaction_makes_real_data_safe(path: str) -> None:
    raw = re.sub(r"```.*?```", "", (ROOT / path).read_text(encoding="utf-8"), flags=re.S)
    text = re.sub(r"\s+", " ", raw)  # prose only: diagrams are not sentences
    for sentence in re.split(r"(?<=[.!?])\s", text):
        if re.search(r"redact", sentence, re.I) and re.search(r"\bsafe\b", sentence, re.I):
            assert re.search(r"\b(?:not|never)\b", sentence, re.I), sentence


def test_provenance_lists_every_locked_package_at_its_locked_version() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    locked = {p["name"]: p["version"] for p in lock["package"] if p["name"] != project["name"]}
    listed = dict(re.findall(r"^\| ([a-z0-9][a-z0-9._-]*) \| ([0-9][^ |]*) \|", PROVENANCE, re.M))
    # A locked package for another platform only may be named in a note instead of a row.
    unlisted = {name for name in locked if name not in listed}
    assert all(f"`{name}`" in PROVENANCE for name in unlisted), unlisted
    assert sorted(set(listed) - set(locked)) == []  # no row for a package that is not locked
    assert {n: (listed[n], locked[n]) for n in listed if listed[n] != locked[n]} == {}


def test_provenance_has_a_licence_for_every_row() -> None:
    rows = re.findall(r"^\| ([a-z0-9][a-z0-9._-]*) \| [0-9][^ |]* \| ([^|]+?) \|", PROVENANCE, re.M)
    assert len(rows) >= 80
    assert all(re.match(r"[A-Z][\w.-]*(?:\s+(?:OR|AND)\s+[A-Z][\w.-]*)*", lic) for _, lic in rows)


@pytest.mark.parametrize(
    "path", ["README.md", "docs/DEMO.md", "docs/ARCHITECTURE.md", "docs/DATA_PROTECTION.md"]
)
def test_no_document_claims_every_action_passes_the_policy_engine(path: str) -> None:
    # Only messages to customers pass evaluate_send (SPEC §18.5, §18.6 step 5; D-046).
    raw = re.sub(r"```.*?```", "", (ROOT / path).read_text(encoding="utf-8"), flags=re.S)
    text = re.sub(r"\s+", " ", raw)
    overclaim = (
        r"every (?:action|side effect|step)[^.;]*pass(?:es)?(?: through)? "
        r"(?:the|a) (?:deterministic )?policy engine"
    )
    assert not re.search(overclaim, text, re.I)
