"""
KPI (performance) tests for the battery telemetry pipeline.

All tests are pure in-memory benchmarks — no network calls, no running services.
Uses unittest.mock for MQTT clients.

Thresholds:
  - Generator: > 1000 msgs/sec for 144 cells × 100 iterations
  - rows_to_parquet: 10,000 rows in < 2 seconds
  - flux_to_sql parsing: 1000 calls in < 100ms
  - to_influx_csv formatting: 1000 rows (timeseries) in < 500ms
"""

import sys
import os
import random
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../source/generator"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../source/parquet_writer"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../subscriber/api"))

from unittest.mock import MagicMock

from generator import build_cells, publish_cell
from writer import rows_to_parquet
from flux_compat import flux_to_sql, to_influx_csv

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

GEN_CONFIG = {
    "sites": 2,
    "racks_per_site": 2,
    "modules_per_rack": 2,
    "cells_per_module": 3,        # 2×2×2×3 = 24 cells per config
    "sample_interval_seconds": 1,
    "topic_prefix": "batteries",
    "topic_mode": "per_cell",
    "cell_data": {
        "voltage":            {"min": 3.0,   "max": 4.2,  "unit": "V",       "nominal": 3.7},
        "current":            {"min": -50.0, "max": 50.0, "unit": "A",       "nominal": 0.0},
        "temperature":        {"min": 20.0,  "max": 45.0, "unit": "C",       "nominal": 25.0},
        "soc":                {"min": 0.0,   "max": 100.0,"unit": "percent", "nominal": 80.0},
        "internal_resistance":{"min": 0.001, "max": 0.05, "unit": "ohm",    "nominal": 0.01},
    },
    "broker":    {"host": "localhost", "port": 1883, "client_id": "kpi"},
    "websocket": {"host": "0.0.0.0",   "port": 8765},
}

# 144-cell config: 2 × 3 × 4 × 6 = 144
GEN_CONFIG_144 = dict(GEN_CONFIG, sites=2, racks_per_site=3, modules_per_rack=4, cells_per_module=6)

S3_CFG = {"s3": {"bucket": "test-bucket", "prefix": "", "access_key": "x", "secret_key": "y"}}

FLUX_TIMESERIES = """
from(bucket:"batteries")
  |> range(start: -1h)
  |> filter(fn: (r) => r.site_id == "0")
  |> aggregateWindow(every: 30s, fn: mean)
"""


# ---------------------------------------------------------------------------
# KPI: Generator throughput
# ---------------------------------------------------------------------------

class TestGeneratorKpi:
    """Generator throughput: 144 cells × 100 iterations > 1000 msgs/sec."""

    def test_publish_throughput(self):
        random.seed(42)
        cells = build_cells(GEN_CONFIG_144)
        assert len(cells) == 144, f"Expected 144 cells, got {len(cells)}"

        client = MagicMock()
        client.publish = MagicMock()

        iterations = 100
        total_msgs = 0

        t0 = time.monotonic()
        for _ in range(iterations):
            for cell in cells:
                total_msgs += publish_cell(client, cell, GEN_CONFIG_144)
        elapsed = time.monotonic() - t0

        msgs_per_sec = total_msgs / elapsed
        assert msgs_per_sec > 1000, (
            f"Generator throughput {msgs_per_sec:.0f} msgs/sec is below 1000 msgs/sec threshold "
            f"({total_msgs} messages in {elapsed:.3f}s)"
        )

    def test_publish_per_cell_item_throughput(self):
        """per_cell_item mode also meets 1000 msgs/sec threshold."""
        random.seed(43)
        cfg = dict(GEN_CONFIG_144, topic_mode="per_cell_item")
        cells = build_cells(cfg)

        client = MagicMock()
        client.publish = MagicMock()

        iterations = 20  # fewer iterations since per_cell_item publishes 5× more
        total_msgs = 0

        t0 = time.monotonic()
        for _ in range(iterations):
            for cell in cells:
                total_msgs += publish_cell(client, cell, cfg)
        elapsed = time.monotonic() - t0

        msgs_per_sec = total_msgs / elapsed
        assert msgs_per_sec > 1000, (
            f"per_cell_item throughput {msgs_per_sec:.0f} msgs/sec below 1000 threshold"
        )

    def test_build_cells_144_cells_fast(self):
        """build_cells for 144 cells completes in under 100ms."""
        random.seed(44)
        t0 = time.monotonic()
        cells = build_cells(GEN_CONFIG_144)
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert len(cells) == 144
        assert elapsed_ms < 100, f"build_cells took {elapsed_ms:.1f}ms, expected < 100ms"


# ---------------------------------------------------------------------------
# KPI: rows_to_parquet throughput
# ---------------------------------------------------------------------------

