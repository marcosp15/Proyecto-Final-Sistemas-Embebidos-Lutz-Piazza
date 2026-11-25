#!/usr/bin/env python3
"""Punto de entrada del servicio Raspberry Pi para el dispenser automático de alimento."""

import argparse
import logging
from pathlib import Path
import signal
import sys

_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from i2c_master.controller import FeederMaster
from service.api import create_app
from service.camera import CameraManager
from service.client_sync import ThreadSafeFeederMaster
from service.config import ServiceConfig
from service.database import init_db
from service.worker import FeederWorkerThread


def parse_args(argv=None) -> ServiceConfig:
    base = ServiceConfig.from_env()
    parser = argparse.ArgumentParser(
        description="Servicio del Dispenser Automático de Alimento (Raspberry Pi)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--mock", action="store_true", default=base.mock, help="Ejecutar con hardware simulado")
    parser.add_argument("--bus", type=int, default=base.i2c_bus, help="Número de bus I2C (/dev/i2c-X)")
    parser.add_argument(
        "--addr",
        type=lambda x: int(str(x), 0),
        default=base.slave_addr,
        help="Dirección I2C del esclavo Arduino (ej. 0x08)",
    )
    parser.add_argument("--db", type=str, default=base.db_path, help="Ruta de la base de datos SQLite")
    parser.add_argument("--media-dir", type=str, default=base.media_dir, help="Directorio de fotos/videos")
    parser.add_argument("--host", type=str, default=base.host, help="Interfaz de escucha HTTP")
    parser.add_argument("--port", type=int, default=base.port, help="Puerto HTTP")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=base.poll_interval_s,
        help="Intervalo de polling I2C del worker en segundos",
    )
    parser.add_argument("--debug", action="store_true", default=base.debug, help="Habilitar logs DEBUG")

    args = parser.parse_args(argv)
    return ServiceConfig(
        i2c_bus=args.bus,
        slave_addr=args.addr,
        mock=args.mock,
        db_path=args.db,
        media_dir=args.media_dir,
        host=args.host,
        port=args.port,
        poll_interval_s=args.poll_interval,
        i2c_delay_s=base.i2c_delay_s,
        debug=args.debug,
    )


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    cfg = parse_args(argv)
    setup_logging(cfg.debug)
    logger = logging.getLogger("service.main")

    logger.info(
        "Iniciando Dispenser (modo=%s, bus=%d, addr=0x%02X, db=%s, media=%s, http=%s:%d)",
        "MOCK" if cfg.mock else "HARDWARE",
        cfg.i2c_bus,
        cfg.slave_addr,
        cfg.db_path,
        cfg.media_dir,
        cfg.host,
        cfg.port,
    )

    init_db(cfg.db_path)

    raw_master = FeederMaster(
        bus=cfg.i2c_bus,
        addr=cfg.slave_addr,
        mock=cfg.mock,
        delay=cfg.i2c_delay_s,
    )
    client = ThreadSafeFeederMaster(raw_master)
    camera = CameraManager(media_dir=cfg.media_dir, mock=cfg.mock)
    worker = FeederWorkerThread(
        client=client,
        db_path=cfg.db_path,
        camera=camera,
        poll_interval=cfg.poll_interval_s,
    )
    worker.start()

    stopped = False

    def shutdown() -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        worker.stop(wait_captures=False)
        worker.join(timeout=3.0)
        client.close()
        logger.info("Servicio detenido ordenadamente.")

    def handle_signal(signum, _frame):
        logger.info("Señal recibida (%s). Deteniendo servicio...", signum)
        shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    app = create_app(client=client, db_path=cfg.db_path, camera=camera, worker=worker)
    try:
        app.run(host=cfg.host, port=cfg.port, threaded=True, use_reloader=False)
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
