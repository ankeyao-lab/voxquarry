"""Authenticated public research controls and a separate capability-only broker."""
from __future__ import annotations

import json
from urllib.parse import quote, unquote

from fastapi import Depends, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .contracts import AgentError, IdentityContext, require_exact_fields
from .eval_export import EvalExporter
from .research_limits import ResearchLimits


class ResearchBodyLimit:
    """Bound untrusted sandbox requests before JSON decoding allocates them."""
    def __init__(self, app, maximum=None):
        self.app = app
        self.maximum = ResearchLimits.from_environment().model_request_bytes if maximum is None else maximum

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/wenquan/internal/broker/"):
            return await self.app(scope, receive, send)
        maximum, count, body_buffer = self.maximum, 0, bytearray()

        class TooLarge(Exception):
            pass

        try:
            lengths = [value for name, value in scope.get("headers", []) if name.lower() == b"content-length"]
            if len(lengths) > 1:
                raise TooLarge
            if lengths:
                if not lengths[0].isdigit():
                    raise TooLarge
                # Avoid int() on an attacker-controlled thousands-digit field.
                # It can raise outside the response guard on modern Python.
                declared = lengths[0].lstrip(b"0") or b"0"
                bound = str(maximum).encode("ascii")
                if len(declared) > len(bound) or (len(declared) == len(bound) and declared > bound):
                    raise TooLarge
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    return
                content = message.get("body", b"")
                count += len(content)
                if count > maximum:
                    raise TooLarge
                # Keep memory proportional to bytes, not the number of tiny or
                # empty ASGI chunks provided by the peer.
                body_buffer.extend(content)
                if not message.get("more_body"):
                    break
        except TooLarge:
            body = json.dumps({"error": {"code": "research_request_too_large", "message": "研究请求超过处理容量"}}).encode()
            await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        pending = True
        async def bounded_receive():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body_buffer), "more_body": False}
            return await receive()
        return await self.app(scope, bounded_receive, send)


