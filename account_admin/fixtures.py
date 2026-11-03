from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .constants import SERVICE_AUTH_SOURCE


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


class FixtureError(ValueError):
    pass


def _load(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FixtureError("fixture root must be an object")
    if value.get("environment") != "local_dev_synthetic" or value.get("auth_source") != SERVICE_AUTH_SOURCE:
        raise FixtureError("fixture must be explicitly local_dev_synthetic/local_fixture")
    if value.get("non_production") is not True:
        raise FixtureError("fixture must be marked non-production")
    return value


def load_principal_fixtures() -> dict[str, Any]:
    value = _load("local-dev-identities-v1.json")
    if value.get("schema_version") != "local-fixture-identities/v1":
        raise FixtureError("unsupported principal fixture schema")
    return value


def load_service_fixtures() -> dict[str, Any]:
    value = _load("local-dev-service-principals-v1.json")
    if value.get("schema_version") != "local-fixture-service-principals/v1":
        raise FixtureError("unsupported service fixture schema")
    return value
