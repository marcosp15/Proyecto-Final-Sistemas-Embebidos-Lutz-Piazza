#include "protocol.h"

static void append_escaped(uint8_t byte, uint8_t* outBuf, uint8_t& idx) {
    if (byte == START_BYTE || byte == END_BYTE || byte == ESC_BYTE) {
        outBuf[idx++] = ESC_BYTE;
        outBuf[idx++] = (uint8_t)(byte ^ ESC_XOR);
    } else {
        outBuf[idx++] = byte;
    }
}

uint8_t empaquetarTrama(uint8_t type, const uint8_t* payload, uint8_t payloadLen, uint8_t* outBuf) {
    if (outBuf == nullptr || payloadLen > PROTOCOL_MAX_PAYLOAD || (payloadLen > 0 && payload == nullptr)) {
        return 0;
    }

    uint8_t logicalSize = (uint8_t)(4U + payloadLen);
    uint8_t idx = 0;

    outBuf[idx++] = START_BYTE;
    append_escaped(logicalSize, outBuf, idx);
    append_escaped(type, outBuf, idx);

    for (uint8_t i = 0; i < payloadLen; i++) {
        append_escaped(payload[i], outBuf, idx);
    }

    outBuf[idx++] = END_BYTE;
    return idx;
}

DecodedPacket decodificarTrama(const uint8_t* inBuf, uint8_t len) {
    DecodedPacket pkt;
    pkt.size = 0;
    pkt.type = 0;
    pkt.payloadLen = 0;
    pkt.valid = false;

    if (inBuf == nullptr || len < 4) {
        return pkt;
    }

    uint8_t raw[2 + PROTOCOL_MAX_PAYLOAD];
    uint8_t rawIdx = 0;
    bool escapeActivo = false;
    bool enTrama = false;
    bool finTrama = false;
    bool overflow = false;

    for (uint8_t i = 0; i < len; i++) {
        uint8_t b = inBuf[i];

        if (b == START_BYTE) {
            rawIdx = 0;
            escapeActivo = false;
            enTrama = true;
            overflow = false;
            continue;
        }

        if (!enTrama) {
            continue;
        }

        if (b == END_BYTE) {
            finTrama = true;
            break;
        }

        if (b == ESC_BYTE) {
            if (escapeActivo) {
                return pkt;
            }
            escapeActivo = true;
            continue;
        }

        if (escapeActivo) {
            b ^= ESC_XOR;
            escapeActivo = false;
        }

        if (rawIdx < sizeof(raw)) {
            raw[rawIdx++] = b;
        } else {
            overflow = true;
        }
    }

    if (!finTrama || escapeActivo || overflow || rawIdx < 2) {
        return pkt;
    }

    pkt.size = raw[0];
    pkt.type = raw[1];
    pkt.payloadLen = (uint8_t)(rawIdx - 2);

    if (pkt.size != (uint8_t)(4U + pkt.payloadLen)) {
        return pkt;
    }

    for (uint8_t i = 0; i < pkt.payloadLen; i++) {
        pkt.payload[i] = raw[2 + i];
    }

    pkt.valid = true;
    return pkt;
}
