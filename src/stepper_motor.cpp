#include "stepper_motor.h"
#include <Arduino.h>
#include <avr/io.h>
#include <util/atomic.h>

// Parte baja de PORTB mapea directamente a D8..D11 (PB0..PB3).
static const uint8_t COIL_MASK = 0x0F;

// Secuencia half-step de 8 fases [IN4:IN3:IN2:IN1]
static const uint8_t HALF_STEP_SEQ[8] = {
    0b0001, // IN1
    0b0011, // IN1 + IN2
    0b0010, // IN2
    0b0110, // IN2 + IN3
    0b0100, // IN3
    0b1100, // IN3 + IN4
    0b1000, // IN4
    0b1001  // IN4 + IN1
};

typedef enum : uint8_t {
    UNJAM_IDLE = 0,
    UNJAM_REV = 1,
    UNJAM_PAUSE = 2,
    UNJAM_FWD = 3
} unjam_state_t;

static bool s_initialized = false;
static bool s_coils_active = false;
static uint8_t s_phase = 0;
static volatile int32_t s_steps_remaining = 0;
static uint16_t s_step_interval_us = STEPPER_DEFAULT_STEP_US;
static uint32_t s_last_step_us = 0;

static volatile unjam_state_t s_unjam_state = UNJAM_IDLE;
static uint16_t s_unjam_wiggle = 0;
static uint32_t s_unjam_pause_start_ms = 0;

static void write_coils(uint8_t pattern) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        PORTB = (uint8_t)((PORTB & ~COIL_MASK) | (pattern & COIL_MASK));
    }
    s_coils_active = (pattern != 0);
}

void stepper_init(uint16_t step_interval_us) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        DDRB |= COIL_MASK;
        PORTB &= ~COIL_MASK;
        s_steps_remaining = 0;
        s_unjam_state = UNJAM_IDLE;
    }
    s_coils_active = false;
    s_phase = 0;
    s_unjam_wiggle = 0;
    stepper_set_speed_us(step_interval_us);
    s_last_step_us = micros();
    s_initialized = true;
}

void stepper_set_speed_us(uint16_t step_interval_us) {
    if (step_interval_us < STEPPER_MIN_STEP_US) {
        s_step_interval_us = STEPPER_MIN_STEP_US;
    } else if (step_interval_us > STEPPER_MAX_STEP_US) {
        s_step_interval_us = STEPPER_MAX_STEP_US;
    } else {
        s_step_interval_us = step_interval_us;
    }
}

uint16_t stepper_get_speed_us(void) {
    return s_step_interval_us;
}

void stepper_move(int32_t steps) {
    if (!s_initialized) {
        return;
    }
    if (steps == 0) {
        stepper_stop();
        return;
    }
    if (!s_coils_active && s_steps_remaining == 0) {
        s_last_step_us = micros() - s_step_interval_us;
    }
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        s_steps_remaining = steps;
    }
}

void stepper_start_unjam(uint16_t wiggle_steps) {
    if (!s_initialized || wiggle_steps == 0) {
        return;
    }
    s_unjam_wiggle = wiggle_steps;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        s_unjam_state = UNJAM_REV;
    }
    stepper_move(-(int32_t)wiggle_steps);
}

void stepper_stop(void) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        s_steps_remaining = 0;
        s_unjam_state = UNJAM_IDLE;
    }
    if (s_initialized) {
        // Desenergizar bobinas inmediatamente para evitar calentamiento del ULN2003
        write_coils(0);
    }
}

bool stepper_is_moving(void) {
    int32_t rem;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        rem = s_steps_remaining;
    }
    return rem != 0;
}

bool stepper_is_busy(void) {
    bool busy;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        busy = (s_steps_remaining != 0) || (s_unjam_state != UNJAM_IDLE);
    }
    return busy;
}

int32_t stepper_steps_remaining(void) {
    int32_t rem;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        rem = s_steps_remaining;
    }
    return rem;
}

void stepper_loop(void) {
    if (!s_initialized) {
        return;
    }

    int32_t rem = s_steps_remaining;

    if (rem != 0 || s_coils_active) {
        uint32_t now_us = micros();
        if ((uint32_t)(now_us - s_last_step_us) >= s_step_interval_us) {
            s_last_step_us = now_us;

            if (rem == 0) {
                write_coils(0);
            } else {
                // Sentido invertido por orientacion de la helice del sinfin
                if (rem > 0) {
                    s_phase = (uint8_t)((s_phase - 1U) & 0x07U);
                    rem--;
                } else {
                    s_phase = (uint8_t)((s_phase + 1U) & 0x07U);
                    rem++;
                }
                ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
                    s_steps_remaining = rem;
                }
                write_coils(HALF_STEP_SEQ[s_phase]);
            }
        }
    }

    // Maquina de estados no bloqueante para secuencia UNJAM
    if (s_unjam_state != UNJAM_IDLE && rem == 0) {
        if (s_unjam_state == UNJAM_REV) {
            s_unjam_pause_start_ms = millis();
            ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
                s_unjam_state = UNJAM_PAUSE;
            }
        } else if (s_unjam_state == UNJAM_PAUSE) {
            if ((uint32_t)(millis() - s_unjam_pause_start_ms) >= STEPPER_UNJAM_PAUSE_MS) {
                ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
                    s_unjam_state = UNJAM_FWD;
                }
                stepper_move((int32_t)s_unjam_wiggle);
            }
        } else if (s_unjam_state == UNJAM_FWD) {
            ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
                s_unjam_state = UNJAM_IDLE;
            }
        }
    }
}
