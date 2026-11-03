"""Offline boundaries for the deliberately narrow real-model regression CLI."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("wenquan_live_eval", Path(__file__).with_name("live_eval.py"))
eval_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eval_cli)


class LiveEvalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def json_file(self, name, value, mode=0o600):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(mode)
        return path

    def config(self, **overrides):
        return self.json_file("config.json", {"manager_token": "m" * 40, "VOXQUARRY_LLM_API_KEY": "k" * 115,
                                              "VOXQUARRY_LLM_BASE_URL": eval_cli.PROVIDER, **overrides})

    def test_only_exact_authorized_provider_and_closed_config_are_accepted(self):
        self.assertEqual(eval_cli.load_config(self.config())["VOXQUARRY_LLM_BASE_URL"], eval_cli.PROVIDER)
        for overrides in ({"VOXQUARRY_LLM_BASE_URL": "https://other.example/v1"}, {"extra": "field"},
                          {"manager_token": "short"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                eval_cli.load_config(self.config(**overrides))

    def test_config_cannot_be_shared_linked_or_hardlinked(self):
        original = self.config()
        original.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            eval_cli.load_config(original)
        original.chmod(0o600)
        linked = self.root / "link.json"
        linked.symlink_to(original)
        with self.assertRaises(OSError):
            eval_cli.load_config(linked)
        linked.unlink()
        os.link(original, linked)
        with self.assertRaisesRegex(ValueError, "input_file_invalid"):
            eval_cli.load_config(original)

    def test_output_is_new_private_and_does_not_contain_inputs_or_git_data(self):
        source = self.root / "source"
        source.mkdir()
        output = eval_cli.make_output(self.root / "output", [source])
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            eval_cli.make_output(output, [source])
        with self.assertRaises(ValueError):
            eval_cli.make_output(self.root, [source])
        with self.assertRaises(ValueError):
            eval_cli.make_output(eval_cli.ROOT / "private-eval", [source])

    def test_dry_run_never_loads_credentials_or_starts_live_broker(self):
        inputs = {name: self.root / name for name in ("report", "report-root", "data-root", "selected-runs")}
        for path in inputs.values():
            path.mkdir()
        argv = ["--report-json", str(inputs["report"]), "--report-root", str(inputs["report-root"]),
                "--data-root", str(inputs["data-root"]), "--selected-runs-json", str(inputs["selected-runs"]),
                "--config", str(self.root / "does-not-exist.json"), "--out", str(self.root / "dry-output")]
        for mode in (None, *eval_cli.INPUT_MODES):
            with self.subTest(mode=mode), \
                    patch.object(eval_cli, "preflight", return_value=(None, None)) as preflight, \
                    patch.object(eval_cli, "load_config", side_effect=AssertionError("credentials read")), \
                    patch.object(eval_cli.socket, "socket", side_effect=AssertionError("network socket")), \
                    patch.object(eval_cli, "live", side_effect=AssertionError("live model")):
                selected = argv[:-1] + [str(self.root / ("dry-output-" + str(mode)))]
                if mode:
                    selected.extend(["--input-mode", mode])
                self.assertEqual(eval_cli.main(selected), 0)
                self.assertEqual(preflight.call_args.args[0].input_mode, mode or "report")
                self.assertEqual(preflight.call_args.args[0].effort, "low")

    def test_preflight_records_associations_without_changing_authorized_materials(self):
        report = self.json_file("report.json", {"report_id": eval_cli.REPORT_ID, "data_snapshot_id": eval_cli.SNAPSHOT_ID})
        selected = self.json_file("selected.json", [{"job": {"id": eval_cli.JOB_ID, "brand": "EGO", "model": "LM2236SP", "channel": "lowes"}}])
        dataset_id = "ds_" + "a" * 64
        manifest = {"dataset_id": dataset_id, "record_counts": {"review_entities": 1717, "written_entities": 807, "occurrences": 1717},
                    "shards": [{"name": "reviews.jsonl"}]}
        export = (json.dumps({"product_url": "https://www.lowes.com/pd/Synthetic", "review_text": "Synthetic public review",
                              "category_id": eval_cli.CATEGORY_ID}) + "\n").encode() * 1717
        for mode in eval_cli.INPUT_MODES:
            with self.subTest(mode=mode):
                output = self.root / mode
                output.mkdir()
                data = SimpleNamespace(call=Mock(return_value=manifest), export=Mock(return_value=export))
                args = SimpleNamespace(report_json=report, selected_runs_json=selected, report_root=self.root,
                    data_root=self.root, effort="low", live=False, input_mode=mode)
                with patch.object(eval_cli, "FrozenDatasetClient", return_value=data) as client, \
                        patch("reporting.research_data.FrozenReviewData") as frozen, \
                        patch.object(eval_cli.socket, "socket", side_effect=AssertionError("network socket")):
                    frozen.return_value.__enter__.return_value = SimpleNamespace(
                        iter_facts=lambda: iter(range(1717)), provenance={"snapshot_id": eval_cli.SNAPSHOT_ID})
                    eval_cli.preflight(args, output)
                receipt = json.loads((output / "preflight.json").read_text())
                self.assertEqual(receipt["input_mode"], mode)
                self.assertEqual(receipt["selected_materials"], {
                    "report_ids": [eval_cli.REPORT_ID] if mode == "report" else [],
                    "dataset_ids": [dataset_id] if mode != "discovery" else []})
                self.assertEqual(receipt["report_id"], eval_cli.REPORT_ID)
                self.assertEqual(receipt["job_id"], eval_cli.JOB_ID)
                self.assertEqual(receipt["public_review_preflight"]["rows"], 1717)
                self.assertFalse(receipt["web_enabled"])
                self.assertFalse(receipt["collection_enabled"])
                client.assert_called_once_with(args.data_root, output, json.loads(selected.read_text()))

    def test_another_report_is_rejected_before_reading_or_freezing_data(self):
        report = self.json_file("report.json", {"report_id": "rpt_another", "data_snapshot_id": eval_cli.SNAPSHOT_ID})
        for mode in eval_cli.INPUT_MODES:
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "report_not_in_authorized"):
                eval_cli.preflight(SimpleNamespace(report_json=report, input_mode=mode), self.root)

    def test_input_modes_cannot_admit_another_source_job(self):
        report = self.json_file("report.json", {"report_id": eval_cli.REPORT_ID, "data_snapshot_id": eval_cli.SNAPSHOT_ID})
        selected = self.json_file("selected.json", [{"job": {"id": "different-job", "brand": "EGO", "model": "LM2236SP", "channel": "lowes"}}])
        for mode in eval_cli.INPUT_MODES:
            with self.subTest(mode=mode), patch.object(eval_cli, "FrozenDatasetClient") as client, \
                    self.assertRaisesRegex(ValueError, "data_not_in_authorized_regression"):
                eval_cli.preflight(SimpleNamespace(report_json=report, selected_runs_json=selected, input_mode=mode), self.root)
            client.assert_not_called()

    def test_preflight_rejects_wrong_actual_frozen_review_category(self):
        report = self.json_file("report.json", {"report_id": eval_cli.REPORT_ID, "data_snapshot_id": eval_cli.SNAPSHOT_ID})
        selected = self.json_file("selected.json", [{"job": {"id": eval_cli.JOB_ID, "brand": "EGO", "model": "LM2236SP", "channel": "lowes"}}])
        manifest = {"dataset_id": "ds_" + "a" * 64,
            "record_counts": {"review_entities": 1717, "written_entities": 807, "occurrences": 1717},
            "shards": [{"name": "reviews.jsonl"}]}
        row = {"product_url": "https://lowes.com/pd/Synthetic", "category_id": "leaf_blower",
               "review_text": "Synthetic category mismatch"}
        data = SimpleNamespace(call=Mock(return_value=manifest), export=Mock(return_value=(json.dumps(row) + "\n").encode()))
        args = SimpleNamespace(report_json=report, selected_runs_json=selected, report_root=self.root,
                               data_root=self.root, effort="low", live=False)
        with patch.object(eval_cli, "FrozenDatasetClient", return_value=data), \
                patch.object(eval_cli.socket, "socket", side_effect=AssertionError("network socket")), \
                self.assertRaisesRegex(ValueError, "review_category_is_not_authorized_mower"):
            eval_cli.preflight(args, self.root)

    def test_unknown_input_mode_is_rejected_before_live_broker_creation(self):
        with patch.object(eval_cli.socket, "socket") as listener, self.assertRaisesRegex(ValueError, "input_mode_invalid"):
            eval_cli.live(SimpleNamespace(input_mode="anywhere"), self.root, None, SimpleNamespace(dataset_id="known"), {})
        listener.assert_not_called()

    def test_disabled_collection_and_web_never_have_a_transport(self):
        from agent.contracts import AgentError
        disabled = eval_cli.DisabledExternalTools()
        for fn in (disabled.call, disabled.search, disabled.fetch):
            with self.assertRaises(AgentError) as caught:
                fn("anything", {})
            self.assertEqual(caught.exception.code, "eval_operation_disabled")

    def test_app_contains_only_production_capability_broker_routes(self):
        app = eval_cli.make_app(SimpleNamespace())
        self.assertEqual({route.path for route in app.routes},
                         {"/wenquan/internal/broker/tools/{name}", "/wenquan/internal/broker/model/v1/chat/completions", "/wenquan/internal/broker/native", "/wenquan/internal/broker/trajectory"})

    def test_dataset_client_accepts_only_ids_frozen_by_this_eval(self):
        from agent.contracts import AgentError
        data = object.__new__(eval_cli.FrozenDatasetClient)
        data.selected, data.available_ids = [], set()
        data.api = SimpleNamespace(open=lambda scope, selected_runs: {"dataset_id": "known"},
                                   query=lambda dataset_id, **kwargs: {"dataset_id": dataset_id, "items": []})
        self.assertEqual(data.call(None, "open", {})["dataset_id"], "known")
        self.assertEqual(data.call(None, "query", {"dataset_id": "known"})["items"], [])
        with self.assertRaises(AgentError):
            data.call(None, "query", {"dataset_id": "another"})

    def frozen_analysis_client(self):
        from reporting import source
        from reporting.tests.test_market_region import SnapshotFixture, _row
        import sqlite3
        fixture = SnapshotFixture(self.root / "analysis-source")
        fixture.add_run(eval_cli.JOB_ID, "North America", [_row("ego-frozen", "North America",
            channel="lowes", text="The mower cuts unevenly on wet grass.")], channel="lowes")
        with sqlite3.connect(fixture.database) as db:
            db.execute("UPDATE jobs SET category='Walk-Behind Mower',brand='EGO',model='LM2236SP'")
        job = source.load_jobs(fixture.root)[0]
        selected = [{"job": job, "run_dir": f"data/runs/{eval_cli.JOB_ID}/lowes"}]
        client = eval_cli.FrozenDatasetClient(fixture.root, self.root / "analysis-output", selected)
        return client

    def test_offline_definition_and_analysis_use_reporting_without_network(self):
        from reporting.research_definition import get_research_definition
        client = self.frozen_analysis_client()
        before = json.dumps(client.selected, sort_keys=True)
        with patch.object(eval_cli.socket, "socket", side_effect=AssertionError("network socket")), \
                patch.object(eval_cli, "load_config", side_effect=AssertionError("credentials read")), \
                patch.object(eval_cli.DisabledExternalTools, "call", side_effect=AssertionError("collection")):
            definition = client.call(None, "definition", {"category_id": eval_cli.CATEGORY_ID})
            manifest = client.call(None, "open", {})
            client.dataset_id = manifest["dataset_id"]
            result = client.call(None, "analyze", {"dataset_id": client.dataset_id, "definition_id": definition["definition_id"]})
        self.assertEqual(definition, get_research_definition(eval_cli.CATEGORY_ID))
        self.assertEqual(result["schema_version"], "wenquan-dataset-analysis/v1")
        self.assertEqual(result["manifest_sha256"], manifest["manifest_sha256"])
        self.assertEqual(result["definition_id"], definition["definition_id"])
        self.assertEqual(result["sample"]["written_entities"], 1)
        self.assertFalse(result["readiness"]["formal_report_created"])
        self.assertEqual(before, json.dumps(client.selected, sort_keys=True))
        self.assertEqual(client.available_ids, {client.dataset_id})

    def test_package_contract_keeps_the_same_frozen_eval_authority(self):
        from agent.contracts import AgentError
        client = self.frozen_analysis_client()
        package = client.call(None, 'prepare', {})
        self.assertTrue(package['coverage']['complete'])
        self.assertEqual(package, client.call(None, 'prepare', {'catalog_id': package['catalog_id']}))
        self.assertEqual(client.available_ids, {package['datasets'][0]['dataset_id']})
        for args in ({'catalog_id': 'foreign'}, {'origin': 'wotu'}, {'catalog_id': package['catalog_id'], 'scope': {'job_ids': ['foreign']}}):
            with self.assertRaises(AgentError):
                client.call(None, 'prepare', args)

    def test_analysis_requires_opened_id_and_keeps_definition_mismatch_409(self):
        from agent.contracts import AgentError
        from reporting.research_definition import get_research_definition
        client = self.frozen_analysis_client()
        # Physically present is insufficient: only call(open) grants this eval
        # access. Another process cannot smuggle a preexisting dataset ID in.
        manifest = client.api.open(selected_runs=client.selected)
        with self.assertRaises(AgentError) as error:
            client.call(None, "analyze", {"dataset_id": manifest["dataset_id"]})
        self.assertEqual(error.exception.http_status, 404)
        client.call(None, "open", {})
        for definition in (get_research_definition("leaf_blower")["definition_id"], "rdef_" + "0" * 64):
            with self.assertRaises(AgentError) as error:
                client.call(None, "analyze", {"dataset_id": manifest["dataset_id"], "definition_id": definition})
            self.assertEqual(error.exception.http_status, 409)
            self.assertEqual(error.exception.code, "analysis_definition_mismatch")
        with self.assertRaises(AgentError) as error:
            client.call(None, "analyze", {"dataset_id": "ds_" + "f" * 64})
        self.assertEqual(error.exception.http_status, 404)

    def test_definition_and_analysis_cannot_expand_category_or_accept_paths(self):
        from agent.contracts import AgentError
        client = self.frozen_analysis_client()
        manifest = client.call(None, "open", {})
        for operation, payload, status in (
            ("definition", {"category_id": "leaf_blower"}, 403),
            ("definition", {"category_id": eval_cli.CATEGORY_ID, "path": "/etc"}, 422),
            ("analyze", {"dataset_id": manifest["dataset_id"], "script": "/tmp/other.py"}, 422),
            ("analyze", {"dataset_id": manifest["dataset_id"], "category_id": "leaf_blower"}, 422),
            ("analyze", {"dataset_id": []}, 404),
            ("analyze", [], 422),
        ):
            with self.subTest(operation=operation, payload=payload), self.assertRaises(AgentError) as error:
                client.call(None, operation, payload)
            self.assertEqual(error.exception.http_status, status)
        client.selected[0]["job"]["category"] = "Leaf Blower"
        with self.assertRaises(AgentError) as error:
            client.call(None, "definition", {"category_id": "leaf_blower"})
        self.assertEqual(error.exception.http_status, 403)

    def test_live_lifecycle_exports_real_stores_and_closes_its_broker_without_network(self):
        self.assert_live_lifecycle()

    def test_discovery_live_lifecycle_has_no_initial_report_or_dataset(self):
        self.assert_live_lifecycle("discovery")

    def test_dataset_live_lifecycle_associates_only_the_frozen_dataset(self):
        self.assert_live_lifecycle("dataset")

    def assert_live_lifecycle(self, input_mode=None):
        from agent.tests.test_harness_research import SyntheticData, SyntheticManager, DATASET_ID
        manager, data, listener = SyntheticManager(), SyntheticData(), Mock()
        data.dataset_id = DATASET_ID
        output = self.root / "offline-lifecycle"
        output.mkdir(mode=0o700)
        server_instances = []

        class FakeServer:
            def __init__(self, config):
                self.started = False
                self.should_exit = False
                server_instances.append(self)

            def run(self, **kwargs):
                self.started = True
                while not self.should_exit:
                    threading.Event().wait(0.01)

        real_make_app = eval_cli.make_app

        def scripted_app(research):
            def answer(payload):
                cap = research.broker.resolve("Bearer " + payload["capability"])
                if input_mode == "discovery":
                    catalog = research.tool(cap, "list_datasets", {})
                    self.assertEqual(len(catalog["items"]), 1)
                    opened = research.tool(cap, "open_dataset", {"scope": {"job_ids": [catalog["items"][0]["job_id"]]}})
                    self.assertEqual(opened['datasets'][0]["dataset_id"], DATASET_ID)
                result = research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "limit": 1})
                research.tool(cap, "submit_answer", {"answer": "这是离线流程测试。[" + result["items"][0]["source_id"] + "]"})
            manager.on_start = answer
            return real_make_app(research)

        fake_uvicorn = SimpleNamespace(Server=FakeServer, Config=lambda *args, **kwargs: None)
        with patch.dict("sys.modules", {"uvicorn": fake_uvicorn}), \
                patch("agent.harness_manager.HarnessManager", return_value=manager), \
                patch.object(eval_cli.socket, "socket", return_value=listener), \
                patch.object(eval_cli, "make_app", side_effect=scripted_app):
            args = SimpleNamespace(effort="low")
            if input_mode is not None:
                args.input_mode = input_mode
            result = eval_cli.live(args, output, None, data,
                                   {"manager_token": "m" * 40, "VOXQUARRY_LLM_API_KEY": "k" * 115,
                                    "VOXQUARRY_LLM_BASE_URL": eval_cli.PROVIDER})
        self.assertEqual(result, 0)
        self.assertTrue(server_instances[0].should_exit)
        listener.close.assert_called_once()
        self.assertIn("离线流程测试", (output / "answer.md").read_text())
        export = json.loads((output / "eval-export.json").read_text())
        self.assertEqual(export["cases"][0]["status"], "completed")
        self.assertTrue(manager.stops)
        final = json.loads((output / "result.json").read_text())
        self.assertEqual(final["run"]["status"], "completed")
        mode = input_mode or "report"
        expected = {"report_ids": [eval_cli.REPORT_ID] if mode == "report" else [],
                    "dataset_ids": [DATASET_ID] if mode != "discovery" else []}
        self.assertEqual(final["input_mode"], mode)
        self.assertEqual(final["selected_materials"], expected)
        started = json.loads((output / "started.json").read_text())
        self.assertEqual(started["input_mode"], mode)
        self.assertEqual(started["selected_materials"], expected)
        prompt = json.loads(manager.starts[0]["prompt"])
        for key, ids in expected.items():
            self.assertEqual(prompt["selected_materials"][key], ids)
            self.assertEqual(final["run"]["request"][key], ids)
        self.assertFalse(prompt["web_enabled"])


if __name__ == "__main__":
    unittest.main()
