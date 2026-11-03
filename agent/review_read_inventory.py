"""Deterministic counts of frozen review pages already read in this run."""

from collections import Counter


def review_read_inventory(report_id, data, saved_sources):
    query = data.get("query_sha256")
    fingerprint = (data.get("provenance") or {}).get("source_fingerprint")
    total = data.get("intersection_count")
    if not query or not fingerprint or type(total) is not int or total < 0:
        return None
    pages = [data]
    for item in saved_sources:
        source = item["source"]
        page = source.get("content")
        if (source.get("source_type") == "report" and source.get("report_id") == report_id
                and (source.get("provenance") or {}).get("tool") == "query_report_reviews"
                and isinstance(page, dict) and page.get("query_sha256") == query
                and (page.get("provenance") or {}).get("source_fingerprint") == fingerprint
                and page.get("intersection_count") == total):
            pages.append(page)
    records = {}
    for page in pages:
        for row in page.get("items") or []:
            identifier = row.get("review_occurrence_id")
            if not isinstance(identifier, str) or not identifier:
                return None
            fact = {key: value for key, value in row.items() if key != "source_id"}
            if identifier in records and records[identifier] != fact:
                return None
            records[identifier] = fact
    if len(records) > total:
        return None
    products = {}
    for row in records.values():
        key = (str(row.get("channel") or ""), str(row.get("source_product_id") or ""))
        group = products.setdefault(key, {"channel": key[0], "source_product_id": key[1],
            "count": 0, "written_count": 0, "ratings": Counter(), "unknown_rating_count": 0})
        group["count"] += 1
        group["written_count"] += row.get("has_text") is True
        rating = row.get("rating")
        if row.get("rating_valid") is True and type(rating) is int and 1 <= rating <= 5:
            group["ratings"][str(rating)] += 1
        else:
            group["unknown_rating_count"] += 1
    groups = [products[key] for key in sorted(products)]
    for group in groups:
        group["ratings"] = dict(sorted(group["ratings"].items()))
    return {"read_records": len(records), "matching_records": total, "complete": len(records) == total,
            "products": groups[:32], "omitted_products": max(0, len(groups) - 32),
            "query_sha256": query, "source_fingerprint": fingerprint,
            "definition": "仅统计本次研究已经读到的同报告、同快照、同筛选原评，按 review_occurrence_id 去重；complete=false 时只是已读子集。型号取 source_product_id，星级为整条评论评分，不是主题满意度。不同型号或筛选范围的比例不能直接比较并归因。"}
