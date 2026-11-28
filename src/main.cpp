#include "boton.h"
#include "i2c_handlers.h"
#include "status_led.h"
#include "stepper_motor.h"
#include "timer.h"
#include "ultrasonic_sensor.h"
#include <Arduino.h>

static void on_manual_button(void) {
    (void)i2c_request_manual_dispense();
}

void setup() {
    timer_init();
    stepper_init(STEPPER_DEFAULT_STEP_US);
    boton_init(on_manual_button);
    ultrasonic_sensor_init(ULTRASONIC_DEFAULT_THRESHOLD_CM);
    status_led_init();
    initI2C();
}

void loop() {
    timer_loop();
    i2c_loop();
    boton_loop();
    ultrasonic_sensor_loop();
    stepper_loop();

    if (i2c_is_motor_busy()) {
        status_led_set_mode(STATUS_LED_MOTOR_BUSY);
    } else if (ultrasonic_sensor_is_live()) {
        status_led_set_mode(STATUS_LED_ULTRASONIC_ACTIVE);
    } else {
        status_led_set_mode(STATUS_LED_IDLE);
    }
}
