from __future__ import annotations

import unittest

from reporting.json_stream import canonical_json_sha256
from reporting.source import canonical_json, sha256_text


class CanonicalJsonHashTests(unittest.TestCase):
    def test_matches_existing_bytes_for_nested_unicode_and_numbers(self) -> None:
        values = [
            None, True, False, 0, -0.0, 1e-20, 1e30,
            float("inf"), float("-inf"), float("nan"),
            {}, [], (1, "two"),
            {"z": ["中文", "\U0001f331", "\n\t\"\\", "\u0001"],
             "a": {"number": 3.141592653589793, "empty": []}},
            [{"index": i, "text": "中文\n\U0001f331" * 300}
             for i in range(120)],
        ]
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(
                    canonical_json_sha256(value),
                    sha256_text(canonical_json(value)),
                )

    def test_rejects_cycles_and_unsupported_values(self) -> None:
        cyclic = []
        cyclic.append(cyclic)
        for value, error in [(cyclic, ValueError), (object(), TypeError)]:
            with self.assertRaises(error):
                canonical_json_sha256(value)


if __name__ == "__main__":
    unittest.main()
