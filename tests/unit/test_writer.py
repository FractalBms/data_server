"""
Unit tests for source/parquet_writer/writer.py.

Covers _flatten(), rows_to_parquet(), and s3_key().
No network calls — S3 is never contacted.
"""

import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../source/parquet_writer"))

import pyarrow.parquet as pq
from io import BytesIO
import pytest

from writer import _flatten, rows_to_parquet, s3_key


# ---------------------------------------------------------------------------
# _flatten()
# ---------------------------------------------------------------------------

class TestFlatten:
    """Tests for _flatten()."""

    def test_unwraps_value_unit_dict(self):
        row = {"voltage": {"value": 3.7, "unit": "V"}}
        result = _flatten(row)
        assert result["voltage"] == 3.7

    def test_leaves_plain_values_unchanged(self):
        row = {"timestamp": 1700000000.0, "site_id": 0}
        result = _flatten(row)
        assert result["timestamp"] == 1700000000.0
        assert result["site_id"] == 0

    def test_mixed_row(self):
        row = {
            "timestamp": 1700000000.0,
            "site_id": 1,
            "voltage":     {"value": 3.85, "unit": "V"},
            "temperature": {"value": 27.3, "unit": "C"},
            "soc":         {"value": 75.0, "unit": "percent"},
        }
        result = _flatten(row)
        assert result["timestamp"] == 1700000000.0
        assert result["site_id"] == 1
        assert result["voltage"] == 3.85
        assert result["temperature"] == 27.3
        assert result["soc"] == 75.0

    def test_discards_unit_key(self):
        row = {"voltage": {"value": 3.7, "unit": "V"}}
        result = _flatten(row)
        assert "unit" not in result

    def test_empty_row(self):
        assert _flatten({}) == {}

    def test_dict_without_value_key_kept_as_is(self):
        """A dict that has no 'value' key should be kept verbatim."""
        row = {"meta": {"foo": "bar"}}
        result = _flatten(row)
        assert result["meta"] == {"foo": "bar"}

    def test_string_value_unchanged(self):
        row = {"label": "site-A"}
        result = _flatten(row)
        assert result["label"] == "site-A"

    def test_integer_value_unchanged(self):
        row = {"cell_id": 3}
        result = _flatten(row)
        assert result["cell_id"] == 3

    def test_none_value_unchanged(self):
        row = {"missing": None}
        result = _flatten(row)
        assert result["missing"] is None

    def test_all_measurements_flattened(self):
        row = {
            "voltage":            {"value": 3.7,   "unit": "V"},
            "current":            {"value": -1.5,  "unit": "A"},
            "temperature":        {"value": 30.0,  "unit": "C"},
            "soc":                {"value": 80.0,  "unit": "percent"},
            "internal_resistance":{"value": 0.012, "unit": "ohm"},
        }
        result = _flatten(row)
        assert result["voltage"]             == 3.7
        assert result["current"]             == -1.5
        assert result["temperature"]         == 30.0
        assert result["soc"]                 == 80.0
        assert result["internal_resistance"] == 0.012


# ---------------------------------------------------------------------------
# rows_to_parquet()
# ---------------------------------------------------------------------------

