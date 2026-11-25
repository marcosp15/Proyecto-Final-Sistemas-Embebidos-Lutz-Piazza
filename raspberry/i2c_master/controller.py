"""Controlador maestro I2C de alto nivel para el dispenser de alimento."""

from typing import Optional, Tuple

from .protocol import (
    CMD_DISPENSE_STEPS,
    CMD_GET_STATUS,
    CMD_NAMES,
    CMD_PING,
    CMD_SET_CONFIG,
    CMD_STOP_MOTOR,
    CMD_UNJAM,
    DEFAULT_DELAY_S,
    DEFAULT_I2C_BUS,
    DEFAULT_RX_LEN,
    DEFAULT_SLAVE_ADDR,
    DEFAULT_UNJAM_STEPS,
    ERR_BUSY,
    ERR_INVALID_ARG,
    ERROR_NAMES,
    MAX_ABS_STEPS,
    MAX_MANUAL_STEPS,
    MAX_STEP_INTERVAL_US,
    MAX_UNJAM_STEPS,
    MIN_MANUAL_STEPS,
    MIN_STEP_INTERVAL_US,
    MIN_UNJAM_STEPS,
    RSP_CONFIG_ACK,
    RSP_DISPENSE_ACK,
    RSP_ERROR,
    RSP_NAMES,
    RSP_PONG,
    RSP_STATUS,
    RSP_STOP_ACK,
    RSP_UNJAM_ACK,
    STATUS_REQ_CONSUME_LATCH,
    STATUS_REQ_READ_ONLY,
    DecodedPacket,
    StatusData,
    decode_error_payload,
    decode_status_payload,
    decodificar_trama,
    empaquetar_trama,
    pack_i32,
    pack_u16,
    pack_u32,
    unpack_i32,
    unpack_u16,
    unpack_u32,
)
from .transport import I2CTransportBase, MockArduinoTransport, crear_transporte


class FeederI2CError(Exception):
    """Error de comunicación o framing I2C con el nodo Arduino."""


class FeederProtocolError(FeederI2CError):
    """Error explícito devuelto por el Arduino mediante RSP_ERROR (0xFE)."""

    def __init__(self, failed_cmd: int, error_code: int, message: Optional[str] = None):
        self.failed_cmd = failed_cmd
        self.error_code = error_code
        cmd_name = CMD_NAMES.get(failed_cmd, f"0x{failed_cmd:02X}")
        err_name = ERROR_NAMES.get(error_code, f"0x{error_code:02X}")
        super().__init__(message or f"Arduino rechazó {cmd_name} con {err_name} (0x{error_code:02X})")


class FeederBusyError(FeederProtocolError):
    """El motor está ocupado ejecutando otro movimiento o secuencia unjam."""


class FeederInvalidArgError(FeederProtocolError):
    """El Arduino rechazó los parámetros del comando (ERR_INVALID_ARG)."""


