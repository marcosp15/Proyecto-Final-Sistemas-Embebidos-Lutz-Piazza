#ifndef ULTRASONIC_SENSOR_H
#define ULTRASONIC_SENSOR_H

#include "protocol.h"
#include <stdbool.h>
#include <stdint.h>

static const uint16_t ULTRASONIC_DEFAULT_THRESHOLD_CM = CONFIG_DEFAULT_THRESHOLD_CM;
static const uint16_t ULTRASONIC_MIN_THRESHOLD_CM     = CONFIG_MIN_THRESHOLD_CM;
static const uint16_t ULTRASONIC_MAX_THRESHOLD_CM     = CONFIG_MAX_THRESHOLD_CM;

void ultrasonic_sensor_init(uint16_t threshold_cm);
void ultrasonic_sensor_set_threshold_cm(uint16_t threshold_cm);
uint16_t ultrasonic_sensor_get_threshold_cm(void);
void ultrasonic_sensor_loop(void);
uint16_t ultrasonic_sensor_get_distance_cm(void);
bool ultrasonic_sensor_is_live(void);
bool ultrasonic_sensor_is_latched(void);
bool ultrasonic_sensor_consume_latched(void);

#endif
