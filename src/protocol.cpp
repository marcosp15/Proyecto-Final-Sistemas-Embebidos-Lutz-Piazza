#include "protocol.h"

uint8_t empaquetarTrama(uint8_t type, const uint8_t* payload, uint8_t payloadLen, uint8_t* outBuf) {
    if (outBuf == nullptr || payloadLen > PROTOCOL_MAX_PAYLOAD || (payloadLen > 0 && payload == nullptr)) {
        return 0;
    }

    uint8_t frameLen = (uint8_t)(2U + payloadLen);
    outBuf[0] = frameLen;
    outBuf[1] = type;

    for (uint8_t i = 0; i < payloadLen; i++) {
        outBuf[2 + i] = payload[i];
    }

    return frameLen;
}

DecodedPacket decodificarTrama(const uint8_t* inBuf, uint8_t len) {
    DecodedPacket pkt;
    pkt.size = 0;
    pkt.type = 0;
    pkt.payloadLen = 0;
    pkt.valid = false;

    if (inBuf == nullptr || len < 2 || len > (uint8_t)(2U + PROTOCOL_MAX_PAYLOAD) || inBuf[0] != len) {
        return pkt;
    }

    pkt.size = len;
    pkt.type = inBuf[1];
    pkt.payloadLen = (uint8_t)(len - 2U);

    for (uint8_t i = 0; i < pkt.payloadLen; i++) {
        pkt.payload[i] = inBuf[2 + i];
    }

    pkt.valid = true;
    return pkt;
}
