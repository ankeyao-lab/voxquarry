"""Question-led research over one immutable report and its frozen review facts.

The model can choose read tools and write an answer. Report identity, data access,
source hashes, lifecycle and budgets remain controlled by the application.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from typing import Any
from urllib.parse import quote

from .contracts import AgentError, IdentityContext, sha256_value
from .answer_checks import delivery_feedback, dual_ranking_size, simple_count_question, style_feedback
from .readonly import ReadonlyQA, record_selection_response, report_href, selection_model_info
from .research_model import ResearchModel, build_research_messages, classification_requested
from .research_grounding import GroundedResearchModel, finish_grounded_answer
from .research_batches import batch_tool, run_batches, semantic_source
from .semantic_batches import SemanticBatchStore
from .research_rankings import build_report_ranking, report_ranking_context, report_ranking_requested
from .report_selection import choose_report
from .question_tasks import explicit_requirements


RESEARCH_PLAN = [
    {"title": "查看报告", "tool_name": "zhuojian.get_report@v1"},
    {"title": "查阅评论并分析"},
    {"title": "核对来源版本"},
    {"title": "整理来源"},
]
RESEARCH_TOOLS = {
    "get_report_section": "读取完整报告 JSON 的任意章节。先用 section=index 查看目录，再用 JSON pointer（例如 /product_insight/aspect_experience、/tables/0）。数组可连续分页，未读后续行不代表不存在。报告是规则分析的结果，可能错误，须用原始评论核对。",
    "query_report_reviews": "检索本报告的完整原始评论 JSON。可按词组、评分、品牌、型号、日期过滤和继续分页；review_ids 可取指定完整原文。英文主题优先用match_mode=word并列出需要的词形，避免turn误匹配return；查原句可用substring。搜索词是线索，不代表主题语义、情绪或故障。想确认缺陷须读全文，区分旧产品、假设和实际经历。一般一次读20条，根据page继续。",
    "aggregate_report_reviews": "对本报告全部冻结评论进行去重计数与分组/交叉统计。term_groups每组内OR、组间AND；可按评分/月份/品牌等分组。结果是完整筛选集的计数，关键词匹配数不是语义认定的投诉数，也不是因果关系。",
}
MAX_MODEL_CALLS = 14
MAX_CONTEXT_CHARS = 500_000
MAX_SOURCE_CHARS = 8_000_000
# Leave room for one answer candidate and bounded correction/review feedback.
# The admitted sources still use the same complete-context encoder as the model.
CONTEXT_HEADROOM_CHARS = 40_000
MAX_DURATION_SECONDS = 900
ASPECT_SECTION = "/product_insight/aspect_experience"


def needs_aspect_baseline(question: str) -> bool:
    """Route broad theme comparisons, not every mention of a negative review."""
    value = question.strip().casefold()
    if re.search(r"这(?:一)?[条段句]|该条|单条|某条|以下(?:原文|评论)|这个问题", value):
        return False
    if dual_ranking_size(question) or re.search(r"优缺点|利弊|\bpros\b.*\bcons\b|\bcons\b.*\bpros\b", value):
        return True
    if re.search(r"交叉|重合|共现|交集|同时提到|互相影响", value) and not re.search(r"排名|排行", value):
        return False
    themes = r"负面|正面|好评|差评|优点|缺点|不足|抱怨|投诉|主题|问题|\bcomplaints?\b|\bstrengths?\b|\bweaknesses?\b"
    broad = r"主要|普遍|常见|最多|最不满|排名|排行|是什么|有哪些|前\s*[一二三四五六七八九十\d]+|\btop\s*\d+|\bmain\b|\bmost common\b"
    return bool(re.search(themes, value) and re.search(broad, value))


def tool_argument_feedback(tools: list[dict[str, Any]]) -> str:
    """Explain only our exposed schemas, without reflecting invalid model input."""
    hints = []

    def describe(node: dict[str, Any], path: str, depth: int = 0) -> None:
        if depth > 3:
            return
        constraints = {key: node[key] for key in ("type", "enum", "minimum", "maximum", "minItems", "maxItems", "maxLength") if key in node}
        if path and constraints:
            hints.append(path + "=" + json.dumps(constraints, ensure_ascii=False, separators=(",", ":")))
        properties = node.get("properties") or {}
        if properties:
            required = node.get("required") or []
            hints.append((path or "args") + " 仅允许字段 " + ",".join(properties) + ("；必填 " + ",".join(required) if required else "；无必填字段"))
            for key, child in properties.items():
                describe(child, path + "." + key if path else key, depth + 1)
        if isinstance(node.get("items"), dict):
            describe(node["items"], path + "[]", depth + 1)

    for tool in tools:
        if tool.get("name") in RESEARCH_TOOLS:
            hints.append(tool["name"] + ":")
            describe(tool["parameters"], "")
    return "按当前工具合同重新填写 args；字段类型、数组和允许值如下。report_id 由程序提供，不要填写；不要把 schema 或示例当参数。\n" + "\n".join(hints)


def validation_feedback(stage: str | None, tools: list[dict[str, Any]] | None = None) -> str:
    details = {
        "finding_required": "当前须返回type=findings及逐项原文依据，先核对发现再写答案。不要直接返回answered正文。",
        "finding_shape": "按findings合同给出finding_id、kind、statement、context和evidence；每项是能独立核对的发现。",
        "finding_evidence": "评论quote必须是该来源正文中的连续原句；报告pointer必须是该来源data中实际存在的JSON路径。保留原意，不拼接或翻译quote。",
        "action_type": '顶层 type 必须是 answer 或 tool。回答结构为 {"type":"answer","status":"answered","answer":"answer in the language requested by the user","citations":[{"source_id":"本轮来源ID"}],"limitations":[]}；查询结构为 {"type":"tool","tool_name":"工具名称","args":{}}。替换占位词、按工具合同填写参数，只返回一个JSON对象。',
        "answer_shape": '请修正 invalid_fields 列出的字段。回答顶层字段是 type="answer"、status="answered"/"insufficient_evidence"/"out_of_scope"、answer字符串、citations数组、limitations字符串数组。citations每项只用{"source_id":"一个已有来源ID"}，不要用数组、原文记录ID或把多个ID拼在一个字符串里；无需quote。不要添加response_contract未列出的字段。报告数字不是自行分类人数，不要为它填写review_groups。',
        "citation_quote_kind": "引用 quote 必须对应本轮来源；不需要逐字引述时可省略 quote，仅填写 source_id。",
        "citation_quote": "引用 quote 必须是对应来源原文中的连续逐字片段，不能改写或拼接；无需逐字引述时删除可选 quote，把解释写在 answer。",
        "citation_marker": "answer 中的 [来源ID] 必须与 citations 的 source_id 完全对应；只引用本轮 sources 中存在的ID。",
        "citation_id": "citations 只能包含本轮 sources 中存在的 source_id，每个ID列一次。",
        "review_group": "review_groups须逐条列出真实评论来源及其连续原句，按原文区分实际、比较和假设。每组group_id唯一；count不要填写，正文用{count:G1}让程序去重计算。不能引用未读来源或凭印象估计分类人数。",
        "citation_link": "用 [来源ID] 引用已有来源，不在 answer 中生成网址、Markdown外链或HTML。",
        "tool_args": "按所选工具 parameters 填写 args，不添加 report_id 或其它未声明字段。",
        "tool_name": "tool_name 只能选当前 tools 列表中的名称。",
    }
    detail = tool_argument_feedback(tools) if stage == "tool_args" and tools else details.get(stage, "请按 response_contract 重写一个 JSON action，保留准确字段和数据类型；仅使用本轮来源ID。")
    return "上次动作未通过格式校验。" + detail


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def report_overview(result: dict[str, Any]) -> dict[str, Any]:
    """Small opening context with a directory, never a clipped table masquerading as complete."""
    data = result["data"]
    fields = ("title", "scope", "samples", "metrics", "metric_definitions", "findings", "summary_text", "executive_summary")
    return {**{key: data[key] for key in fields if key in data},
            "table_directory": [{"index": index, "title": item.get("title"), "table_id": item.get("table_id"),
                                 "row_count": len(item.get("rows") or []), "pointer": f"/tables/{index}"}
                                for index, item in enumerate(data.get("tables") or [])],
            "full_report_tool": "get_report_section",
            "note": "这是报告目录及摘要，完整章节和原始评论都可用工具读取。报告主题/情绪分类未经语义复核。"}


def model_tools(registry: Any) -> list[dict[str, Any]]:
    items = []
    for name, description in RESEARCH_TOOLS.items():
        schema = copy.deepcopy(registry.get("zhuojian." + name + "@v1").input_schema)
        schema.pop("$id", None)
        schema["properties"].pop("report_id", None)
        schema["required"] = [key for key in schema.get("required", []) if key != "report_id"]
        items.append({"name": name, "description": description, "parameters": schema})
    return items


class ReportResearchQA(ReadonlyQA):
    plan = RESEARCH_PLAN

    def __init__(self, service, *, recover=False):
        super().__init__(service, recover=recover)
        self.semantic = SemanticBatchStore(self.store)

    def resume(self, actor, session_id, run_id, request_id):
        run = self.store.get_run(actor, run_id)
        if run["session_id"] != session_id or run["intent"] != "report_readonly_answer":
            raise AgentError("run_not_found", "没有找到对应分析。", http_status=404)
        if run["status"] == "running":
            raise AgentError("run_busy", "这次分析仍在进行。", http_status=409)
        if run["status"] == "completed":
            raise AgentError("resume_unavailable", "这次回答已完成，可以继续提出新问题。", http_status=409)
        draft = self.turns.draft(actor, run_id)
        if not draft or draft.get("action", {}).get("resume_version") != "research-v2":
            raise AgentError("resume_unavailable", "这次分析没有可继续的记录，请重新提问。", http_status=409)
        original = next((m for m in self.store.list_messages(actor, session_id)
                         if m.get("run_id") == run_id and m["role"] == "user"), None)
        if not original:
            raise AgentError("resume_unavailable", "没有找到原问题，请重新提问。", http_status=409)
        report_id = original["metadata"].get("resolved_report_id") or original["metadata"].get("report_id")
        if not report_id:
            raise AgentError("resume_unavailable", "尚未确定报告，请重新提问。", http_status=409)
        return self.submit(actor, session_id=session_id, question=original["content"], report_id=report_id,
                           request_id=request_id, selection_mode="explicit", resume_from=run_id)

    def cancel(self, actor, session_id, run_id):
        result = super().cancel(actor, session_id, run_id)
        draft = self.turns.draft(actor, run_id)
        if draft and draft.get("action", {}).get("resume_version") == "research-v2":
            with self.store._connect() as db:
                row = db.execute("SELECT message_id,metadata_json FROM messages WHERE run_id=? AND role='assistant'", (run_id,)).fetchone()
                if row:
                    info = json.loads(row["metadata_json"])
                    info["can_resume"] = True
                    db.execute("UPDATE messages SET metadata_json=? WHERE message_id=?", (encode(info), row["message_id"]))
        return result

    @property
    def generation_enabled(self) -> bool:
        return os.environ.get("VOXQUARRY_WENQUAN_REPORT_GENERATION_ENABLED") == "true"

    def resolve_report(self, actor: IdentityContext, session_id: str, run_id: str, question: str,
                       messages: list[dict[str, Any]], progress: Any, active: Any) -> str:
        event_id = progress("查找对应报告和已有评论")
        reports = self.reports(actor)["items"]
        transport = self.service.executor.transport if self.service.executor else None
        workflow = getattr(transport, "report_workflow", None)
        candidates = []
        active()
        previous = next((m.get("metadata", {}).get("resolved_report_id") or m.get("metadata", {}).get("report_id")
                         for m in reversed(messages) if m.get("metadata", {}).get("resolved_report_id") or m.get("metadata", {}).get("report_id")), None)
        query = question
        if messages and (messages[-1].get("metadata", {}).get("error") or {}).get("code") == "report_selection_required":
            last_question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            query = last_question + "\n补充条件：" + question
        try:
            kind, chosen = choose_report(query, reports, [], previous)
        except AgentError:
            if not (self.generation_enabled and workflow and actor.allowed_report_ids is None):
                raise
            candidates = workflow("catalog", {}, actor).get("items", [])
            active()
            kind, chosen = choose_report(query, reports, candidates, previous)
        if kind == "report":
            progress("已找到对应报告", "completed", event_id, chosen["title"])
            return chosen["report_id"]
        if not workflow:
            raise AgentError("research_data_unavailable", "没有匹配的报告，报告生成服务尚不可用。", http_status=409)
        progress("已有对应评论，开始准备报告", "completed", event_id, chosen["title"])
        generation = progress("正在生成报告", detail=chosen["title"])
        active()
        prepared = workflow("ensure", {"candidate_id": chosen["candidate_id"], "date_range": chosen.get("date_range", {})}, actor)
        job_id = prepared["job"]["job_id"]
        token = prepared["job_token"]
        try:
            while prepared["job"]["status"] not in {"completed", "failed", "cancelled"}:
                active()
                job = prepared["job"]
                progress("正在生成报告", "running", generation, str(job.get("stage_label") or "处理中"))
                time.sleep(1)
                prepared = workflow("status", {"job_id": job_id, "job_token": token}, actor)
            active()
        except AgentError:
            if not self.turns.active(actor, run_id):
                workflow("cancel", {"job_id": job_id, "job_token": token}, actor)
            raise
        job = prepared["job"]
        if job["status"] != "completed" or not job.get("report_id"):
            progress("报告生成未完成", "failed", generation)
            error = job.get("error") or {}
            raise AgentError("report_generation_failed", error.get("message") or "报告生成没有完成，已有评论仍在。可以稍后继续这个问题。", http_status=409, retryable=error.get("retryable", True))
        progress("报告已生成，继续回答原问题", "completed", generation, chosen["title"])
        return job["report_id"]

    def source(self, actor: IdentityContext, session_id: str, citation_id: str, offset: int | None = None) -> dict[str, Any]:
        """Re-read a cited source using the session owner's current authorization."""
        self.store.get_session(actor, session_id)
        ref = next((c for m in self.store.list_messages(actor, session_id)
                    for c in m.get("metadata", {}).get("citations", [])
                    if c.get("citation_id") == citation_id and c.get("source_request")), None)
        if ref is None:
            raise AgentError("source_not_found", "没有找到这条来源。", http_status=404)
        request = ref["source_request"]
        if request["tool"] == "semantic_analysis":
            result = semantic_source(self, actor, session_id, request["payload"])
            self._verify_source_result(ref, result)
            if offset is not None and not 0 <= offset <= result["data"]["processed_records"]:
                raise AgentError("invalid_request", "该原文页不存在。")
            result = semantic_source(self, actor, session_id, request["payload"], include_reviews=True, offset=offset or 0)
            return {**result, "report_href": report_href(ref["report_id"])}
        result = self.invoke(actor, request["tool"], request["payload"], session_id=session_id)
        self._verify_source_result(ref, result)
        if offset is not None:
            request = ref["source_request"]
            if request["tool"] != "get_report_section" or not result.get("page") or offset < 0 or offset > 100000:
                raise AgentError("invalid_request", "这条来源不支持该分页。")
            fresh = self.invoke(actor, request["tool"], {**request["payload"], "offset": offset}, session_id=session_id)
            self.consistent(fresh, result)
            result = fresh
        if ref.get("record_id"):
            item = next((r for r in result["data"].get("items", []) if r.get("record_id") == ref["record_id"]), None)
            if item is None:
                raise AgentError("source_changed", "这条原文已无法核对，请重新提问。", http_status=409)
            return {"report": result["report"], "snapshot": result["snapshot"], "review": item}
        return {"report": result["report"], "snapshot": result["snapshot"], "data": result["data"],
                "page": result.get("page"), "report_href": report_href(ref["report_id"])}

    @staticmethod
    def _verify_source_result(source: dict[str, Any], result: dict[str, Any]) -> None:
        if (source["report_id"] != (result.get("report") or {}).get("report_id")
                or source["snapshot_id"] != (result.get("snapshot") or {}).get("snapshot_id")
                or source["report_identity_sha256"] != sha256_value(result.get("report"))
                or source["snapshot_identity_sha256"] != sha256_value(result.get("snapshot"))
                or source["source_sha256"] != sha256_value(result["data"])):
            raise AgentError("source_changed", "引用的内容发生变化，本轮回答未发布。", http_status=409, retryable=True)

    def execute(self, actor: IdentityContext, session_id: str, run: dict[str, Any], question: str,
                report_id: str | None, request_id: str) -> None:
        started, run_id, steps = time.monotonic(), run["run_id"], run["steps"]
        metadata: dict[str, Any] = {"request_id": request_id, "report_id": report_id, "claims": [], "citations": [], "model_calls": 0}
        trace: dict[str, Any] = {}
        sources: dict[str, dict[str, Any]] = {}
        observations: list[dict[str, Any]] = []
        report: dict[str, Any] = {}
        source_number = 0
        requests: dict[str, dict[str, Any]] = {}
        history: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        report_context: dict[str, Any] = {}
        resume_state: dict[str, Any] = {"type": "research_progress"}
        previous_calls = 0
        restoring = False
        semantic_jobs = []

        def persist(candidate=None):
            nonlocal resume_state
            if restoring:
                return
            if candidate is not None:
                resume_state = copy.deepcopy(candidate)
            payload = {**resume_state, "resume_version": "research-v2",
                       "semantic_jobs": semantic_jobs,
                       "observations": observations[-60:], "answer_tasks": metadata.get("answer_tasks", []),
                       "cumulative_model_calls": previous_calls + trace.get("model_calls", 0)}
            self.turns.checkpoint(actor, run_id, payload, sources, metadata.get("grounding_checks", []))

        def can_resume():
            saved = self.turns.draft(actor, run_id)
            return bool(saved and saved.get("action", {}).get("resume_version") == "research-v2")

        def progress(title: str, status: str = "running", event_id: str | None = None, detail: str = "") -> str:
            return self.turns.event(actor, run_id, title, status, event_id=event_id, detail=detail)

        def active() -> None:
            if not self.turns.active(actor, run_id):
                raise AgentError("run_cancelled", "已停止回答。", http_status=409)
            if time.monotonic() - started > MAX_DURATION_SECONDS:
                raise AgentError("research_budget_exhausted", "这次分析尚未完成，已保存进度，可以继续分析。", http_status=409, retryable=True)

        def step(index: int, status: str) -> None:
            active()
            self.turns.step(actor, run_id, steps[index]["step_id"], status)

        def call(name: str, payload: dict[str, Any], index: int = 1) -> dict[str, Any]:
            active()
            # The model never supplies identity, report ID, filesystem or network targets.
            if payload.get("report_id") != report_id:
                raise AgentError("scope_mismatch", "本次只能查询所选报告的资料。", http_status=409)
            title = {"get_report_section": "读取报告章节", "query_report_reviews": "查阅相关评论", "aggregate_report_reviews": "计算评论统计"}.get(name, "读取资料")
            if name == "get_report_section":
                title = {"/scope": "确认报告对应产品", "/samples": "读取评论总数", "index": "查看报告目录",
                         "/product_insight/aspect_experience": "读取完整优缺点统计", "/findings": "阅读报告结论",
                         "/executive_summary": "阅读报告摘要"}.get(payload.get("section"), title)
            event_id = progress(title)
            try:
                result = (semantic_source(self, actor, session_id, payload, check_active=active,
                          on_progress=lambda page, total: progress("核对已分析的原文", "running", event_id, f"已核对{page}/{total}批"),
                          run_id=run_id, step_id=steps[index]["step_id"]) if name == "semantic_analysis" else
                          self.invoke(actor, name, payload, session_id=session_id, run_id=run_id, step_id=steps[index]["step_id"]))
            except AgentError:
                progress(title + "未完成", "failed", event_id)
                raise
            detail = f"读到{len(result.get('data', {}).get('items', []))}条完整评论" if name == "query_report_reviews" else ""
            progress(title + "完成", "completed", event_id, detail)
            active()
            if (result.get("report") or {}).get("report_id") != report_id:
                raise AgentError("upstream_contract_invalid", "读取到的报告与当前选择不一致。", http_status=502)
            if name in {"get_report_section", "query_report_reviews", "aggregate_report_reviews"}:
                provenance = (result.get("data") or {}).get("provenance") or {}
                if (provenance.get("report_id") != report_id
                        or provenance.get("snapshot_id") != (result.get("snapshot") or {}).get("snapshot_id")
                        or provenance.get("analysis_version") != (result.get("report") or {}).get("analysis_version")):
                    raise AgentError("source_changed", "资料来源与所选报告的版本不一致，请重新提问。", http_status=409)
            if report:
                self.consistent(result, report)
            return result

        def register(name: str, payload: dict[str, Any], result: dict[str, Any], *, opening: bool = False) -> None:
            nonlocal source_number
            request_key = sha256_value({"tool": name, "payload": payload})
            data = result["data"]
            fingerprint = sha256_value(data)
            if request_key in requests:
                if requests[request_key]["hash"] != fingerprint:
                    raise AgentError("source_changed", "资料在分析过程中发生变化，请重新提问。", http_status=409)
                observations.append({"tool_name": name, "source_ids": requests[request_key]["source_ids"],
                                     "page": result.get("page"), "repeated_read": True})
                if payload.get("review_ids"):
                    for sid in requests[request_key]["source_ids"]:
                        sources[sid]["in_model"] = True
                return
            common = {"report_id": report_id, "snapshot_id": result["snapshot"]["snapshot_id"],
                      "report_identity_sha256": sha256_value(result["report"]),
                      "snapshot_identity_sha256": sha256_value(result["snapshot"]),
                      "source_sha256": fingerprint, "source_request": {"tool": name, "payload": payload}}
            source_ids = []
            pending_sources: dict[str, dict[str, Any]] = {}

            def add(kind: str, text: str, label: str, **extra: Any) -> None:
                if kind == "review" and extra.get("record_id"):
                    existing = next(((sid, s) for sid, s in (sources | pending_sources).items() if s.get("record_id") == extra["record_id"]), None)
                    if existing:
                        # The same review may appear in several searches; do not
                        # inflate context or the number of independently read records.
                        if existing[1]["text"] != text:
                            raise AgentError("source_changed", "同一条评论出现不同内容，请重新提问。", http_status=409)
                        source_ids.append(existing[0])
                        if payload.get("review_ids"):
                            existing[1]["in_model"] = True
                        return
                sid = "S" + str(source_number + len(pending_sources) + 1)
                source_ids.append(sid)
                pending_sources[sid] = {**common, "kind": kind, "text": text, "label": label, **extra}

            if name == "query_report_reviews":
                for item in data.get("items", []):
                    original = "\n".join(str(item.get(k) or "") for k in ("review_title", "review_text")).strip()
                    add("review", encode(item), "评论原文 · " + str(item.get("review_title") or item.get("record_id")),
                        record_id=item["record_id"], quote_text=original)
                # Counts and pagination are independently citable, including an empty search.
                add("analysis", encode({k: v for k, v in data.items() if k not in {"items", "sources"}} | {"page": result.get("page")}), "评论检索结果")
            elif name == "query_evidence":
                for item in data.get("items", []):
                    original = str(item.get("quote") or item.get("snippet") or "")
                    add("review", encode(item), "报告引用的评论片段", quote_text=original,
                        evidence_id=item.get("evidence_id"))
            else:
                add("report" if name in {"get_report", "get_report_section"} else "analysis",
                    encode(report_overview(result) if opening else data),
                    "报告摘要" if opening else "报告章节" if name == "get_report_section" else "评论逐条分析" if name == "semantic_analysis" else "评论统计")
            observation = {"tool_name": name, "args": {k: v for k, v in payload.items() if k != "report_id"},
                           "source_ids": source_ids, "page": result.get("page")}
            proposed = sources | pending_sources
            budget = MAX_CONTEXT_CHARS - CONTEXT_HEADROOM_CHARS
            fits = sum(len(s["text"]) for s in proposed.values()) <= MAX_SOURCE_CHARS
            if fits:
                try:
                    encoded = build_research_messages(
                        question=question, report_context=report_context, history=history,
                        observations=observations + [observation], tools=tools, sources=proposed,
                    )[1]["content"]
                    fits = len(encoded) <= budget
                except AgentError as error:
                    if error.code != "report_context_too_large":
                        raise
                    fits = False
            if restoring:
                fits = sum(len(s["text"]) for s in proposed.values()) <= MAX_SOURCE_CHARS
            if not fits:
                single_too_large = any(s["kind"] == "review" and len(s["text"]) > budget for s in pending_sources.values())
                message = ("其中单条评论本身超过本轮阅读容量，无法完整核对此条；没有截断或加入其正文。"
                           if single_too_large else "本次完整资料加上已有来源和对话超过本轮阅读容量，尚未加入任何新来源。")
                rejected_args = ({"args": observation["args"]} if len(encode(observation["args"])) <= 2000
                                 else {"argument_fields": sorted(observation["args"])})
                observations.append({"tool_name": name, **rejected_args,
                                     "tool_error": {"code": "research_context_too_large", "message": message},
                                     "next_step": "减少每页条数、改读更小的报告子节点或使用更精确的筛选；已读取的来源仍可使用。单条本身过大时如实说明无法完整核对；不能把本次未读取解释为没有相关证据，也不能宣称完成全部核对。"})
                return
            # Commit together: a rejected page must not consume IDs, poison
            # repeated-request checks, or leave unread text in model context.
            sources.update(pending_sources)
            source_number += len(pending_sources)
            requests[request_key] = {"hash": fingerprint, "source_ids": source_ids}
            observations.append(observation)
            persist()

        def correction(kind: str, feedback: list[str], answer: str) -> None:
            # Only the current draft is needed by the next model call. Earlier
            # drafts needlessly multiply context and are never persisted.
            observations[:] = [item for item in observations if "draft_answer" not in item]
            observations.append({"draft_answer": answer, kind: feedback})

        def record_check(kind: str, feedback: list[str], answer: str, **extra: Any) -> None:
            metadata.setdefault("answer_checks", []).append({
                "kind": kind, "feedback": feedback, "candidate_chars": len(answer), **extra,
            })

        def invoke_model(method: Any, **kwargs: Any) -> dict[str, Any]:
            attempt_hook = kwargs.pop("_attempt_hook", None)
            active()
            if trace.get("model_calls", 0) >= MAX_MODEL_CALLS:
                raise AgentError("research_budget_exhausted", "这次分析尚未完成，已保存进度，可以继续分析。", http_status=409, retryable=True)
            if sum(len(s["text"]) for s in sources.values()) > MAX_SOURCE_CHARS:
                raise AgentError("research_context_too_large", "这次涉及的评论较多，尚未完成全部核对。请按主题或时间分段研究。", http_status=409, retryable=True)
            call_info: dict[str, Any] = {"call_index": trace.get("model_calls", 0) + 1, "status": "preparing"}
            title = {"review_answer": "核对答案与原文", "repair_answer": "修正有问题的句子或引用",
                     "plan_question": "梳理需要回答的问题", "review_findings": "逐项核对发现与原文", "write_answer": "对照原文组织答案",
                     "classify_page": "逐条核对这一批评论", "review_writing": "检查答案是否改变原意",
                     "review_writing_batch": "核对这一批答案与原文",
                     "review_coverage": "检查是否答全了你的问题"}.get(method.__name__, "分析问题与资料")
            event_id = progress(title)
            candidate_content = None
            def streaming(update):
                description = {"waiting": "正在连接模型", "thinking": "模型正在分析资料",
                               "answering": "正在生成本步结果"}[update["phase"]]
                progress(title, "running", event_id, f"{description} · 已用时{update['elapsed_seconds']}秒")
            def received(response: dict[str, Any], info: dict[str, Any]) -> None:
                nonlocal candidate_content
                record_selection_response(trace, response, info)
                if method.__name__ in {"next_action", "write_answer", "classify_page", "review_writing", "review_writing_batch", "review_coverage"}:
                    try:
                        content = response["choices"][0]["message"]["content"]
                        if isinstance(content, str) and len(content) <= 50000:
                            candidate_content = content
                    except (KeyError, IndexError, TypeError):
                        pass
            try:
                if isinstance(getattr(method, "__self__", None), ResearchModel):
                    method.__self__.request_control = {"check_active": active, "on_progress": streaming}
                action = method(**kwargs, diagnostics=call_info,
                                on_response=received)
            except AgentError as error:
                # Private final JSON and bounded diagnostics support a precise
                # repair. Provider reasoning never enters this response.
                error.raw_candidate = candidate_content
                error.call_diagnostics = call_info
                raise
            finally:
                if isinstance(getattr(method, "__self__", None), ResearchModel):
                    method.__self__.request_control = {}
                if call_info.pop("request_started", False):
                    trace["model_calls"] = trace.get("model_calls", 0) + 1
                    trace.setdefault("calls", []).append(call_info)
                    if attempt_hook:
                        attempt_hook(f"{run_id}:{trace['model_calls']}")
                metadata.update(selection_model_info(trace))
                if call_info.get("status") == "invalid" and candidate_content is not None:
                    persist({**resume_state, "raw_candidate": candidate_content, "raw_candidate_stage": method.__name__,
                             "raw_candidate_context": call_info.get("classification_batch")})
                else:
                    # Even a cancelled provider attempt consumed a call. Keep
                    # its count with the resumable work without publishing it.
                    persist()
                if self.turns.active(actor, run_id):
                    valid = call_info.get("status") == "valid"
                    progress(title + ("完成" if valid else "需要调整"), "completed" if valid else "failed", event_id)
            active()
            return action

        def classify(args, job_id=None):
            def save_job(job, task_args):
                semantic_jobs[:] = [row for row in semantic_jobs if row["job_id"] != job["job_id"]]
                semantic_jobs.append({"job_id": job["job_id"], "args": task_args, "complete": job["complete"],
                                      "revision": job["revision"], "processed": job["next_offset"], "total": job["total"]})
                persist()
            job = run_batches(store=self.semantic, actor=actor, session_id=session_id, question=question,
                report_id=report_id, runtime=self.service.planner.runtime, args=args, call=call, register=register,
                invoke=invoke_model, budget_left=lambda: min(MAX_MODEL_CALLS - trace.get("model_calls", 0),
                    0 if time.monotonic() - started > MAX_DURATION_SECONDS - 180 else MAX_MODEL_CALLS),
                save_job=save_job, progress=progress, sources=sources, job_id=job_id)
            if job["revision"]:
                payload = {"report_id": report_id, "job_id": job["job_id"], "revision": job["revision"]}
                result = call("semantic_analysis", payload)
                for source in sources.values():
                    if source["source_request"]["tool"] == "semantic_analysis":
                        source["in_model"] = False
                register("semantic_analysis", payload, result)
            observations.append({"semantic_progress": {"processed": job["next_offset"], "total": job["total"],
                "complete": job["complete"], "instruction": "只使用逐条分析来源的程序计数；未处理部分不能当成零，未读全时不得给全量排名。"}})
            return job

        try:
            step(0, "running")
            all_messages = self.store.list_messages(actor, session_id)
            submitted = next((m.get("metadata", {}) for m in all_messages if m.get("run_id") == run_id and m["role"] == "user"), {})
            resumed = self.turns.draft(actor, submitted["resume_from"]) if submitted.get("resume_from") else None
            if submitted.get("resume_from") and not resumed:
                raise AgentError("resume_unavailable", "之前的分析记录已不可用，请重新提问。", http_status=409)
            if resumed:
                # Carry the complete checkpoint before any upstream read can
                # fail. A failed restore must remain resumable from that work,
                # not overwrite it with the new run's opening report sections.
                self.turns.checkpoint(actor, run_id, resumed["action"], resumed["sources"], resumed.get("checks", []))
                restoring = True
            messages = [m for m in all_messages if m.get("run_id") != run_id]
            if not report_id and submitted.get("selection_mode") == "auto":
                report_id = self.resolve_report(actor, session_id, run_id, question, messages, progress, active)
            if not report_id:
                report_id = next((m["metadata"]["report_id"] for m in reversed(messages) if m.get("metadata", {}).get("report_id")), None)
            if not report_id:
                raise AgentError("report_selection_required", "请先选择一份报告。", http_status=409)
            metadata["report_id"] = report_id
            with self.store._connect() as db:
                submitted["resolved_report_id"] = report_id
                db.execute("UPDATE messages SET metadata_json=? WHERE run_id=? AND role='user' AND owner_user_id=? AND scope_id=?",
                           (encode(submitted), run_id, actor.owner_user_id, actor.scope_id))
            matching_runs = {m.get("run_id") for m in messages if m.get("metadata", {}).get("report_id") == report_id}
            history = [{"role": m["role"], "text": m["content"], "previous_sources":
                        [{"label": c.get("label"), "record_id": c.get("record_id"), "request": c.get("source_request")}
                         for c in m.get("metadata", {}).get("citations", []) if c.get("source_request")]}
                       for m in messages if m.get("run_id") in matching_runs and m.get("run_id") is not None][-12:]
            tools = model_tools(self.service.registry)
            if classification_requested(question):
                tools.append(batch_tool(tools))
            # Opening a large report must not load all tables or scan hundreds
            # of thousands of reviews before a simple question can be answered.
            report = call("get_report_section", {"report_id": report_id, "section": "/scope"}, 0)
            if report["report"].get("status") != "completed":
                raise AgentError("report_not_ready", "这份报告还没有完成。", http_status=409)
            report_context = {"report": report["report"], "snapshot": report["snapshot"], "scope": report["data"].get("content")}
            register("get_report_section", {"report_id": report_id, "section": "/scope"}, report)
            overview = call("get_report_section", {"report_id": report_id, "section": "/samples"}, 0)
            register("get_report_section", {"report_id": report_id, "section": "/samples"}, overview)
            index = call("get_report_section", {"report_id": report_id, "section": "index"}, 0)
            register("get_report_section", {"report_id": report_id, "section": "index"}, index)
            ranking_action = None
            aspect_rows, aspect_sources = [], []
            if needs_aspect_baseline(question):
                offset, baseline_rows = 0, 0
                baseline_status = "unavailable"
                for _ in range(5):
                    payload = {"report_id": report_id, "section": ASPECT_SECTION, "offset": offset, "limit": 100}
                    try:
                        baseline = call("get_report_section", payload, 0)
                    except AgentError as error:
                        if error.code not in {"report_section_not_found", "research_result_too_large"}:
                            raise
                        break
                    content = (baseline.get("data") or {}).get("content")
                    if not isinstance(content, list) or not content:
                        break
                    register("get_report_section", payload, baseline)
                    if sha256_value({"tool": "get_report_section", "payload": payload}) not in requests:
                        break
                    baseline_rows += len(content)
                    aspect_rows.extend(content)
                    aspect_sources.extend(requests[sha256_value({"tool": "get_report_section", "payload": payload})]["source_ids"])
                    baseline_status = "partial"
                    page = baseline.get("page") or {}
                    if page.get("offset") != offset or page.get("returned") != len(content):
                        break
                    next_offset = offset + len(content)
                    if page.get("has_more") is False and page.get("next_offset") is None and page.get("total") == next_offset:
                        baseline_status = "complete"
                        break
                    if page.get("has_more") is not True or page.get("next_offset") != next_offset:
                        break
                    offset = next_offset
                observations.append({"aspect_baseline": {
                    "status": baseline_status, "rows_read": baseline_rows,
                    "note": ("已读取所选报告全部有文字评论的主题比较基线，没有新增星级筛选。原表按总提及数排列；优点、缺点须分别按 explicit_positive_entities、explicit_negative_entities 排序并说明并列。正负比例以该主题 review_entities 为分母。"
                             if baseline_status == "complete" else "尚未读全报告主题比较基线；缺失、空节点或容量不足不等于相关问题为零，可继续自主查询。")
                            + "不能将只看低星评论的发现外推全体。规则提及数不是已纠错的投诉排名；继续读取原文，可纠正规则误判，不要求采纳规则结论。",
                }})
                if baseline_status == "complete" and dual_ranking_size(question):
                    computed_ranking = build_report_ranking(aspect_rows, dual_ranking_size(question), aspect_sources)
                    if computed_ranking is not None:
                        if report_ranking_requested(question):
                            ranking_action = computed_ranking
                        else:
                            observations.append({"report_ranking_context": report_ranking_context(computed_ranking)})
            step(0, "completed")
            step(1, "running")
            planner = self.service.planner
            if planner is None and ranking_action is None:
                raise AgentError("llm_unavailable", "问答模型尚未配置。", http_status=503)
            model_type = ResearchModel if simple_count_question(question) and not classification_requested(question) else GroundedResearchModel
            model = model_type(planner.runtime) if planner else None
            if resumed:
                if resumed.get("action", {}).get("resume_version") != "research-v2":
                    raise AgentError("resume_unavailable", "分析记录版本已更新，请重新提问。", http_status=409)
                restoring = True
                sources.clear(); requests.clear(); observations.clear(); source_number = 0
                prior_requests = set()
                for old in resumed["sources"].values():
                    req = old["source_request"]
                    if old["report_id"] != report_id:
                        raise AgentError("source_changed", "之前的资料不属于当前报告，请重新提问。", http_status=409)
                    key = sha256_value(req)
                    if key not in prior_requests:
                        result = call(req["tool"], req["payload"])
                        self._verify_source_result(old, result)
                        register(req["tool"], req["payload"], result)
                        prior_requests.add(key)
                for sid, old in resumed["sources"].items():
                    if sid not in sources or sources[sid].get("record_id") != old.get("record_id") or sources[sid]["source_sha256"] != old["source_sha256"]:
                        raise AgentError("source_changed", "之前的原文已发生变化，请重新提问。", http_status=409)
                    if "in_model" in old:
                        sources[sid]["in_model"] = old["in_model"]
                resume_state = resumed["action"]
                semantic_jobs[:] = resume_state.get("semantic_jobs", [])
                observations[:] = resume_state.get("observations", observations)
                previous_calls = resume_state.get("cumulative_model_calls", 0)
                current_tasks = {row["task_id"]: row for row in explicit_requirements(question)}
                current_tasks.update({row["task_id"]: row for row in resume_state.get("answer_tasks", [])
                                      if row["task_id"] not in current_tasks})
                metadata["answer_tasks"] = list(current_tasks.values())
                if getattr(model, "grounded", False):
                    model.answer_tasks = metadata["answer_tasks"]
                restoring = False
                persist()
                progress("已恢复上次分析", "completed", detail="重新核对了报告与原文，继续处理未完成部分")
                for job in list(semantic_jobs):
                    if not job["complete"]:
                        classify(job["args"], job["job_id"])
                        # New classified evidence requires a fresh answer;
                        # previously checked partial totals are historical.
                        resume_state = {"type": "research_progress"}
                        persist()
            if getattr(model, "grounded", False) and ranking_action is None:
                if not model.answer_tasks:
                    model.answer_tasks = explicit_requirements(question)
                    try:
                        invoke_model(model.plan_question, question=question)
                    except AgentError as error:
                        if error.code != "llm_contract_invalid":
                            raise
                        # Planning cannot expand or erase the original question.
                        metadata["question_plan_fallback"] = True
                metadata["answer_tasks"] = model.answer_tasks
                observations.append({"answer_tasks": model.answer_tasks,
                    "instruction": "原文调查和最后回答应覆盖这些用户要求；不要把用户没问的未知项当成未完成。"})
            finding_revisions = 0
            review_count = 0
            consecutive_format_errors = 0
            delivery_corrections = 0
            style_corrections = 0
            style_revision_pending = False
            repaired_action = ({"type": "findings", "findings": resume_state["findings"]}
                               if resumed and resume_state.get("type") == "answer_progress" else None)
            if dual_ranking_size(question):
                observations.append({"answer_requirements": f"请实际交付两份独立榜单，优点和缺点各前{dual_ranking_size(question)}，使用明确标题和编号/排名列；读取相关完整原文核对含义并说明排序依据。"})
            action: dict[str, Any] = {}
            while True:
                if ranking_action is not None:
                    action = ranking_action
                    metadata["rankings"] = action.pop("rankings")
                    progress("已计算正面与负面排名", "completed", detail="已核对完整表格，数量相同并列")
                    break
                kwargs = {"question": question, "report_context": report_context, "history": history,
                          "observations": observations, "tools": tools, "sources": sources}
                if style_revision_pending:
                    kwargs["tools"] = []
                elif trace.get("model_calls", 0) >= max(2, MAX_MODEL_CALLS - (6 if getattr(model, "grounded", False) else 4)):
                    kwargs["tools"] = []
                    observations.append({"budget_feedback": "本轮调查即将结束。请停止取数，依据已经核对的资料回答；未核对部分只说明会影响本题判断的缺口，不能声称全量复核。"})
                try:
                    action = repaired_action if repaired_action is not None else invoke_model(model.next_action, **kwargs)
                    repaired_action = None
                except AgentError as error:
                    failed_stage = trace.get("calls", [{}])[-1].get("failure_stage")
                    reparable_stage = failed_stage in {
                        "action_parse", "action_shape", "action_type", "action_fields", "tool_name", "tool_args",
                        "answer_shape", "citation_link", "citation_marker", "citation_id", "citation_quote", "citation_quote_kind", "review_group",
                        "finding_required", "finding_shape", "finding_evidence",
                    }
                    if (error.code in {"llm_contract_invalid", "research_action_invalid"}
                            and reparable_stage and consecutive_format_errors < 1):
                        consecutive_format_errors += 1
                        last_diagnostics = trace.get("calls", [{}])[-1]
                        observations.append({"validation_feedback": validation_feedback(failed_stage, kwargs["tools"]),
                                             "invalid_fields": last_diagnostics.get("schema_errors", [])})
                        continue
                    raise
                # A valid action ends the consecutive-error streak. Later
                # repairs still consume the unchanged total model-call budget.
                consecutive_format_errors = 0
                if action["type"] == "tool":
                    name = action["tool_name"]
                    if name == "classify_report_reviews" and classification_requested(question):
                        classify(action["args"])
                        continue
                    if name not in RESEARCH_TOOLS or "report_id" in action["args"]:
                        raise AgentError("scope_mismatch", "本次只能查询所选报告的资料。", http_status=409)
                    payload = {**action["args"], "report_id": report_id}
                    try:
                        result = call(name, payload)
                    except AgentError as error:
                        if error.code in {"report_section_not_found", "research_result_too_large", "report_review_too_large", "invalid_request", "evidence_unavailable", "evidence_group_not_found"}:
                            observations.append({"tool_name": name, "args": action["args"],
                                                 "tool_error": {"code": error.code, "message": error.message},
                                                 "next_step": "调整读取节点、筛选条件或分页后继续。不要重复相同请求，也不要把读取失败解释为没有相关评论。"})
                            continue
                        raise
                    register(name, payload, result)
                    continue
                if action["type"] == "findings":
                    review_records = sum(bool(s.get("record_id")) for s in sources.values())
                    if needs_aspect_baseline(question) and not review_records:
                        finding_revisions += 1
                        if finding_revisions > 2:
                            raise AgentError("answer_incomplete", "相关评论还没有核对完成，已保存本次分析。", http_status=409, retryable=True)
                        observations.append({"finding_feedback": "先读取相关完整评论后再核对发现；主题标签和整机星级不能代替用户对功能的实际评价。"})
                        continue
                    def grounding_checkpoint(candidate):
                        persist(candidate)
                    completed, feedback = finish_grounded_answer(model=model, findings=action["findings"],
                        kwargs=kwargs, invoke=invoke_model, checkpoint=grounding_checkpoint, metadata=metadata,
                        review_groups=action.get("review_groups"), allow_partial=finding_revisions >= 2,
                        resume=resume_state if resumed and resume_state.get("type") == "answer_progress" else None)
                    if completed is not None:
                        action = completed
                        break
                    finding_revisions += 1
                    if finding_revisions > 2:
                        raise AgentError("answer_review_failed", "部分发现仍未能准确核对，已保存本次分析供继续修正。", http_status=409, retryable=True)
                    observations[:] = [item for item in observations if "finding_feedback" not in item]
                    observations.append({"findings_to_repair": action["findings"], "finding_feedback": feedback,
                                         "instruction": "保留已成立发现，只修正指出的问题；可以继续读取缺少的原文。删掉一项不能漏答用户的关键子问题。"})
                    continue
                style_revision_pending = False
                preview_dropped = any(item.get("preview_dropped") is True for item in trace.get("calls", []))
                if preview_dropped:
                    metadata["preview_dropped"] = True
                    metadata["preview_dropped_source_ids"] = list(dict.fromkeys(
                        source_id for item in trace.get("calls", []) for source_id in item.get("preview_dropped_source_ids", [])
                        if isinstance(source_id, str) and source_id in sources
                    ))
                    if not any("citation_preview_feedback" in item for item in observations):
                        observations.append({"citation_preview_feedback": "有不符合逐字原文的可选引用预览已移除。请对照完整来源核对答案正文中的引号句、数字和解释；预览被移除不证明正文正确，缺少依据的说法必须修订。"})
                # A distinct review checks the answer's relevance and interpretation;
                # it is not confused with cryptographic source validation below.
                if action["status"] == "answered":
                    self.turns.checkpoint(actor, run_id, action, sources, metadata.get("answer_checks", []))
                    review_records = sum(bool(s.get("record_id")) for s in sources.values())
                    corrections = delivery_feedback(question, action["answer"], review_records=review_records)
                    if needs_aspect_baseline(question) and not review_records and not dual_ranking_size(question):
                        corrections.append("本轮尚未读取完整评论。请用 query_report_reviews 读取相关评论全文后核对主题解释；报告主题表、精选片段和历史助手摘要不能代替本轮完整原文。无法取得原文时如实使用 insufficient_evidence，不能声称已用原文核对。")
                    record_check("delivery", corrections, action["answer"])
                    if corrections:
                        if delivery_corrections >= 2:
                            raise AgentError("answer_incomplete", "这次回答还没有答全你的问题，请重试。", http_status=409, retryable=True)
                        delivery_corrections += 1
                        correction("delivery_feedback", corrections, action["answer"])
                        continue
                    editorial = style_feedback(question, action["answer"])
                    record_check("style", editorial, action["answer"])
                    if editorial and style_corrections < 1:
                        style_corrections += 1
                        style_revision_pending = True
                        correction("style_feedback", editorial, action["answer"])
                        continue
                complex_answer = preview_dropped or style_corrections > 0 or not simple_count_question(question) or any(s["kind"] == "review" for s in sources.values())
                if preview_dropped or (complex_answer and action["status"] == "answered"):
                    observations[:] = [item for item in observations if "draft_answer" not in item]
                    feedback = invoke_model(model.review_answer, **kwargs, answer_action=action)
                    metadata["answer_review"] = feedback
                    record_check("review", feedback["feedback"], action["answer"], status=feedback["status"])
                    self.turns.checkpoint(actor, run_id, action, sources, metadata.get("answer_checks", []))
                    review_count += 1
                    if feedback["status"] == "revise":
                        if review_count >= 3 or trace.get("model_calls", 0) > MAX_MODEL_CALLS - 2:
                            fallback = build_report_ranking(aspect_rows, 5, aspect_sources) if aspect_rows and baseline_status == "complete" and re.search(r"负面|差评|缺点|抱怨", question) else None
                            if fallback:
                                negative = fallback["rankings"][1]["rows"]
                                text = "报告中负面提及最多的主题如下，按评论数排序：\n\n" + "\n".join(f"- {row['label']}：{row['count']}条。" for row in negative)
                                action = {"type": "answer", "status": "insufficient_evidence", "answer": text + "\n\n发生场景的解释还没有准确完成，这次先保留已确认的主题和数量。\n\n" + " ".join(f"[{sid}]" for sid in aspect_sources), "citations": fallback["citations"], "limitations": []}
                                metadata["partial_answer"] = True
                                break
                            raise AgentError("answer_review_failed", "这次仍有解释未能准确核实，答案已保存供继续修正。", http_status=409, retryable=True)
                        repaired_action = invoke_model(model.repair_answer, **kwargs, answer_action=action, feedback=feedback["feedback"])
                        continue
                break
            step(1, "completed")
            incomplete_jobs = [job for job in semantic_jobs if not job["complete"]]
            if incomplete_jobs:
                action["status"] = "insufficient_evidence"
                metadata["partial_answer"] = True
                latest = incomplete_jobs[-1]
                action["answer"] += f"\n\n已逐条分析{latest['processed']}/{latest['total']}条评论，以上统计只对应已分析部分。可以继续分析剩余评论。"
            metadata["semantic_jobs"] = semantic_jobs
            step(2, "running")
            refs = action["citations"]
            used = {ref["source_id"]: sources[ref["source_id"]] for ref in refs}
            # Re-read every cited query/section, once per request; hashes include full data.
            checked: set[str] = set()
            for source in used.values():
                source_request = source["source_request"]
                key = sha256_value(source_request)
                if key not in checked:
                    fresh = call(source_request["tool"], source_request["payload"], 2)
                    self._verify_source_result(source, fresh)
                    checked.add(key)
            step(2, "completed")
            step(3, "running")
            content = action["answer"]
            citations = []
            for index, ref in enumerate(refs, 1):
                source = used[ref["source_id"]]
                citation_id = "cit_" + sha256_value({"source": source["source_request"], "hash": source["source_sha256"],
                                                    "record_id": source.get("record_id"), "evidence_id": source.get("evidence_id")})[7:31]
                citation = {key: source[key] for key in ("report_id", "snapshot_id", "report_identity_sha256", "snapshot_identity_sha256", "source_sha256", "source_request", "label", "record_id", "evidence_id") if key in source}
                citation.update({"citation_id": citation_id, "source_type": source["kind"],
                                 "href": f"/wenquan/api/v1/sessions/{quote(session_id, safe='')}/sources/{citation_id}"})
                if ref.get("quote"):
                    citation["quote"] = ref["quote"]
                citation["source_href"] = citation["href"]
                citation["report_href"] = report_href(report_id)
                content = content.replace("[" + ref["source_id"] + "]", "[" + str(index) + "]")
                citations.append(citation)
            if len(content) > 20000:
                raise AgentError("answer_too_large", "回答过长，尚未整理完成，请按主题分开提问。", http_status=409)
            metadata.update({"contract": "voxquarry-wenquan-research-answer/v1", "citations": citations,
                             "limitations": action["limitations"], "report": report["report"], "snapshot": report["snapshot"],
                             "research": {"model_calls": trace.get("model_calls", 0), "read_requests": len(requests),
                                          "review_records_read": len({s.get("record_id") for s in sources.values() if s.get("record_id")}),
                                          "report_unchanged": True},
                             "duration_ms": round((time.monotonic() - started) * 1000),
                             "cumulative_model_calls": previous_calls + trace.get("model_calls", 0),
                             "can_resume": action["status"] != "answered" and can_resume()})
            if action.get("review_groups"):
                metadata["review_groups"] = [{**group, "members": [{**member, "record_id": sources[member["source_id"]].get("record_id")} for member in group["members"]]} for group in action["review_groups"]]
            step(3, "completed")
            self.turns.finish(actor, run_id, status="completed" if action["status"] == "answered" else "blocked", content=content, metadata=metadata)
        except AgentError as error:
            if error.code == "run_cancelled":
                return
            metadata.pop("contract", None)
            metadata.update({"claims": [], "citations": [], "error": {"code": error.code, "message": error.message, "retryable": error.retryable},
                             "duration_ms": round((time.monotonic() - started) * 1000)})
            if error.safe_next_step:
                metadata["error"]["safe_next_step"] = error.safe_next_step
            metadata["research_diagnostics"] = {"calls": trace.get("calls", [])}
            metadata["can_resume"] = can_resume() and error.code not in {"source_changed", "scope_mismatch", "report_forbidden"}
            blocked = error.code in {"report_selection_required", "report_not_found", "report_forbidden", "report_not_ready", "scope_mismatch", "research_data_unavailable", "research_source_unavailable"}
            self.turns.finish(actor, run_id, status="blocked" if blocked else "failed", content=error.message, metadata=metadata)
        except Exception:
            metadata.pop("contract", None)
            metadata.update({"claims": [], "citations": [], "can_resume": can_resume(), "error": {"code": "answer_failed", "message": "回答处理失败，请重试。", "retryable": True}})
            self.turns.finish(actor, run_id, status="failed", content="回答处理失败，请重试。", metadata=metadata)
