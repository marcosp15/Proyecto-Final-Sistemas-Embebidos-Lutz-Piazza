#ifndef TIMER_H
#define TIMER_H

#include <stdint.h>

typedef void (*timer_callback_t)(void);

typedef struct timer timer_t;

typedef struct {
    uint16_t periodo_ms;
    timer_callback_t callback;
} timer_cfg_t;

void timer_init(void);
void timer_loop(void);
timer_t* timer_create(const timer_cfg_t* cfg);
void timer_start(timer_t* timer);
void timer_stop(timer_t* timer);
uint16_t timer_remaining_ms(const timer_t* timer);

#endif
