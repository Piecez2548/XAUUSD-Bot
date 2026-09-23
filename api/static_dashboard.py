"""Production static dashboard serving with safe SPA history fallback."""

from __future__ import annotations

from pathlib import Path

from starlette.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles


class DashboardStaticFiles(StaticFiles):
    """Serve a Vite build without masking API or asset failures."""

    def __init__(self, directory: Path) -> None:
        # The directory may be absent when Node/npm is unavailable.  The API
        # must still start and return a normal 404 instead of crashing.
        super().__init__(directory=str(directory), html=False, check_dir=False)

    async def check_config(self) -> None:
        """Allow the API to start while the optional build is unavailable."""

        return None

    async def get_response(self, path: str, scope):
        method = str(scope.get("method", "GET")).upper()
        if method not in {"GET", "HEAD"}:
            return PlainTextResponse("Method Not Allowed", status_code=405)

        # StaticFiles uses the host OS separator when deriving the mounted
        # path.  Normalize it before applying route-boundary decisions.
        normalized = path.replace("\\", "/").strip("/")
        if normalized in {"", ".", "index.html"}:
            return await super().get_response("index.html", scope)

        # API routes retain their own routing and status semantics.  An
        # unknown API path must never become the SPA shell.
        if normalized == "api" or normalized.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            response = PlainTextResponse("Not Found", status_code=404)
        if response.status_code != 404:
            return response

        # Assets and file-like paths must return a real 404.  Only navigation
        # paths without a filename extension are eligible for SPA fallback.
        filename = normalized.rsplit("/", 1)[-1]
        if normalized.startswith("assets/") or "." in filename:
            return response
        return await super().get_response("index.html", scope)


__all__ = ["DashboardStaticFiles"]
