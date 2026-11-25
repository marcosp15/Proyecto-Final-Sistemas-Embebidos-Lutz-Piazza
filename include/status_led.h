#ifndef STATUS_LED_H
#define STATUS_LED_H

#include <stdint.h>

typedef enum : uint8_t {
    STATUS_LED_IDLE = 0,
    STATUS_LED_MOTOR_BUSY = 1,
    STATUS_LED_IR_ACTIVE = 2
} status_led_mode_t;

void status_led_init(void);
void status_led_set_mode(status_led_mode_t mode);

#endif
