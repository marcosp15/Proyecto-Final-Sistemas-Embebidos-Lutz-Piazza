"""API REST Flask (Contrato 4) e interfaz web local del dispenser de alimento."""

import logging
import math
from pathlib import Path
import threading
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request, send_from_directory

try:
    from i2c_master.controller import (
        FeederBusyError,
        FeederI2CError,
        FeederInvalidArgError,
    )
    from i2c_master.protocol import (
        DEFAULT_UNJAM_STEPS,
        MAX_ABS_STEPS,
        MAX_UNJAM_STEPS,
        MIN_UNJAM_STEPS,
        STEPS_PER_REVOLUTION,
    )
    from service import database
    from service.camera import (
        CameraBusyError,
        CameraError,
        CameraManager,
        MAX_VIDEO_DURATION_S,
        MIN_VIDEO_DURATION_S,
    )
    from service.client_sync import ThreadSafeFeederMaster
    from service.worker import FeederWorkerThread, compute_next_schedule
except ImportError:
    from ..i2c_master.controller import (
        FeederBusyError,
        FeederI2CError,
        FeederInvalidArgError,
    )
    from ..i2c_master.protocol import (
        DEFAULT_UNJAM_STEPS,
        MAX_ABS_STEPS,
        MAX_UNJAM_STEPS,
        MIN_UNJAM_STEPS,
        STEPS_PER_REVOLUTION,
    )
    from . import database
    from .camera import (
        CameraBusyError,
        CameraError,
        CameraManager,
        MAX_VIDEO_DURATION_S,
        MIN_VIDEO_DURATION_S,
    )
    from .client_sync import ThreadSafeFeederMaster
    from .worker import FeederWorkerThread, compute_next_schedule

logger = logging.getLogger("service.api")
WEB_DIR = Path(__file__).resolve().parent / "web"
_SCHEDULE_ALLOWED_FIELDS = frozenset({"time_hhmm", "portions", "enabled"})


