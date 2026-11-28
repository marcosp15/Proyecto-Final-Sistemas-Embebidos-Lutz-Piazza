#include "i2c_handlers.h"
#include "protocol.h"
#include "stepper_motor.h"
#include "ultrasonic_sensor.h"
#include <Wire.h>
#include <avr/io.h>
#include <util/atomic.h>

struct I2CMailbox {
    bool pending_stop;
    bool pending_config;
    bool pending_dispense;
    bool pending_unjam;
    int32_t dispense_steps;
    bool has_dispense_interval;
    uint16_t dispense_interval_us;
    uint16_t unjam_steps;
    uint32_t cfg_manual_steps;
    uint16_t cfg_step_interval_us;
    uint16_t cfg_ultrasonic_threshold_cm;
};

static volatile I2CMailbox s_mailbox = {};
static volatile uint32_t s_manual_steps = STEPPER_STEPS_PER_REV;
static volatile bool s_manual_dispense_latched = false;
static volatile uint8_t s_last_error = ERR_NONE;

static volatile uint8_t s_tx_buf[PROTOCOL_MAX_FRAME];
static volatile uint8_t s_tx_len = 0;

static void send_error_reply(uint8_t failed_cmd, uint8_t err_code) {
    s_last_error = err_code;
    uint8_t payload[2] = {failed_cmd, err_code};
    s_tx_len = empaquetarTrama(RSP_ERROR, payload, sizeof(payload), (uint8_t*)s_tx_buf);
}

static bool motor_busy_isr_unsafe(void) {
    if (s_mailbox.pending_dispense || s_mailbox.pending_unjam) {
        return true;
    }
    if (s_mailbox.pending_stop) {
        return false;
    }
    return stepper_is_busy();
}

static int32_t steps_remaining_isr_unsafe(void) {
    if (s_mailbox.pending_dispense) {
        return s_mailbox.dispense_steps;
    }
    if (s_mailbox.pending_unjam) {
        return -(int32_t)s_mailbox.unjam_steps;
    }
    if (s_mailbox.pending_stop) {
        return 0;
    }
    return stepper_steps_remaining();
}

void initI2C(void) {
    Wire.begin(SLAVE_ADDR);

    // Desactivar pull-ups internos de 5 V en PC4 (SDA/A4) y PC5 (SCL/A5) para compatibilidad 3.3 V RPi
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        DDRC &= ~((1 << PC4) | (1 << PC5));
        PORTC &= ~((1 << PC4) | (1 << PC5));
    }

    Wire.onReceive(onReceiveHandler);
    Wire.onRequest(onRequestHandler);
}

