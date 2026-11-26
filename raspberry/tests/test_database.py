"""Tests for Contract 3 SQLite persistence (WAL, settings, schedules, events, media)."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

RASPBERRY_DIR = Path(__file__).resolve().parents[1]
if str(RASPBERRY_DIR) not in sys.path:
    sys.path.insert(0, str(RASPBERRY_DIR))

from service import database as db


class TestDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "feeder.db")
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_wal_mode_and_idempotent_init(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                ).fetchall()
            }
            self.assertTrue({"settings", "schedules", "dispense_events", "media_captures"} <= tables)
        finally:
            conn.close()

        # Modify a setting and re-run init_db: existing value must be preserved
        db.set_setting(self.db_path, "steps_per_portion", 2048)
        db.init_db(self.db_path)
        self.assertEqual(db.get_setting(self.db_path, "steps_per_portion"), 2048)

    def test_settings_defaults_updates_and_validation(self) -> None:
        cfg = db.get_settings(self.db_path)
        self.assertEqual(cfg, db.DEFAULT_SETTINGS)
        self.assertEqual(cfg["step_interval_us"], 1000)
        self.assertEqual(cfg["schedule_mode"], "schedules")
        self.assertIs(cfg["interval_enabled"], False)

        updated = db.update_settings(
            self.db_path,
            {
                "steps_per_portion": 8192,
                "step_interval_us": 1500,
                "capture_mode": "video",
                "video_duration_s": 10,
                "ir_cooldown_s": 45,
                "max_media_files": 50,
                "interval_enabled": True,
                "interval_minutes": 120,
                "interval_portions": 3,
                "last_interval_run_at": "2026-11-25T10:00:00-03:00",
            },
        )
        self.assertEqual(updated["steps_per_portion"], 8192)
        self.assertEqual(updated["capture_mode"], "video")
        self.assertIs(updated["interval_enabled"], True)
        self.assertEqual(updated["schedule_mode"], "interval")
        self.assertEqual(updated["last_interval_run_at"], "2026-11-25T10:00:00-03:00")

        # Out-of-range and invalid types rejected atomically (no partial writes)
        invalid_cases = [
            {"unknown_key": 1},
            {"steps_per_portion": 100},
            {"steps_per_portion": 50000},
            {"steps_per_portion": True},
            {"step_interval_us": 500},
            {"step_interval_us": 20000},
            {"capture_mode": "gif"},
            {"schedule_mode": "always"},
            {"video_duration_s": 1},
            {"video_duration_s": 16},
            {"ir_cooldown_s": 4},
            {"ir_cooldown_s": 601},
            {"max_media_files": 9},
            {"max_media_files": 501},
            {"interval_enabled": "maybe"},
            {"interval_minutes": 0},
            {"interval_minutes": 1441},
            {"interval_portions": 0},
            {"interval_portions": 11},
            {"last_interval_run_at": "not-a-timestamp"},
        ]
        for bad in invalid_cases:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    db.update_settings(self.db_path, {"steps_per_portion": 4096, **bad})

        # Value must remain 8192 (atomic rollback)
        self.assertEqual(db.get_setting(self.db_path, "steps_per_portion"), 8192)

    def test_schedule_mode_and_interval_enabled_sync(self) -> None:
        # schedule_mode -> interval_enabled
        cfg = db.update_settings(self.db_path, {"schedule_mode": "interval"})
        self.assertEqual(cfg["schedule_mode"], "interval")
        self.assertIs(cfg["interval_enabled"], True)

        cfg = db.update_settings(self.db_path, {"schedule_mode": "manual"})
        self.assertEqual(cfg["schedule_mode"], "manual")
        self.assertIs(cfg["interval_enabled"], False)

        # Disabling interval_enabled while in manual preserves manual mode
        cfg = db.update_settings(self.db_path, {"interval_enabled": False})
        self.assertEqual(cfg["schedule_mode"], "manual")
        self.assertIs(cfg["interval_enabled"], False)

        # Legacy interval_enabled=True activates interval mode
        cfg = db.update_settings(self.db_path, {"interval_enabled": True})
        self.assertEqual(cfg["schedule_mode"], "interval")
        self.assertIs(cfg["interval_enabled"], True)

        # Legacy interval_enabled=False from interval switches back to schedules
        cfg = db.update_settings(self.db_path, {"interval_enabled": False})
        self.assertEqual(cfg["schedule_mode"], "schedules")
        self.assertIs(cfg["interval_enabled"], False)

        # Explicit schedule_mode takes precedence over conflicting interval_enabled
        cfg = db.update_settings(
            self.db_path, {"schedule_mode": "schedules", "interval_enabled": True}
        )
        self.assertEqual(cfg["schedule_mode"], "schedules")
        self.assertIs(cfg["interval_enabled"], False)

        # Legacy DB upgrade: interval_enabled='1' without schedule_mode row
        legacy_db = str(Path(self._tmpdir.name) / "legacy.db")
        conn = sqlite3.connect(legacy_db)
        try:
            conn.executescript(db.SCHEMA_SQL)
            conn.execute("INSERT INTO settings (key, value) VALUES ('interval_enabled', '1');")
            conn.commit()
        finally:
            conn.close()
        db.init_db(legacy_db)
        legacy_cfg = db.get_settings(legacy_db)
        self.assertEqual(legacy_cfg["schedule_mode"], "interval")
        self.assertIs(legacy_cfg["interval_enabled"], True)

    def test_schedules_crud_and_validation(self) -> None:
        s1 = db.create_schedule(self.db_path, "18:30", 2, enabled=True)
        s2 = db.create_schedule(self.db_path, "07:00", 1, enabled=False)

        # Ordered by time_hhmm ascending
        all_scheds = db.list_schedules(self.db_path)
        self.assertEqual([s["time_hhmm"] for s in all_scheds], ["07:00", "18:30"])
        self.assertEqual([s["id"] for s in db.list_schedules(self.db_path, enabled_only=True)], [s1["id"]])

        # Get & update
        self.assertEqual(db.get_schedule(self.db_path, s1["id"])["portions"], 2)
        updated = db.update_schedule(self.db_path, s2["id"], time_hhmm="08:15", portions=4, enabled=True)
        self.assertIsNotNone(updated)
        self.assertEqual(updated["time_hhmm"], "08:15")
        self.assertEqual(updated["portions"], 4)
        self.assertTrue(updated["enabled"])

        # Mark triggered
        self.assertTrue(db.mark_schedule_triggered(self.db_path, s1["id"], "2026-11-25"))
        self.assertEqual(db.get_schedule(self.db_path, s1["id"])["last_triggered_date"], "2026-11-25")

        # Delete & nonexistent
        self.assertTrue(db.delete_schedule(self.db_path, s1["id"]))
        self.assertFalse(db.delete_schedule(self.db_path, s1["id"]))
        self.assertIsNone(db.get_schedule(self.db_path, s1["id"]))
        self.assertIsNone(db.update_schedule(self.db_path, 99999, portions=1))

        # Validation failures
        for bad_time in ("8:00", "24:00", "12:60", "ab:cd", "12:00:00", 800):
            with self.assertRaises(ValueError):
                db.create_schedule(self.db_path, bad_time, 1)  # type: ignore[arg-type]
        for bad_portions in (0, 11, -1, True, "two"):
            with self.assertRaises(ValueError):
                db.create_schedule(self.db_path, "09:00", bad_portions)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            db.mark_schedule_triggered(self.db_path, s2["id"], "25-11-2026")

    def test_dispense_events_crud_and_pagination(self) -> None:
        id1 = db.add_dispense_event(
            self.db_path, "schedule", 2, 8192, "ok", "08:00", timestamp="2026-11-25T08:00:00-03:00"
        )
        id2 = db.add_dispense_event(
            self.db_path, "manual_web", 1, 4096, "busy", "ocupado", timestamp="2026-11-25T09:00:00-03:00"
        )
        id3 = db.add_dispense_event(
            self.db_path, "unjam", 0, 512, "error", "i2c fail", timestamp="2026-11-25T10:00:00-03:00"
        )
        self.assertEqual(db.count_dispense_events(self.db_path), 3)

        page1 = db.list_dispense_events(self.db_path, limit=2, offset=0)
        self.assertEqual([e["id"] for e in page1], [id3, id2])
        page2 = db.list_dispense_events(self.db_path, limit=2, offset=2)
        self.assertEqual([e["id"] for e in page2], [id1])

        with self.assertRaises(ValueError):
            db.add_dispense_event(self.db_path, "invalid_source", 1, 4096, "ok")
        with self.assertRaises(ValueError):
            db.add_dispense_event(self.db_path, "schedule", 1, 4096, "unknown_status")

    def test_media_captures_and_pruning(self) -> None:
        items = []
        for idx in range(5):
            media_type = "video" if idx % 2 == 1 else "photo"
            ext = "mp4" if media_type == "video" else "jpg"
            item = db.add_media_capture(
                self.db_path,
                trigger="ir" if idx < 3 else "manual",
                media_type=media_type,
                filename=f"20261125_10000{idx}_cap.{ext}",
                size_bytes=100 + idx,
                duration_s=5.0 if media_type == "video" else 0.0,
                timestamp=f"2026-11-25T10:00:0{idx}-03:00",
            )
            items.append(item)

        self.assertEqual(db.count_media_captures(self.db_path), 5)
        self.assertEqual(db.count_media_captures(self.db_path, media_type="photo"), 3)
        self.assertEqual(db.count_media_captures(self.db_path, media_type="video"), 2)

        photos = db.list_media_captures(self.db_path, media_type="photo")
        self.assertEqual([p["filename"] for p in photos], [items[4]["filename"], items[2]["filename"], items[0]["filename"]])

        # Prune down to 3 newest -> oldest 2 (idx 0, idx 1) removed
        pruned = db.prune_excess_media(self.db_path, max_files=3)
        self.assertEqual(set(pruned), {items[0]["filename"], items[1]["filename"]})
        self.assertEqual(db.count_media_captures(self.db_path), 3)

        # Single delete
        deleted = db.delete_media_capture(self.db_path, items[4]["id"])
        self.assertIsNotNone(deleted)
        self.assertEqual(deleted["filename"], items[4]["filename"])
        self.assertIsNone(db.get_media_capture(self.db_path, items[4]["id"]))
        self.assertIsNone(db.delete_media_capture(self.db_path, items[4]["id"]))

        # Validation failures (path traversal, invalid trigger/type)
        for bad_name in ("../evil.jpg", "sub/a.jpg", "a\\b.jpg", "..", ""):
            with self.assertRaises(ValueError):
                db.add_media_capture(self.db_path, "manual", "photo", bad_name)
        with self.assertRaises(ValueError):
            db.add_media_capture(self.db_path, "bad_trigger", "photo", "ok.jpg")
        with self.assertRaises(ValueError):
            db.add_media_capture(self.db_path, "manual", "audio", "ok.mp3")


if __name__ == "__main__":
    unittest.main()
