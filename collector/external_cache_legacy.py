"""Read-only verification of the pre-import Home Depot collector artifact format."""
import csv
import json
from collections import Counter

from .store import canonical_target_url


LEGACY_FIELDS = (
    'channel', 'product_id', 'product_title', 'product_url', 'review_id',
    'source_site', 'source_product_id', 'is_syndicated', 'submission_time',
    'title', 'is_recommended', 'user_nickname', 'review_text', 'rating',
    'verified_purchaser', 'where_purchase', 'age',
)


def verify_legacy_home_depot(run_dir, item, source_product_id):
    """Compare every CSV value to its raw projection, then reconcile the report.

    This is a frozen historical format, not a fallback for malformed imports.
    Original files and raw review fields are never rewritten.
    """
    manifest = json.loads((run_dir / 'selection_manifest.json').read_text())
    report = json.loads((run_dir / 'validation_report.json').read_text())
    product = manifest.get('selected')
    if (item['channel'] != 'homedepot' or 'schema_version' in manifest
            or manifest.get('channel') != 'homedepot' or not isinstance(product, dict)
            or str(product.get('id')) != source_product_id
            or canonical_target_url(product.get('url')) != item['target_key']
            or report.get('product') != product or report.get('channel') != 'homedepot'
            or product not in manifest.get('candidates', [])):
        raise ValueError('legacy collector product identity mismatch')
    stored = (item.get('result') or {}).get('homedepot') or {}
    for key in ('status', 'source_total', 'unique_reviews', 'rating_histogram'):
        if stored.get(key) != report.get(key):
            raise ValueError('legacy collector report disagrees with active result')
    seen, ratings, ratings_only = set(), Counter(), 0
    with (run_dir / 'reviews.raw.jsonl').open(encoding='utf-8') as raw_file, \
            (run_dir / 'reviews.csv').open(encoding='utf-8-sig', newline='') as csv_file:
        reader = csv.DictReader(csv_file)
        if tuple(reader.fieldnames or ()) != LEGACY_FIELDS:
            raise ValueError('unsupported legacy collector CSV columns')
        for line in raw_file:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError('legacy collector raw row is not an object')
            rid = str(raw.get('Id') or '')
            rating = raw.get('Rating')
            if not rid or rid in seen or type(rating) is not int or rating not in range(1, 6):
                raise ValueError('legacy collector review identity or rating is invalid')
            seen.add(rid)
            contexts = raw.get('ContextDataValues') or {}
            def context(key):
                return (contexts.get(key) or {}).get('Value') or ''
            # Frozen projection written by the historical collector, including
            # booleans serialized by csv.DictWriter as True/False.
            expected = dict(zip(LEGACY_FIELDS, (
                'homedepot', product['id'], product['title'], product['url'], rid,
                product.get('source_site', 'homedepot'),
                raw.get('ProductId') or product.get('source_product_id', product['id']),
                raw.get('IsSyndicated', ''), raw.get('SubmissionTime') or '',
                raw.get('Title') or '', raw.get('IsRecommended', ''),
                raw.get('UserNickname') or '', raw.get('ReviewText') or '', rating,
                context('VerifiedPurchaser') or ('True' if 'verifiedPurchaser' in (raw.get('Badges') or {}) else ''),
                context('WherePurchase'), context('Age'),
            )))
            expected = {k: '' if v is None else str(v) for k, v in expected.items()}
            if next(reader, None) != expected:
                raise ValueError('legacy collector CSV disagrees with raw projection')
            ratings[str(rating)] += 1
            ratings_only += not expected['review_text'].strip()
        if next(reader, None) is not None:
            raise ValueError('legacy collector CSV has extra rows')
    count, total = len(seen), report.get('source_total')
    complete = item.get('active_result_status') == 'completed'
    status = 'COMPLETE' if complete else 'PARTIAL_SUCCESS'
    if (type(total) is not int or count <= 0 or count > total
            or (count == total) != complete or report.get('status') != status
            or report.get('unique_reviews') != count or report.get('rating_histogram') != dict(ratings)
            or report.get('ratings_only') != ratings_only):
        raise ValueError('legacy collector counts disagree with raw evidence')
    notes = report.get('notes') or {}
    if not complete and not notes.get('stop_reason'):
        raise ValueError('legacy partial collector result has no stop reason')
    if 'review_record_total' in notes and notes['review_record_total'] != total:
        raise ValueError('legacy collector source totals disagree')
    rating_total = report.get('source_rating_total')
    if rating_total is not None and (
        type(rating_total) is not int or rating_total < total
        or report.get('aggregate_ratings_without_review_record') != rating_total - total
    ):
        raise ValueError('legacy collector aggregate rating totals disagree')
    return {'manifest': manifest, 'validated': {'source_records_collected': count},
            'report': {**report, 'source_records_collected': count,
                       'source_missing_records': total - count, 'source_reconciled': complete,
                       'stop_reason': notes.get('stop_reason')}}
