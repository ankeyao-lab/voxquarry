import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from types import SimpleNamespace

import httpx

from agent.contracts import AgentError, IdentityContext
from agent.research_clients import ResearchAuthorityClient, ResearchCollectionClient, ResearchDataClient
from agent.upstream import HttpZhuojianTransport

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)


class ResearchClientTests(unittest.TestCase):
    def test_catalog_contract_negotiation_does_not_change_user_arguments(self):
        calls = []
        transport = SimpleNamespace(_post=lambda path, payload, actor, **options: calls.append((payload, options)) or {})
        args = {'scope': {'brands': ['EGO']}}
        ResearchDataClient(transport).call(None, 'catalog', args)
        self.assertEqual(calls[0][0]['catalog_contract'], 'voxquarry.wenquan-catalog/v2')
        self.assertNotIn('catalog_contract', args)
        self.assertNotIn('max_response_bytes', calls[0][1])
    def test_source_reason_and_capacity_survive_without_untrusted_diagnostics(self):
        detail = {'code': 'dataset_too_large', 'message': 'bounded failure', 'diagnostic': {
            'reason': 'source_metadata_bytes', 'observed': 40000000, 'limit': 33554432,
            'request_id': 'a' * 32, 'path': '/private/source', 'body': 'private prompt'}}
        transport = SimpleNamespace(_post=lambda *args, **kwargs: {'error': detail})
        with self.assertRaises(AgentError) as raised:
            ResearchDataClient(transport).call(None, 'catalog', {})
        self.assertEqual(raised.exception.upstream_diagnostic, {
            'code': 'dataset_too_large', 'reason': 'source_metadata_bytes', 'observed': 40000000,
            'limit': 33554432, 'request_id': 'a' * 32})
        detail['diagnostic'] = {'reason': 'private prompt'}
        with self.assertRaises(AgentError) as raised:
            ResearchDataClient(transport).call(None, 'catalog', {})
        self.assertFalse(hasattr(raised.exception, 'upstream_diagnostic'))

    def test_structured_diagnostics_keep_numeric_evidence_and_drop_paths(self):
        detail = {'code': 'invalid_source', 'diagnostic': {'reason': 'validation_count_mismatch',
            'phase': 'validation_manifest', 'field': 'eligible_reviews', 'expected': 3, 'observed': 4,
            'sqlite_code': 5, 'source_reason': 'eligible_excluded_mismatch',
            'counts': {'eligible_reviews': 3, 'excluded_reviews': True, 'private_path': '/secret'}, 'exception': '/private/trace'}}
        transport = SimpleNamespace(_post=lambda *args, **kwargs: {'error': detail})
        with self.assertRaises(AgentError) as raised:
            ResearchDataClient(transport).call(None, 'prepare', {})
        safe = raised.exception.upstream_diagnostic
        self.assertEqual((safe['expected'], safe['observed'], safe['sqlite_code']), (3, 4, 5))
        self.assertEqual(safe['counts'], {'eligible_reviews': 3})
        self.assertNotIn('exception', safe)
        from agent.research_clients import DATA_FIELDS, DATA_PHASES, COMPLETENESS_REASONS
        from reporting.wenquan_datasets import DIAGNOSTIC_FIELDS, DIAGNOSTIC_PHASES, COMPLETENESS_REASONS as source_reasons
        self.assertEqual(DATA_FIELDS, DIAGNOSTIC_FIELDS)
        self.assertEqual(DATA_PHASES, DIAGNOSTIC_PHASES)
        self.assertEqual(COMPLETENESS_REASONS, source_reasons)

    def test_collection_http_errors_are_not_connection_failures(self):
        for status, code, retry in ((500, 'collection_server_error', False), (503, 'collection_unavailable', True),
                                    (403, 'collection_forbidden', False), (422, 'collection_rejected', False)):
            response = httpx.Response(status, text='private traceback', headers={'x-request-id': 'req_fixture'})
            client = ResearchCollectionClient('http://collector', 'private', client=httpx.Client(transport=httpx.MockTransport(lambda req: response)))
            with self.subTest(status=status), self.assertRaises(AgentError) as raised:
                client.call('ensure', {})
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.retryable, retry)
            self.assertEqual(raised.exception.upstream_diagnostic['status'], status)
            self.assertNotIn('traceback', str(raised.exception))

    def test_reporting_plaintext_500_preserves_status_without_leaking_body(self):
        transport = HttpZhuojianTransport(base_url='http://reporting', gateway_token='fixture')
        actor = IdentityContext('actor', 'owner', 'scope', 'authority', mode='synthetic')
        error = urllib.error.HTTPError('http://reporting', 500, 'failed', {}, io.BytesIO(b'private traceback'))
        with patch('agent.upstream.urllib.request.urlopen', side_effect=error), self.assertRaises(AgentError) as raised:
            transport._post('/reporting/api/v1/research-data/open', {}, actor)
        self.assertEqual(raised.exception.code, 'upstream_server_error')
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.upstream_diagnostic['status'], 500)

    def claims(self, **changes):
        return {"schema_version": "voxquarry.research-authority/v1", "principal_id": "prn_user",
            "required_capability": "chat.session.own", "roles": ["user"], "allowed": True,
            "owner_scope": "principal:prn_user", "mode": "local_dev_account_admin",
            "authorization_version": "authv_" + "a" * 64, "checked_at": NOW.isoformat(), "ttl_seconds": 30,
            **changes}

    def client(self, response):
        return ResearchAuthorityClient("http://private-edge:80", "a" * 40,
            transport=httpx.MockTransport(lambda req: response(req) if callable(response) else httpx.Response(200, json=response)), clock=lambda: NOW)

    def test_authority_protocol_is_fixed_and_public_fields_only(self):
        seen = []
        def response(req):
            seen.append(req)
            return httpx.Response(200, json=self.claims(private_guid="not-forwarded"))
        value = self.client(response).authorize("prn_user", "chat.session.own")
        self.assertTrue(value["allowed"])
        self.assertNotIn("private_guid", value)
        self.assertEqual(str(seen[0].url), "http://private-edge/account-admin/internal/research-authorize")
        self.assertEqual(json.loads(seen[0].content), {"principal_id": "prn_user", "required_capability": "chat.session.own"})
        self.assertNotIn("cookie", seen[0].headers)

    def test_identity_owner_capability_schema_and_mode_must_match(self):
        for changes in ({"principal_id": "someone_else"}, {"owner_scope": "all"}, {"required_capability": "collect.command"},
                        {"schema_version": "v0"}, {"mode": "production"}, {"roles": ["unknown"]}, {"allowed": 1},
                        {"authorization_version": "bad"}, {"ttl_seconds": 300}, {"ttl_seconds": True}):
            with self.subTest(changes=changes), self.assertRaises(AgentError):
                self.client(self.claims(**changes)).authorize("prn_user", "chat.session.own")

    def test_expired_future_and_timezone_less_authority_rejected(self):
        for timestamp in ((NOW - timedelta(seconds=30)).isoformat(), (NOW + timedelta(seconds=6)).isoformat(), "2026-09-14T08:00:00"):
            with self.assertRaises(AgentError):
                self.client(self.claims(checked_at=timestamp)).authorize("prn_user", "chat.session.own")

    def test_resume_must_match_pinned_password_and_role_revision(self):
        with self.assertRaises(AgentError) as raised:
            self.client(self.claims()).authorize("prn_user", "chat.session.own", expected_authorization_version="authv_" + "b" * 64)
        self.assertEqual(raised.exception.code, "research_authorization_changed")

    def test_errors_and_redirects_fail_closed_without_upstream_details(self):
        for status in (302, 403, 409, 503):
            with self.assertRaises(AgentError) as raised:
                self.client(lambda req: httpx.Response(status, text="private ldap details", headers={"location": "http://other/secret"})).authorize("prn_user", "chat.session.own")
            self.assertNotIn("ldap", str(raised.exception))
        with self.assertRaises(AgentError):
            self.client(lambda req: httpx.Response(200, content=b"x" * 20000)).authorize("prn_user", "chat.session.own")

    def test_unconfigured_or_non_private_authority_never_issues_request(self):
        with self.assertRaises(AgentError):
            ResearchAuthorityClient("https://example.com", "a" * 40)
        with self.assertRaises(AgentError):
            ResearchAuthorityClient("http://private-edge/path", "a" * 40)
        with self.assertRaises(AgentError):
            ResearchAuthorityClient("", "").authorize("prn_user", "chat.session.own")

    def test_collection_status_keeps_research_run_query_parameter(self):
        seen = []
        def response(req):
            seen.append(req)
            return httpx.Response(200, json={"status": "queued"})
        client = ResearchCollectionClient("http://collector", "private", client=httpx.Client(transport=httpx.MockTransport(response)))
        client.call("status", {"request_key": "request_one", "research_run_id": "research_one"})
        self.assertEqual(seen[0].url.params["research_run_id"], "research_one")
        self.assertEqual(seen[0].url.path, "/api/research/operations/request_one")

    def test_raw_export_propagates_source_type(self):
        seen = []
        def response(req):
            seen.append(req)
            return httpx.Response(200, content=b'{"raw":"complete"}\n')
        client = httpx.Client(transport=httpx.MockTransport(response))
        transport = SimpleNamespace(_base_url="http://reporting", _headers=lambda actor: {})
        with patch("agent.research_clients.httpx.Client", return_value=client):
            result = ResearchDataClient(transport).export(None, "ds_fixture", "raw-00000-00000.jsonl", source_type="channel_raw_payload")
        self.assertEqual(result, b'{"raw":"complete"}\n')
        self.assertEqual(json.loads(seen[0].content)["source_type"], "channel_raw_payload")

    def test_complete_analysis_evidence_survives_legacy_one_mb_ceiling(self):
        actor = IdentityContext("actor", "owner", "scope", "authority", mode="synthetic")
        transport = HttpZhuojianTransport(base_url="http://127.0.0.1:8787", gateway_token="fixture")
        # Real single-product rule output is 1,041,940 bytes. Keep the tail
        # marker to catch a transport that silently accepts a partial result.
        body = {"evidence": [{"quote": "x" * 1_050_000}], "last_record_id": "review_final"}
        raw = json.dumps(body).encode()
        with patch("agent.upstream.urllib.request.urlopen", return_value=io.BytesIO(raw)):
            value = ResearchDataClient(transport).call(actor, "analyze", {"dataset_id": "ds_fixture"})
        self.assertEqual(value, body)
        for operation in ("catalog", "open", "query", "definition"):
            with self.subTest(operation=operation), patch("agent.upstream.urllib.request.urlopen", return_value=io.BytesIO(raw)):
                with self.assertRaisesRegex(AgentError, "超过大小限制"):
                    ResearchDataClient(transport).call(actor, operation, {})

    def test_source_contract_rejections_are_not_advertised_as_transient(self):
        actor = IdentityContext("actor", "owner", "scope", "authority", mode="synthetic")
        transport = HttpZhuojianTransport(base_url="http://127.0.0.1:8787", gateway_token="fixture")
        for detail, retryable in (({"code": "unsafe_source"}, False), ({"code": "invalid_source"}, False),
                                  ({"code": "dataset_busy"}, True), ({"code": "custom_rejection", "retryable": False}, False)):
            raw = json.dumps({"error": {"message": "fixture rejection", **detail}}).encode()
            with self.subTest(code=detail["code"]), patch("agent.upstream.urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                    "http://127.0.0.1", 409, "rejected", {}, io.BytesIO(raw))):
                with self.assertRaises(AgentError) as raised:
                    ResearchDataClient(transport).call(actor, "open", {"scope": {"job_ids": ["sample-job"]}})
                self.assertEqual(raised.exception.code, detail["code"])
                self.assertEqual(raised.exception.retryable, retryable)

    def test_analysis_success_and_error_responses_remain_bounded(self):
        actor = IdentityContext("actor", "owner", "scope", "authority", mode="synthetic")
        transport = HttpZhuojianTransport(base_url="http://127.0.0.1:8787", gateway_token="fixture")
        raw = b" " * (8 * 1024**2 + 1)
        for failed in (False, True):
            stream = io.BytesIO(raw)
            options = {"side_effect": urllib.error.HTTPError("http://127.0.0.1", 502, "failed", {}, stream)} if failed else {"return_value": stream}
            with self.subTest(failed=failed), patch("agent.upstream.urllib.request.urlopen", **options):
                with self.assertRaisesRegex(AgentError, "超过大小限制"):
                    ResearchDataClient(transport).call(actor, "analyze", {})
                if failed:
                    self.assertEqual(stream.tell(), 8 * 1024**2 + 1)

    def test_invalid_transport_allowance_never_starts_request(self):
        transport = HttpZhuojianTransport(base_url="http://127.0.0.1:8787", gateway_token="fixture")
        with patch("agent.upstream.urllib.request.urlopen") as opened:
            for maximum in (True, 0, -1, 8 * 1024**2 + 1):
                with self.assertRaises(AgentError):
                    transport._post("/reporting/api/v1/research-data/analyze", {}, None, max_response_bytes=maximum)
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