class TestRowsToParquet:
    """Tests for rows_to_parquet()."""

    def _make_rows(self, n=10):
        return [
            {
                "timestamp": 1700000000.0 + i,
                "site_id": 0,
                "voltage":     {"value": 3.7 + i * 0.001, "unit": "V"},
                "temperature": {"value": 25.0,             "unit": "C"},
            }
            for i in range(n)
        ]

    def test_returns_bytes(self):
        data = rows_to_parquet(self._make_rows(), "snappy")
        assert isinstance(data, bytes)

    def test_returns_non_empty_bytes(self):
        data = rows_to_parquet(self._make_rows(), "snappy")
        assert len(data) > 0

    def test_round_trip_row_count(self):
        rows = self._make_rows(50)
        data = rows_to_parquet(rows, "snappy")
        table = pq.read_table(BytesIO(data))
        assert table.num_rows == 50

    def test_round_trip_preserves_timestamp(self):
        rows = self._make_rows(5)
        data = rows_to_parquet(rows, "snappy")
        table = pq.read_table(BytesIO(data))
        result_dict = table.to_pydict()
        original_timestamps = [r["timestamp"] for r in rows]
        assert sorted(result_dict["timestamp"]) == sorted(original_timestamps)

    def test_round_trip_preserves_voltage(self):
        rows = self._make_rows(5)
        data = rows_to_parquet(rows, "snappy")
        table = pq.read_table(BytesIO(data))
        result_dict = table.to_pydict()
        original_voltages = [r["voltage"]["value"] for r in rows]
        assert sorted(result_dict["voltage"]) == sorted(original_voltages)

    def test_columns_include_expected_fields(self):
        rows = self._make_rows(3)
        data = rows_to_parquet(rows, "snappy")
        table = pq.read_table(BytesIO(data))
        assert "timestamp" in table.schema.names
        assert "voltage" in table.schema.names

    def test_compression_none_works(self):
        data = rows_to_parquet(self._make_rows(5), "none")
        assert len(data) > 0

    def test_compression_gzip_works(self):
        data = rows_to_parquet(self._make_rows(5), "gzip")
        assert len(data) > 0

    def test_single_row(self):
        rows = [{"timestamp": 1.0, "voltage": {"value": 3.7, "unit": "V"}}]
        data = rows_to_parquet(rows, "snappy")
        table = pq.read_table(BytesIO(data))
        assert table.num_rows == 1

    def test_plain_row_without_nested_dicts(self):
        rows = [{"timestamp": 1.0, "site_id": 0, "voltage": 3.7}]
        data = rows_to_parquet(rows, "snappy")
        table = pq.read_table(BytesIO(data))
        assert table.num_rows == 1
        assert table.to_pydict()["voltage"] == [3.7]


# ---------------------------------------------------------------------------
# s3_key()
# ---------------------------------------------------------------------------

class TestS3Key:
    """Tests for s3_key() — config-driven partition layout."""

    def _ts(self, year=2024, month=3, day=14, hour=15, minute=9, second=26):
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
        key = s3_key(self._cfg(), "0", self._ts())
        assert "project=0" in key

    def test_contains_site_segment(self):
        key = s3_key(self._cfg(), "1", self._ts())
        assert "site=1" in key

    def test_contains_year_month_day(self):
        ts = self._ts()
        key = s3_key(self._cfg(), "0", ts)
        assert "2024" in key
        assert "03" in key
        assert "14" in key

    def test_filename_ends_with_parquet(self):
        key = s3_key(self._cfg(), "0", self._ts())
        assert key.endswith(".parquet")

    def test_filename_contains_timestamp(self):
        ts = self._ts()
        key = s3_key(self._cfg(), "0", ts)
        assert "20240314T150926Z" in key

    def test_prefix_included_when_set(self):
        key = s3_key(self._cfg(prefix="my/prefix"), "1", self._ts())
        assert key.startswith("my/prefix/")

    def test_empty_prefix_no_leading_slash(self):
        key = s3_key(self._cfg(), "0", self._ts())
        assert not key.startswith("/")

    def test_prefix_strip_leading_slash(self):
        key = s3_key(self._cfg(prefix="/myprefix"), "2", self._ts())
        assert not key.startswith("/")
        assert "myprefix" in key

    def test_segment_order_default(self):
        key = s3_key(self._cfg(), "3", self._ts())
        parts = key.split("/")
        assert parts[0].startswith("project=")
        assert parts[1].startswith("site=")
        assert parts[2] == "2024"   # year
        assert parts[3] == "03"     # month
        assert parts[4] == "14"     # day
        assert parts[5].endswith(".parquet")

    def test_site_id_as_integer(self):
        key = s3_key(self._cfg(), 5, self._ts())
        assert "site=5" in key

    def test_project_id_from_config(self):
        key = s3_key(self._cfg(project_id=7), "0", self._ts())
        assert "project=7" in key

    def test_different_dates_produce_different_keys(self):
        ts1, ts2 = self._ts(day=1), self._ts(day=2)
        assert s3_key(self._cfg(), "0", ts1) != s3_key(self._cfg(), "0", ts2)

    def test_different_sites_produce_different_keys(self):
        ts = self._ts()
        assert s3_key(self._cfg(), "0", ts) != s3_key(self._cfg(), "1", ts)

    def test_custom_partition_order(self):
        cfg = self._cfg(partitions=["{year}", "{month}", "{day}", "site={site_id}"])
        key = s3_key(cfg, "2", self._ts())
        parts = key.split("/")
        assert parts[0] == "2024"
        assert parts[3].startswith("site=")
