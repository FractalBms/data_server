"""
Unit tests for source/generator/generator.py.

Covers MeasurementState.step(), build_cells(), publish_cell(), and public_config().
No network calls — MQTT client is mocked with unittest.mock.
"""

import sys
import os
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../source/generator"))

from unittest.mock import MagicMock, patch
import pytest

import generator as gen
from generator import MeasurementState, build_cells, publish_cell, public_config

# ---------------------------------------------------------------------------
# Shared config fixture
# ---------------------------------------------------------------------------

GEN_CONFIG = {
    "sites": 2,
    "racks_per_site": 2,
    "modules_per_rack": 2,
    "cells_per_module": 3,
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
    "broker":    {"host": "localhost", "port": 1883, "client_id": "test"},
    "websocket": {"host": "0.0.0.0",   "port": 8765},
}


# ---------------------------------------------------------------------------
# MeasurementState.step()
# ---------------------------------------------------------------------------

class TestMeasurementStateStep:
    """Tests for MeasurementState.step()."""

    def _make_state(self, value=3.7, lo=3.0, hi=4.2):
        return MeasurementState(value=value, min=lo, max=hi)

    def test_step_returns_float(self):
        random.seed(42)
        state = self._make_state()
        result = state.step()
        assert isinstance(result, float)

    def test_step_stays_within_bounds(self):
        """After many steps the value must always remain in [min, max]."""
        random.seed(0)
        state = self._make_state(value=3.7, lo=3.0, hi=4.2)
        for _ in range(10_000):
            val = state.step()
            assert 3.0 <= val <= 4.2, f"Value {val} out of [3.0, 4.2]"

    def test_step_clamps_at_minimum(self):
        """State starting at min should never go below min."""
        random.seed(1)
        state = self._make_state(value=3.0, lo=3.0, hi=4.2)
        for _ in range(1000):
            val = state.step()
            assert val >= 3.0

    def test_step_clamps_at_maximum(self):
        """State starting at max should never go above max."""
        random.seed(2)
        state = self._make_state(value=4.2, lo=3.0, hi=4.2)
        for _ in range(1000):
            val = state.step()
            assert val <= 4.2

    def test_step_drifts_from_nominal(self):
        """Over enough steps the value should change from its starting point."""
        random.seed(3)
        state = self._make_state(value=3.7, lo=3.0, hi=4.2)
        values = {state.step() for _ in range(100)}
        assert len(values) > 1, "Value never changed — random walk is broken"

    def test_step_rounded_to_4_decimal_places(self):
        """Returned value should have at most 4 decimal places."""
        random.seed(4)
        state = self._make_state()
        for _ in range(200):
            val = state.step()
            assert round(val, 4) == val

    def test_step_updates_internal_state(self):
        """Calling step() must mutate self.value."""
        random.seed(5)
        state = self._make_state(value=3.7)
        original = state.value
        # At least one step out of many should change the stored value
        changed = False
        for _ in range(50):
            state.step()
            if state.value != original:
                changed = True
                break
        assert changed

    def test_step_narrow_range(self):
        """Works correctly when min == max (degenerate case)."""
        state = MeasurementState(value=1.0, min=1.0, max=1.0)
        for _ in range(20):
            val = state.step()
            assert val == 1.0


# ---------------------------------------------------------------------------
# build_cells()
# ---------------------------------------------------------------------------

