"""Tests for Contract 2 binary I2C protocol, C++/Python constant parity, and mock transport."""

from pathlib import Path
import re
import sys
import unittest

RASPBERRY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = RASPBERRY_DIR.parent
if str(RASPBERRY_DIR) not in sys.path:
    sys.path.insert(0, str(RASPBERRY_DIR))

from i2c_master import protocol as proto
from i2c_master.controller import (
    FeederBusyError,
    FeederI2CError,
    FeederInvalidArgError,
    FeederMaster,
    FeederProtocolError,
)
from i2c_master.transport import MockArduinoTransport
from service.client_sync import ThreadSafeFeederMaster


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestProtocolFraming(unittest.TestCase):
    def test_round_trip_all_commands_and_responses(self) -> None:
        cases = [
            (proto.CMD_GET_STATUS, b""),
            (proto.CMD_GET_STATUS, bytes((proto.STATUS_REQ_CONSUME_LATCH,))),
            (proto.CMD_DISPENSE_STEPS, proto.pack_i32(4096)),
            (proto.CMD_DISPENSE_STEPS, proto.pack_i32(-204800) + proto.pack_u16(2500)),
            (proto.CMD_STOP_MOTOR, b""),
            (proto.CMD_UNJAM, b""),
            (proto.CMD_UNJAM, proto.pack_u16(512)),
            (proto.CMD_SET_CONFIG, proto.pack_u32(4096) + proto.pack_u16(2000)),
            (proto.CMD_PING, b""),
            (
                proto.RSP_STATUS,
                proto.encode_status_payload(
                    proto.FLAG_MOTOR_BUSY | proto.FLAG_IR_LATCHED | proto.FLAG_IR_LIVE,
                    -1024,
                    proto.ERR_NONE,
                ),
            ),
            (proto.RSP_DISPENSE_ACK, proto.pack_i32(8192)),
            (proto.RSP_STOP_ACK, proto.pack_i32(1234)),
            (proto.RSP_UNJAM_ACK, proto.pack_u16(512)),
            (proto.RSP_CONFIG_ACK, proto.pack_u32(4096) + proto.pack_u16(2000)),
            (proto.RSP_PONG, bytes((proto.PROTOCOL_VERSION,))),
            (proto.RSP_ERROR, proto.encode_error_payload(proto.CMD_DISPENSE_STEPS, proto.ERR_BUSY)),
        ]
        for msg_type, payload in cases:
            with self.subTest(msg_type=hex(msg_type), payload=payload.hex()):
                frame = proto.empaquetar_trama(msg_type, payload)
                expected_len = 2 + len(payload)
                self.assertEqual(len(frame), expected_len)
                self.assertEqual(frame[0], expected_len)
                self.assertEqual(frame[1], msg_type)
                self.assertEqual(frame[2:], payload)
                self.assertLessEqual(len(frame), proto.DEFAULT_RX_LEN)
                self.assertLessEqual(len(frame), proto.MAX_FRAME_BYTES)

                # Exact-length decode (Arduino Wire.onReceive semantics)
                decoded_exact = proto.decodificar_trama(frame, exact_len=True)
                self.assertTrue(decoded_exact.valid, decoded_exact.error)
                self.assertEqual(decoded_exact.type, msg_type)
                self.assertEqual(decoded_exact.payload, payload)
                self.assertEqual(decoded_exact.size, expected_len)

                # Simulate trailing 0xFF padding from Wire.requestFrom / I2C read
                padded = frame + b"\xff" * 8
                decoded = proto.decodificar_trama(padded)
                self.assertTrue(decoded.valid, decoded.error)
                self.assertEqual(decoded.type, msg_type)
                self.assertEqual(decoded.payload, payload)
                self.assertEqual(decoded.size, expected_len)

    def test_raw_special_bytes_pass_verbatim_without_escaping(self) -> None:
        raw_bytes = bytes((0x7E, 0x7F, 0x7D, 0x00, 0xFF))
        payload = raw_bytes * 2  # 10 bytes => LEN=12 <= DEFAULT_RX_LEN (16)
        frame = proto.empaquetar_trama(proto.RSP_STATUS, payload)

        self.assertEqual(len(frame), 2 + len(payload))
        self.assertEqual(frame[0], 2 + len(payload))
        self.assertEqual(frame[1], proto.RSP_STATUS)
        self.assertEqual(frame[2:], payload)
        self.assertLessEqual(len(frame), proto.DEFAULT_RX_LEN)
        self.assertLessEqual(len(frame), proto.MAX_FRAME_BYTES)

        decoded = proto.decodificar_trama(frame + b"\xff" * 4)
        self.assertTrue(decoded.valid, decoded.error)
        self.assertEqual(decoded.type, proto.RSP_STATUS)
        self.assertEqual(decoded.payload, payload)

        for byte_val in (0x7E, 0x7F, 0x7D, 0x00, 0xFF):
            f = proto.empaquetar_trama(byte_val, bytes((byte_val,)))
            self.assertEqual(f, bytes((3, byte_val, byte_val)))
            d = proto.decodificar_trama(f, exact_len=True)
            self.assertTrue(d.valid, d.error)
            self.assertEqual(d.type, byte_val)
            self.assertEqual(d.payload, bytes((byte_val,)))

    def test_malformed_and_invalid_len_frames_rejected(self) -> None:
        # Empty and < 2 bytes
        self.assertFalse(proto.decodificar_trama(b"").valid)
        self.assertFalse(proto.decodificar_trama(b"\x02").valid)

        # Bus stuck low (0x00) or floating high (0xFF)
        self.assertFalse(proto.decodificar_trama(b"\x00" * proto.DEFAULT_RX_LEN).valid)
        self.assertFalse(proto.decodificar_trama(b"\xff" * proto.DEFAULT_RX_LEN).valid)

        # LEN < 2
        self.assertFalse(proto.decodificar_trama(bytes((0, proto.CMD_PING))).valid)
        self.assertFalse(proto.decodificar_trama(bytes((1, proto.CMD_PING))).valid)

        # LEN > MAX_FRAME_BYTES (32)
        self.assertFalse(proto.decodificar_trama(bytes((33, proto.CMD_PING)) + b"\x00" * 31).valid)

        # Truncated frame (claims LEN=6, only 3 bytes received)
        self.assertFalse(proto.decodificar_trama(bytes((6, proto.CMD_PING, 0x01))).valid)

        # exact_len=True rejects trailing bytes (e.g. padding)
        valid_ping = proto.empaquetar_trama(proto.CMD_PING)
        self.assertTrue(proto.decodificar_trama(valid_ping + b"\xff", exact_len=False).valid)
        self.assertFalse(proto.decodificar_trama(valid_ping + b"\xff", exact_len=True).valid)
        self.assertFalse(proto.decodificar_trama(valid_ping + b"\x00", exact_len=True).valid)

        # Packing payload exceeding MAX_FRAME_BYTES raises ValueError
        with self.assertRaises(ValueError):
            proto.empaquetar_trama(proto.CMD_PING, b"\x00" * (proto.MAX_FRAME_BYTES - 1))

    def test_big_endian_and_payload_helpers(self) -> None:
        self.assertEqual(proto.unpack_i32(proto.pack_i32(-204800)), -204800)
        self.assertEqual(proto.unpack_i32(proto.pack_i32(204800)), 204800)
        self.assertEqual(proto.unpack_u32(proto.pack_u32(40960)), 40960)
        self.assertEqual(proto.unpack_u16(proto.pack_u16(10000)), 10000)

        flags = proto.FLAG_MOTOR_BUSY | proto.FLAG_MANUAL_DISPENSE_LATCHED
        raw = proto.encode_status_payload(flags, -512, proto.ERR_BUSY)
        st = proto.decode_status_payload(raw)
        self.assertTrue(st.motor_busy)
        self.assertFalse(st.ir_latched)
        self.assertFalse(st.ir_live)
        self.assertTrue(st.manual_dispense_latched)
        self.assertEqual(st.steps_remaining, -512)
        self.assertEqual(st.last_error, proto.ERR_BUSY)
        self.assertEqual(st.to_dict()["last_error_name"], "ERR_BUSY")

        with self.assertRaises(ValueError):
            proto.decode_status_payload(b"\x00\x01")
        with self.assertRaises(ValueError):
            proto.decode_error_payload(b"\x00")


