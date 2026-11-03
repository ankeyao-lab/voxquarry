"""Offline comparison of the former per-packet write transaction and read guard.

Run from repository root. Creates only disposable synthetic SQLite state. The
result measures the guard itself, not network/model latency or end-to-end time.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.model_registry import ModelRegistry
from agent.contracts import IdentityContext
from agent.research_state import ResearchState
from agent.store import WenquanStore


def benchmark(reads=2000, rounds=5):
    with tempfile.TemporaryDirectory(prefix="wq-guard-fixture-") as directory:
        store = WenquanStore(Path(directory) / "fixture.sqlite3")
        store.initialize()
        registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-benchmark-only"})
        state = ResearchState(store, registry)
        state.initialize()
        user = IdentityContext(actor_id="fixture-user", owner_user_id="fixture-user", scope_id="fixture-scope",
                               authority_id="fixture-gateway", roles=(), mode="synthetic")
        session = store.create_session(user, "synthetic guard benchmark")
        run = state.accept(user, session["session_id"], {"question": "synthetic", "request_id": "fixture"},
                           registry.freeze({"web_enabled": False}))["run"]
        elapsed = {"former_transaction": [], "stream_identity_reader": []}
        for _ in range(rounds):
            started = time.perf_counter()
            for _ in range(reads):
                with state._connect() as db:
                    state._active_identity(db, user, run["run_id"])
            elapsed["former_transaction"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            with state.identity_reader() as read:
                for _ in range(reads):
                    read(user, run["run_id"])
            elapsed["stream_identity_reader"].append((time.perf_counter() - started) * 1000)
        medians = {key: round(statistics.median(values), 3) for key, values in elapsed.items()}
        return {"fixture": "synthetic_sqlite_identity_only", "reads_per_round": reads, "rounds": rounds,
                "real_model_calls": 0, "median_ms": medians,
                "samples_ms": {key: [round(value, 3) for value in values] for key, values in elapsed.items()},
                "scope": "local_guard_only;not_end_to_end_or_unattributed_handoff_gap"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reads", type=int, default=2000)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.reads <= 100000 or not 1 <= args.rounds <= 20:
        parser.error("reads must be 1..100000; rounds must be 1..20")
    print(json.dumps(benchmark(args.reads, args.rounds), ensure_ascii=False))
