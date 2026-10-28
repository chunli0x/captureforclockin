"""Robot vision video: private ingest, approval, variants and delivery access.

The explicit migrator creates missing MediaAsset/MediaVariant/IngestJob/MediaAccess
ORM tables transactionally before adding the nullable pair references below.
Existing image rows are unchanged.
"""
COLUMNS = [
    ("judgment_pairs", "asset_a_id", "VARCHAR(32) REFERENCES media_assets(asset_id)"),
    ("judgment_pairs", "asset_b_id", "VARCHAR(32) REFERENCES media_assets(asset_id)"),
]
