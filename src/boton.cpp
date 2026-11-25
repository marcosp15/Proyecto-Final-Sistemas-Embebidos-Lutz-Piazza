#include "boton.h"
#include <Arduino.h>
#include <avr/interrupt.h>
#include <avr/io.h>
#include <stdbool.h>
#include <util/atomic.h>

static const uint32_t BOTON_DEBOUNCE_MS = 200UL;

static boton_callback_t s_callback = nullptr;
static volatile bool s_pressed = false;
static volatile uint32_t s_last_isr_ms = 0;

void boton_init(boton_callback_t callback) {
    s_callback = callback;

    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        s_pressed = false;
        s_last_isr_ms = 0;

        // PD2 (D2 / INT0): entrada con pull-up interno
        DDRD &= ~(1 << DDD2);
        PORTD |= (1 << PORTD2);

        // Flanco de bajada en INT0 (ISC01=1, ISC00=0)
        EICRA = (uint8_t)((EICRA | (1 << ISC01)) & ~(1 << ISC00));
        EIFR |= (1 << INTF0);
        EIMSK |= (1 << INT0);
    }
}

void boton_loop(void) {
    bool triggered = false;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        if (s_pressed) {
            s_pressed = false;
            triggered = true;
        }
    }
    if (triggered && s_callback != nullptr) {
        s_callback();
    }
}

ISR(INT0_vect) {
    uint32_t now = millis();
    if ((uint32_t)(now - s_last_isr_ms) >= BOTON_DEBOUNCE_MS) {
        s_pressed = true;
        s_last_isr_ms = now;
    }
}
