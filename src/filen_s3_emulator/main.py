"""FastAPI entrypoint: an S3-compatible front for the Filen CLI's S3 gateway."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from filen_s3_emulator.config import Settings, get_settings
from filen_s3_emulator.errors import S3Error, s3_error_handler, unhandled_error_handler
from filen_s3_emulator.multipart import MultipartStore
from filen_s3_emulator.routes import router
from filen_s3_emulator.upstream import Upstream

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 3600


async def _sweep_forever(store: MultipartStore, max_age_seconds: float) -> None:
    while True:
        try:
            removed = await run_in_threadpool(store.sweep, max_age_seconds)
            if removed:
                log.info("removed %s abandoned multipart uploads", removed)
        except Exception:
            log.exception("multipart sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


def create_app(settings: Settings | None = None, upstream: Upstream | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.s3_access_key_id or not settings.s3_secret_access_key:
            raise RuntimeError("S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set")
        app.state.upstream = upstream or Upstream(
            settings.filen_endpoint,
            settings.s3_access_key_id,
            settings.s3_secret_access_key,
            region=settings.filen_region,
            cache_seconds=settings.list_cache_seconds,
        )
        sweeper = asyncio.create_task(
            _sweep_forever(app.state.store, settings.multipart_expiry_hours * 3600)
        )
        yield
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper

    # No docs routes: /docs and /redoc would shadow buckets of those names.
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        redirect_slashes=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.store = MultipartStore(Path(settings.staging_dir))
    app.add_exception_handler(S3Error, s3_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)  # type: ignore[arg-type]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "ETag",
            "Content-Range",
            "Content-Length",
            "Accept-Ranges",
            "x-amz-request-id",
        ],
    )

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        try:
            await asyncio.wait_for(run_in_threadpool(app.state.upstream.list_buckets), 10)
        except Exception as exc:
            return JSONResponse({"status": "unavailable", "error": str(exc)}, status_code=503)
        return {"status": "ok"}

    app.include_router(router)
    return app


app = create_app()