class FeederMaster:
    """Cliente I2C del dispenser de alimento sobre el protocolo binario del Contrato 2."""

    def __init__(
        self,
        bus: int = DEFAULT_I2C_BUS,
        addr: int = DEFAULT_SLAVE_ADDR,
        mock: bool = False,
        delay: float = DEFAULT_DELAY_S,
        rx_len: int = DEFAULT_RX_LEN,
        transport: Optional[I2CTransportBase] = None,
    ):
        self.bus_num = bus
        self.slave_addr = addr
        self.delay = delay
        self.rx_len = rx_len
        self.transport = transport or crear_transporte(self.bus_num, self.slave_addr, forzar_mock=mock)
        self.is_mock = mock or isinstance(self.transport, MockArduinoTransport)

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "FeederMaster":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _send_command(
        self,
        cmd_type: int,
        payload: bytes = b"",
        expected_rsp: Optional[int] = None,
    ) -> DecodedPacket:
        tx_frame = empaquetar_trama(cmd_type, payload)
        try:
            rx_raw = self.transport.send_and_receive(tx_frame, rx_len=self.rx_len, delay_s=self.delay)
        except OSError as exc:
            detail = f"Error I2C en bus {self.bus_num} (addr 0x{self.slave_addr:02X}): {exc}"
            raise FeederI2CError(detail) from exc
        except Exception as exc:
            raise FeederI2CError(f"Fallo de transporte I2C: {exc}") from exc

        decoded = decodificar_trama(rx_raw)
        if not decoded.valid:
            raise FeederI2CError(f"Trama inválida desde Arduino: {decoded.error}")

        if decoded.type == RSP_ERROR:
            try:
                failed_cmd, error_code = decode_error_payload(decoded.payload)
            except ValueError as exc:
                raise FeederI2CError(str(exc)) from exc
            if error_code == ERR_BUSY:
                raise FeederBusyError(failed_cmd, error_code)
            if error_code == ERR_INVALID_ARG:
                raise FeederInvalidArgError(failed_cmd, error_code)
            raise FeederProtocolError(failed_cmd, error_code)

        if expected_rsp is not None and decoded.type != expected_rsp:
            got = RSP_NAMES.get(decoded.type, f"0x{decoded.type:02X}")
            want = RSP_NAMES.get(expected_rsp, f"0x{expected_rsp:02X}")
            raise FeederI2CError(f"Respuesta inesperada: {got} (esperada {want})")

        return decoded

    def ping(self) -> int:
        """Verifica enlace I2C (CMD_PING) y retorna la versión de protocolo."""
        pkt = self._send_command(CMD_PING, b"", RSP_PONG)
        if len(pkt.payload) != 1:
            raise FeederI2CError(f"RSP_PONG inválido: longitud {len(pkt.payload)}")
        return pkt.payload[0]

    def get_status(self, consume_latch: bool = True) -> StatusData:
        """Consulta estado del motor, sensor IR, botón manual y último error."""
        req_flag = STATUS_REQ_CONSUME_LATCH if consume_latch else STATUS_REQ_READ_ONLY
        pkt = self._send_command(CMD_GET_STATUS, bytes((req_flag,)), RSP_STATUS)
        try:
            return decode_status_payload(pkt.payload)
        except ValueError as exc:
            raise FeederI2CError(str(exc)) from exc

    def dispense_steps(self, steps: int, step_interval_us: Optional[int] = None) -> int:
        """Solicita mover el stepper la cantidad de pasos indicada (con signo)."""
        if isinstance(steps, bool) or not isinstance(steps, int) or steps == 0 or abs(steps) > MAX_ABS_STEPS:
            raise ValueError(f"steps debe ser entero no nulo en [-{MAX_ABS_STEPS}, {MAX_ABS_STEPS}]")

        payload = pack_i32(steps)
        if step_interval_us is not None:
            if (
                isinstance(step_interval_us, bool)
                or not isinstance(step_interval_us, int)
                or not (MIN_STEP_INTERVAL_US <= step_interval_us <= MAX_STEP_INTERVAL_US)
            ):
                raise ValueError(
                    f"step_interval_us debe estar entre {MIN_STEP_INTERVAL_US} y {MAX_STEP_INTERVAL_US}"
                )
            payload += pack_u16(step_interval_us)

        pkt = self._send_command(CMD_DISPENSE_STEPS, payload, RSP_DISPENSE_ACK)
        if len(pkt.payload) != 4:
            raise FeederI2CError(f"RSP_DISPENSE_ACK inválido: longitud {len(pkt.payload)}")
        return unpack_i32(pkt.payload, 0)

    def stop_motor(self) -> int:
        """Detiene el motor inmediatamente y retorna los pasos cancelados."""
        pkt = self._send_command(CMD_STOP_MOTOR, b"", RSP_STOP_ACK)
        if len(pkt.payload) != 4:
            raise FeederI2CError(f"RSP_STOP_ACK inválido: longitud {len(pkt.payload)}")
        return unpack_i32(pkt.payload, 0)

    def unjam(self, wiggle_steps: int = DEFAULT_UNJAM_STEPS) -> int:
        """Ejecuta secuencia de destrabe (reversa -> pausa -> avance)."""
        if (
            isinstance(wiggle_steps, bool)
            or not isinstance(wiggle_steps, int)
            or not (MIN_UNJAM_STEPS <= wiggle_steps <= MAX_UNJAM_STEPS)
        ):
            raise ValueError(f"wiggle_steps debe estar entre {MIN_UNJAM_STEPS} y {MAX_UNJAM_STEPS}")

        pkt = self._send_command(CMD_UNJAM, pack_u16(wiggle_steps), RSP_UNJAM_ACK)
        if len(pkt.payload) != 2:
            raise FeederI2CError(f"RSP_UNJAM_ACK inválido: longitud {len(pkt.payload)}")
        return unpack_u16(pkt.payload, 0)

    def set_config(self, manual_steps: int, step_interval_us: int) -> Tuple[int, int]:
        """Actualiza manual_steps y step_interval_us en Arduino y retorna los valores aplicados."""
        if (
            isinstance(manual_steps, bool)
            or not isinstance(manual_steps, int)
            or not (MIN_MANUAL_STEPS <= manual_steps <= MAX_MANUAL_STEPS)
        ):
            raise ValueError(f"manual_steps debe estar entre {MIN_MANUAL_STEPS} y {MAX_MANUAL_STEPS}")
        if (
            isinstance(step_interval_us, bool)
            or not isinstance(step_interval_us, int)
            or not (MIN_STEP_INTERVAL_US <= step_interval_us <= MAX_STEP_INTERVAL_US)
        ):
            raise ValueError(
                f"step_interval_us debe estar entre {MIN_STEP_INTERVAL_US} y {MAX_STEP_INTERVAL_US}"
            )

        payload = pack_u32(manual_steps) + pack_u16(step_interval_us)
        pkt = self._send_command(CMD_SET_CONFIG, payload, RSP_CONFIG_ACK)
        if len(pkt.payload) != 6:
            raise FeederI2CError(f"RSP_CONFIG_ACK inválido: longitud {len(pkt.payload)}")
        return unpack_u32(pkt.payload, 0), unpack_u16(pkt.payload, 4)

    def simulate_ir_detection(self, live_duration_s: float = 1.0) -> bool:
        """Inyecta una detección IR cuando el transporte activo es MockArduinoTransport."""
        if not isinstance(self.transport, MockArduinoTransport):
            raise RuntimeError("simulate_ir_detection solo está disponible en modo mock.")
        self.transport.simulate_ir_detection(live_duration_s=live_duration_s)
        return True

    def simulate_manual_button(self) -> bool:
        """Simula pulsación del botón manual físico cuando el transporte es MockArduinoTransport."""
        if not isinstance(self.transport, MockArduinoTransport):
            raise RuntimeError("simulate_manual_button solo está disponible en modo mock.")
        return self.transport.simulate_manual_button()
