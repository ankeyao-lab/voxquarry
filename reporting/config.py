from __future__ import annotations

from pathlib import Path


SCHEMA_VERSION = "report-json/v1"
ANALYSIS_VERSION = "voxquarry-reporting/0.13.2"
LEGACY_FACT_SCHEMA_VERSION = "review-fact/v1"
LEGACY_PARSER_VERSION = "review-parser/1.0.0"
LEGACY_NORMALIZATION_VERSION = "review-normalization/1.0.0"
FACT_SCHEMA_VERSION = "review-fact/v2"
PARSER_VERSION = "review-parser/2.0.0"
NORMALIZATION_VERSION = "review-normalization/2.0.0"
MARKET_CONTRACT_VERSION = "review-market-eligibility/1.1.0"
MARKET_REGIONS = (
    "North America",
    "Europe",
    "Australia",
    "Other",
)
SELECTION_POLICY_VERSION = "complete-first-partial-fallback/1.2.0"
DICTIONARY_VERSION = "voxquarry-review-factors/1.3.0"
RESEARCH_PLAN_VERSION = "full-category/1.2.0"
ATOMIC_SCRIPT_REGISTRY_VERSION = "atomic-extractors/2.0.0"
LOW_SAMPLE_N = 20
ELIGIBLE_VALIDATION_STATUSES = {
    "COMPLETE",
    "COMPLETE_FALLBACK_SOURCE",
    "PARTIAL_SUCCESS",
}

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
DEFAULT_VAR_ROOT = PACKAGE_ROOT / "var"
DEFAULT_FRONTEND_ROOT = REPO_ROOT / "web" / "dist" / "client"
