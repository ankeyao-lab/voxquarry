from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import WenquanSettings
from .contracts import (
    CORE_CONTRACT,
    AgentError,
    IdentityContext,
    require_exact_fields,
    sha256_value,
)
from .identity import AccountAdminIdentityProvider, TrustedHeaderIdentityProvider
from .llm import OpenAICompatibleLLMRuntime
from .planner import SemanticPlanner
from .research import ReportResearchQA
from .service import WenquanService
from .store import WenquanStore
from .tool_registry import ToolRegistry
from .upstream import HttpZhuojianTransport, ToolExecutor

try:
    from fastapi import Depends, FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # pragma: no cover - runtime verification installs declared dependencies.
    Depends = FastAPI = Request = FileResponse = JSONResponse = StaticFiles = None  # type: ignore[assignment,misc]


FEEDBACK_REASONS = {
    "wrong_scope",
    "wrong_tool_sequence",
    "unnecessary_confirmation",
    "untrusted_number_or_evidence",
    "off_topic",
    "unclear_failure",
    "artifact_unavailable",
    "too_slow",
    "other",
}


def create_app(
    *,
    service: WenquanService,
    identity_provider: TrustedHeaderIdentityProvider,
    research_backend: Any = None,
) -> Any:
    if FastAPI is None:
        raise RuntimeError("FastAPI runtime dependency unavailable")
    app = FastAPI(
        title="VoxQuarry Wenquan",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/wenquan/api/v1/openapi.json",
    )
    app.state.wenquan = service
    app.state.identity_provider = identity_provider
    readonly = ReportResearchQA(service, recover=True)
    app.state.readonly = readonly
    app.state.research = research_backend
    frontend_root = Path(__file__).resolve().parent / "frontend"
    app.mount(
        "/wenquan/assets",
        StaticFiles(directory=frontend_root),
        name="wenquan-assets",
    )

    @app.exception_handler(AgentError)
    async def agent_error_handler(_request: Request, error: AgentError) -> JSONResponse:
        return JSONResponse(
            status_code=error.http_status,
            content={
                "contract": "voxquarry-wenquan-error/v1",
                "status": "error",
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "safe_next_step": error.safe_next_step,
                },
            },
        )

    def identity(request: Request) -> IdentityContext:
        return identity_provider.resolve(
            {key.lower(): value for key, value in request.headers.items()}
        )

    @app.get("/health/live")
    def live() -> dict[str, Any]:
        return {
            "contract": "voxquarry-wenquan-health/v1",
            "status": "live",
            "service": "wenquan",
        }

    @app.get("/ready")
    def ready() -> JSONResponse:
        payload = service.readiness()
        return JSONResponse(status_code=200 if payload["ready"] else 503, content=payload)

    @app.get("/dependencies")
    def dependencies() -> dict[str, Any]:
        return service.dependencies()

    @app.get("/wenquan/api/v1/dependencies")
    def scoped_dependencies(actor: IdentityContext = Depends(identity)) -> dict[str, Any]:
        del actor
        payload = service.dependencies()
        payload["dependencies"]["llm"]["status"] = "configured" if service.planner else "unavailable"
        payload["dependencies"]["llm"]["verification"] = "validated_per_turn"
        payload["dependencies"]["zhuojian"]["verification"] = "validated_on_report_read"
        payload["capabilities"] = {"existing_report_qa": True, "create_report": readonly.generation_enabled, "collect": False}
        if research_backend is not None:
            payload["capabilities"].update(harness_research=True, raw_datasets=True, attachments=True, collect=True, web_search=True)
        return payload

    @app.get("/wenquan/api/v1/reports")
    def report_catalog(actor: IdentityContext = Depends(identity)) -> dict[str, Any]:
        return {"contract": CORE_CONTRACT, **readonly.reports(actor)}

    @app.get("/wenquan", include_in_schema=False)
    @app.get("/wenquan/", include_in_schema=False)
    def frontend() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[1] / "web" / "dist" / "client" / "index.html")

    @app.get("/wenquan/api/v1/tool-registry")
    def tool_registry(
        include_schema: bool = False,
        actor: IdentityContext = Depends(identity),
    ) -> dict[str, Any]:
        del actor
        return service.registry.payload(include_schema=include_schema)

    @app.post("/wenquan/api/v1/sessions", status_code=201)
    def create_session(
        payload: dict[str, Any], actor: IdentityContext = Depends(identity)
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload,
            allowed={"title"},
            required={"title"},
            label="session request",
        )
        return {"contract": CORE_CONTRACT, "session": service.create_session(actor, value["title"])}

    @app.get("/wenquan/api/v1/sessions")
    def list_sessions(
        include_archived: bool = False,
        actor: IdentityContext = Depends(identity),
    ) -> dict[str, Any]:
        items = service.store.list_sessions(actor, include_archived=include_archived)
        if research_backend is not None:
            activity = research_backend.state.session_activity(actor, [i['session_id'] for i in items])
            items = [{**i, 'activity': activity.get(i['session_id'], {})} for i in items]
        return {
            "contract": CORE_CONTRACT,
            "owner_user_id": actor.owner_user_id,
            "scope_id": actor.scope_id,
            "items": items,
        }

    @app.get("/wenquan/api/v1/sessions/{session_id}")
    def get_session(
        session_id: str, actor: IdentityContext = Depends(identity)
    ) -> dict[str, Any]:
        bundle = service.session_bundle(actor, session_id)
        if research_backend is not None:
            research_backend.refresh_waiting(actor, session_id)
            bundle = research_backend.state.enrich_bundle(actor, bundle)
        return {"contract": CORE_CONTRACT, **bundle}

    @app.patch("/wenquan/api/v1/sessions/{session_id}")
    def update_session(
        session_id: str,
        payload: dict[str, Any],
        actor: IdentityContext = Depends(identity),
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload, allowed={"title", "archived"}, label="session update"
        )
        if not value:
            raise AgentError("invalid_request", "session update 不能为空")
        if "archived" in value and not isinstance(value["archived"], bool):
            raise AgentError("invalid_request", "archived 必须是布尔值")
        return {
            "contract": CORE_CONTRACT,
            "session": service.store.update_session(
                actor,
                session_id,
                title=value.get("title"),
                archived=value.get("archived"),
            ),
        }

    @app.post("/wenquan/api/v1/sessions/{session_id}/answers")
    def answer(
        session_id: str,
        payload: dict[str, Any],
        actor: IdentityContext = Depends(identity),
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload,
            allowed={"question", "report_id", "metric_id", "evidence_group_id"},
            required={"question", "report_id", "metric_id", "evidence_group_id"},
            label="answer request",
        )
        return {
            "contract": CORE_CONTRACT,
            **service.answer_metric_with_evidence(
                actor,
                session_id=session_id,
                question=value["question"],
                report_id=value["report_id"],
                metric_id=value["metric_id"],
                evidence_group_id=value["evidence_group_id"],
            ),
        }

    @app.post("/wenquan/api/v1/sessions/{session_id}/turns", status_code=202)
    def turn(
        session_id: str,
        payload: dict[str, Any],
        actor: IdentityContext = Depends(identity),
    ) -> dict[str, Any]:
        if research_backend is not None:
            return {"contract": CORE_CONTRACT, **research_backend.submit(actor, session_id=session_id, **payload)}
        value = require_exact_fields(
            payload,
            allowed={"question", "report_id", "request_id", "selection_mode"},
            required={"question", "request_id"},
            label="turn request",
        )
        return {
            "contract": CORE_CONTRACT,
            **readonly.submit(
                actor, session_id=session_id, question=value["question"],
                report_id=value.get("report_id"), request_id=value["request_id"], selection_mode=value.get("selection_mode")
            ),
        }

    @app.post("/wenquan/api/v1/sessions/{session_id}/runs/{run_id}/cancel")
    def cancel_turn(session_id: str, run_id: str, payload: dict[str, Any],
                    actor: IdentityContext = Depends(identity)) -> dict[str, Any]:
        require_exact_fields(payload, allowed=set(), label="cancel request")
        if research_backend is not None and service.store.get_run(actor, run_id)["intent"] == "harness_research":
            return {"contract": CORE_CONTRACT, **research_backend.cancel(actor, session_id, run_id)}
        return {"contract": CORE_CONTRACT, **readonly.cancel(actor, session_id, run_id)}

    @app.get("/wenquan/api/v1/sessions/{session_id}/sources/{citation_id}")
    def read_answer_source(session_id: str, citation_id: str,
                           actor: IdentityContext = Depends(identity), offset: int | None = None) -> dict[str, Any]:
        if research_backend is not None and citation_id.startswith("src_"):
            return research_backend.source(actor, session_id, citation_id, offset)
        return readonly.source(actor, session_id, citation_id, offset)

    @app.post("/wenquan/api/v1/sessions/{session_id}/runs/{run_id}/resume", status_code=202)
    def resume_turn(session_id: str, run_id: str, payload: dict[str, Any],
                    actor: IdentityContext = Depends(identity)) -> dict[str, Any]:
        if research_backend is not None and service.store.get_run(actor, run_id)["intent"] == "harness_research":
            value = require_exact_fields(payload, allowed={"request_id", "action"}, required={"request_id"}, label="resume request")
            return {"contract": CORE_CONTRACT, **research_backend.resume(actor, session_id, run_id, value["request_id"], action=value.get("action"))}
        value = require_exact_fields(payload, allowed={"request_id"}, required={"request_id"}, label="resume request")
        return {"contract": CORE_CONTRACT, **readonly.resume(actor, session_id, run_id, value["request_id"])}

    @app.post("/wenquan/api/v1/sessions/{session_id}/action-confirmations/preview", status_code=201)
    def preview_business_action(
        session_id: str,
        payload: dict[str, Any],
        actor: IdentityContext = Depends(identity),
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload,
            allowed={
                "action",
                "product_ids",
                "channels",
                "time_range",
                "estimated_review_count",
                "estimated_duration_minutes",
                "reused_object_ids",
                "created_object_types",
                "scope_expanded",
                "external_paid",
            },
            required={
                "action",
                "product_ids",
                "channels",
                "time_range",
                "estimated_review_count",
                "estimated_duration_minutes",
                "reused_object_ids",
                "created_object_types",
                "scope_expanded",
                "external_paid",
            },
            label="business action preview",
        )
        if not isinstance(value["scope_expanded"], bool) or not isinstance(value["external_paid"], bool):
            raise AgentError("invalid_request", "scope_expanded/external_paid 必须是布尔值")
        return {
            "contract": CORE_CONTRACT,
            "confirmation": service.preview_business_action(
                actor,
                session_id=session_id,
                action=value["action"],
                product_ids=value["product_ids"],
                channels=value["channels"],
                time_range=value["time_range"],
                estimated_review_count=value["estimated_review_count"],
                estimated_duration_minutes=value["estimated_duration_minutes"],
                reused_object_ids=value["reused_object_ids"],
                created_object_types=value["created_object_types"],
                scope_expanded=value["scope_expanded"],
                external_paid=value["external_paid"],
            ),
        }

    @app.get("/wenquan/api/v1/memory")
    def memory(actor: IdentityContext = Depends(identity)) -> dict[str, Any]:
        status = service.store.memory_status(actor)
        return {
            "contract": CORE_CONTRACT,
            "status": status,
            "preferences": service.store.list_preferences(actor),
        }

    @app.patch("/wenquan/api/v1/memory")
    def set_memory(
        payload: dict[str, Any], actor: IdentityContext = Depends(identity)
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload, allowed={"enabled"}, required={"enabled"}, label="memory setting"
        )
        if not isinstance(value["enabled"], bool):
            raise AgentError("invalid_request", "enabled 必须是布尔值")
        return {
            "contract": CORE_CONTRACT,
            "status": service.store.set_memory_enabled(actor, value["enabled"]),
        }

    @app.post("/wenquan/api/v1/memory/preferences/preview", status_code=201)
    def preview_preference(
        payload: dict[str, Any], actor: IdentityContext = Depends(identity)
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload,
            allowed={"preference_key", "value"},
            required={"preference_key", "value"},
            label="preference preview",
        )
        return {
            "contract": CORE_CONTRACT,
            "confirmation": service.preview_preference(
                actor, preference_key=value["preference_key"], value=value["value"]
            ),
        }

    @app.post("/wenquan/api/v1/memory/preferences/delete-preview", status_code=201)
    def preview_preference_delete(
        payload: dict[str, Any], actor: IdentityContext = Depends(identity)
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload,
            allowed={"preference_key"},
            required={"preference_key"},
            label="preference delete preview",
        )
        return {
            "contract": CORE_CONTRACT,
            "confirmation": service.preview_preference_delete(
                actor, preference_key=value["preference_key"]
            ),
        }

    @app.post("/wenquan/api/v1/confirmations/{confirmation_id}/confirm")
    def confirm(
        confirmation_id: str,
        payload: dict[str, Any],
        actor: IdentityContext = Depends(identity),
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload,
            allowed={"payload_hash"},
            required={"payload_hash"},
            label="confirmation",
        )
        return {
            "contract": CORE_CONTRACT,
            "confirmation": service.store.confirm(
                actor, confirmation_id, value["payload_hash"]
            ),
        }

    @app.post("/wenquan/api/v1/memory/preferences", status_code=201)
    def write_preference(
        payload: dict[str, Any], actor: IdentityContext = Depends(identity)
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload,
            allowed={"preference_key", "value", "confirmation_id"},
            required={"preference_key", "value", "confirmation_id"},
            label="preference write",
        )
        return {
            "contract": CORE_CONTRACT,
            "preference": service.store.write_preference(
                actor,
                preference_key=value["preference_key"],
                value=value["value"],
                confirmation_id=value["confirmation_id"],
            ),
        }

    @app.post("/wenquan/api/v1/memory/preferences/delete")
    def delete_preference(
        payload: dict[str, Any], actor: IdentityContext = Depends(identity)
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload,
            allowed={"preference_key", "confirmation_id"},
            required={"preference_key", "confirmation_id"},
            label="preference delete",
        )
        return {
            "contract": CORE_CONTRACT,
            "preference": service.store.delete_preference(
                actor,
                preference_key=value["preference_key"],
                confirmation_id=value["confirmation_id"],
            ),
        }

    @app.post("/wenquan/api/v1/sessions/{session_id}/feedback", status_code=201)
    def feedback(
        session_id: str,
        payload: dict[str, Any],
        actor: IdentityContext = Depends(identity),
    ) -> dict[str, Any]:
        value = require_exact_fields(
            payload,
            allowed={"message_id", "run_id", "rating", "reason_codes", "comment"},
            required={"message_id", "rating", "reason_codes"},
            label="feedback",
        )
        reasons = value["reason_codes"]
        if not isinstance(reasons, list) or any(reason not in FEEDBACK_REASONS for reason in reasons):
            raise AgentError("invalid_request", "feedback reason code 无效")
        bundle = service.store.session_bundle(actor, session_id)
        message = next(
            (item for item in bundle["messages"] if item["message_id"] == value["message_id"]),
            None,
        )
        if message is None:
            raise AgentError("message_not_found", "消息不存在", http_status=404)
        if message["role"] != "assistant" or (value.get("run_id") is not None and value["run_id"] != message.get("run_id")):
            raise AgentError("invalid_request", "反馈必须对应这条回答及其研究任务")
        context = {
            "message_sha256": sha256_value(message["content"]),
            "answer_status": message.get("answer_status"),
            "tool_catalog_version": "wenquan-r1-core/1.0.0",
        }
        saved_feedback = service.store.add_feedback(
                actor,
                session_id=session_id,
                message_id=value["message_id"],
                run_id=message.get("run_id"),
                rating=value["rating"],
                reason_codes=reasons,
                comment=value.get("comment"),
                context=context,
            )
        if research_backend is not None and message.get("run_id"):
            run = service.store.get_run(actor, message["run_id"])
            if run["intent"] == "harness_research":
                state = research_backend.state.get(actor, run["run_id"])
                if state.get("trace_id"):
                    research_backend.traces.record_feedback(actor, state["trace_id"], feedback_id=saved_feedback["feedback_id"], rating={"up": "helpful", "down": "unhelpful"}.get(value["rating"], value["rating"]), labels=reasons, comment=value.get("comment") or "")
        return {"contract": CORE_CONTRACT, "feedback": saved_feedback}

    @app.get("/wenquan/api/v1/audit-events")
    def audit_events(
        audit_id: str | None = None,
        actor: IdentityContext = Depends(identity),
    ) -> dict[str, Any]:
        return {
            "contract": CORE_CONTRACT,
            "items": service.store.list_audit_events(actor, audit_id=audit_id),
        }

    if research_backend is not None:
        from .research_routes import install_research_routes
        install_research_routes(app, research_backend, identity)
    return app


