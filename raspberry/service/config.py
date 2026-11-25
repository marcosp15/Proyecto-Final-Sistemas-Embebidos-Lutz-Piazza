"""Configuración del servicio Raspberry Pi con valores por defecto y overrides por entorno."""

from dataclasses import dataclass
import os
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None else int(raw.strip(), 0)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None else float(raw.strip())


@dataclass
class ServiceConfig:
    """Parámetros de ejecución del servicio (bus I2C, SQLite, medios, HTTP y worker)."""

    i2c_bus: int = 1
    slave_addr: int = 0x08
    mock: bool = False
    db_path: str = "data/feeder.db"
    media_dir: str = "data/media"
    host: str = "0.0.0.0"
    port: int = 5000
    poll_interval_s: float = 0.5
    i2c_delay_s: float = 0.04
    debug: bool = False

    @classmethod
    def from_env(cls, **overrides: Any) -> "ServiceConfig":
        cfg = cls(
            i2c_bus=_env_int("FEEDER_I2C_BUS", cls.i2c_bus),
            slave_addr=_env_int("FEEDER_SLAVE_ADDR", cls.slave_addr),
            mock=_env_bool("FEEDER_MOCK", cls.mock),
            db_path=os.environ.get("FEEDER_DB_PATH", cls.db_path),
            media_dir=os.environ.get("FEEDER_MEDIA_DIR", cls.media_dir),
            host=os.environ.get("FEEDER_HOST", cls.host),
            port=_env_int("FEEDER_PORT", cls.port),
            poll_interval_s=_env_float("FEEDER_POLL_INTERVAL", cls.poll_interval_s),
            i2c_delay_s=_env_float("FEEDER_I2C_DELAY", cls.i2c_delay_s),
            debug=_env_bool("FEEDER_DEBUG", cls.debug),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg
