"""Capa de transporte I2C: SMBus2, Linux /dev/i2c-X y MockArduinoTransport realista."""

from abc import ABC, abstractmethod
import os
import time
from typing import Callable, Optional

from .protocol import (
    CMD_DISPENSE_STEPS,
    CMD_GET_STATUS,
    CMD_PING,
    CMD_SET_CONFIG,
    CMD_STOP_MOTOR,
    CMD_UNJAM,
    DEFAULT_DELAY_S,
    DEFAULT_I2C_BUS,
    DEFAULT_MANUAL_STEPS,
    DEFAULT_RX_LEN,
    DEFAULT_SLAVE_ADDR,
    DEFAULT_STEP_INTERVAL_US,
    DEFAULT_ULTRASONIC_THRESHOLD_CM,
    DEFAULT_UNJAM_STEPS,
    ERR_BUSY,
    ERR_INVALID_ARG,
    ERR_INVALID_FRAME,
    ERR_NONE,
    ERR_UNKNOWN_CMD,
    FLAG_MANUAL_DISPENSE_LATCHED,
    FLAG_MOTOR_BUSY,
    FLAG_ULTRASONIC_LATCHED,
    FLAG_ULTRASONIC_LIVE,
    MAX_ABS_STEPS,
    MAX_MANUAL_STEPS,
    MAX_STEP_INTERVAL_US,
    MAX_ULTRASONIC_THRESHOLD_CM,
    MAX_UNJAM_STEPS,
    MIN_MANUAL_STEPS,
    MIN_STEP_INTERVAL_US,
    MIN_ULTRASONIC_THRESHOLD_CM,
    MIN_UNJAM_STEPS,
    PROTOCOL_VERSION,
    RSP_CONFIG_ACK,
    RSP_DISPENSE_ACK,
    RSP_ERROR,
    RSP_PONG,
    RSP_STATUS,
    RSP_STOP_ACK,
    RSP_UNJAM_ACK,
    STATUS_REQ_CONSUME_LATCH,
    STATUS_REQ_READ_ONLY,
    ULTRASONIC_NO_ECHO_CM,
    decodificar_trama,
    empaquetar_trama,
    encode_error_payload,
    encode_status_payload,
    pack_i32,
    pack_u16,
    pack_u32,
    unpack_i32,
    unpack_u16,
    unpack_u32,
)

try:
    import smbus2

    HAS_SMBUS2 = True
except ImportError:
    HAS_SMBUS2 = False

try:
    import fcntl

    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

UNJAM_PAUSE_S: float = 0.150


class I2CTransportBase(ABC):
    """Interfaz abstracta para la capa de transporte I2C."""

    @abstractmethod
    def send_and_receive(
        self,
        tx_bytes: bytes,
        rx_len: int = DEFAULT_RX_LEN,
        delay_s: float = DEFAULT_DELAY_S,
    ) -> bytes:
        """Transmite bytes al esclavo y retorna la respuesta leída."""

    def close(self) -> None:
        """Libera recursos del transporte."""


class SMBus2Transport(I2CTransportBase):
    """Transporte I2C usando smbus2 con transacciones i2c_rdwr."""

    def __init__(self, bus_num: int = DEFAULT_I2C_BUS, slave_addr: int = DEFAULT_SLAVE_ADDR):
        if not HAS_SMBUS2:
            raise ImportError("La biblioteca 'smbus2' no está disponible en este entorno.")
        self.bus_num = bus_num
        self.slave_addr = slave_addr
        self.bus = smbus2.SMBus(self.bus_num)

    def send_and_receive(
        self,
        tx_bytes: bytes,
        rx_len: int = DEFAULT_RX_LEN,
        delay_s: float = DEFAULT_DELAY_S,
    ) -> bytes:
        write_msg = smbus2.i2c_msg.write(self.slave_addr, list(tx_bytes))
        self.bus.i2c_rdwr(write_msg)

        if delay_s > 0:
            time.sleep(delay_s)

        read_msg = smbus2.i2c_msg.read(self.slave_addr, rx_len)
        self.bus.i2c_rdwr(read_msg)
        return bytes(read_msg)

    def close(self) -> None:
        try:
            self.bus.close()
        except Exception:
            pass


