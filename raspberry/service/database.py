"""Persistencia SQLite (Contrato 3) con modo WAL, settings validados y CRUD completo."""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Tuple

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time_hhmm TEXT NOT NULL,
    portions INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_triggered_date TEXT
);

CREATE TABLE IF NOT EXISTS dispense_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    portions REAL NOT NULL,
    steps INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS media_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    trigger TEXT NOT NULL,
    media_type TEXT NOT NULL,
    filename TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    duration_s REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dispense_ts ON dispense_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_media_ts ON media_captures(timestamp DESC);
"""

DEFAULT_SETTINGS: Dict[str, Any] = {
    "steps_per_portion": 4096,
    "step_interval_us": 1000,
    "capture_mode": "photo",
    "video_duration_s": 5,
    "ultrasonic_cooldown_s": 30,
    "ultrasonic_threshold_cm": 20,
    "max_media_files": 100,
    "schedule_mode": "schedules",
    "interval_enabled": False,
    "interval_minutes": 240,
    "interval_portions": 1,
    "last_interval_run_at": "",
}

_INT_SETTING_BOUNDS: Dict[str, Tuple[int, int]] = {
    "steps_per_portion": (256, 40960),
    "step_interval_us": (1000, 10000),
    "video_duration_s": (2, 15),
    "ultrasonic_cooldown_s": (5, 600),
    "ultrasonic_threshold_cm": (5, 200),
    "max_media_files": (10, 500),
    "interval_minutes": (1, 1440),
    "interval_portions": (1, 10),
}

VALID_CAPTURE_MODES = frozenset({"photo", "video"})
VALID_SCHEDULE_MODES = frozenset({"schedules", "interval", "manual"})
VALID_DISPENSE_SOURCES = frozenset({"schedule", "interval", "manual_web", "manual_button", "unjam"})
VALID_DISPENSE_STATUSES = frozenset({"ok", "busy", "error"})
VALID_MEDIA_TRIGGERS = frozenset({"ultrasonic", "manual"})
VALID_MEDIA_TYPES = frozenset({"photo", "video"})

_TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def now_iso() -> str:
    """Retorna marca temporal ISO-8601 con zona horaria local."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def _connection(db_path: str) -> Iterator[sqlite3.Connection]:
    if db_path != ":memory:":
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in ("1", "true", "yes", "on"):
            return True
        if norm in ("0", "false", "no", "off"):
            return False
    raise ValueError(f"{field} debe ser booleano o 0/1")


def _parse_int_in_range(value: Any, field: str, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} debe ser entero entre {lo} y {hi}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} debe ser entero entre {lo} y {hi}") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} debe ser entero entre {lo} y {hi}")
    if not (lo <= parsed <= hi):
        raise ValueError(f"{field} fuera de rango ({lo}..{hi}): {parsed}")
    return parsed


def _validate_and_serialize_setting(key: str, value: Any) -> str:
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"Clave de configuración desconocida: {key}")

    if key in _INT_SETTING_BOUNDS:
        lo, hi = _INT_SETTING_BOUNDS[key]
        return str(_parse_int_in_range(value, key, lo, hi))

    if key == "capture_mode":
        if not isinstance(value, str) or value not in VALID_CAPTURE_MODES:
            raise ValueError(f"capture_mode inválido: {value!r} (permitidos: photo, video)")
        return value

    if key == "schedule_mode":
        if not isinstance(value, str) or value not in VALID_SCHEDULE_MODES:
            raise ValueError(
                f"schedule_mode inválido: {value!r} (permitidos: schedules, interval, manual)"
            )
        return value

    if key == "interval_enabled":
        return "1" if _parse_bool(value, key) else "0"

    if key == "last_interval_run_at":
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("last_interval_run_at debe ser texto ISO-8601 o cadena vacía")
        if value:
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"last_interval_run_at no es ISO-8601 válido: {value!r}") from exc
        return value

    raise ValueError(f"Clave de configuración no soportada: {key}")


def _deserialize_setting(key: str, raw: str) -> Any:
    if key in _INT_SETTING_BOUNDS:
        return int(raw)
    if key == "interval_enabled":
        return raw == "1"
    return raw


