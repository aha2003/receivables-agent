# Golden synthetic fixtures

**All data here is fictional.** Customers, TRNs, licence numbers, emails (`*.example`), invoices, payments and messages were invented for the specification; any resemblance to real entities is coincidental.

| File | Source |
|---|---|
| `customers.csv`, `invoices.csv`, `payments.csv`, `events.csv`, `bad_invoices.csv` | SPEC §6.8, byte-identical to the spec blocks |
| `correspondence_labeled.csv` | SPEC §6.10 (header chosen in docs/DECISIONS.md D-009) |
| `expected/golden_results.json` | SPEC §6.9, `as_of = 2026-10-31`; amounts and scores are decimal strings |

`tests/unit/test_fixtures.py` checks these files against the spec and for internal consistency. Never edit a fixture to make a test pass (CLAUDE.md); change the spec first and log it in `docs/DECISIONS.md`.