void onReceiveHandler(int numBytes) {
    (void)numBytes;

    uint8_t inBuf[PROTOCOL_MAX_FRAME];
    uint8_t idx = 0;
    while (Wire.available() && idx < PROTOCOL_MAX_FRAME) {
        inBuf[idx++] = (uint8_t)Wire.read();
    }
    // Drenar excedente si existiera para no dejar bytes residuales en TWI
    while (Wire.available()) {
        (void)Wire.read();
    }

    DecodedPacket pkt = decodificarTrama(inBuf, idx);
    if (!pkt.valid) {
        send_error_reply(0x00, ERR_INVALID_FRAME);
        return;
    }

    switch (pkt.type) {
        case CMD_GET_STATUS: {
            if (pkt.payloadLen > 1) {
                send_error_reply(CMD_GET_STATUS, ERR_INVALID_ARG);
                return;
            }
            uint8_t reqFlags = (pkt.payloadLen == 1) ? pkt.payload[0] : 0x00;
            bool consume = (reqFlags & 0x01U) != 0;

            uint8_t statusFlags = 0;
            if (motor_busy_isr_unsafe()) {
                statusFlags |= STATUS_MOTOR_BUSY;
            }
            bool ultrasonicLatched = consume ? ultrasonic_sensor_consume_latched() : ultrasonic_sensor_is_latched();
            if (ultrasonicLatched) {
                statusFlags |= STATUS_ULTRASONIC_LATCHED;
            }
            if (ultrasonic_sensor_is_live()) {
                statusFlags |= STATUS_ULTRASONIC_LIVE;
            }
            bool manualLatched = s_manual_dispense_latched;
            if (consume) {
                s_manual_dispense_latched = false;
            }
            if (manualLatched) {
                statusFlags |= STATUS_MANUAL_DISPENSE_LATCHED;
            }

            uint8_t rsp[8];
            rsp[0] = statusFlags;
            proto_write_i32_be(&rsp[1], steps_remaining_isr_unsafe());
            rsp[5] = s_last_error;
            proto_write_u16_be(&rsp[6], ultrasonic_sensor_get_distance_cm());
            s_tx_len = empaquetarTrama(RSP_STATUS, rsp, sizeof(rsp), (uint8_t*)s_tx_buf);
            break;
        }

        case CMD_DISPENSE_STEPS: {
            if (pkt.payloadLen != 4 && pkt.payloadLen != 6) {
                send_error_reply(CMD_DISPENSE_STEPS, ERR_INVALID_ARG);
                return;
            }
            int32_t steps = proto_read_i32_be(&pkt.payload[0]);
            if (steps == 0 || steps < -DISPENSE_MAX_ABS_STEPS || steps > DISPENSE_MAX_ABS_STEPS) {
                send_error_reply(CMD_DISPENSE_STEPS, ERR_INVALID_ARG);
                return;
            }

            bool hasInterval = (pkt.payloadLen == 6);
            uint16_t intervalUs = 0;
            if (hasInterval) {
                intervalUs = proto_read_u16_be(&pkt.payload[4]);
                if (intervalUs < CONFIG_MIN_STEP_US || intervalUs > CONFIG_MAX_STEP_US) {
                    send_error_reply(CMD_DISPENSE_STEPS, ERR_INVALID_ARG);
                    return;
                }
            }

            if (motor_busy_isr_unsafe()) {
                send_error_reply(CMD_DISPENSE_STEPS, ERR_BUSY);
                return;
            }

            s_mailbox.dispense_steps = steps;
            s_mailbox.has_dispense_interval = hasInterval;
            s_mailbox.dispense_interval_us = intervalUs;
            s_mailbox.pending_dispense = true;
            s_last_error = ERR_NONE;

            uint8_t rsp[4];
            proto_write_i32_be(rsp, steps);
            s_tx_len = empaquetarTrama(RSP_DISPENSE_ACK, rsp, sizeof(rsp), (uint8_t*)s_tx_buf);
            break;
        }

        case CMD_STOP_MOTOR: {
            if (pkt.payloadLen != 0) {
                send_error_reply(CMD_STOP_MOTOR, ERR_INVALID_ARG);
                return;
            }

            int32_t cancelled = steps_remaining_isr_unsafe();
            s_mailbox.pending_dispense = false;
            s_mailbox.pending_unjam = false;
            s_mailbox.pending_stop = true;
            s_last_error = ERR_NONE;

            uint8_t rsp[4];
            proto_write_i32_be(rsp, cancelled);
            s_tx_len = empaquetarTrama(RSP_STOP_ACK, rsp, sizeof(rsp), (uint8_t*)s_tx_buf);
            break;
        }

        case CMD_UNJAM: {
            if (pkt.payloadLen != 0 && pkt.payloadLen != 2) {
                send_error_reply(CMD_UNJAM, ERR_INVALID_ARG);
                return;
            }

            uint16_t wiggle = (pkt.payloadLen == 2) ? proto_read_u16_be(&pkt.payload[0]) : UNJAM_DEFAULT_STEPS;
            if (wiggle < UNJAM_MIN_STEPS || wiggle > UNJAM_MAX_STEPS) {
                send_error_reply(CMD_UNJAM, ERR_INVALID_ARG);
                return;
            }

            if (motor_busy_isr_unsafe()) {
                send_error_reply(CMD_UNJAM, ERR_BUSY);
                return;
            }

            s_mailbox.unjam_steps = wiggle;
            s_mailbox.pending_unjam = true;
            s_last_error = ERR_NONE;

            uint8_t rsp[2];
            proto_write_u16_be(rsp, wiggle);
            s_tx_len = empaquetarTrama(RSP_UNJAM_ACK, rsp, sizeof(rsp), (uint8_t*)s_tx_buf);
            break;
        }

        case CMD_SET_CONFIG: {
            if (pkt.payloadLen != 8) {
                send_error_reply(CMD_SET_CONFIG, ERR_INVALID_ARG);
                return;
            }

            uint32_t manualSteps = proto_read_u32_be(&pkt.payload[0]);
            uint16_t stepIntervalUs = proto_read_u16_be(&pkt.payload[4]);
            uint16_t thresholdCm = proto_read_u16_be(&pkt.payload[6]);
            if (manualSteps < CONFIG_MIN_MANUAL_STEPS || manualSteps > CONFIG_MAX_MANUAL_STEPS ||
                stepIntervalUs < CONFIG_MIN_STEP_US || stepIntervalUs > CONFIG_MAX_STEP_US ||
                thresholdCm < CONFIG_MIN_THRESHOLD_CM || thresholdCm > CONFIG_MAX_THRESHOLD_CM) {
                send_error_reply(CMD_SET_CONFIG, ERR_INVALID_ARG);
                return;
            }

            s_mailbox.cfg_manual_steps = manualSteps;
            s_mailbox.cfg_step_interval_us = stepIntervalUs;
            s_mailbox.cfg_ultrasonic_threshold_cm = thresholdCm;
            s_mailbox.pending_config = true;
            s_last_error = ERR_NONE;

            uint8_t rsp[8];
            proto_write_u32_be(&rsp[0], manualSteps);
            proto_write_u16_be(&rsp[4], stepIntervalUs);
            proto_write_u16_be(&rsp[6], thresholdCm);
            s_tx_len = empaquetarTrama(RSP_CONFIG_ACK, rsp, sizeof(rsp), (uint8_t*)s_tx_buf);
            break;
        }

        case CMD_PING: {
            if (pkt.payloadLen != 0) {
                send_error_reply(CMD_PING, ERR_INVALID_ARG);
                return;
            }
            uint8_t ver = PROTOCOL_VERSION;
            s_tx_len = empaquetarTrama(RSP_PONG, &ver, 1, (uint8_t*)s_tx_buf);
            break;
        }

        default:
            send_error_reply(pkt.type, ERR_UNKNOWN_CMD);
            break;
    }
}

