"""Explicit owner-only export of Agent runs for reproducible evaluation."""
from __future__ import annotations

import copy
from typing import Any

from .agent_trace import AgentTraceStore
from .contracts import AgentError, IdentityContext, sha256_value


EVAL_EXPORT_CONTRACT = "wenquan-agent-eval-export/v1"
EVAL_CASE_CONTRACT = "wenquan-agent-eval-case/v1"


class EvalExporter:
    def __init__(self, store: AgentTraceStore):
        self.store = store

    def _content(self, actor, trace_id, descriptor, field, include_content):
        result = copy.deepcopy(descriptor)
        if result is None:
            return None
        result["content"] = "omitted"
        if include_content and result.get("payload_ref"):
            try:
                artifact = self.store.read_payload(actor, trace_id, result["payload_ref"], include_content=True)
            except AgentError as error:
                if error.code != "trace_payload_unavailable":
                    raise
                result["content"] = "unavailable"
            else:
                result["value"] = artifact["data"].get(field)
                result["content"] = "included"
                result["artifact_sha256"] = artifact["sha256"]
        return result

    def export(self, actor: IdentityContext, trace_ids: list[str], *, include_content: bool = False) -> dict[str, Any]:
        if not isinstance(actor, IdentityContext):
            raise AgentError("identity_invalid", "导出需要已验证的用户身份", http_status=401)
        if (type(include_content) is not bool or not isinstance(trace_ids, list) or not 1 <= len(trace_ids) <= 100
                or any(not isinstance(value, str) for value in trace_ids) or len(set(trace_ids)) != len(trace_ids)):
            raise AgentError("invalid_request", "导出参数无效")
        # Authorize the entire selection before reading any private artifacts.
        traces = [self.store.get_trace(actor, trace_id) for trace_id in trace_ids]
        cases = []
        for trace in traces:
            trace_id = trace["trace_id"]
            events, after_seq = [], 0
            # Freeze the export at this observed revision even if the run grows.
            through_seq = trace["last_seq"]
            while after_seq < through_seq:
                page = self.store.list_events(actor, trace_id, after_seq=after_seq, limit=1000)
                selected = [event for event in page["items"] if event["seq"] <= through_seq]
                if not selected:
                    raise AgentError("trace_export_incomplete", "日志事件不完整，暂时不能导出", http_status=409)
                events.extend(selected)
                after_seq = selected[-1]["seq"]
            if [event["seq"] for event in events] != list(range(1, through_seq + 1)):
                raise AgentError("trace_export_incomplete", "日志事件序号不完整", http_status=409)
            if include_content:
                for event in events:
                    if event.get("payload_ref"):
                        try:
                            event["payload"] = self.store.read_payload(actor, trace_id, event["payload_ref"], include_content=True)["data"]
                        except AgentError as error:
                            if error.code != "trace_payload_unavailable":
                                raise
                            event["payload_status"] = "expired_or_unavailable"
            human = []
            for feedback in trace["human_feedback"]:
                item = copy.deepcopy(feedback)
                item["comment"] = self._content(actor, trace_id, item["comment"], "comment", include_content)
                human.append(item)
            case = {
                "contract": EVAL_CASE_CONTRACT, "case_id": "eval_" + trace_id,
                "trace_id": trace_id, "owner_user_id": actor.owner_user_id,
                "session_id": trace["session_id"], "run_id": trace["run_id"],
                "through_seq": through_seq, "status": trace["status"], "created_at": trace["created_at"],
                "question": self._content(actor, trace_id, trace["question"], "question", include_content),
                "answer": self._content(actor, trace_id, trace["answer"], "answer", include_content),
                "model_config": trace["model_config"], "data_manifest": trace["data_manifest"],
                "model_events": [event for event in events if event["event_type"].split(".")[0] == "model"],
                "tool_events": [event for event in events if event["event_type"].split(".")[0] == "tool"],
                "source_events": [event for event in events if event["event_type"].split(".")[0] in {"data", "source"}],
                "events": events, "human_feedback": human,
                "label_policy": "Only record_feedback user submissions are human labels; model reviews are diagnostics.",
                "reproduction": {"has_question_content": False, "full_content_requested": include_content,
                                 "requires_source_manifest_resolution": True, "deterministic_replay_guaranteed": False},
            }
            case["reproduction"]["has_question_content"] = case["question"]["content"] == "included"
            case["case_sha256"] = sha256_value(case)
            cases.append(case)
        result = {"contract": EVAL_EXPORT_CONTRACT, "owner_user_id": actor.owner_user_id,
                  "include_content": include_content, "cases": cases}
        result["export_sha256"] = sha256_value(result)
        return result
