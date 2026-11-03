"""Local methods must agree with the server's existing normalized populations."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.research_methods import ResearchDataError, ReviewSet, iter_jsonl, load_result, schema
from reporting.tests.test_market_region import SnapshotFixture, _row
from reporting.wenquan_datasets import DatasetError, WenquanDatasets, _hash, _json
from reporting import source
from reporting.wenquan_union import aggregate_union
from reporting.wenquan_data_routes import SharedResearchData
from agent.tests.test_harness_research import HarnessFixture
from agent.research_delivery import delivery_issues


class ResearchMethodsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = SnapshotFixture(self.root / "source")
        self.service = WenquanDatasets(self.fixture.root, self.root / "datasets")

    def add(self, job_id, rows, channel="ego"):
        self.fixture.add_run(job_id, "North America", rows, channel=channel)

    def export(self, scope=None):
        manifest = self.service.open(scope)
        paths = []
        for part in manifest["shards"]:
            path = self.root / (manifest["dataset_id"] + part["name"])
            path.write_bytes(b"".join(self.service.export(manifest["dataset_id"], shard=part["name"])))
            paths.append(str(path))
        return {"dataset_id": manifest["dataset_id"], "manifest_sha256": manifest["manifest_sha256"],
                "manifest": manifest, "analysis_paths": paths, "raw_payload_paths": ["/must/not/be/read.jsonl"]}

    def checked(self, exports):
        return ReviewSet(exports, temp_dir=self.root)

    def reseal(self, item):
        """Construct malformed-content fixtures without a checksum mismatch masking them."""
        for path, part in zip(item["analysis_paths"], item["manifest"]["shards"]):
            content = Path(path).read_bytes()
            part.update(bytes=len(content), records=len(content.splitlines()), sha256="sha256:" + hashlib.sha256(content).hexdigest())
        item["manifest"]["record_counts"]["occurrences"] = sum(part["records"] for part in item["manifest"]["shards"])
        material = {k: v for k, v in item["manifest"].items() if k != "manifest_sha256"}
        item["manifest_sha256"] = item["manifest"]["manifest_sha256"] = _hash(material)

    def test_complete_stream_retains_tail_and_generic_reader_rejects_invalid_tail(self):
        text = "  Battery\nfull original " + "context " * 20000 + "FINAL_SENTINEL"
        self.add("one", [_row("r1", "North America", text=text)])
        exported = self.export()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("bulk read")):
            with self.checked([exported]) as reviews:
                row = next(reviews.rows())
                self.assertTrue(row["review_text"].endswith("FINAL_SENTINEL"))
                self.assertEqual(row["original_fields"]["review_text"], text)
                self.assertEqual(reviews.coverage()["record_counts"]["review_entities"], 1)
        path = self.root / "plain.jsonl"
        path.write_text('{"record":1}\n{"record":2}\ninvalid\n')
        iterator = iter_jsonl(path)
        self.assertEqual(next(iterator), {"record": 1})
        self.assertEqual(next(iterator), {"record": 2})
        with self.assertRaises(ResearchDataError):
            next(iterator)

    def test_cross_batch_overlap_unknown_identity_and_canonical_before_filter_match_server(self):
        same = _row("same", "North America", text="Battery Straße: full context")
        self.add("one", [same, _row("", "North America"), _row("", "North America"),
                         {**_row("empty", "North America", text="", valid_text="false"), "rating": "", "submission_time": ""},
                         {**_row("low", "North America", text="The battery failed"), "rating": "1"}])
        self.add("two", [{**same, "channel": "lowes"}, _row("new", "North America", channel="lowes")], channel="lowes")
        first = self.export({"job_ids": ["one"]})
        second = self.export({"job_ids": ["two"]})
        overlapping = self.export()
        exports = [second, overlapping, first]
        ids = [item["dataset_id"] for item in exports]
        selectors = [
            {}, {"query": "STRASSE"}, {"filters": {"channels": ["lowes"]}},
            {"filters": {"ratings": [1, 5], "has_text": True}},
            {"filters": {"brands": ["EGO"], "models": ["MODEL-one"]}},
            {"filters": {"source_product_ids": ["source-same"]}},
            {"filters": {"market_regions": ["North America"], "category_ids": ["leaf_blower"]}},
            {"filters": {"job_ids": ["one"], "date_start": "2026-07-01", "date_end": "2026-07-31"}},
            {"filters": {"ratings": []}}, {"query": "not present"},
        ]
        with self.checked(exports) as reviews:
            coverage = reviews.coverage(expected_dataset_ids=ids)
            self.assertTrue(coverage["requested_datasets_complete"])
            self.assertEqual(coverage["record_counts"], {"loaded_occurrences": 14, "occurrences": 7,
                             "overlapping_occurrences": 7, "review_entities": 6,
                             "written_occurrences": 6, "written_entities": 5})
            self.assertEqual(len(list(reviews.rows())), 6)
            self.assertEqual(len(list(reviews.rows(population="occurrences"))), 7)
            for population in ("entities", "occurrences"):
                for groups in ([], ["rating"], ["channel", "has_text"], ["submission_month", "brand", "model"]):
                    for selector in selectors:
                        arguments = dict(population=population, group_by=groups, **selector)
                        with self.subTest(**arguments):
                            remote = aggregate_union(self.service, ids, **arguments)
                            local = reviews.aggregate(**arguments)
                            self.assertEqual(local, {key: remote[key] for key in local})
            # The Lowes occurrence of the shared entity is not its canonical row.
            self.assertEqual(reviews.aggregate(filters={"channels": ["lowes"]})["total"], 1)
            self.assertEqual(reviews.aggregate(filters={"channels": ["lowes"]}, population="occurrences")["total"], 2)
            self.assertEqual(reviews.coverage(expected_dataset_ids=ids + ["missing"])["missing_dataset_ids"], ["missing"])

    def test_single_batch_every_group_field_matches_existing_aggregate(self):
        self.add("one", [_row("a", "North America"), _row("b", "North America", text="", valid_text="false")])
        exported = self.export()
        with self.checked([exported]) as reviews:
            for population in ("entities", "occurrences"):
                for group in schema()["group_fields"]:
                    with self.subTest(population=population, group=group):
                        arguments = dict(group_by=[group], population=population)
                        local = reviews.aggregate(**arguments)
                        remote = self.service.aggregate(exported["dataset_id"], **arguments)
                        self.assertEqual(local, {key: remote[key] for key in local})

    def test_empty_frozen_export_is_zero_not_missing_files_and_source_coverage_is_separate(self):
        self.add("one", [_row("a", "North America")])
        item = self.export()
        item["analysis_paths"] = []
        item["manifest"]["shards"] = []
        item["manifest"]["source_complete"] = False
        self.reseal(item)
        with self.checked([item]) as reviews:
            self.assertEqual(reviews.aggregate(), {"population": "entities", "groups": [{"group": {}, "count": 0}], "total": 0, "count_basis": "去重评论实体"})
            self.assertEqual(reviews.aggregate(group_by=["rating"])["groups"], [])
            self.assertEqual(list(reviews.rows()), [])
            info = reviews.coverage()
            self.assertTrue(info["export_complete"])
            self.assertFalse(info["source_complete"])
            self.assertIsNone(info["requested_datasets_complete"])
            with self.assertRaises(ResearchDataError):
                reviews.citation("invented")

    def test_missing_corrupt_shards_and_conflicting_manifests_fail_before_any_result(self):
        self.service.limits = replace(self.service.limits, shard_bytes=4000)
        self.add("one", [_row(str(n), "North America") for n in range(5)])
        exported = self.export()
        self.assertGreater(len(exported["analysis_paths"]), 1)
        bad = {**exported, "analysis_paths": exported["analysis_paths"][:-1]}
        with self.assertRaises(ResearchDataError) as caught:
            self.checked([bad])
        self.assertEqual(caught.exception.code, "incomplete_export")
        self.assertFalse(list(self.root.glob(".research-methods-*")))
        bad = copy.deepcopy(exported)
        bad["manifest"]["scope"] = {"brands": ["different"]}
        with self.assertRaises(ResearchDataError) as caught:
            self.checked([bad])
        self.assertEqual(caught.exception.code, "dataset_changed")
        last = Path(exported["analysis_paths"][-1])
        last.write_bytes(last.read_bytes().replace(b"Fixture", b"Mutated"))
        with self.assertRaises(ResearchDataError) as caught:
            self.checked([exported])
        self.assertEqual(caught.exception.code, "dataset_changed")

    def test_overlapping_content_conflict_and_mixed_normalization_are_not_silent_dedup(self):
        self.add("one", [_row("a", "North America")])
        first, second = self.export({"job_ids": ["one"]}), self.export()
        second = copy.deepcopy(second)
        second["manifest"]["normalization_version"] = "future/99"
        self.reseal(second)
        with self.assertRaises(ResearchDataError) as caught:
            self.checked([first, second])
        self.assertEqual(caught.exception.code, "mixed_normalization_versions")
        second["manifest"]["normalization_version"] = first["manifest"]["normalization_version"]
        path = Path(second["analysis_paths"][0])
        row = json.loads(path.read_text())
        row["original_fields"]["where_purchase"] = "conflicting frozen source field"
        path.write_text(json.dumps(row) + "\n")
        self.reseal(second)
        with self.assertRaises(ResearchDataError) as caught:
            self.checked([first, second])
        self.assertEqual(caught.exception.code, "overlapping_version_conflict")

    def test_duplicate_occurrence_inside_one_export_is_invalid(self):
        self.add("one", [_row("a", "North America")])
        exported = self.export()
        path = Path(exported["analysis_paths"][0])
        path.write_bytes(path.read_bytes() * 2)
        self.reseal(exported)
        with self.assertRaises(ResearchDataError) as caught:
            self.checked([exported])
        self.assertEqual(caught.exception.code, "invalid_source")

    def test_legacy_and_changed_parser_contracts_match_server_union_compatibility(self):
        self.add("one", [_row("a", "North America")])
        current = self.export()
        old_manifest = {**current["manifest"], "version": "wenquan-dataset/1.1.0"}
        old_manifest.pop("normalization_contracts")
        identity = {key: old_manifest[key] for key in ("contract", "version", "normalization_version", "scope", "inputs", "shard_bytes")}
        old_id = "ds_" + _hash(identity)[7:]
        old_manifest.update(dataset_id=old_id, dataset_version=old_id)
        old_manifest["manifest_sha256"] = _hash({key: value for key, value in old_manifest.items() if key != "manifest_sha256"})
        destination = self.service.storage_root / old_id
        shutil.copytree(self.service.storage_root / current["dataset_id"], destination)
        manifest_path = destination / "manifest.json"
        manifest_path.chmod(0o600)
        manifest_path.write_text(_json(old_manifest))
        manifest_path.chmod(0o400)
        old = {"dataset_id": old_id, "manifest": old_manifest,
               "analysis_paths": [str(destination / p["name"]) for p in old_manifest["shards"]]}
        with self.checked([old]) as reviews:
            remote = aggregate_union(self.service, [old_id], group_by=["rating"])
            local = reviews.aggregate(group_by=["rating"])
            self.assertEqual(local, {key: remote[key] for key in local})
            self.assertEqual(reviews.coverage()["normalization_contracts"], "legacy-unversioned-contracts")
        with patch.object(source, "PARSER_VERSION", "fixture-parser/future"):
            changed = self.export()
        self.assertEqual(current["manifest"]["normalization_version"], changed["manifest"]["normalization_version"])
        for exports in ([old, current], [current, changed]):
            with self.subTest(datasets=[item["dataset_id"] for item in exports]):
                with self.assertRaises(ResearchDataError) as local_error:
                    self.checked(exports)
                self.assertEqual(local_error.exception.code, "mixed_normalization_versions")
                with self.assertRaises(DatasetError) as remote_error:
                    aggregate_union(self.service, [item["dataset_id"] for item in exports])
                self.assertEqual(remote_error.exception.diagnostic["reason"], "mixed_normalization_versions")

    def test_citations_include_noncanonical_occurrences_and_require_actual_frozen_rows(self):
        row = _row("same", "North America")
        self.add("one", [row])
        self.add("two", [{**row, "channel": "lowes"}], channel="lowes")
        item = self.export()
        with self.checked([item]) as reviews:
            rows = list(reviews.rows(population="occurrences"))
            duplicate = next(r for r in rows if r["is_duplicate_occurrence"])
            self.assertEqual(reviews.citation(duplicate["record_id"]), f"[review:{item['dataset_id']}:{duplicate['record_id']}]")
            handles = reviews.citations([r["record_id"] for r in rows] * 2)
            self.assertEqual(len(handles), 2)
            self.assertNotIn("dsrc_", "".join(handles))
            for missing in ("absent", "unknown:identifier"):
                with self.assertRaises(ResearchDataError):
                    reviews.citation(missing)
            with self.assertRaises(ResearchDataError):
                reviews.citation(rows[0]["record_id"], dataset_id="ds_wrong")
            path = Path(item["analysis_paths"][0])
            path.write_bytes(path.read_bytes().replace(b"Fixture", b"Mutated"))
            with self.assertRaises(ResearchDataError) as caught:
                reviews.citation(rows[0]["record_id"])
            self.assertEqual(caught.exception.code, "dataset_changed")

    def test_known_result_paths_are_explicit_and_bad_selectors_do_not_silently_expand_scope(self):
        path = self.root / "result.json"
        path.write_text(json.dumps({"result": {"data": [1]}, "exports": [], "a/b": {"~": 4}}))
        self.assertEqual(load_result(path, "/result/data/0"), 1)
        self.assertEqual(load_result(path, "/a~1b/~0"), 4)
        self.assertEqual(load_result(path, "/exports"), [])
        for pointer in ("/result/items", "/result/data/01", "/result/data/-1", "/~2"):
            with self.assertRaises(ResearchDataError):
                load_result(path, pointer)
        self.add("one", [_row("a", "North America")])
        with self.checked([self.export()]) as reviews:
            invalid = [{"filters": {"brand": ["EGO"]}}, {"filters": {"ratings": [True]}},
                       {"filters": {"has_text": "true"}}, {"filters": {"date_start": "2026-7-1"}},
                       {"group_by": ["rating", "rating"]}, {"population": "people"}]
            for arguments in invalid:
                with self.subTest(**arguments), self.assertRaises(ResearchDataError):
                    reviews.aggregate(**arguments)

    def test_cli_and_analysis_image_import_contract(self):
        self.add("one", [_row("a", "North America")])
        path = self.root / "results.json"
        path.write_text(json.dumps({"exports": [self.export()]}))
        result = subprocess.run([sys.executable, "-m", "agent.research_methods", "aggregate", "--exports", str(path), "--pointer", "/exports", "--group-by", "rating"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["groups"], [{"group": {"rating": 5}, "count": 1}])
        result = subprocess.run([sys.executable, "-m", "agent.research_methods", "coverage", "--exports", str(path), "--pointer", "/wrong"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "result_path_missing")
        dockerfile = Path("runtime/harness/Dockerfile").read_text()
        analysis = dockerfile.split("FROM base AS analysis")[1].split("FROM base AS runner")[0]
        self.assertIn("COPY agent/research_methods.py /usr/local/lib/python3.11/site-packages/research_methods.py", analysis)


class ResearchPackageMethodsIntegrationTests(HarnessFixture, unittest.TestCase):
    def test_real_frozen_package_loads_locally_and_cites_noncanonical_review_without_requery_tool(self):
        root = Path(self.temp.name)
        fixture = SnapshotFixture(root / "snapshot")
        original = _row("same", "North America")
        fixture.add_run("one", "North America", [original])
        fixture.add_run("two", "North America", [{**original, "channel": "lowes"}], channel="lowes")
        backend = SharedResearchData(SimpleNamespace(snapshot_root=fixture.root, var_root=root / "reporting"))
        def call(actor, operation, arguments):
            if operation == "prepare":
                return backend.prepare(**arguments)
            if operation == "manifest":
                return backend.snapshot.manifest(arguments["dataset_id"])
            if operation == "query":
                return backend.snapshot.query(**arguments)
            raise AssertionError(operation)
        self.research.data = SimpleNamespace(call=call, export=lambda actor, dataset_id, shard, source_type:
                                            b"".join(backend.snapshot.export(dataset_id, shard=shard, source_type=source_type)))
        run = self.accept()
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question=run["request"]["question"])
        _, cap = self.research.broker.issue(self.user, run, trace["trace_id"])
        result = self.research.tool(cap, "open_dataset", {})
        self.assertNotIn("error", result)
        self.assertEqual(delivery_issues(self.state.get(self.user, run["run_id"])["checkpoint"]), [])
        inputs = root / "analysis-inputs"
        inputs.mkdir()
        for _, name, content in self.manager.inputs:
            (inputs / name).write_bytes(content)
        package = inputs / Path(result["package_path"]).name
        exports = load_result(package, "/datasets")
        for exported in exports:
            # Emulate the container's input mount without writing the host /inputs.
            exported["analysis_paths"] = [str(inputs / Path(p).name) for p in exported["analysis_paths"]]
        with ReviewSet(exports, temp_dir=root) as reviews:
            self.assertEqual(reviews.aggregate()["total"], 1)
            self.assertEqual(reviews.aggregate(population="occurrences")["total"], 2)
            noncanonical = next(row for row in reviews.rows(population="occurrences") if row["is_duplicate_occurrence"])
            handle = reviews.citation(noncanonical["record_id"])
        answer = self.research._resolve_review_citations(self.user, run["run_id"], "完整原评 " + handle)
        self.assertIn("[src_", answer)
        self.assertNotIn("[review:", answer)
        # The native model did not need a second query_dataset tool call.
        self.assertEqual(cap.tool_calls, 1)

    def test_compact_preparation_continues_same_scope_and_latest_package_is_not_double_counted(self):
        root = Path(self.temp.name)
        fixture = SnapshotFixture(root / "snapshot")
        fixture.add_run("one", "North America", [_row("one", "North America")])
        fixture.add_run("two", "North America", [_row("two", "North America")])
        backend = SharedResearchData(SimpleNamespace(snapshot_root=fixture.root, var_root=root / "reporting"))
        preparations = []
        def call(actor, operation, arguments):
            if operation == "prepare":
                result = backend.prepare(**arguments)
                preparations.append(result)
                return result
            if operation == "manifest":
                return backend.snapshot.manifest(arguments["dataset_id"])
            raise AssertionError(operation)
        self.research.data = SimpleNamespace(call=call, export=lambda actor, dataset_id, shard, source_type:
                                            b"".join(backend.snapshot.export(dataset_id, shard=shard, source_type=source_type)))
        run = self.accept()
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question=run["request"]["question"])
        _, cap = self.research.broker.issue(self.user, run, trace["trace_id"])
        with patch("reporting.wenquan_catalog.BATCH_RUNS", 1):
            first = self.research.tool(cap, "open_dataset", {"scope": {"brands": ["EGO"]}, "max_batches": 1})
        self.assertFalse(first["coverage"]["complete"])
        self.assertEqual(first["coverage"]["prepared_batches"], 1)
        self.assertEqual(len(delivery_issues(self.state.get(self.user, run["run_id"])["checkpoint"])), 1)
        second = self.research.tool(cap, "open_dataset", {"catalog_id": first["catalog_id"]})
        self.assertNotIn("error", second)
        self.assertTrue(second["coverage"]["complete"])
        self.assertEqual(second["scope"], {"brands": ["EGO"]})
        self.assertTrue(all("catalog_binding" not in dataset and "batch_id" in dataset for dataset in preparations[-1]["datasets"]))
        self.assertEqual(delivery_issues(self.state.get(self.user, run["run_id"])["checkpoint"]), [])
        inputs = root / "analysis-inputs"
        inputs.mkdir()
        for _, name, content in self.manager.inputs:
            (inputs / name).write_bytes(content)
        exports = load_result(inputs / Path(second["package_path"]).name, "/datasets")
        self.assertEqual(len(exports), 2)
        self.assertEqual(len({item["dataset_id"] for item in exports}), 2)
        for exported in exports:
            exported["analysis_paths"] = [str(inputs / Path(p).name) for p in exported["analysis_paths"]]
        with ReviewSet(exports, temp_dir=root) as reviews:
            self.assertEqual(reviews.aggregate()["total"], 2)


if __name__ == "__main__":
    unittest.main()
