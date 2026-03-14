"""
Unit tests for subscriber/api/flux_compat.py.

Covers _parse_range(), _parse_filters(), _parse_agg_seconds(),
_query_type(), flux_to_sql(), and to_influx_csv().
No network calls, no DuckDB execution.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../subscriber/api"))

import pytest

from flux_compat import (
    _parse_range,
    _parse_filters,
    _parse_agg_seconds,
    _query_type,
    flux_to_sql,
    to_influx_csv,
)

# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------

S3_CFG = {
    "s3": {
        "bucket": "test-bucket",
        "prefix": "",
        "access_key": "x",
        "secret_key": "y",
    }
}

# ---------------------------------------------------------------------------
# Representative Flux query strings
# ---------------------------------------------------------------------------

FLUX_TIMESERIES = """
from(bucket:"batteries")
  |> range(start: -1h)
  |> filter(fn: (r) => r.site_id == "0")
  |> aggregateWindow(every: 30s, fn: mean)
"""

FLUX_HEATMAP = """
from(bucket:"batteries")
  |> range(start: -15m)
  |> filter(fn: (r) => r.site_id == "1")
  |> filter(fn: (r) => r._field == "voltage")
  |> last()
"""

FLUX_WRITERATE = """
from(bucket:"batteries")
  |> range(start: -5m)
  |> filter(fn: (r) => r._field == "voltage")
  |> count()
  |> sum()
