"""Single-origin application gateway for the five public modules.

The browser never chooses a principal, capability or trusted service header.
Core services retain their own validation, contracts and error handling.
"""
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from account_admin.config import LocalDevConfig
from account_admin.service import AccountAdminService, AuthenticationError, AuthorizationError, RegistrationError
from account_admin.gateway import capabilities_for_roles
from account_admin.research_authority import ResearchAuthority
from .data import ingest, snapshot, demo_payload

ROOT = Path(__file__).resolve().parents[1]


def private_config(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "secrets.json"
    if not path.exists():
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            json.dump({key: secrets.token_urlsafe(48) for key in ("identity", "service", "gateway")}, stream)
    return json.loads(path.read_text())


def create_app():
    runtime = Path(os.getenv("VOXQUARRY_DATA_DIR", ROOT / ".runtime")).resolve()
    config = private_config(runtime)
    origin = os.getenv("VOXQUARRY_ORIGIN", "http://127.0.0.1:18763").rstrip("/")
    from urllib.parse import urlsplit
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
        raise RuntimeError("VOXQUARRY_ORIGIN must be an http(s) origin")
    internal_url = os.getenv("VOXQUARRY_INTERNAL_URL", "http://127.0.0.1:18763")
    origins = {origin}
    if parsed.hostname in {"127.0.0.1", "localhost"}:
        origins |= {origin.replace("127.0.0.1", "localhost"), origin.replace("localhost", "127.0.0.1")}
    allowed_hosts = {urlsplit(value).netloc for value in origins}
    from agent.config import validate_private_service_url
    validate_private_service_url(internal_url, name="Internal application URL")
    allowed_hosts.add(urlsplit(internal_url).netloc)
    os.environ.update({
        "DATA_DIR": str(runtime / "source" / "data"),
        "VOXQUARRY_RETAIL_MEDIA": str(runtime / "products" / "media"),
        "VOXQUARRY_WENQUAN_DATA_DIR": str(runtime / "research"),
        "VOXQUARRY_WENQUAN_IDENTITY_PROVIDER": "account_admin_v1",
        "VQ_LOCAL_IDENTITY_SIGNING_KEY": config["identity"],
        "VOXQUARRY_IDENTITY_DEPENDENCY_STATUS": "ready",
        "VOXQUARRY_ZHUOJIAN_BASE_URL": internal_url,
        "VOXQUARRY_ZHUOJIAN_GATEWAY_TOKEN": config["gateway"],
        "VOXQUARRY_AGENT_GATEWAY_TOKEN": config["gateway"],
        "VOXQUARRY_REPORT_MANAGEMENT_GATEWAY_TOKEN": config["gateway"],
        "VOXQUARRY_RESEARCH_BRIDGE_TOKEN": config["gateway"],
        "VOXQUARRY_RESEARCH_COLLECTOR_URL": internal_url,
        "VOXQUARRY_WENQUAN_WOTU_SOURCE_ROOT": str(runtime / "source"),
        "VOXQUARRY_RESEARCH_AUTHORITY_URL": internal_url,
        "VOXQUARRY_RESEARCH_AUTHORITY_TOKEN": config["gateway"],
        "VOXQUARRY_AGENT_TOOLS_ENABLED": "true",
        "VOXQUARRY_WENQUAN_REPORT_GENERATION_ENABLED": "true",
    })
    accounts = AccountAdminService(LocalDevConfig(data_dir=runtime / "accounts",
        identity_signing_keys={"local-identity-v1": config["identity"].encode()},
        service_signing_keys={"local-service-v1": config["service"].encode()},
        allowed_origins=frozenset(origins), allow_self_registration=True))
    # Import after assigning this instance's storage and identity configuration.
    from collector import main as collection
    from reporting.app import create_app as reports_app
    from retail_landscape.app import create_app as products_app
    from agent.app import create_runtime_app
    initial = runtime / "snapshots" / "empty"
    backups = initial / "data" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(collection.store.db_path) as src, sqlite3.connect(backups / "jobs-consistent-snapshot-initial.sqlite3") as dst:
        src.backup(dst)
    reports = reports_app(initial, runtime / "reports", frontend_root=ROOT / "web" / "dist" / "client", execution_mode="thread")
    products = products_app(runtime / "products" / "products.sqlite3", identity_resolver=lambda request: request.scope["vq_actor"])
    research = create_runtime_app()
    from .links import ProductSources
    links = ProductSources(runtime / "product-sources.sqlite3")
    authority = ResearchAuthority(accounts)
    mutation_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            if os.getenv("VOXQUARRY_WORKERS", "true") == "true":
                await stack.enter_async_context(collection.app.router.lifespan_context(collection.app))
            await stack.enter_async_context(reports.router.lifespan_context(reports))
            try:
                yield
            finally:
                research.state.readonly.close()

    api = FastAPI(title="VoxQuarry", version="0.1.0-alpha.1", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api.state.accounts = accounts
    api.state.collection = collection
    api.state.reports = reports.state.reporting
    api.state.products = products.state.retail_store
    api.state.runtime = runtime
    api.state.research = research

    @api.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"error": "invalid_request", "detail": str(exc)}, status_code=422)

    @api.exception_handler(RuntimeError)
    async def runtime_error(_request, exc):
        return JSONResponse({"error": "operation_failed", "detail": str(exc)}, status_code=422)

    @api.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0-alpha.1"}

    @api.get("/api/v1/auth/status")
    def auth_status():
        return {"setup_required": not bool(accounts.store.list_principals())}

    def request_info(request):
        return dict(request_id=uuid.uuid4().hex, client_ip=request.client.host if request.client else "local",
                    user_agent=request.headers.get("user-agent", "")[:200])

    def login_response(credentials):
        response = JSONResponse({"user": accounts.public_principal(credentials.principal), "csrf_token": credentials.csrf_token})
        response.set_cookie("vq_session", credentials.token, httponly=True, samesite="strict", secure=parsed.scheme == "https", max_age=43200)
        response.headers["Cache-Control"] = "no-store"
        return response

    @api.post("/api/v1/auth/setup")
    def setup(payload: dict, request: Request):
        username = str(payload.get("username", ""))
        password = payload.get("password", "")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,40}", username) or not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise HTTPException(422, "Use a 3–40 character username and a password of at least 12 characters")
        with mutation_lock:
            if accounts.store.list_principals():
                raise HTTPException(409, "Setup has already been completed")
            accounts.bootstrap_admin(employee_id=username, username=username, display_name=payload.get("display_name") or username, password=password)
        return login_response(accounts.login_local_account(username=username, password=password, remember_me=False, **request_info(request)))

    @api.post("/api/v1/auth/login")
    def login(payload: dict, request: Request):
        try:
            credentials = accounts.login_local_account(username=payload.get("username", ""), password=payload.get("password", ""), remember_me=False, **request_info(request))
        except (AuthenticationError, RegistrationError):
            raise HTTPException(401, "Invalid credentials or login temporarily limited")
        return login_response(credentials)

    @api.get("/api/v1/me")
    def me(request: Request):
        session = request.scope["vq_session"]
        return {"user": accounts.public_principal(accounts.store.get_principal(session["principal_id"])),
                "capabilities": sorted(capabilities_for_roles(session["roles"]))}

    @api.post("/api/v1/auth/logout")
    def logout(request: Request):
        session = request.scope["vq_session"]
        accounts.revoke_session(session, session["session_id"], reason="logout", **request_info(request))
        response = JSONResponse({"ok": True})
        response.delete_cookie("vq_session")
        return response

    @api.post("/api/v1/auth/refresh")
    def refresh_login(request: Request):
        return login_response(accounts.rotate_session(request.scope["vq_session"], **request_info(request)))

    @api.get("/api/v1/settings")
    def settings():
        return {"version": "0.1.0-alpha.1", "analysis_languages": ["en"], "interface_languages": ["en", "zh-CN"],
                "llm_configured": bool(research.state.wenquan.planner),
                "harness_configured": research.state.research is not None,
                "collection_limits": {"max_pages": 3, "max_reviews": 100, "timeout_seconds": 120},
                "channels": sorted(collection.COLLECTION_CHANNELS)}

    @api.post("/account-admin/internal/research-authorize")
    def authorize(payload: dict):
        status, value = authority.authorize(payload.get("principal_id"), payload.get("required_capability"))
        return JSONResponse(value, status_code=status)

    def refresh():
        target = snapshot(runtime / "source", runtime / "snapshots")
        reports.state.reporting.snapshot_root = target
        from reporting.app import build_catalog_payload
        reports.state.reporting.catalog_payload = build_catalog_payload(target)
        return target

    @api.get("/api/v1/products")
    def linked_products():
        items = []
        saved = reports.state.reporting.store.list_reports()
        for category in products.state.retail_store.list_categories():
            for product in products.state.retail_store.list_products(category["code"]):
                jobs = links.sources(product["id"])
                product["source_job_ids"] = jobs
                product["linked_report_ids"] = [r["report_id"] for r in saved
                    if set((r.get("scope") or {}).get("job_ids") or []) & set(jobs)]
                items.append(product)
        return {"items": items}

    @api.post("/api/v1/products/{product_id}/sources")
    def link_source(product_id: str, payload: dict):
        job_id = payload.get("job_id")
        if not products.state.retail_store.get_product(product_id) or not isinstance(job_id, str) or not collection.store.get_job(job_id):
            raise HTTPException(404, "Product or source task not found")
        links.add(product_id, job_id)
        return {"product_id": product_id, "job_ids": links.sources(product_id)}

    @api.post("/api/v1/import")
    def import_reviews(payload: dict):
        with mutation_lock:
            return ingest(collection.store, collection.runner, payload)

    @api.post("/api/v1/demo")
    def demo():
        with mutation_lock:
            marker = runtime / "demo.json"
            if marker.exists():
                saved = json.loads(marker.read_text())
                links.add(saved["product_id"], saved["job_id"])
                return saved
            result = ingest(collection.store, collection.runner, demo_payload())
            product = products.state.retail_store.create_product({"category_code": "walk_behind_mower", "brand": "Northstar Demo", "model_number": "M40", "product_name": "M40 · Synthetic demo", "energy_type": "DC", "voltage_platform": "40V", "notes": "Entirely synthetic fixture; not a real product."}, actor="demo-seed")
            refresh()
            from reporting.contracts import normalize_request
            from reporting.contracts import default_scope
            from reporting.config import ANALYSIS_VERSION
            normalized = normalize_request({"report_name": "M40 · Synthetic review study", "scope": {"category_id": "walk_behind_mower", "job_ids": [result["job_id"]]}}, defaults=default_scope(), analysis_version=ANALYSIS_VERSION)
            report = reports.state.reporting.run_sync(normalized)
            value = {**result, "product_id": product["id"], "report_id": report["report_id"]}
            links.add(product["id"], result["job_id"])
            marker.write_text(json.dumps(value))
            return value

    @api.post("/api/v1/reports")
    def generate_report(payload: dict):
        with mutation_lock:
            refresh()
            from reporting.contracts import normalize_request
            from reporting.contracts import default_scope
            from reporting.config import ANALYSIS_VERSION
            request = normalize_request(payload, defaults=default_scope(), analysis_version=ANALYSIS_VERSION)
            return reports.state.reporting.run_sync(request)

    @api.get("/api/v1/reports/{report_id}/export")
    def export_report(report_id: str, locale: str = "en"):
        from .presentation import report_markdown
        report = reports.state.reporting.store.get_report(report_id)
        if report is None:
            raise HTTPException(404, "Report not found")
        if locale not in {"en", "zh-CN"}:
            raise HTTPException(422, "Unsupported locale")
        return Response(report_markdown(report, locale), media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{report_id}-{locale}.md"'})

    api.mount("/assets", StaticFiles(directory=ROOT / "web" / "dist" / "client" / "assets"), name="assets")

    @api.get("/{path:path}")
    def frontend(path: str):
        if path.startswith(("api/", "account-admin/")):
            raise HTTPException(404, "Not found")
        return FileResponse(ROOT / "web" / "dist" / "client" / "index.html")

    class Gateway:
        state = api.state
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await api(scope, receive, send)
            headers = dict(scope["headers"])
            path, method = scope["path"], scope["method"]
            async def deny(status, code):
                return await JSONResponse({"error": code}, status_code=status)(scope, receive, send)
            host = headers.get(b"host", b"").decode()
            if host not in allowed_hosts:
                return await deny(400, "invalid_host")
            try:
                length = int(headers.get(b"content-length", b"0") or 0)
            except ValueError:
                return await deny(400, "invalid_content_length")
            if length < 0:
                return await deny(400, "invalid_content_length")
            if length > 20 * 1024 * 1024:
                return await deny(413, "request_too_large")
            broker_path = path.startswith("/wenquan/internal/broker/")
            if broker_path:
                return await research(scope, receive, send)
            private_path = path.startswith(("/reporting/api/v1/agent-", "/reporting/api/v1/tools", "/reporting/api/v1/research-data/", "/api/research/", "/account-admin/internal/"))
            token = headers.get(b"x-voxquarry-gateway-token", b"")
            bearer = headers.get(b"authorization", b"")
            internal = private_path and (hmac.compare_digest(token, config["gateway"].encode()) or hmac.compare_digest(bearer, ("Bearer " + config["gateway"]).encode()))
            public = path in {"/health", "/api/v1/auth/status", "/api/v1/auth/setup", "/api/v1/auth/login"} or path.startswith("/assets/") or not path.startswith(("/api/", "/reporting/", "/retail/", "/wenquan/", "/account-admin/"))
            if private_path and not internal:
                return await deny(401, "service_auth_required")
            if not internal:
                if method not in {"GET", "HEAD", "OPTIONS"} and headers.get(b"origin", b"").decode() not in origins:
                    return await deny(403, "origin_denied")
                # Strip every client-supplied identity/authority header, even on public paths.
                scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if not k.lower().startswith((b"x-vq-", b"x-voxquarry-"))]}
                if not public:
                    request = Request(scope)
                    try:
                        session = accounts.authenticate(request.cookies.get("vq_session"))
                        caps = capabilities_for_roles(session["roles"])
                        if method not in {"GET", "HEAD", "OPTIONS"}:
                            if path != "/api/v1/auth/refresh":
                                accounts.verify_csrf(session, headers.get(b"x-csrf-token", b"").decode())
                            required = "admin.manage_users"
                            if path.startswith("/api/v1/auth/"): required = "platform.read"
                            elif path.startswith(("/api/master-data", "/api/shared-master-data", "/api/admin/master-data", "/api/v2/master-data")): required = "master_data.manage"
                            elif path.startswith(("/api/v1/import", "/api/imports/", "/api/jobs", "/api/resolve", "/api/search")): required = "collect.command"
                            elif path.startswith(("/reporting/", "/api/v1/reports")): required = "report.command"
                            elif path.startswith(("/retail/", "/api/v1/products/")): required = "retail.command"
                            elif path.startswith("/wenquan/"): required = "chat.session.own"
                            elif path.startswith("/api/v1/demo"): required = "admin.manage_users"
                            accounts.require_capability(session, required)
                        request_id = uuid.uuid4().hex
                        actor = {k: session[k] for k in ("principal_id", "display_name", "roles")}
                        scope["vq_actor"], scope["vq_session"] = actor, session
                        trusted = {"x-vq-identity-context": accounts.signed_identity_context(session, request_id),
                            "x-voxquarry-report-management-token": config["gateway"],
                            "x-voxquarry-authority-id": "voxquarry-local-account",
                            "x-voxquarry-principal-id": session["principal_id"],
                            "x-voxquarry-permissions": ",".join(sorted(caps)), "x-voxquarry-request-id": request_id}
                        scope["headers"] += [(k.encode(), v.encode()) for k, v in trusted.items()]
                    except AuthenticationError:
                        return await deny(401, "login_required")
                    except AuthorizationError:
                        return await deny(403, "permission_denied")
            # Bound the actual payload as well as Content-Length, including
            # chunked uploads. Parse only after authentication and this limit.
            if method not in {"GET", "HEAD", "OPTIONS"}:
                chunks, size = [], 0
                while True:
                    part = await receive()
                    if part["type"] == "http.disconnect":
                        return
                    body = part.get("body", b"")
                    size += len(body)
                    if size > 20 * 1024 * 1024:
                        return await deny(413, "request_too_large")
                    chunks.append(body)
                    if not part.get("more_body"):
                        break
                original_receive = receive
                pending = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]
                async def buffered_receive():
                    return pending.pop() if pending else await original_receive()
                receive = buffered_receive
            target = api
            if path.startswith("/reporting/"): target = reports
            elif path.startswith("/retail/"): target = products
            elif path.startswith("/wenquan/"): target = research
            elif path.startswith("/api/") and not path.startswith("/api/v1/"): target = collection.app
            async def secure_send(message):
                if message["type"] == "http.response.start":
                    message.setdefault("headers", []).extend([(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"),
                        (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"),
                        (b"cache-control", b"no-store")])
                await send(message)
            await target(scope, receive, secure_send)
    return Gateway()
