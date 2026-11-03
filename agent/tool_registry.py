from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import AgentError, TOOL_REGISTRY_CONTRACT, sha256_value

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError
except ImportError:  # pragma: no cover - readiness fails closed without the dependency.
    Draft202012Validator = None  # type: ignore[assignment]
    ValidationError = Exception  # type: ignore[assignment,misc]


ZHUOJIAN_TOOL_NAMES = (
    "zhuojian.list_reports@v1",
    "zhuojian.get_report@v1",
    "zhuojian.get_metric@v1",
    "zhuojian.list_evidence_groups@v1",
    "zhuojian.query_evidence@v1",
    "zhuojian.get_data_quality@v1",
)
RESEARCH_TOOL_NAMES = (
    "zhuojian.get_report_section@v1",
    "zhuojian.query_report_reviews@v1",
    "zhuojian.aggregate_report_reviews@v1",
)

FORBIDDEN_TOOL_TOKENS = (
    "shell",
    "filesystem",
    "file_system",
    "web",
    "http",
    "sql",
    "subagent",
    "delete",
    "cancel",
    "deploy",
    "production",
)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    owner: str
    effect: str
    dependency: str
    dependency_status: str
    input_schema: dict[str, Any]
    input_schema_sha256: str
    response_contract: str
    confirmation: str
    timeout_ms: int
    max_response_bytes: int

    def public(self, *, include_schema: bool = False) -> dict[str, Any]:
        item = {
            "name": self.name,
            "owner": self.owner,
            "effect": self.effect,
            "dependency": self.dependency,
            "dependency_status": self.dependency_status,
            "input_schema_sha256": self.input_schema_sha256,
            "response_contract": self.response_contract,
            "confirmation": self.confirmation,
            "timeout_ms": self.timeout_ms,
            "max_response_bytes": self.max_response_bytes,
        }
        if include_schema:
            item["input_schema"] = self.input_schema
        return item


class ToolRegistry:
    def __init__(
        self,
        *,
        schema_root: Path | None = None,
        zhuojian_status: str = "unavailable",
        enable_research: bool = False,
    ) -> None:
        if zhuojian_status not in {"ready", "unavailable", "synthetic_test_only"}:
            raise ValueError("invalid zhuojian dependency status")
        root = schema_root or Path(__file__).resolve().parents[1] / "reporting" / "schemas"
        self.enable_research = enable_research
        self._names = ZHUOJIAN_TOOL_NAMES + (RESEARCH_TOOL_NAMES if enable_research else ())
        definitions: dict[str, ToolDefinition] = {}
        for name in self._names:
            filename = name.replace("@", "-") + ".schema.json"
            schema = json.loads((root / filename).read_text(encoding="utf-8"))
            if schema.get("additionalProperties") is not False:
                raise AgentError(
                    "tool_contract_invalid",
                    f"{name} 输入 Schema 未关闭未知字段",
                    http_status=503,
                )
            definitions[name] = ToolDefinition(
                name=name,
                owner="zhuojian-reporting",
                effect="read_only",
                dependency="zhuojian-agent-tools/v1",
                dependency_status=zhuojian_status,
                input_schema=schema,
                input_schema_sha256=sha256_value(schema),
                response_contract="zhuojian-agent-tools/v1",
                confirmation="none",
                timeout_ms=120000 if name in RESEARCH_TOOL_NAMES else 10000,
                max_response_bytes=1_000_000,
            )
        self._definitions = definitions
        self._validate_boundary()

    def _validate_boundary(self) -> None:
        if tuple(self._definitions) != self._names:
            raise AgentError("tool_contract_invalid", "AI 研究工具清单漂移", http_status=503)
        for name, definition in self._definitions.items():
            lower = name.lower()
            if any(token in lower for token in FORBIDDEN_TOOL_TOKENS):
                raise AgentError("tool_contract_invalid", f"禁止工具进入 registry：{name}", http_status=503)
            if definition.effect != "read_only":
                raise AgentError("tool_contract_invalid", "上游 mutation 不得注册", http_status=503)

    @property
    def dependency_status(self) -> str:
        return next(iter(self._definitions.values())).dependency_status

    def get(self, name: str) -> ToolDefinition:
        definition = self._definitions.get(name)
        if definition is None:
            raise AgentError(
                "capability_unavailable",
                "请求的工具未向AI 研究开放",
                http_status=404,
            )
        return definition

    def validate_input(self, name: str, payload: object) -> ToolDefinition:
        definition = self.get(name)
        if Draft202012Validator is None:
            raise AgentError(
                "schema_validator_unavailable",
                "工具输入校验器不可用",
                http_status=503,
            )
        try:
            Draft202012Validator(definition.input_schema).validate(payload)
        except ValidationError as exc:
            raise AgentError("invalid_request", "工具输入不符合闭合 Schema") from exc
        return definition

    def payload(self, *, include_schema: bool = False, model_visible_only: bool = False) -> dict[str, Any]:
        items = []
        for definition in self._definitions.values():
            if model_visible_only and definition.dependency_status != "ready":
                continue
            items.append(definition.public(include_schema=include_schema))
        return {
            "contract": TOOL_REGISTRY_CONTRACT,
            "catalog_version": "wenquan-report-research/1.0.0" if self.enable_research else "wenquan-r1-core/1.0.0",
            "tools": items,
            "model_visible_tool_count": sum(
                1 for item in self._definitions.values() if item.dependency_status == "ready"
            ),
            "forbidden_capabilities": [
                "shell",
                "filesystem",
                "web",
                "arbitrary_http",
                "sql",
                "subagent",
                "production",
                "upstream_mutation",
            ],
        }
