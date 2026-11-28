"""Protocolo binario I2C (Contrato 2) — framing [LEN][TYPE][PAYLOAD] y empaquetado Big-Endian."""

from dataclasses import dataclass
import struct
from typing import Dict, Optional, Tuple

DEFAULT_SLAVE_ADDR: int = 0x08
DEFAULT_I2C_BUS: int = 1
DEFAULT_RX_LEN: int = 16
DEFAULT_DELAY_S: float = 0.04
PROTOCOL_VERSION: int = 0x01
MAX_FRAME_BYTES: int = 32

# Comandos (RPi -> Arduino)
CMD_GET_STATUS: int = 0x10
CMD_DISPENSE_STEPS: int = 0x11
CMD_STOP_MOTOR: int = 0x12
CMD_UNJAM: int = 0x13
CMD_SET_CONFIG: int = 0x14
CMD_PING: int = 0x1F

# Respuestas (Arduino -> RPi)
RSP_STATUS: int = 0x90
RSP_DISPENSE_ACK: int = 0x91
RSP_STOP_ACK: int = 0x92
RSP_UNJAM_ACK: int = 0x93
RSP_CONFIG_ACK: int = 0x94
RSP_PONG: int = 0x9F
RSP_ERROR: int = 0xFE

# Flags de solicitud para CMD_GET_STATUS
STATUS_REQ_READ_ONLY: int = 0x00
STATUS_REQ_CONSUME_LATCH: int = 0x01

# Bitmask status_flags (byte 0 de RSP_STATUS)
FLAG_MOTOR_BUSY: int = 0x01
FLAG_ULTRASONIC_LATCHED: int = 0x02
FLAG_ULTRASONIC_LIVE: int = 0x04
FLAG_MANUAL_DISPENSE_LATCHED: int = 0x08

# Códigos de error (RSP_ERROR)
ERR_NONE: int = 0x00
ERR_BUSY: int = 0x01
ERR_INVALID_ARG: int = 0x02
ERR_UNKNOWN_CMD: int = 0x03
ERR_INVALID_FRAME: int = 0x04

# Rangos válidos del contrato
STEPS_PER_REVOLUTION: int = 4096
MIN_ABS_STEPS: int = 1
MAX_ABS_STEPS: int = 204800
DEFAULT_UNJAM_STEPS: int = 512
MIN_UNJAM_STEPS: int = 64
MAX_UNJAM_STEPS: int = 4096
DEFAULT_MANUAL_STEPS: int = 4096
MIN_MANUAL_STEPS: int = 256
MAX_MANUAL_STEPS: int = 40960
DEFAULT_STEP_INTERVAL_US: int = 1000
MIN_STEP_INTERVAL_US: int = 1000
MAX_STEP_INTERVAL_US: int = 10000
ULTRASONIC_NO_ECHO_CM: int = 0xFFFF
DEFAULT_ULTRASONIC_THRESHOLD_CM: int = 20
MIN_ULTRASONIC_THRESHOLD_CM: int = 5
MAX_ULTRASONIC_THRESHOLD_CM: int = 200

CMD_NAMES: Dict[int, str] = {
    CMD_GET_STATUS: "CMD_GET_STATUS",
    CMD_DISPENSE_STEPS: "CMD_DISPENSE_STEPS",
    CMD_STOP_MOTOR: "CMD_STOP_MOTOR",
    CMD_UNJAM: "CMD_UNJAM",
    CMD_SET_CONFIG: "CMD_SET_CONFIG",
    CMD_PING: "CMD_PING",
}

RSP_NAMES: Dict[int, str] = {
    RSP_STATUS: "RSP_STATUS",
    RSP_DISPENSE_ACK: "RSP_DISPENSE_ACK",
    RSP_STOP_ACK: "RSP_STOP_ACK",
    RSP_UNJAM_ACK: "RSP_UNJAM_ACK",
    RSP_CONFIG_ACK: "RSP_CONFIG_ACK",
    RSP_PONG: "RSP_PONG",
    RSP_ERROR: "RSP_ERROR",
}

ERROR_NAMES: Dict[int, str] = {
    ERR_NONE: "ERR_NONE",
    ERR_BUSY: "ERR_BUSY",
    ERR_INVALID_ARG: "ERR_INVALID_ARG",
    ERR_UNKNOWN_CMD: "ERR_UNKNOWN_CMD",
    ERR_INVALID_FRAME: "ERR_INVALID_FRAME",
}


@dataclass
class DecodedPacket:
    """Trama decodificada y validada."""

    size: int
    type: int
    payload: bytes
    valid: bool
    error: str = ""

    @property
    def type_name(self) -> str:
        return RSP_NAMES.get(self.type, CMD_NAMES.get(self.type, f"0x{self.type:02X}"))


