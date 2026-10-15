"""scripts/live_smoke.py is safe to keep in the repository (D-032).

It refuses to run without live mode and its settings, prints no key, model id or raw output,
and CI never runs it. The live API is never called here: refusals happen before any client is
built, and the happy path runs with a FakeClient in its place.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from llm_capture import CLASSIFICATION

from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.redacting import RedactingClient

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_smoke.py"
FAKE_KEY = "test-only-not-a-real-anthropic-key-0123456789"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a fixed command: this interpreter and the script
        [sys.executable, str(SCRIPT)],
        env={**os.environ, **env},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_it_refuses_without_live_mode() -> None:
    done = _run({"ANTHROPIC_API_KEY": FAKE_KEY})
    assert done.returncode == 2
    assert "set LLM_MODE=live" in done.stderr and FAKE_KEY not in done.stdout + done.stderr


def test_it_refuses_live_mode_without_model_ids() -> None:
    done = _run({"LLM_MODE": "live", "ANTHROPIC_API_KEY": FAKE_KEY})
    assert done.returncode == 2
    assert "ANTHROPIC_MODEL_CLASSIFIER" in done.stderr
    assert FAKE_KEY not in done.stdout + done.stderr


def test_it_refuses_while_the_sdk_would_log_the_request() -> None:
    done = _run({"LLM_MODE": "live", "ANTHROPIC_API_KEY": FAKE_KEY, "ANTHROPIC_LOG": "debug"})
    assert done.returncode == 2
    assert "unset ANTHROPIC_LOG" in done.stderr and FAKE_KEY not in done.stdout + done.stderr


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_prints_a_summary_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    fake = FakeClient(classify=[CLASSIFICATION])
    sent: list[str] = []

    def make(settings: object, config: object, *, customer_names: object) -> RedactingClient:
        client = RedactingClient(
            fake,
            redact_before_send=True,
            pseudonymize_customer_names=True,
            customer_names=customer_names,  # type: ignore[arg-type]
        )
        sent.append("built")
        return client

    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setattr(module, "make_llm_client", make)
    configured: list[object] = []
    monkeypatch.setattr(module, "configure_logging", configured.append)
    assert module.main() == 0
    assert len(configured) == 1  # the scrubbed pipeline is in place before the call
    out = capsys.readouterr().out
    assert "label:        PO_AMOUNT_MISMATCH (expected PO_AMOUNT_MISMATCH)" in out
    assert "attempts:     1" in out and "tokens:       None in, None out" in out
    assert "PO amount differs" not in out  # no raw model output (summary_en)
    assert sent == ["built"] and len(fake.calls) == 1


def test_ci_never_runs_it() -> None:
    for workflow in (ROOT / ".github" / "workflows").glob("*.y*ml"):
        assert "live_smoke" not in workflow.read_text(encoding="utf-8"), workflow.name
