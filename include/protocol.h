#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <stdbool.h>
#include <stdint.h>

// Framing [LEN][TYPE][PAYLOAD] (LEN = 2 + payloadLen) y direccion I2C (Contrato 2)
const uint8_t SLAVE_ADDR       = 0x08;
const uint8_t PROTOCOL_VERSION = 0x01;

// Comandos (RPi -> Arduino)
const uint8_t CMD_GET_STATUS     = 0x10;
const uint8_t CMD_DISPENSE_STEPS = 0x11;
const uint8_t CMD_STOP_MOTOR     = 0x12;
const uint8_t CMD_UNJAM          = 0x13;
const uint8_t CMD_SET_CONFIG     = 0x14;
const uint8_t CMD_PING           = 0x1F;

// Respuestas (Arduino -> RPi)
const uint8_t RSP_STATUS       = 0x90;
const uint8_t RSP_DISPENSE_ACK = 0x91;
const uint8_t RSP_STOP_ACK     = 0x92;
const uint8_t RSP_UNJAM_ACK    = 0x93;
const uint8_t RSP_CONFIG_ACK   = 0x94;
const uint8_t RSP_PONG         = 0x9F;
const uint8_t RSP_ERROR        = 0xFE;

// Bitmask status_flags (byte 0 de RSP_STATUS)
const uint8_t STATUS_MOTOR_BUSY              = 0x01;
const uint8_t STATUS_ULTRASONIC_LATCHED      = 0x02;
const uint8_t STATUS_ULTRASONIC_LIVE         = 0x04;
const uint8_t STATUS_MANUAL_DISPENSE_LATCHED = 0x08;

// Codigos de error (RSP_ERROR)
const uint8_t ERR_NONE          = 0x00;
const uint8_t ERR_BUSY          = 0x01;
const uint8_t ERR_INVALID_ARG   = 0x02;
const uint8_t ERR_UNKNOWN_CMD   = 0x03;
const uint8_t ERR_INVALID_FRAME = 0x04;

// Limites de parametros (Contrato 2)
const int32_t  DISPENSE_MAX_ABS_STEPS      = 204800L;
const uint16_t UNJAM_DEFAULT_STEPS         = 512U;
const uint16_t UNJAM_MIN_STEPS             = 64U;
const uint16_t UNJAM_MAX_STEPS             = 4096U;
const uint32_t CONFIG_MIN_MANUAL_STEPS     = 256UL;
const uint32_t CONFIG_MAX_MANUAL_STEPS     = 40960UL;
const uint16_t CONFIG_MIN_STEP_US          = 1000U;
const uint16_t CONFIG_MAX_STEP_US          = 10000U;
const uint16_t ULTRASONIC_NO_ECHO_CM       = 0xFFFFU;
const uint16_t CONFIG_DEFAULT_THRESHOLD_CM = 20U;
const uint16_t CONFIG_MIN_THRESHOLD_CM     = 5U;
const uint16_t CONFIG_MAX_THRESHOLD_CM     = 200U;

// Trama [LEN][TYPE][PAYLOAD]: LEN = 2 + payloadLen => max 14 bytes <= 32 B (buffer Wire.h)
const uint8_t PROTOCOL_MAX_PAYLOAD = 12;
const uint8_t PROTOCOL_MAX_FRAME   = 32;

struct DecodedPacket {
    uint8_t size;
    uint8_t type;
    uint8_t payload[PROTOCOL_MAX_PAYLOAD];
    uint8_t payloadLen;
    bool valid;
};

uint8_t empaquetarTrama(uint8_t type, const uint8_t* payload, uint8_t payloadLen, uint8_t* outBuf);
DecodedPacket decodificarTrama(const uint8_t* inBuf, uint8_t len);

// Helpers Big-Endian
static inline uint16_t proto_read_u16_be(const uint8_t* buf) {
    return ((uint16_t)buf[0] << 8) | (uint16_t)buf[1];
}

static inline uint32_t proto_read_u32_be(const uint8_t* buf) {
    return ((uint32_t)buf[0] << 24) |
           ((uint32_t)buf[1] << 16) |
           ((uint32_t)buf[2] << 8)  |
           (uint32_t)buf[3];
}

static inline int32_t proto_read_i32_be(const uint8_t* buf) {
    return (int32_t)proto_read_u32_be(buf);
}

static inline void proto_write_u16_be(uint8_t* buf, uint16_t val) {
    buf[0] = (uint8_t)((val >> 8) & 0xFFU);
    buf[1] = (uint8_t)(val & 0xFFU);
}

static inline void proto_write_u32_be(uint8_t* buf, uint32_t val) {
    buf[0] = (uint8_t)((val >> 24) & 0xFFUL);
    buf[1] = (uint8_t)((val >> 16) & 0xFFUL);
    buf[2] = (uint8_t)((val >> 8) & 0xFFUL);
    buf[3] = (uint8_t)(val & 0xFFUL);
}

static inline void proto_write_i32_be(uint8_t* buf, int32_t val) {
    proto_write_u32_be(buf, (uint32_t)val);
}

#endif