@dataclass(frozen=True)
class StatusData:
    """Estado estructurado recibido en RSP_STATUS (8 bytes)."""

    motor_busy: bool
    ultrasonic_latched: bool
    ultrasonic_live: bool
    manual_dispense_latched: bool
    steps_remaining: int
    last_error: int
    distance_cm: Optional[int]
    raw_flags: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "motor_busy": self.motor_busy,
            "ultrasonic_latched": self.ultrasonic_latched,
            "ultrasonic_live": self.ultrasonic_live,
            "manual_dispense_latched": self.manual_dispense_latched,
            "steps_remaining": self.steps_remaining,
            "last_error": self.last_error,
            "last_error_name": ERROR_NAMES.get(self.last_error, f"0x{self.last_error:02X}"),
            "distance_cm": self.distance_cm,
            "raw_flags": self.raw_flags,
        }


def pack_i32(value: int) -> bytes:
    return struct.pack(">i", value)


def unpack_i32(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">i", data, offset)[0]


def pack_u32(value: int) -> bytes:
    return struct.pack(">I", value)


def unpack_u32(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def pack_u16(value: int) -> bytes:
    return struct.pack(">H", value)


def unpack_u16(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def encode_status_payload(
    flags: int,
    steps_remaining: int,
    last_error: int,
    distance_cm: Optional[int] = None,
) -> bytes:
    raw_distance = ULTRASONIC_NO_ECHO_CM if distance_cm is None else (int(distance_cm) & 0xFFFF)
    return struct.pack(">BiBH", flags & 0xFF, steps_remaining, last_error & 0xFF, raw_distance)


def decode_status_payload(payload: bytes) -> StatusData:
    if len(payload) != 8:
        raise ValueError(f"Payload RSP_STATUS inválido: longitud {len(payload)}, esperada 8")
    flags, steps_remaining, last_error, raw_distance = struct.unpack(">BiBH", payload)
    return StatusData(
        motor_busy=bool(flags & FLAG_MOTOR_BUSY),
        ultrasonic_latched=bool(flags & FLAG_ULTRASONIC_LATCHED),
        ultrasonic_live=bool(flags & FLAG_ULTRASONIC_LIVE),
        manual_dispense_latched=bool(flags & FLAG_MANUAL_DISPENSE_LATCHED),
        steps_remaining=steps_remaining,
        last_error=last_error,
        distance_cm=None if raw_distance == ULTRASONIC_NO_ECHO_CM else raw_distance,
        raw_flags=flags,
    )


def encode_error_payload(failed_cmd: int, error_code: int) -> bytes:
    return bytes((failed_cmd & 0xFF, error_code & 0xFF))


def decode_error_payload(payload: bytes) -> Tuple[int, int]:
    if len(payload) != 2:
        raise ValueError(f"Payload RSP_ERROR inválido: longitud {len(payload)}, esperada 2")
    return payload[0], payload[1]


def empaquetar_trama(msg_type: int, payload: bytes = b"") -> bytes:
    """Construye trama [LEN][TYPE][PAYLOAD] donde LEN = 2 + len(payload)."""
    if not (0 <= msg_type <= 0xFF):
        raise ValueError(f"msg_type fuera de rango de byte: {msg_type}")

    payload_bytes = bytes(payload)
    frame_len = 2 + len(payload_bytes)
    if frame_len > MAX_FRAME_BYTES:
        raise ValueError(f"Payload demasiado grande: LEN={frame_len} > {MAX_FRAME_BYTES}")

    return bytes((frame_len, msg_type)) + payload_bytes


def decodificar_trama(in_buf: bytes, exact_len: bool = False) -> DecodedPacket:
    """Decodifica trama I2C [LEN][TYPE][PAYLOAD] validando LEN y longitud del buffer."""
    if len(in_buf) < 2:
        return DecodedPacket(0, 0, b"", False, "Trama demasiado corta (< 2 bytes)")

    frame_len = in_buf[0]
    if not (2 <= frame_len <= MAX_FRAME_BYTES):
        return DecodedPacket(
            frame_len,
            in_buf[1],
            b"",
            False,
            f"Longitud de trama inválida: LEN={frame_len} (válido 2..{MAX_FRAME_BYTES})",
        )

    if exact_len:
        if len(in_buf) != frame_len:
            return DecodedPacket(
                frame_len,
                in_buf[1],
                b"",
                False,
                f"Longitud exacta requerida: LEN={frame_len}, recibidos={len(in_buf)}",
            )
    elif len(in_buf) < frame_len:
        return DecodedPacket(
            frame_len,
            in_buf[1],
            b"",
            False,
            f"Trama truncada: LEN={frame_len}, recibidos={len(in_buf)}",
        )

    return DecodedPacket(frame_len, in_buf[1], bytes(in_buf[2:frame_len]), True)
