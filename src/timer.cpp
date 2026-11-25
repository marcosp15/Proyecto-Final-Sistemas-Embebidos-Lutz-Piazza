#include "timer.h"
#include <avr/interrupt.h>
#include <avr/io.h>
#include <stdbool.h>
#include <util/atomic.h>

#define MAX_TIMERS 16

struct timer {
    volatile bool in_use;
    volatile bool is_running;
    uint16_t periodo_ms;
    volatile uint16_t counter_ms;
    volatile bool triggered;
    timer_callback_t callback;
};

static struct timer timer_pool[MAX_TIMERS];

void timer_init(void) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        for (uint8_t i = 0; i < MAX_TIMERS; i++) {
            timer_pool[i].in_use = false;
            timer_pool[i].is_running = false;
            timer_pool[i].periodo_ms = 0;
            timer_pool[i].counter_ms = 0;
            timer_pool[i].triggered = false;
            timer_pool[i].callback = nullptr;
        }

        // Timer2 en modo CTC, prescaler 64 (16 MHz / 64 = 250 kHz -> 250 ticks = 1 ms)
        TCCR2A = (uint8_t)((TCCR2A & ~(1 << WGM20)) | (1 << WGM21));
        TCCR2B = (uint8_t)((TCCR2B & ~((1 << WGM22) | (1 << CS21) | (1 << CS20))) | (1 << CS22));
        TCNT2 = 0;
        OCR2A = 249;
        TIFR2 |= (1 << OCF2A);
        TIMSK2 |= (1 << OCIE2A);
    }
}

void timer_loop(void) {
    for (uint8_t i = 0; i < MAX_TIMERS; i++) {
        bool fired = false;
        ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
            if (timer_pool[i].triggered) {
                timer_pool[i].triggered = false;
                fired = true;
            }
        }
        if (fired && timer_pool[i].callback != nullptr) {
            timer_pool[i].callback();
        }
    }
}

timer_t* timer_create(const timer_cfg_t* cfg) {
    if (cfg == nullptr || cfg->periodo_ms == 0) {
        return nullptr;
    }

    for (uint8_t i = 0; i < MAX_TIMERS; i++) {
        if (!timer_pool[i].in_use) {
            ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
                timer_pool[i].periodo_ms = cfg->periodo_ms;
                timer_pool[i].callback = cfg->callback;
                timer_pool[i].counter_ms = 0;
                timer_pool[i].triggered = false;
                timer_pool[i].is_running = false;
                timer_pool[i].in_use = true;
            }
            return &timer_pool[i];
        }
    }
    return nullptr;
}

void timer_start(timer_t* timer) {
    if (timer == nullptr) {
        return;
    }
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        timer->counter_ms = 0;
        timer->triggered = false;
        timer->is_running = true;
    }
}

void timer_stop(timer_t* timer) {
    if (timer == nullptr) {
        return;
    }
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        timer->is_running = false;
        timer->triggered = false;
    }
}

uint16_t timer_remaining_ms(const timer_t* timer) {
    if (timer == nullptr) {
        return 0;
    }
    bool active = false;
    uint16_t counter = 0;
    uint16_t periodo = 0;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        active = timer->in_use && timer->is_running;
        counter = timer->counter_ms;
        periodo = timer->periodo_ms;
    }
    if (!active || counter >= periodo) {
        return 0U;
    }
    return (uint16_t)(periodo - counter);
}

ISR(TIMER2_COMPA_vect) {
    for (uint8_t i = 0; i < MAX_TIMERS; i++) {
        if (timer_pool[i].in_use && timer_pool[i].is_running) {
            uint16_t c = timer_pool[i].counter_ms + 1U;
            if (c >= timer_pool[i].periodo_ms) {
                timer_pool[i].triggered = true;
                timer_pool[i].counter_ms = 0;
            } else {
                timer_pool[i].counter_ms = c;
            }
        }
    }
}