"""


# ---------------------------------------------------------------------------
# _parse_range()
# ---------------------------------------------------------------------------

class TestParseRange:
    """Tests for _parse_range()."""

    def test_1h_offset(self):
        before = time.time()
        start, end = _parse_range('range(start: -1h)')
        after = time.time()
        assert abs(end - after) < 2
        assert abs((end - start) - 3600) < 2

    def test_30m_offset(self):
        start, end = _parse_range('range(start: -30m)')
        assert abs((end - start) - 1800) < 2

    def test_5s_offset(self):
        start, end = _parse_range('range(start: -5s)')
        assert abs((end - start) - 5) < 2

    def test_1d_offset(self):
        start, end = _parse_range('range(start: -1d)')
        assert abs((end - start) - 86400) < 2

    def test_default_when_no_range(self):
        start, end = _parse_range('no range here')
        assert abs((end - start) - 3600) < 2

    def test_returns_tuple_of_two_floats(self):
        result = _parse_range('range(start: -10m)')
        assert len(result) == 2
        assert all(isinstance(v, float) for v in result)

    def test_start_before_end(self):
        start, end = _parse_range('range(start: -2h)')
        assert start < end

    def test_15m_offset(self):
        start, end = _parse_range(FLUX_HEATMAP)
        assert abs((end - start) - 900) < 2


# ---------------------------------------------------------------------------
# _parse_filters()
# ---------------------------------------------------------------------------

class TestParseFilters:
    """Tests for _parse_filters()."""

    def test_single_filter(self):
        filters = _parse_filters('filter(fn: (r) => r.site_id == "0")')
        assert filters == {"site_id": "0"}

    def test_multiple_filters(self):
        flux = 'r.site_id == "1"  r.rack_id == "2"'
        filters = _parse_filters(flux)
        assert filters["site_id"] == "1"
        assert filters["rack_id"] == "2"

    def test_no_filters(self):
        assert _parse_filters("range(start: -1h)") == {}

    def test_field_filter(self):
        filters = _parse_filters('r._field == "voltage"')
        assert filters["_field"] == "voltage"

    def test_returns_dict(self):
        result = _parse_filters('r.site_id == "0"')
        assert isinstance(result, dict)

    def test_all_id_fields(self):
        flux = 'r.site_id == "0" r.rack_id == "1" r.module_id == "2" r.cell_id == "3"'
        filters = _parse_filters(flux)
        assert filters == {"site_id": "0", "rack_id": "1", "module_id": "2", "cell_id": "3"}

    def test_timeseries_flux_filters(self):
        filters = _parse_filters(FLUX_TIMESERIES)
        assert "site_id" in filters
        assert filters["site_id"] == "0"


# ---------------------------------------------------------------------------
# _parse_agg_seconds()
# ---------------------------------------------------------------------------

class TestParseAggSeconds:
    """Tests for _parse_agg_seconds()."""

    def test_10s(self):
        assert _parse_agg_seconds("every: 10s") == 10

    def test_30s(self):
        assert _parse_agg_seconds("every: 30s") == 30

    def test_5m(self):
        assert _parse_agg_seconds("every: 5m") == 300

    def test_1h(self):
        assert _parse_agg_seconds("every: 1h") == 3600

    def test_default_when_absent(self):
        assert _parse_agg_seconds("aggregateWindow(fn: mean)") == 10

    def test_returns_int(self):
        result = _parse_agg_seconds("every: 20s")
        assert isinstance(result, int)

    def test_from_timeseries_flux(self):
        # FLUX_TIMESERIES uses every: 30s
        assert _parse_agg_seconds(FLUX_TIMESERIES) == 30

    def test_60s(self):
        assert _parse_agg_seconds("every: 60s") == 60


# ---------------------------------------------------------------------------
# _query_type()
# ---------------------------------------------------------------------------

class TestQueryType:
    """Tests for _query_type()."""

    def test_timeseries(self):
        assert _query_type(FLUX_TIMESERIES) == "timeseries"

    def test_heatmap(self):
        assert _query_type(FLUX_HEATMAP) == "heatmap"

    def test_writerate(self):
        assert _query_type(FLUX_WRITERATE) == "writerate"

    def test_aggregate_window_keyword(self):
        assert _query_type("aggregateWindow(every:10s, fn:mean)") == "timeseries"

    def test_last_keyword(self):
        assert _query_type("|> last()") == "heatmap"

    def test_plain_query_is_writerate(self):
        assert _query_type("from(bucket:x) |> range(start:-1h)") == "writerate"


# ---------------------------------------------------------------------------
# flux_to_sql()
# ---------------------------------------------------------------------------

class TestFluxToSql:
    """Tests for flux_to_sql()."""

    def test_returns_5_tuple(self):
        result = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        assert len(result) == 5

    def test_timeseries_qtype(self):
        qtype, sql, _, _, fields = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        assert qtype == "timeseries"

    def test_heatmap_qtype(self):
        qtype, *_ = flux_to_sql(FLUX_HEATMAP, S3_CFG)
        assert qtype == "heatmap"

    def test_writerate_qtype(self):
        qtype, *_ = flux_to_sql(FLUX_WRITERATE, S3_CFG)
        assert qtype == "writerate"

    def test_timeseries_sql_has_where_timestamp(self):
        _, sql, _, _, _ = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        assert "timestamp" in sql.lower()
        assert "where" in sql.lower()

    def test_timeseries_sql_has_group_by(self):
        _, sql, _, _, _ = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        assert "group by" in sql.lower()

    def test_timeseries_fields_list(self):
        _, _, _, _, fields = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        assert isinstance(fields, list)
        assert len(fields) > 0
        assert "voltage" in fields

    def test_heatmap_fields_is_voltage_only(self):
        _, _, _, _, fields = flux_to_sql(FLUX_HEATMAP, S3_CFG)
        assert fields == ["voltage"]

    def test_writerate_fields_empty(self):
        _, _, _, _, fields = flux_to_sql(FLUX_WRITERATE, S3_CFG)
        assert fields == []

    def test_bucket_name_in_sql(self):
        _, sql, _, _, _ = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        assert "test-bucket" in sql

    def test_site_id_filter_in_sql(self):
        _, sql, _, _, _ = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        # site_id == "0" should produce a WHERE clause referencing site_id
        assert "site_id" in sql

    def test_from_ts_before_to_ts(self):
        _, _, from_ts, to_ts, _ = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        assert from_ts < to_ts

    def test_sql_is_string(self):
        _, sql, _, _, _ = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        assert isinstance(sql, str)

    def test_timeseries_sql_has_order_by(self):
        _, sql, _, _, _ = flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        assert "order by" in sql.lower()

    def test_prefix_in_path_when_set(self):
        cfg = {"s3": {"bucket": "b", "prefix": "mydata", "access_key": "x", "secret_key": "y"}}
        _, sql, _, _, _ = flux_to_sql(FLUX_TIMESERIES, cfg)
        assert "mydata" in sql


# ---------------------------------------------------------------------------
# to_influx_csv()
# ---------------------------------------------------------------------------

class TestToInfluxCsv:
    """Tests for to_influx_csv()."""

    def _ts_rows(self, n=3):
        """Synthetic timeseries result rows: (bucket_ts, voltage, temperature, ...)."""
        col_names = ["bucket_ts", "voltage", "temperature", "soc", "current", "internal_resistance"]
        base = 1700000000.0
        rows = [(base + i * 30, 3.7 + i * 0.01, 25.0, 80.0, 0.0, 0.01) for i in range(n)]
        return col_names, rows

    # --- timeseries ---

    def test_timeseries_starts_with_datatype(self):
        col_names, rows = self._ts_rows()
        csv = to_influx_csv("timeseries", col_names, rows, ["voltage"])
        assert csv.startswith("#datatype")

    def test_timeseries_has_result_header(self):
        col_names, rows = self._ts_rows()
        csv = to_influx_csv("timeseries", col_names, rows, ["voltage"])
        assert ",result,table,_time,_value,_field" in csv

    def test_timeseries_data_rows_count(self):
        col_names, rows = self._ts_rows(5)
        csv = to_influx_csv("timeseries", col_names, rows, ["voltage"])
        # Data lines start with ",,"
        data_lines = [l for l in csv.splitlines() if l.startswith(",,")]
        assert len(data_lines) == 5

    def test_timeseries_multiple_fields(self):
        col_names, rows = self._ts_rows(2)
        csv = to_influx_csv("timeseries", col_names, rows, ["voltage", "temperature"])
        data_lines = [l for l in csv.splitlines() if l.startswith(",,")]
        # 2 rows × 2 fields
        assert len(data_lines) == 4

    def test_timeseries_field_in_last_column(self):
        col_names, rows = self._ts_rows(1)
        csv = to_influx_csv("timeseries", col_names, rows, ["voltage"])
        data_lines = [l for l in csv.splitlines() if l.startswith(",,")]
        assert data_lines[0].endswith(",voltage")

    # --- heatmap ---

    def test_heatmap_starts_with_datatype(self):
        col_names = ["timestamp", "module_id", "cell_id", "voltage"]
        rows = [(1700000000.0, 0, 1, 3.85)]
        csv = to_influx_csv("heatmap", col_names, rows, ["voltage"])
        assert csv.startswith("#datatype")

    def test_heatmap_header_includes_module_cell(self):
        col_names = ["timestamp", "module_id", "cell_id", "voltage"]
        rows = [(1700000000.0, 0, 1, 3.85)]
        csv = to_influx_csv("heatmap", col_names, rows, ["voltage"])
        assert ",result,table,_time,_value,module_id,cell_id" in csv

    def test_heatmap_data_rows_count(self):
        col_names = ["timestamp", "module_id", "cell_id", "voltage"]
        rows = [(1700000000.0 + i, i % 4, i % 6, 3.7) for i in range(6)]
        csv = to_influx_csv("heatmap", col_names, rows, ["voltage"])
        data_lines = [l for l in csv.splitlines() if l.startswith(",,")]
        assert len(data_lines) == 6

    # --- writerate ---

    def test_writerate_starts_with_datatype(self):
        csv = to_influx_csv("writerate", ["cnt"], [(42,)], [])
        assert csv.startswith("#datatype")

    def test_writerate_has_single_value_line(self):
        csv = to_influx_csv("writerate", ["cnt"], [(99,)], [])
        data_lines = [l for l in csv.splitlines() if l.startswith(",,")]
        assert len(data_lines) == 1
        assert "99" in data_lines[0]

    def test_writerate_empty_rows_returns_zero(self):
        csv = to_influx_csv("writerate", ["cnt"], [], [])
        data_lines = [l for l in csv.splitlines() if l.startswith(",,")]
        assert "0" in data_lines[0]

    # --- general ---

    def test_returns_string(self):
        col_names, rows = self._ts_rows(1)
        result = to_influx_csv("timeseries", col_names, rows, ["voltage"])
        assert isinstance(result, str)

    def test_sections_separated_by_blank_line(self):
        col_names, rows = self._ts_rows(1)
        csv = to_influx_csv("timeseries", col_names, rows, ["voltage", "temperature"])
        # Two fields → two sections → at least one blank line separator
        blank_lines = [l for l in csv.splitlines() if l == ""]
        assert len(blank_lines) >= 1
