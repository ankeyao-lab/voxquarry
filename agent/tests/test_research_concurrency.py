import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from agent.research_concurrency import ToolGate
from agent.research_limits import ResearchCapacity
from agent.tests.test_harness_research import HarnessFixture


class ConcurrentResearchTools(HarnessFixture, unittest.TestCase):
    def test_independent_reads_overlap_without_losing_sources_or_checkpoint_entries(self):
        run = self.accept()
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run['run_id'], question=run['request']['question'])
        cap = self.research.broker.issue(self.user, run, trace['trace_id'])[1]
        barrier = threading.Barrier(2)
        datasets = ['ds_' + 'a' * 64, 'ds_' + 'b' * 64]
        def data(_actor, operation, args):
            self.assertEqual(operation, 'aggregate')
            barrier.wait(timeout=3)  # Serialization would break this barrier.
            return {'dataset_id': args['dataset_id'], 'manifest_sha256': 'sha256:' + 'c' * 64, 'count': 10}
        self.data.call = Mock(side_effect=data)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda ds: self.research.tool(cap, 'aggregate_dataset', {'dataset_id': ds}), datasets))
        self.assertTrue(all(result.get('source_id') for result in results))
        checkpoint = self.state.get(self.user, run['run_id'])['checkpoint']
        self.assertEqual(set(checkpoint['raw_data_evidence']['analyses']), set(datasets))
        self.assertEqual(len(self.state.list_sources(self.user, run['run_id'])), 2)
        self.assertEqual(cap.tool_calls, 2)

    def test_capacity_configuration_changes_admission_without_changing_session_exclusivity(self):
        self.state.capacity = ResearchCapacity(researches=2, queued=0)
        first = self.accept()
        self.assert_code('research_busy', self.accept, request_id='same-session')
        self.sid = self.store.create_session(self.user, 'second')['session_id']
        second = self.accept()
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.sid = self.store.create_session(self.user, 'third')['session_id']
        self.assert_code('research_capacity_exceeded', self.accept)


class ToolGateTests(unittest.TestCase):
    def test_write_waits_until_all_readers_exit(self):
        gate = ToolGate(2)
        both_reading = threading.Barrier(3)
        release = threading.Event()
        writing = threading.Event()
        def read():
            with gate.enter(read_only=True):
                both_reading.wait(timeout=3)
                release.wait(3)
        def write():
            with gate.enter(read_only=False):
                writing.set()
        with ThreadPoolExecutor(max_workers=3) as pool:
            readers = [pool.submit(read), pool.submit(read)]
            both_reading.wait(timeout=3)
            writer = pool.submit(write)
            self.assertFalse(writing.wait(.05))
            release.set()
            for future in [*readers, writer]:
                future.result(timeout=3)
        self.assertTrue(writing.is_set())
