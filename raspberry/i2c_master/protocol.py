"""Protocolo binario I2C (Contrato 2) — framing, byte stuffing y empaquetado Big-Endian."""

from dataclasses import dataclass
import struct
from typing import Dict, Tuple

DEFAULT_SLAVE_ADDR: int = 0x08
DEFAULT_I2C_BUS: int = 1
DEFAULT_RX_LEN: int = 24
DEFAULT_DELAY_S: float = 0.04
PROTOCOL_VERSION: int = 0x01
MAX_FRAME_BYTES: int = 32

# Delimitadores de trama y byte stuffing
START_BYTE: int = 0x7E
END_BYTE: int = 0x7F
ESC_BYTE: int = 0x7D
ESC_XOR: int = 0x20
RESERVED_BYTES = frozenset({START_BYTE, END_BYTE, ESC_BYTE})

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
FLAG_IR_LATCHED: int = 0x02
FLAG_IR_LIVE: int = 0x04
FLAG_MANUAL_DISPENSE_LATCHED: int = 0x08

# Códigos de error (RSP_ERROR)
ERR_NONE: int = 0x00
ERR_BUSY: int = 0x01
ERR_INVALID_ARG: int = 0x02
ERR_UNKNOWN_CMD: int = 0x03
ERR_INVALID_FRAME: int = 0x04

# Rangos válidos del contrato
MIN_ABS_STEPS: int = 1
MAX_ABS_STEPS: int = 204800
DEFAULT_UNJAM_STEPS: int = 512
MIN_UNJAM_STEPS: int = 64
MAX_UNJAM_STEPS: int = 4096
DEFAULT_MANUAL_STEPS: int = 4096
MIN_MANUAL_STEPS: int = 256
MAX_MANUAL_STEPS: int = 40960
DEFAULT_STEP_INTERVAL_US: int = 2000
MIN_STEP_INTERVAL_US: int = 1000
MAX_STEP_INTERVAL_US: int = 10000

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
    """Estado estructurado recibido en RSP_STATUS (6 bytes)."""

    motor_busy: bool
    ir_latched: bool
    ir_live: bool
    manual_dispense_latched: bool
    steps_remaining: int
    last_error: int
    raw_flags: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "motor_busy": self.motor_busy,
            "ir_latched": self.ir_latched,
            "ir_live": self.ir_live,
            "manual_dispense_latched": self.manual_dispense_latched,
            "steps_remaining": self.steps_remaining,
            "last_error": self.last_error,
            "last_error_name": ERROR_NAMES.get(self.last_error, f"0x{self.last_error:02X}"),
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


def encode_status_payload(flags: int, steps_remaining: int, last_error: int) -> bytes:
    return struct.pack(">BiB", flags & 0xFF, steps_remaining, last_error & 0xFF)


def decode_status_payload(payload: bytes) -> StatusData:
    if len(payload) != 6:
        raise ValueError(f"Payload RSP_STATUS inválido: longitud {len(payload)}, esperada 6")
    flags, steps_remaining, last_error = struct.unpack(">BiB", payload)
    return StatusData(
        motor_busy=bool(flags & FLAG_MOTOR_BUSY),
        ir_latched=bool(flags & FLAG_IR_LATCHED),
        ir_live=bool(flags & FLAG_IR_LIVE),
        manual_dispense_latched=bool(flags & FLAG_MANUAL_DISPENSE_LATCHED),
        steps_remaining=steps_remaining,
        last_error=last_error,
        raw_flags=flags,
    )


def encode_error_payload(failed_cmd: int, error_code: int) -> bytes:
    return bytes((failed_cmd & 0xFF, error_code & 0xFF))


def decode_error_payload(payload: bytes) -> Tuple[int, int]:
    if len(payload) != 2:
        raise ValueError(f"Payload RSP_ERROR inválido: longitud {len(payload)}, esperada 2")
    return payload[0], payload[1]


def empaquetar_trama(msg_type: int, payload: bytes = b"") -> bytes:
    """Construye trama [START][SIZE][TYPE][PAYLOAD][END] con byte stuffing."""
    if not (0 <= msg_type <= 0xFF):
        raise ValueError(f"msg_type fuera de rango de byte: {msg_type}")

    payload_bytes = bytes(payload)
    logical_size = 4 + len(payload_bytes)
    if logical_size > 0xFF:
        raise ValueError(f"Payload demasiado grande: SIZE={logical_size}")

    out = bytearray((START_BYTE,))

    def append_escaped(b: int) -> None:
        if b in RESERVED_BYTES:
            out.append(ESC_BYTE)
            out.append(b ^ ESC_XOR)
        else:
            out.append(b)

    append_escaped(logical_size)
    append_escaped(msg_type)
    for b in payload_bytes:
        append_escaped(b)

    out.append(END_BYTE)
    return bytes(out)


def decodificar_trama(in_buf: bytes) -> DecodedPacket:
    """Decodifica trama I2C validando delimitadores, escapes y SIZE == 4 + len(payload)."""
    raw = bytearray()
    escape_active = False
    in_frame = False
    found_end = False

    for b in in_buf:
        if b == START_BYTE:
            raw.clear()
            escape_active = False
            in_frame = True
            continue

        if not in_frame:
            continue

        if b == END_BYTE:
            found_end = True
            break

        if b == ESC_BYTE:
            if escape_active:
                return DecodedPacket(0, 0, b"", False, "Secuencia de escape doble inválida")
            escape_active = True
            continue

        if escape_active:
            b ^= ESC_XOR
            escape_active = False

        raw.append(b)

    if not in_frame:
        return DecodedPacket(0, 0, b"", False, "No se encontró delimitador de inicio (0x7E)")

    if not found_end:
        return DecodedPacket(0, 0, b"", False, "No se encontró delimitador de fin (0x7F)")

    if escape_active:
        return DecodedPacket(0, 0, b"", False, "Secuencia de escape incompleta antes de END")

    if len(raw) < 2:
        return DecodedPacket(0, 0, b"", False, "Trama demasiado corta (faltan SIZE o TYPE)")

    logical_size = raw[0]
    msg_type = raw[1]
    payload = bytes(raw[2:])
    expected_size = 4 + len(payload)

    if logical_size != expected_size:
        return DecodedPacket(
            logical_size,
            msg_type,
            payload,
            False,
            f"Error de longitud: SIZE={logical_size}, esperado={expected_size}",
        )

    return DecodedPacket(logical_size, msg_type, payload, True)
