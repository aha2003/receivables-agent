"""Upload handling shared by ``POST /api/imports/{kind}`` and the upload page (§4.6, §9)."""

from dataclasses import dataclass

from fastapi import Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.importer import ImportResult, import_csv
from receivables.api.auth import error_response
from receivables.api.limits import MAX_UPLOAD_BYTES
from receivables.api.security import SessionUser
from receivables.core.validation import FILE_KINDS

__all__ = ["ImportRefusal", "run_import"]

LATER_KINDS = {"correspondence": "S3.06"}


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
    if kind in LATER_KINDS:
        return ImportRefusal(
            501, "not_implemented", f"{kind} import arrives in {LATER_KINDS[kind]}."
        )
    if kind not in FILE_KINDS:
        return ImportRefusal(404, "not_found", f"Unknown file kind {kind[:40]!r}.")
    if dry_run is not None and dry_run.lower() not in {"false", "0", ""}:
        return ImportRefusal(
            400, "not_supported", "dry_run previews the deferred mapping layer (T-API-08)."
        )
    if mode != "upsert":
        return ImportRefusal(422, "invalid_request", "mode must be 'upsert'.")
    if file is None:
        return ImportRefusal(422, "invalid_request", "A CSV file field named 'file' is required.")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return ImportRefusal(413, "payload_too_large", "Uploads are limited to 5 MB.")
    user: SessionUser = request.state.user
    filename = (file.filename or f"{kind}.csv")[:200]
    return await run_in_threadpool(
        import_csv,
        request.app.state.engine,
        kind,
        raw,
        filename=filename,
        clock=request.app.state.clock,
        actor=user.username,
    )