def create_runtime_app() -> Any:
    settings = WenquanSettings.from_environment()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = WenquanStore(settings.data_dir / "wenquan.sqlite3")
    store.initialize()
    if settings.identity_provider_mode == "account_admin_v1":
        identity_provider = AccountAdminIdentityProvider(
            settings.account_admin_identity_signing_key,
            key_id=settings.account_admin_identity_key_id,
        )
    else:
        identity_provider = TrustedHeaderIdentityProvider(settings.identity_hmac_secret)
    identity_status = (
        settings.identity_dependency_status
        if identity_provider.configured
        else "unavailable"
    )
    if bool(settings.zhuojian_base_url) != bool(settings.zhuojian_gateway_token):
        raise AgentError(
            "dependency_config_invalid",
            "分析报告 base URL 与 Gateway token 必须同时配置",
            http_status=503,
        )
    executor = None
    zhuojian_status = "unavailable"
    if settings.zhuojian_base_url and settings.zhuojian_gateway_token:
        zhuojian_status = "ready"
    registry = ToolRegistry(zhuojian_status=zhuojian_status, enable_research=True)
    if zhuojian_status == "ready":
        transport = HttpZhuojianTransport(
            base_url=settings.zhuojian_base_url or "",
            gateway_token=settings.zhuojian_gateway_token or "",
        )
        executor = ToolExecutor(
            registry=registry,
            transport=transport,
            store=store,
            envelope_schema_path=str(
                Path(__file__).resolve().parents[1]
                / "reporting"
                / "schemas"
                / "zhuojian-tool-envelope-v1.schema.json"
            ),
        )
    planner = None
    if settings.llm is not None:
        planner = SemanticPlanner(OpenAICompatibleLLMRuntime(settings.llm, timeout_seconds=120.0, max_tokens=16384))
    service = WenquanService(
        store=store,
        registry=registry,
        executor=executor,
        planner=planner,
        identity_status=identity_status,
        llm_status="ready" if settings.llm is not None else "unavailable",
        chengmu_status="ready",
    )
    import os
    backend = None
    if os.environ.get("VOXQUARRY_WENQUAN_HARNESS_ENABLED", "false") == "true":
        from .harness_manager import HarnessManager
        from .harness_research import HarnessResearch
        from .research_clients import ResearchAuthorityClient
        manager = HarnessManager(os.environ.get("VOXQUARRY_WENQUAN_MANAGER_URL", ""), os.environ.get("VOXQUARRY_WENQUAN_MANAGER_TOKEN", ""))
        authority = ResearchAuthorityClient(os.environ.get("VOXQUARRY_RESEARCH_AUTHORITY_URL", ""), os.environ.get("VOXQUARRY_RESEARCH_AUTHORITY_TOKEN", ""),
            expected_mode="production_account_admin" if settings.identity_provider_mode == "account_admin_v2" else "local_dev_account_admin")
        backend = HarnessResearch(service, manager, authority=authority, recover=True)
    return create_app(service=service, identity_provider=identity_provider, research_backend=backend)