void onRequestHandler(void) {
    if (s_tx_len > 0) {
        Wire.write((const uint8_t*)s_tx_buf, s_tx_len);
    }
}

void i2c_loop(void) {
    I2CMailbox snap;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        snap.pending_stop = s_mailbox.pending_stop;
        snap.pending_config = s_mailbox.pending_config;
        snap.pending_dispense = s_mailbox.pending_dispense;
        snap.pending_unjam = s_mailbox.pending_unjam;
        snap.dispense_steps = s_mailbox.dispense_steps;
        snap.has_dispense_interval = s_mailbox.has_dispense_interval;
        snap.dispense_interval_us = s_mailbox.dispense_interval_us;
        snap.unjam_steps = s_mailbox.unjam_steps;
        snap.cfg_manual_steps = s_mailbox.cfg_manual_steps;
        snap.cfg_step_interval_us = s_mailbox.cfg_step_interval_us;
        snap.cfg_ultrasonic_threshold_cm = s_mailbox.cfg_ultrasonic_threshold_cm;

        s_mailbox.pending_stop = false;
        s_mailbox.pending_config = false;
        s_mailbox.pending_dispense = false;
        s_mailbox.pending_unjam = false;
    }

    if (snap.pending_config) {
        ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
            s_manual_steps = snap.cfg_manual_steps;
        }
        stepper_set_speed_us(snap.cfg_step_interval_us);
        ultrasonic_sensor_set_threshold_cm(snap.cfg_ultrasonic_threshold_cm);
    }

    if (snap.pending_stop) {
        stepper_stop();
    }
    if (snap.pending_dispense) {
        if (snap.has_dispense_interval) {
            stepper_set_speed_us(snap.dispense_interval_us);
        }
        stepper_move(snap.dispense_steps);
    } else if (snap.pending_unjam) {
        stepper_start_unjam(snap.unjam_steps);
    }
}

bool i2c_is_motor_busy(void) {
    bool busy;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        busy = motor_busy_isr_unsafe();
    }
    return busy;
}

uint32_t i2c_get_manual_steps(void) {
    uint32_t steps;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        steps = s_manual_steps;
    }
    return steps;
}

bool i2c_request_manual_dispense(void) {
    bool accepted = false;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        if (!motor_busy_isr_unsafe()) {
            s_mailbox.dispense_steps = (int32_t)s_manual_steps;
            s_mailbox.has_dispense_interval = false;
            s_mailbox.pending_dispense = true;
            s_manual_dispense_latched = true;
            accepted = true;
        }
    }
    return accepted;
}
