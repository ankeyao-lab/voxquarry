"""Uvicorn factory for the isolated product-master read process."""
from __future__ import annotations

import os

from .product_master_read_service import create_product_master_read_app


def create_runtime_app():
    data_path = os.environ.get("VOXQUARRY_RETAIL_DATA_PATH", "").strip()
    if not data_path:
        raise RuntimeError("VOXQUARRY_RETAIL_DATA_PATH is required")
    category_manifest_id = os.environ.get(
        "VOXQUARRY_RETAIL_CATEGORY_MANIFEST_ID", ""
    ).strip()
    if not category_manifest_id:
        raise RuntimeError("VOXQUARRY_RETAIL_CATEGORY_MANIFEST_ID is required")
    return create_product_master_read_app(
        data_path,
        required_category_manifest_id=category_manifest_id,
    )