class TestBuildCells:
    """Tests for build_cells()."""

    def test_count_matches_topology(self):
        random.seed(10)
        cells = build_cells(GEN_CONFIG)
        expected = (
            GEN_CONFIG["sites"]
            * GEN_CONFIG["racks_per_site"]
            * GEN_CONFIG["modules_per_rack"]
            * GEN_CONFIG["cells_per_module"]
        )
        assert len(cells) == expected

    def test_cell_ids_cover_full_range(self):
        random.seed(11)
        cells = build_cells(GEN_CONFIG)
        site_ids   = {c.site_id   for c in cells}
        rack_ids   = {c.rack_id   for c in cells}
        module_ids = {c.module_id for c in cells}
        cell_ids   = {c.cell_id   for c in cells}
        assert site_ids   == set(range(GEN_CONFIG["sites"]))
        assert rack_ids   == set(range(GEN_CONFIG["racks_per_site"]))
        assert module_ids == set(range(GEN_CONFIG["modules_per_rack"]))
        assert cell_ids   == set(range(GEN_CONFIG["cells_per_module"]))

    def test_each_cell_has_all_measurements(self):
        random.seed(12)
        cells = build_cells(GEN_CONFIG)
        expected_keys = set(GEN_CONFIG["cell_data"].keys())
        for cell in cells:
            assert set(cell.measurements.keys()) == expected_keys

    def test_measurement_initial_values_in_bounds(self):
        random.seed(13)
        cells = build_cells(GEN_CONFIG)
        for cell in cells:
            for name, state in cell.measurements.items():
                cfg = GEN_CONFIG["cell_data"][name]
                assert cfg["min"] <= state.value <= cfg["max"], (
                    f"Cell {cell.cell_path}: {name}={state.value} out of "
                    f"[{cfg['min']}, {cfg['max']}]"
                )

    def test_measurement_min_max_set_correctly(self):
        random.seed(14)
        cells = build_cells(GEN_CONFIG)
        for cell in cells:
            for name, state in cell.measurements.items():
                cfg = GEN_CONFIG["cell_data"][name]
                assert state.min == cfg["min"]
                assert state.max == cfg["max"]

    def test_single_cell_config(self):
        cfg = dict(GEN_CONFIG)
        cfg.update({"sites": 1, "racks_per_site": 1, "modules_per_rack": 1, "cells_per_module": 1})
        random.seed(15)
        cells = build_cells(cfg)
        assert len(cells) == 1

    def test_returns_list(self):
        random.seed(16)
        result = build_cells(GEN_CONFIG)
        assert isinstance(result, list)

    def test_cell_path_format(self):
        random.seed(17)
        cells = build_cells(GEN_CONFIG)
        for cell in cells:
            path = cell.cell_path
            assert "site=" in path
            assert "rack=" in path
            assert "module=" in path
            assert "cell=" in path


# ---------------------------------------------------------------------------
# publish_cell()
# ---------------------------------------------------------------------------

