#include "status_led.h"
#include "timer.h"
#include <avr/io.h>
#include <stdbool.h>
#include <util/atomic.h>

// Tick base de 50 ms: 20 ticks = 1000 ms (idle), 3 ticks = 150 ms (girando)
static const uint16_t LED_TICK_MS = 50U;
static const uint8_t IDLE_HALF_TICKS = 20U;
static const uint8_t BUSY_HALF_TICKS = 3U;
static const uint8_t ULTRASONIC_CYCLE_TICKS = 20U;

static timer_t* s_led_timer = nullptr;
static status_led_mode_t s_mode = STATUS_LED_IDLE;
static uint8_t s_tick = 0;

static void led_write(bool on) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        if (on) {
            PORTB |= (1 << PORTB5);
        } else {
            PORTB &= ~(1 << PORTB5);
        }
    }
}

static void led_toggle(void) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        PORTB ^= (1 << PORTB5);
    }
}

static void on_led_tick(void) {
    switch (s_mode) {
        case STATUS_LED_MOTOR_BUSY:
            s_tick++;
            if (s_tick >= BUSY_HALF_TICKS) {
                s_tick = 0;
                led_toggle();
            }
            break;

        case STATUS_LED_ULTRASONIC_ACTIVE: {
            // Doble pulso cada 1000 ms: ON [0..1], OFF [2..3], ON [4..5], OFF [6..19]
            bool on = (s_tick < 2U) || (s_tick >= 4U && s_tick < 6U);
            led_write(on);
            s_tick = (uint8_t)((s_tick + 1U) % ULTRASONIC_CYCLE_TICKS);
            break;
        }

        case STATUS_LED_IDLE:
        default:
            s_tick++;
            if (s_tick >= IDLE_HALF_TICKS) {
                s_tick = 0;
                led_toggle();
            }
            break;
    }
}

void status_led_init(void) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        DDRB |= (1 << DDB5);
        PORTB &= ~(1 << PORTB5);
    }
    s_mode = STATUS_LED_IDLE;
    s_tick = 0;

    timer_cfg_t cfg = {LED_TICK_MS, on_led_tick};
    s_led_timer = timer_create(&cfg);
    if (s_led_timer != nullptr) {
        timer_start(s_led_timer);
    }
}

void status_led_set_mode(status_led_mode_t mode) {
    if (mode == s_mode) {
        return;
    }
    s_mode = mode;
    s_tick = 0;
    if (mode == STATUS_LED_ULTRASONIC_ACTIVE) {
        led_write(true);
    }
}
