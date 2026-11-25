"""Adaptador thread-safe (threading.Lock) sobre FeederMaster para acceso concurrente al bus I2C."""

import threading
from typing import Optional, Tuple

try:
    from i2c_master.controller import FeederMaster
    from i2c_master.protocol import DEFAULT_UNJAM_STEPS, StatusData
except ImportError:
    from ..i2c_master.controller import FeederMaster
    from ..i2c_master.protocol import DEFAULT_UNJAM_STEPS, StatusData


class ThreadSafeFeederMaster:
    """Garantiza exclusión mutua en todas las transacciones I2C entre worker y API Flask."""

    def __init__(self, master: FeederMaster):
        self._master = master
        self._lock = threading.Lock()

    @property
    def master(self) -> FeederMaster:
        return self._master

    @property
    def is_mock(self) -> bool:
        return self._master.is_mock

    @property
    def slave_addr(self) -> int:
        return self._master.slave_addr

    @property
    def bus_num(self) -> int:
        return self._master.bus_num

    def ping(self) -> int:
        with self._lock:
            return self._master.ping()

    def get_status(self, consume_latch: bool = True) -> StatusData:
        with self._lock:
            return self._master.get_status(consume_latch=consume_latch)

    def dispense_steps(self, steps: int, step_interval_us: Optional[int] = None) -> int:
        with self._lock:
            return self._master.dispense_steps(steps, step_interval_us=step_interval_us)

    def stop_motor(self) -> int:
        with self._lock:
            return self._master.stop_motor()

    def unjam(self, wiggle_steps: int = DEFAULT_UNJAM_STEPS) -> int:
        with self._lock:
            return self._master.unjam(wiggle_steps=wiggle_steps)

    def set_config(self, manual_steps: int, step_interval_us: int) -> Tuple[int, int]:
        with self._lock:
            return self._master.set_config(manual_steps, step_interval_us)

    def simulate_ir_detection(self, live_duration_s: float = 1.0) -> bool:
        with self._lock:
            return self._master.simulate_ir_detection(live_duration_s=live_duration_s)

    def simulate_manual_button(self) -> bool:
        with self._lock:
            return self._master.simulate_manual_button()

    def close(self) -> None:
        with self._lock:
            self._master.close()

    def __enter__(self) -> "ThreadSafeFeederMaster":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