class TestPublishCell:
    """Tests for publish_cell() — uses a mock MQTT client."""

    def _make_cell(self, config=GEN_CONFIG):
        random.seed(20)
        cells = build_cells(config)
        return cells[0]

    def _mock_client(self):
        client = MagicMock()
        client.publish = MagicMock()
        return client

    def test_per_cell_returns_one(self):
        random.seed(21)
        cfg = dict(GEN_CONFIG, topic_mode="per_cell")
        client = self._mock_client()
        cell = self._make_cell(cfg)
        result = publish_cell(client, cell, cfg)
        assert result == 1

    def test_per_cell_publishes_once(self):
        random.seed(22)
        cfg = dict(GEN_CONFIG, topic_mode="per_cell")
        client = self._mock_client()
        cell = self._make_cell(cfg)
        publish_cell(client, cell, cfg)
        assert client.publish.call_count == 1

    def test_per_cell_topic_contains_cell_path(self):
        random.seed(23)
        cfg = dict(GEN_CONFIG, topic_mode="per_cell")
        client = self._mock_client()
        cell = self._make_cell(cfg)
        publish_cell(client, cell, cfg)
        call_args = client.publish.call_args
        topic = call_args[0][0]
        assert cell.cell_path in topic
        assert cfg["topic_prefix"] in topic

    def test_per_cell_item_returns_len_measurements(self):
        random.seed(24)
        cfg = dict(GEN_CONFIG, topic_mode="per_cell_item")
        client = self._mock_client()
        cell = self._make_cell(cfg)
        result = publish_cell(client, cell, cfg)
        assert result == len(GEN_CONFIG["cell_data"])

    def test_per_cell_item_publishes_once_per_measurement(self):
        random.seed(25)
        cfg = dict(GEN_CONFIG, topic_mode="per_cell_item")
        client = self._mock_client()
        cell = self._make_cell(cfg)
        publish_cell(client, cell, cfg)
        assert client.publish.call_count == len(GEN_CONFIG["cell_data"])

    def test_per_cell_item_topics_include_measurement_name(self):
        random.seed(26)
        cfg = dict(GEN_CONFIG, topic_mode="per_cell_item")
        client = self._mock_client()
        cell = self._make_cell(cfg)
        publish_cell(client, cell, cfg)
        published_topics = [call[0][0] for call in client.publish.call_args_list]
        for mname in GEN_CONFIG["cell_data"]:
            assert any(mname in t for t in published_topics), (
                f"Measurement {mname!r} not found in any published topic"
            )

    def test_unknown_topic_mode_raises(self):
        random.seed(27)
        cfg = dict(GEN_CONFIG, topic_mode="unknown_mode")
        client = self._mock_client()
        cell = self._make_cell(cfg)
        with pytest.raises(ValueError, match="Unknown topic_mode"):
            publish_cell(client, cell, cfg)

    def test_per_cell_payload_is_valid_json(self):
        import json
        random.seed(28)
        cfg = dict(GEN_CONFIG, topic_mode="per_cell")
        client = self._mock_client()
        cell = self._make_cell(cfg)
        publish_cell(client, cell, cfg)
        raw_payload = client.publish.call_args[0][1]
        parsed = json.loads(raw_payload)
        assert "timestamp" in parsed
        assert "site_id" in parsed

    def test_per_cell_item_payload_has_value_and_unit(self):
        import json
        random.seed(29)
        cfg = dict(GEN_CONFIG, topic_mode="per_cell_item")
        client = self._mock_client()
        cell = self._make_cell(cfg)
        publish_cell(client, cell, cfg)
        for call in client.publish.call_args_list:
            payload = json.loads(call[0][1])
            assert "value" in payload
            assert "unit" in payload
            assert "timestamp" in payload


# ---------------------------------------------------------------------------
# public_config()
# ---------------------------------------------------------------------------

class TestPublicConfig:
    """Tests for public_config() — reads from g_config global."""

    EXPECTED_KEYS = {
        "sites",
        "racks_per_site",
        "modules_per_rack",
        "cells_per_module",
        "sample_interval_seconds",
        "topic_mode",
    }

    def _set_g_config(self, cfg=GEN_CONFIG):
        gen.g_config = dict(cfg)

    def test_returns_dict(self):
        self._set_g_config()
        result = public_config()
        assert isinstance(result, dict)

    def test_contains_expected_keys(self):
        self._set_g_config()
        result = public_config()
        assert self.EXPECTED_KEYS == set(result.keys())

    def test_values_match_config(self):
        self._set_g_config()
        result = public_config()
        assert result["sites"]                   == GEN_CONFIG["sites"]
        assert result["racks_per_site"]          == GEN_CONFIG["racks_per_site"]
        assert result["modules_per_rack"]        == GEN_CONFIG["modules_per_rack"]
        assert result["cells_per_module"]        == GEN_CONFIG["cells_per_module"]
        assert result["sample_interval_seconds"] == GEN_CONFIG["sample_interval_seconds"]
        assert result["topic_mode"]              == GEN_CONFIG["topic_mode"]

    def test_does_not_expose_broker_credentials(self):
        self._set_g_config()
        result = public_config()
        assert "broker" not in result
        assert "password" not in result

    def test_topic_mode_reflected_correctly(self):
        cfg = dict(GEN_CONFIG, topic_mode="per_cell_item")
        self._set_g_config(cfg)
        result = public_config()
        assert result["topic_mode"] == "per_cell_item"
