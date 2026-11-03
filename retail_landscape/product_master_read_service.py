"""Dedicated read-only HTTP adapter for retail-product-master/v2."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .store import RetailStore


SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas"


def create_product_master_read_app(
    data_path: Path | str,
    *,
    required_category_manifest_id: str,
) -> FastAPI:
    """Create the v2 reader after fail-closed storage readiness succeeds."""
    store = RetailStore(
        data_path,
        runtime_mode="read_only",
        require_filesystem_read_only=True,
        required_category_manifest_id=required_category_manifest_id,
    )
    app = FastAPI(
        title="VoxQuarry Chengmu Product Master Read Service",
        version="retail-product-master/v2",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.retail_store = store

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "chengmu-product-master-read",
            "schema_version": "retail-product-master/v2",
            "storage_mode": "sqlite-read-only",
            "commands_enabled": False,
            "category_manifest_id": required_category_manifest_id,
        }

    @app.get("/retail/api/v2/product-master/schema")
    def product_master_schema() -> FileResponse:
        return FileResponse(
            SCHEMA_ROOT / "retail-product-master-v2.schema.json",
            media_type="application/schema+json",
        )

    @app.get("/retail/api/v2/product-master/models")
    def product_master_models(
        request: Request,
        category: Optional[str] = None,
        model_id: list[str] = Query(default=[]),
        voltage_platform_id: list[str] = Query(default=[]),
    ) -> Response:
        status_code, payload, headers = store.product_master_http_response(
            category_code=category,
            model_ids=model_id,
            voltage_platform_ids=voltage_platform_id,
            if_none_match=request.headers.get("if-none-match"),
        )
        if status_code == 304:
            return Response(status_code=304, headers=headers)
        if status_code != 200:
            return JSONResponse(status_code=status_code, content=payload, headers=headers)
        return Response(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            media_type="application/json",
            headers=headers,
        )

    return app
