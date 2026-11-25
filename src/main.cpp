#include "boton.h"
#include "i2c_handlers.h"
#include "ir_sensor.h"
#include "status_led.h"
#include "stepper_motor.h"
#include "timer.h"
#include <Arduino.h>

static void on_manual_button(void) {
    (void)i2c_request_manual_dispense();
}

void setup() {
    timer_init();
    stepper_init(STEPPER_DEFAULT_STEP_US);
    boton_init(on_manual_button);
    ir_sensor_init();
    status_led_init();
    initI2C();
}

void loop() {
    timer_loop();
    i2c_loop();
    boton_loop();
    ir_sensor_loop();
    stepper_loop();

    if (i2c_is_motor_busy()) {
        status_led_set_mode(STATUS_LED_MOTOR_BUSY);
    } else if (ir_sensor_is_live()) {
        status_led_set_mode(STATUS_LED_IR_ACTIVE);
    } else {
        status_led_set_mode(STATUS_LED_IDLE);
    }
}
