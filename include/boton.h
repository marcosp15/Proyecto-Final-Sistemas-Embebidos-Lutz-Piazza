#ifndef BOTON_H
#define BOTON_H

typedef void (*boton_callback_t)(void);

void boton_init(boton_callback_t callback);
void boton_loop(void);

#endif