class TestCppPythonProtocolParity(unittest.TestCase):
    """Single-source-of-truth parity check parsing include/protocol.h vs i2c_master/protocol.py."""

    @staticmethod
    def _parse_cpp_constants(header_path: Path) -> dict:
        text = header_path.read_text(encoding="utf-8")
        pattern = re.compile(
            r"const\s+(?:u?int(?:8|16|32)_t)\s+([A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)[UuLl]*\s*;"
        )
        return {name: int(val, 0) for name, val in pattern.findall(text)}

    def test_protocol_h_matches_python_constants(self) -> None:
        header = REPO_ROOT / "include" / "protocol.h"
        self.assertTrue(header.is_file(), f"Missing {header}")
        cpp = self._parse_cpp_constants(header)

        mapping = {
            "SLAVE_ADDR": proto.DEFAULT_SLAVE_ADDR,
            "PROTOCOL_VERSION": proto.PROTOCOL_VERSION,
            "CMD_GET_STATUS": proto.CMD_GET_STATUS,
            "CMD_DISPENSE_STEPS": proto.CMD_DISPENSE_STEPS,
            "CMD_STOP_MOTOR": proto.CMD_STOP_MOTOR,
            "CMD_UNJAM": proto.CMD_UNJAM,
            "CMD_SET_CONFIG": proto.CMD_SET_CONFIG,
            "CMD_PING": proto.CMD_PING,
            "RSP_STATUS": proto.RSP_STATUS,
            "RSP_DISPENSE_ACK": proto.RSP_DISPENSE_ACK,
            "RSP_STOP_ACK": proto.RSP_STOP_ACK,
            "RSP_UNJAM_ACK": proto.RSP_UNJAM_ACK,
            "RSP_CONFIG_ACK": proto.RSP_CONFIG_ACK,
            "RSP_PONG": proto.RSP_PONG,
            "RSP_ERROR": proto.RSP_ERROR,
            "STATUS_MOTOR_BUSY": proto.FLAG_MOTOR_BUSY,
            "STATUS_IR_LATCHED": proto.FLAG_IR_LATCHED,
            "STATUS_IR_LIVE": proto.FLAG_IR_LIVE,
            "STATUS_MANUAL_DISPENSE_LATCHED": proto.FLAG_MANUAL_DISPENSE_LATCHED,
            "ERR_NONE": proto.ERR_NONE,
            "ERR_BUSY": proto.ERR_BUSY,
            "ERR_INVALID_ARG": proto.ERR_INVALID_ARG,
            "ERR_UNKNOWN_CMD": proto.ERR_UNKNOWN_CMD,
            "ERR_INVALID_FRAME": proto.ERR_INVALID_FRAME,
            "DISPENSE_MAX_ABS_STEPS": proto.MAX_ABS_STEPS,
            "UNJAM_DEFAULT_STEPS": proto.DEFAULT_UNJAM_STEPS,
            "UNJAM_MIN_STEPS": proto.MIN_UNJAM_STEPS,
            "UNJAM_MAX_STEPS": proto.MAX_UNJAM_STEPS,
            "CONFIG_MIN_MANUAL_STEPS": proto.MIN_MANUAL_STEPS,
            "CONFIG_MAX_MANUAL_STEPS": proto.MAX_MANUAL_STEPS,
            "CONFIG_MIN_STEP_US": proto.MIN_STEP_INTERVAL_US,
            "CONFIG_MAX_STEP_US": proto.MAX_STEP_INTERVAL_US,
            "PROTOCOL_MAX_FRAME": proto.MAX_FRAME_BYTES,
        }

        for cpp_name, py_val in mapping.items():
            with self.subTest(constant=cpp_name):
                self.assertIn(cpp_name, cpp, f"{cpp_name} missing in include/protocol.h")
                self.assertEqual(cpp[cpp_name], py_val)


