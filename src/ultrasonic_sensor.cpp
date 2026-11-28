#include "ultrasonic_sensor.h"
#include "timer.h"
#include <Arduino.h>
#include <avr/interrupt.h>
#include <avr/io.h>
#include <util/atomic.h>

// Timer1 prescaler 8 (16 MHz / 8 = 2 MHz -> 0.5 us/tick): 1 cm = 58 us = 116 ticks
static const uint16_t MEASURE_PERIOD_MS = 100U;
static const uint32_t ECHO_TIMEOUT_MS   = 30UL;
static const uint16_t TRIG_PULSE_TICKS  = 20U;     // 10 us
static const uint16_t TICKS_PER_CM      = 116U;
static const uint16_t ECHO_MIN_TICKS    = 232U;    // 2 cm
static const uint16_t ECHO_MAX_TICKS    = 46400U;  // 400 cm
static const uint16_t HYSTERESIS_CM     = 3U;
static const uint8_t  CONFIRM_COUNT     = 3U;

static timer_t* s_measure_timer = nullptr;
static bool s_trigger_due = false;
static uint32_t s_trig_ms = 0;

static volatile uint16_t s_threshold_cm = ULTRASONIC_DEFAULT_THRESHOLD_CM;
static volatile uint16_t s_distance_cm = ULTRASONIC_NO_ECHO_CM;
static volatile bool s_live = false;
static volatile bool s_latched = false;

static volatile bool s_waiting_echo = false;
static volatile bool s_echo_rising_seen = false;
static volatile bool s_echo_ready = false;
static volatile uint16_t s_echo_start = 0;
static volatile uint16_t s_echo_ticks = 0;

static uint8_t s_detect_streak = 0;
static uint8_t s_clear_streak = 0;

static void on_measure_tick(void) {
    s_trigger_due = true;
}

static void process_sample(uint16_t dist_cm) {
    uint16_t threshold = ultrasonic_sensor_get_threshold_cm();

    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        s_distance_cm = dist_cm;
    }

    if (!s_live) {
        if (dist_cm != ULTRASONIC_NO_ECHO_CM && dist_cm <= threshold) {
            if (s_detect_streak < CONFIRM_COUNT) {
                s_detect_streak++;
            }
            if (s_detect_streak >= CONFIRM_COUNT) {
                ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
                    s_live = true;
                    s_latched = true;
                }
                s_clear_streak = 0;
            }
        } else {
            s_detect_streak = 0;
        }
    } else {
        if (dist_cm == ULTRASONIC_NO_ECHO_CM || dist_cm > (uint16_t)(threshold + HYSTERESIS_CM)) {
            if (s_clear_streak < CONFIRM_COUNT) {
                s_clear_streak++;
            }
            if (s_clear_streak >= CONFIRM_COUNT) {
                ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
                    s_live = false;
                }
                s_detect_streak = 0;
            }
        } else {
            s_clear_streak = 0;
        }
    }
}

void ultrasonic_sensor_init(uint16_t threshold_cm) {
    ultrasonic_sensor_set_threshold_cm(threshold_cm);

    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        s_distance_cm = ULTRASONIC_NO_ECHO_CM;
        s_live = false;
        s_latched = false;
        s_waiting_echo = false;
        s_echo_rising_seen = false;
        s_echo_ready = false;
        s_echo_start = 0;
        s_echo_ticks = 0;

        // Timer1 modo normal, prescaler 8 (0.5 us/tick)
        TCCR1A = 0;
        TCCR1B = (1 << CS11);
        TCNT1 = 0;

        // PD4 (D4 / TRIG): salida en reposo LOW
        DDRD |= (1 << DDD4);
        PORTD &= ~(1 << PORTD4);

        // PD3 (D3 / ECHO / INT1): entrada sin pull-up interno (salida push-pull 0-5V del HC-SR04)
        DDRD &= ~(1 << DDD3);
        PORTD &= ~(1 << PORTD3);

        // INT1 en cualquier cambio logico (ISC11=0, ISC10=1)
        EICRA = (uint8_t)((EICRA & ~(1 << ISC11)) | (1 << ISC10));
        EIFR |= (1 << INTF1);
        EIMSK |= (1 << INT1);
    }

    s_trigger_due = false;
    s_trig_ms = 0;
    s_detect_streak = 0;
    s_clear_streak = 0;

    if (s_measure_timer == nullptr) {
        timer_cfg_t cfg = {MEASURE_PERIOD_MS, on_measure_tick};
        s_measure_timer = timer_create(&cfg);
    }
    if (s_measure_timer != nullptr) {
        timer_start(s_measure_timer);
    }
}

