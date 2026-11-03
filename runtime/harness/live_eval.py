#!/usr/bin/env python3
"""Run the frozen EGO regression with DeepSeek or Qwen through Harness.

Default: validate and materialize local inputs, without opening a socket or
calling a model. --live starts a temporary broker on 127.0.0.1:19191 and submits
one run to an already configured manager on 127.0.0.1:19291. It does not manage
Docker, the 8088 application, account identities, collection, or web search.

Inputs (outside Git):
  --report-json: the complete saved report JSON, not a prose export.
  --report-root: directory containing snapshots/<snapshot_id>/manifest.json and
                 review_fact.preview.jsonl.gz used by that report.
  --data-root: frozen source root with data/runs/<job_id>/lowes/{reviews.csv,
               validation_report.json}; channel raw JSONL is optional.
  --selected-runs-json: [{"job": <exact job metadata>, "run_dir": <relative dir>}].
                       Only the already authorized job is accepted; no jobs DB.
  --config: owner-only 0600 JSON with manager_token, VOXQUARRY_LLM_API_KEY and
            VOXQUARRY_LLM_BASE_URL. No environment or account auto-discovery.
  --out: NEW directory outside the repository; created 0700, files 0600.
  --input-mode: report (default) associates the report and frozen dataset;
                discovery associates neither, so Harness locates the materials;
                dataset associates only the same frozen dataset. All modes can
                access only the authorized report and single EGO source below.
                Registered category definitions and deterministic dataset
                analysis are local; private research files may be generated,
                but shared formal reports cannot be created or modified.

Output includes preflight.json, result.json, answer.md, eval-export.json,
sources.json, events.jsonl and private SQLite/payload files. Model reasoning and
credentials are removed by the existing AgentTraceStore sanitizer. This is a
single real-model regression, not proof of LDAP isolation or overall quality.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REPORT_ID = "rpt_42ca4618321e3d88090d344f"
SNAPSHOT_ID = "snap_990150d6983ff76ad5323afb"
JOB_ID = "566e7a02dea1"
CATEGORY_ID = "walk_behind_mower"
QUESTION = "主要负面评价有哪些？在什么场景下发生的？"
MODEL = "qwen3.8-max"
MODEL_IDS = {MODEL: "qwen3.8-max-0902", "deepseek-v4.1-flash": "deepseek-v4.1-flash-1017"}
PROVIDER = "https://ws-yxqbkf8tf15bgd2u.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
MANAGER = "http://127.0.0.1:19291"
INPUT_MODES = ("report", "discovery", "dataset")


def read_json(path, *, private=False, maximum=32 * 1024**2):
    """No symlinks, hardlinks, unbounded reads, or credential-bearing errors."""
    path = Path(path).absolute()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("input_file_invalid")
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("config_must_be_owner_only_0600")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("input_file_too_large")
        return json.loads(raw)
    finally:
        os.close(fd)


def load_config(path):
    value = read_json(path, private=True, maximum=16384)
    keys = {"manager_token", "VOXQUARRY_LLM_API_KEY", "VOXQUARRY_LLM_BASE_URL"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("config_fields_invalid")
    if value["VOXQUARRY_LLM_BASE_URL"].rstrip("/") != PROVIDER:
        raise ValueError("provider_is_not_authorized_PSG_workspace")
    for key in ("manager_token", "VOXQUARRY_LLM_API_KEY"):
        token = value[key]
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or any(c.isspace() for c in token):
            raise ValueError("credential_shape_invalid")
    return value


def make_output(path, inputs):
    output = Path(path).absolute()
    resolved = output.resolve()
    # Neither reports, raw data, credentials nor run logs belong in a checkout.
    if resolved.is_relative_to(ROOT) or any(resolved == p.resolve() or p.resolve().is_relative_to(resolved)
                                            for p in inputs):
        raise ValueError("output_must_be_separate_from_repository_and_inputs")
    parent = output.parent.resolve(strict=True)
    output = parent / output.name
    output.mkdir(mode=0o700, exist_ok=False)
    return output


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


class SingleReport:
    """A read-only report store adapter: no discovery or writes in source roots."""
    def __init__(self, root, report):
        self.root, self.report = Path(root).resolve(strict=True), report

    def get_report(self, report_id):
        return copy.deepcopy(self.report) if report_id == self.report["report_id"] else None

    def is_archived(self, report_id):
        return False

    def display_name_metadata_for_report(self, report):
        title = report.get("title") or report["report_id"]
        return {"display_name": title, "original_title": title, "custom_display_name": None,
                "version": 0, "updated_at": None}

    def list_reports(self, **kwargs):
        from reporting.storage import ReportStore
        return [ReportStore._report_card(self, self.report)]


class FrozenReportTransport:
    dependency_status = "ready"

    def __init__(self, report_store):
        from reporting.agent_tools import AgentToolService
        self.api = AgentToolService(report_store, None)

    def call(self, tool_name, payload, identity, definition):
        from agent.contracts import AgentError
        from reporting.agent_tools import AgentToolError, ToolAuthority
        allowed = {"zhuojian." + name + "@v1" for name in
                   ("list_reports", "get_report_section", "query_report_reviews", "aggregate_report_reviews")}
        if tool_name not in allowed:
            raise AgentError("eval_operation_disabled", "此次评测只读取已冻结报告和评论", http_status=403)
        authority = ToolAuthority(identity.authority_id, identity.owner_user_id, frozenset({REPORT_ID}))
        try:
            return self.api.invoke(tool_name, payload, authority)
        except AgentToolError as error:
            raise AgentError(error.code, error.message, http_status=error.http_status) from error

    def report_workflow(self, *args, **kwargs):
        from agent.contracts import AgentError
        raise AgentError("eval_operation_disabled", "此次评测不创建或修改共享分析报告报告，可分析冻结数据并保存私有研究文件", http_status=403)


class FrozenDatasetClient:
    def __init__(self, data_root, output, selected):
        from reporting.wenquan_datasets import WenquanDatasets
        self.api = WenquanDatasets(data_root, output / "datasets")
        self.selected = selected
        self.dataset_id = None
        self.available_ids = set()
        self.prepared = {}

    def call(self, actor, operation, payload):
        from agent.contracts import AgentError
        from reporting import source
        from reporting.wenquan_datasets import DatasetError
        if not isinstance(payload, dict):
            raise AgentError("invalid_request", "数据请求参数无效", http_status=422)
        args = dict(payload)
        try:
            if operation == 'prepare':
                # The evaluation remains confined to its pre-authorized source;
                # this adapts the package contract without enabling discovery
                # against a production catalog or accepting arbitrary IDs.
                allowed = {'scope', 'origin', 'catalog_id', 'max_batches'}
                if set(args) - allowed or type(args.get('max_batches', 4)) is not int or not 1 <= args.get('max_batches', 4) <= 8:
                    raise AgentError('invalid_request', '数据准备参数无效', http_status=422)
                if args.get('catalog_id'):
                    prepared = self.prepared.get(args['catalog_id'])
                    if prepared is None or args.get('scope', prepared['scope']) != prepared['scope'] or args.get('origin', 'auto') not in {'auto', 'snapshot'}:
                        raise AgentError('eval_operation_disabled', '目录不在本次冻结评测范围', http_status=403)
                    return prepared
                manifest = self.call(actor, 'open', {k: v for k, v in args.items() if k in {'scope', 'origin'}})
                jobs = sorted({item['job']['id'] for item in manifest['inputs']})
                catalog_id = 'cat_eval_' + manifest['dataset_id'].removeprefix('ds_')
                binding = {'catalog_id': catalog_id, 'scope': args.get('scope', {}), 'origin': 'snapshot',
                    'total': len(jobs), 'total_sources': len(jobs), 'unavailable_count': 0, 'batch_count': 1}
                prepared = {'status': 'ready', 'catalog_id': catalog_id, 'scope': binding['scope'], 'origin': 'snapshot',
                    'catalog_binding': binding, 'datasets': [{'dataset_id': manifest['dataset_id'],
                        'manifest_sha256': manifest['manifest_sha256'], 'batch_id': 'batch_eval', 'source_coverage': {'complete': True, 'unavailable_count': 0}}],
                    'coverage': {'requested_sources': len(jobs), 'available_sources': len(jobs), 'prepared_sources': len(jobs),
                        'unavailable_count': 0, 'total_batches': 1, 'prepared_batches': 1, 'complete': True},
                    'pending_batch_ids': [], 'failed_batches': [], 'next': None}
                self.prepared[catalog_id] = prepared
                return prepared
            if operation == "definition":
                from reporting.research_definition import get_research_definition
                if set(args) != {"category_id"}:
                    raise AgentError("invalid_request", "分析定义请求参数无效", http_status=422)
                category = source._job_category_id(self.selected[0]["job"]) if len(self.selected) == 1 else None
                if category != CATEGORY_ID or args["category_id"] != category:
                    raise AgentError("eval_operation_disabled", "此次评测只读取指定EGO产品的真实品类定义", http_status=403)
                return get_research_definition(category)
            if operation in {"catalog", "open"}:
                if args.pop("origin", "auto") not in {"auto", "snapshot"} or set(args) - {"scope"}:
                    raise AgentError("eval_operation_disabled", "此次评测只读取指定冻结数据", http_status=403)
                result = getattr(self.api, operation)(args.get("scope"), selected_runs=self.selected)
                if operation == "open":
                    self.available_ids.add(result["dataset_id"])
                return result
            if not isinstance(args.get("dataset_id"), str) or args["dataset_id"] not in self.available_ids:
                raise AgentError("dataset_not_found", "不在本次评测的数据中", http_status=404)
            dataset_id = args.pop("dataset_id")
            if operation == "analyze":
                from reporting.dataset_analysis import analyze_dataset
                if set(args) - {"definition_id"}:
                    raise AgentError("invalid_request", "分析请求参数无效", http_status=422)
                return analyze_dataset(self.api, dataset_id, **args)
            if operation not in {"manifest", "query", "aggregate"}:
                raise AgentError("eval_operation_disabled", "此次数据操作未开放", http_status=403)
            return getattr(self.api, operation)(dataset_id, **args)
        except DatasetError as error:
            raise AgentError(error.code, error.message, http_status=error.http_status) from error

    def export(self, actor, dataset_id, shard, *, source_type="normalized_review"):
        from agent.contracts import AgentError
        if dataset_id not in self.available_ids:
            raise AgentError("dataset_not_found", "不在本次评测的数据中", http_status=404)
        return b"".join(self.api.export(dataset_id, shard=shard, source_type=source_type))


class DisabledExternalTools:
    def call(self, *args, **kwargs):
        from agent.contracts import AgentError
        raise AgentError("eval_operation_disabled", "此次评测不联网搜索或采集", http_status=403)

    search = fetch = call


def input_selection(args, data):
    mode = getattr(args, "input_mode", "report")
    if mode not in INPUT_MODES:
        raise ValueError("input_mode_invalid")
    return {"input_mode": mode, "selected_materials": {
        "report_ids": [REPORT_ID] if mode == "report" else [],
        "dataset_ids": [data.dataset_id] if mode in {"report", "dataset"} else []}}


def preflight(args, output):
    from reporting.research_data import FrozenReviewData
    report = read_json(args.report_json)
    if report.get("report_id") != REPORT_ID or report.get("data_snapshot_id") != SNAPSHOT_ID:
        raise ValueError("report_not_in_authorized_regression")
    selected = read_json(args.selected_runs_json)
    if not isinstance(selected, list) or len(selected) != 1 or not isinstance(selected[0], dict):
        raise ValueError("exactly_one_selected_run_required")
    job = selected[0].get("job", {})
    if (job.get("id") != JOB_ID or str(job.get("brand", "")).upper() != "EGO"
            or job.get("model") != "LM2236SP" or job.get("channel") != "lowes"):
        raise ValueError("data_not_in_authorized_regression")
    data = FrozenDatasetClient(args.data_root, output, selected)
    manifest = data.call(None, "open", {})
    data.dataset_id = manifest["dataset_id"]
    counts = manifest["record_counts"]
    if counts["review_entities"] != 1717 or counts["written_entities"] != 807:
        raise ValueError("authorized_review_counts_changed")
    total = 0
    for shard in manifest["shards"]:
        for line in data.export(None, data.dataset_id, shard["name"]).splitlines():
            fact = json.loads(line)
            host = (urlsplit(fact.get("product_url", "")).hostname or "").lower()
            if host not in {"lowes.com", "www.lowes.com"}:
                raise ValueError("review_origin_is_not_public_lowes")
            if fact.get("category_id") != CATEGORY_ID:
                raise ValueError("review_category_is_not_authorized_mower")
            # Fail without printing raw content. This reproduces the approved
            # narrow public-review preflight, not a general-purpose DLP claim.
            text = " ".join(str(fact.get(key, "")) for key in ("review_title", "review_text"))
            if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\bsk-[A-Za-z0-9_-]{16,}", text):
                raise ValueError("review_requires_privacy_review")
            total += 1
    with FrozenReviewData(Path(args.report_root), report) as frozen:
        report_count = sum(1 for _ in frozen.iter_facts())
        provenance = frozen.provenance
    if total != counts["occurrences"] or report_count != counts["occurrences"]:
        raise ValueError("report_and_dataset_row_counts_differ")
    receipt = {"contract": "wenquan-frozen-live-eval/v1", "mode": "live" if args.live else "dry_run",
               **input_selection(args, data),
               "question": getattr(args, "question", QUESTION), "model_id": MODEL_IDS[getattr(args, "model_key", MODEL)], "effort": args.effort,
               "report_id": REPORT_ID, "snapshot_id": SNAPSHOT_ID, "job_id": JOB_ID,
               "report_sha256": "sha256:" + hashlib.sha256(Path(args.report_json).read_bytes()).hexdigest(),
               "dataset": manifest, "report_provenance": provenance,
               "public_review_preflight": {"rows": total, "expected_entities": 1717, "expected_written": 807},
               "web_enabled": False, "collection_enabled": False,
               "account_validation": "isolated eval identity; not a login-isolation acceptance test"}
    write_json(output / "preflight.json", receipt)
    return SingleReport(args.report_root, report), data


def make_app(research):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from agent.contracts import AgentError
    from agent.research_routes import install_research_routes
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(AgentError)
    async def error_handler(request, error):
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.http_status)

    def deny_public():
        raise AgentError("eval_private_broker_only", "Only the private eval broker is available", http_status=403)

    install_research_routes(app, research, deny_public)
    # Reuse the production broker, but expose no user API in this eval process.
    app.router.routes = [route for route in app.router.routes
                         if getattr(route, "path", "").startswith("/wenquan/internal/broker/")]
    return app


def live(args, output, report_store, data, config):
    import uvicorn
    from agent.agent_trace import AgentTraceStore
    from agent.contracts import IdentityContext
    from agent.eval_export import EvalExporter
    from agent.harness_manager import HarnessManager
    from agent.harness_research import HarnessResearch, TERMINAL
    from agent.model_registry import ModelRegistry
    from agent.service import WenquanService
    from agent.store import WenquanStore
    from agent.tool_registry import ToolRegistry
    from agent.upstream import ToolExecutor

    selection = input_selection(args, data)
    # Bind before submitting; fail instead of interfering with any existing
    # broker (including the deployed application). No address/port override.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Repeated regressions may leave closed connections in TIME_WAIT. This
    # permits their reuse but still rejects an actively listening 8088 broker.
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 19191))
    listener.listen(128)
    registry = ToolRegistry(zhuojian_status="ready", enable_research=True)
    store = WenquanStore(output / "wenquan.sqlite3")
    store.initialize()
    executor = ToolExecutor(registry=registry, transport=FrozenReportTransport(report_store), store=store,
                            envelope_schema_path=str(ROOT / "reporting/schemas/zhuojian-tool-envelope-v1.schema.json"))
    service = WenquanService(store=store, registry=registry, executor=executor)
    trace_store = AgentTraceStore(output / "traces/traces.sqlite3", content_enabled=True,
                                 max_payload_bytes=16 * 1024**2)
    research = HarnessResearch(service, HarnessManager(MANAGER, config["manager_token"]),
                               models=ModelRegistry({key: config[key] for key in ("VOXQUARRY_LLM_API_KEY", "VOXQUARRY_LLM_BASE_URL")}),
                               traces=trace_store, data=data, collection=DisabledExternalTools(), web=DisabledExternalTools())
    actor = IdentityContext(actor_id="frozen-eval", owner_user_id="frozen-eval", scope_id="principal:frozen-eval",
                            authority_id="isolated-frozen-eval", roles=("user",), mode="synthetic",
                            allowed_report_ids=frozenset({REPORT_ID}))
    server = uvicorn.Server(uvicorn.Config(make_app(research), access_log=False, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    question = getattr(args, "question", QUESTION)
    session_id = store.create_session(actor, "EGO 冻结资料真实模型评测")["session_id"]
    run_id, status, failure = None, None, None
    started = time.monotonic()
    thread.start()
    try:
        until = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < until:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("eval_broker_start_failed")
        accepted = research.submit(actor, session_id=session_id, question=question, request_id="frozen-question",
                                   **selection["selected_materials"],
                                   model_key=getattr(args, "model_key", MODEL), effort=args.effort, web_enabled=False)
        run_id = accepted["run"]["run_id"]
        write_json(output / "started.json", {"session_id": session_id, "run_id": run_id, "output": str(output), **selection})
        print(json.dumps({"status": "running", "run_id": run_id, "input_mode": selection["input_mode"], "output": str(output)}, ensure_ascii=False), flush=True)
        cursor, last_notice = 0, 0
        with (output / "events.jsonl").open("x", encoding="utf-8") as events_file:
            while time.monotonic() - started < 720:
                current = research.state.get(actor, run_id)
                if current.get("trace_id"):
                    page = trace_store.list_events(actor, current["trace_id"], after_seq=cursor, limit=1000)
                    for event in page["items"]:
                        events_file.write(json.dumps(event, ensure_ascii=False) + "\n")
                        cursor = event["seq"]
                    events_file.flush()
                if current["status"] in TERMINAL:
                    status = current["status"]
                    break
                if time.monotonic() - last_notice >= 30:
                    print(json.dumps({"status": current["status"], "seconds": round(time.monotonic() - started),
                                      "recorded_events": cursor}), flush=True)
                    last_notice = time.monotonic()
                time.sleep(1)
            if status is None:
                failure = "eval_watchdog_timeout"
                research.cancel(actor, session_id, run_id)
    except (KeyboardInterrupt, Exception) as error:
        # Never echo arbitrary exception/provider bodies, which can contain input.
        failure = getattr(error, "code", type(error).__name__)
        if run_id:
            research.cancel(actor, session_id, run_id)
    finally:
        try:
            # Let execute() stop only its own VM attempt and finish the trace
            # before freezing evidence. Broker stays available during cleanup.
            research._pool.shutdown(wait=True, cancel_futures=True)
            if run_id:
                final = research.state.get(actor, run_id)
                bundle = store.session_bundle(actor, session_id)
                write_json(output / "result.json", {"elapsed_seconds": round(time.monotonic() - started, 3),
                                                    "failure": failure, "run": final, "session": bundle, **selection})
                messages = store.list_messages(actor, session_id)
                with (output / "answer.md").open("x", encoding="utf-8") as answer:
                    answer.write("\n\n".join(item["content"] for item in messages if item["role"] == "assistant") + "\n")
                write_json(output / "sources.json", research.state.list_sources(actor, run_id))
                if final.get("trace_id"):
                    exported = EvalExporter(trace_store).export(actor, [final["trace_id"]], include_content=True)
                    write_json(output / "eval-export.json", exported)
                    write_json(output / "usage.json", {"model_events": [{"event_type": e["event_type"], "data": e["data"]}
                               for e in exported["cases"][0]["model_events"]],
                               "budget_events": [e for e in exported["cases"][0]["events"] if e["event_type"].startswith("budget.")]})
        finally:
            research.close()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
    print(json.dumps({"status": status or "failed", "failure": failure, "input_mode": selection["input_mode"], "output": str(output)}, ensure_ascii=False))
    return 0 if status == "completed" and not failure else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ("report-json", "report-root", "data-root", "selected-runs-json", "out"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--config", type=Path, help="Required for --live; private 0600 credentials JSON")
    parser.add_argument("--model-key", choices=tuple(MODEL_IDS), default=MODEL)
    parser.add_argument("--input-mode", choices=INPUT_MODES, default="report",
                        help="Initial associations only: report+dataset (default), none (discovery), or dataset only; authorized sources do not change")
    parser.add_argument("--question", default=QUESTION, help="Question about the same fixed report and public dataset")
    parser.add_argument("--effort", choices=("low", "high", "max"))
    parser.add_argument("--live", action="store_true", help="Start one bounded research using the selected authorized model")
    args = parser.parse_args(argv)
    if not args.question.strip() or len(args.question) > 2000:
        parser.error("--question must contain 1–2000 characters")
    from agent.contracts import AgentError
    from agent.model_registry import ModelRegistry
    try:
        registry = ModelRegistry({})
        selected = registry.selection({"model_key": args.model_key, "effort": args.effort or "default"})
    except AgentError as error:
        parser.error(error.message)
    model = next(item for item in registry.catalog()["items"] if item["key"] == args.model_key)
    args.effort = selected["effort"] if selected["effort"] != "default" else model["default_effort"]
    if args.live and args.config is None:
        parser.error("--live requires --config")
    previous_umask = os.umask(0o077)
    try:
        # Dry-run never loads secrets or contacts the manager/provider.
        config = load_config(args.config) if args.live else None
        paths = [args.report_json, args.report_root, args.data_root, args.selected_runs_json]
        if args.config:
            paths.append(args.config)
        output = make_output(args.out, paths)
        report, data = preflight(args, output)
        if not args.live:
            print(json.dumps({"status": "dry_run_passed", "model_called": False, "input_mode": args.input_mode, "output": str(output)}, ensure_ascii=False))
            return 0
        return live(args, output, report, data, config)
    except Exception as error:
        # Generic local failures are classified without printing credentials/raw data.
        code = getattr(error, "code", None) or (str(error) if isinstance(error, ValueError) and re.fullmatch(r"[a-zA-Z0-9_]+", str(error)) else type(error).__name__)
        print(json.dumps({"status": "failed", "error": code}), file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
