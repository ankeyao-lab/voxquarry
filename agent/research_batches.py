"""Research integration for bounded semantic analysis; no separate executor."""
from __future__ import annotations

import copy
import json

from .contracts import AgentError, sha256_value
from .semantic_batches import DEFAULT_BATCH_SIZE, PROMPT_VERSION, SemanticBatchModel


def batch_tool(tools):
    query = copy.deepcopy(next(t["parameters"] for t in tools if t["name"] == "query_report_reviews"))
    for key in ("offset", "limit", "cursor"):
        query["properties"].pop(key, None)
    query["required"] = [key for key in query.get("required", []) if key in query["properties"]]
    return {"name": "classify_report_reviews", "description":
        "按固定类别逐条读取并分析筛选内全部有文字评论。程序连续分页、保存进度、去重计数；实际正负体验与比较、假设、不确定分开。用于真实投诉数量、语义统计、纠正报告排名。query可空对象；不要以低星代替负面。一次分析可能分多轮完成。",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["query", "categories"],
            "properties": {"query": query, "categories": {"type": "array", "minItems": 1, "maxItems": 12,
                "items": {"type": "object", "additionalProperties": False, "required": ["category_id", "definition"],
                    "properties": {"category_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$"},
                                   "definition": {"type": "string", "minLength": 1, "maxLength": 1200}}}}}}}


def run_batches(*, store, actor, session_id, question, report_id, runtime, args, call, register,
                invoke, budget_left, save_job, progress, sources, job_id=None):
    query = {**args["query"], "population": "entities",
             "filters": {**args["query"].get("filters", {}), "has_text": True}}
    first = call("query_report_reviews", {**query, "report_id": report_id, "offset": 0, "limit": DEFAULT_BATCH_SIZE})
    model_name = runtime._settings.model
    spec = {"question": question, "report": first["report"], "snapshot": first["snapshot"],
            "query": first["data"]["query"], "categories": args["categories"], "model": model_name,
            "prompt_version": PROMPT_VERSION, "source_provenance": first["data"]["provenance"],
            "query_sha256": first["data"]["query_sha256"]}
    job = (store.load_job(actor=actor, session_id=session_id, job_id=job_id, spec=spec) if job_id
           else store.create_job(actor=actor, session_id=session_id, spec=spec))
    bound = {"actor": actor, "session_id": session_id, "job_id": job["job_id"]}
    store.prepare_page(**bound, result=first)
    classifier = SemanticBatchModel(runtime)
    save_job(job, args)
    while not job["complete"] and budget_left() > 5:
        offset = job["next_offset"]
        payload = {**spec["query"], "report_id": report_id, "offset": offset, "limit": DEFAULT_BATCH_SIZE}
        result = first if offset == 0 else call("query_report_reviews", payload)
        prepared = store.prepare_page(**bound, result=result)
        classified = prepared["classification"]
        if not prepared["already_saved"]:
            if result["data"]["items"]:
                # Each attempted provider request, including failed ones, is
                # recorded by the same parent budget and cancellation wrapper.
                invocation = {"job": job, "result": result,
                    "_attempt_hook": lambda attempt: store.record_call(**bound, attempt_id=attempt)}
                try:
                    classified = invoke(classifier.classify_page, **invocation)
                except AgentError as error:
                    if error.code != "llm_contract_invalid" or budget_left() <= 4:
                        raise
                    try:
                        previous = json.loads(getattr(error, "raw_candidate", None) or "null")
                    except (ValueError, TypeError):
                        previous = None
                    info = getattr(error, "call_diagnostics", {})
                    feedback = {key: info[key] for key in ("failure_stage", "schema_errors", "classification_coverage", "classification_batch") if key in info}
                    classified = invoke(classifier.classify_page, **invocation,
                        previous=previous, format_feedback=feedback or {"error": error.message})
            else:
                classified = {"results": []}
            job = store.save_page(**bound, result=result, classification=classified)
        else:
            job = prepared["job"]
        register("query_report_reviews", payload, result)
        save_job(job, args)
        progress("逐条分析评论", "completed", detail=f"已分析{job['next_offset']}/{job['total']}条评论")
        # Keep all originals stored, with balanced examples in the next model
        # context. Membership below remains accessible by review_ids.
        summary = store.summary(**bound)
        member_ids = {m["record_id"] for c in summary["categories"] for m in c["members"]}
        selected = set()
        for category in summary["categories"]:
            groups = {}
            for member in category["members"]:
                groups.setdefault((member["experience"], member["polarity"]), []).append(member["record_id"])
            for ids in groups.values():
                selected.update(ids[:2] + ids[-1:])
        # Unclassified and contradictory records are not silently discarded.
        selected.update(row["record_id"] for row in summary["unknown"][:12])
        page_ids = {row["record_id"] for row in result["data"]["items"]}
        for source in sources.values():
            if source.get("record_id") in member_ids | page_ids:
                source["in_model"] = source["record_id"] in selected
        save_job(job, args)
    return job


def semantic_source(qa, actor, session_id, payload, *, include_reviews=False, offset=0,
                    check_active=None, on_progress=None, run_id=None, step_id=None):
    """Reload the cited immutable revision and verify every original page."""
    summary = qa.semantic.summary(actor=actor, session_id=session_id, job_id=payload["job_id"], revision=payload["revision"])
    spec = summary["spec"]
    if payload["report_id"] != spec["report"]["report_id"]:
        raise AgentError("source_changed", "分析对应的报告不一致。", http_status=409)
    originals = []
    for index, page in enumerate(summary["pages"], 1):
        if check_active:
            check_active()
        request = page["source_request"]
        context = {"session_id": session_id}
        if run_id is not None:
            context.update(run_id=run_id, step_id=step_id)
        fresh = qa.invoke(actor, request["tool"], request["payload"], **context)
        if check_active:
            check_active()
        if (fresh["report"] != spec["report"] or fresh["snapshot"] != spec["snapshot"]
                or sha256_value(fresh["data"]) != page["source_sha256"]):
            raise AgentError("source_changed", "已分析的原文发生变化，请重新分析。", http_status=409)
        if include_reviews:
            originals.extend(fresh["data"]["items"])
        if on_progress:
            on_progress(index, len(summary["pages"]))
    result = {"report": spec["report"], "snapshot": spec["snapshot"], "data": summary}
    if include_reviews:
        end = min(offset + DEFAULT_BATCH_SIZE, len(originals))
        result["data"] = {**summary, "items": originals[offset:end]}
        result["page"] = {"offset": offset, "returned": end - offset, "total": len(originals),
                          "has_more": end < len(originals), "next_offset": end if end < len(originals) else None}
    return result