def _error_response(message: str, code: int, **extra: Any):
    payload: Dict[str, Any] = {"status": "error", "error": message}
    payload.update(extra)
    return jsonify(payload), code


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_pagination(default_limit: int = 50, max_limit: int = 500) -> tuple:
    raw_limit = request.args.get("limit", str(default_limit))
    raw_offset = request.args.get("offset", "0")
    try:
        limit = int(raw_limit)
        offset = int(raw_offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit y offset deben ser enteros") from exc
    limit = max(1, min(max_limit, limit))
    offset = max(0, offset)
    return limit, offset


def _is_safe_filename(filename: str) -> bool:
    if not filename or "\x00" in filename or "/" in filename or "\\" in filename:
        return False
    if filename in (".", "..") or Path(filename).name != filename:
        return False
    return True


def _format_config_response(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    out["turns_per_portion"] = round(cfg["steps_per_portion"] / float(STEPS_PER_REVOLUTION), 2)
    return out


def _parse_turns_per_portion(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("turns_per_portion debe ser un número entre 0.5 y 10.0 en pasos de 0.5")
    turns = float(value)
    if not math.isfinite(turns) or not (0.5 <= turns <= 10.0):
        raise ValueError("turns_per_portion debe estar entre 0.5 y 10.0")
    doubled = turns * 2.0
    if abs(doubled - round(doubled)) > 1e-9:
        raise ValueError("turns_per_portion debe ser múltiplo de 0.5")
    return int(round(turns * STEPS_PER_REVOLUTION))


def _fmt_portions(value: float) -> str:
    return f"{round(float(value), 2):.2f}".rstrip("0").rstrip(".")


def create_app(
    client: ThreadSafeFeederMaster,
    db_path: str,
    camera: CameraManager,
    worker: Optional[FeederWorkerThread] = None,
) -> Flask:
    """Fábrica de la aplicación Flask implementando todos los endpoints del Contrato 4."""
    database.init_db(db_path)
    app = Flask(__name__)
    action_lock = threading.Lock()
    action_state: Dict[str, Any] = {"kind": "idle", "steps": 0, "portions": 0.0}

    @app.route("/", methods=["GET"])
    def index():
        resp = send_from_directory(str(WEB_DIR), "index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/api/status", methods=["GET"])
    def api_status():
        try:
            try:
                st = client.get_status(consume_latch=False)
                arduino_info = {
                    "connected": True,
                    "motor_busy": st.motor_busy,
                    "steps_remaining": st.steps_remaining,
                    "ir_live": st.ir_live,
                    "last_error": st.last_error,
                }
            except FeederI2CError:
                arduino_info = {
                    "connected": False,
                    "motor_busy": False,
                    "steps_remaining": 0,
                    "ir_live": False,
                    "last_error": 0,
                }

            worker_info = (
                worker.get_status()
                if worker is not None
                else {
                    "running": False,
                    "polls": 0,
                    "errors": 0,
                    "last_poll_at": None,
                    "next_schedule": compute_next_schedule(db_path),
                }
            )

            if camera.is_capturing:
                current_action = "Capturando multimedia..."
            elif arduino_info["motor_busy"]:
                with action_lock:
                    kind = action_state["kind"]
                    tracked_portions = action_state["portions"]
                steps_rem = abs(arduino_info["steps_remaining"])
                if kind == "unjam":
                    current_action = f"Destrabando ({steps_rem} pasos rest.)"
                else:
                    if kind == "dispense" and steps_rem > 0 and tracked_portions > 0:
                        porc_val = tracked_portions
                    else:
                        settings = database.get_settings(db_path)
                        porc_val = steps_rem / float(settings["steps_per_portion"])
                    current_action = (
                        f"Dispensando {steps_rem} pasos ({_fmt_portions(porc_val)} porc.)"
                    )
            else:
                with action_lock:
                    action_state["kind"] = "idle"
                    action_state["steps"] = 0
                    action_state["portions"] = 0.0
                current_action = "En espera"

            return (
                jsonify(
                    {
                        "status": "ok",
                        "mode": "mock" if client.is_mock else "hardware",
                        "current_action": current_action,
                        "arduino": arduino_info,
                        "worker": worker_info,
                        "camera": {
                            "backend": camera.backend_name,
                            "capturing": camera.is_capturing,
                        },
                        "server_time": database.now_iso(),
                    }
                ),
                200,
            )
        except Exception as exc:
            logger.exception("Error en GET /api/status: %s", exc)
            return _error_response(str(exc), 500)

    @app.route("/api/config", methods=["GET"])
    def api_get_config():
        try:
            cfg = _format_config_response(database.get_settings(db_path))
            return jsonify({"status": "ok", "config": cfg}), 200
        except Exception as exc:
            logger.exception("Error en GET /api/config: %s", exc)
            return _error_response(str(exc), 500)

    @app.route("/api/config", methods=["PUT", "POST"])
    def api_put_config():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data:
            return _error_response("Se requiere un objeto JSON no vacío", 400)

        try:
            prev = database.get_settings(db_path)
            updates = dict(data)

            if "turns_per_portion" in updates:
                derived_steps = _parse_turns_per_portion(updates.pop("turns_per_portion"))
                if "steps_per_portion" in updates:
                    raw_steps = updates["steps_per_portion"]
                    if not _is_strict_int(raw_steps) or raw_steps != derived_steps:
                        return _error_response(
                            "turns_per_portion y steps_per_portion entran en conflicto", 400
                        )
                updates["steps_per_portion"] = derived_steps

            # Si se activa intervalo y no había referencia previa, anclar a ahora
            transitioning_to_interval = False
            if "schedule_mode" in updates:
                transitioning_to_interval = updates["schedule_mode"] == "interval"
            elif "interval_enabled" in updates:
                enabling = updates["interval_enabled"]
                transitioning_to_interval = enabling in (True, 1) or (
                    isinstance(enabling, str)
                    and enabling.strip().lower() in ("1", "true", "yes", "on")
                )

            if (
                transitioning_to_interval
                and prev["schedule_mode"] != "interval"
                and "last_interval_run_at" not in updates
            ):
                updates["last_interval_run_at"] = database.now_iso()

            cfg = database.update_settings(db_path, updates)
        except ValueError as exc:
            return _error_response(str(exc), 400)
        except Exception as exc:
            logger.exception("Error en %s /api/config: %s", request.method, exc)
            return _error_response(str(exc), 500)

        arduino_synced = False
        if (
            "steps_per_portion" in data
            or "turns_per_portion" in data
            or "step_interval_us" in data
        ):
            try:
                client.set_config(cfg["steps_per_portion"], cfg["step_interval_us"])
                arduino_synced = True
            except FeederI2CError as exc:
                logger.warning("Config guardada en BD pero falló sync con Arduino: %s", exc)
                arduino_synced = False

        return (
            jsonify(
                {
                    "status": "ok",
                    "config": _format_config_response(cfg),
                    "arduino_synced": arduino_synced,
                }
            ),
            200,
        )

    @app.route("/api/schedules", methods=["GET"])
    def api_list_schedules():
        try:
            return jsonify({"status": "ok", "schedules": database.list_schedules(db_path)}), 200
        except Exception as exc:
            logger.exception("Error en GET /api/schedules: %s", exc)
            return _error_response(str(exc), 500)

    @app.route("/api/schedules", methods=["POST"])
    def api_create_schedule():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error_response("Se requiere un objeto JSON", 400)

        unknown = set(data.keys()) - _SCHEDULE_ALLOWED_FIELDS
        if unknown:
            return _error_response(f"Campos desconocidos: {sorted(unknown)}", 400)
        null_fields = [k for k, v in data.items() if v is None]
        if null_fields:
            return _error_response(f"Campos nulos no permitidos: {sorted(null_fields)}", 400)
        if "time_hhmm" not in data or "portions" not in data:
            return _error_response("Los campos 'time_hhmm' y 'portions' son obligatorios", 400)

        try:
            sched = database.create_schedule(
                db_path,
                time_hhmm=data["time_hhmm"],
                portions=data["portions"],
                enabled=data.get("enabled", True),
            )
            return jsonify({"status": "ok", "schedule": sched}), 201
        except ValueError as exc:
            return _error_response(str(exc), 400)
        except Exception as exc:
            logger.exception("Error en POST /api/schedules: %s", exc)
            return _error_response(str(exc), 500)

    @app.route("/api/schedules/<int:schedule_id>", methods=["PUT"])
    def api_update_schedule(schedule_id: int):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data:
            return _error_response("Se requiere un objeto JSON no vacío", 400)

        unknown = set(data.keys()) - _SCHEDULE_ALLOWED_FIELDS
        if unknown:
            return _error_response(f"Campos desconocidos: {sorted(unknown)}", 400)
        null_fields = [k for k, v in data.items() if v is None]
        if null_fields:
            return _error_response(f"Campos nulos no permitidos: {sorted(null_fields)}", 400)

        try:
            sched = database.update_schedule(
                db_path,
                schedule_id=schedule_id,
                time_hhmm=data.get("time_hhmm"),
                portions=data.get("portions"),
                enabled=data.get("enabled"),
            )
        except ValueError as exc:
            return _error_response(str(exc), 400)
        except Exception as exc:
            logger.exception("Error en PUT /api/schedules/%d: %s", schedule_id, exc)
            return _error_response(str(exc), 500)

        if sched is None:
            return _error_response(f"Horario {schedule_id} no encontrado", 404)
        return jsonify({"status": "ok", "schedule": sched}), 200

    @app.route("/api/schedules/<int:schedule_id>", methods=["DELETE"])
    def api_delete_schedule(schedule_id: int):
        try:
            deleted = database.delete_schedule(db_path, schedule_id)
        except Exception as exc:
            logger.exception("Error en DELETE /api/schedules/%d: %s", schedule_id, exc)
            return _error_response(str(exc), 500)

        if not deleted:
            return _error_response(f"Horario {schedule_id} no encontrado", 404)
        return jsonify({"status": "ok", "deleted_id": schedule_id}), 200

    @app.route("/api/dispense", methods=["POST"])
    def api_dispense():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error_response("Se requiere JSON con 'portions' o 'steps'", 400)

        settings = database.get_settings(db_path)
        steps_per_portion = settings["steps_per_portion"]
        step_interval_us = settings["step_interval_us"]

        if "steps" in data:
            raw_steps = data["steps"]
            if not _is_strict_int(raw_steps) or raw_steps == 0 or abs(raw_steps) > MAX_ABS_STEPS:
                return _error_response(
                    f"'steps' debe ser entero no nulo en [-{MAX_ABS_STEPS}, {MAX_ABS_STEPS}]", 400
                )
            steps = raw_steps
            portions = round(steps / float(steps_per_portion), 3)
        elif "portions" in data:
            raw_portions = data["portions"]
            if not _is_strict_int(raw_portions) or not (1 <= raw_portions <= 10):
                return _error_response("'portions' debe ser un entero entre 1 y 10", 400)
            portions = float(raw_portions)
            steps = raw_portions * steps_per_portion
            if abs(steps) > MAX_ABS_STEPS:
                return _error_response("Total de pasos excede el límite del motor", 400)
        else:
            return _error_response("Debe indicar 'portions' (1..10) o 'steps'", 400)

        try:
            accepted = client.dispense_steps(steps, step_interval_us=step_interval_us)
            with action_lock:
                action_state["kind"] = "dispense"
                action_state["steps"] = accepted
                action_state["portions"] = portions
            event_id = database.add_dispense_event(
                db_path,
                source="manual_web",
                portions=portions,
                steps=accepted,
                status="ok",
                detail="Dispensado manual web",
            )
            return (
                jsonify(
                    {
                        "status": "ok",
                        "steps": accepted,
                        "portions": portions,
                        "event_id": event_id,
                    }
                ),
                200,
            )
        except FeederBusyError as exc:
            event_id = database.add_dispense_event(
                db_path,
                source="manual_web",
                portions=portions,
                steps=steps,
                status="busy",
                detail=str(exc),
            )
            return _error_response(str(exc), 409, event_id=event_id)
        except (FeederInvalidArgError, ValueError) as exc:
            return _error_response(str(exc), 400)
        except FeederI2CError as exc:
            event_id = database.add_dispense_event(
                db_path,
                source="manual_web",
                portions=portions,
                steps=steps,
                status="error",
                detail=str(exc),
            )
            return _error_response(str(exc), 503, event_id=event_id)

    @app.route("/api/unjam", methods=["POST"])
    def api_unjam():
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _error_response("El cuerpo debe ser un objeto JSON", 400)

        wiggle = data.get("wiggle_steps", DEFAULT_UNJAM_STEPS)
        if not _is_strict_int(wiggle) or not (MIN_UNJAM_STEPS <= wiggle <= MAX_UNJAM_STEPS):
            return _error_response(
                f"'wiggle_steps' debe ser entero entre {MIN_UNJAM_STEPS} y {MAX_UNJAM_STEPS}", 400
            )

        try:
            accepted = client.unjam(wiggle_steps=wiggle)
            with action_lock:
                action_state["kind"] = "unjam"
                action_state["steps"] = accepted
                action_state["portions"] = 0.0
            event_id = database.add_dispense_event(
                db_path,
                source="unjam",
                portions=0.0,
                steps=accepted,
                status="ok",
                detail="Secuencia de destrabe",
            )
            return jsonify({"status": "ok", "wiggle_steps": accepted, "event_id": event_id}), 200
        except FeederBusyError as exc:
            event_id = database.add_dispense_event(
                db_path,
                source="unjam",
                portions=0.0,
                steps=wiggle,
                status="busy",
                detail=str(exc),
            )
            return _error_response(str(exc), 409, event_id=event_id)
        except (FeederInvalidArgError, ValueError) as exc:
            return _error_response(str(exc), 400)
        except FeederI2CError as exc:
            event_id = database.add_dispense_event(
                db_path,
                source="unjam",
                portions=0.0,
                steps=wiggle,
                status="error",
                detail=str(exc),
            )
            return _error_response(str(exc), 503, event_id=event_id)

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        try:
            cancelled = client.stop_motor()
            with action_lock:
                action_state["kind"] = "idle"
                action_state["steps"] = 0
                action_state["portions"] = 0.0
            return jsonify({"status": "ok", "steps_cancelled": cancelled}), 200
        except FeederI2CError as exc:
            return _error_response(str(exc), 503)

    @app.route("/api/history", methods=["GET"])
    def api_history():
        try:
            limit, offset = _parse_pagination()
        except ValueError as exc:
            return _error_response(str(exc), 400)

        try:
            events = database.list_dispense_events(db_path, limit=limit, offset=offset)
            total = database.count_dispense_events(db_path)
            return jsonify({"status": "ok", "total": total, "events": events}), 200
        except Exception as exc:
            logger.exception("Error en GET /api/history: %s", exc)
            return _error_response(str(exc), 500)

    @app.route("/api/media", methods=["GET"])
    def api_list_media():
        try:
            limit, offset = _parse_pagination()
        except ValueError as exc:
            return _error_response(str(exc), 400)

        media_type = request.args.get("type")
        if media_type is not None and media_type not in database.VALID_MEDIA_TYPES:
            return _error_response("'type' debe ser 'photo' o 'video'", 400)

        try:
            items = database.list_media_captures(
                db_path, limit=limit, offset=offset, media_type=media_type
            )
            for item in items:
                item["url"] = f"/media/{item['filename']}"
            total = database.count_media_captures(db_path, media_type=media_type)
            return jsonify({"status": "ok", "total": total, "items": items}), 200
        except Exception as exc:
            logger.exception("Error en GET /api/media: %s", exc)
            return _error_response(str(exc), 500)

    @app.route("/api/media/capture", methods=["POST"])
    def api_capture_media():
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _error_response("El cuerpo debe ser un objeto JSON", 400)

        settings = database.get_settings(db_path)
        mode = data.get("mode", settings["capture_mode"])
        if mode not in database.VALID_CAPTURE_MODES:
            return _error_response("'mode' debe ser 'photo' o 'video'", 400)

        duration_s = data.get("duration_s", settings["video_duration_s"])
        if mode == "video":
            if not _is_strict_int(duration_s) or not (
                MIN_VIDEO_DURATION_S <= duration_s <= MAX_VIDEO_DURATION_S
            ):
                return _error_response(
                    f"'duration_s' debe ser entero entre {MIN_VIDEO_DURATION_S} y {MAX_VIDEO_DURATION_S}",
                    400,
                )

        try:
            if mode == "video":
                result = camera.capture_video(duration_s=duration_s, trigger="manual")
            else:
                result = camera.capture_photo(trigger="manual")

            item = database.add_media_capture(
                db_path,
                trigger="manual",
                media_type=result.media_type,
                filename=result.filename,
                size_bytes=result.size_bytes,
                duration_s=result.duration_s,
                timestamp=result.timestamp,
            )
            pruned = database.prune_excess_media(db_path, max_files=settings["max_media_files"])
            if pruned:
                camera.delete_files(pruned)

            item["url"] = f"/media/{item['filename']}"
            return jsonify({"status": "ok", "item": item}), 201
        except CameraBusyError as exc:
            return _error_response(str(exc), 409)
        except ValueError as exc:
            return _error_response(str(exc), 400)
        except CameraError as exc:
            return _error_response(str(exc), 503)

    @app.route("/media/<path:filename>", methods=["GET"])
    def serve_media(filename: str):
        if not _is_safe_filename(filename):
            return _error_response("Archivo no encontrado", 404)
        is_download = request.args.get("download", "").lower() in ("1", "true", "yes")
        return send_from_directory(str(camera.media_dir), filename, as_attachment=is_download)

    @app.route("/api/media/<int:media_id>", methods=["DELETE"])
    def api_delete_media(media_id: int):
        try:
            deleted = database.delete_media_capture(db_path, media_id)
        except Exception as exc:
            logger.exception("Error en DELETE /api/media/%d: %s", media_id, exc)
            return _error_response(str(exc), 500)

        if deleted is None:
            return _error_response(f"Captura {media_id} no encontrada", 404)

        camera.delete_file(deleted["filename"])
        return jsonify({"status": "ok", "deleted_id": media_id}), 200

    @app.route("/api/mock/ir", methods=["POST"])
    def api_mock_ir():
        if not client.is_mock:
            return _error_response("Endpoint solo habilitado en modo --mock", 404)
        client.simulate_ir_detection()
        return jsonify({"status": "ok", "simulated": True}), 200

    return app