class TestMockTransportAndController(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(1000.0)
        self.transport = MockArduinoTransport(clock=self.clock)
        self.master = FeederMaster(transport=self.transport, delay=0.0)
        self.client = ThreadSafeFeederMaster(self.master)

    def test_ping_config_dispense_busy_stop_lifecycle(self) -> None:
        self.assertEqual(self.client.ping(), proto.PROTOCOL_VERSION)
        self.assertEqual(self.client.set_config(2048, 2000), (2048, 2000))

        # Dispense 1000 steps @ 2000us = 2.0s total
        self.assertEqual(self.client.dispense_steps(1000, step_interval_us=2000), 1000)
        st = self.client.get_status(consume_latch=False)
        self.assertTrue(st.motor_busy)
        self.assertEqual(st.steps_remaining, 1000)

        # Concurrent dispense/unjam rejected with FeederBusyError
        with self.assertRaises(FeederBusyError):
            self.client.dispense_steps(100)
        with self.assertRaises(FeederBusyError):
            self.client.unjam(256)

        # Advance 0.5s -> 250 steps done, 750 remaining; stop cancels 750
        self.clock.advance(0.5)
        cancelled = self.client.stop_motor()
        self.assertEqual(cancelled, 750)
        st_after = self.client.get_status(consume_latch=False)
        self.assertFalse(st_after.motor_busy)
        self.assertEqual(st_after.steps_remaining, 0)

    def test_default_constants(self) -> None:
        self.assertEqual(proto.DEFAULT_STEP_INTERVAL_US, 1000)
        self.assertEqual(proto.STEPS_PER_REVOLUTION, 4096)

    def test_unjam_three_phase_sequence(self) -> None:
        # 100 steps @ 1000us (default) = 0.1s rev + 0.15s pause + 0.1s fwd = 0.35s total
        self.assertEqual(self.client.unjam(100), 100)

        # Phase 1: reverse (negative remaining) at t=0.05s
        self.clock.advance(0.05)
        st_rev = self.client.get_status(consume_latch=False)
        self.assertTrue(st_rev.motor_busy)
        self.assertLess(st_rev.steps_remaining, 0)

        # Phase 2: pause (still busy, 0 remaining) at t=0.15s
        self.clock.advance(0.10)
        st_pause = self.client.get_status(consume_latch=False)
        self.assertTrue(st_pause.motor_busy)
        self.assertEqual(st_pause.steps_remaining, 0)

        # Phase 3: forward (positive remaining) at t=0.30s
        self.clock.advance(0.15)
        st_fwd = self.client.get_status(consume_latch=False)
        self.assertTrue(st_fwd.motor_busy)
        self.assertGreater(st_fwd.steps_remaining, 0)

        # Done at t=0.40s
        self.clock.advance(0.10)
        st_idle = self.client.get_status(consume_latch=False)
        self.assertFalse(st_idle.motor_busy)
        self.assertEqual(st_idle.steps_remaining, 0)

    def test_ir_and_manual_button_latches(self) -> None:
        self.client.simulate_ir_detection(live_duration_s=1.0)
        self.assertTrue(self.client.simulate_manual_button())

        # Read without consuming keeps latches set
        st1 = self.client.get_status(consume_latch=False)
        self.assertTrue(st1.ir_latched)
        self.assertTrue(st1.ir_live)
        self.assertTrue(st1.manual_dispense_latched)
        self.assertTrue(st1.motor_busy)

        # Consume clears latches
        st2 = self.client.get_status(consume_latch=True)
        self.assertTrue(st2.ir_latched)
        self.assertTrue(st2.manual_dispense_latched)

        st3 = self.client.get_status(consume_latch=False)
        self.assertFalse(st3.ir_latched)
        self.assertFalse(st3.manual_dispense_latched)

        # IR live drops after live_duration_s
        self.clock.advance(1.5)
        st4 = self.client.get_status(consume_latch=False)
        self.assertFalse(st4.ir_live)

    def test_protocol_errors_from_mock(self) -> None:
        # Unknown command
        raw = self.transport.send_and_receive(proto.empaquetar_trama(0x55))
        dec = proto.decodificar_trama(raw)
        self.assertTrue(dec.valid)
        self.assertEqual(dec.type, proto.RSP_ERROR)
        self.assertEqual(proto.decode_error_payload(dec.payload), (0x55, proto.ERR_UNKNOWN_CMD))

        # Invalid frame
        raw_bad = self.transport.send_and_receive(b"\x01\x02\x03")
        dec_bad = proto.decodificar_trama(raw_bad)
        self.assertTrue(dec_bad.valid)
        self.assertEqual(dec_bad.type, proto.RSP_ERROR)
        self.assertEqual(proto.decode_error_payload(dec_bad.payload), (0x00, proto.ERR_INVALID_FRAME))

        # Trailing bytes on TX rejected (matches Arduino Wire.onReceive exact length check)
        raw_trailing = self.transport.send_and_receive(proto.empaquetar_trama(proto.CMD_PING) + b"\xff")
        dec_trailing = proto.decodificar_trama(raw_trailing)
        self.assertTrue(dec_trailing.valid)
        self.assertEqual(dec_trailing.type, proto.RSP_ERROR)
        self.assertEqual(proto.decode_error_payload(dec_trailing.payload), (0x00, proto.ERR_INVALID_FRAME))

        # Controller local validation
        with self.assertRaises(ValueError):
            self.client.dispense_steps(0)
        with self.assertRaises(ValueError):
            self.client.unjam(10)
        with self.assertRaises(ValueError):
            self.client.set_config(100, 2000)

        # Protocol-level invalid arg raised as FeederInvalidArgError
        with self.assertRaises(FeederInvalidArgError):
            self.master._send_command(proto.CMD_DISPENSE_STEPS, proto.pack_i32(0), proto.RSP_DISPENSE_ACK)
        with self.assertRaises(FeederProtocolError):
            self.master._send_command(0x55, b"", proto.RSP_PONG)


if __name__ == "__main__":
    unittest.main()
