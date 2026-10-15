"""Upload handling shared by ``POST /api/imports/{kind}`` and the upload page (§4.6, §9)."""

import re
from dataclasses import dataclass

from fastapi import Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.importer import ImportResult, import_csv
from receivables.api.auth import error_response
from receivables.api.limits import MAX_UPLOAD_BYTES
from receivables.api.security import SessionUser
from receivables.core.validation import FILE_KINDS

__all__ = ["ImportRefusal", "run_import", "safe_filename"]

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_filename(name: str | None, kind: str) -> str:
    """The upload's base name without path segments or control characters (data only)."""
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = _CONTROL.sub("", base).strip()[:200]
    return cleaned or f"{kind}.csv"


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in {"", "false", "0", "no", "off"}


@dataclass(frozen=True, slots=True)
class ImportRefusal:
    status: int
    error: str
    detail: str

    def response(self) -> JSONResponse:
        return error_response(self.status, self.error, self.detail)


async def run_import(
    request: Request,
    kind: str,
    file: UploadFile | None,
    *,
    mode: str = "upsert",
    dry_run: str | None = None,
) -> ImportResult | ImportRefusal:
    if kind not in FILE_KINDS:
        return ImportRefusal(404, "not_found", f"Unknown file kind {kind[:40]!r}.")
    form = await request.form()  # options may come as query parameters or form fields
    form_dry_run, form_mode = form.get("dry_run"), form.get("mode")
    if _truthy(dry_run) or _truthy(form_dry_run if isinstance(form_dry_run, str) else None):
        return ImportRefusal(
            400, "not_supported", "dry_run previews the deferred mapping layer (T-API-08)."
        )
    if mode != "upsert" or (form_mode is not None and form_mode != "upsert"):
        return ImportRefusal(422, "invalid_request", "mode must be 'upsert'.")
    if file is None:
        return ImportRefusal(422, "invalid_request", "A CSV file field named 'file' is required.")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return ImportRefusal(413, "payload_too_large", "Uploads are limited to 5 MB.")
    user: SessionUser = request.state.user
    filename = safe_filename(file.filename, kind)
    return await run_in_threadpool(
        import_csv,
        request.app.state.engine,
        kind,
        raw,
        filename=filename,
        clock=request.app.state.clock,
        actor=user.username,
    )
