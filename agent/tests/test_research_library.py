import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent.research_library import read_reference, ROOT
from agent.contracts import AgentError

class ResearchLibraryTests(unittest.TestCase):
    def test_only_synthetic_reference_is_distributed(self):
        index=read_reference()
        self.assertEqual([x['reference_id'] for x in index['references']], ['synthetic-study'])
        self.assertIn('fictional', read_reference('synthetic-study','home')['text'])
        self.assertEqual(len(read_reference('synthetic-study')['original_sha256']),64)

    def test_new_edition_does_not_inherit_notes(self):
        index=json.loads((ROOT/'index.json').read_text())
        index['references'][0]['original_sha256']='f'*64
        with tempfile.TemporaryDirectory() as d, patch('agent.research_library.ROOT',Path(d)):
            (Path(d)/'index.json').write_text(json.dumps(index))
            value=read_reference('synthetic-study')
        self.assertFalse(value['edition_notes_match'])
        self.assertEqual(value['interpretation_notes'], ['Synthetic example only.'])

    def test_invalid_reference_cannot_select_filesystem_path(self):
        with self.assertRaises(AgentError):read_reference('../../private')
