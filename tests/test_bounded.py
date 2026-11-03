import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from collector.bounded import collect_sample, save_limits, SCHEMA
from collector.runner import Runner
from reporting.source_completeness import source_completeness_audit, SourceCompletenessError


def record(i):
    return {'review_id':str(i),'review_text':'A synthetic review with enough written text.', 'rating':3,'raw':{'synthetic':True,'id':i},'review_word_count':8,'is_valid_text_review':True,'is_eligible':True}

class BoundedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=SimpleNamespace(data_dir=Path(self.temp.name))
        self.runner=Runner(self.store)
        self.product={'id':'demo','source_product_id':'demo','market_region':'North America'}
        save_limits(self.store,'demo',{'max_pages':3,'max_reviews':100,'timeout_seconds':10})

    def run_sample(self, responses, channel='walmart', **limits):
        if limits:save_limits(self.store,'demo',limits)
        values=iter(responses)
        async def fetch(page):
            value=next(values)
            if isinstance(value,Exception):raise value
            return value
        with patch('collector.bounded.asyncio.sleep',new=AsyncMock()):
            records,total,notes=asyncio.run(collect_sample(self.runner,self.product,'demo',channel,fetch,lambda x:x))
        target=Path(self.temp.name)/'final';target.mkdir(exist_ok=True)
        report=self.runner.persist_and_validate(target,channel,self.product,records,total,notes)
        return records,total,notes,report

    def test_review_cap_preserves_population(self):
        rows,total,notes,report=self.run_sample([([record(i) for i in range(10)],50)],max_reviews=3,max_pages=3)
        self.assertEqual((len(rows),total),(3,50));self.assertEqual(notes['stop_reason'],'review_limit')
        self.assertTrue(notes['requested_sample_complete']);self.assertFalse(report['source_reconciled'])
        self.assertEqual(report['status'],'PARTIAL_SUCCESS');self.assertEqual(report['source_missing_records'],47)
        source_completeness_audit({'id':'demo','status':'partial_success','channel':'walmart'},report,csv_eligible_rows=3,raw_source_rows=3)

    def test_page_cap_and_checkpoint(self):
        rows,total,notes,report=self.run_sample([([record(1)],9)],max_pages=1,max_reviews=100)
        self.assertEqual(notes['stop_reason'],'page_limit')
        saved=Path(self.temp.name)/'runs/demo/walmart/reviews.csv'
        self.assertTrue(saved.is_file());self.assertIn('synthetic',saved.read_text())

    def test_exact_complete_is_reconciled(self):
        rows,total,notes,report=self.run_sample([([record(1),record(2)],2)],channel='homedepot')
        self.assertEqual(report['status'],'COMPLETE');self.assertTrue(report['source_reconciled'])
        self.assertEqual(notes['stop_reason'],'source_complete')

    def test_later_failure_retains_partial(self):
        rows,total,notes,report=self.run_sample([([record(1)],9),RuntimeError('sensitive challenge body never persisted')])
        self.assertEqual(len(rows),1);self.assertEqual(report['status'],'PARTIAL_SUCCESS')
        self.assertEqual(notes['stop_reason'],'source_error');self.assertNotIn('sensitive',json.dumps(notes))

    def test_timeout_keeps_checkpoint(self):
        rows,total,notes,report=self.run_sample([([record(1)],9),asyncio.TimeoutError()])
        self.assertEqual(notes['stop_reason'],'time_limit');self.assertEqual(report['source_missing_records'],8)

    def test_drifting_total_cannot_be_claimed_complete(self):
        rows,total,notes,report=self.run_sample([([record(1)],9),([record(2)],10)])
        self.assertEqual(len(rows),1);self.assertEqual(total,10);self.assertEqual(notes['stop_reason'],'source_drift')
        self.assertFalse(notes['requested_sample_complete']);self.assertFalse(report['source_reconciled'])

    def test_duplicate_page_is_not_silently_counted(self):
        rows,total,notes,report=self.run_sample([([record(1)],4),([record(1)],4)])
        self.assertEqual(len(rows),1);self.assertEqual(notes['stop_reason'],'duplicate_page')
        self.assertEqual(report['source_missing_records'],3)

    def test_count_equality_does_not_override_integrity_error(self):
        rows,total,notes,report=self.run_sample([([record(1),record(2),record(2)],2)])
        self.assertEqual(len(rows),2);self.assertFalse(report['source_reconciled'])
        self.assertEqual(report['status'],'INCOMPLETE_SOURCE_LIMIT')
        with self.assertRaises(SourceCompletenessError):
            source_completeness_audit({'id':'demo','status':'completed'},report)

    def test_first_page_failure_never_becomes_zero_success(self):
        with self.assertRaisesRegex(RuntimeError,'public source unavailable'):
            self.run_sample([ValueError('challenge')])

    def test_empty_page_with_nonzero_population_not_zero_complete(self):
        rows,total,notes,report=self.run_sample([([],5)])
        self.assertEqual(report['status'],'INCOMPLETE_SOURCE_LIMIT');self.assertEqual(total,5)

    def test_confirmed_empty_source_is_distinct(self):
        rows,total,notes,report=self.run_sample([([],0)])
        self.assertEqual(report['status'],'COMPLETE');self.assertTrue(notes['empty_source_confirmed'])

    def test_invalid_population_cannot_be_coerced(self):
        for value in [True,-1,'10',None]:
            with self.subTest(value=value),self.assertRaises(RuntimeError):self.run_sample([([],value)])

if __name__=='__main__':unittest.main()
