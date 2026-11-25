"""Tests for CameraManager: backend autodetection, mock/CLI capture, concurrency, safe pruning."""

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

RASPBERRY_DIR = Path(__file__).resolve().parents[1]
if str(RASPBERRY_DIR) not in sys.path:
    sys.path.insert(0, str(RASPBERRY_DIR))

from service.camera import CameraBusyError, CameraError, CameraManager


class TestCameraBackendAutodetection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.media_dir = Path(self._tmpdir.name) / "media"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_priority_order_rpicam_over_libcamera_over_legacy_over_none(self) -> None:
        # All present -> rpicam wins
        all_bins = {
            "rpicam-still": "/usr/bin/rpicam-still",
            "rpicam-vid": "/usr/bin/rpicam-vid",
            "libcamera-still": "/usr/bin/libcamera-still",
            "raspistill": "/usr/bin/raspistill",
        }
        with mock.patch("shutil.which", side_effect=lambda name: all_bins.get(name)):
            cam = CameraManager(self.media_dir)
            self.assertEqual(cam.backend_name, "rpicam")

        # Only libcamera + legacy -> libcamera wins
        lib_bins = {
            "libcamera-still": "/usr/bin/libcamera-still",
            "libcamera-vid": "/usr/bin/libcamera-vid",
            "raspistill": "/usr/bin/raspistill",
        }
        with mock.patch("shutil.which", side_effect=lambda name: lib_bins.get(name)):
            cam = CameraManager(self.media_dir)
            self.assertEqual(cam.backend_name, "libcamera")

        # Only legacy -> legacy wins
        legacy_bins = {"raspistill": "/usr/bin/raspistill", "raspivid": "/usr/bin/raspivid"}
        with mock.patch("shutil.which", side_effect=lambda name: legacy_bins.get(name)):
            cam = CameraManager(self.media_dir)
            self.assertEqual(cam.backend_name, "legacy")

        # None present -> "none"
        with mock.patch("shutil.which", return_value=None):
            cam = CameraManager(self.media_dir)
            self.assertEqual(cam.backend_name, "none")
            with self.assertRaises(CameraError):
                cam.capture_photo()
            with self.assertRaises(CameraError):
                cam.capture_video(duration_s=3)

        # Mock mode ignores system binaries
        with mock.patch("shutil.which", return_value=None):
            cam_mock = CameraManager(self.media_dir, mock=True)
            self.assertEqual(cam_mock.backend_name, "mock")
            cam_mock2 = CameraManager(self.media_dir, backend="mock")
            self.assertEqual(cam_mock2.backend_name, "mock")

        with self.assertRaises(ValueError):
            CameraManager(self.media_dir, backend="unsupported")


