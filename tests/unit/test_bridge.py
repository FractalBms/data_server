"""
Unit tests for source/nats_bridge/bridge.py.

Covers mqtt_topic_to_nats_subject().  No network calls.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../source/nats_bridge"))

from bridge import mqtt_topic_to_nats_subject


class TestMqttTopicToNatsSubject:
    """Tests for mqtt_topic_to_nats_subject()."""

    def test_basic_conversion(self):
        assert mqtt_topic_to_nats_subject("batteries/site=0/rack=1") == "batteries.site=0.rack=1"

    def test_full_cell_path(self):
        topic = "batteries/site=0/rack=1/module=2/cell=3"
        expected = "batteries.site=0.rack=1.module=2.cell=3"
        assert mqtt_topic_to_nats_subject(topic) == expected

    def test_per_cell_item_topic(self):
        topic = "batteries/site=0/rack=1/module=2/cell=3/voltage"
        expected = "batteries.site=0.rack=1.module=2.cell=3.voltage"
        assert mqtt_topic_to_nats_subject(topic) == expected

    def test_no_slashes_unchanged(self):
        topic = "batteries"
        assert mqtt_topic_to_nats_subject(topic) == "batteries"

    def test_single_slash(self):
        assert mqtt_topic_to_nats_subject("a/b") == "a.b"

    def test_leading_slash(self):
        assert mqtt_topic_to_nats_subject("/topic") == ".topic"

    def test_trailing_slash(self):
        assert mqtt_topic_to_nats_subject("topic/") == "topic."

    def test_empty_string(self):
        assert mqtt_topic_to_nats_subject("") == ""

    def test_multiple_consecutive_slashes(self):
        assert mqtt_topic_to_nats_subject("a//b") == "a..b"

    def test_returns_string(self):
        result = mqtt_topic_to_nats_subject("a/b/c")
        assert isinstance(result, str)

    def test_no_slashes_in_result(self):
        topic = "batteries/site=0/rack=0/module=0/cell=0"
        result = mqtt_topic_to_nats_subject(topic)
        assert "/" not in result

    def test_all_slashes_replaced(self):
        topic = "a/b/c/d/e"
        result = mqtt_topic_to_nats_subject(topic)
        assert result.count(".") == 4
        assert result == "a.b.c.d.e"

    def test_non_battery_topic(self):
        assert mqtt_topic_to_nats_subject("sensors/temp/room1") == "sensors.temp.room1"

    def test_idempotent_on_nats_subject(self):
        # A subject with no slashes should not be changed
        subject = "batteries.site=0.rack=1"
        assert mqtt_topic_to_nats_subject(subject) == subject