void ultrasonic_sensor_set_threshold_cm(uint16_t threshold_cm) {
    uint16_t clamped = threshold_cm;
    if (clamped < ULTRASONIC_MIN_THRESHOLD_CM) {
        clamped = ULTRASONIC_MIN_THRESHOLD_CM;
    } else if (clamped > ULTRASONIC_MAX_THRESHOLD_CM) {
        clamped = ULTRASONIC_MAX_THRESHOLD_CM;
    }
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        s_threshold_cm = clamped;
    }
}

uint16_t ultrasonic_sensor_get_threshold_cm(void) {
    uint16_t threshold;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        threshold = s_threshold_cm;
    }
    return threshold;
}

void ultrasonic_sensor_loop(void) {
    bool echo_ready = false;
    uint16_t echo_ticks = 0;
    bool waiting = false;

    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        if (s_echo_ready) {
            echo_ticks = s_echo_ticks;
            s_echo_ready = false;
            echo_ready = true;
        }
        waiting = s_waiting_echo;
    }

    if (echo_ready) {
        uint16_t dist_cm = ULTRASONIC_NO_ECHO_CM;
        if (echo_ticks >= ECHO_MIN_TICKS && echo_ticks <= ECHO_MAX_TICKS) {
            dist_cm = (uint16_t)(echo_ticks / TICKS_PER_CM);
        }
        process_sample(dist_cm);
    } else if (waiting && (uint32_t)(millis() - s_trig_ms) >= ECHO_TIMEOUT_MS) {
        bool timed_out = false;
        ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
            if (s_waiting_echo && !s_echo_ready) {
                s_waiting_echo = false;
                s_echo_rising_seen = false;
                timed_out = true;
            }
        }
        if (timed_out) {
            process_sample(ULTRASONIC_NO_ECHO_CM);
            waiting = false;
        }
    }

    if (s_trigger_due && !waiting) {
        s_trigger_due = false;
        s_trig_ms = millis();

        // Seccion critica de 10 us: evita corromper el registro TEMP de 16 bits de TCNT1
        ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
            s_echo_rising_seen = false;
            s_echo_ready = false;
            s_waiting_echo = true;
            uint16_t t0 = TCNT1;
            PORTD |= (1 << PORTD4);
            while ((uint16_t)(TCNT1 - t0) < TRIG_PULSE_TICKS) {}
            PORTD &= ~(1 << PORTD4);
            EIFR |= (1 << INTF1);
        }
    }
}

uint16_t ultrasonic_sensor_get_distance_cm(void) {
    uint16_t dist;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        dist = s_distance_cm;
    }
    return dist;
}

bool ultrasonic_sensor_is_live(void) {
    return s_live;
}

bool ultrasonic_sensor_is_latched(void) {
    return s_latched;
}

bool ultrasonic_sensor_consume_latched(void) {
    bool was_latched;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        was_latched = s_latched;
        s_latched = false;
    }
    return was_latched;
}

ISR(INT1_vect) {
    if (!s_waiting_echo) {
        return;
    }
    uint16_t now = TCNT1;
    if ((PIND & (1 << PD3)) != 0) {
        s_echo_start = now;
        s_echo_rising_seen = true;
    } else if (s_echo_rising_seen) {
        s_echo_ticks = (uint16_t)(now - s_echo_start);
        s_echo_rising_seen = false;
        s_waiting_echo = false;
        s_echo_ready = true;
    }
}