class LinuxI2CDevTransport(I2CTransportBase):
    """Transporte I2C nativo del kernel Linux (/dev/i2c-X + ioctl I2C_SLAVE)."""

    I2C_SLAVE = 0x0703

    def __init__(self, bus_num: int = DEFAULT_I2C_BUS, slave_addr: int = DEFAULT_SLAVE_ADDR):
        if not HAS_FCNTL:
            raise RuntimeError("El módulo fcntl de Linux no está disponible.")
        self.bus_num = bus_num
        self.slave_addr = slave_addr
        self.dev_path = f"/dev/i2c-{bus_num}"
        self.fd: Optional[int] = os.open(self.dev_path, os.O_RDWR)
        fcntl.ioctl(self.fd, self.I2C_SLAVE, self.slave_addr)

    def send_and_receive(
        self,
        tx_bytes: bytes,
        rx_len: int = DEFAULT_RX_LEN,
        delay_s: float = DEFAULT_DELAY_S,
    ) -> bytes:
        if self.fd is None:
            raise RuntimeError("Descriptor I2C cerrado.")
        os.write(self.fd, tx_bytes)

        if delay_s > 0:
            time.sleep(delay_s)

        return os.read(self.fd, rx_len)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None


class MockArduinoTransport(I2CTransportBase):
    """Emulador realista del firmware Arduino del dispenser para desarrollo y tests."""

    def __init__(
        self,
        bus_num: int = DEFAULT_I2C_BUS,
        slave_addr: int = DEFAULT_SLAVE_ADDR,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.bus_num = bus_num
        self.slave_addr = slave_addr
        self._clock = clock

        self.manual_steps: int = DEFAULT_MANUAL_STEPS
        self.step_interval_us: int = DEFAULT_STEP_INTERVAL_US
        self.ultrasonic_threshold_cm: int = DEFAULT_ULTRASONIC_THRESHOLD_CM
        self.last_error: int = ERR_NONE

        self.distance_cm: int = ULTRASONIC_NO_ECHO_CM
        self.ultrasonic_latched: bool = False
        self.ultrasonic_live: bool = False
        self._ultrasonic_live_until: Optional[float] = None
        self.manual_dispense_latched: bool = False

        # Estado de movimiento del stepper (dispense normal o secuencia unjam)
        self._motion_steps: int = 0
        self._motion_start_t: float = 0.0
        self._active_interval_us: int = DEFAULT_STEP_INTERVAL_US
        self._steps_remaining: int = 0

        self._unjam_phase: str = "idle"  # idle | rev | pause | fwd
        self._unjam_wiggle_steps: int = 0
        self._unjam_phase_start_t: float = 0.0

    def _update_state(self) -> None:
        now = self._clock()

        if (
            self.ultrasonic_live
            and self._ultrasonic_live_until is not None
            and now >= self._ultrasonic_live_until
        ):
            self.ultrasonic_live = False
            self.distance_cm = ULTRASONIC_NO_ECHO_CM
            self._ultrasonic_live_until = None

        step_s = self._active_interval_us / 1_000_000.0

        if self._unjam_phase != "idle":
            wiggle = self._unjam_wiggle_steps
            move_dur = wiggle * step_s
            while self._unjam_phase != "idle":
                elapsed = max(0.0, now - self._unjam_phase_start_t)
                if self._unjam_phase == "rev":
                    if elapsed + 1e-9 >= move_dur:
                        self._unjam_phase = "pause"
                        self._unjam_phase_start_t += move_dur
                        self._steps_remaining = 0
                    else:
                        done = int(elapsed / step_s + 1e-9)
                        self._steps_remaining = -(wiggle - done)
                        break
                elif self._unjam_phase == "pause":
                    if elapsed + 1e-9 >= UNJAM_PAUSE_S:
                        self._unjam_phase = "fwd"
                        self._unjam_phase_start_t += UNJAM_PAUSE_S
                        self._steps_remaining = wiggle
                    else:
                        self._steps_remaining = 0
                        break
                elif self._unjam_phase == "fwd":
                    if elapsed + 1e-9 >= move_dur:
                        self._unjam_phase = "idle"
                        self._steps_remaining = 0
                    else:
                        done = int(elapsed / step_s + 1e-9)
                        self._steps_remaining = wiggle - done
                        break
            return

        if self._motion_steps != 0:
            elapsed = max(0.0, now - self._motion_start_t)
            done = int(elapsed / step_s + 1e-9)
            total = abs(self._motion_steps)
            if done >= total:
                self._motion_steps = 0
                self._steps_remaining = 0
            else:
                remaining = total - done
                self._steps_remaining = remaining if self._motion_steps > 0 else -remaining

    @property
    def is_motor_busy(self) -> bool:
        self._update_state()
        return self._unjam_phase != "idle" or self._steps_remaining != 0

    def simulate_ultrasonic_detection(
        self,
        distance_cm: int = 12,
        live_duration_s: float = 1.0,
    ) -> None:
        """Inyecta una detección ultrasónica (activa latch, presencia en vivo y distancia temporal)."""
        self.distance_cm = int(distance_cm) & 0xFFFF
        self.ultrasonic_latched = True
        self.ultrasonic_live = True
        self._ultrasonic_live_until = self._clock() + max(0.0, live_duration_s)

    def simulate_manual_button(self) -> bool:
        """Simula pulsación del botón físico D2; dispara manual_steps si el motor está libre."""
        if self.is_motor_busy:
            return False
        self._start_motion(self.manual_steps, self.step_interval_us)
        self.manual_dispense_latched = True
        return True

    def _start_motion(self, steps: int, interval_us: int) -> None:
        self._unjam_phase = "idle"
        self._motion_steps = steps
        self._steps_remaining = steps
        self._active_interval_us = interval_us
        self._motion_start_t = self._clock()

    def _error_frame(self, failed_cmd: int, error_code: int) -> bytes:
        self.last_error = error_code
        return empaquetar_trama(RSP_ERROR, encode_error_payload(failed_cmd, error_code))

    def send_and_receive(
        self,
        tx_bytes: bytes,
        rx_len: int = DEFAULT_RX_LEN,
        delay_s: float = DEFAULT_DELAY_S,
    ) -> bytes:
        self._update_state()
        pkt = decodificar_trama(tx_bytes, exact_len=True)
        if not pkt.valid:
            resp = self._error_frame(0x00, ERR_INVALID_FRAME)
        else:
            resp = self._handle_packet(pkt.type, pkt.payload)

        if len(resp) < rx_len:
            resp = resp + (b"\xff" * (rx_len - len(resp)))
        return resp[:rx_len]

    def _handle_packet(self, cmd: int, payload: bytes) -> bytes:
        if cmd == CMD_PING:
            if len(payload) != 0:
                return self._error_frame(cmd, ERR_INVALID_ARG)
            return empaquetar_trama(RSP_PONG, bytes((PROTOCOL_VERSION,)))

        if cmd == CMD_GET_STATUS:
            if len(payload) > 1:
                return self._error_frame(cmd, ERR_INVALID_ARG)
            req_flags = payload[0] if len(payload) == 1 else STATUS_REQ_READ_ONLY
            flags = 0
            if self._unjam_phase != "idle" or self._steps_remaining != 0:
                flags |= FLAG_MOTOR_BUSY
            if self.ultrasonic_latched:
                flags |= FLAG_ULTRASONIC_LATCHED
            if self.ultrasonic_live:
                flags |= FLAG_ULTRASONIC_LIVE
            if self.manual_dispense_latched:
                flags |= FLAG_MANUAL_DISPENSE_LATCHED
            frame = empaquetar_trama(
                RSP_STATUS,
                encode_status_payload(
                    flags,
                    self._steps_remaining,
                    self.last_error,
                    self.distance_cm,
                ),
            )
            if req_flags & STATUS_REQ_CONSUME_LATCH:
                self.ultrasonic_latched = False
                self.manual_dispense_latched = False
            return frame

        if cmd == CMD_DISPENSE_STEPS:
            if len(payload) not in (4, 6):
                return self._error_frame(cmd, ERR_INVALID_ARG)
            steps = unpack_i32(payload, 0)
            if steps == 0 or abs(steps) > MAX_ABS_STEPS:
                return self._error_frame(cmd, ERR_INVALID_ARG)
            if len(payload) == 6:
                interval_us = unpack_u16(payload, 4)
                if not (MIN_STEP_INTERVAL_US <= interval_us <= MAX_STEP_INTERVAL_US):
                    return self._error_frame(cmd, ERR_INVALID_ARG)
            else:
                interval_us = self.step_interval_us

            if self._unjam_phase != "idle" or self._steps_remaining != 0:
                return self._error_frame(cmd, ERR_BUSY)

            self._start_motion(steps, interval_us)
            return empaquetar_trama(RSP_DISPENSE_ACK, pack_i32(steps))

        if cmd == CMD_STOP_MOTOR:
            if len(payload) != 0:
                return self._error_frame(cmd, ERR_INVALID_ARG)
            cancelled = self._steps_remaining
            self._motion_steps = 0
            self._steps_remaining = 0
            self._unjam_phase = "idle"
            return empaquetar_trama(RSP_STOP_ACK, pack_i32(cancelled))

        if cmd == CMD_UNJAM:
            if len(payload) == 0:
                wiggle = DEFAULT_UNJAM_STEPS
            elif len(payload) == 2:
                wiggle = unpack_u16(payload, 0)
            else:
                return self._error_frame(cmd, ERR_INVALID_ARG)

            if not (MIN_UNJAM_STEPS <= wiggle <= MAX_UNJAM_STEPS):
                return self._error_frame(cmd, ERR_INVALID_ARG)
            if self._unjam_phase != "idle" or self._steps_remaining != 0:
                return self._error_frame(cmd, ERR_BUSY)

            self._motion_steps = 0
            self._unjam_phase = "rev"
            self._unjam_wiggle_steps = wiggle
            self._active_interval_us = self.step_interval_us
            self._unjam_phase_start_t = self._clock()
            self._steps_remaining = -wiggle
            return empaquetar_trama(RSP_UNJAM_ACK, pack_u16(wiggle))

        if cmd == CMD_SET_CONFIG:
            if len(payload) != 8:
                return self._error_frame(cmd, ERR_INVALID_ARG)
            manual_steps = unpack_u32(payload, 0)
            interval_us = unpack_u16(payload, 4)
            threshold_cm = unpack_u16(payload, 6)
            if (
                not (MIN_MANUAL_STEPS <= manual_steps <= MAX_MANUAL_STEPS)
                or not (MIN_STEP_INTERVAL_US <= interval_us <= MAX_STEP_INTERVAL_US)
                or not (MIN_ULTRASONIC_THRESHOLD_CM <= threshold_cm <= MAX_ULTRASONIC_THRESHOLD_CM)
            ):
                return self._error_frame(cmd, ERR_INVALID_ARG)
            self.manual_steps = manual_steps
            self.step_interval_us = interval_us
            self.ultrasonic_threshold_cm = threshold_cm
            return empaquetar_trama(
                RSP_CONFIG_ACK,
                pack_u32(manual_steps) + pack_u16(interval_us) + pack_u16(threshold_cm),
            )

        return self._error_frame(cmd, ERR_UNKNOWN_CMD)


def crear_transporte(
    bus_num: int = DEFAULT_I2C_BUS,
    slave_addr: int = DEFAULT_SLAVE_ADDR,
    forzar_mock: bool = False,
) -> I2CTransportBase:
    """Fábrica de transporte I2C con selección automática según disponibilidad."""
    if forzar_mock:
        return MockArduinoTransport(bus_num, slave_addr)

    if HAS_SMBUS2:
        try:
            return SMBus2Transport(bus_num, slave_addr)
        except Exception:
            pass

    if HAS_FCNTL and os.path.exists(f"/dev/i2c-{bus_num}"):
        return LinuxI2CDevTransport(bus_num, slave_addr)

    raise RuntimeError(
        f"No se pudo inicializar la interfaz I2C en el bus {bus_num}. "
        "Verifique /dev/i2c-X o ejecute con --mock."
    )
