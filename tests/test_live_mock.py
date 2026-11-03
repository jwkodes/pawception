import unittest

from pawception.live_mock import MockSession, LiveMonitor


def event(name="Chewing", threshold=10):
    return {"name": name, "description": f"A pet doing {name.lower()}.", "threshold_seconds": threshold}


def settings(**changes):
    return {"duration": 97, "source_name": "Test video", "window_seconds": 10,
            "overlap_seconds": 0, "mock_delay_seconds": 5,
            "model": "muse-spark-1.3-contributor", "events": [event()], **changes}


class MockSessionTests(unittest.TestCase):
    def test_gap_setting_is_validated_and_exposed_in_session(self):
        self.assertEqual(MockSession(settings()).snapshot(0)["merge_gap_seconds"], 1)
        self.assertEqual(MockSession(settings(merge_gap_seconds=0.5)).snapshot(0)["merge_gap_seconds"], 0.5)
        self.assertEqual(MockSession(settings(merge_gap_seconds=0)).snapshot(0)["merge_gap_seconds"], 0)
        for gap in (-1, 31, True, float("nan")):
            with self.subTest(gap=gap), self.assertRaises(ValueError):
                MockSession(settings(merge_gap_seconds=gap))

    def test_merged_episode_keeps_original_alert_id_and_viewed_status(self):
        session = MockSession(settings(window_seconds=30, merge_gap_seconds=1))
        session.windows = [{"id": "0", "start_seconds": 0, "end_seconds": 30, "input_duration_seconds": 30,
                            "status": "completed", "event_ids": ["event_1"]}]
        session.records = {"0": {"status": "completed", "output": {"event_1": {
            "detected": True, "intervals": [{"start": 6.5, "end": 29.5}], "evidence": "Test"}}}}
        original = session.snapshot(0)["alerts"][0]
        session.view_alert(original["id"], 0)
        session.windows.append({"id": "1", "start_seconds": 30.013, "end_seconds": 60.013,
                                "input_duration_seconds": 30, "status": "completed", "event_ids": ["event_1"]})
        session.records["1"] = {"status": "completed", "output": {"event_1": {
            "detected": True, "intervals": [{"start": 0, "end": 18}], "evidence": "Test"}}}
        session.event_replays.clear()
        state = session.snapshot(0)
        self.assertEqual(len(state["alerts"]), 1)
        self.assertEqual(state["alerts"][0]["id"], original["id"])
        self.assertEqual(state["viewed_alert_ids"], [original["id"]])
        self.assertEqual(len(state["detections"]), 1)
        self.assertEqual(state["events"][0]["longest_detected_seconds"], 41)

    def test_window_cannot_be_analyzed_before_it_is_captured(self):
        session = MockSession(settings())
        self.assertEqual(session.snapshot(9)["windows"], [])
        state = session.snapshot(10)
        self.assertEqual(state["active_window"]["start_seconds"], 0)
        self.assertEqual(state["active_window"]["end_seconds"], 10)
        self.assertEqual(state["alerts"], [])
        self.assertEqual(session.snapshot(14)["analyzed_through"], 0)
        self.assertEqual(session.snapshot(15)["analyzed_through"], 10)

    def test_queue_is_bounded_and_only_oldest_pending_jobs_are_dropped(self):
        session = MockSession(settings(window_seconds=3, mock_delay_seconds=30))
        state = session.snapshot(15)
        self.assertEqual(state["queue_size"], 2)
        self.assertEqual(state["active_window"]["id"], "0000")
        self.assertEqual([job["id"] for job in state["skipped_windows"]], ["0001", "0002"])
        self.assertEqual([job["id"] for job in state["windows"] if job["status"] == "pending"], ["0003", "0004"])
        self.assertTrue(all(job["input_duration_seconds"] == 3 for job in state["windows"]))

    def test_skipped_time_does_not_accumulate_toward_an_alert(self):
        session = MockSession(settings(window_seconds=3, mock_delay_seconds=12))
        state = session.snapshot(150)
        self.assertGreater(len(state["skipped_windows"]), 0)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["alerts"], [])

    def test_polling_cadence_does_not_change_window_drops_or_alerts(self):
        frequent = MockSession(settings(window_seconds=3, overlap_seconds=1, mock_delay_seconds=11))
        delayed = MockSession(settings(window_seconds=3, overlap_seconds=1, mock_delay_seconds=11))
        for at in range(151):
            frequent.snapshot(at)
        a, b = frequent.snapshot(150), delayed.snapshot(150)
        for key in ("windows", "alerts", "skipped_windows", "analyzed_through", "status"):
            self.assertEqual(a[key], b[key], key)

    def test_overlap_and_partial_tail_have_fixed_boundaries(self):
        session = MockSession(settings(duration=17, window_seconds=10, overlap_seconds=3, mock_delay_seconds=1))
        state = session.snapshot(30)
        self.assertEqual([(job["start_seconds"], job["end_seconds"]) for job in state["windows"]], [(0, 10), (7, 17)])
        self.assertEqual(state["status"], "completed")
        self.assertEqual([alert["at"] for alert in state["alerts"]], [15])

    def test_eof_drains_outstanding_work_and_final_partial_window(self):
        session = MockSession(settings(duration=23))
        state = session.snapshot(23)
        self.assertEqual(state["status"], "finishing")
        self.assertEqual(state["captured_seconds"], 23)
        self.assertEqual(state["windows"][-1]["input_duration_seconds"], 3)
        self.assertEqual(session.snapshot(40)["status"], "completed")

    def test_added_event_does_not_change_inflight_request_or_retroactively_alert(self):
        session = MockSession(settings(mock_delay_seconds=20))
        session.snapshot(12)
        added = session.add_event(event("Drinking", 2), now=12)
        self.assertEqual(added["active_from"], 12)
        state = session.snapshot(31)
        self.assertEqual(state["windows"][0]["event_ids"], ["event_1"])
        self.assertEqual(state["windows"][1]["event_ids"], ["event_1", "event_2"])
        self.assertEqual(state["events"][1]["analyzed_windows"], 0)
        state = session.snapshot(60)
        new_alerts = [alert for alert in state["alerts"] if alert["event_id"] == "event_2"]
        self.assertTrue(new_alerts)
        self.assertGreaterEqual(new_alerts[0]["evidence_span"]["start"], 12)
        self.assertEqual(session.records["0000"]["output"].keys(), {"event_1"})

    def test_adding_event_does_not_reset_an_existing_events_alert(self):
        session = MockSession(settings())
        original = session.snapshot(25)["alerts"][0]
        session.add_event(event("Playing"), now=26)
        updated = session.snapshot(40)["alerts"]
        self.assertIn(original, updated)

    def test_timer_is_based_on_event_duration_not_number_of_windows(self):
        for window in (3, 10, 60):
            with self.subTest(window=window):
                state = MockSession(settings(window_seconds=window, mock_delay_seconds=0.1)).snapshot(200)
                self.assertEqual([alert["at"] for alert in state["alerts"]], [15, 55])

    def test_stop_freezes_capture_and_cancels_work_without_making_more_alerts(self):
        session = MockSession(settings(mock_delay_seconds=25))
        session.stop(22)
        first = session.snapshot(22)
        later = session.snapshot(200)
        self.assertEqual(first, later)
        self.assertEqual(later["status"], "stopped")
        self.assertEqual(later["captured_seconds"], 22)
        self.assertEqual(later["queue_size"], 0)
        self.assertIsNone(later["active_window"])
        self.assertEqual(later["alerts"], [])
        with self.assertRaises(ValueError):
            session.add_event(event(), now=200)

    def test_snapshots_are_detached_from_future_state_changes(self):
        session = MockSession(settings())
        first = session.snapshot(10)
        session.snapshot(15)
        self.assertEqual(first["windows"][0]["status"], "running")

    def test_alerts_start_unread_and_viewing_one_does_not_mark_others(self):
        session = MockSession(settings())
        state = session.snapshot(80)
        self.assertEqual(len(state["alerts"]), 2)
        self.assertEqual(state["viewed_alert_ids"], [])
        alert_id = state["alerts"][0]["id"]
        session.view_alert(alert_id, 80)
        session.view_alert(alert_id, 81)
        self.assertEqual(session.snapshot(82)["viewed_alert_ids"], [alert_id])
        self.assertNotEqual(state["alerts"][1]["id"], alert_id)
        with self.assertRaises(ValueError):
            session.view_alert("another-session-alert", 82)

    def test_viewed_state_survives_polls_and_can_be_updated_after_stop(self):
        clock = [0]
        manager = LiveMonitor(clock=lambda: clock[0])
        state = manager.create(settings())
        clock[0] = 25
        state = manager.update(state["id"], "stop")
        alert_id = state["alerts"][0]["id"]
        manager.update(state["id"], "viewed", {"alert_id": alert_id})
        self.assertEqual(manager.current()["viewed_alert_ids"], [alert_id])
        self.assertEqual(manager.update(state["id"])["viewed_alert_ids"], [alert_id])

    def test_invalid_parameters_are_rejected_before_start(self):
        invalid = [{"window_seconds": 0}, {"overlap_seconds": 10},
                   {"overlap_seconds": 9.5}, {"mock_delay_seconds": float("nan")},
                   {"duration": True}, {"events": []}, {"model": "invalid"},
                   {"events": [event(threshold=0)]}, {"events": [None]}]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                MockSession(settings(**changes))

    def test_event_colours_remain_unique_and_preserve_requested_slots(self):
        session = MockSession(settings(events=[{**event(), "palette_index": 7}, {**event("Drinking"), "palette_index": 7}]))
        self.assertEqual([item["palette_index"] for item in session.events], [7, 0])
        for index in range(18):
            session.add_event(event(f"Event {index}"), now=0)
        self.assertEqual(len({item["palette_index"] for item in session.events}), 20)
        self.assertEqual(session.events[0]["palette_index"], 7)

    def test_manager_allows_only_one_active_session_and_rejects_unknown_ids(self):
        at = [0]
        manager = LiveMonitor(clock=lambda: at[0])
        state = manager.create(settings())
        self.assertEqual(manager.current()["id"], state["id"])
        with self.assertRaises(ValueError):
            manager.create(settings())
        with self.assertRaises(ValueError):
            manager.update("unknown")
        at[0] = 12
        state = manager.update(state["id"], "stop")
        self.assertEqual(state["status"], "stopped")
        self.assertNotEqual(manager.create(settings())["id"], state["id"])


if __name__ == "__main__":
    unittest.main()