class TestCameraMockAndCLI(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.media_dir = self.root / "media"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_mock_photo_valid_jpeg_and_mock_video(self) -> None:
        cam = CameraManager(self.media_dir, mock=True)

        photo = cam.capture_photo(trigger="ir")
        self.assertEqual(photo.media_type, "photo")
        self.assertEqual(photo.duration_s, 0.0)
        self.assertTrue(photo.filename.endswith("_ir.jpg"))
        self.assertNotIn("/", photo.filename)
        photo_path = cam.media_dir / photo.filename
        self.assertTrue(photo_path.is_file())
        data = photo_path.read_bytes()
        self.assertEqual(len(data), photo.size_bytes)
        self.assertTrue(data.startswith(b"\xff\xd8"))
        self.assertTrue(data.endswith(b"\xff\xd9"))

        video = cam.capture_video(duration_s=5, trigger="manual")
        self.assertEqual(video.media_type, "video")
        self.assertEqual(video.duration_s, 5.0)
        self.assertTrue(video.filename.endswith("_manual.mp4"))
        video_path = cam.media_dir / video.filename
        self.assertTrue(video_path.is_file())
        vdata = video_path.read_bytes()
        self.assertEqual(len(vdata), video.size_bytes)
        self.assertIn(b"ftyp", vdata[:16])

    def test_video_duration_validation(self) -> None:
        cam = CameraManager(self.media_dir, mock=True)
        for bad in (1, 0, -5, 16, True, False, 5.0, "5"):
            with self.subTest(duration=bad):
                with self.assertRaises(ValueError):
                    cam.capture_video(duration_s=bad)  # type: ignore[arg-type]

        # Boundaries 2 and 15 accepted
        self.assertEqual(cam.capture_video(duration_s=2).duration_s, 2.0)
        self.assertEqual(cam.capture_video(duration_s=15).duration_s, 15.0)

    def test_cli_video_remux_and_cleanup_on_missing_ffmpeg_or_timeout(self) -> None:
        bins = {
            "rpicam-still": "/usr/bin/rpicam-still",
            "rpicam-vid": "/usr/bin/rpicam-vid",
            "ffmpeg": "/usr/bin/ffmpeg",
        }
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            if cmd[0] == "/usr/bin/rpicam-vid":
                out_idx = cmd.index("-o") + 1
                Path(cmd[out_idx]).write_bytes(b"\x00\x00\x00\x01h264data")
            elif cmd[0] == "/usr/bin/ffmpeg":
                Path(cmd[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("shutil.which", side_effect=lambda name: bins.get(name)):
            cam = CameraManager(self.media_dir)
            with mock.patch("subprocess.run", side_effect=fake_run):
                res = cam.capture_video(duration_s=4, trigger="ir")
        self.assertEqual(res.media_type, "video")
        self.assertEqual(len(calls), 2)
        self.assertIn("-c", calls[1])
        self.assertIn("copy", calls[1])
        # Temporary .h264 must be cleaned up
        self.assertEqual(list(self.media_dir.glob("*.h264")), [])

        # Missing ffmpeg -> clear CameraError
        no_ffmpeg = {"rpicam-vid": "/usr/bin/rpicam-vid"}
        with mock.patch("shutil.which", side_effect=lambda name: no_ffmpeg.get(name)):
            cam2 = CameraManager(self.media_dir)
            with self.assertRaisesRegex(CameraError, "ffmpeg"):
                cam2.capture_video(duration_s=3)

        # Timeout cleans up temp .h264
        with mock.patch("shutil.which", side_effect=lambda name: bins.get(name)):
            cam3 = CameraManager(self.media_dir)
            with mock.patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["rpicam-vid"], timeout=25)
            ):
                with self.assertRaises(CameraError):
                    cam3.capture_video(duration_s=3)
        self.assertEqual(list(self.media_dir.glob("*.h264")), [])

    def test_concurrent_capture_raises_camera_busy_error(self) -> None:
        bins = {"rpicam-still": "/usr/bin/rpicam-still"}
        entered = threading.Event()
        release = threading.Event()

        def slow_run(cmd, **_kwargs):
            entered.set()
            release.wait(timeout=5.0)
            out_idx = cmd.index("-o") + 1
            Path(cmd[out_idx]).write_bytes(b"\xff\xd8\xff\xd9")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("shutil.which", side_effect=lambda name: bins.get(name)):
            cam = CameraManager(self.media_dir)
            result_holder = []

            def worker_capture():
                result_holder.append(cam.capture_photo(trigger="manual"))

            with mock.patch("subprocess.run", side_effect=slow_run):
                t = threading.Thread(target=worker_capture)
                t.start()
                self.assertTrue(entered.wait(timeout=2.0))
                self.assertTrue(cam.is_capturing)
                with self.assertRaises(CameraBusyError):
                    cam.capture_photo(trigger="ir")
                with self.assertRaises(CameraBusyError):
                    cam.capture_video(duration_s=3, trigger="ir")
                release.set()
                t.join(timeout=2.0)

        self.assertFalse(cam.is_capturing)
        self.assertEqual(len(result_holder), 1)

    def test_safe_deletion_rejects_path_traversal_and_symlinks(self) -> None:
        cam = CameraManager(self.media_dir, mock=True)
        captured = cam.capture_photo(trigger="manual")

        outside_file = self.root / "secret.txt"
        outside_file.write_text("keep me", encoding="utf-8")

        escape_link = cam.media_dir / "escape.jpg"
        escape_link.symlink_to(outside_file)

        subdir = cam.media_dir / "nested"
        subdir.mkdir()

        for bad in ("../secret.txt", str(outside_file), "nested/x.jpg", "a\\b.jpg", "a\x00b.jpg", ".", "..", "escape.jpg", "nested", ""):
            with self.subTest(bad=bad):
                self.assertFalse(cam.delete_file(bad))

        self.assertTrue(outside_file.is_file())
        self.assertEqual(cam.prune_old_files([captured.filename, "../secret.txt"]), 1)
        self.assertFalse((cam.media_dir / captured.filename).exists())
        self.assertTrue(outside_file.is_file())


if __name__ == "__main__":
    unittest.main()
