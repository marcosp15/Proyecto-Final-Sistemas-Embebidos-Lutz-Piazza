"""Hilo de fondo del dispenser: polling I2C, captura IR asíncrona y planificador de comidas."""

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

try:
    from i2c_master.controller import FeederBusyError, FeederI2CError
    from i2c_master.protocol import StatusData
    from service import database
    from service.camera import CameraBusyError, CameraError, CameraManager
    from service.client_sync import ThreadSafeFeederMaster
except ImportError:
    from ..i2c_master.controller import FeederBusyError, FeederI2CError
    from ..i2c_master.protocol import StatusData
    from . import database
    from .camera import CameraBusyError, CameraError, CameraManager
    from .client_sync import ThreadSafeFeederMaster

logger = logging.getLogger("service.worker")


def compute_next_schedule(db_path: str, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Retorna la próxima comida programada según schedule_mode, o None si no hay activa."""
    settings = database.get_settings(db_path)
    mode = settings["schedule_mode"]
    if mode == "manual":
        return None

    current = now or datetime.now().astimezone()

    if mode == "interval":
        interval_minutes = settings["interval_minutes"]
        last_raw = settings["last_interval_run_at"]
        base_dt = current
        if last_raw:
            try:
                base_dt = datetime.fromisoformat(last_raw)
            except ValueError:
                base_dt = current
        next_dt = base_dt + timedelta(minutes=interval_minutes)
        return {
            "mode": "interval",
            "time_hhmm": next_dt.strftime("%H:%M"),
            "portions": settings["interval_portions"],
            "interval_minutes": interval_minutes,
        }

    schedules = database.list_schedules(db_path, enabled_only=True)
    if not schedules:
        return None

    hhmm = current.strftime("%H:%M")
    today = current.strftime("%Y-%m-%d")

    for sched in schedules:
        if sched["time_hhmm"] > hhmm or (
            sched["time_hhmm"] == hhmm and sched["last_triggered_date"] != today
        ):
            return {
                "mode": "schedules",
                "id": sched["id"],
                "time_hhmm": sched["time_hhmm"],
                "portions": sched["portions"],
            }

    first = schedules[0]
    return {
        "mode": "schedules",
        "id": first["id"],
        "time_hhmm": first["time_hhmm"],
        "portions": first["portions"],
    }


def _elapsed_seconds(current: datetime, previous: datetime) -> float:
    if current.tzinfo is not None and previous.tzinfo is None:
        previous = previous.replace(tzinfo=current.tzinfo)
    elif current.tzinfo is None and previous.tzinfo is not None:
        current = current.replace(tzinfo=previous.tzinfo)
    return (current - previous).total_seconds()


class FeederWorkerThread(threading.Thread):
    """Hilo daemon que coordina polling I2C, capturas IR asíncronas y horarios."""

    def __init__(
        self,
        client: ThreadSafeFeederMaster,
        db_path: str,
        camera: CameraManager,
        poll_interval: float = 0.5,
        now_fn: Optional[Callable[[], datetime]] = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="FeederWorkerThread", daemon=True)
        self.client = client
        self.db_path = db_path
        self.camera = camera
        self.poll_interval = max(0.05, float(poll_interval))
        self._now_fn = now_fn or (lambda: datetime.now().astimezone())
        self._monotonic_fn = monotonic_fn

        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feeder-cam")
        self._capture_future: Optional[Future] = None
        self._last_ir_capture_mono: Optional[float] = None

        self._arduino_config_synced: bool = False
        self._connected: bool = False
        self._last_status: Optional[StatusData] = None
        self._polls: int = 0
        self._errors: int = 0
        self._last_poll_at: Optional[str] = None
        self._last_error_msg: str = ""

    def run(self) -> None:
        logger.info("Worker iniciado (poll_interval=%.2fs)", self.poll_interval)
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("Error inesperado en iteración del worker: %s", exc)
                with self._state_lock:
                    self._errors += 1
                    self._last_error_msg = str(exc)
            self._stop_event.wait(self.poll_interval)
        logger.info("Worker detenido.")

    def stop(self, wait_captures: bool = False) -> None:
        """Detiene el bucle del worker y cierra el ejecutor de capturas."""
        self._stop_event.set()
        self._capture_executor.shutdown(wait=wait_captures, cancel_futures=not wait_captures)

    def wait_pending_captures(self, timeout: Optional[float] = None) -> None:
        """Espera a que finalice la última captura asíncrona en curso (útil en tests/apagado)."""
        with self._state_lock:
            fut = self._capture_future
        if fut is not None:
            fut.result(timeout=timeout)

    def run_once(self, now: Optional[datetime] = None) -> None:
        """Ejecuta un ciclo completo: sync config, poll I2C, IR, botón manual y schedulers."""
        current_dt = now or self._now_fn()
        current_iso = current_dt.isoformat(timespec="seconds")
        settings = database.get_settings(self.db_path)

        # 1. Sincronizar configuración de motor al iniciar o tras reconexión I2C
        if not self._arduino_config_synced:
            try:
                self.client.set_config(
                    settings["steps_per_portion"],
                    settings["step_interval_us"],
                )
                self._arduino_config_synced = True
            except FeederI2CError as exc:
                logger.warning("No se pudo sincronizar config inicial con Arduino: %s", exc)
                with self._state_lock:
                    self._connected = False
                    self._errors += 1
                    self._last_poll_at = current_iso
                    self._last_error_msg = str(exc)
                return

        # 2. Consultar estado del Arduino consumiendo latches de IR y botón manual
        try:
            status = self.client.get_status(consume_latch=True)
        except FeederI2CError as exc:
            logger.warning("Fallo de polling I2C: %s", exc)
            self._arduino_config_synced = False
            with self._state_lock:
                self._connected = False
                self._errors += 1
                self._last_poll_at = current_iso
                self._last_error_msg = str(exc)
            return

        with self._state_lock:
            self._connected = True
            self._last_status = status
            self._polls += 1
            self._last_poll_at = current_iso
            self._last_error_msg = ""

        # 3. Registrar dispensado iniciado desde el botón físico D2
        if status.manual_dispense_latched:
            database.add_dispense_event(
                self.db_path,
                source="manual_button",
                portions=1.0,
                steps=settings["steps_per_portion"],
                status="ok",
                detail="Botón físico D2",
                timestamp=current_iso,
            )

        # 4. Disparar captura asíncrona por detección IR respetando cooldown
        if status.ir_latched:
            self._maybe_trigger_ir_capture(settings)

        # 5. Ejecutar planificador exclusivo según schedule_mode
        mode = settings["schedule_mode"]
        if mode == "schedules":
            self._run_fixed_schedules(current_dt, current_iso, settings)
        elif mode == "interval":
            self._run_interval_schedule(current_dt, current_iso, settings)

    def _maybe_trigger_ir_capture(self, settings: Dict[str, Any]) -> None:
        cooldown_s = settings["ir_cooldown_s"]
        mono_now = self._monotonic_fn()

        with self._state_lock:
            if (
                self._last_ir_capture_mono is not None
                and (mono_now - self._last_ir_capture_mono) < cooldown_s
            ):
                return
            if self._capture_future is not None and not self._capture_future.done():
                logger.debug("Captura IR omitida: ya existe una captura en curso.")
                return
            self._last_ir_capture_mono = mono_now
            self._capture_future = self._capture_executor.submit(
                self._execute_ir_capture,
                settings["capture_mode"],
                settings["video_duration_s"],
                settings["max_media_files"],
            )

    def _execute_ir_capture(
        self,
        capture_mode: str,
        video_duration_s: int,
        max_media_files: int,
    ) -> None:
        try:
            if capture_mode == "video":
                result = self.camera.capture_video(duration_s=video_duration_s, trigger="ir")
            else:
                result = self.camera.capture_photo(trigger="ir")

            database.add_media_capture(
                self.db_path,
                trigger="ir",
                media_type=result.media_type,
                filename=result.filename,
                size_bytes=result.size_bytes,
                duration_s=result.duration_s,
                timestamp=result.timestamp,
            )
            pruned = database.prune_excess_media(self.db_path, max_files=max_media_files)
            if pruned:
                self.camera.delete_files(pruned)
        except CameraBusyError:
            logger.warning("Cámara ocupada al procesar disparo IR.")
        except (CameraError, Exception) as exc:
            logger.error("Error durante captura IR (%s): %s", capture_mode, exc)

    def _execute_scheduled_dispense(
        self,
        source: str,
        portions: int,
        steps: int,
        step_interval_us: int,
        timestamp: str,
        detail: str,
    ) -> None:
        try:
            accepted = self.client.dispense_steps(steps, step_interval_us=step_interval_us)
            database.add_dispense_event(
                self.db_path,
                source=source,
                portions=float(portions),
                steps=accepted,
                status="ok",
                detail=detail,
                timestamp=timestamp,
            )
        except FeederBusyError as exc:
            logger.warning("Dispensado %s rechazado por motor ocupado: %s", source, exc)
            database.add_dispense_event(
                self.db_path,
                source=source,
                portions=float(portions),
                steps=steps,
                status="busy",
                detail=str(exc),
                timestamp=timestamp,
            )
        except ValueError as exc:
            logger.error("Parámetros inválidos en dispensado %s: %s", source, exc)
            database.add_dispense_event(
                self.db_path,
                source=source,
                portions=float(portions),
                steps=steps,
                status="error",
                detail=str(exc),
                timestamp=timestamp,
            )
        except FeederI2CError as exc:
            logger.error("Error I2C en dispensado %s: %s", source, exc)
            self._arduino_config_synced = False
            database.add_dispense_event(
                self.db_path,
                source=source,
                portions=float(portions),
                steps=steps,
                status="error",
                detail=str(exc),
                timestamp=timestamp,
            )

    def _run_fixed_schedules(
        self,
        current_dt: datetime,
        current_iso: str,
        settings: Dict[str, Any],
    ) -> None:
        hhmm = current_dt.strftime("%H:%M")
        today = current_dt.strftime("%Y-%m-%d")

        for sched in database.list_schedules(self.db_path, enabled_only=True):
            if sched["time_hhmm"] == hhmm and sched["last_triggered_date"] != today:
                database.mark_schedule_triggered(self.db_path, sched["id"], today)
                steps = sched["portions"] * settings["steps_per_portion"]
                self._execute_scheduled_dispense(
                    source="schedule",
                    portions=sched["portions"],
                    steps=steps,
                    step_interval_us=settings["step_interval_us"],
                    timestamp=current_iso,
                    detail=f"Horario {hhmm}",
                )

    def _run_interval_schedule(
        self,
        current_dt: datetime,
        current_iso: str,
        settings: Dict[str, Any],
    ) -> None:
        if settings["schedule_mode"] != "interval":
            return

        last_run_raw = settings["last_interval_run_at"]
        if not last_run_raw:
            # Al habilitar sin historial previo, anclar a ahora para disparar tras interval_minutes
            database.set_setting(self.db_path, "last_interval_run_at", current_iso)
            return

        try:
            last_dt = datetime.fromisoformat(last_run_raw)
        except ValueError:
            database.set_setting(self.db_path, "last_interval_run_at", current_iso)
            return

        if _elapsed_seconds(current_dt, last_dt) >= settings["interval_minutes"] * 60:
            database.set_setting(self.db_path, "last_interval_run_at", current_iso)
            portions = settings["interval_portions"]
            steps = portions * settings["steps_per_portion"]
            self._execute_scheduled_dispense(
                source="interval",
                portions=portions,
                steps=steps,
                step_interval_us=settings["step_interval_us"],
                timestamp=current_iso,
                detail=f"Intervalo cada {settings['interval_minutes']} min",
            )

    def get_last_arduino_status(self) -> Tuple[bool, Optional[StatusData]]:
        with self._state_lock:
            return self._connected, self._last_status

    def get_status(self) -> Dict[str, Any]:
        with self._state_lock:
            polls = self._polls
            errors = self._errors
            last_poll_at = self._last_poll_at
        return {
            "running": self.is_alive() and not self._stop_event.is_set(),
            "polls": polls,
            "errors": errors,
            "last_poll_at": last_poll_at,
            "next_schedule": compute_next_schedule(self.db_path, self._now_fn()),
        }
