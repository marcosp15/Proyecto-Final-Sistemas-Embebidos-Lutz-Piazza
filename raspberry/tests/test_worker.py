"""Tests for FeederWorkerThread: IR async capture/cooldown/pruning, schedules, interval, manual button, sync."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

RASPBERRY_DIR = Path(__file__).resolve().parents[1]
if str(RASPBERRY_DIR) not in sys.path:
    sys.path.insert(0, str(RASPBERRY_DIR))

from i2c_master.controller import FeederI2CError, FeederMaster
from i2c_master.transport import MockArduinoTransport
from service import database as db
from service.camera import CameraManager
from service.client_sync import ThreadSafeFeederMaster
from service.worker import FeederWorkerThread, compute_next_schedule


class FakeClock:
    def __init__(self, start_mono: float = 100.0, start_dt: datetime | None = None) -> None:
        self.mono = start_mono
        self.dt = start_dt or datetime(2026, 11, 25, 8, 29, 0, tzinfo=timezone(timedelta(hours=-3)))

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.dt

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.dt += timedelta(seconds=seconds)


class FlakyTransport(MockArduinoTransport):
    """MockArduinoTransport that can simulate temporary I2C disconnection."""

    def __init__(self, clock) -> None:
        super().__init__(clock=clock)
        self.fail_next = False

    def send_and_receive(self, tx_bytes, rx_len=24, delay_s=0.0):
        if self.fail_next:
            raise OSError("Simulated I2C NACK")
        return super().send_and_receive(tx_bytes, rx_len=rx_len, delay_s=delay_s)


class TestFeederWorker(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.db_path = str(self.root / "feeder.db")
        self.media_dir = self.root / "media"
        db.init_db(self.db_path)

        self.clock = FakeClock()
        self.transport = FlakyTransport(clock=self.clock.monotonic)
        self.master = FeederMaster(transport=self.transport, delay=0.0)
        self.client = ThreadSafeFeederMaster(self.master)
        self.camera = CameraManager(self.media_dir, mock=True)
        self.worker = FeederWorkerThread(
            client=self.client,
            db_path=self.db_path,
            camera=self.camera,
            poll_interval=0.05,
            now_fn=self.clock.now,
            monotonic_fn=self.clock.monotonic,
        )

    def tearDown(self) -> None:
        self.worker.stop(wait_captures=True)
        self._tmpdir.cleanup()

    def test_arduino_config_sync_on_startup_and_reconnect(self) -> None:
        db.update_settings(self.db_path, {"steps_per_portion": 6144, "step_interval_us": 1800})
        self.worker.run_once()
        self.assertEqual(self.transport.manual_steps, 6144)
        self.assertEqual(self.transport.step_interval_us, 1800)
        connected, st = self.worker.get_last_arduino_status()
        self.assertTrue(connected)
        self.assertIsNotNone(st)

        # Simulate Arduino disconnect + reset to defaults
        self.transport.fail_next = True
        self.worker.run_once()
        connected, _ = self.worker.get_last_arduino_status()
        self.assertFalse(connected)
        self.assertGreaterEqual(self.worker.get_status()["errors"], 1)

        # Reconnect: worker must re-push settings via CMD_SET_CONFIG
        self.transport.manual_steps = 4096
        self.transport.step_interval_us = 2000
        self.transport.fail_next = False
        self.worker.run_once()
        connected, _ = self.worker.get_last_arduino_status()
        self.assertTrue(connected)
        self.assertEqual(self.transport.manual_steps, 6144)
        self.assertEqual(self.transport.step_interval_us, 1800)

    def test_ir_detection_async_capture_cooldown_and_pruning(self) -> None:
        db.update_settings(
            self.db_path,
            {"capture_mode": "photo", "ir_cooldown_s": 30, "max_media_files": 10},
        )
        # Seed 10 existing files on disk + DB so the next IR capture triggers pruning
        oldest_name = None
        for i in range(10):
            res = self.camera.capture_photo(trigger="manual")
            db.add_media_capture(
                self.db_path,
                trigger="manual",
                media_type="photo",
                filename=res.filename,
                size_bytes=res.size_bytes,
                timestamp=f"2026-11-25T07:00:{i:02d}-03:00",
            )
            if i == 0:
                oldest_name = res.filename

        # First IR detection triggers async capture and prunes oldest
        self.client.simulate_ir_detection()
        self.worker.run_once()
        self.worker.wait_pending_captures(timeout=5.0)
        self.assertEqual(db.count_media_captures(self.db_path), 10)
        self.assertFalse((self.media_dir / oldest_name).exists())
        newest = db.list_media_captures(self.db_path, limit=1)[0]
        self.assertEqual(newest["trigger"], "ir")
        self.assertEqual(newest["media_type"], "photo")

        # Second IR within 30s cooldown must NOT trigger a capture
        self.clock.advance(15.0)
        self.client.simulate_ir_detection()
        self.worker.run_once()
        self.worker.wait_pending_captures(timeout=5.0)
        self.assertEqual(db.list_media_captures(self.db_path, limit=1)[0]["filename"], newest["filename"])

        # After cooldown expires, switch to video mode and trigger again
        db.update_settings(self.db_path, {"capture_mode": "video", "video_duration_s": 4})
        self.clock.advance(20.0)
        self.client.simulate_ir_detection()
        self.worker.run_once()
        self.worker.wait_pending_captures(timeout=5.0)
        latest = db.list_media_captures(self.db_path, limit=1)[0]
        self.assertEqual(latest["trigger"], "ir")
        self.assertEqual(latest["media_type"], "video")
        self.assertEqual(latest["duration_s"], 4.0)
        self.assertEqual(db.count_media_captures(self.db_path), 10)

    def test_fixed_schedule_single_fire_per_day_and_next_day(self) -> None:
        sched = db.create_schedule(self.db_path, "08:30", 2, enabled=True)
        db.create_schedule(self.db_path, "08:30", 1, enabled=False)

        # 08:29 -> does not fire; next_schedule points to 08:30
        self.worker.run_once()
        self.assertEqual(db.count_dispense_events(self.db_path), 0)
        nxt = compute_next_schedule(self.db_path, self.clock.now())
        self.assertEqual(nxt["id"], sched["id"])

        # 08:30:00 -> fires once
        self.clock.advance(60.0)
        self.worker.run_once()
        events = db.list_dispense_events(self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "schedule")
        self.assertEqual(events[0]["portions"], 2.0)
        self.assertEqual(events[0]["steps"], 2 * 4096)
        self.assertEqual(events[0]["status"], "ok")

        # 08:30:20 -> still same day/minute, must NOT fire again
        self.clock.advance(20.0)
        self.worker.run_once()
        self.assertEqual(db.count_dispense_events(self.db_path), 1)

        # Next day 08:30:00 -> fires a second time
        self.clock.advance(24 * 3600 - 20.0)
        self.worker.run_once()
        self.assertEqual(db.count_dispense_events(self.db_path), 2)

    def test_interval_schedule_anchor_and_fire(self) -> None:
        db.update_settings(
            self.db_path,
            {
                "interval_enabled": True,
                "interval_minutes": 10,
                "interval_portions": 2,
                "last_interval_run_at": "",
            },
        )
        # First tick anchors last_interval_run_at without dispensing
        self.worker.run_once()
        self.assertEqual(db.count_dispense_events(self.db_path), 0)
        anchor = db.get_setting(self.db_path, "last_interval_run_at")
        self.assertTrue(anchor)

        # 9 minutes later -> still no dispense
        self.clock.advance(9 * 60)
        self.worker.run_once()
        self.assertEqual(db.count_dispense_events(self.db_path), 0)

        # 1 more minute (total 10 min) -> dispenses 2 portions
        self.clock.advance(60)
        self.worker.run_once()
        events = db.list_dispense_events(self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "interval")
        self.assertEqual(events[0]["portions"], 2.0)
        self.assertEqual(events[0]["steps"], 8192)
        self.assertNotEqual(db.get_setting(self.db_path, "last_interval_run_at"), anchor)

    def test_manual_button_event_logging(self) -> None:
        self.worker.run_once()  # initial config sync
        self.assertTrue(self.client.simulate_manual_button())
        self.worker.run_once()
        events = db.list_dispense_events(self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "manual_button")
        self.assertEqual(events[0]["portions"], 1.0)
        self.assertEqual(events[0]["steps"], 4096)
        self.assertEqual(events[0]["status"], "ok")

    def test_scheduled_dispense_exceeding_max_steps_logs_error_without_aborting(self) -> None:
        db.update_settings(
            self.db_path,
            {"steps_per_portion": 40960, "interval_enabled": True, "last_interval_run_at": ""},
        )
        db.create_schedule(self.db_path, "08:29", 10, enabled=True)  # 409600 > MAX_ABS_STEPS (204800)
        self.worker.run_once()

        events = db.list_dispense_events(self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "schedule")
        self.assertEqual(events[0]["status"], "error")
        # Interval scheduler still ran in the same tick and anchored last_interval_run_at
        self.assertTrue(db.get_setting(self.db_path, "last_interval_run_at"))

    def test_worker_thread_start_stop_lifecycle(self) -> None:
        self.worker.start()
        self.assertTrue(self.worker.is_alive())
        self.worker.stop(wait_captures=True)
        self.worker.join(timeout=2.0)
        self.assertFalse(self.worker.is_alive())
        self.assertFalse(self.worker.get_status()["running"])


if __name__ == "__main__":
    unittest.main()
