"""
Unit tests for aws/data_store/server.py.

Covers _make_parquet_bytes() and _s3_key().
No network calls — S3 is never contacted.
"""

import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../aws/data_store"))

import pyarrow.parquet as pq
from io import BytesIO
import pytest

from server import _make_parquet_bytes, _s3_key


# ---------------------------------------------------------------------------
# _make_parquet_bytes()
# ---------------------------------------------------------------------------

class TestMakeParquetBytes:
    """Tests for _make_parquet_bytes()."""

    def _make_rows(self, n=5):
        return [
            {
                "timestamp": 1700000000.0 + i,
                "site_id":   0,
                "rack_id":   0,
                "module_id": 0,
                "cell_id":   i % 6,
                "voltage":   round(3.7 + i * 0.001, 4),
                "temperature": 25.0,
                "soc":       80.0,
            }
            for i in range(n)
        ]

    def test_returns_bytes(self):
        data = _make_parquet_bytes(self._make_rows())
        assert isinstance(data, bytes)

    def test_returns_non_empty_bytes(self):
        data = _make_parquet_bytes(self._make_rows())
        assert len(data) > 0

    def test_empty_rows_returns_empty_bytes(self):
        data = _make_parquet_bytes([])
        assert data == b""

    def test_round_trip_row_count(self):
        rows = self._make_rows(20)
        data = _make_parquet_bytes(rows)
        table = pq.read_table(BytesIO(data))
        assert table.num_rows == 20

    def test_round_trip_columns_present(self):
        rows = self._make_rows(3)
        data = _make_parquet_bytes(rows)
        table = pq.read_table(BytesIO(data))
        names = table.schema.names
        assert "timestamp" in names
        assert "site_id"   in names
        assert "voltage"   in names

    def test_round_trip_preserves_voltage(self):
        rows = self._make_rows(5)
        data = _make_parquet_bytes(rows)
        table = pq.read_table(BytesIO(data))
        result = table.to_pydict()
        expected = [r["voltage"] for r in rows]
        assert sorted(result["voltage"]) == sorted(expected)

    def test_round_trip_preserves_timestamp(self):
        rows = self._make_rows(5)
        data = _make_parquet_bytes(rows)
        table = pq.read_table(BytesIO(data))
        result = table.to_pydict()
        expected = sorted(r["timestamp"] for r in rows)
        assert sorted(result["timestamp"]) == expected

    def test_single_row(self):
        rows = [{"timestamp": 1.0, "voltage": 3.7, "site_id": 0}]
        data = _make_parquet_bytes(rows)
        table = pq.read_table(BytesIO(data))
        assert table.num_rows == 1

    def test_default_compression_produces_valid_parquet(self):
        # Default compression is snappy
        rows = self._make_rows(10)
        data = _make_parquet_bytes(rows)
        # Should parse without error
        pq.read_table(BytesIO(data))

    def test_explicit_snappy_compression(self):
        rows = self._make_rows(5)
        data = _make_parquet_bytes(rows, compression="snappy")
        assert len(data) > 0

    def test_none_compression(self):
        rows = self._make_rows(5)
        data = _make_parquet_bytes(rows, compression="none")
        assert len(data) > 0

    def test_large_dataset(self):
        rows = self._make_rows(1000)
        data = _make_parquet_bytes(rows)
        table = pq.read_table(BytesIO(data))
        assert table.num_rows == 1000

    def test_all_measurement_columns(self):
        rows = [
            {
                "timestamp": 1700000000.0,
                "site_id": 0, "rack_id": 0, "module_id": 0, "cell_id": 0,
                "voltage": 3.7, "current": -1.0, "temperature": 27.0,
                "soc": 82.0, "internal_resistance": 0.012,
            }
        ]
        data = _make_parquet_bytes(rows)
        table = pq.read_table(BytesIO(data))
        for col in ("voltage", "current", "temperature", "soc", "internal_resistance"):
            assert col in table.schema.names


# ---------------------------------------------------------------------------
# _s3_key()
# ---------------------------------------------------------------------------

class TestS3Key:
    """Tests for _s3_key() — config-driven partition layout."""

    def _ts(self, year=2024, month=6, day=1, hour=12, minute=0, second=0):
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)

    def _cfg(self, prefix="", project_id=0, partitions=None):
        return {"s3": {
            "prefix": prefix,
            "project_id": project_id,
            "partitions": partitions or [
                "project={project_id}", "site={site_id}",
                "{year}", "{month}", "{day}",
            ],
        }}

    def test_contains_project_segment(self):
        key = _s3_key(self._cfg(), 0, self._ts())
        assert "project=0" in key

    def test_contains_site_segment(self):
        key = _s3_key(self._cfg(), 1, self._ts())
        assert "site=1" in key

    def test_contains_year_month_day(self):
        key = _s3_key(self._cfg(), 0, self._ts())
        assert "2024" in key
        assert "06" in key
        assert "01" in key

    def test_ends_with_parquet(self):
        key = _s3_key(self._cfg(), 0, self._ts())
        assert key.endswith(".parquet")

    def test_filename_timestamp_format(self):
        key = _s3_key(self._cfg(), 0, self._ts())
        assert "20240601T120000Z" in key

    def test_prefix_included(self):
        key = _s3_key(self._cfg(prefix="data/raw"), 3, self._ts())
        assert key.startswith("data/raw/")

    def test_empty_prefix_no_leading_slash(self):
        key = _s3_key(self._cfg(), 0, self._ts())
        assert not key.startswith("/")

    def test_slash_prefix_stripped(self):
        key = _s3_key(self._cfg(prefix="/myprefix"), 0, self._ts())
        assert not key.startswith("/")

    def test_segment_order_default(self):
        key = _s3_key(self._cfg(), 4, self._ts())
        parts = key.split("/")
        assert parts[0].startswith("project=")
        assert parts[1].startswith("site=")
        assert parts[2] == "2024"
        assert parts[3] == "06"
        assert parts[4] == "01"
        assert parts[5].endswith(".parquet")

    def test_different_sites_produce_different_keys(self):
        ts = self._ts()
        assert _s3_key(self._cfg(), 0, ts) != _s3_key(self._cfg(), 1, ts)

    def test_different_dates_produce_different_keys(self):
        ts1, ts2 = self._ts(day=10), self._ts(day=11)
        assert _s3_key(self._cfg(), 0, ts1) != _s3_key(self._cfg(), 0, ts2)

    def test_project_id_from_config(self):
        key = _s3_key(self._cfg(project_id=5), 0, self._ts())
        assert "project=5" in key

    def test_custom_partition_order(self):
        cfg = self._cfg(partitions=["{year}", "site={site_id}"])
        key = _s3_key(cfg, 2, self._ts())
        parts = key.split("/")
        assert parts[0] == "2024"
        assert parts[1].startswith("site=")
