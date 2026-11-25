#ifndef IR_SENSOR_H
#define IR_SENSOR_H

#include <stdbool.h>

void ir_sensor_init(void);
void ir_sensor_loop(void);
bool ir_sensor_is_live(void);
bool ir_sensor_is_latched(void);
bool ir_sensor_consume_latched(void);

#endif
