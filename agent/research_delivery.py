"""Delivery facts, independent of the model's chosen research method.

Receipts can establish unavailable inputs or unfinished requested operations.
They cannot establish that an analysis is good, or infer a mandatory workflow
from a question's wording. A useful answer and its files survive these gaps.
"""
from __future__ import annotations


def delivery_issues(checkpoint):
    issues = []
    outcomes = {item.get("request_key") or key: item for key, item in
                zip(checkpoint.get("waiting_operation_ids", []), checkpoint.get("collection_outcomes", []))}
    outcomes.update({item["request_key"]: item for item in checkpoint.get("collection_outcomes", []) if item.get("request_key")})
    for key, operation in checkpoint.get("operations", {}).items():
        result = outcomes.get(key) or operation.get("result") or {}
        if not result.get("data", {}).get("ready"):
            issues.append({"kind": "collection", "operation_id": key,
                           "status": result.get("status", "unknown")})
    report = checkpoint.get("report_job", {}).get("job", {})
    if report:
        result = next((item.get("job", {}) for item in reversed(checkpoint.get("report_outcomes", []))
                       if item.get("job", {}).get("job_id") == report.get("job_id")), report)
        if result.get("status") != "completed":
            issues.append({"kind": "report", "job_id": report.get("job_id"),
                           "status": result.get("status", "unknown")})
    if checkpoint.get("report_scope_wait"):
        issues.append({"kind": "report", "status": "waiting_data"})
    for item in checkpoint.get("report_delivery", {}).get("failures", []):
        issues.append({"kind": "file", **item})
    # Only flag known missing inputs. No requirement to call a particular
    # catalog, classifier, Python wrapper or aggregate endpoint before delivery.
    for dataset, item in checkpoint.get("raw_data_evidence", {}).get("opened", {}).items():
        if item.get("complete") is False:
            issues.append({"kind": "data", "dataset_id": dataset, "status": "incomplete"})
    from .research_data_goal import coverage
    for plan in coverage(checkpoint.get('raw_data_evidence', {})):
        if not plan['complete']:
            issues.append({'kind': 'data', 'status': 'incomplete', **plan})
    return issues


def delivery_notice(issues):
    labels = {"collection": "部分采集尚未成功完成", "report": "分析报告报告尚未生成完成",
              "data": "部分数据尚未取得", "file": "部分下载格式未生成"}
    missing = list(dict.fromkeys(labels[item["kind"]] for item in issues))
    coverage = [f"原始数据已读取 {i['opened_batch_count']}/{i['batch_count']} 个批次、{i['opened_source_count']}/{i['total_sources']} 个来源，已知不可用来源 {i['unavailable_count']} 个"
                for i in issues if i['kind'] == 'data' and 'opened_batch_count' in i]
    return "；".join([*missing, *coverage]) + "。以下保留已完成的分析，相关缺口不代表已补齐。" if missing else ""