def validate_time_hhmm(value: Any) -> str:
    if not isinstance(value, str) or not _TIME_HHMM_RE.match(value):
        raise ValueError(f"time_hhmm debe tener formato estricto 'HH:MM' (00:00..23:59): {value!r}")
    return value


def validate_portions(value: Any) -> int:
    return _parse_int_in_range(value, "portions", 1, 10)


def _validate_flat_filename(filename: Any) -> str:
    if not isinstance(filename, str) or not filename:
        raise ValueError("filename debe ser un nombre de archivo no vacío")
    if "/" in filename or "\\" in filename or filename in (".", "..") or Path(filename).name != filename:
        raise ValueError(f"filename debe ser un nombre plano sin rutas: {filename!r}")
    return filename


def init_db(db_path: str) -> None:
    """Inicializa esquema SQLite e inserta valores por defecto de forma idempotente."""
    with _connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Migración legacy: si interval_enabled='1' existía sin schedule_mode, sembrar 'interval'
        mode_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'schedule_mode';"
        ).fetchone()
        if mode_row is None:
            int_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'interval_enabled';"
            ).fetchone()
            if int_row is not None and int_row["value"] == "1":
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('schedule_mode', 'interval');"
                )
        # Migración legacy: ir_cooldown_s -> ultrasonic_cooldown_s y trigger 'ir' -> 'ultrasonic'
        old_ir_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'ir_cooldown_s';"
        ).fetchone()
        if old_ir_row is not None:
            has_ultra = conn.execute(
                "SELECT 1 FROM settings WHERE key = 'ultrasonic_cooldown_s';"
            ).fetchone()
            if has_ultra is None:
                try:
                    migrated_cd = _validate_and_serialize_setting(
                        "ultrasonic_cooldown_s", old_ir_row["value"]
                    )
                except ValueError:
                    lo, hi = _INT_SETTING_BOUNDS["ultrasonic_cooldown_s"]
                    try:
                        clamped = max(lo, min(hi, int(old_ir_row["value"])))
                    except (TypeError, ValueError):
                        clamped = DEFAULT_SETTINGS["ultrasonic_cooldown_s"]
                    migrated_cd = str(clamped)
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('ultrasonic_cooldown_s', ?);",
                    (migrated_cd,),
                )
            conn.execute("DELETE FROM settings WHERE key = 'ir_cooldown_s';")
        conn.execute("UPDATE media_captures SET trigger = 'ultrasonic' WHERE trigger = 'ir';")
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);",
            [(k, _validate_and_serialize_setting(k, v)) for k, v in DEFAULT_SETTINGS.items()],
        )


# --- Settings ---


