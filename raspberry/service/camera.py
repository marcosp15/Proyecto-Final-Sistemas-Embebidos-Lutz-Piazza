"""Camera capture manager with CLI autodetection, mock backend, and safe pruning."""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Tuple, Union

logger = logging.getLogger(__name__)

MIN_VIDEO_DURATION_S = 2
MAX_VIDEO_DURATION_S = 15
DEFAULT_PHOTO_TIMEOUT_S = 15
DEFAULT_FRAMERATE = 24

_BACKENDS: Tuple[Tuple[str, str, str], ...] = (
    ("rpicam", "rpicam-still", "rpicam-vid"),
    ("libcamera", "libcamera-still", "libcamera-vid"),
    ("legacy", "raspistill", "raspivid"),
)
_BACKEND_MAP = {name: (still, vid) for name, still, vid in _BACKENDS}
_SAFE_TRIGGER_RE = re.compile(r"[^A-Za-z0-9_-]+")

# Valid baseline 1x1 JFIF JPEG (SOI FF D8 .. EOI FF D9) for zero-dependency mock captures.
_MOCK_JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYzLjI0LjEwMAD/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABKAAEAAAAAAAAAAAAAAAAAAAAAAQEAAAAAAAAAAAAAAAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAEQEAAAAAAAAAAAAAAAAAAAAA/8AAEQgAAQABAwEiAAIRAAMRAP/aAAwDAQACEQMRAD8AAA//2Q=="
)

# Minimal ISO BMFF MP4 container stub (ftyp + free boxes) for mock video captures.
_MOCK_MP4_BYTES = (
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    b"\x00\x00\x00\x08free"
)


class CameraError(Exception):
    """Raised when a camera operation fails."""


class CameraBusyError(CameraError):
    """Raised when a capture is requested while another capture is in progress."""


@dataclass
class CaptureResult:
    """Metadata for a completed photo or video capture."""

    filename: str
    media_type: str
    size_bytes: int
    duration_s: float
    timestamp: str


def _sanitize_trigger(trigger: str) -> str:
    cleaned = _SAFE_TRIGGER_RE.sub("", (trigger or "").strip().lower())[:24]
    return cleaned or "manual"


def _detect_backend() -> Tuple[str, Optional[str], Optional[str]]:
    for name, still_bin, vid_bin in _BACKENDS:
        still_path = shutil.which(still_bin)
        vid_path = shutil.which(vid_bin)
        if still_path or vid_path:
            return name, still_path, vid_path
    return "none", None, None


