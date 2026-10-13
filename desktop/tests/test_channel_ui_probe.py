"""Small regression checks for the live channel UI probe readiness gates."""

from __future__ import annotations

import unittest

from desktop.channel_ui_probe import _topics_ready_for_probe


class _TopicList:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def isEnabled(self) -> bool:
        return self.enabled


class _Page:
    _topics_loaded = True
    _topics_loading = False
    _thread = None

    def __init__(self):
        self.topic_list = _TopicList()
        self.busy = False

    def is_busy(self) -> bool:
        return self.busy


class ChannelUiProbeTests(unittest.TestCase):
    def test_topics_ready_waits_for_worker_and_enabled_controls(self):
        page = _Page()
        self.assertTrue(_topics_ready_for_probe(page))

        page.busy = True
        self.assertFalse(_topics_ready_for_probe(page))
        page.busy = False
        page._thread = object()
        self.assertFalse(_topics_ready_for_probe(page))
        page._thread = None
        page.topic_list.enabled = False
        self.assertFalse(_topics_ready_for_probe(page))


if __name__ == "__main__":
    unittest.main()
