#ifndef I2C_HANDLERS_H
#define I2C_HANDLERS_H

#include <stdbool.h>
#include <stdint.h>

void initI2C(void);
void i2c_loop(void);
void onReceiveHandler(int numBytes);
void onRequestHandler(void);

bool i2c_is_motor_busy(void);
uint32_t i2c_get_manual_steps(void);
bool i2c_request_manual_dispense(void);

#endif
