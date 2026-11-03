"""Controlled maintenance CLI for one audited external-import partial.

Dry-run is the default.  Production execution belongs to VoxQuarry total
assembly and requires an explicit data directory, exact stable task/attempt,
both immutable import audit identifiers, and ``--execute``.
"""

from __future__ import annotations

import argparse
import json
import os

from .store import Store


def parser():
    value = argparse.ArgumentParser(
        description=(
            "Promote the latest validated partial external-import attempt only "
            "when the prior completed active validation is not reportable."
        )
    )
    value.add_argument("--data-dir", required=True)
    value.add_argument("--job-id", required=True)
    value.add_argument("--attempt-no", required=True, type=int)
    value.add_argument("--expected-import-id", required=True)
    value.add_argument("--expected-upload-sha256", required=True)
    value.add_argument(
        "--execute",
        action="store_true",
        help="perform the promotion; without this flag the command is read-only",
    )
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    os.environ["DATA_DIR"] = args.data_dir
    store = Store(initialize=False)
    kwargs = {
        "expected_import_id": args.expected_import_id,
        "expected_upload_sha256": args.expected_upload_sha256,
    }
    if args.execute:
        result = store.promote_external_import_partial(
            args.job_id,
            args.attempt_no,
            **kwargs,
        )
    else:
        result = store.inspect_external_import_partial_promotion(
            args.job_id,
            args.attempt_no,
            **kwargs,
        )
    result = dict(result)
    result["mode"] = "execute" if args.execute else "dry_run"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