def install_research_routes(app, research, identity):
    prefix = "/wenquan/api/v1"
    app.add_middleware(ResearchBodyLimit)

    def attachment_public(item):
        sid, aid = quote(item["session_id"], safe=""), item["attachment_id"]
        return {**item, "size_bytes": item["byte_size"], "preview_url": f"{prefix}/sessions/{sid}/attachments/{aid}/content",
                "image_url": f"{prefix}/sessions/{sid}/attachments/{aid}/image" if item["media_type"] in {"image/png", "image/jpeg", "image/webp"} and item["status"] == "ready" else None,
                "download_url": f"{prefix}/sessions/{sid}/attachments/{aid}/download"}

    @app.get(prefix + "/models")
    def models(actor: IdentityContext = Depends(identity)):
        return research.models.catalog()

    @app.patch(prefix + "/sessions/{session_id}/settings")
    def settings(session_id: str, payload: dict, actor: IdentityContext = Depends(identity)):
        return {"settings": research.state.update_settings(actor, session_id, payload)}

    @app.get(prefix + "/datasets")
    def datasets(query: str = "", actor: IdentityContext = Depends(identity)):
        if len(query) > 1000:
            raise AgentError("invalid_request", "查询内容过长")
        if not research.data:
            raise AgentError("data_unavailable", "原始数据服务尚未连接", http_status=503)
        result = research.data.call(actor, "catalog", {})
        items = []
        for row in result.get("items", []):
            title = " · ".join(str(row[key]) for key in ("brand", "model", "channel") if row.get(key)) or row["job_id"]
            if query.casefold() not in (title + " " + str(row.get("category_id", ""))).casefold():
                continue
            dataset_id = "job:" + row["job_id"]
            items.append({**row, "dataset_id": dataset_id, "title": title, "scope_label": row.get("market_region"),
                "record_count": row.get("eligible_reviews"), "version": row.get("validation_sha256"),
                "preview_url": f"{prefix}/datasets/{quote(dataset_id, safe='')}/preview"})
        return {"items": items, "total": len(items)}

    @app.get(prefix + "/datasets/{dataset_id}/preview")
    def dataset_preview(dataset_id: str, actor: IdentityContext = Depends(identity)):
        if not research.data:
            raise AgentError("data_unavailable", "原始数据服务尚未连接", http_status=503)
        if dataset_id.startswith("job:"):
            manifest = research.data.call(actor, "open", {"scope": {"job_ids": [dataset_id[4:]]}})
            dataset_id = manifest["dataset_id"]
        else:
            manifest = research.data.call(actor, "manifest", {"dataset_id": dataset_id})
        sample = research.data.call(actor, "query", {"dataset_id": dataset_id, "limit": 5})
        return {"source_type": "dataset", "title": "原始评论预览", "dataset_id": dataset_id, "manifest": manifest, "sample": sample}

    @app.post(prefix + "/sessions/{session_id}/attachments", status_code=201)
    async def upload_attachment(session_id: str, request: Request, actor: IdentityContext = Depends(identity)):
        research.store.get_session(actor, session_id)
        maximum = research.attachments.max_file_bytes
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise AgentError("invalid_request", "附件大小无效") from None
        if length < 0 or length > maximum:
            raise AgentError("attachment_too_large", "单个附件不能超过20MB", http_status=413)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > maximum:
                raise AgentError("attachment_too_large", "单个附件不能超过20MB", http_status=413)
            chunks.append(chunk)
        try:
            filename = unquote(request.headers.get("x-filename", ""), errors="strict")
        except UnicodeError:
            raise AgentError("invalid_request", "附件文件名编码无效") from None
        item = await run_in_threadpool(research.attachments.create, actor, session_id, filename,
            request.headers.get("content-type", "application/octet-stream"), b"".join(chunks))
        if item["status"] == "pending":
            research.queue_attachment_parse(actor, session_id, item["attachment_id"])
        return {"attachment": attachment_public(item)}

    @app.get(prefix + "/sessions/{session_id}/attachments")
    def list_attachments(session_id: str, actor: IdentityContext = Depends(identity)):
        return {"items": [attachment_public(item) for item in research.attachments.list(actor, session_id)["items"]]}

    @app.get(prefix + "/sessions/{session_id}/attachments/{attachment_id}/content")
    def attachment_content(session_id: str, attachment_id: str, offset: int = 0, actor: IdentityContext = Depends(identity)):
        result = research.attachments.read_text(actor, session_id, attachment_id, offset=offset)
        metadata = attachment_public(result["attachment"])
        return {**result, "source_type": "upload", "title": metadata["filename"], "content": result["text"],
            "locator": result.get("locators"), "download_url": metadata["download_url"],
            "next_url": metadata["preview_url"] + "?offset=" + str(result["next_offset"]) if result["next_offset"] is not None else None}

    @app.get(prefix + "/sessions/{session_id}/attachments/{attachment_id}/download")
    def attachment_download(session_id: str, attachment_id: str, actor: IdentityContext = Depends(identity)):
        item = research.attachments.get(actor, session_id, attachment_id)
        path = research.attachments.download_path(actor, session_id, attachment_id)
        return FileResponse(path, media_type="application/octet-stream", filename=item["filename"],
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Content-Security-Policy": "sandbox"})

    @app.get(prefix + "/sessions/{session_id}/attachments/{attachment_id}/image")
    def attachment_image(session_id: str, attachment_id: str, actor: IdentityContext = Depends(identity)):
        item = research.attachments.get(actor, session_id, attachment_id)
        if item["media_type"] not in {"image/png", "image/jpeg", "image/webp"} or item["status"] != "ready":
            raise AgentError("attachment_image_unavailable", "图片尚未就绪或类型不支持预览", http_status=404)
        return FileResponse(research.attachments.download_path(actor, session_id, attachment_id), media_type=item["media_type"],
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Content-Security-Policy": "sandbox"})

    @app.post(prefix + "/sessions/{session_id}/attachments/{attachment_id}/retry")
    def attachment_retry(session_id: str, attachment_id: str, payload: dict, actor: IdentityContext = Depends(identity)):
        require_exact_fields(payload, allowed=set(), label="附件重试")
        item = research.attachments.request_retry(actor, session_id, attachment_id)
        if item.pop("retry_requested", False):
            research.queue_attachment_parse(actor, session_id, attachment_id, retry=True)
        return {"attachment": attachment_public(item)}

    @app.get(prefix + "/sessions/{session_id}/sources/{source_id}/content")
    def source_content(session_id: str, source_id: str, offset: int | None = None, actor: IdentityContext = Depends(identity)):
        return research.source(actor, session_id, source_id, offset)

    @app.get(prefix + "/sessions/{session_id}/runs/{run_id}/trace")
    def trace(session_id: str, run_id: str, after_seq: int = 0, actor: IdentityContext = Depends(identity)):
        run = research._run(actor, session_id, run_id)
        if not run.get("trace_id"):
            return {"trace": None, "items": []}
        from .trajectory import trace_overview, trace_timing_page
        return {"trace": research.traces.get_trace(actor, run["trace_id"]),
                "overview": trace_overview(research.traces, actor, run["trace_id"]),
                **trace_timing_page(research.traces, actor, run["trace_id"], after_seq=after_seq)}

    @app.get("/wenquan/trajectory")
    @app.get("/wenquan/api/v1/trajectory")
    def trajectory_page(actor: IdentityContext = Depends(identity)):
        from pathlib import Path
        return FileResponse(Path(__file__).resolve().parents[1] / "web" / "dist" / "client" / "index.html", media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get(prefix + "/sessions/{session_id}/artifacts/{artifact_id}/download")
    def artifact_download(session_id: str, artifact_id: str, actor: IdentityContext = Depends(identity)):
        item, path = research.artifact(actor, session_id, artifact_id)
        return FileResponse(path, media_type="application/octet-stream", filename=item["metadata"]["filename"],
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Content-Security-Policy": "sandbox"})

    @app.get(prefix + "/sessions/{session_id}/runs/{run_id}/trace/payloads/{payload_id}")
    def trace_payload(session_id: str, run_id: str, payload_id: str, include_content: bool = False, actor: IdentityContext = Depends(identity)):
        run = research._run(actor, session_id, run_id)
        if not run.get("trace_id"):
            raise AgentError("trace_not_found", "研究日志尚未建立", http_status=404)
        return research.traces.read_payload(actor, run["trace_id"], payload_id, include_content=include_content)

    @app.post(prefix + "/sessions/{session_id}/evals/export")
    def eval_export(session_id: str, payload: dict, actor: IdentityContext = Depends(identity)):
        value = require_exact_fields(payload, allowed={"run_ids", "include_content"}, required={"run_ids"}, label="Evals导出")
        ids = value["run_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(not isinstance(v, str) for v in ids) or not isinstance(value.get("include_content", False), bool):
            raise AgentError("invalid_request", "请选择1–20条本会话的研究记录")
        traces = []
        for run_id in ids:
            run = research._run(actor, session_id, run_id)
            if not run.get("trace_id"):
                raise AgentError("trace_not_found", "部分研究日志尚未建立", http_status=409)
            traces.append(run["trace_id"])
        return EvalExporter(research.traces).export(actor, trace_ids=traces, include_content=value.get("include_content", False))

    @app.post("/wenquan/internal/broker/tools/{name}")
    def broker_tool(name: str, payload: dict, request: Request):
        cap = research.broker.resolve(request.headers.get("authorization"))
        value = require_exact_fields(payload, allowed={"arguments", "tool_call_id"}, required={"arguments"}, label="研究工具")
        return research.tool(cap, name, value["arguments"], value.get("tool_call_id", ""))

    @app.post("/wenquan/internal/broker/native")
    def native_io(payload: dict, request: Request):
        cap = research.broker.resolve(request.headers.get("authorization"))
        research.broker.check(cap)
        try:
            return research.manager.native_io(cap.attempt_id, payload)
        except AgentError as error:
            research.broker.event(cap, "error.native_transport", data={
                "code": error.code, "transport_kind": getattr(error, "transport_kind", None)})
            raise

    @app.post("/wenquan/internal/broker/trajectory")
    def native_trajectory(payload: dict, request: Request):
        from .trajectory import native_event
        cap = research.broker.resolve(request.headers.get("authorization"))
        value = require_exact_fields(payload, allowed={"session_id", "event"}, required={"session_id", "event"}, label="原生轨迹")
        if value["session_id"] not in {cap.attempt_id, cap.session_key}:
            raise AgentError("trajectory_session_mismatch", "轨迹不属于本次执行", http_status=403)
        data, content, event_id = native_event(value["event"])
        event = research.broker.event(cap, "research.native", data=data, payload=content,
                                      event_id=cap.attempt_id + ":" + event_id)
        research.native_progress(cap, value["event"])
        return {"saved": True, "sequence": event["seq"]}

    @app.post("/wenquan/internal/broker/model/v1/chat/completions")
    def broker_model(payload: dict, request: Request):
        cap = research.broker.resolve(request.headers.get("authorization"))
        from .harness_tools import sdk_research_tools
        stream = research.broker.model_stream(cap, payload, sdk_research_tools(cap.runtime_contract),
            relay_prepare_ms=request.headers.get('x-voxquarry-relay-prepare-ms'))
        # Advance through authorization/provider headers before committing HTTP
        # 200; errors then keep their useful HTTP status for the official SDK.
        first = next(stream)
        def output():
            yield first
            yield from stream
        return StreamingResponse(output(), media_type="text/event-stream" if payload.get("stream") else "application/json",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
