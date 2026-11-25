#include "ir_sensor.h"
#include <Arduino.h>
#include <avr/interrupt.h>
#include <avr/io.h>
#include <util/atomic.h>

static const uint32_t IR_DEBOUNCE_MS = 150UL;

static volatile bool s_latched = false;
static volatile uint32_t s_last_isr_ms = 0;

void ir_sensor_init(void) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        s_latched = false;
        s_last_isr_ms = 0;

        // PD3 (D3 / INT1): entrada con pull-up interno, activo en LOW
        DDRD &= ~(1 << DDD3);
        PORTD |= (1 << PORTD3);

        // Flanco de bajada en INT1 (ISC11=1, ISC10=0)
        EICRA = (uint8_t)((EICRA | (1 << ISC11)) & ~(1 << ISC10));
        EIFR |= (1 << INTF1);
        EIMSK |= (1 << INT1);
    }
}

void ir_sensor_loop(void) {
    // Eventos IR capturados por INT1 y consultados via ir_sensor_is_live/latched
}

bool ir_sensor_is_live(void) {
    return (PIND & (1 << PD3)) == 0;
}

bool ir_sensor_is_latched(void) {
    return s_latched;
}

bool ir_sensor_consume_latched(void) {
    bool was_latched;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        was_latched = s_latched;
        s_latched = false;
    }
    return was_latched;
}

ISR(INT1_vect) {
    uint32_t now = millis();
    if ((uint32_t)(now - s_last_isr_ms) >= IR_DEBOUNCE_MS) {
        s_latched = true;
        s_last_isr_ms = now;
    }
}