class CameraManager:
    """Serialized Raspberry Pi camera manager supporting rpicam, libcamera, legacy, and mock."""

    def __init__(
        self,
        media_dir: Union[str, Path],
        mock: bool = False,
        width: int = 1280,
        height: int = 720,
        backend: Optional[str] = None,
        framerate: int = DEFAULT_FRAMERATE,
        photo_timeout_s: int = DEFAULT_PHOTO_TIMEOUT_S,
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive integers")
        if framerate <= 0:
            raise ValueError("framerate must be a positive integer")
        if photo_timeout_s <= 0:
            raise ValueError("photo_timeout_s must be positive")

        self._media_dir = Path(media_dir).resolve()
        self._media_dir.mkdir(parents=True, exist_ok=True)
        self.width = int(width)
        self.height = int(height)
        self.framerate = int(framerate)
        self.photo_timeout_s = int(photo_timeout_s)
        self._lock = threading.Lock()

        if mock or backend == "mock":
            self._backend = "mock"
            self._still_cmd: Optional[str] = None
            self._vid_cmd: Optional[str] = None
        elif backend is not None:
            if backend not in _BACKEND_MAP:
                raise ValueError(f"Unsupported camera backend: {backend!r}")
            still_bin, vid_bin = _BACKEND_MAP[backend]
            self._backend = backend
            self._still_cmd = shutil.which(still_bin)
            self._vid_cmd = shutil.which(vid_bin)
        else:
            self._backend, self._still_cmd, self._vid_cmd = _detect_backend()

    @property
    def media_dir(self) -> Path:
        return self._media_dir

    @property
    def backend_name(self) -> str:
        return self._backend

    @property
    def is_capturing(self) -> bool:
        return self._lock.locked()

    def _allocate_path(self, now: datetime, trigger: str, ext: str) -> Tuple[str, Path]:
        self._media_dir.mkdir(parents=True, exist_ok=True)
        safe_trigger = _sanitize_trigger(trigger)
        stamp = now.strftime("%Y%m%d_%H%M%S_%f")
        base = f"{stamp}_{safe_trigger}"
        filename = f"{base}.{ext}"
        path = self._media_dir / filename
        counter = 1
        while path.exists():
            filename = f"{base}_{counter}.{ext}"
            path = self._media_dir / filename
            counter += 1
        return filename, path

    def capture_photo(self, trigger: str = "manual") -> CaptureResult:
        """Capture a single JPEG photo. Raises CameraBusyError if already capturing."""
        if not self._lock.acquire(blocking=False):
            raise CameraBusyError("Camera is currently busy with another capture")

        try:
            now = datetime.now().astimezone()
            filename, out_path = self._allocate_path(now, trigger, "jpg")

            if self._backend == "mock":
                out_path.write_bytes(_MOCK_JPEG_BYTES)
            else:
                self._capture_photo_cli(out_path)

            size = self._verify_output_file(out_path, "photo")
            return CaptureResult(
                filename=filename,
                media_type="photo",
                size_bytes=size,
                duration_s=0.0,
                timestamp=now.isoformat(timespec="seconds"),
            )
        finally:
            self._lock.release()

    def capture_video(self, duration_s: int = 5, trigger: str = "manual") -> CaptureResult:
        """Capture an MP4 video clip (2..15 s). Raises CameraBusyError if already capturing."""
        if isinstance(duration_s, bool) or not isinstance(duration_s, int):
            raise ValueError("duration_s must be an integer between 2 and 15")
        if not (MIN_VIDEO_DURATION_S <= duration_s <= MAX_VIDEO_DURATION_S):
            raise ValueError(
                f"duration_s must be between {MIN_VIDEO_DURATION_S} and {MAX_VIDEO_DURATION_S}"
            )

        if not self._lock.acquire(blocking=False):
            raise CameraBusyError("Camera is currently busy with another capture")

        try:
            now = datetime.now().astimezone()
            filename, out_path = self._allocate_path(now, trigger, "mp4")

            if self._backend == "mock":
                out_path.write_bytes(_MOCK_MP4_BYTES)
            else:
                self._capture_video_cli(out_path, duration_s)

            size = self._verify_output_file(out_path, "video")
            return CaptureResult(
                filename=filename,
                media_type="video",
                size_bytes=size,
                duration_s=float(duration_s),
                timestamp=now.isoformat(timespec="seconds"),
            )
        finally:
            self._lock.release()

    def _capture_photo_cli(self, out_path: Path) -> None:
        if self._backend == "none":
            raise CameraError("No camera CLI backend found (tried rpicam, libcamera, legacy)")
        if not self._still_cmd:
            raise CameraError(f"Still-capture binary not available for backend {self._backend!r}")

        if self._backend == "legacy":
            cmd = [
                self._still_cmd,
                "-o", str(out_path),
                "-w", str(self.width),
                "-h", str(self.height),
                "-n",
                "-t", "500",
            ]
        else:
            cmd = [
                self._still_cmd,
                "-o", str(out_path),
                "--width", str(self.width),
                "--height", str(self.height),
                "--nopreview",
                "-t", "500",
            ]

        try:
            self._run_cmd(cmd, timeout=self.photo_timeout_s, label=f"{self._backend} photo")
        except CameraError:
            out_path.unlink(missing_ok=True)
            raise

    def _capture_video_cli(self, out_path: Path, duration_s: int) -> None:
        if self._backend == "none":
            raise CameraError("No camera CLI backend found (tried rpicam, libcamera, legacy)")
        if not self._vid_cmd:
            raise CameraError(f"Video-capture binary not available for backend {self._backend!r}")

        ffmpeg_cmd = shutil.which("ffmpeg")
        if not ffmpeg_cmd:
            raise CameraError(
                "ffmpeg is required to remux H.264 video into MP4; install it with 'sudo apt install ffmpeg'"
            )

        duration_ms = duration_s * 1000
        total_timeout = float(duration_s + 20)
        deadline = time.monotonic() + total_timeout

        fd, tmp_name = tempfile.mkstemp(prefix=".capture_", suffix=".h264", dir=str(self._media_dir))
        os.close(fd)
        tmp_h264 = Path(tmp_name)

        try:
            if self._backend == "legacy":
                rec_cmd = [
                    self._vid_cmd,
                    "-o", str(tmp_h264),
                    "-t", str(duration_ms),
                    "-w", str(self.width),
                    "-h", str(self.height),
                    "-fps", str(self.framerate),
                    "-n",
                ]
            else:
                rec_cmd = [
                    self._vid_cmd,
                    "-o", str(tmp_h264),
                    "-t", str(duration_ms),
                    "--width", str(self.width),
                    "--height", str(self.height),
                    "--framerate", str(self.framerate),
                    "--nopreview",
                ]

            self._run_cmd(rec_cmd, timeout=total_timeout, label=f"{self._backend} video")
            if not tmp_h264.is_file() or tmp_h264.stat().st_size == 0:
                raise CameraError("Video recording produced an empty H.264 file")

            remaining = max(5.0, deadline - time.monotonic())
            remux_cmd = [
                ffmpeg_cmd,
                "-y",
                "-framerate", str(self.framerate),
                "-i", str(tmp_h264),
                "-c", "copy",
                str(out_path),
            ]
            self._run_cmd(remux_cmd, timeout=remaining, label="ffmpeg remux")
        except CameraError:
            out_path.unlink(missing_ok=True)
            raise
        finally:
            tmp_h264.unlink(missing_ok=True)

    def _run_cmd(self, cmd: list[str], timeout: float, label: str) -> None:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CameraError(f"{label} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise CameraError(f"{label} failed to launch: {exc}") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise CameraError(
                f"{label} failed (exit {proc.returncode})" + (f": {detail}" if detail else "")
            )

    @staticmethod
    def _verify_output_file(path: Path, kind: str) -> int:
        if not path.is_file():
            raise CameraError(f"Expected {kind} output file was not created: {path.name}")
        size = path.stat().st_size
        if size <= 0:
            path.unlink(missing_ok=True)
            raise CameraError(f"Captured {kind} file is empty: {path.name}")
        return size

    def delete_file(self, filename: str) -> bool:
        """Safely delete a flat filename strictly inside media_dir. Returns True if removed."""
        if not isinstance(filename, str) or not filename:
            return False
        if (
            "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or filename in (".", "..")
            or Path(filename).name != filename
        ):
            logger.warning("Rejected unsafe filename for deletion: %r", filename)
            return False

        base = self._media_dir.resolve()
        candidate = base / filename
        try:
            resolved = candidate.resolve()
        except OSError:
            return False

        # Ensure resolved target (including symlinks) stays strictly a direct child of media_dir.
        if resolved.parent != base or resolved == base:
            logger.warning("Rejected path outside media_dir: %r", filename)
            return False

        if candidate.is_dir():
            return False

        try:
            candidate.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("Failed to delete %s: %s", filename, exc)
            return False

    def delete_files(self, filenames: Iterable[str]) -> int:
        """Delete multiple filenames safely inside media_dir. Returns count of deleted files."""
        return sum(1 for name in filenames if self.delete_file(name))

    prune_old_files = delete_files