class TestParquetWriterKpi:
    """rows_to_parquet: 10,000 rows completes in < 2 seconds."""

    def _make_rows(self, n):
        return [
            {
                "timestamp":   1700000000.0 + i,
                "site_id":     i % 2,
                "rack_id":     i % 3,
                "module_id":   i % 4,
                "cell_id":     i % 6,
                "voltage":     {"value": round(3.5 + (i % 100) * 0.007, 4), "unit": "V"},
                "current":     {"value": round(-10.0 + (i % 200) * 0.1, 4), "unit": "A"},
                "temperature": {"value": round(20.0 + (i % 50) * 0.5, 4),   "unit": "C"},
                "soc":         {"value": round(50.0 + (i % 100) * 0.5, 4),  "unit": "percent"},
                "internal_resistance": {"value": 0.012, "unit": "ohm"},
            }
            for i in range(n)
        ]

    def test_10k_rows_under_2_seconds(self):
        rows = self._make_rows(10_000)

        t0 = time.monotonic()
        data = rows_to_parquet(rows, "snappy")
        elapsed = time.monotonic() - t0

        assert len(data) > 0, "rows_to_parquet returned empty bytes"
        assert elapsed < 2.0, (
            f"rows_to_parquet for 10,000 rows took {elapsed:.3f}s, expected < 2s"
        )

    def test_parquet_output_correct_row_count(self):
        """Sanity-check the round-trip row count alongside the timing test."""
        import pyarrow.parquet as pq
        from io import BytesIO

        rows = self._make_rows(1000)
        data = rows_to_parquet(rows, "snappy")
        table = pq.read_table(BytesIO(data))
        assert table.num_rows == 1000

    def test_1k_rows_per_iteration_repeated(self):
        """Repeated small batches (100 × 1k rows) all complete within 5s total."""
        rows = self._make_rows(1_000)
        t0 = time.monotonic()
        for _ in range(100):
            data = rows_to_parquet(rows, "snappy")
            assert len(data) > 0
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"100 × 1k batches took {elapsed:.3f}s, expected < 5s"


# ---------------------------------------------------------------------------
# KPI: flux_to_sql parsing
# ---------------------------------------------------------------------------

class TestFluxToSqlKpi:
    """flux_to_sql: 1000 parse calls complete in < 100ms."""

    def test_1000_calls_under_100ms(self):
        t0 = time.monotonic()
        for _ in range(1000):
            flux_to_sql(FLUX_TIMESERIES, S3_CFG)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert elapsed_ms < 100, (
            f"flux_to_sql 1000 calls took {elapsed_ms:.1f}ms, expected < 100ms"
        )

    def test_heatmap_1000_calls_under_100ms(self):
        flux_heatmap = """
        from(bucket:"batteries")
          |> range(start: -15m)
          |> filter(fn: (r) => r.site_id == "1")
          |> last()
        """
        t0 = time.monotonic()
        for _ in range(1000):
            flux_to_sql(flux_heatmap, S3_CFG)
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms < 100, (
            f"flux_to_sql heatmap 1000 calls took {elapsed_ms:.1f}ms, expected < 100ms"
        )


# ---------------------------------------------------------------------------
# KPI: to_influx_csv formatting
# ---------------------------------------------------------------------------

class TestInfluxCsvKpi:
    """to_influx_csv: 1000 timeseries rows formatted in < 500ms."""

    def _make_ts_data(self, n_rows):
        col_names = ["bucket_ts", "voltage", "temperature", "soc", "current", "internal_resistance"]
        base = 1700000000.0
        rows = [
            (
                base + i * 30,
                round(3.5 + (i % 100) * 0.007, 6),
                round(20.0 + (i % 50) * 0.5, 6),
                round(50.0 + (i % 100) * 0.5, 6),
                round(-5.0 + (i % 200) * 0.05, 6),
                0.012,
            )
            for i in range(n_rows)
        ]
        return col_names, rows

    def test_1000_rows_under_500ms(self):
        col_names, rows = self._make_ts_data(1000)
        fields = ["voltage", "temperature", "soc", "current", "internal_resistance"]

        t0 = time.monotonic()
        csv = to_influx_csv("timeseries", col_names, rows, fields)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert isinstance(csv, str)
        assert len(csv) > 0
        assert elapsed_ms < 500, (
            f"to_influx_csv for 1000 rows took {elapsed_ms:.1f}ms, expected < 500ms"
        )

    def test_1000_rows_output_has_data_lines(self):
        col_names, rows = self._make_ts_data(1000)
        csv = to_influx_csv("timeseries", col_names, rows, ["voltage"])
        data_lines = [l for l in csv.splitlines() if l.startswith(",,")]
        assert len(data_lines) == 1000

    def test_heatmap_1000_rows_under_500ms(self):
        col_names = ["timestamp", "module_id", "cell_id", "voltage"]
        rows = [(1700000000.0 + i, i % 4, i % 6, round(3.7 + i * 0.0001, 6)) for i in range(1000)]

        t0 = time.monotonic()
        csv = to_influx_csv("heatmap", col_names, rows, ["voltage"])
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert elapsed_ms < 500, (
            f"to_influx_csv heatmap 1000 rows took {elapsed_ms:.1f}ms, expected < 500ms"
        )

    def test_repeated_formatting_100x_under_2s(self):
        """Repeated calls with 100 rows each should be fast in aggregate."""
        col_names, rows = self._make_ts_data(100)
        fields = ["voltage"]

        t0 = time.monotonic()
        for _ in range(100):
            to_influx_csv("timeseries", col_names, rows, fields)
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, f"100 × 100-row format calls took {elapsed:.3f}s, expected < 2s"
