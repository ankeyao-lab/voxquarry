"""Durable, atomic lifecycle for read-only turns; existing history stays compatible."""
from __future__ import annotations

import json
from typing import Any

from .contracts import AgentError, IdentityContext, new_id, sha256_value, utc_now
from .store import WenquanStore


class ReadonlyTurns:
    def __init__(self, store: WenquanStore) -> None:
        self.store = store
        with store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS readonly_turns (
                owner_user_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
                PRIMARY KEY(owner_user_id, scope_id, session_id, request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS research_drafts (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id), owner_user_id TEXT NOT NULL,
                scope_id TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def recover_interrupted(self) -> None:
        # Only this executor's runs, never historical planner/confirmation work.
        with self.store._connect() as db:
            rows = db.execute("""SELECT r.* FROM runs r JOIN readonly_turns t USING(run_id)
                                  WHERE r.status='running'""").fetchall()
            for row in rows:
                now = utc_now()
                draft = db.execute("SELECT payload_json FROM research_drafts WHERE run_id=?", (row["run_id"],)).fetchone()
                resumable = bool(draft and json.loads(draft[0]).get("action", {}).get("resume_version") == "research-v2")
                message = "服务重启中断了回答，已保存资料，可以继续分析。" if resumable else "服务重启中断了回答，请重新提问。"
                metadata = {"error": {"code": "run_interrupted", "message": message, "retryable": True}, "claims": [], "citations": [], "can_resume": resumable}
                db.execute("UPDATE runs SET status='failed', updated_at=? WHERE run_id=?", (now, row["run_id"]))
                db.execute("UPDATE steps SET status='skipped', finished_at=? WHERE run_id=? AND status IN ('queued','running')", (now, row["run_id"]))
                db.execute("""INSERT INTO messages (message_id,session_id,owner_user_id,scope_id,role,content,answer_status,run_id,metadata_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (new_id("msg"), row["session_id"], row["owner_user_id"], row["scope_id"], "assistant", metadata["error"]["message"], "failed", row["run_id"], json.dumps(metadata, ensure_ascii=False), now))

    def accept(self, actor: IdentityContext, session_id: str, *, question: str,
               report_id: str | None, request_id: str, plan: list[dict[str, str]],
               selection_mode: str | None = None, resume_from: str | None = None) -> tuple[dict[str, Any], bool]:
        session = self.store.get_session(actor, session_id)
        if session["archived"]:
            raise AgentError("session_archived", "请在新会话中提问。", http_status=409)
        request = {"question": question, "report_id": report_id}
        if selection_mode is not None:
            request["selection_mode"] = selection_mode
        if resume_from is not None:
            request["resume_from"] = resume_from
        request_hash = sha256_value(request)
        key = (actor.owner_user_id, actor.scope_id, session_id)
        duplicate = False
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM readonly_turns WHERE owner_user_id=? AND scope_id=? AND session_id=? AND request_id=?", (*key, request_id)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise AgentError("request_conflict", "同一请求标识不能用于不同的问题或报告。", http_status=409)
                run_id, duplicate = old["run_id"], True
            else:
                active = db.execute("SELECT count(*) FROM runs r JOIN readonly_turns t USING(run_id) WHERE r.status='running'").fetchone()[0]
                same_session = db.execute("SELECT 1 FROM runs r JOIN readonly_turns t USING(run_id) WHERE r.session_id=? AND r.status='running'", (session_id,)).fetchone()
                if same_session or active >= 8:
                    raise AgentError("run_busy", "已有问题正在处理，请等待完成或停止后再提问。", http_status=409, retryable=True)
                run_id, now = new_id("run"), utc_now()
                db.execute("""INSERT INTO runs (run_id,session_id,owner_user_id,scope_id,intent,plan_hash,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (run_id, session_id, actor.owner_user_id, actor.scope_id, "report_readonly_answer", sha256_value(plan), "running", now, now))
                db.execute("INSERT INTO readonly_turns VALUES (?,?,?,?,?,?)", (*key, request_id, request_hash, run_id))
                for index, step in enumerate(plan, 1):
                    db.execute("""INSERT INTO steps (step_id,run_id,session_id,owner_user_id,scope_id,sequence_no,title,tool_name,status,input_summary_json,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (new_id("stp"), run_id, session_id, actor.owner_user_id, actor.scope_id, index, step["title"], step.get("tool_name"), "queued", "{}", now))
                metadata = json.dumps({"request_id": request_id, "report_id": report_id,
                                       "selection_mode": selection_mode or "legacy",
                                       **({"resume_from": resume_from} if resume_from else {})}, ensure_ascii=False)
                db.execute("""INSERT INTO messages (message_id,session_id,owner_user_id,scope_id,role,content,run_id,metadata_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (new_id("msg"), session_id, actor.owner_user_id, actor.scope_id, "user", question, run_id, metadata, now))
                db.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (now, session_id))
        return self.store.get_run(actor, run_id), duplicate

    def event(self, actor: IdentityContext, run_id: str, title: str,
              status: str = "running", *, event_id: str | None = None,
              detail: str = "") -> str:
        """Persist public progress separately from model reasoning or raw arguments."""
        run = self.store.get_run(actor, run_id)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM runs WHERE run_id=? AND status='running'", (run_id,)).fetchone():
                raise AgentError("run_cancelled", "已停止回答。", http_status=409)
            now = utc_now()
            summary = json.dumps({"live_event": True, "detail": detail[:500]}, ensure_ascii=False)
            if event_id:
                changed = db.execute("""UPDATE steps SET title=?,status=?,input_summary_json=?,finished_at=?
                    WHERE step_id=? AND run_id=? AND owner_user_id=? AND scope_id=?""",
                    (title[:200], status, summary, now if status != "running" else None,
                     event_id, run_id, actor.owner_user_id, actor.scope_id)).rowcount
                if not changed:
                    raise AgentError("run_not_found", "没有找到对应进度。", http_status=404)
            else:
                event_id = new_id("stp")
                sequence = db.execute("SELECT COALESCE(MAX(sequence_no),0)+1 FROM steps WHERE run_id=?", (run_id,)).fetchone()[0]
                db.execute("""INSERT INTO steps
                    (step_id,run_id,session_id,owner_user_id,scope_id,sequence_no,title,status,input_summary_json,started_at,finished_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_id,run_id,run["session_id"],actor.owner_user_id,actor.scope_id,sequence,title[:200],status,
                     summary,now,now if status != "running" else None,now))
        return event_id

    def checkpoint(self, actor: IdentityContext, run_id: str, action: dict[str, Any],
                   sources: dict[str, Any], checks: list[dict[str, Any]]) -> None:
        """Keep the latest candidate and replay pointers, never full source bodies or reasoning."""
        self.store.get_run(actor, run_id)
        pointers = {sid: {k: source[k] for k in ("source_request", "source_sha256", "record_id", "report_id", "snapshot_id",
                    "report_identity_sha256", "snapshot_identity_sha256", "kind", "in_model") if k in source}
                    for sid, source in sources.items()}
        payload = json.dumps({"action": action, "sources": pointers, "checks": checks[-12:]}, ensure_ascii=False)
        if len(payload) > 2_000_000:
            return
        with self.store._connect() as db:
            db.execute("INSERT OR REPLACE INTO research_drafts VALUES (?,?,?,?,?)",
                       (run_id, actor.owner_user_id, actor.scope_id, payload, utc_now()))
            db.execute("""DELETE FROM research_drafts WHERE owner_user_id=? AND scope_id=? AND run_id NOT IN
                (SELECT run_id FROM research_drafts WHERE owner_user_id=? AND scope_id=? ORDER BY updated_at DESC LIMIT 20)""",
                (actor.owner_user_id,actor.scope_id,actor.owner_user_id,actor.scope_id))

    def draft(self, actor: IdentityContext, run_id: str) -> dict[str, Any] | None:
        self.store.get_run(actor, run_id)
        with self.store._connect() as db:
            row = db.execute("SELECT payload_json FROM research_drafts WHERE run_id=? AND owner_user_id=? AND scope_id=?",
                             (run_id, actor.owner_user_id, actor.scope_id)).fetchone()
        return json.loads(row[0]) if row else None

    def active(self, actor: IdentityContext, run_id: str) -> bool:
        return self.store.get_run(actor, run_id)["status"] == "running"

    def step(self, actor: IdentityContext, run_id: str, step_id: str, status: str) -> None:
        with self.store._connect() as db:
            now = utc_now()
            cursor = db.execute("""UPDATE steps SET status=?, started_at=COALESCE(started_at,?), finished_at=?
                WHERE step_id=? AND run_id=? AND owner_user_id=? AND scope_id=?
                AND EXISTS(SELECT 1 FROM runs WHERE run_id=? AND status='running')""",
                (status, now, now if status == "completed" else None, step_id, run_id, actor.owner_user_id, actor.scope_id, run_id))
            if not cursor.rowcount:
                raise AgentError("run_cancelled", "已停止回答。", http_status=409)

    def finish(self, actor: IdentityContext, run_id: str, *, status: str, content: str,
               metadata: dict[str, Any]) -> dict[str, Any]:
        run = self.store.get_run(actor, run_id)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = utc_now()
            changed = db.execute("UPDATE runs SET status=?, updated_at=? WHERE run_id=? AND status='running'", (status, now, run_id)).rowcount
            if changed:
                db.execute("UPDATE steps SET status=?, error_code=?, finished_at=? WHERE run_id=? AND status='running'", ("skipped" if status == "cancelled" else "failed", (metadata.get("error") or {}).get("code"), now, run_id))
                db.execute("UPDATE steps SET status='skipped', finished_at=? WHERE run_id=? AND status='queued'", (now, run_id))
                db.execute("""INSERT INTO messages (message_id,session_id,owner_user_id,scope_id,role,content,answer_status,run_id,metadata_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (new_id("msg"), run["session_id"], actor.owner_user_id, actor.scope_id, "assistant", content[:20000], "answered" if status == "completed" else "insufficient_evidence" if status == "blocked" else status, run_id, json.dumps(metadata, ensure_ascii=False), now))
                db.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (now, run["session_id"]))
        return self.store.get_run(actor, run_id)
