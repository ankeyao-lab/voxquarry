"""Question-led research coordinated through an isolated official Harness."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import mimetypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from .research_delivery import delivery_issues, delivery_notice
from pathlib import Path
from urllib.parse import quote

from jsonschema import Draft202012Validator

from .agent_trace import AgentTraceStore
from .contracts import AgentError, IdentityContext, new_id, sha256_value, utc_now
from .evidence_semantics import model_evidence_view
from .harness_broker import HarnessBroker
from .harness_manager import TRANSPORT_KINDS
from .harness_tools import SYSTEM_PROMPT, CONCURRENT_READ_TOOLS, research_tools, sdk_research_tools
from .model_registry import ModelRegistry
from .readonly import report_href
from .research_attachments import AttachmentStore, IsolatedAttachmentParser
from .research_clients import ResearchCollectionClient, ResearchDataClient
from .research_egress import ResearchEgressPolicy
from .research_report_export import portable_report, report_bundle
from . import research_data_goal
from .harness_findings import AUDIT_PROMPT, evidence_packet, parse_audit, documents, validate_frequency_claim
from . import review_topic_counts
from .research_state import ANSWER_REPAIR_CODES, ResearchState
from .answer_checks import percentage_relation_conflicts
from .review_citation_resolution import entity_ids, markers, replace_markers, resolve_entities, review_source_error, source_citation, source_marked_content
from .research_web import ResearchWeb, public_url


LABELS = {
    "list_reports": "查找相关报告", "get_report_section": "阅读报告内容",
    "get_metric": "读取已有指标", "list_evidence_groups": "定位报告证据",
    "query_evidence": "阅读报告证据", "get_data_quality": "核对报告数据范围",
    "read_research_reference": "阅读品牌研究报告",
    "query_report_reviews": "核对原始评论", "aggregate_report_reviews": "统计报告中的评论",
    "list_datasets": "查找产品原始数据", "open_dataset": "准备完整评论数据",
    "query_dataset": "阅读原始评论", "aggregate_dataset": "计算评论分布", "export_dataset": "准备分析文件",
    "list_attachments": "查看用户附件", "read_attachment": "阅读附件",
    "search_web": "搜索公开资料", "read_web_page": "阅读公开网页",
    "find_review_links": "按品牌型号查找网评链接",
    "collection_capabilities": "检查可采集渠道", "resolve_product": "核对产品链接",
    "collect_product": "补充采集评论", "wait_for_collection": "等待新评论采集",
    "report_candidates": "查找可生成的报告", "create_report": "生成报告",
    "wait_for_report_data": "等待新评论进入洞察数据版本",
    "run_python": "运行分析代码", "run_shell": "处理分析文件", "read_file": "读取分析文件", "write_file": "保存分析脚本",
    "submit_answer": "整理研究结论", "publish_artifact": "保存分析结果",
    "read_saved_source": "回看已有原文",
    "get_product_review_definition": "读取品类报告章节与分析方法",
    "analyze_product_reviews": "分析单产品评论",
    "publish_report": "生成可下载报告",
    "prepare_product_research": "准备产品评论与分析资料", "review_findings": "核对结论与原文",
    "rank_review_topics": "逐条核对评论归类并统计评论数",
    "bash": "运行分析代码", "read": "读取分析文件", "write": "保存分析文件", "edit": "修改分析文件",
    "glob": "查找分析文件", "grep": "检索文件内容", "read_image": "查看图片",
    "job_list": "查看后台任务", "job_output": "读取后台任务结果", "job_kill": "停止后台任务",
    "todo_write": "更新研究步骤", "skill": "读取分析方法",
}
TERMINAL = {"completed", "failed", "cancelled", "blocked", "partial"}
# These describe storage/ingestion, not the reviewer's experience. They remain
# available in the immutable original result and the saved citation source.
# Keep unknown fields by default so new channel-specific context is not lost.
REVIEW_STORAGE_FIELDS = frozenset({
    "attempt_no", "collection_method", "fact_schema_version", "job_finished_at",
    "normalization_version", "parser_version", "row_content_sha256",
    "source_artifact_path", "source_artifact_sha256", "source_row_number", "time_parse_rule",
})
REVIEW_ORIGINAL_ALIASES = {"title": "review_title", "submission_time": "submission_time_raw", "source_site": "source_site_raw"}


def tool_progress_summary(name, arguments, result=None):
    """Public action/result descriptions; never scripts, payloads or reasoning."""
    if name in {"run_python", "run_shell"}:
        code = str(arguments.get("code") or arguments.get("command") or "").lower()
        if any(word in code for word in ("assignments", "topic_id", "classif")):
            action = "检查并整理评论归类明细"
        elif any(word in code for word in ("value_counts", "groupby", "counter(", "aggregate")):
            action = "统计评论数量和分布"
        elif any(word in code for word in ("review_text", "review_title", "reviews")):
            action = "读取并分析原始评论"
        else:
            action = "运行 Python 分析" if name == "run_python" else "执行文件处理命令"
        if isinstance(result, dict) and type(result.get("exit_code")) is int:
            action += "；执行成功" if result["exit_code"] == 0 else "；执行出错，正在调整"
        return action
    if name in {"read_file", "write_file"}:
        # Only a bounded ordinary basename, no host paths or file contents.
        filename = str(arguments.get("path", "")).rsplit("/", 1)[-1]
        if re.fullmatch(r"[\w. -]{1,90}", filename):
            return ("读取 " if name == "read_file" else "保存 ") + filename
    if name == "rank_review_topics":
        if isinstance(result, dict) and type(result.get("checked_reviews")) is int and type(result.get("total_reviews")) is int:
            summary = f"已核对 {result['checked_reviews']}/{result['total_reviews']} 条评论"
            if result.get("needs_revision"):
                summary += f"；本批 {len(result['needs_revision'])} 条需修正"
            return summary
        if isinstance(result, dict) and result.get("status") == "ready":
            return "完整归类已核对，优缺点评论数已生成"
        return "对照完整原文核对每条归类，按评论数排序"
    if name == "review_findings":
        return "逐项核对结论是否得到原文支持"
    if name in {"query_dataset", "query_report_reviews", "aggregate_dataset", "export_dataset"}:
        count = result.get("total") if isinstance(result, dict) else None
        if type(count) is int:
            return f"找到 {count} 条评论"
    if name == "submit_answer":
        return "整理回答、引用和报告文件"
    return ""


class HarnessResearch:
    def __init__(self, service, manager, *, models=None, traces=None, state=None, attachments=None,
                 data=None, collection=None, web=None, authority=None, recover=False):
        self.service, self.store, self.manager = service, service.store, manager
        self.models = models or ModelRegistry()
        root = self.store.database_path.parent
        self.state = state or ResearchState(self.store, self.models)
        self.state.initialize()
        self.egress = ResearchEgressPolicy(self.store)
        self.egress.initialize()
        self.traces = traces or AgentTraceStore(root / "agent-traces" / "traces.sqlite3",
            content_enabled=os.environ.get("VOXQUARRY_WENQUAN_TRACE_CONTENT", "false") == "true",
            max_payload_bytes=8 * 1024 * 1024)
        self.traces.initialize()
        self.attachments = attachments or AttachmentStore(self.store, root / "attachments",
            parser=IsolatedAttachmentParser(self._parse_attachment, "wenquan-analysis-v1"))
        self.attachments.initialize()
        self.data = data or (ResearchDataClient(service.executor.transport) if service.executor else None)
        self.collection = collection or ResearchCollectionClient(os.environ.get("VOXQUARRY_RESEARCH_COLLECTOR_URL", ""), os.environ.get("VOXQUARRY_RESEARCH_BRIDGE_TOKEN", ""))
        self.web = web or ResearchWeb(os.environ.get("VOXQUARRY_LLM_API_KEY", ""))
        self.authority = authority
        self._authority_cache, self._authority_lock = {}, threading.Lock()
        self.tools = research_tools()
        self._schemas = {tool["name"]: Draft202012Validator(tool["parameters"]) for tool in self.tools}
        self.broker = HarnessBroker(self.state, self.models, self.traces,
            authorize=(lambda actor, run_id: self._authorize_run(actor, run_id, force=True)) if authority is not None else None)
        # Default remains one research. Raising capacity also requires matching
        # manager admission and its dedicated-VM resource preflight.
        self._pool = ThreadPoolExecutor(max_workers=self.state.capacity.researches, thread_name_prefix="wenquan-harness")
        self._parse_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wenquan-attachment")
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._scheduled = set()
        self._schedule_lock = threading.Lock()
        self._stopped = threading.Event()
        if recover:
            self.state.recover_interrupted()
            self.attachments.recover_interrupted()
        self._monitor = threading.Thread(target=self._monitor_waiting, daemon=True, name="wenquan-collection-wait")
        self._monitor.start()

    def _current_authority(self, actor, capability="chat.session.own", *, force=False):
        if self.authority is None:
            return None
        key = (actor.owner_user_id, capability)
        with self._authority_lock:
            cached = self._authority_cache.get(key)
            if not force and cached and cached[0] > time.monotonic():
                result = cached[1]
            else:
                result = self.authority.authorize(actor.owner_user_id, capability)
                self._authority_cache[key] = (time.monotonic() + min(result["ttl_seconds"], 30), result)
        if result["owner_scope"] != actor.scope_id or set(result["roles"]) != set(actor.roles):
            raise AgentError("research_authority_changed", "账号权限已变化，请重新登录后发起研究。", http_status=403)
        return result

    def _authorize_run(self, actor, run_id, capability="chat.session.own", *, force=False):
        fresh = self._current_authority(actor, capability, force=force)
        if fresh is not None:
            pinned = self.state.get(actor, run_id)["checkpoint"].get("authorization_version")
            if not pinned or pinned != fresh["authorization_version"]:
                raise AgentError("research_authority_changed", "账号权限已变化，本次研究已停止。请重新登录后发起研究。", http_status=403)
        return fresh

    def _block_waiting_authority(self, actor, run, error):
        """Settle a revoked wait without admission, a model call, or new access.

        The actor may be reconstructed from the persisted owner solely for this
        internal state transition. It must never be used for data/model tools.
        """
        queued = run["status"] == "running" and run.get("execution_status") == "queued"
        if error.http_status != 403 or not (queued or run["status"] in {"waiting_collection", "waiting_data"}):
            return False
        message = "账号权限已变化，本次研究已停止。请重新登录后新建问题。"
        try:
            stopped = self.state.finish(actor, run["run_id"], message, status="blocked", attempt_id=run["attempt_id"], allow_waiting=True,
                metadata={"error": {"code": "research_authority_changed", "message": message, "retryable": False},
                          "can_resume": False, "resume_disabled": True, "citations": []})
        except AgentError:
            # A concurrent user cancel/resume must win; never overwrite a new
            # attempt using the old scheduler snapshot.
            return False
        self.broker.revoke(run["run_id"], run["attempt_id"])
        with self._pending_lock:
            self._pending.pop(run["run_id"], None)
        if stopped.get("trace_id"):
            try:
                self.traces.finish_trace(actor, stopped["trace_id"], status="blocked", error={"code": "research_authority_changed"}, attempt_id=run["attempt_id"])
            except AgentError:
                # An expired diagnostic artifact must not leave a revoked run
                # displayed as waiting after its state was already settled.
                pass
        return True

    def _restore_waiting(self):
        if self.authority is None:
            return
        for saved in self.state.pending_runs(limit=1000):
            queued = saved.get("execution_status") == "queued" and saved["status"] == "running"
            if not queued and saved["status"] not in {"waiting_collection", "waiting_data"}:
                continue
            if queued:
                with self._schedule_lock:
                    if saved["attempt_id"] in self._scheduled:
                        continue
            with self._pending_lock:
                if saved["run_id"] in self._pending:
                    continue
            # This minimal identity is only for owner-checked terminal writes
            # when the account service rejects the principal. Saved roles or
            # cookies cannot grant it permission to execute anything.
            state_actor = IdentityContext(actor_id=saved["owner_user_id"], owner_user_id=saved["owner_user_id"],
                scope_id=saved["scope_id"], authority_id="wenquan-background-state", roles=(),
                allowed_report_ids=frozenset(), allow_archived_reports=False, mode="authenticated")
            try:
                # The saved principal is only a locator. Roles and permission
                # come from a fresh account/AD check, never a saved cookie.
                original = saved["actor_snapshot"]
                fresh = self.authority.authorize(original["owner_user_id"], "chat.session.own")
                actor = IdentityContext(actor_id=original["owner_user_id"], owner_user_id=original["owner_user_id"],
                    scope_id=fresh["owner_scope"], authority_id=original["authority_id"], roles=tuple(fresh["roles"]),
                    allowed_report_ids=None if original.get("allowed_report_ids") is None else frozenset(original["allowed_report_ids"]),
                    allow_archived_reports=False, mode="authenticated")
                if (fresh["authorization_version"] != saved["checkpoint"].get("authorization_version")
                        or fresh["owner_scope"] != saved["scope_id"]):
                    raise AgentError("research_authority_changed", "账号权限已变化", http_status=403)
                self._authorize_run(actor, saved["run_id"], force=True)
                if queued:
                    self._schedule(actor, self.state.get(actor, saved["run_id"]))
                    continue
                with self._pending_lock:
                    self._pending.setdefault(saved["run_id"], actor)
            except AgentError as error:
                self._block_waiting_authority(state_actor, saved, error)
                continue

    def close(self):
        self._stopped.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._parse_pool.shutdown(wait=False, cancel_futures=True)
        self.broker.close()

    def _schedule(self, actor, run):
        """Durable queue in existing attempts; one native executor, no extra service."""
        attempt_id = run["attempt_id"]
        with self._schedule_lock:
            if attempt_id in self._scheduled or self._stopped.is_set():
                return
            queued = self.state.enqueue(actor, run["run_id"], attempt_id=attempt_id)
            self.state.event(actor, run["run_id"], "等待前一项研究完成", "queued", event_id="research", attempt_id=attempt_id)
            self._scheduled.add(attempt_id)
            future = self._pool.submit(self.execute, actor, queued)
        def finished(_future):
            with self._schedule_lock:
                self._scheduled.discard(attempt_id)
        future.add_done_callback(finished)

    def _parse_attachment(self, request):
        # input_path is created by AttachmentStore after owner and file checks,
        # never accepted from a browser or model tool. Only bytes cross the VM.
        content = Path(request["input_path"]).read_bytes()
        if "sha256:" + hashlib.sha256(content).hexdigest() != request["source_sha256"]:
            raise AgentError("attachment_changed", "附件内容发生变化", http_status=409)
        attempt_id = new_id("parse")
        extension = Path(request["filename"]).suffix.lower()
        filename = request["attachment_id"] + extension
        try:
            self.manager.start({"attempt_id": attempt_id, "run_id": request["attachment_id"], "mode": "analysis", "deadline_seconds": 120})
            self.manager.put_input(attempt_id, filename, content)
            result = self.manager.tool(attempt_id, "parse_attachment", {"filename": filename,
                "media_type": request["media_type"], "source_sha256": request["source_sha256"], "timeout_seconds": 30})
            return result
        finally:
            self.manager.stop(attempt_id)

    def queue_attachment_parse(self, actor, session_id, attachment_id, *, retry=False):
        self.attachments.get(actor, session_id, attachment_id)
        self._parse_pool.submit(self.attachments.parse_pending, actor, session_id, attachment_id, retry=retry)

    def validate_request(self, actor, session_id, payload):
        self.store.get_session(actor, session_id)
        allowed = {"question", "request_id", "report_id", "selection_mode", "report_ids", "dataset_ids", "attachment_ids", "product_urls", "model_key", "effort", "web_enabled", "thinking_enabled"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise AgentError("invalid_request", "提问参数无效")
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 20000:
            raise AgentError("invalid_request", "请输入 1–20000 个字符的问题")
        result = dict(payload)
        result["runtime_contract"] = "wenquan-dsh/v2"
        result["question"] = question.strip()
        for key in ("report_ids", "dataset_ids", "attachment_ids", "product_urls"):
            values = result.get(key, [])
            if not isinstance(values, list) or len(values) > 10 or any(not isinstance(v, str) or not v or len(v) > (4000 if key == "product_urls" else 200) for v in values) or len(set(values)) != len(values):
                raise AgentError("invalid_request", "关联资料参数无效")
            result[key] = values
        if result.get("report_id"):
            if not isinstance(result["report_id"], str):
                raise AgentError("invalid_request", "报告标识无效")
            if result["report_id"] not in result["report_ids"]:
                result["report_ids"].append(result["report_id"])
        for report_id in result["report_ids"]:
            if not re.fullmatch(r"rpt_[A-Za-z0-9_-]{8,80}", report_id):
                raise AgentError("invalid_request", "报告标识无效")
            if actor.allowed_report_ids is not None and report_id not in actor.allowed_report_ids:
                raise AgentError("report_not_found", "没有找到可访问的报告", http_status=404)
        for attachment_id in result["attachment_ids"]:
            item = self.attachments.get(actor, session_id, attachment_id)
            if item["status"] != "ready":
                raise AgentError("attachment_not_ready", "附件还没有解析完成，请稍后发送", http_status=409)
        for dataset_id in result["dataset_ids"]:
            if not re.fullmatch(r"(?:ds_[0-9a-f]{64}|job:[A-Za-z0-9_-]{1,128})", dataset_id):
                raise AgentError("invalid_request", "数据集标识无效")
        # Register links from the actual saved user question, not model output.
        # A one-sentence URL request must not require the separate attachment UI.
        question_urls = [value.rstrip(".,;:!?，。；：！？)]}）】") for value in re.findall(r"https?://[^\s<>\"'`]+", result["question"])]
        explicit_urls = [public_url(value) for value in result["product_urls"]]
        for value in question_urls:
            try:
                explicit_urls.append(public_url(value))
            except AgentError:
                # Internal/invalid links in prose are not admitted for collection.
                continue
        result["product_urls"] = list(dict.fromkeys(explicit_urls))
        if len(result["product_urls"]) > 10:
            raise AgentError("invalid_request", "一次问题最多包含10个产品链接。")
        selected = self.state.settings(actor, session_id)
        selected.update({key: result[key] for key in ("model_key", "effort", "web_enabled", "thinking_enabled") if key in result})
        return result, self.models.freeze(selected)

    def submit(self, actor, *, session_id, **payload):
        request, frozen = self.validate_request(actor, session_id, payload)
        authority = self._current_authority(actor, force=True)
        accepted = self.state.accept(actor, session_id, request, frozen)
        if accepted["created"]:
            if authority is not None:
                self.state.checkpoint(actor, accepted["run"]["run_id"], {"authorization_version": authority["authorization_version"]},
                    status="running", attempt_id=accepted["attempt_id"])
                accepted["run"] = self.state.get(actor, accepted["run"]["run_id"])
            self.state.event(actor, accepted["run"]["run_id"], "研究已加入队列", "queued", event_id="research", attempt_id=accepted["attempt_id"])
            self._schedule(actor, accepted["run"])
            accepted["run"] = self.state.get(actor, accepted["run"]["run_id"])
        return {"run": accepted["run"], "duplicate": not accepted["created"]}

    def _run(self, actor, session_id, run_id):
        self.store.get_session(actor, session_id)
        run = self.state.get(actor, run_id)
        if run["session_id"] != session_id:
            raise AgentError("run_not_found", "没有找到这次研究", http_status=404)
        return run

    def resume(self, actor, session_id, run_id, request_id, *, action=None):
        run = self._run(actor, session_id, run_id)
        self._authorize_run(actor, run_id, force=True)
        if action not in {None, "continue_available"}:
            raise AgentError("invalid_request", "不支持此继续操作")
        self.models.runtime(run["settings"])
        if run.get("execution_status") == "recovering" or run["status"] == "waiting_collection" and action is None:
            return {"run": run, "duplicate": True}
        accepted = self.state.resume(actor, run_id, request_id=request_id)
        if accepted["created"]:
            self.broker.revoke(run_id, run["attempt_id"])
            with self._pending_lock:
                self._pending.pop(run_id, None)
            if action == "continue_available" or run["checkpoint"].get("collection_deadline_at", time.time() + 1) <= time.time():
                current = accepted["run"]
                self.state.checkpoint(actor, run_id, {**current["checkpoint"], "waiting_operation_ids": [],
                    "continuation_instruction": "用户要求立即使用现有资料回答。不要再次等待或发起补充采集；共享采集可以自行完成，但本次回答不再等待。"},
                    status="running", attempt_id=current["attempt_id"])
                accepted["run"] = self.state.get(actor, run_id)
            self._schedule(actor, accepted["run"])
            accepted["run"] = self.state.get(actor, run_id)
        return {"run": accepted["run"], "duplicate": not accepted["created"]}

    def cancel(self, actor, session_id, run_id):
        run = self._run(actor, session_id, run_id)
        cancelled = self.state.cancel(actor, run_id)
        self.broker.revoke(run_id)
        with self._pending_lock:
            self._pending.pop(run_id, None)
        try:
            self.manager.stop(run["attempt_id"])
        except AgentError:
            pass
        for operation_id in run["checkpoint"].get("operations", {}):
            try:
                self.collection.call("unsubscribe", {"request_key": operation_id, "research_run_id": run_id})
            except AgentError:
                pass
        if run.get("trace_id") and cancelled["status"] == "cancelled":
            self.traces.cancel_trace(actor, run["trace_id"], attempt_id=run["attempt_id"])
        return {"run": cancelled}

    def _checkpoint(self, cap, **updates):
        with cap.checkpoint_lock:
            current = self.state.get(cap.actor, cap.run_id)["checkpoint"]
            return self.state.checkpoint(cap.actor, cap.run_id, {**current, **updates}, status="running", attempt_id=cap.attempt_id)

    def _source(self, cap, value):
        item = self.state.add_source(cap.actor, cap.run_id, value, attempt_id=cap.attempt_id)
        provenance = value.get("provenance") or {}
        self.broker.event(cap, "source.read", data={"source_id": item["source_id"], "source_hash": item["source_hash"], "source_type": value.get("source_type"), "title": value.get("title"),
            "dataset_id": value.get("dataset_id"), "report_id": value.get("report_id"), "provenance": {key: provenance[key] for key in ("manifest_sha256", "snapshot", "report", "operation", "arguments", "sha256") if key in provenance}})
        return item["source_id"]

    def _report_tool(self, cap, name, arguments):
        if 'requests' in arguments:
            results = []
            for index, request in enumerate(arguments['requests']):
                self.broker.check(cap)
                try:
                    if index:
                        self.broker.reserve_tool(cap, name)
                    value = self._report_tool(cap, name, request)
                    results.append({'index': index, 'operation_success': True, 'result': value})
                except AgentError as exc:
                    error = self._tool_error(exc)
                    results.append({'index': index, 'operation_success': False, 'error': error})
                    self.broker.event(cap, 'tool.batch_item.failed', data={'name': name, 'index': index, **error})
            complete = all(item['operation_success'] for item in results)
            return {'contract': 'wenquan-batch-read/v1', 'results': results, 'complete': complete,
                    **({} if complete else {'error': {'code': 'batch_incomplete', 'origin': 'batch', 'retryable': False,
                        'message': '部分读取未成功；保留results中成功项，仅按失败项恢复动作继续。', 'recovery_action': 'inspect_failed_items'}})}
        if not self.service.executor:
            raise AgentError("reports_unavailable", "报告服务尚未连接", http_status=503)
        result = self.service.executor.invoke(cap.actor, tool_name="zhuojian." + name + "@v1", payload=arguments, session_id=cap.session_id, run_id=cap.run_id).result
        if result.get("status") == "error":
            error = result.get("error") or {}
            raise AgentError(error.get("code", "report_unavailable"), error.get("message", "报告暂时不可用"), http_status=502,
                             retryable=error.get('retryable') is True)
        if name == "list_reports":
            return result
        report = result.get("report") or {}
        notes = []
        if name in {"query_report_reviews", "aggregate_report_reviews"} and report.get("schema_version") == "report-json/v1" and report.get("analysis_version") == "voxquarry-reporting/0.12.0":
            if (arguments.get("filters") or {}).get("models"):
                notes.append("本次 models 筛选的是报告归并型号标签，不保证对应单一商品 SKU。核对原评 source_product_id 字段与报告范围；联合多个商品的样本应就地注明联合范围，未核实前称归并型号组，不能把组内总数归给单一 SKU，也不能仅凭评论自述型号推定混样原因。")
            if name == "aggregate_report_reviews" and (arguments.get("term_groups") or arguments.get("query")):
                notes.append("本次计数是给定词表的字面命中，标签由查询者命名，不是该主题的语义提及或满意/抱怨频次。整机高低星不改变这一限制；light 可能指轻便而非灯，词面出现也可能是否定或反例。不可把这些计数放在优缺点表下冒充主题占比或频次排名；没有可信语义频次时，按原文支持和影响程度定性排序并说明依据，不为 Top10 重做全量分类。")
        inventory = None
        if name == "query_report_reviews" and notes:
            from agent.review_read_inventory import review_read_inventory
            inventory = review_read_inventory(arguments["report_id"], result.get("data") or {}, self.state.list_sources(cap.actor, cap.run_id))
            if inventory is not None:
                result = {**result, "read_scope_summary": inventory}
                notes.append("read_scope_summary 是已读原评的程序计数，先核对 complete 与筛选范围。引用型号数和星级条数应使用对应分组，不从长文本手算；不同型号范围不能用比例差异归因于写评偏差。")
        if notes:
            result = {**result, "interpretation_notes": notes}
        source = {"source_type": "report", "title": report.get("title") or "报告相关章节", "report_id": arguments["report_id"],
                  "content": result.get("data"), "page": result.get("page"), "report_href": report_href(arguments["report_id"]),
                  "provenance": {"report": report, "snapshot": result.get("snapshot"), "tool": name, "arguments": arguments},
                  "locator": arguments.get("section", "")}
        if notes:
            source["provenance"]["interpretation_notes"] = notes
        if inventory is not None:
            source["provenance"]["read_scope_summary"] = inventory
        source_id = self._source(cap, source)
        result = {**result, "source_id": source_id}
        if name == "query_report_reviews":
            data = result.get("data") or {}
            rows = data.get("items") or []
            result = {**result, "data": {**data, "items": [{**row, "source_id": self._source(cap, {
                "source_type": "review", "title": row.get("review_title") or "原始评论", "content": row,
                "report_id": arguments["report_id"], "provenance": {"report": report, "snapshot": result.get("snapshot")},
                "report_href": report_href(arguments["report_id"])})} for row in rows]}}
        return result

    def _data_tool(self, cap, name, arguments, *, prepare=True):
        if self.data is None:
            raise AgentError("data_unavailable", "原始数据服务尚未连接", http_status=503)
        if prepare and name == 'open_dataset' and cap.runtime_contract == 'wenquan-dsh/v2' and not arguments.get('batch_id'):
            return self._prepare_research_package(cap, arguments)
        if name == 'list_datasets':
            scope = arguments.get('scope', {})
            searches = self.state.get(cap.actor, cap.run_id)['checkpoint'].get('review_link_searches', {})
            identities = {(item['brand'], item['model']) for item in searches.values() if item.get('brand') and item.get('model')}
            if (not arguments.get('cursor') and len(identities) == 1 and not scope.get('job_ids') and not scope.get('models')
                and not research_data_goal.required(self.state.get(cap.actor, cap.run_id)['request']['question'])):
                brand, model = next(iter(identities))
                if not scope.get('brands') or {value.casefold() for value in scope['brands']} == {brand.casefold()}:
                    # An omitted model must not turn a single-model discovery
                    # into an expensive whole-brand artifact scan.
                    arguments = {**arguments, 'scope': {**scope, 'brands': [brand], 'models': [model]}}
        operation = {"list_datasets": "catalog", "open_dataset": "open", "query_dataset": "query", "aggregate_dataset": "aggregate", "export_dataset": "manifest"}[name]
        if name == 'open_dataset' and arguments.get('catalog_id') and arguments.get('batch_id'):
            key = sha256_value([arguments['catalog_id'], arguments['batch_id']])
            rejected = self.state.get(cap.actor, cap.run_id)['checkpoint'].get('data_rejections', {}).get(key)
            if rejected:
                raise AgentError(rejected['code'], rejected['message'], http_status=409)
        request = {key: value for key, value in arguments.items() if key not in {'include_raw', 'force', 'max_batches'}}
        result = self.data.call(cap.actor, operation, request)
        products = result.get("items", []) if name == "list_datasets" else [item.get("job", {}) for item in result.get("inputs", [])]
        public_products = [{"catalog_id": item.get("job_id") or item.get("id"), "product_url": item.get("product_url") or item.get("target_url"),
                            "brand": item["brand"], "model": item["model"]}
                           for item in products if isinstance(item, dict) and all(isinstance(item.get(key), str) and item[key] for key in ("brand", "model"))
                           and (item.get("job_id") or item.get("id")) and (item.get("product_url") or item.get("target_url"))]
        for item in public_products[:32]:
            try:
                self.egress.register_catalog_products(cap.actor, cap.run_id, [item])
            except AgentError as error:
                if error.code not in {"public_product_limit", "public_product_invalid", "web_url_invalid", "egress_url_limit"}:
                    raise
        if name == "list_datasets" or (name == 'open_dataset' and result.get('status') == 'requires_batches'):
            self._record_data_goal(cap, name, arguments, result)
            return result
        if name == 'open_dataset':
            checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
            ids = [item.get('job', {}).get('id') for item in result.get('inputs', [])]
            self._checkpoint(cap, read_job_ids=sorted(set(checkpoint.get('read_job_ids', [])) | {value for value in ids if value}))
        dataset_id = result.get("dataset_id") or arguments.get("dataset_id")
        if name == "export_dataset":
            result = self._export_research_files(cap, result, arguments)
        union = name == 'aggregate_dataset' and bool(arguments.get('dataset_ids'))
        source_id = self._source(cap, {"source_type": "analysis" if union else "dataset", "title": "原始评论数据" if name != "aggregate_dataset" else "评论统计",
            **({} if union else {"dataset_id": dataset_id}), "content": result,
            "provenance": {**({'dataset_ids': arguments['dataset_ids'], 'sources': result.get('sources', [])} if union else {'dataset_id': dataset_id}),
                           "manifest_sha256": result.get("manifest_sha256"), "operation": operation, "arguments": arguments}})
        if name == "query_dataset":
            result = {**result, "items": [{**row, "source_id": self._source(cap, {"source_type": "review", "title": row.get("review_title") or "原始评论", "content": row,
                "dataset_id": dataset_id, "locator": row.get("record_id"), "provenance": {"dataset_id": dataset_id, "manifest_sha256": result.get("manifest_sha256")}})} for row in result["items"]]}
        self._record_data_goal(cap, name, arguments, result, source_id)
        self.broker.event(cap, "data.read", data={"dataset_id": dataset_id, "operation": operation, "manifest_sha256": result.get("manifest_sha256"), "total": result.get("total")})
        return {**result, "source_id": source_id}

    def _export_research_files(self, cap, manifest, arguments):
        dataset_id = manifest['dataset_id']
        native = cap.runtime_contract == 'wenquan-dsh/v2'
        include_raw = arguments.get('include_raw', not native)
        key = sha256_value([cap.attempt_id, dataset_id, manifest.get('manifest_sha256'), include_raw])
        checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
        cached = checkpoint.get('dataset_exports', {}).get(key)
        if cached and not arguments.get('force'):
            self.broker.event(cap, 'data.export_reused', data={'dataset_id': dataset_id, 'manifest_sha256': manifest.get('manifest_sha256')})
            return {**cached, 'manifest': manifest}
        paths, raw_paths, total = [], [], 0
        refresh_suffix = '-' + new_id('refresh')[-12:] if arguments.get('force') else ''
        shards = [('normalized_review', part) for part in manifest.get('shards', [])]
        normalized_key = sha256_value([cap.attempt_id, dataset_id, manifest.get('manifest_sha256'), False])
        normalized = checkpoint.get('dataset_exports', {}).get(normalized_key) if include_raw and not arguments.get('force') else None
        if normalized:
            paths, total = list(normalized['analysis_paths']), normalized['bytes']
        if include_raw:
            shards.extend(('channel_raw_payload', part) for artifact in manifest.get('raw_artifacts', []) for part in artifact['shards'])
        for index, (source_type, shard) in enumerate(shards):
            if normalized and source_type == 'normalized_review':
                continue
            self.broker.check(cap)
            content = self.data.export(cap.actor, dataset_id, shard.get('path') or shard.get('name'), source_type=source_type)
            total += len(content)
            if total > 8 * 1024**3:
                raise AgentError('dataset_export_limit', '本次数据超过分析文件容量')
            if shard.get('sha256') and shard['sha256'] != 'sha256:' + hashlib.sha256(content).hexdigest():
                raise AgentError('dataset_changed', '数据分片与导出版本不一致', http_status=409)
            filename = dataset_id + '-' + source_type + '-' + str(index) + refresh_suffix + '.jsonl'
            self.manager.put_input(cap.attempt_id, filename, content)
            (raw_paths if source_type == 'channel_raw_payload' else paths).append('/inputs/' + filename)
        result = {'dataset_id': dataset_id, 'manifest_sha256': manifest.get('manifest_sha256'), 'manifest': manifest,
                  'analysis_paths': paths, 'raw_payload_paths': raw_paths, 'bytes': total,
                  'citation_prefix': 'review:' + dataset_id + ':',
                  'instructions': 'analysis_paths包含筛选后的标准出现记录，跨批实体计数用research_methods.ReviewSet去重；raw_payload_paths是渠道原始载荷，可能包含排除项，不能合并计数。正文review_text，标题review_title。读完正文后直接引用[review:dataset_id:record_id]；dsrc_不是正文src_引用。文件只在当前分析环境复用；缺失时export_dataset(force=true)。'}
        if native:
            schema = {'contract': 'wenquan-research-package/v1', 'format': 'jsonl', 'row_grain': 'occurrences',
                      'fields': {'record_id': 'string: occurrence ID', 'review_entity_id': 'string: deduplication identity',
                                 'review_text': 'string: full original review text', 'review_title': 'string', 'rating': 'number|null'},
                      'methods': {'module': 'research_methods', 'api': ['load_result(path, pointer="")', 'ReviewSet([export_result, ...])'],
                                  'cli': 'python -m research_methods schema'},
                      'citation': '[review:dataset_id:record_id]'}
            for suffix, value in [('manifest', manifest), ('schema', schema)]:
                filename = dataset_id + '-' + suffix + refresh_suffix + '.json'
                self.manager.put_input(cap.attempt_id, filename, json.dumps(value, ensure_ascii=False).encode())
                result[suffix + '_path'] = '/inputs/' + filename
            result['methods'] = schema['methods']
        with cap.checkpoint_lock:
            checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
            exports = dict(checkpoint.get('dataset_exports', {}))
            if arguments.get('force'):
                exports = {k: value for k, value in exports.items() if value.get('dataset_id') != dataset_id}
            exports[key] = {k: v for k, v in result.items() if k != 'manifest'}
            self._checkpoint(cap, dataset_exports=dict(list(exports.items())[-64:]),
                             exported_datasets=list(dict.fromkeys([*checkpoint.get('exported_datasets', []), dataset_id])))
        return result

    def _prepare_research_package(self, cap, arguments):
        prepared = self.data.call(cap.actor, 'prepare', arguments)
        self._record_data_goal(cap, 'prepare', arguments, prepared)
        datasets = []
        for item in prepared.get('datasets', []):
            self.broker.check(cap)
            manifest = self.data.call(cap.actor, 'manifest', {'dataset_id': item['dataset_id']})
            if item.get('manifest_sha256') and item['manifest_sha256'] != manifest.get('manifest_sha256'):
                raise AgentError('dataset_changed', '数据包清单与冻结版本不一致', http_status=409)
            binding = item.get('catalog_binding')
            if binding is None and prepared.get('catalog_binding'):
                binding = {**prepared['catalog_binding'], 'batch_id': item['batch_id'],
                           'job_ids': sorted({row['job']['id'] for row in manifest.get('inputs', []) if row.get('job', {}).get('id')})}
            opened = {**manifest, **({'catalog_binding': binding} if binding else {}),
                      **({'source_coverage': item['source_coverage']} if 'source_coverage' in item else {})}
            source_id = self._source(cap, {'source_type': 'dataset', 'title': '原始评论数据', 'dataset_id': item['dataset_id'],
                'content': opened, 'provenance': {'operation': 'open', 'manifest_sha256': manifest.get('manifest_sha256')}})
            self._record_data_goal(cap, 'open_dataset', arguments, opened, source_id)
            files = self._export_research_files(cap, manifest, {'include_raw': False})
            export_source = self._source(cap, {'source_type': 'dataset', 'title': '原始评论数据', 'dataset_id': item['dataset_id'],
                'content': files, 'provenance': {'dataset_id': item['dataset_id'], 'operation': 'manifest',
                                                'manifest_sha256': manifest.get('manifest_sha256')}})
            datasets.append({**files, 'source_id': export_source,
                             **{key: item[key] for key in ('catalog_binding', 'source_coverage') if key in item}})
            with cap.checkpoint_lock:
                checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
                ids = {row['job']['id'] for row in manifest.get('inputs', []) if row.get('job', {}).get('id')}
                self._checkpoint(cap, read_job_ids=sorted(set(checkpoint.get('read_job_ids', [])) | ids))
        result = {**prepared, 'contract': 'wenquan-research-package/v1', 'datasets': datasets,
                  'instructions': '先检查coverage和failed_batches。使用全部datasets构建ReviewSet，在一个Python脚本完成覆盖、去重、统计、原文检查和引用。完整原始载荷入口为export_dataset(include_raw=true)。有pending_batch_ids时传同一catalog_id继续，不改变原scope。'}
        content = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        digest = 'sha256:' + hashlib.sha256(content).hexdigest()
        filename = 'research-package-' + digest.removeprefix('sha256:') + '.json'
        self.manager.put_input(cap.attempt_id, filename, content)
        return {**result, 'package_path': '/inputs/' + filename,
                'package_file': {'full_result_file': '/inputs/' + filename, 'full_result_bytes': len(content), 'sha256': digest}}

    def _record_data_goal(self, cap, name, args, result, source_id=None):
        with cap.checkpoint_lock:
            checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
            self._checkpoint(cap, raw_data_evidence=research_data_goal.record(checkpoint, name, args, result, source_id))

    @staticmethod
    def _trace_arguments(name, arguments):
        # Preserve replayable query selectors by default, but keep user
        # answer text, scripts and file contents in opt-in protected payloads.
        private = {"answer", "code", "content", "research_note", "command", "markdown", "findings", "report"}
        return {key: {"sha256": sha256_value(value), "characters": len(value) if isinstance(value, str) else None}
                if key in private else value for key, value in arguments.items()}

    @staticmethod
    def _tool_error(exc):
        caller_codes = {'tool_arguments_invalid', 'invalid_filter', 'invalid_group_by',
                        'invalid_section', 'citation_unknown', 'citation_requires_original_source'}
        preconditions = {'report_scope_changed', 'report_scope_mismatch', 'report_scope_unverified', 'report_source_not_read',
                         'retry_requires_attempt', 'answer_invalid', 'analysis_path_rejected', 'product_identity_unverified', 'catalog_expired'}
        source_codes = {'invalid_source', 'unsafe_source', 'unsafe_path', 'source_unavailable', 'source_changed', 'dataset_changed',
                        'dataset_conflict', 'mixed_normalization_versions'}
        origin = ('budget' if exc.code in {'research_budget_reached', 'research_active_budget_reached', 'research_web_budget_reached',
                    'research_cost_budget_reached', 'research_cost_unreconciled'} else 'caller' if exc.code in caller_codes else 'precondition' if exc.code in preconditions
                  else 'tool_contract' if exc.code in {'invalid_request', 'invalid_payload'} else 'source' if exc.code in source_codes
                  else 'capacity' if exc.code in {'dataset_too_large', 'response_too_large', 'dataset_export_limit', 'report_too_large'}
                  else 'model_service' if exc.code.startswith('model_') else 'service')
        recovery = ('deliver_available_evidence' if origin == 'budget' else 'correct_arguments' if origin == 'caller' else 'refresh_required_evidence' if origin == 'precondition'
                    else 'continue_original_scope_batches' if origin == 'capacity'
                    else 'retry_after_service_recovery' if exc.retryable else 'inspect_source_or_use_existing_evidence')
        error = {'code': exc.code, 'message': exc.message, 'retryable': exc.retryable,
                 'origin': origin, 'recovery_action': recovery}
        if getattr(exc, 'upstream_diagnostic', None):
            error['diagnostic'] = exc.upstream_diagnostic
        return error

    def tool(self, cap, name, arguments, tool_call_id=""):
        self.broker.check(cap)
        violations = list(self._schemas[name].iter_errors(arguments)) if name in self._schemas else [None]
        if violations:
            error = self._tool_error(AgentError('tool_arguments_invalid', '工具参数不符合当前接口，请按工具说明修正'))
            error['fields'] = sorted({'/'.join(map(str, item.absolute_path)) or '/' for item in violations if item is not None})[:16]
            self.state.event(cap.actor, cap.run_id, LABELS.get(name, '调用研究工具'), 'failed', tool_name=name,
                detail={'summary': error['message'], 'code': error['code']}, attempt_id=cap.attempt_id)
            self.broker.event(cap, 'tool.rejected', data={'name': name, 'tool_call_id': tool_call_id, **error})
            return {'operation_success': False, 'error': error}
        self.broker.reserve_tool(cap, name)
        label, event_id = LABELS[name], new_id("evt")
        self.state.event(cap.actor, cap.run_id, label, "running", tool_name=name, event_id=event_id,
                         detail={"summary": tool_progress_summary(name, arguments)}, attempt_id=cap.attempt_id)
        self.broker.event(cap, "tool.started", data={"name": name, "tool_call_id": tool_call_id, "arguments": self._trace_arguments(name, arguments)}, payload={"arguments": arguments})
        started = time.monotonic()
        collection_key = sha256_value({'attempt': cap.attempt_id, 'channel': arguments.get('channel'), 'url': arguments.get('product_url')}) if name == 'collect_product' else None
        try:
            with cap.tool_gate.enter(read_only=cap.runtime_contract == 'wenquan-dsh/v2' and name in CONCURRENT_READ_TOOLS):
                cap.measure('tool_queue_ms', (time.monotonic() - started) * 1000)
                self.broker.check(cap)
                rejected = self.state.get(cap.actor, cap.run_id)['checkpoint'].get('collection_rejections', {}).get(collection_key) if collection_key else None
                if rejected:
                    error = AgentError(rejected['code'], rejected['message'], http_status=409)
                    error.repeated_collection_rejection = True
                    raise error
                result = self._dispatch(cap, name, arguments)
            summary = tool_progress_summary(name, arguments, result)
            step_status = 'failed' if isinstance(result, dict) and (result.get('error') or
                type(result.get('exit_code')) is int and result['exit_code'] != 0) else 'completed'
            result = self._tool_delivery(cap, result, name=name)
            if self.state.get(cap.actor, cap.run_id)["status"] == "running":
                self.state.event(cap.actor, cap.run_id, label, step_status, tool_name=name, event_id=event_id, detail={"summary": summary}, attempt_id=cap.attempt_id)
            self.broker.event(cap, "tool.finished", data={"name": name, "tool_call_id": tool_call_id, "status": step_status, "duration_ms": int((time.monotonic() - started) * 1000), "output_sha256": sha256_value(result)}, payload={"result": result})
            return result
        except AgentError as exc:
            if self.state.get(cap.actor, cap.run_id)["status"] == "running":
                self.state.event(cap.actor, cap.run_id, label, "failed", tool_name=name, event_id=event_id, detail={"summary": exc.message, "code": exc.code}, attempt_id=cap.attempt_id)
            diagnostic = {"name": name, "code": exc.code, "duration_ms": int((time.monotonic() - started) * 1000)}
            if getattr(exc, 'upstream_diagnostic', None):
                diagnostic['upstream'] = exc.upstream_diagnostic
            if getattr(exc, "transport_kind", None) in TRANSPORT_KINDS:
                diagnostic["transport_kind"] = exc.transport_kind
            self.broker.event(cap, "tool.failed", data={**diagnostic, "tool_call_id": tool_call_id})
            # Native tool errors are visible to the Harness so it can repair
            # arguments or use another source; one failure does not erase work.
            error = self._tool_error(exc)
            if name == 'open_dataset' and arguments.get('catalog_id') and arguments.get('batch_id'):
                with cap.checkpoint_lock:
                    checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
                    changes = {'raw_data_evidence': research_data_goal.failure(checkpoint, arguments, error)}
                    if not exc.retryable and exc.code in {'unsafe_source', 'unsafe_path', 'invalid_source', 'source_changed', 'source_unavailable', 'catalog_expired'}:
                        rejected = dict(checkpoint.get('data_rejections', {}))
                        rejected[sha256_value([arguments['catalog_id'], arguments['batch_id']])] = error
                        changes['data_rejections'] = dict(list(rejected.items())[-256:])
                    self._checkpoint(cap, **changes)
            if collection_key and not exc.retryable and exc.code not in {'product_identity_unverified', 'tool_arguments_invalid'} and self.state.get(cap.actor, cap.run_id)['status'] == 'running':
                with cap.checkpoint_lock:
                    rejected = dict(self.state.get(cap.actor, cap.run_id)['checkpoint'].get('collection_rejections', {}))
                    self._checkpoint(cap, collection_rejections={**rejected, collection_key: error})
            # Infrastructure failures cannot be repaired by changing a product
            # or setting retry=true. End with the actual failure, preserving
            # the request and receipts for an explicit resume after repair.
            fatal = getattr(exc, 'repeated_collection_rejection', False) or exc.code in {'collection_schema_unavailable', 'collection_server_error',
                'collection_contract_invalid', 'dataset_storage_unavailable', 'upstream_server_error'}
            transient = exc.code in {'collection_unavailable', 'upstream_unavailable', 'authority_unavailable'} and exc.retryable
            if transient:
                with cap.checkpoint_lock:
                    checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
                    key = sha256_value({'attempt': cap.attempt_id, 'tool': name, 'code': exc.code})
                    failures = dict(checkpoint.get('service_failures', {}))
                    failures[key] = failures.get(key, 0) + 1
                    self._checkpoint(cap, service_failures=failures)
                    fatal = failures[key] >= 2
            native_delivery = self.state.get(cap.actor, cap.run_id)['request'].get('runtime_contract') == 'wenquan-dsh/v2'
            if fatal and native_delivery:
                error['recovery_hint'] = '此操作暂不能完成，不要重复相同失败请求。复用已有证据交付能确认的结果，就地说明缺口。'
            if fatal and not native_delivery and self.state.get(cap.actor, cap.run_id)['status'] == 'running':
                self.state.finish(cap.actor, cap.run_id, exc.message + ' 本次任务受阻，修复服务后可继续。', status='blocked',
                    metadata={'error': error, 'can_resume': True, 'citations': []}, attempt_id=cap.attempt_id)
                self.traces.finish_trace(cap.actor, cap.trace_id, status='blocked', error=diagnostic, attempt_id=cap.attempt_id)
                self.broker.pause(cap)
            if name == "open_dataset" and not exc.retryable and exc.code in {"unsafe_source", "invalid_source"}:
                error["recovery_hint"] = (
                    "这是当前来源的读取或契约问题，不代表数据不存在，也不是连接超时。"
                    "没有新证据时不要反复改origin、渠道或逐个尝试任务来读取同一批资料，不要扩大范围或绕过校验。"
                    "先用已读报告回答能确认的部分，只将受影响的原评核查列为未完成；"
                    "有明确的新来源或来源已修复后再读取。"
                )
            return {"operation_success": False, "error": error}
        finally:
            cap.measure('business_tool_ms', (time.monotonic() - started) * 1000)

    @staticmethod
    def _review_reading_view(row):
        if not isinstance(row, dict):
            return row
        view = {key: value for key, value in row.items() if key not in REVIEW_STORAGE_FIELDS and key != "original_fields"}
        original = row.get("original_fields")
        if isinstance(original, dict):
            # Remove only exact duplicates. Preserve differing original text,
            # null/false/zero, and unknown fields without interpretation.
            unique = {}
            for key, value in original.items():
                normalized = REVIEW_ORIGINAL_ALIASES.get(key, key)
                # Python container equality conflates nested false/0 and 1/1.0;
                # canonical JSON preserves those scalar types at every depth.
                if normalized not in view or sha256_value(value) != sha256_value(view[normalized]):
                    unique[key] = value
            if unique:
                view["original_fields"] = unique
        elif "original_fields" in row:
            view["original_fields"] = original
        return view

    @classmethod
    def _reading_view(cls, name, result):
        if not isinstance(result, dict):
            return result
        if name == "query_dataset" and isinstance(result.get("items"), list):
            return {**result, "items": [cls._review_reading_view(row) for row in result["items"]]}
        data = result.get("data")
        if not isinstance(data, dict):
            return result
        if name == "list_reports" and isinstance(data.get("items"), list) and all(isinstance(row, dict) for row in data["items"]):
            # This is a selection index. Keep the full catalog in the existing
            # original_result_file, not repeated provenance in the model context.
            scope_keys = {"brands", "models", "categories", "category_id", "market_regions", "channels",
                "voltage_platform_keys", "voltage_platforms", "energy_type", "energy", "date_range", "ratings",
                "has_text", "keywords", "keyword_mode", "keyword_field", "exclude_categories", "include_unknown_voltage_platform"}
            items = []
            for row in data["items"]:
                item = {key: row[key] for key in ("report_id", "report_type", "generated_at", "status", "archived") if key in row}
                item["title"] = row.get("display_name") or row.get("title")
                item["scope"] = {key: value for key, value in (row.get("scope") or {}).items()
                    if key in scope_keys and value not in ("", [], {})}
                item["samples"] = {key: value for key, value in (row.get("samples") or {}).items()
                    if key in {"review_entities", "written_review_entities"}}
                item["quality"] = {key: value for key, value in (row.get("quality") or {}).items() if key in {"grade", "limitation_count"}}
                items.append(item)
            return {**{key: value for key, value in result.items() if key != "citations"}, "data": {**data, "items": items},
                "instruction": "这是选报告的目录，按scope中的型号、品类等筛选摘要选取，不能只看标题。匹配后读get_data_quality及相关章节；目录不是研究证据。完整筛选条件、来源和未展开字段保存在original_result_file，需要时再读。"}
        if name == "query_report_reviews" and isinstance(data.get("items"), list):
            return {**result, "data": {**data, "items": [cls._review_reading_view(row) for row in data["items"]]}}
        content = data.get("content")
        if (name == "get_report_section" and data.get("section") == "index" and isinstance(content, dict)
                and isinstance(data.get("descriptors"), list)
                and sha256_value(data["descriptors"]) == sha256_value(content.get("descriptors"))):
            return {**result, "data": {key: value for key, value in data.items() if key != "descriptors"}}
        return result

    def _save_tool_result(self, cap, raw, prefix="tool-result-"):
        digest = hashlib.sha256(raw).hexdigest()
        filename = prefix + digest + ".json"
        self.manager.put_input(cap.attempt_id, filename, raw)
        return {"full_result_file": "/inputs/" + filename, "full_result_bytes": len(raw), "sha256": "sha256:" + digest}

    @staticmethod
    def _report_reading_view(value):
        """Keep known field meanings visible even when the report is in a file."""
        groups = {}
        def collect(kind, definition, path):
            key = sha256_value({"kind": kind, "definition": definition})
            if key not in groups:
                if len(groups) >= 8:
                    return
                groups[key] = {"kind": kind, "definition": definition, "example_paths": []}
            if len(path) <= 1000 and len(groups[key]["example_paths"]) < 2:
                groups[key]["example_paths"].append(path)
        view = model_evidence_view(value, definition_sink=collect)
        # Full, node-specific definitions remain in the reading JSON. This is
        # only a bounded index, not an interpretation of unknown report fields.
        return view, list(groups.values())

    def _tool_delivery(self, cap, result, *, name=""):
        """Keep full tool results accessible without replaying them every turn."""
        raw = json.dumps(result, ensure_ascii=False).encode()
        view = self._reading_view(name, result)
        guide = []
        report_read = name in {"get_report_section", "aggregate_report_reviews"}
        saved_report = name == "read_saved_source" and isinstance(result, dict) and result.get("source_type") == "report"
        saved_analysis = name == "read_saved_source" and isinstance(result, dict) and result.get("source_type") == "analysis" and isinstance(result.get("content"), dict)
        analysis_meta = result["content"] if saved_analysis else result if name == "analyze_product_reviews" and isinstance(result, dict) else {}
        report_meta = (result.get("report") or {}) if report_read else (
            (result.get("provenance") or {}).get("report") or {}) if saved_report else {}
        # These definitions were checked against this producer. Unknown/future
        # versions keep their original values without inferred field meanings.
        if ((report_read or saved_report) and report_meta.get("schema_version") == "report-json/v1" and report_meta.get("analysis_version") == "voxquarry-reporting/0.12.0") or (
                analysis_meta.get("schema_version") == "wenquan-dataset-analysis/v1" and analysis_meta.get("version") == "wenquan-dataset-analysis/1.0.0"):
            view, guide = self._report_reading_view(view)
        original_file = None
        if view != result:
            original_file = self._save_tool_result(cap, raw, "tool-original-")
            result = view
            raw = json.dumps(result, ensure_ascii=False).encode()
        # A requested reference chapter is already a bounded reading unit.
        # Keep ordinary chapters together with their citation ID; larger tables
        # still use the existing file path, as do bulk tool results.
        inline_limit = 32 * 1024 if name == 'read_research_reference' else 12 * 1024
        if len(raw) > inline_limit:
            reference = (result['package_file'] if name == 'open_dataset' and result.get('contract') == 'wenquan-research-package/v1'
                         and result.get('package_file') else self._save_tool_result(cap, raw))
            fields = {key: {"type": type(value).__name__, "length": len(value) if isinstance(value, (dict, list, str)) else None,
                            **({"keys": list(value)[:40]} if isinstance(value, dict) else {})}
                      for key, value in result.items()} if isinstance(result, dict) else {"type": type(result).__name__}
            result = {**reference, "structure": fields, "complete_content_in_file": True,
                "instruction": "完整工具结果已保存为 JSON 文件，尚未全部展示给你。可用 bash 运行 Python 读取所需字段或逐条原文；不要一次打印整个文件，也不要将字段目录当作已经阅读完整原文。"}
            if original_file:
                result["instruction"] = "优先读取 full_result_file 中的完整阅读视图；记录、完整正文、数值和引用 source_id 均保留，重复与存储字段另存，已知报告指标附有说明。可用 bash 运行 Python 读取所需字段或逐条原文及其_evidence_semantics，不要一次打印整个文件。需要核对未经整理的 JSON 时再读取 original_result_file。"
            original = json.loads(raw)
            if isinstance(original, dict):
                for key in ("source_id", "analysis_result_id", "citation_eligible", "dataset_id", "definition_id", "analysis_id", "report_id", "page", "status", "total", "next_cursor", "manifest_sha256", "exit_code", "output_truncated", "sample", "readiness", "category_id", "analysis_scope", "citation_prefix", "registered_urls", "url", "error", "complete"):
                    if key in original:
                        if key in original:
                            result[key] = original[key]
                if name in {'list_datasets', 'open_dataset'} and original.get('catalog_id'):
                    for key in ('catalog_id', 'scope', 'batch_count', 'total_sources', 'unavailable_count', 'catalog_complete', 'coverage', 'pending_batch_ids', 'failed_batches', 'next', 'package_path'):
                        if key in original:
                            result[key] = original[key]
                    result['batches'] = [{k: b[k] for k in ('batch_id', 'compatibility')} |
                                         {'job_count': len(b['job_ids'])} for b in original.get('batches', [])]
                    result['instruction'] += ' 本页兼容批次可按 catalog_id + batch_id 打开；next_cursor 继续原目录，单批成功不代表原范围完成。'
                if name in {"read_research_reference", "read_saved_source", "query_report_reviews", "aggregate_report_reviews"}:
                    notes = original.get("interpretation_notes") or (original.get("provenance") or {}).get("interpretation_notes")
                    if notes:
                        result["interpretation_notes"] = notes
                    inventory = original.get("read_scope_summary") or (original.get("provenance") or {}).get("read_scope_summary")
                    if inventory is not None:
                        result["read_scope_summary"] = inventory
                section_data = original.get('data') if isinstance(original.get('data'), dict) else original
                if name == 'get_report_section' and 'content' in section_data:
                    # A small, explicitly partial view avoids a file round-trip
                    # when the requested rows already answer the question.
                    def preview(value, depth=0):
                        if depth > 5:
                            return {'omitted': True, 'type': type(value).__name__}
                        if isinstance(value, dict):
                            keys = [k for k in value if k not in {'evidence_ids', 'positive_evidence_ids', 'negative_evidence_ids', 'raw_record'}]
                            shown = {k: preview(value[k], depth + 1) for k in keys[:12]}
                            if len(shown) < len(value):
                                shown['_omitted_fields'] = [k for k in value if k not in shown]
                            return shown
                        if isinstance(value, list):
                            return {'items': [preview(v, depth + 1) for v in value[:4]], 'total_items': len(value), 'partial': len(value) > 4}
                        if isinstance(value, str) and len(value) > 900:
                            return {'excerpt': value[:900], 'partial': True}
                        return value
                    brief = preview(section_data['content'])
                    if len(json.dumps(brief, ensure_ascii=False).encode()) <= 8000:
                        result.update(content_preview=brief, preview_is_partial=True,
                                      preview_note='仅为已请求章节的部分内容，不是全文或完整排名；范围/反例未覆盖时继续读完整结果文件。')
        if original_file:
            result = {**result, "original_result_file": original_file["full_result_file"],
                "original_result_bytes": original_file["full_result_bytes"], "original_result_sha256": original_file["sha256"]}
        if guide:
            result = {**result, "report_field_guide": {"definitions": guide,
                "instruction": "这些说明只适用于所列路径识别出的字段。完整阅读JSON在各节点保存_evidence_semantics；不认识的同名字段不据此解释。原始报告与引用内容未改。"}}
        if isinstance(result, dict):
            result = {**result, 'result_contract': 'wenquan-tool-result/v1',
                      'operation_success': not bool(result.get('error')) and result.get('exit_code', 0) == 0}
            if result.get('full_result_file'):
                result['loader'] = {'module': 'research_methods', 'function': 'load_result',
                                    'path': result['full_result_file'], 'pointer': ''}
            budget = self.broker.budget_status(cap)
            result = {**result, "research_progress": {
                "remaining_model_calls": budget["remaining_model_calls"],
                "remaining_tokens": max(0, budget["token_limit"] - budget["total_tokens"]),
                "token_limit": budget["token_limit"],
                "remaining_search_calls": budget["remaining_search_calls"],
                "remaining_fetch_calls": budget["remaining_fetch_calls"],
                "remaining_active_seconds": int(budget["remaining_active_seconds"]),
                "remaining_model_budget_cny": budget["model_cost"]["remaining_cny"]}}
        return result

    def _dispatch(self, cap, name, args):
        if name == "prepare_product_research":
            if len(args["job_ids"]) != 1:
                raise AgentError("invalid_request", "单产品研究需要一个已核实的采集任务编号。")
            dataset = self._data_tool(cap, "open_dataset", {"scope": {"job_ids": args["job_ids"]}, "origin": "wotu"}, prepare=False)
            collection_dataset_id = dataset["dataset_id"]
            jobs = [item.get("job") or {} for item in dataset.get("inputs", [])]
            target_ids = {str(job["source_product_id"]) for job in jobs if job.get("source_product_id")
                          and (job.get("source_product_identity") or {}).get("status") == "resolved"}
            if len(jobs) == 1 and len(target_ids) == 1:
                dataset = self._data_tool(cap, "open_dataset", {"scope": {"job_ids": args["job_ids"],
                    "source_product_ids": sorted(target_ids)}, "origin": "wotu"})
            prior = self.state.get(cap.actor, cap.run_id)["checkpoint"].get("prepared_product") or {}
            if prior.get("attempt_id") == cap.attempt_id and prior.get("dataset_id") == dataset["dataset_id"]:
                return prior["result"]
            report_scope = {"job_ids": args["job_ids"], **({"source_product_ids": sorted(target_ids)} if target_ids else {})}
            candidates = self._dispatch(cap, "report_candidates", {"scope": report_scope})
            result = {"dataset_id": dataset["dataset_id"], "dataset_source_id": dataset["source_id"],
                "collected_dataset_id": collection_dataset_id, "source_product_ids": sorted(target_ids),
                "review_counts": dataset.get("record_counts"),
                "manifest_sha256": dataset.get("manifest_sha256"),
                "report_candidates": candidates, "report_scope": report_scope,
                "instruction": "先查已有匹配报告；没有时，候选文字评论达到500条且范围完全匹配，优先create_report，完成后读相关章节与证据。注意候选基于快照，可能比当前采集旧或含其他SKU：先核对目标source_product_ids与样本量，不把扩大范围的报告归给目标商品。小样本或快照尚未覆盖时用analyze_product_reviews复用分析报告原子分析；只在缺口需要时export_dataset。没有自动导出或重新分析。"}
            self._checkpoint(cap, prepared_product={"attempt_id": cap.attempt_id, "dataset_id": dataset["dataset_id"], "result": result})
            return result
        if name == "review_findings":
            return self.review_findings(cap, args["findings"])
        if name == "rank_review_topics":
            return self.rank_review_topics(cap, args)
        if name == "read_research_reference":
            from .research_library import read_reference
            result = read_reference(**args)
            if "text" in result:
                result["source_id"] = self._source(cap, {"source_type": "reference", "title": result["title"],
                    "content": result["text"], "locator": {"section_id": result["section_id"], "original_filename": result["original_filename"]},
                    "provenance": {"sha256": result["original_sha256"], "reference_id": result["reference_id"],
                        "scope": result["scope"], "edition_registered_at": result["edition_registered_at"],
                        "interpretation_notes": result["interpretation_notes"]}})
            return result
        if name == "read_saved_source":
            row = self.state.source(cap.actor, cap.session_id, args["source_id"])
            source = row["source"]
            source_id = row["source_id"] if row["run_id"] == cap.run_id else self._source(cap, source)
            if self._execution_preview(source):
                return {**source, "analysis_result_id": source_id, "citation_eligible": False,
                    "instruction": "这是此前代码的输出，可能只含原文预览。引用评论请取完整原文的source_id，不能把这份输出作为评论来源。"}
            return {**source, "source_id": source_id, "source_sha256": row["source_hash"], "saved_source_id": row["source_id"],
                "instruction": "这是已经保存的完整来源。正文引用本次返回的source_id；同一会话已读来源的source_id可继续引用。"}
        if name in {"list_reports", "get_report_section", "query_report_reviews", "aggregate_report_reviews", "get_metric", "list_evidence_groups", "query_evidence", "get_data_quality"}:
            return self._report_tool(cap, name, args)
        if name in {"list_datasets", "open_dataset", "query_dataset", "aggregate_dataset", "export_dataset"}:
            return self._data_tool(cap, name, args)
        if name in {"get_product_review_definition", "analyze_product_reviews"}:
            if self.data is None:
                raise AgentError("data_unavailable", "原始数据服务尚未连接", http_status=503)
            result = self.data.call(cap.actor, "definition" if name == "get_product_review_definition" else "analyze", args)
            source_id = self._source(cap, {"source_type": "analysis", "title": "品类报告分析方法" if name == "get_product_review_definition" else "单产品网评分析",
                "content": result, "dataset_id": result.get("dataset_id"),
                "provenance": {key: result[key] for key in ("schema_version", "dataset_id", "manifest_sha256", "definition_id", "definition_sha256", "analysis_id", "analysis_sha256", "source_versions") if key in result}})
            self._record_data_goal(cap, name, args, result, source_id)
            return {**result, "source_id": source_id}
        if name == "list_attachments":
            return self.attachments.list(cap.actor, cap.session_id)
        if name == "read_attachment":
            result = self.attachments.read_text(cap.actor, cap.session_id, **args)
            provenance = {"sha256": result["attachment"]["sha256"]}
            if result["attachment"]["media_type"] in {"image/png", "image/jpeg", "image/webp"}:
                try:
                    key = self.broker.image_reader_key()
                    analysis = self.attachments.image_analysis(cap.actor, cap.session_id, args["attachment_id"], key)
                    cached = analysis is not None
                    if analysis is None:
                        original = self.attachments.download_path(cap.actor, cap.session_id, args["attachment_id"]).read_bytes()
                        analysis = self.broker.read_image(cap, original, result["attachment"]["media_type"])
                        self.broker.check(cap)
                        self.attachments.image_analysis(cap.actor, cap.session_id, args["attachment_id"], key, value=analysis)
                    provenance.update(image_reader=analysis["model"], image_reader_version=analysis["version"])
                    result.update(ocr_text=result["text"], text="图片视觉解读（Qwen 3.8 Flash，模型解读，不是用户原文）：\n" + analysis["text"],
                                  image_analysis={"model": analysis["model"], "cached": cached}, offset=0, next_offset=None, locators=[])
                    result.update(total_chars=len(result["text"]), text_sha256="sha256:" + hashlib.sha256(result["text"].encode()).hexdigest(),
                                  start_line=1, end_line=len(result["text"].splitlines()))
                except AgentError as error:
                    if error.code not in {"vision_unavailable", "vision_price_unverified", "model_auth_failed", "model_access_denied", "model_not_found", "model_rate_limited", "model_unavailable", "model_stream_interrupted", "vision_response_incomplete"} or not result["text"].strip():
                        raise
                    # OCR remains useful for screenshots, but is never presented
                    # as a successful visual interpretation of charts/photos.
                    result.update(image_analysis={"status": "unavailable", "error_code": error.code},
                                  warning="视觉识别暂未完成，以下仅为OCR文字；不能据此推断图片布局、颜色或无文字内容。")
            sid = self._source(cap, {"source_type": "upload", "title": result["attachment"]["filename"], "attachment_id": args["attachment_id"], "content": result["text"], "page": {"offset": result["offset"], "next_offset": result["next_offset"]}, "locator": result.get("locators"), "provenance": provenance})
            return {**result, "source_id": sid}
        if name in {"search_web", "read_web_page", "find_review_links"} and not cap.settings["web_enabled"]:
            raise AgentError("web_disabled", "已关闭直接搜索和网页补充；仍可通过数据采集采集评论并使用内部资料。")
        if name == 'find_review_links':
            from .review_link_discovery import candidates
            plan = self.egress.plan_review_search(cap.actor, cap.run_id, args['brand'], args['model'], args.get('channel', 'all'))
            checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
            previous = checkpoint.get('review_link_searches', {})
            if previous.get(plan['search_id'], {}).get('discovery_version') == 'review-links/2':
                return previous[plan['search_id']]
            result = self.web.search(plan['query'], check_cancel=lambda: self.broker.check(cap))
            urls = self.egress.register_search_results(cap.actor, cap.run_id, plan['search_id'], result)
            capabilities = self.collection.call('capabilities')
            items = candidates(result, urls, plan['brand'], plan['model'], capabilities.get('auto_collection_channels', []))
            discovery = None
            if not any(item['auto_collectable'] for item in items):
                discovery = self.collection.call('discover', {key: plan[key] for key in ('brand', 'model', 'channel')})
                discovered_urls = self.egress.register_collection_discovery(cap.actor, cap.run_id, plan['search_id'], discovery)
                lookup = {'content': [{'type': 'text', 'text': json.dumps(discovery)}]}
                extra = candidates(lookup, discovered_urls, plan['brand'], plan['model'], capabilities.get('auto_collection_channels', []))
                provenance = {row['url']: row['discovery_source'] for row in discovery['items']}
                for item in extra:
                    item['identity_evidence'] = provenance[item['product_url']]
                items = list({item['product_url']: item for item in [*items, *extra]}.values())[:12]
            source_id = self._source(cap, {'source_type': 'web', 'title': plan['brand'] + ' ' + plan['model'] + ' 网评链接搜索',
                'content': {'web_search': result.get('content'), 'collection_discovery': discovery},
                'provenance': {'provider': 'aliyun-websearch-and-collection-discovery', 'query': plan['query'], 'retrieved_at': utc_now()}})
            output = {'discovery_version': 'review-links/2', 'brand': plan['brand'], 'model': plan['model'], 'channel': plan['channel'], 'items': items,
                'source_id': source_id, 'coverage': discovery['coverage'] if discovery else 'returned_search_results_only',
                'warnings': discovery.get('warnings', []) if discovery else [],
                'instructions': '先核对resolve_product；型号冲突或needs_verification不可直接采集。同一渠道选择一个精确型号商品，最多关联三个采集产品。没有搜索结果不代表渠道没有评论；不支持自动采集的渠道明确列出。采集完成后按job_id读取数据并生成或复用洞察报告。'}
            links = dict(checkpoint.get('review_link_candidates', {}))
            for item in items:
                links[item['product_url']] = {**item, 'requested_brand': plan['brand'], 'requested_model': plan['model'], 'source_id': source_id}
            self._checkpoint(cap, review_link_candidates=links, review_link_searches={**previous, plan['search_id']: output})
            return output
        if name in {"search_web", "read_web_page"}:
            cancel = {"check_cancel": lambda: self.broker.check(cap)} if isinstance(self.web, ResearchWeb) else {}
            plan = self.egress.plan_search(cap.actor, cap.run_id, args["query"]) if name == "search_web" else self.egress.resolve_url(cap.actor, cap.run_id, args["url"])
            if not plan["approved"]:
                return plan
            result = self.web.search(plan["query"], **cancel) if name == "search_web" else self.web.fetch(plan["url"], **cancel)
            urls = []
            if name == "search_web":
                urls = self.egress.register_search_results(cap.actor, cap.run_id, plan["search_id"], result)
            sid = self._source(cap, {"source_type": "web", "title": result.get("title") or "公开网页搜索", "content": result.get("text") or result.get("content"), "url": result.get("url"), "provenance": {"provider": "aliyun-websearch" if name == "search_web" else "public-web", "retrieved_at": utc_now(), "result_urls": urls}})
            return {**result, "source_id": sid, "registered_urls": urls}
        if name == "collection_capabilities":
            return self.collection.call("capabilities")
        if name == "resolve_product":
            plan = self.egress.resolve_collection_url(cap.actor, cap.run_id, args["product_url"])
            if not plan["approved"]:
                return plan
            result = self.collection.call("resolve", {"product_url": plan["url"]})
            checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
            links = dict(checkpoint.get('review_link_candidates', {}))
            if plan['url'] in links:
                item = dict(links[plan['url']])
                product = result.get('product', {})
                actual = str(product.get('model') or '').strip().casefold()
                if actual:
                    actual_brand = str(product.get('brand') or '').strip().casefold()
                    item['identity_status'] = 'resolved_match' if actual == item['requested_model'].casefold() and actual_brand == item['requested_brand'].casefold() else 'model_conflict'
                item['resolved'] = True
                links[plan['url']] = item
                self._checkpoint(cap, review_link_candidates=links)
                result = {**result, 'requested_identity_status': item['identity_status']}
            return result
        if name == "collect_product":
            plan = self.egress.resolve_collection_url(cap.actor, cap.run_id, args["product_url"])
            if not plan["approved"]:
                return plan
            candidate = self.state.get(cap.actor, cap.run_id)['checkpoint'].get('review_link_candidates', {}).get(plan['url'])
            if candidate and (not candidate.get('resolved') or candidate['identity_status'] not in {'exact_search_match', 'resolved_match'}):
                raise AgentError('product_identity_unverified', '请先核实该链接对应用户要求的完整型号；相近型号或冲突型号不能自动采集。')
            self._authorize_run(cap.actor, cap.run_id, "collect.command", force=True)
            if not set(cap.actor.roles) & {"user", "admin", "administrator"}:
                raise AgentError("collection_forbidden", "当前账号没有采集权限", http_status=403)
            operation_id = "wq_" + sha256_value({"run_id": cap.run_id, "channel": args["channel"], "url": public_url(args["product_url"]), "retry": args.get("retry", False)}).split(":")[1]
            checkpoint = self.state.get(cap.actor, cap.run_id)["checkpoint"]
            if checkpoint.get("collection_deadline_at", time.time() + 1) <= time.time():
                raise AgentError("collection_wait_expired", "补充采集等待已超过24小时，请用现有资料回答能够确认的部分")
            operations = dict(checkpoint.get("operations", {}))
            if operation_id not in operations and len(operations) >= 3:
                raise AgentError("collection_budget_reached", "本次研究已关联三个采集产品，请先使用已有资料")
            payload = {**args, "request_key": operation_id, "research_run_id": cap.run_id, "product_url": public_url(args["product_url"])}
            if args.get("retry"):
                prior = next((v for v in operations.values() if v.get("channel") == args["channel"] and v.get("product_url") == args["product_url"]), None)
                if not prior:
                    raise AgentError("retry_requires_attempt", "先读取已有采集结果，再重试对应采集")
                payload["expected_attempt_no"] = prior["result"].get("attempt_no")
            result = self.collection.call("ensure", payload)
            try:
                self.broker.check(cap)
                operations[operation_id] = {"channel": args["channel"], "product_url": args["product_url"], "result": result}
                self._checkpoint(cap, operations=operations, collection_deadline_at=checkpoint.get("collection_deadline_at", time.time() + 86400))
            except AgentError:
                # The shared task can remain useful to others. If cancellation
                # arrived during creation, remove only this run's subscription.
                self.collection.call("unsubscribe", {"request_key": operation_id, "research_run_id": cap.run_id})
                raise
            return {"operation_id": operation_id, **self._public_collection(result)}
        if name == "wait_for_collection":
            run = self.state.get(cap.actor, cap.run_id)
            operations = run["checkpoint"].get("operations", {})
            if not args["operation_ids"] or set(args["operation_ids"]) - set(operations):
                raise AgentError("collection_not_found", "只能等待本次研究已发起或关联的采集")
            if run["checkpoint"].get("collection_deadline_at", time.time() + 1) <= time.time():
                raise AgentError("collection_wait_expired", "补充采集等待已超过24小时，请用现有资料回答能够确认的部分")
            checkpoint = {**run["checkpoint"], "waiting_operation_ids": args["operation_ids"], "research_note": args["research_note"], "wait_started_at": time.time()}
            self.state.checkpoint(cap.actor, cap.run_id, checkpoint, status="waiting_collection", attempt_id=cap.attempt_id)
            self.broker.pause(cap)
            with self._pending_lock:
                self._pending[cap.run_id] = cap.actor
            return {"status": "waiting_collection", "message": "资料采集完成后会自动继续本次问题，无需保持页面打开。"}
        if name == 'wait_for_report_data':
            run = self.state.get(cap.actor, cap.run_id)
            if not args['job_ids'] or set(args['job_ids']) - set(run['checkpoint'].get('read_job_ids', [])):
                raise AgentError('report_source_not_read', '请先读取本次指定采集任务的原评数据')
            self.state.checkpoint(cap.actor, cap.run_id, {**run['checkpoint'],
                'report_scope_wait': {'job_ids': args['job_ids']}, 'report_scope_deadline_at': time.time() + 3600,
                'research_note': args['research_note'], 'wait_started_at': time.time()}, status='waiting_data', attempt_id=cap.attempt_id)
            self.broker.pause(cap)
            with self._pending_lock:
                self._pending[cap.run_id] = cap.actor
            return {'status': 'waiting_data', 'message': '评论已采集并读取，正在等待洞察数据版本自动更新；完成后会继续生成报告。'}
        if name in {"report_candidates", "create_report"}:
            transport = self.service.executor.transport if self.service.executor else None
            if not transport:
                raise AgentError("reports_unavailable", "报告服务尚未连接", http_status=503)
            scope = args.get('scope', {})
            bindings = self.state.get(cap.actor, cap.run_id)['checkpoint'].get('report_candidate_scopes', {})
            binding = bindings.get(args.get('candidate_id'))
            if name == 'create_report' and binding:
                if 'scope' in args and args['scope'] not in (binding['query_scope'], binding['candidate_scope']):
                    raise AgentError('report_scope_changed', '报告范围与已读取候选不一致，请重新查候选。', http_status=409)
                scope = binding['query_scope']
            payload = {"scope": self._report_workflow_scope(cap, scope, creating=name == 'create_report')}
            if name == "create_report":
                payload.update(candidate_id=args["candidate_id"], date_range=args.get("date_range", {}))
            result = transport.report_workflow("catalog" if name == "report_candidates" else "ensure", payload, cap.actor)
            if name == 'report_candidates':
                with cap.checkpoint_lock:
                    bindings = dict(self.state.get(cap.actor, cap.run_id)['checkpoint'].get('report_candidate_scopes', {}))
                    for item in result.get('items', []):
                        bindings[item['candidate_id']] = {'query_scope': payload['scope'], 'candidate_scope': item['scope']}
                    self._checkpoint(cap, report_candidate_scopes=dict(list(bindings.items())[-256:]))
                result = {**result, 'items': [{**item, 'create_arguments': {'candidate_id': item['candidate_id']}}
                                             for item in result.get('items', [])]}
            if name == "create_report" and result.get("job", {}).get("status") != "completed":
                run = self.state.get(cap.actor, cap.run_id)
                self.state.checkpoint(cap.actor, cap.run_id, {**run["checkpoint"], "report_job": result, "wait_started_at": time.time()}, status="waiting_data", attempt_id=cap.attempt_id)
                self.broker.pause(cap)
                with self._pending_lock:
                    self._pending[cap.run_id] = cap.actor
            return {k: v for k, v in result.items() if k != "job_token"}
        if name in {"run_python", "run_shell", "read_file", "write_file"}:
            result = self.manager.tool(cap.attempt_id, name, args)
            if name in {"run_python", "run_shell"}:
                source_id = self._source(cap, {"source_type": "artifact", "title": "分析过程输出", "content": result, "citation_eligible": False,
                    "provenance": {"tool": name, "code_sha256": sha256_value(args), "attempt_id": cap.attempt_id}})
                self._record_data_goal(cap, name, args, result, source_id)
                return {**result, "analysis_result_id": source_id, "citation_eligible": False,
                    "instruction": "代码输出不是原始评论来源，尤其是review_text[:600]等预览。引用评论须使用查询结果每条评论的source_id，并读取该评论完整正文；自定义计算请保存完整方法和结果工件，并引用输入数据。"}
            return result
        if name == "publish_artifact":
            return self.publish_artifact(cap, args)
        if name == "publish_report":
            return self.publish_report(cap, args)
        if name == "submit_answer":
            return self.submit_answer(cap, args)
        raise AgentError("tool_not_found", "没有此研究工具", http_status=404)

    @staticmethod
    def _public_collection(result):
        return {key: value for key, value in result.items() if key not in {"selected_run", "data", "artifacts", "job_token"}} | {"data": {key: value for key, value in (result.get("data") or {}).items() if key not in {"selected_run", "artifacts"}}}

    def _report_workflow_scope(self, cap, scope, *, creating=False):
        """Keep discovered-model reports on verified product identities.

        A channel's task can contain a family review pool. Its model label
        alone does not prove that every row belongs to the requested SKU.
        """
        checkpoint = self.state.get(cap.actor, cap.run_id)['checkpoint']
        if not checkpoint.get('review_link_searches'):
            return scope
        ids = scope.get('job_ids', [])
        if not ids:
            if creating:
                raise AgentError('report_scope_unverified', '按型号生成报告前，请先读取精确任务数据，并在scope.job_ids中使用这些任务。')
            return scope
        products = {}
        for row in self.state.list_sources(cap.actor, cap.run_id):
            source = row['source']
            if source.get('source_type') != 'dataset' or source.get('provenance', {}).get('operation') != 'open':
                continue
            for item in source.get('content', {}).get('inputs', []):
                job = item.get('job') or {}
                if job.get('id') in ids and job.get('source_product_id') and (job.get('source_product_identity') or {}).get('status') == 'resolved':
                    products.setdefault(job['id'], set()).add(str(job['source_product_id']))
        if set(ids) != products.keys() or any(len(values) != 1 for values in products.values()):
            raise AgentError('report_scope_unverified', '请先读取所选任务并核实商品身份，再生成精确型号报告；不能把商品评论池当作该型号样本。')
        expected = sorted({value for values in products.values() for value in values})
        if scope.get('source_product_ids') and sorted(set(scope['source_product_ids'])) != expected:
            raise AgentError('report_scope_mismatch', '报告商品范围与已核实任务不一致，请沿用准备产品研究返回的精确范围。')
        return {**scope, 'source_product_ids': expected}

    @staticmethod
    def _execution_preview(source):
        # Compatibility includes previously saved previews without the new flag.
        # Executed code can slice, paraphrase or invent text; its stdout is not
        # an immutable review. Explicitly published results remain inspectable.
        return source.get("citation_eligible") is False or (
            source.get("source_type") == "artifact" and source.get("provenance", {}).get("tool") in {"run_python", "run_shell"})

    def _resolve_review_citations(self, actor, run_id, answer):
        references = [m for m in markers(answer) if m.kind == 'review']
        matches = [re.fullmatch(r'review:(ds_[0-9a-f]{64}):([^\]\r\n]{1,200})', m.value) for m in references]
        if any(match is None for match in matches):
            raise AgentError('citation_unknown', '原评引用格式无效，请使用导出清单的citation_prefix与实际record_id。')
        handles = list(dict.fromkeys(match.groups() for match in matches))
        if not handles:
            return answer
        if len(handles) > 100:
            raise AgentError('citation_limit', '一份正文最多引用100条独立原评；完整清单可保存为分析文件。')
        run = self.state.get(actor, run_id)
        manifests = {}
        # A code-created file or a catalog is never authority for a citation.
        for row in self.state.list_sources(actor, run_id):
            source = row['source']
            if source.get('source_type') == 'dataset' and source.get('provenance', {}).get('operation') == 'manifest':
                exported = source.get('content') or {}
                manifest = exported.get('manifest') or {}
                digest = manifest.get('manifest_sha256')
                if (manifest.get('contract') == 'voxquarry.wenquan-dataset/v1'
                        and manifest.get('export_complete') is True
                        and re.fullmatch(r'sha256:[0-9a-f]{64}', str(digest))
                        and digest == source['provenance'].get('manifest_sha256')):
                    manifests[source['dataset_id']] = digest
        replacements = {}
        for dataset in dict.fromkeys(dataset for dataset, _ in handles):
            if dataset not in manifests or self.data is None:
                raise AgentError('citation_unknown', '原评引用的数据集尚未通过本轮导出清单核验，请导出该冻结数据或用query_dataset读取引用原文。')
            wanted = [record for ds, record in handles if ds == dataset]
            args = {'dataset_id': dataset, 'review_ids': wanted, 'population': 'occurrences', 'limit': 100}
            rows, cursors = {}, set()
            for _ in range(len(wanted) + 1):
                result = self.data.call(actor, 'query', args)
                if result.get('manifest_sha256') != manifests[dataset]:
                    break
                rows.update({row.get('record_id'): row for row in result.get('items', [])})
                cursor = result.get('next_cursor')
                if not cursor or cursor in cursors or set(rows) == set(wanted):
                    break
                cursors.add(cursor)
                args = {**args, 'cursor': cursor}
            if result.get('manifest_sha256') != manifests[dataset] or set(rows) != set(wanted):
                raise AgentError('citation_unknown', '原评引用与已导出的冻结版本不一致，或记录未找到；请核对实际record_id。')
            for record in wanted:
                row = rows[record]
                source = self.state.add_source(actor, run_id, {'source_type': 'review',
                    'title': row.get('review_title') or '原始评论', 'content': row, 'dataset_id': dataset, 'locator': record,
                    'provenance': {'dataset_id': dataset, 'manifest_sha256': manifests[dataset]}}, attempt_id=run['attempt_id'])
                replacements['review:' + dataset + ':' + record] = '[' + source['source_id'] + ']'
        return replace_markers(answer, 'review', replacements)

    def validate_answer(self, actor, run_id, answer):
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 60000:
            raise AgentError("answer_invalid", "请提交完整回答正文")
        if any(marker.kind == 'number' for marker in markers(answer)):
            raise AgentError('citation_unknown', '正文数字序号尚未绑定来源。请使用本会话已保存的完整[src_编号]，'
                             '不要直接复制前轮的[1]等显示序号；可用read_saved_source核对原文。')
        answer = self._resolve_review_citations(actor, run_id, answer)
        session_id = self.state.get(actor, run_id)["session_id"]
        sources = {item["source_id"]: item for item in self.state.list_sources(actor, run_id)
                   if (item['owner_user_id'], item['scope_id'], item['session_id'], item['run_id'])
                   == (actor.owner_user_id, actor.scope_id, session_id, run_id)}
        review_sources = list(sources.values())
        wanted = entity_ids(answer)
        if wanted:
            try:
                review_sources = self.state._review_citation_sources(actor, session_id, run_id, wanted, utc_now())
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                raise AgentError('citation_unknown', '原评来源暂时无法完整读取，不能交付未核验引用。请保留分析并核对已保存原文。') from None
        answer, _ = resolve_entities(answer, review_sources, run_id=run_id)
        conflicts = percentage_relation_conflicts(answer)
        if conflicts:
            raise AgentError("answer_numeric_relation_conflict", "正文中的百分比大小关系不成立：" + "；".join(conflicts[:4])
                + "。核对数值或符号；若表示改善优先级，请用文字说明依据，不把优先级写成百分比大小关系。只修正相关正文与报告，复用已有资料和计算。")
        # Native web tools use Markdown URL citations. Resolve only URLs that
        # were actually read/returned in this owned run, never model-made URLs.
        web_sources = {}
        for row in sources.values():
            source = row['source']
            if source.get('source_type') == 'web':
                for url in source.get('provenance', {}).get('result_urls', []):
                    web_sources.setdefault(url, row['source_id'])
                if source.get('url'):
                    web_sources[source['url']] = row['source_id']
        def web_citation(match):
            sid = web_sources.get(match[1])
            return match[0] + ('[' + sid + ']' if sid and '[' + sid + ']' not in answer else '')
        answer = re.sub(r'\[[^\]\r\n]+\]\((https?://[^\s)]+)\)', web_citation, answer)
        cited = list(dict.fromkeys(m.value for m in markers(answer) if m.kind == 'source'))
        # Native follow-ups keep prior source ids. Ownership remains checked by
        # the source store against the same user and application conversation.
        for sid in cited:
            if sid not in sources:
                try:
                    sources[sid] = self.state.source(actor, session_id, sid)
                except AgentError as error:
                    if error.code != "source_not_found":
                        raise
        unknown = [sid for sid in cited if sid not in sources]
        if unknown:
            examples = ", ".join(sid[:100] for sid in unknown[:8])
            raise AgentError("citation_unknown", "正文有" + str(len(unknown)) + "个未读取的来源编号：" + examples
                + "。保留已写好的正确内容，只修正这些引用；从查询结果提取对应评论的完整source_id，不要自己拼接编号。")
        previews = [sid for sid in cited if self._execution_preview(sources[sid]["source"])]
        if previews:
            raise AgentError("citation_requires_original_source", "这些编号只是代码输出，不能作为评论原文来源：" + ", ".join(previews) + "。保留已写好的内容，用query_dataset/query_report_reviews按review_ids取得完整原文和source_id，仅修正相应引用。自定义计算可publish_artifact保存完整方法和结果，并引用输入数据。")
        invalid_reviews = [sid for sid in cited if sources[sid]['source'].get('source_type') == 'review'
                           and review_source_error(sources[sid])]
        if invalid_reviews:
            raise AgentError('citation_unknown', '原评正文或保存版本无法核验：' + ', '.join(invalid_reviews[:8])
                             + '。请重新读取对应原文的完整source_id。')
        if sources and not cited:
            raise AgentError("citation_missing", "请在重要结论后引用已读取的source_id，便于用户核对原文")
        citations = []
        replacements = {}
        for number, source_id in enumerate(cited, 1):
            row = sources[source_id]
            citations.append(source_citation(row, number))
            replacements[source_id] = '[' + str(number) + ']'
        answer = replace_markers(answer, 'source', replacements)
        return answer.strip(), citations

    def _verify_collection_goal(self, actor, run_id):
        run = self.state.get(actor, run_id)
        request, checkpoint = run['request'], run['checkpoint']
        discovery = checkpoint.get('review_link_candidates', {})
        discovery_goal = bool(checkpoint.get('review_link_searches')) and bool(re.search(r'采集|抓取|collect\b|scrape\b', request['question'], re.I))
        if not discovery_goal and not re.search(r'^\s*(?:请|帮我|重新|重跑|继续)?\s*(?:采集|抓取|collect\b|scrape\b)', request['question'], re.I):
            return
        targets = set(request.get('product_urls', []))
        if discovery_goal:
            chosen = {}
            operations = {op.get('product_url') for op in checkpoint.get('operations', {}).values()}
            for url, item in sorted(discovery.items(), key=lambda pair: pair[0] not in operations):
                if item.get('auto_collectable') and item.get('identity_status') in {'exact_search_match', 'resolved_match'}:
                    chosen.setdefault(item['channel'], url)
            targets.update(chosen.values())
            if not targets:
                raise AgentError('collection_goal_incomplete', '尚未核实并采集目标型号的网评链接，本次任务未完成。', http_status=409)
        if not targets:
            return
        outcomes = {item.get('request_key') or key: item for key, item in zip(checkpoint.get('waiting_operation_ids', []), checkpoint.get('collection_outcomes', []))}
        ready = set()
        for key, op in checkpoint.get('operations', {}).items():
            result = outcomes.get(key, op.get('result', {}))
            job_id = result.get('job_id') or result.get('data', {}).get('job_id')
            if result.get('data', {}).get('ready') and job_id in checkpoint.get('read_job_ids', []):
                ready.add(public_url(op['product_url']))
        if not re.search(r'重新采集|重新抓取|最新采集|recollect\b|rescrape\b', request['question'], re.I):
            # Reusing already collected data is a valid user-requested outcome.
            # Only a trusted, opened immutable manifest can establish it; a
            # catalog listing, model claim or unrelated report cannot.
            for row in self.state.list_sources(actor, run_id):
                source = row['source']
                provenance = source.get('provenance') or {}
                manifest = source.get('content') or {}
                if (source.get('source_type') != 'dataset' or provenance.get('operation') != 'open'
                        or not isinstance(manifest, dict) or manifest.get('contract') != 'voxquarry.wenquan-dataset/v1'
                        or manifest.get('export_complete') is not True
                        or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(manifest.get('manifest_sha256', '')))
                        or provenance.get('manifest_sha256') != manifest.get('manifest_sha256')):
                    continue
                for item in manifest.get('inputs', []):
                    job = item.get('job') or {}
                    if (item.get('status') == 'completed' and job.get('id') in checkpoint.get('read_job_ids', [])
                            and job.get('target_url') and job.get('source_product_id')
                            and job['source_product_id'] in manifest.get('scope', {}).get('source_product_ids', [])):
                        ready.add(public_url(job['target_url']))
        if targets - ready:
            raise AgentError('collection_goal_incomplete', '目标商品的采集或原评读取尚未完成，本次任务受阻；其他型号资料不能替代目标结果。', http_status=409)

    def _with_report_links(self, actor, run_id, answer):
        """Deliver browser routes from reports actually read by this owner.

        An internal report URI in model prose is not a usable browser link.
        Do not trust arbitrary IDs in that prose or expose unread reports.
        """
        if not re.search(r'报告|report\b', self.state.get(actor, run_id)['request']['question'], re.I):
            return answer
        ids = list(dict.fromkeys(row['source'].get('report_id') for row in self.state.list_sources(actor, run_id)
            if row['source'].get('source_type') == 'report'
            and re.fullmatch(r'rpt_[0-9a-f]{24}', str(row['source'].get('report_id', '')))))
        links = [f'[打开洞察报告 {index}]({report_href(rid)})' for index, rid in enumerate(ids[:3], 1)
                 if report_href(rid) not in answer]
        return answer + ('\n\n' + ' · '.join(links) if links else '')

    def _last_model_failure(self, actor, trace_id, attempt_id):
        """Explain only the last failed model call of this attempt.

        Read safe structured events, never provider bodies or payload files.
        A later completed or still-running request makes an older error
        unsuitable as an explanation for the final Harness failure.
        """
        messages = {
            "model_access_denied": ("所选模型尚未开通调用权限，请开通权限或换用其他模型。", False),
            "model_auth_failed": ("模型服务认证失败，请更新 API Key 后重试。", False),
            "model_not_found": ("模型服务找不到所选模型，请检查模型名称和服务地址。", False),
            "model_rate_limited": ("模型调用已达到服务限制，请稍后重试。", True),
            "model_connection_failed": ("模型服务暂时无法连接，已保留资料，请稍后继续分析。", True),
            "model_stream_interrupted": ("模型连接中断，已保留资料，请稍后继续分析。", True),
            "model_unavailable": ("模型服务暂时未能完成回复，已保留资料，请稍后继续分析。", True),
            "model_response_invalid": ("模型返回内容无法读取，已保留资料，请稍后继续分析。", True),
            "model_output_limit": ("模型回复超出处理容量，已保留资料，可以继续分析。", True),
        }
        if not trace_id:
            return None

        try:
            end = self.traces.get_trace(actor, trace_id)["last_seq"]
            target_call, candidate, seen_error = None, None, False
            while end:
                start = max(0, end - 1000)
                events = self.traces.list_events(actor, trace_id, after_seq=start, limit=1000)["items"]
                for event in reversed(events):
                    if event["seq"] > end or event.get("attempt_id") != attempt_id:
                        continue
                    kind, data = event["event_type"], event.get("data") or {}
                    call = data.get("call")
                    if target_call is None:
                        if kind == "model.started":
                            return None
                        if kind != "model.finished":
                            continue
                        if data.get("complete") is not False or type(call) is not int or call < 1:
                            return None
                        target_call = call
                        continue
                    if kind in {"model.started", "model.finished"} and type(call) is int and call > target_call:
                        return None
                    if kind == "error.model" and type(call) is int and call == target_call and not seen_error:
                        seen_error = True
                        code = data.get("code")
                        if isinstance(code, str) and code in messages:
                            message, retryable = messages[code]
                            retryable = data.get("retryable") if type(data.get("retryable")) is bool else retryable
                            status = data.get("http_status", 502)
                            status = status if type(status) is int and 400 <= status <= 599 else 502
                            if code == "model_unavailable" and not retryable:
                                message = "模型服务拒绝了本次请求，请检查模型参数或换用其他模型；重复继续不会修复该配置问题。"
                            candidate = AgentError(code, message, http_status=status, retryable=retryable)
                    if kind == "model.started" and type(call) is int and call == target_call:
                        return candidate
                end = start
            return candidate
        except AgentError:
            # Missing/expired diagnostics should keep the generic failure,
            # never replace it with an unrelated log-storage error.
            return None

    def _exhausted_budget(self, actor, trace_id, attempt_id):
        if not trace_id:
            return None
        end = self.traces.get_trace(actor, trace_id)["last_seq"]
        events = self.traces.list_events(actor, trace_id, after_seq=max(0, end - 1000), limit=1000)["items"]
        for event in reversed(events):
            data = event.get("data") or {}
            if event.get("attempt_id") == attempt_id and event["event_type"] == "budget.exhausted" and data.get("can_resume") is False:
                messages = {"research_budget_reached": "本次研究已用完模型调用额度，现有资料已保留。",
                    "research_cost_budget_reached": "本次研究的模型预算已不足，现有资料已保留。",
                    "research_active_budget_reached": "本次研究已达到当前配置的分析时长，现有资料已保留。"}
                code = data.get("code")
                if code in messages:
                    return AgentError(code, messages[code], http_status=429, retryable=False)
        return None

    def _resume_context(self, actor, run_id):
        run = self.state.get(actor, run_id)
        sources = self.state.list_sources(actor, run_id)
        checkpoint = run["checkpoint"]
        index = []
        for row in sources:
            source = row["source"]
            item = {"source_id": row["source_id"], "source_sha256": row["source_hash"],
                "source_type": source.get("source_type"), "title": source.get("title")}
            for key in ("report_id", "dataset_id", "locator"):
                if source.get(key) is not None:
                    item[key] = source[key]
            index.append(item)
        context = {"saved_research": {k: v for k, v in checkpoint.items()
            if k not in {"operations", "report_job", "answer_draft", "answer_feedback", "published_report", "prepared_product", "topic_classification", "topic_reviews", "auto_recovery"}},
            "saved_sources": index}
        if checkpoint.get("auto_recovery"):
            context["continuation_instruction"] = "上次执行暂时中断，继续完成原问题。复用原会话、文件和已读资料；若采集或报告创建的返回未知，先查已有任务状态，不要重复创建。已有发布结果可直接交付，只补尚未完成的内容。"
        feedback = checkpoint.get("answer_feedback")
        if isinstance(feedback, dict) and feedback.get("code") in ANSWER_REPAIR_CODES:
            context["answer_feedback"] = feedback
            context["continuation_instruction"] = ("上次最终正文未能保存，具体错误如下。保留原会话中已写好的内容，只修正反馈指出的百分比数值或比较符号；直接给出修正后的最终回答，不重新研究整题。"
                if feedback["code"] == "answer_numeric_relation_conflict" else
                "上次最终正文未能保存，具体引用错误如下。保留原会话中已写好的内容，只修正这些引用，使用已保存来源的完整source_id；直接给出修正后的最终回答。不重新采集、统计或研究整题。")
        if "collection_outcomes" in context["saved_research"]:
            context["saved_research"]["collection_outcomes"] = [self._public_collection(item)
                for item in context["saved_research"]["collection_outcomes"]]
        draft = checkpoint.get("topic_classification")
        if isinstance(draft, dict):
            context["saved_research"]["topic_classification"] = {"dataset_id": draft["dataset_id"],
                "total_reviews": len(draft["content"]["reviews"]),
                "checked_reviews": len((checkpoint.get("topic_reviews") or {}).get("passed", {})),
                "instruction": "这是旧流程保存的归类进度，不约束当前研究方法，也不等于已确认的结论。"}
        published = checkpoint.get("published_report")
        if isinstance(published, dict) and published.get("status") == "files_available":
            # Publication state is a pointer to this owned run's saved files,
            # never a draft answer or permission to copy a different run's work.
            by_source = {row["source_id"]: row["source"] for row in sources}
            artifacts = {item["artifact_id"]: item for item in self.store.get_run(actor, run_id)["artifacts"]}
            files = published.get("files")
            valid = isinstance(files, list) and bool(files)
            for file in files if valid else []:
                if not isinstance(file, dict):
                    valid = False
                    break
                source, artifact = by_source.get(file.get("source_id"), {}), artifacts.get(file.get("artifact_id"), {})
                if (source.get("source_type") != "artifact" or source.get("artifact_id") != file.get("artifact_id") or artifact.get("status") != "available"
                        or artifact.get("content_hash") != file.get("sha256")
                        or (source.get("provenance") or {}).get("sha256") != file.get("sha256")
                        or any(source.get(key) != file.get(key) for key in ("filename", "media_type", "download_url"))
                        or (source.get("provenance") or {}).get("bytes") != file.get("bytes")):
                    valid = False
                    break
            if valid:
                context["published_report"] = published
                context["continuation_instruction"] = (
                    "下列文件已经保存，可按当前问题复用或修正；完整内容可用read_saved_source读取。"
                    "文件存在不代表结论已经核实。")
        context['pending_deliverables'] = delivery_issues(checkpoint)
        return context

    def _finish_answer(self, cap, answer, citations, *, interruption=None):
        """One finalizer for native prose and compatibility submissions."""
        self._authorize_run(cap.actor, cap.run_id, force=True)
        run = self.state.get(cap.actor, cap.run_id)
        checkpoint = run['checkpoint']
        issues = delivery_issues(checkpoint)
        notice = delivery_notice(issues)
        if notice:
            answer = notice + '\n\n' + answer
        answer = self._with_report_links(cap.actor, cap.run_id, answer)
        budget = self.broker.budget_status(cap)
        status = 'partial' if issues or interruption else 'completed'
        metadata = {"citations": citations, "model": run["settings"]["model_id"],
            "model_calls": cap.model_calls, "usage": {"total_tokens": cap.total_tokens}, "budget": budget,
            "delivery": {"answer_saved": True, "issues": issues},
            "data_coverage": checkpoint.get("raw_data_evidence", {}), "can_resume": status == 'partial'}
        with cap.lock:
            timings = dict(cap.timings)
        metadata['performance'] = {**timings, 'attempt_wall_ms': int((time.monotonic() - cap.started_at) * 1000),
            'basis': 'current_attempt;tool_times_are_sums_and_may_overlap', 'tool_parallelism': self.broker.limits.tool_parallelism,
            'research_concurrency': self.state.capacity.researches}
        if interruption:
            metadata['error'] = {"code": interruption.code, "message": interruption.message,
                                 "retryable": interruption.retryable, "http_status": interruption.http_status}
            metadata['can_resume'] = interruption.retryable
            metadata['resume_disabled'] = not interruption.retryable
        self.state.finish(cap.actor, cap.run_id, answer, status=status, metadata=metadata, attempt_id=cap.attempt_id)
        self.broker.event(cap, "budget.finished", data=budget)
        self.traces.finish_trace(cap.actor, cap.trace_id, status=status, answer=answer, attempt_id=cap.attempt_id)

    def native_progress(self, cap, event):
        """Project public tool actions; never expose private model reasoning."""
        kind, data = event.get("type"), event.get("data", {})
        if not isinstance(data, dict):
            return
        native = {"bash", "read", "write", "edit", "glob", "grep", "read_image", "job_list", "job_output", "job_kill", "todo_write", "skill"}
        if kind == "tool/call" and data.get("name") in native:
            call_id = str(data.get("callId", ""))
            arguments = data.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            name = data["name"]
            summary = str(arguments.get("description", "")).replace("\n", " ")[:120]
            if not summary and name in {"read", "write", "edit"}:
                summary = str(arguments.get("file_path", "")).rsplit("/", 1)[-1][:100]
            with cap.lock:
                if call_id in cap.native_calls:
                    return
                cap.native_calls[call_id] = (name, summary, time.monotonic())
            status = "running"
        elif kind == "tool/result":
            message = data.get("message", {})
            call_id = str(message.get("source", {}).get("callId", ""))
            with cap.lock:
                pending = cap.native_calls.pop(call_id, None)
            if pending is None:
                return
            name, summary, started = pending
            cap.measure('native_tool_ms', (time.monotonic() - started) * 1000)
            failed = self._native_result_failed(message.get('content', []), bash=name == 'bash')
            self.broker.event(cap, 'tool.native_finished', data={'name': name, 'tool_call_id': call_id,
                'operation_success': not failed, 'duration_ms': int((time.monotonic() - started) * 1000),
                **({'origin': 'native_execution', 'code': 'native_tool_failed'} if failed else {})})
            status = "failed" if failed else "completed"
        elif kind in {"compaction/start", "compaction/end"}:
            with cap.lock:
                if kind.endswith("start"):
                    cap.native_calls["_compaction"] = "compact_" + hashlib.sha256(str(event.get("id", event.get("seq"))).encode()).hexdigest()[:32]
                event_id = cap.native_calls.get("_compaction")
            if not event_id:
                return
            self.state.event(cap.actor, cap.run_id, "整理对话上下文", "running" if kind.endswith("start") else "failed" if data.get("error") else "completed",
                event_id=event_id, detail={"summary": "保留研究进度，整理较早的对话内容"}, attempt_id=cap.attempt_id)
            return
        else:
            return
        self.state.event(cap.actor, cap.run_id, LABELS[name], status, tool_name=name,
            event_id="native_" + hashlib.sha256(call_id.encode()).hexdigest()[:32],
            detail={"summary": summary}, attempt_id=cap.attempt_id)

    @staticmethod
    def _native_result_failed(content, *, bash=False):
        # Inspect structured result nodes and the SDK's anchored exit footer;
        # arbitrary review prose containing "error" is never an error signal.
        if isinstance(content, list):
            return any(HarnessResearch._native_result_failed(item, bash=bash) for item in content)
        if not isinstance(content, dict):
            return False
        if content.get('isError') is True:
            return True
        if bash and any(type(content.get(k)) is int and content[k] != 0 for k in ('exit_code', 'exitCode')):
            return True
        text = content.get('text')
        if bash and isinstance(text, str):
            if any(int(code) != 0 for code in re.findall(r'^\[exit code: (-?\d+)\]\s*$', text, re.MULTILINE)):
                return True
        return any(HarnessResearch._native_result_failed(content[key], bash=bash)
                   for key in ('content', 'result', 'output') if key in content)

    @staticmethod
    def _context_prompt(document, context_path):
        """Keep user constraints verbatim; index older assistant prose on disk.

        This is lossless material placement, not a second summarizing model.
        The original document is installed before the harness starts.
        """
        original = json.dumps(document, ensure_ascii=False)
        history = document.get('conversation', [])
        recent = {i for i, item in enumerate(history) if item.get('role') == 'assistant'}
        recent = set(sorted(recent)[-2:])
        view = []
        for index, item in enumerate(history):
            if item.get('role') == 'assistant' and (index not in recent or len(item.get('content', '')) > 4000):
                view.append({'role': 'assistant', 'content_file': context_path, 'json_pointer': '/conversation/' + str(index),
                    'characters': len(item.get('content', '')), 'citations': item.get('citations', [])})
            else:
                view.append(item)
        prompt = {**document, 'conversation': view, 'context_document': context_path,
            'context_instruction': '原始问题、所有用户约束和完整对话保存在context_document。用户消息原样保留在当前输入；较长/较早的助手正文用JSON pointer定位。需要继承既往结论、指代或恢复压缩后的约束时，用research_methods.load_result读取对应原文，不能把索引当作已读内容。'}
        return json.dumps(prompt, ensure_ascii=False), original

    def execute(self, actor, run):
        run_id, attempt_id = run["run_id"], run["attempt_id"]
        trace_id, cap = run.get("trace_id"), None
        try:
            current = self.state.active_identity(actor, run_id)
            if current["status"] != "running" or current["attempt_id"] != attempt_id:
                return
            self._authorize_run(actor, run_id, force=True)
            self.state.claim(actor, run_id, attempt_id=attempt_id)
            if not trace_id:
                trace_id = self.traces.start_trace(actor, session_id=run["session_id"], run_id=run_id, question=run["request"]["question"], model_config=run["settings"])["trace_id"]
                self.state.attach_trace(actor, run_id, trace_id, attempt_id=attempt_id)
            else:
                self.traces.resume_trace(actor, trace_id, attempt_id=attempt_id)
            token, cap = self.broker.issue(actor, run, trace_id)
            self.egress.register_user_urls(actor, run_id)
            self.egress.register_user_hints(actor, run_id)
            active_seconds = int(self.broker.budget_status(cap)["remaining_active_seconds"])
            if active_seconds < 1:
                raise AgentError("research_active_budget_reached", "本次研究已达到当前配置的分析时长，现有资料已保留。", http_status=429)
            self.state.event(actor, run_id, "理解问题并准备资料", "running", event_id="research", attempt_id=attempt_id)
            history = self.state.project_message_citations(actor, run["session_id"],
                self.store.list_messages(actor, run["session_id"]))
            context = [{"role": item["role"],
                "content": source_marked_content(item["content"], (item.get("metadata") or {}).get("citations", [])),
                "citations": [{key: citation.get(key) for key in ("label", "source_id", "source_sha256", "title")}
                for citation in (item.get("metadata") or {}).get("citations", []) if isinstance(citation, dict)]}
                for item in history if item.get("run_id") != run_id]
            document = {"user_question": run["request"]["question"], "selected_materials": {key: run["request"].get(key, []) for key in ("report_ids", "dataset_ids", "attachment_ids", "product_urls")},
                "web_enabled": run["settings"]["web_enabled"], "conversation": context,
                "attachments": self.attachments.list(actor, run["session_id"]),
                "dataset_handle_instruction": "关联数据中job:前缀表示原始采集任务，可用open_dataset(scope={job_ids:[去掉job:的编号]})获取冻结dataset_id；ds_表示已冻结数据集。",
                **self._resume_context(actor, run_id)}
            context_path = '/inputs/research-context-' + attempt_id + '.json'
            prompt, context_document = self._context_prompt(document, context_path)
            self.broker.event(cap, 'research.context_prepared', data={'original_bytes': len(context_document.encode()),
                'prompt_bytes': len(prompt.encode()), 'history_messages': len(context),
                'history_user_messages': sum(item['role'] == 'user' for item in context)})
            self.manager.start({"attempt_id": attempt_id, "run_id": run_id, "model_id": run["settings"]["model_id"], "system_prompt": SYSTEM_PROMPT,
                "prompt": prompt, "context_document": context_document, "tools": sdk_research_tools(run["request"].get("runtime_contract", "wenquan-dsh/v1")),
                "capability": token, "deadline_seconds": active_seconds, "session_key": cap.session_key,
                "reasoning_effort": self.broker.registry.runtime(cap.settings).reasoning_effort,
                "limits": self.broker.limits.runtime(run["settings"]["model_id"])})
            cursor, deadline = 0, time.monotonic() + active_seconds + 30
            while time.monotonic() < deadline and not self._stopped.is_set():
                current = self.state.get(actor, run_id)
                if current["attempt_id"] != attempt_id or current["status"] != "running":
                    self.manager.stop(attempt_id)
                    return
                status = self.manager.status(attempt_id, after=cursor)
                if status.get("transport_recovery"):
                    self.broker.event(cap, "lifecycle.manager_recovered", data=status["transport_recovery"])
                cursor = status.get("cursor", cursor)
                for event in status.get("events", []):
                    self.broker.event(cap, "lifecycle.harness", data=event)
                if status.get("status") in {"completed", "failed", "cancelled"}:
                    if isinstance((status.get('result') or {}).get('runtime_diagnostics'), dict):
                        self.broker.event(cap, 'research.runtime_diagnostics', data=status['result']['runtime_diagnostics'])
                    current = self.state.get(actor, run_id)
                    if current["attempt_id"] != attempt_id or current["status"] != "running":
                        return
                    draft = current["checkpoint"].get("answer_draft")
                    if draft and draft.get("attempt_id") == attempt_id and status.get("status") != "cancelled":
                        answer, citations = draft["answer"], draft["citations"]
                    else:
                        # The SDK may finish its turn with residual prose after
                        # a failed stream. A known model failure takes precedence
                        # over validating that prose as an answer. Only a draft
                        # accepted by submit_answer above can survive cleanup.
                        model_error = self._exhausted_budget(actor, trace_id, attempt_id) or self._last_model_failure(actor, trace_id, attempt_id)
                        deadline_exceeded = (status.get("error") or {}).get("code") == "attempt_deadline_exceeded" or self.broker.budget_status(cap)["remaining_active_seconds"] < 1
                        result = status.get("result") or {}
                        usable_result = status.get("status") == "completed" and result.get("finish_reason") == "completed" and result.get("answer") and not model_error and not deadline_exceeded
                        # Preserve transport/SDK termination diagnostics before
                        # the sandbox is removed, without copying its payloads.
                        diagnostic = {"manager_status": status["status"], "model_calls": cap.model_calls}
                        for key, value in {
                            "code": (status.get("error") or {}).get("code"),
                            "runner_error_type": (status.get("error") or {}).get("runner_error_type"),
                            "finish_reason": (status.get("result") or {}).get("finish_reason"),
                        }.items():
                            if key == "finish_reason":
                                safe = isinstance(value, str) and value in {"completed", "error", "aborted", "max-tokens"}
                            else:
                                safe = isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value)
                            if safe:
                                diagnostic[key] = value
                        if not usable_result:
                            self.broker.event(cap, "error.harness", data=diagnostic)
                        if deadline_exceeded:
                            raise AgentError("research_active_budget_reached", "本次研究已达到当前配置的分析时长，现有资料已保留。", http_status=429)
                        if model_error is not None:
                            raise model_error
                        if not usable_result:
                            raise AgentError("harness_execution_failed", "分析没有完成，已保存读过的资料，可以继续分析。", http_status=502, retryable=True)
                        try:
                            answer, citations = self.validate_answer(actor, run_id, result["answer"])
                        except AgentError as error:
                            if error.code in ANSWER_REPAIR_CODES:
                                # The native conversation already holds the final
                                # prose. Return the same feedback a tool submission
                                # would receive, through the existing continuation.
                                self._checkpoint(cap, answer_feedback={"code": error.code, "message": error.message})
                            raise
                    self._finish_answer(cap, answer, citations)
                    return
                self._stopped.wait(3)
            if self._stopped.is_set():
                raise AgentError("research_interrupted", "研究服务正在重启，已保留进度。", http_status=503, retryable=True)
            raise AgentError("research_active_budget_reached", "本次研究已达到当前配置的分析时长，现有资料已保留。", http_status=429)
        except Exception as exc:
            error = exc if isinstance(exc, AgentError) else AgentError("research_failed", "分析遇到问题，已保留资料和日志，可以继续。", http_status=502, retryable=True)
            current = self.state.get(actor, run_id)
            if current["attempt_id"] == attempt_id and current["status"] == "running":
                draft = current["checkpoint"].get("answer_draft", {})
                if (cap and draft.get("attempt_id") == attempt_id and error.http_status not in {401, 403}
                        and error.code not in {"research_cancelled", "research_authority_changed", "research_not_running"}):
                    self._finish_answer(cap, draft['answer'], draft['citations'], interruption=error)
                    return
                repairable = error.code in ANSWER_REPAIR_CODES
                deferred = (error.retryable or repairable) and self.state.defer_recovery(actor, run_id, error.code, attempt_id=attempt_id)
                if deferred:
                    with self._pending_lock:
                        self._pending[run_id] = actor
                else:
                    disabled = (not error.retryable and not repairable) or error.http_status in {401, 403} or error.code in {"research_budget_reached", "research_cost_budget_reached", "research_active_budget_reached",
                        "research_authority_changed", "model_access_denied", "model_auth_failed", "model_not_found", "model_config_changed", "model_permission_denied"}
                    self.state.finish(actor, run_id, error.message, status="blocked", metadata={"error": {"code": error.code, "message": error.message, "retryable": error.retryable, "http_status": error.http_status}, "can_resume": not disabled, "resume_disabled": disabled, "citations": [],
                        **({"budget": self.broker.budget_status(cap)} if cap else {})}, attempt_id=attempt_id)
                if trace_id:
                    diagnostic = {"code": error.code, "type": type(exc).__name__}
                    kind = getattr(error, "transport_kind", None)
                    if kind in TRANSPORT_KINDS:
                        diagnostic["transport_kind"] = kind
                    if deferred:
                        self.traces.append_event(actor, trace_id, event_id="recovery:" + attempt_id,
                            event_type="lifecycle.recovery_scheduled", data={"error": diagnostic}, attempt_id=attempt_id)
                    else:
                        self.traces.finish_trace(actor, trace_id, status="blocked", error=diagnostic, attempt_id=attempt_id)
        finally:
            self.broker.revoke(run_id, attempt_id)
            try:
                self.manager.stop(attempt_id)
            except Exception:
                pass

    def refresh_waiting(self, actor, session_id):
        # A fresh authenticated page access revalidates ownership after a
        # service restart before any saved account snapshot is used for tools.
        bundle = self.store.session_bundle(actor, session_id)
        with self._pending_lock:
            for run in bundle["runs"]:
                if run["intent"] == "harness_research" and run["status"] in {"waiting_collection", "waiting_data"}:
                    self._pending[run["run_id"]] = actor

    def _monitor_waiting(self):
        while not self._stopped.wait(15):
            self._restore_waiting()
            with self._pending_lock:
                pending = list(self._pending.items())
            for run_id, actor in pending:
                try:
                    run = self.state.get(actor, run_id)
                    if run["status"] not in {"waiting_collection", "waiting_data"}:
                        with self._pending_lock:
                            self._pending.pop(run_id, None)
                        continue
                    if run["status"] == "waiting_collection" and run["checkpoint"].get("collection_deadline_at", time.time() + 1) <= time.time():
                        message = "补充采集等待已超过24小时。已保留现有资料，可以继续分析；共享采集任务不受影响。"
                        self.state.finish(actor, run_id, message, status="blocked", attempt_id=run["attempt_id"], allow_waiting=True,
                            metadata={"error": {"code": "collection_wait_expired", "message": message, "retryable": False}, "can_resume": True, "citations": []})
                        self.broker.revoke(run_id, run["attempt_id"])
                        with self._pending_lock:
                            self._pending.pop(run_id, None)
                        for key in run["checkpoint"].get("waiting_operation_ids", []):
                            try:
                                self.collection.call("unsubscribe", {"request_key": key, "research_run_id": run_id})
                            except AgentError:
                                pass
                        if run.get("trace_id"):
                            self.traces.finish_trace(actor, run["trace_id"], status="blocked", error={"code": "collection_wait_expired"}, attempt_id=run["attempt_id"])
                        continue
                    try:
                        self._authorize_run(actor, run_id)
                    except AgentError as error:
                        self._block_waiting_authority(actor, run, error)
                        continue
                    checkpoint = run["checkpoint"]
                    recovery = checkpoint.get("auto_recovery", {})
                    if recovery.get("pending") and recovery.get("attempt_id") == run["attempt_id"]:
                        # Wait until execute/finally has returned, then confirm
                        # cleanup. A lost DELETE response is safe to query again;
                        # no model or business tool call is replayed here.
                        with self._schedule_lock:
                            if run["attempt_id"] in self._scheduled:
                                continue
                        self.manager.stop(run["attempt_id"])
                        self.models.runtime(run["settings"])
                        resumed = self.state.resume(actor, run_id, request_id="recovery:" + run["attempt_id"], expected_attempt_id=run["attempt_id"])
                        with self._pending_lock:
                            self._pending.pop(run_id, None)
                        if resumed["created"]:
                            self._schedule(actor, resumed["run"])
                        continue
                    if run["status"] == "waiting_collection":
                        ids = checkpoint.get("waiting_operation_ids", [])
                        outcomes = [self.collection.call("status", {"request_key": key, "research_run_id": run_id}) for key in ids]
                        ready = bool(outcomes) and all(item.get("data", {}).get("ready") for item in outcomes)
                        failed = any(item.get("status") in {"completed", "failed", "cancelled", "partial", "partial_success", "unavailable"} and not item.get("data", {}).get("ready") for item in outcomes)
                    elif checkpoint.get('report_scope_wait'):
                        outcome = self.service.executor.transport.report_workflow('catalog', {'scope': checkpoint['report_scope_wait']}, actor)
                        outcomes = [outcome]
                        ready = bool(outcome.get('items'))
                        failed = time.time() >= checkpoint.get('report_scope_deadline_at', 0)
                        if failed and not ready:
                            message = '新采集评论尚未进入可生成洞察报告的数据版本，等待已超时；原评和进度已保留。'
                            self.state.finish(actor, run_id, message, status='blocked', attempt_id=run['attempt_id'], allow_waiting=True,
                                metadata={'error': {'code': 'report_data_wait_expired', 'message': message, 'retryable': False}, 'can_resume': True, 'citations': []})
                            with self._pending_lock:
                                self._pending.pop(run_id, None)
                            if run.get('trace_id'):
                                self.traces.finish_trace(actor, run['trace_id'], status='blocked', error={'code': 'report_data_wait_expired'}, attempt_id=run['attempt_id'])
                            continue
                    else:
                        job = checkpoint["report_job"]
                        outcome = self.service.executor.transport.report_workflow("status", {"job_id": job["job"]["job_id"], "job_token": job["job_token"]}, actor)
                        outcomes = [outcome]
                        ready = outcome.get("job", {}).get("status") == "completed"
                        failed = outcome.get("job", {}).get("status") in {"failed", "cancelled"}
                    if ready or failed:
                        resumed = self.state.resume(actor, run_id, request_id="auto:" + run["attempt_id"], expected_attempt_id=run["attempt_id"])
                        current = resumed["run"]
                        updates = {'collection_outcomes': [self._public_collection(item) for item in outcomes], 'collection_completed': ready} if run['status'] == 'waiting_collection' else {'report_outcomes': outcomes, 'report_scope_wait': None}
                        self.state.checkpoint(actor, run_id, {**checkpoint, **updates,
                            "continuation_instruction": "新资料已准备好，请直接读取并继续原问题。" if ready else "补充资料未采集成功。用已获得的资料回答能确认的部分，不要自动无限重试。"}, status="running", attempt_id=current["attempt_id"])
                        with self._pending_lock:
                            self._pending.pop(run_id, None)
                        if resumed["created"]:
                            self._schedule(actor, self.state.get(actor, run_id))
                except AgentError as error:
                    # Temporary collector errors do not consume another model
                    # turn or discard the durable subscription.
                    if error.code in {"model_config_changed", "model_permission_denied", "model_budget_unavailable"}:
                        self.state.finish(actor, run_id, error.message, status="blocked", attempt_id=run["attempt_id"], allow_waiting=True,
                            metadata={"error": {"code": error.code, "message": error.message, "retryable": False}, "resume_disabled": True, "can_resume": False})
                        with self._pending_lock:
                            self._pending.pop(run_id, None)
                    continue

    def source(self, actor, session_id, source_id, offset=None):
        row = self.state.source(actor, session_id, source_id)
        source = dict(row["source"])
        result = {"source_id": source_id, "source_type": source.get("source_type"), "title": source.get("title"), "content": source.get("content"), "locator": source.get("locator"), "provenance": source.get("provenance"), "page": source.get("page"), "source_sha256": row["source_hash"]}
        result.update({key: source[key] for key in ("filename", "media_type", "download_url", "artifact_id", "dataset_id") if key in source})
        if source.get("report_id"):
            result["report_href"] = report_href(source["report_id"])
            provenance = source.get("provenance") or {}
            result.update(report=provenance.get("report"), snapshot=provenance.get("snapshot"))
            if source.get("source_type") == "review":
                result["review"] = source["content"]
            elif source.get("source_type") == "report":
                result["data"] = source["content"]
                if offset is not None:
                    if provenance.get("tool") != "get_report_section" or offset < 0:
                        raise AgentError("invalid_request", "这条来源不支持此分页")
                    payload = {**provenance["arguments"], "offset": offset}
                    fresh = self.service.executor.invoke(actor, tool_name="zhuojian.get_report_section@v1", payload=payload, session_id=session_id).result
                    if sha256_value(fresh.get("report")) != sha256_value(provenance.get("report")) or sha256_value(fresh.get("snapshot")) != sha256_value(provenance.get("snapshot")):
                        raise AgentError("source_changed", "报告版本发生变化，已保留原引用内容", http_status=409)
                    result.update(data=fresh.get("data"), page=fresh.get("page"))
                page = result.get("page") or {}
                if page.get("has_more"):
                    following = page.get("next_offset", page.get("offset", 0) + page.get("returned", page.get("limit", 100)))
                    result["next_url"] = f"/wenquan/api/v1/sessions/{quote(session_id, safe='')}/sources/{source_id}/content?offset={following}"
        elif source.get("source_type") == "review":
            result["review"] = source["content"]
        if source.get("source_type") == "dataset":
            content = source.get("content") or {}
            total = content.get("total") if isinstance(content, dict) else None
            result["dataset_summary"] = {"description": f"本次分析使用的原始评论，共 {total:,} 条。" if type(total) is int else "本次分析使用的原始评论；展开后可逐条阅读。"}
            if offset is not None:
                if type(offset) is not int or offset < 0 or not self.data:
                    raise AgentError("invalid_request", "评论页码无效")
                provenance = source.get("provenance") or {}
                selectors = {k: v for k, v in provenance.get("arguments", {}).items() if k in {"filters", "query", "review_ids", "population"}}
                fresh = self.data.call(actor, "query", {**selectors, "dataset_id": source["dataset_id"], "offset": offset, "limit": 20})
                if fresh.get("dataset_id") != source["dataset_id"] or not provenance.get("manifest_sha256") or fresh.get("manifest_sha256") != provenance["manifest_sha256"]:
                    raise AgentError("source_changed", "数据版本发生变化，请重新打开来源", http_status=409)
                items = fresh.get("items", [])
                page = fresh.get("page") or {}
                result.update(dataset_reviews=items, page={**page, "next_offset": offset + len(items)})
        if source.get("url"):
            result["url"] = source["url"]
        if source.get("source_type") == "upload":
            attachment_id = source["attachment_id"]
            value = ({"text": source["content"], "locators": []} if (source.get("provenance") or {}).get("image_reader")
                     else self.attachments.read_text(actor, session_id, attachment_id, offset=offset or 0))
            result.update(content=value["text"], locator=value.get("locators"))
            result["full_url"] = f"/wenquan/api/v1/sessions/{quote(session_id, safe='')}/attachments/{attachment_id}/download"
            if value.get("next_offset") is not None:
                result["next_url"] = f"/wenquan/api/v1/sessions/{quote(session_id, safe='')}/sources/{source_id}/content?offset={value['next_offset']}"
        return result

    def rank_review_topics(self, cap, args):
        dataset_id = args["dataset_id"]
        checkpoint = self.state.get(cap.actor, cap.run_id)["checkpoint"]
        if dataset_id not in checkpoint.get("exported_datasets", []):
            raise AgentError("dataset_not_exported", "请先export_dataset读取目标完整评论，再提交逐条归类。")
        draft = checkpoint.get("topic_classification") or {}
        if args.get("path"):
            try:
                value = json.loads(self.manager.read_output(cap.attempt_id, args["path"]))
            except (ValueError, UnicodeError):
                raise AgentError("review_classification_invalid", "归类文件必须是有效JSON。") from None
        elif draft.get("dataset_id") == dataset_id:
            value = draft["content"]
        else:
            raise AgentError("review_classification_invalid", "首次核对请提供归类文件path；后续可以省略path继续保存的归类。")
        manifest = self.data.call(cap.actor, "manifest", {"dataset_id": dataset_id})
        if manifest.get("dataset_id") != dataset_id or not manifest.get("manifest_sha256") or not manifest.get("shards"):
            raise AgentError("dataset_changed", "目标数据版本无法确认。")
        if not args.get("path") and draft.get("manifest_sha256") != manifest["manifest_sha256"]:
            raise AgentError("dataset_changed", "保存的归类与当前数据版本不一致，请重新核对。")
        originals = {}
        for shard in manifest["shards"]:
            self.broker.check(cap)
            raw = self.data.export(cap.actor, dataset_id, shard.get("path") or shard.get("name"), source_type="normalized_review")
            if shard.get("sha256") != "sha256:" + hashlib.sha256(raw).hexdigest():
                raise AgentError("dataset_changed", "评论分片与数据版本不一致。")
            for line in raw.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                key = row.get("record_id")
                if not isinstance(key, str) or not key or key in originals:
                    raise AgentError("dataset_identity_invalid", "原始评论缺少唯一ID，不能计算独立评论数。")
                originals[key] = self._review_reading_view(row)
        topics, rows = review_topic_counts.prepare(value, originals)
        # Persist the actual draft, not only audit hashes. A new sandbox after
        # interruption can continue without reconstructing hundreds of labels.
        self._checkpoint(cap, topic_classification={"dataset_id": dataset_id,
                         "manifest_sha256": manifest["manifest_sha256"], "content": value})
        draft_bytes = json.dumps(value, ensure_ascii=False).encode()
        draft_filename = "review-topics-" + hashlib.sha256(draft_bytes).hexdigest()[:24] + ".json"
        self.manager.put_input(cap.attempt_id, draft_filename, draft_bytes)
        catalog_hash = sha256_value({"topics": topics, "manifest": manifest["manifest_sha256"]})
        cache = checkpoint.get("topic_reviews") or {}
        passed = dict(cache.get("passed", {})) if cache.get("catalog_sha256") == catalog_hash else {}
        passed = {row["record_id"]: passed[row["record_id"]] for row in rows
                  if passed.get(row["record_id"]) == sha256_value(row)}
        pending = [row for row in rows if passed.get(row["record_id"]) != sha256_value(row)]
        revisions = []
        issues = list(cache.get("topic_issues", [])) if cache.get("catalog_sha256") == catalog_hash else []
        # Full originals, including unassigned reviews, go to an independent
        # input. Cache only exact passed rows so local repairs retain progress.
        # One model review per tool call fits the existing model/tool transport
        # deadlines. Further calls continue from the persisted exact-row cache.
        for batch in ([pending[:50]] if pending else []):
            incoming = {"stream": False, "messages": [{"role": "system", "content": review_topic_counts.AUDIT},
                {"role": "user", "content": json.dumps({"topics": topics, "reviews": batch}, ensure_ascii=False)}]}
            raw = b"".join(self.broker.model_stream(cap, incoming, []))
            decisions, topic_issues = review_topic_counts.parse_review(raw, batch, topics)
            hashes = {row["record_id"]: sha256_value(row) for row in batch}
            for row in decisions:
                if row["supported"]:
                    passed[row["record_id"]] = hashes[row["record_id"]]
                else:
                    revisions.append({"record_id": row["record_id"], "feedback": row["feedback"]})
            issues.extend(topic_issues)
            self._checkpoint(cap, topic_reviews={"catalog_sha256": catalog_hash, "passed": passed,
                                                "topic_issues": issues})
            self.broker.event(cap, "lifecycle.review_topics_checked", data={"checked": len(passed),
                "total": len(rows), "revision": len(revisions), "topic_issues": len(issues)})
        if revisions or issues:
            return {"status": "needs_revision", "checked_reviews": len(passed), "total_reviews": len(rows),
                    "classification_file": "/inputs/" + draft_filename,
                    "topic_issues": issues, "needs_revision": revisions,
                    "instruction": "请从classification_file读取已保存归类，按反馈修正后写到/workspace，再用path提交；已通过且未改动的评论不重复核对。也可先交付有证据的定性发现，不声称全量排名。"}
        if len(passed) < len(rows):
            return {"status": "reviewing", "checked_reviews": len(passed), "total_reviews": len(rows),
                    "instruction": "本批归类和核对进度已保存。继续用相同dataset_id调用rank_review_topics，可省略path处理下一批；即使换了执行环境也能继续。全部完成后返回可引用的统计结果。"}
        result = review_topic_counts.statistics(topics, rows, dataset_id, manifest["manifest_sha256"], len(originals))
        source_id = self._source(cap, {"source_type": "analysis", "title": "优缺点逐条归类与人数统计",
            "content": result, "dataset_id": dataset_id,
            "provenance": {"operation": "rank_review_topics", "dataset_id": dataset_id,
                           "manifest_sha256": manifest["manifest_sha256"], "classification_sha256": result["classification_sha256"]}})
        return {"status": "ready", "source_id": source_id, "dataset_id": dataset_id, "sample": result["sample"],
            "counts": [{key: item[key] for key in ("topic_id", "section", "title", "count", "denominator", "percent")}
                       for item in result["counts"]],
            "instruction": "评论数由程序计算，完整归类与原文已保存。review_findings每个优缺点附frequency={source_id,topic_id}，系统按评论数排序；再补代表性原文evidence。不要把关键词命中数或举例数作为支持评论数。"}

    def review_findings(self, cap, findings):
        if not findings:
            raise AgentError("finding_invalid", "请提交至少一条有依据的发现。")
        sources = {row["source_id"]: row for row in self.state.list_sources(cap.actor, cap.run_id)}
        prepared, packet = evidence_packet(findings, sources, review_view=self._review_reading_view)
        identity = sha256_value(packet)
        prior = self.state.get(cap.actor, cap.run_id)["checkpoint"].get("reviewed_findings") or {}
        if prior.get("input_sha256") == identity:
            return prior["result"]
        prior_items = {item["finding_id"]: item for item in prior.get("items", [])}
        reused = {item["finding_id"] for item in prepared if prior_items.get(item["finding_id"]) == item
                  and all(prior.get("source_hashes", {}).get(ref["source_id"]) == sources[ref["source_id"]]["source_hash"] for ref in item["evidence"])}
        pending = [item for item in findings if item["finding_id"] not in reused]
        rows = [{"finding_id": key, "supported": True, "feedback": ""} for key in reused]
        if pending:
            pending_prepared, packet = evidence_packet(pending, sources, review_view=self._review_reading_view)
            question = self.state.get(cap.actor, cap.run_id)["request"]["question"]
            incoming = {"stream": False, "messages": [{"role": "system", "content": AUDIT_PROMPT},
                {"role": "user", "content": json.dumps({"question": question, **packet}, ensure_ascii=False)}]}
            raw = b"".join(self.broker.model_stream(cap, incoming, []))
            rows.extend(parse_audit(raw, pending_prepared))
        passed = {row["finding_id"] for row in rows if row["supported"]}
        # Preserve passed findings when the model repairs only flagged items.
        replacements = {item["finding_id"] for item in prepared}
        accepted = [item for item in prior.get("items", []) if item["finding_id"] not in replacements]
        accepted.extend(item for item in prepared if item["finding_id"] in passed)
        identifier = "findings_" + sha256_value(accepted).split(":")[1]
        result = {"finding_set_id": identifier if accepted else None,
            "accepted_finding_ids": [item["finding_id"] for item in accepted],
            "needs_revision": [{"finding_id": item["finding_id"], "feedback": item["feedback"][:1000]} for item in rows if not item["supported"]],
            "instruction": "已通过的条目可以交付；请先修正具体错误，完整后用finding_set_id及report调用submit_answer，无需再写一遍报告。"}
        self._checkpoint(cap, reviewed_findings={"input_sha256": identity, "finding_set_id": identifier,
            "items": accepted, "result": result,
            "source_hashes": {ref["source_id"]: sources[ref["source_id"]]["source_hash"] for item in accepted for ref in item["evidence"]}})
        self.broker.event(cap, "lifecycle.findings_reviewed", data={"accepted": len(accepted), "revision": sum(not row["supported"] for row in rows), "reused": len(reused), "reviewed": len(pending)})
        return result

    def submit_answer(self, cap, args):
        checkpoint = self.state.get(cap.actor, cap.run_id)["checkpoint"]
        key = sha256_value(args)
        prior = checkpoint.get("answer_draft") or {}
        if prior.get("delivery_key") == key and prior.get("attempt_id") == cap.attempt_id:
            return {"accepted": True, "instruction": "这份答复已保存，无需重复生成。"}
        report = args.get("report")
        if report and (not report.get("formats", ["md"]) or not re.fullmatch(r"[^/\\\x00-\x1f:]{1,100}", report["filename"]) or re.sub(r"(?:\.(?:md|pdf))+$", "", report["filename"], flags=re.IGNORECASE) in {"", ".", ".."}):
            raise AgentError("report_format_invalid", "请检查报告文件名并至少选择一种格式。")
        if args.get("finding_set_id"):
            checked = checkpoint.get("reviewed_findings") or {}
            if checked.get("finding_set_id") != args["finding_set_id"] or not checked.get("items"):
                raise AgentError("finding_set_invalid", "请使用本次研究已核对的发现编号。")
            for sid, digest in checked["source_hashes"].items():
                if self.state.source(cap.actor, cap.session_id, sid)["source_hash"] != digest:
                    raise AgentError("source_changed", "结论所依据的原文已变化，请重新核对。")
            for item in checked["items"]:
                validate_frequency_claim(item)
            selected = args.get("summary_finding_ids") if report else [item["finding_id"] for item in checked["items"]]
            body, report_body = documents(checked["items"], selected)
            if args.get("answer"):
                raise AgentError("finding_answer_conflict", "已核对的发现会直接生成答复，不需要另写answer。")
        else:
            body = args.get("answer")
            if report:
                report_body = report.get("markdown", body)
        answer, citations = self.validate_answer(cap.actor, cap.run_id, body)
        if report:
            # Validate both bodies before any artifact creates new sources. In
            # follow-ups, the answer may cite a previous run while a truncated
            # alternate report has no sources in the current run at all.
            report_validated = self.validate_answer(cap.actor, cap.run_id, report_body)
            if citations and not report_validated[1]:
                raise AgentError("report_citations_missing", "报告正文缺少答案使用的来源引用。请提交完整正文和引用，或省略 report.markdown 直接使用完整答案生成文件。")
            # Deterministic content/size errors must reach the model before
            # acceptance so it can split the report without dropping sources.
            self._report_bundle(cap, report["title"], report_validated, re.sub(r"(?:\.(?:md|pdf))+$", "", report["filename"], flags=re.IGNORECASE))
        # Save the answer before format rendering. A PDF error or interrupted
        # download must not discard an already checked useful chat response.
        draft = {"attempt_id": cap.attempt_id, "answer": answer, "citations": citations, "delivery_key": key}
        self._checkpoint(cap, answer_draft=draft, answer_feedback=None)
        failures, files = [], []
        if report:
            formats = report.get("formats", ["md"])
            if not formats:
                raise AgentError("report_format_invalid", "请至少选择一种报告格式。")
            for form in sorted(formats, key=lambda value: value != "md"):
                try:
                    output = self._publish_validated_report(cap, {**report, "formats": [form]}, report_validated)
                    files.extend(file for file in output["files"] if file["artifact_id"] not in {saved["artifact_id"] for saved in files})
                except AgentError as error:
                    if error.code in {"research_cancelled", "research_not_running", "research_authorization_denied", "research_authority_changed"}:
                        raise
                    failures.append({"format": form, "code": error.code})
            if files:
                draft["answer"] += "\n\n" + " · ".join("[下载 " + file["filename"] + "](" + file["download_url"] + ")" for file in files)
            if failures:
                draft["answer"] += "\n\n" + "、".join(item["format"].upper() for item in failures) + " 文件暂未生成；以上分析已保存。"
            self._checkpoint(cap, answer_draft=draft, report_delivery={"files": files, "failures": failures})
        return {"accepted": True, "files": files, "format_failures": failures,
            "instruction": "答案、引用及可用文件已保存，本次研究结束。"}

    def publish_report(self, cap, args):
        return self._publish_validated_report(cap, args)

    def _portable_report(self, cap, title, validated):
        body, citations = validated
        sources = {item["source_id"]: self.state.source(cap.actor, cap.session_id, item["source_id"])
            for item in citations}
        return portable_report(title, body, citations, sources)

    def _report_bundle(self, cap, title, validated, filename):
        body, citations = validated
        sources = {item["source_id"]: self.state.source(cap.actor, cap.session_id, item["source_id"]) for item in citations}
        return report_bundle(title, body, citations, sources, filename)

    def _publish_validated_report(self, cap, args, validated=None):
        filename = args["filename"]
        if not re.fullmatch(r"[^/\\\x00-\x1f:]{1,100}", filename) or filename in {".", ".."}:
            raise AgentError("artifact_name_invalid", "报告文件名无效")
        # Callers may provide a display filename; the renderer adds the format.
        filename = re.sub(r"(?:\.(?:md|pdf))+$", "", filename, flags=re.IGNORECASE)
        if not filename or filename in {".", ".."}:
            raise AgentError("artifact_name_invalid", "报告文件名无效")
        formats = args.get("formats", ["md"])
        if not formats:
            raise AgentError("report_format_invalid", "请至少选择一种报告格式")
        body, citations = validated if validated is not None else self.validate_answer(cap.actor, cap.run_id, args["markdown"])
        documents = self._report_bundle(cap, args["title"], (body, citations), filename)
        published = []
        # Publish appendices first, then link the body to the actual owned files.
        for document in [*documents[1:], documents[0]]:
            forms = ["md"] if document['role'] == 'appendix' else formats
            markdown = document['markdown']
            for appendix in published:
                markdown = markdown.replace('(' + quote(appendix['filename']) + ')', '(' + appendix['download_url'] + ')')
            cache_key = sha256_value({'markdown': markdown, 'filename': document['filename'], 'formats': forms})
            cached = self.state.get(cap.actor, cap.run_id)['checkpoint'].get('report_parts', {}).get(cache_key)
            if cached and cached['attempt_id'] == cap.attempt_id:
                published.extend(cached['files'])
                continue
            rendered = self.manager.tool(cap.attempt_id, "render_report", {"title": args["title"], "markdown": markdown,
                "filename": document['filename'], "formats": forms})
            files = rendered.get("files") if isinstance(rendered, dict) else None
            if not isinstance(files, list) or len(files) != len(forms) or {item.get("filename") for item in files if isinstance(item, dict)} != {document['filename'] + "." + form for form in forms}:
                raise AgentError("report_render_failed", "报告文件生成没有完成，请重试", http_status=502)
            part_files = []
            for item in files:
                self.broker.check(cap)
                if item['filename'].lower().endswith('.md'):
                    # These numbers were bound by our validator above. Verify
                    # the renderer preserved those exact bytes; model-uploaded
                    # Markdown still goes through publish_artifact validation.
                    raw = self.manager.read_output(cap.attempt_id, item['path'])
                    if raw != markdown.encode('utf-8'):
                        raise AgentError('report_render_failed', '生成的 Markdown 与已核验正文不一致，请重新生成文件。')
                    part_files.append(self._persist_artifact_bytes(cap, raw, item['filename']))
                else:
                    part_files.append(self.publish_artifact(cap, {"path": item["path"], "filename": item["filename"]}))
            parts = self.state.get(cap.actor, cap.run_id)['checkpoint'].get('report_parts', {})
            self._checkpoint(cap, report_parts={**parts, cache_key: {'attempt_id': cap.attempt_id, 'files': part_files}})
            published.extend(part_files)
        self._checkpoint(cap, published_report={"status": "files_available", "attempt_id": cap.attempt_id,
            "title": args["title"], "files": published,
            "citation_source_ids": [citation["source_id"] for citation in citations]})
        return {"status": "available", "title": args["title"], "files": published,
            "instruction": "报告已保存，引用已转换并自动附上原文，文件大小与草稿不同是正常结果。使用返回的download_url交付；下载链接供用户浏览器使用，无需在沙箱探测宿主地址或重复附加原文。请submit_answer简述主要发现；具体体验仍引用已读取的原始评论，不把刚写的报告作为新事实证明。"}

    def publish_artifact(self, cap, args):
        filename = args["filename"]
        if not re.fullmatch(r"[^/\\\x00-\x1f:]{1,160}", filename) or filename in {".", ".."}:
            raise AgentError("artifact_name_invalid", "结果文件名无效")
        raw = self.manager.read_output(cap.attempt_id, args["path"])
        if Path(filename).suffix.lower() == '.md':
            try:
                markdown = raw.decode('utf-8')
            except UnicodeDecodeError:
                raise AgentError('artifact_encoding_invalid', 'Markdown 文件须使用 UTF-8 编码。') from None
            if markers(markdown):
                validated = self.validate_answer(cap.actor, cap.run_id, markdown)
                title = re.search(r'^#\s+(.+)$', markdown, re.M)
                documents = self._report_bundle(cap, title[1] if title else Path(filename).stem, validated, Path(filename).stem)
                published = []
                for document in [*documents[1:], documents[0]]:
                    body = document['markdown']
                    for appendix in published:
                        body = body.replace('(' + quote(appendix['filename']) + ')', '(' + appendix['download_url'] + ')')
                    content = body.encode()
                    input_name = 'published-markdown-' + hashlib.sha256(content).hexdigest() + '.md'
                    self.manager.put_input(cap.attempt_id, input_name, content)
                    output_name = filename if document['role'] == 'body' else document['filename'] + '.md'
                    published.append(self._persist_artifact_bytes(cap, content, output_name,
                        portable_path='/inputs/' + input_name))
                return {**published[-1], **({'related_files': published[:-1]} if len(published) > 1 else {})}
        return self._persist_artifact_bytes(cap, raw, filename)

    def _persist_artifact_bytes(self, cap, raw, filename, *, portable_path=None):
        if len(raw) > 16 * 1024**2:
            raise AgentError('artifact_quota_exceeded', '单份研究文件不能超过16MiB。')
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        mime = "text/markdown" if Path(filename).suffix.lower() == ".md" else mimetypes.guess_type(filename)[0] or "application/octet-stream"
        run = self.store.get_run(cap.actor, cap.run_id)
        prior = next((item for item in run['artifacts'] if item['artifact_type'] == 'research_output'
                      and item.get('status') == 'available' and item.get('content_hash') == digest
                      and item.get('metadata', {}).get('filename') == filename), None)
        if prior:
            self.artifact(cap.actor, cap.session_id, prior['artifact_id'])  # Verify bytes before reusing a receipt.
            result = self._artifact_source(cap, prior, raw, mime)
            return {**result, 'reused': True, **({'portable_path': portable_path} if portable_path else {})}
        root = self.store.database_path.parent / "research-artifacts"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise AgentError("artifact_storage_invalid", "分析结果目录不可用", http_status=503)
        if sum(item.get("metadata", {}).get("bytes", 0) for item in run["artifacts"]) + len(raw) > 100 * 1024**2:
            raise AgentError("artifact_quota_exceeded", "本次研究的保存文件已达到100MB")
        disk_id = new_id("out")
        path = root / disk_id
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
        try:
            self.broker.check(cap)
            artifact = self.store.add_artifact(cap.actor, run_id=cap.run_id, artifact_type="research_output", upstream_id=disk_id,
                contract_version="wenquan-research-artifact/v1", status="available", display_name=filename, content_hash=digest,
                metadata={"filename": filename, "media_type": mime, "bytes": len(raw), "attempt_id": cap.attempt_id})
        except Exception:
            path.unlink(missing_ok=True)
            raise
        result = self._artifact_source(cap, artifact, raw, mime)
        return {**result, **({'portable_path': portable_path} if portable_path else {})}

    def _artifact_source(self, cap, artifact, raw, mime):
        aid, digest = artifact["artifact_id"], artifact['content_hash']
        filename = artifact['metadata']['filename']
        download = f"/wenquan/api/v1/sessions/{quote(cap.session_id, safe='')}/artifacts/{aid}/download"
        text = raw.decode("utf-8", errors="replace") if mime.startswith("text/") or mime == "application/json" else ""
        source_id = self._source(cap, {"source_type": "artifact", "title": filename, "artifact_id": aid, "filename": filename,
            "media_type": mime, "content": text if len(text) <= 100000 else "文件已保存，可下载完整结果。", "download_url": download,
            "provenance": {"sha256": digest, "bytes": len(raw), "attempt_id": artifact['metadata']['attempt_id']}})
        return {"artifact_id": aid, "source_id": source_id, "filename": filename, "media_type": mime,
            "bytes": len(raw), "download_url": download, "sha256": digest}

    def artifact(self, actor, session_id, artifact_id):
        bundle = self.store.session_bundle(actor, session_id)
        artifact = next((item for run in bundle["runs"] for item in run["artifacts"] if item["artifact_id"] == artifact_id and item["artifact_type"] == "research_output"), None)
        if not artifact or not re.fullmatch(r"out_[0-9a-f]{24}", artifact["upstream_id"]):
            raise AgentError("artifact_not_found", "没有找到这份研究文件", http_status=404)
        path = self.store.database_path.parent / "research-artifacts" / artifact["upstream_id"]
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise AgentError("artifact_not_found", "研究文件不可用", http_status=404)
        if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != artifact["content_hash"]:
            raise AgentError("artifact_changed", "研究文件内容校验失败", http_status=409)
        return artifact, path