def get_settings(db_path: str) -> Dict[str, Any]:
    """Retorna todas las claves de configuración tipadas."""
    result = dict(DEFAULT_SETTINGS)
    with _connection(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings;").fetchall()
        for row in rows:
            key = row["key"]
            if key in DEFAULT_SETTINGS:
                result[key] = _deserialize_setting(key, row["value"])
    return result


def get_setting(db_path: str, key: str) -> Any:
    """Retorna una clave de configuración tipada."""
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"Clave de configuración desconocida: {key}")
    with _connection(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?;", (key,)).fetchone()
        return _deserialize_setting(key, row["value"]) if row else DEFAULT_SETTINGS[key]


def update_settings(db_path: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Valida y actualiza atómicamente un subconjunto de settings; retorna el estado completo."""
    if not isinstance(updates, dict):
        raise ValueError("updates debe ser un diccionario")
    effective = dict(updates)
    sync_mode_on_disable = False
    if "schedule_mode" in effective:
        mode = _validate_and_serialize_setting("schedule_mode", effective["schedule_mode"])
        effective["interval_enabled"] = mode == "interval"
    elif "interval_enabled" in effective:
        enabled = _parse_bool(effective["interval_enabled"], "interval_enabled")
        if enabled:
            effective["schedule_mode"] = "interval"
        else:
            sync_mode_on_disable = True

    serialized = [(k, _validate_and_serialize_setting(k, v)) for k, v in effective.items()]
    with _connection(db_path) as conn:
        if sync_mode_on_disable:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'schedule_mode';"
            ).fetchone()
            if row and row["value"] == "interval":
                serialized.append(("schedule_mode", "schedules"))
        if serialized:
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                serialized,
            )
        rows = conn.execute("SELECT key, value FROM settings;").fetchall()
    result = dict(DEFAULT_SETTINGS)
    for row in rows:
        if row["key"] in DEFAULT_SETTINGS:
            result[row["key"]] = _deserialize_setting(row["key"], row["value"])
    return result


def set_setting(db_path: str, key: str, value: Any) -> Any:
    """Actualiza una sola clave validada y retorna su nuevo valor tipado."""
    return update_settings(db_path, {key: value})[key]


# --- Schedules CRUD ---


def _schedule_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "time_hhmm": row["time_hhmm"],
        "portions": row["portions"],
        "enabled": bool(row["enabled"]),
        "last_triggered_date": row["last_triggered_date"],
    }


def list_schedules(db_path: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
    query = "SELECT id, time_hhmm, portions, enabled, last_triggered_date FROM schedules"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY time_hhmm ASC, id ASC;"
    with _connection(db_path) as conn:
        return [_schedule_row_to_dict(r) for r in conn.execute(query).fetchall()]


def get_schedule(db_path: str, schedule_id: int) -> Optional[Dict[str, Any]]:
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, time_hhmm, portions, enabled, last_triggered_date FROM schedules WHERE id = ?;",
            (int(schedule_id),),
        ).fetchone()
        return _schedule_row_to_dict(row) if row else None


def create_schedule(
    db_path: str,
    time_hhmm: str,
    portions: int,
    enabled: bool = True,
) -> Dict[str, Any]:
    hhmm = validate_time_hhmm(time_hhmm)
    valid_portions = validate_portions(portions)
    enabled_int = 1 if _parse_bool(enabled, "enabled") else 0
    with _connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO schedules (time_hhmm, portions, enabled, last_triggered_date) VALUES (?, ?, ?, NULL);",
            (hhmm, valid_portions, enabled_int),
        )
        row = conn.execute(
            "SELECT id, time_hhmm, portions, enabled, last_triggered_date FROM schedules WHERE id = ?;",
            (cur.lastrowid,),
        ).fetchone()
        return _schedule_row_to_dict(row)


def update_schedule(
    db_path: str,
    schedule_id: int,
    time_hhmm: Optional[str] = None,
    portions: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    fields: List[str] = []
    params: List[Any] = []
    if time_hhmm is not None:
        fields.append("time_hhmm = ?")
        params.append(validate_time_hhmm(time_hhmm))
    if portions is not None:
        fields.append("portions = ?")
        params.append(validate_portions(portions))
    if enabled is not None:
        fields.append("enabled = ?")
        params.append(1 if _parse_bool(enabled, "enabled") else 0)

    with _connection(db_path) as conn:
        if fields:
            params.append(int(schedule_id))
            cur = conn.execute(f"UPDATE schedules SET {', '.join(fields)} WHERE id = ?;", params)
            if cur.rowcount == 0:
                return None
        row = conn.execute(
            "SELECT id, time_hhmm, portions, enabled, last_triggered_date FROM schedules WHERE id = ?;",
            (int(schedule_id),),
        ).fetchone()
        return _schedule_row_to_dict(row) if row else None


def mark_schedule_triggered(db_path: str, schedule_id: int, date_str: str) -> bool:
    if not isinstance(date_str, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise ValueError(f"date_str debe tener formato 'YYYY-MM-DD': {date_str!r}")
    with _connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE schedules SET last_triggered_date = ? WHERE id = ?;",
            (date_str, int(schedule_id)),
        )
        return cur.rowcount > 0


def delete_schedule(db_path: str, schedule_id: int) -> bool:
    with _connection(db_path) as conn:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?;", (int(schedule_id),))
        return cur.rowcount > 0


# --- Dispense events ---


def add_dispense_event(
    db_path: str,
    source: str,
    portions: float,
    steps: int,
    status: str,
    detail: str = "",
    timestamp: Optional[str] = None,
) -> int:
    if source not in VALID_DISPENSE_SOURCES:
        raise ValueError(f"source inválido: {source!r}")
    if status not in VALID_DISPENSE_STATUSES:
        raise ValueError(f"status inválido: {status!r}")
    ts = timestamp or now_iso()
    with _connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO dispense_events (timestamp, source, portions, steps, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (ts, source, float(portions), int(steps), status, str(detail or "")),
        )
        return int(cur.lastrowid)


def list_dispense_events(db_path: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    limit_val = max(0, int(limit))
    offset_val = max(0, int(offset))
    with _connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, source, portions, steps, status, detail "
            "FROM dispense_events ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?;",
            (limit_val, offset_val),
        ).fetchall()
        return [dict(r) for r in rows]


def count_dispense_events(db_path: str) -> int:
    with _connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM dispense_events;").fetchone()
        return int(row["total"]) if row else 0


# --- Media captures ---


def add_media_capture(
    db_path: str,
    trigger: str,
    media_type: str,
    filename: str,
    size_bytes: int = 0,
    duration_s: float = 0.0,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    if trigger not in VALID_MEDIA_TRIGGERS:
        raise ValueError(f"trigger inválido: {trigger!r}")
    if media_type not in VALID_MEDIA_TYPES:
        raise ValueError(f"media_type inválido: {media_type!r}")
    clean_name = _validate_flat_filename(filename)
    ts = timestamp or now_iso()
    with _connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO media_captures (timestamp, trigger, media_type, filename, size_bytes, duration_s) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (ts, trigger, media_type, clean_name, max(0, int(size_bytes)), max(0.0, float(duration_s))),
        )
        row = conn.execute(
            "SELECT id, timestamp, trigger, media_type, filename, size_bytes, duration_s "
            "FROM media_captures WHERE id = ?;",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)


def list_media_captures(
    db_path: str,
    limit: int = 50,
    offset: int = 0,
    media_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if media_type is not None and media_type not in VALID_MEDIA_TYPES:
        raise ValueError(f"media_type inválido: {media_type!r}")
    limit_val = max(0, int(limit))
    offset_val = max(0, int(offset))
    query = (
        "SELECT id, timestamp, trigger, media_type, filename, size_bytes, duration_s "
        "FROM media_captures"
    )
    params: List[Any] = []
    if media_type is not None:
        query += " WHERE media_type = ?"
        params.append(media_type)
    query += " ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?;"
    params.extend([limit_val, offset_val])
    with _connection(db_path) as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def count_media_captures(db_path: str, media_type: Optional[str] = None) -> int:
    if media_type is not None and media_type not in VALID_MEDIA_TYPES:
        raise ValueError(f"media_type inválido: {media_type!r}")
    with _connection(db_path) as conn:
        if media_type is not None:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM media_captures WHERE media_type = ?;",
                (media_type,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS total FROM media_captures;").fetchone()
        return int(row["total"]) if row else 0


def get_media_capture(db_path: str, media_id: int) -> Optional[Dict[str, Any]]:
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, timestamp, trigger, media_type, filename, size_bytes, duration_s "
            "FROM media_captures WHERE id = ?;",
            (int(media_id),),
        ).fetchone()
        return dict(row) if row else None


def delete_media_capture(db_path: str, media_id: int) -> Optional[Dict[str, Any]]:
    """Elimina un registro de media_captures y retorna sus datos (incl. filename) o None."""
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, timestamp, trigger, media_type, filename, size_bytes, duration_s "
            "FROM media_captures WHERE id = ?;",
            (int(media_id),),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM media_captures WHERE id = ?;", (int(media_id),))
        return dict(row)


def prune_excess_media(db_path: str, max_files: Optional[int] = None) -> List[str]:
    """Elimina los registros más antiguos que excedan max_files y retorna sus filenames."""
    keep = get_setting(db_path, "max_media_files") if max_files is None else max(0, int(max_files))
    with _connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, filename FROM media_captures "
            "ORDER BY timestamp DESC, id DESC LIMIT -1 OFFSET ?;",
            (keep,),
        ).fetchall()
        if not rows:
            return []
        ids = [(r["id"],) for r in rows]
        conn.executemany("DELETE FROM media_captures WHERE id = ?;", ids)
        return [r["filename"] for r in rows]
