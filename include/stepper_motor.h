#ifndef STEPPER_MOTOR_H
#define STEPPER_MOTOR_H

#include <stdbool.h>
#include <stdint.h>

// ULN2003 wiring: IN1->D8 (PB0), IN2->D9 (PB1), IN3->D10 (PB2), IN4->D11 (PB3).
#define STEPPER_STEPS_PER_REV   4096UL
#define STEPPER_MIN_STEP_US     1000U
#define STEPPER_MAX_STEP_US     10000U
#define STEPPER_DEFAULT_STEP_US 2000U
#define STEPPER_UNJAM_PAUSE_MS  150UL

void stepper_init(uint16_t step_interval_us);
void stepper_set_speed_us(uint16_t step_interval_us);
uint16_t stepper_get_speed_us(void);
void stepper_move(int32_t steps);
void stepper_start_unjam(uint16_t wiggle_steps);
void stepper_stop(void);
bool stepper_is_moving(void);
bool stepper_is_busy(void);
int32_t stepper_steps_remaining(void);
void stepper_loop(void);

#endif
