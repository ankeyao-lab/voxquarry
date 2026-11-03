from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .store import RetailStore
from .research_context import annotate_product, remember_actor, research_context, context_markdown, manual_evidence


PACKAGE_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = PACKAGE_ROOT / "frontend"
DEFAULT_DATA_PATH = PACKAGE_ROOT / "var" / "retail-landscape.sqlite3"
MEDIA_ROOT = Path(os.environ.get("VOXQUARRY_RETAIL_MEDIA", str(PACKAGE_ROOT / "var" / "media")))
SCHEMA_ROOT = PACKAGE_ROOT / "schemas"


def create_app(
    data_path: Path | str = DEFAULT_DATA_PATH,
    *,
    store_runtime_mode: Literal["read_write", "read_only"] = "read_write",
    identity_resolver: Callable[[Request], dict[str, str]] | None = None,
) -> FastAPI:
    """Create the local preview app.

    ``read_only`` exists for contract verification and for the separate,
    authenticated product-master reader.  The preview keeps its historical
    read/write default; it must never be used as a production command plane.
    """
    store = RetailStore(data_path, runtime_mode=store_runtime_mode)
    if store_runtime_mode == "read_write":
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    app = FastAPI(
        title="VoxQuarry Retail Landscape API",
        version="0.1.0",
        description=(
            "Local development preview CRUD API. It is unauthenticated and must not be "
            "exposed as the production approved-data API."
        ),
    )
    app.state.retail_store = store

    def actor(request: Request) -> str:
        return getattr(request.state, "retail_actor", {}).get("principal_id", "local_preview")

    @app.middleware("http")
    async def reject_commands_from_read_only_runtime(request: Request, call_next: Any) -> Response:
        if identity_resolver and request.url.path != "/retail/api/v1/health":
            try:
                request.state.retail_actor = identity_resolver(request)
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    remember_actor(store, request.state.retail_actor)
            except (ValueError, KeyError):
                return JSONResponse(status_code=401, content={"detail": "请先登录，审核记录将绑定当前账号。"})
        if store_runtime_mode == "read_only" and request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse(
                status_code=405,
                content={
                    "error": {
                        "code": "read_only_runtime",
                        "message": "This retail runtime does not accept commands.",
                    }
                },
            )
        return await call_next(request)

    @app.get("/retail/api/v1/me")
    def me(request: Request) -> dict[str, Any]:
        return getattr(request.state, "retail_actor", {"principal_id": "local_preview", "display_name": "本地开发预览（未关联账号）"})

    @app.get("/retail/api/v1/research-context")
    def get_research_context(category: str, model_id: list[str] = Query(default=[]),
                             review_scope: str = "all", market_region: str = "", format: str = "json") -> Response:
        try:
            context = research_context(store, category=category, model_ids=model_id,
                                       review_scope=review_scope, market_region=market_region)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if format == "markdown":
            return Response(context_markdown(context), media_type="text/markdown; charset=utf-8")
        if format != "json":
            raise HTTPException(422, "format 必须为 json 或 markdown")
        return JSONResponse({**context, "material_markdown": context_markdown(context)})

    @app.get("/retail/api/v1/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "schema_version": "retail-landscape/v1.5",
            "product_master_schema_version": "retail-product-master/v2",
            "local_only": True,
            "api_mode": "local-development-preview",
        }

    @app.get("/retail/api/v1/summary")
    def summary() -> dict[str, Any]:
        return store.summary()

    @app.get("/retail/api/v1/categories")
    def categories() -> list[dict[str, Any]]:
        return store.list_categories()

    @app.get("/retail/api/v1/master/brands")
    def brands() -> list[dict[str, Any]]:
        return store.brand_summary()

    @app.get("/retail/api/v1/categories/{category_code}/fields")
    def fields(category_code: str) -> list[dict[str, Any]]:
        if not store.category(category_code):
            raise HTTPException(404, "category not found")
        return store.field_definitions(category_code)

    @app.get("/retail/api/v1/categories/{category_code}/voltage-breakdown")
    def voltage_breakdown(category_code: str) -> list[dict[str, Any]]:
        if not store.category(category_code):
            raise HTTPException(404, "category not found")
        return store.voltage_breakdown(category_code)

    @app.get("/retail/api/v1/master/market-regions")
    def market_regions() -> list[dict[str, Any]]:
        return store.market_regions()

    @app.get("/retail/api/v1/master/retailers")
    def retailers() -> list[dict[str, Any]]:
        return store.retailers()

    @app.post("/retail/api/v1/master/retailers", status_code=201)
    def create_retailer(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return store.save_retailer(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/retail/api/v1/master/retailers/{retailer_id}")
    def update_retailer(retailer_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return store.save_retailer(payload, retailer_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/retail/api/v1/master/retailers/{retailer_id}", status_code=204)
    def delete_retailer(retailer_id: str) -> Response:
        try:
            store.delete_retailer(retailer_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    @app.get("/retail/api/v1/master/units")
    def units() -> list[dict[str, Any]]:
        return store.units()

    @app.post("/retail/api/v1/master/units", status_code=201)
    def create_unit(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return store.save_unit(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/retail/api/v1/master/units/{unit_id}")
    def update_unit(unit_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return store.save_unit(payload, unit_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/retail/api/v1/master/units/{unit_id}", status_code=204)
    def delete_unit(unit_id: str) -> Response:
        try:
            store.delete_unit(unit_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    @app.post("/retail/api/v1/categories/{category_code}/parameters", status_code=201)
    def create_parameter(category_code: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return store.save_parameter(category_code, payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/retail/api/v1/categories/{category_code}/parameters/{field_id}")
    def update_parameter(category_code: str, field_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return store.save_parameter(category_code, payload, field_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/retail/api/v1/categories/{category_code}/parameters/{field_id}", status_code=204)
    def delete_parameter(category_code: str, field_id: str) -> Response:
        try:
            store.delete_parameter(category_code, field_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    @app.get("/retail/api/v1/products")
    def products(
        category: str = "",
        q: str = "",
        market_region: str = "",
        review_status: str = "",
        brand: str = "",
        energy_type: str = "",
        voltage_platform: str = "",
        retailer_id: str = "",
    ) -> list[dict[str, Any]]:
        if category and not store.category(category):
            raise HTTPException(404, "category not found")
        return [annotate_product(store, p) for code in ([category] if category else [c["code"] for c in store.list_categories()]) for p in store.list_products(
            code,
            q=q,
            market_region=market_region,
            review_status=review_status,
            brand=brand,
            energy_type=energy_type,
            voltage_platform=voltage_platform,
            retailer_id=retailer_id,
        )]

    @app.get("/retail/api/v1/products/{product_id}")
    def product(product_id: str) -> dict[str, Any]:
        result = store.get_product(product_id)
        if not result:
            raise HTTPException(404, "product not found")
        return annotate_product(store, result)

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
        if status_code != 200:
            if status_code == 304:
                return Response(status_code=304, headers=headers)
            return JSONResponse(status_code=status_code, content=payload, headers=headers)
        return Response(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            media_type="application/json",
            headers=headers,
        )

    @app.put("/retail/api/v2/product-master/brands/{brand_id}")
    def rename_brand(brand_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return store.rename_brand(
                brand_id,
                str(payload.get("canonical_name") or ""),
                aliases=[str(item) for item in payload.get("aliases") or []],
            )
        except KeyError as exc:
            raise HTTPException(404, "brand not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/retail/api/v1/products", status_code=201)
    def create_product(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return annotate_product(store, store.create_product(manual_evidence(payload, actor(request)), actor=actor(request)))
        except sqlite_error as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/retail/api/v1/products/{product_id}")
    def update_product(product_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return annotate_product(store, store.update_product(product_id, manual_evidence(payload, actor(request)), actor=actor(request)))
        except KeyError as exc:
            raise HTTPException(404, "product not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/retail/api/v1/products/{product_id}")
    def delete_product(product_id: str) -> dict[str, Any]:
        try:
            return store.delete_product(product_id)
        except KeyError as exc:
            raise HTTPException(404, "product not found") from exc

    @app.post("/retail/api/v1/products/{product_id}/review")
    def review_product(product_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            if not isinstance(payload.get("approved"), bool):
                raise ValueError("approved 必须为布尔值")
            return store.review_product(
                product_id,
                approved=bool(payload.get("approved")),
                reviewer=actor(request) if identity_resolver else str(payload.get("reviewer") or "local_preview"),
                note=str(payload.get("note") or ""),
                expected_version=payload.get("expected_version"),
            )
        except KeyError as exc:
            raise HTTPException(404, "product not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/retail/api/v1/products/{product_id}/review-preview")
    def review_preview(product_id: str) -> dict[str, Any]:
        try:
            return store.review_preview(product_id)
        except KeyError as exc:
            raise HTTPException(404, "product not found") from exc

    @app.post("/retail/api/v1/reviews/bulk")
    def bulk_review(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            if not isinstance(payload.get("approved"), bool):
                raise ValueError("approved 必须为布尔值")
            return store.bulk_review(
                [str(item) for item in payload.get("product_ids") or []],
                approved=bool(payload.get("approved")),
                reviewer=actor(request) if identity_resolver else str(payload.get("reviewer") or "local_preview"),
                note=str(payload.get("note") or ""),
                expected_versions=payload.get("expected_versions"),
            )
        except KeyError as exc:
            raise HTTPException(404, "product not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/retail/api/v1/products/{product_id}/attachments", status_code=201)
    def upload_attachment(product_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not store.get_product(product_id):
            raise HTTPException(404, "product not found")
        filename = Path(str(payload.get("filename") or "image")).name
        content_type = str(payload.get("content_type") or "").casefold()
        extensions = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        if content_type not in extensions:
            raise HTTPException(422, "图片附件仅支持 JPEG、PNG 或 WebP")
        try:
            content = base64.b64decode(str(payload.get("data_base64") or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "图片附件编码无效") from exc
        if not content or len(content) > 10 * 1024 * 1024:
            raise HTTPException(422, "单张图片必须小于 10MB")
        digest = hashlib.sha256(content).hexdigest()
        relative = f"{product_id}/{digest}.{extensions[content_type]}"
        target = MEDIA_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
        try:
            return store.add_attachment_asset(
                product_id,
                storage_path=relative,
                original_filename=filename,
                content_hash=f"sha256:{digest}",
                file_size=len(content),
                make_hero=bool(payload.get("make_hero")),
                source_url=str(payload.get("source_url") or "") or None,
                source_type=str(payload.get("source_type") or "local_attachment"),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/retail/api/v1/products/{product_id}/link-assets", status_code=201)
    def add_link_asset(product_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return store.add_link_asset(
                product_id,
                role=str(payload.get("role") or ""),
                url=str(payload.get("url") or ""),
            )
        except KeyError as exc:
            raise HTTPException(404, "product not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/retail/api/v1/products/{product_id}/assets/{asset_id}/hero")
    def set_hero(product_id: str, asset_id: str) -> dict[str, Any]:
        try:
            return store.set_hero_asset(product_id, asset_id)
        except KeyError as exc:
            raise HTTPException(404, "asset not found") from exc

    @app.delete("/retail/api/v1/products/{product_id}/assets/{asset_id}")
    def delete_asset(product_id: str, asset_id: str) -> dict[str, Any]:
        try:
            return store.delete_asset(product_id, asset_id)
        except KeyError as exc:
            raise HTTPException(404, "asset not found") from exc

    @app.get("/retail/api/v1/exports.json")
    def export_json(
        category: str = Query(...), q: str = "", market_region: str = "",
        review_status: str = "", brand: str = "", energy_type: str = "",
        voltage_platform: str = "", retailer_id: str = "",
    ) -> Response:
        rows = store.list_products(
            category, q=q, market_region=market_region, review_status=review_status,
            brand=brand, energy_type=energy_type, voltage_platform=voltage_platform,
            retailer_id=retailer_id,
        )
        payload = {
            "schema_version": "retail-landscape-preview/v1.5",
            "category": category,
            "review_scope": "local-preview-includes-pending",
            "contains_unreviewed": any(p["review_status"] not in {"approved", "published"} for p in rows),
            "products": [annotate_product(store, p) for p in rows],
        }
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{category}-retail-landscape.json"'},
        )

    @app.get("/retail/api/v1/exports.csv")
    def export_csv(
        category: str = Query(...), q: str = "", market_region: str = "",
        review_status: str = "", brand: str = "", energy_type: str = "",
        voltage_platform: str = "", retailer_id: str = "",
    ) -> Response:
        rows = store.list_products(
            category, q=q, market_region=market_region, review_status=review_status,
            brand=brand, energy_type=energy_type, voltage_platform=voltage_platform,
            retailer_id=retailer_id,
        )
        output = io.StringIO()
        writer = csv.writer(output)
        fields = [field for field in store.field_definitions(category) if field.get("is_matrix")]
        writer.writerow([
            "model_uid", "brand", "model_number", "energy_type", "voltage_platform",
            "review_status", "reviewer", "reviewed_at", "review_notice", "review_count", "review_count_status", "review_count_as_of",
            "market_regions", "retailers", "listing_urls", "retail_prices", "notes",
            *[f"{field['label']} ({field.get('canonical_unit') or ''})" for field in fields],
        ])
        for product in rows:
            product = annotate_product(store, product)
            writer.writerow([
                product["id"], product["brand"], product["model_number"],
                product.get("energy_type"), product.get("voltage_platform"),
                product["review_status"], (product.get("reviewer") or {}).get("display_name"),
                product.get("reviewed_at"), product["review_notice"], product.get("review_count"),
                product.get("review_count_status"), product.get("review_count_as_of"),
                " | ".join(item["name"] for item in product["markets"]),
                " | ".join(item["name"] for item in product["retailers"]),
                " | ".join(item["online_url"] for item in product["listings"]),
                " | ".join(
                    f"{item.get('retailer') or 'Listing'} · "
                    f"{item['currency_code']} {item['normalized_amount']:g} · "
                    f"{item.get('online_url') or ''}"
                    for item in product.get("retail_prices", [])
                ),
                product.get("notes"),
                *[(product["specs"].get(field["code"]) or {}).get("numeric_value") for field in fields],
            ])
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{category}-retail-landscape.csv"'},
        )

    @app.get("/retail/")
    def index() -> FileResponse:
        return FileResponse(FRONTEND_ROOT / "index.html")

    app.mount("/retail/static", StaticFiles(directory=FRONTEND_ROOT), name="retail-static")
    app.mount(
        "/retail/media",
        StaticFiles(directory=MEDIA_ROOT, check_dir=store_runtime_mode == "read_write"),
        name="retail-media",
    )
    return app


try:
    import sqlite3

    sqlite_error = sqlite3.IntegrityError
except ImportError:  # pragma: no cover
    sqlite_error = RuntimeError


app = create_app(
    Path(os.environ.get("RETAIL_DATA_PATH", DEFAULT_DATA_PATH)),
    store_runtime_mode=os.environ.get("RETAIL_STORE_RUNTIME_MODE", "read_write"),
)
