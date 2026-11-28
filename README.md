# Dispenser Automático de Alimento para Mascotas

Proyecto Final de la materia **Sistemas Embebidos** (comisión **Lutz – Piazza**).

Sistema embebido distribuido compuesto por una **Raspberry Pi 3B v1.2** (maestro I²C, servidor web, cámara y planificador horario con hora NTP) y un **Arduino UNO (`ATmega328P-PU`)** (esclavo I²C `0x08`, control directo de registros AVR sin FreeRTOS) que acciona un tornillo sinfín mediante un motor paso a paso **28BYJ-48 + ULN2003** y detecta a la mascota con un sensor ultrasónico **`HC-SR04`** en el plato.

---

## Arquitectura General

```text
┌────────────────────────────────────┐      Bus I²C (0x08, 3.3V)      ┌───────────────────────────────────────────────┐
│      Raspberry Pi 3B v1.2          │ ◄────────────────────────────► │         Arduino UNO (ATmega328P-PU)           │
│           (Maestro)                │   GPIO2/3 (SDA/SCL) ↔ A4/A5    │                 (Esclavo)                     │
│                                    │   GND común                    │                                               │
│ • Python 3 + Flask + SQLite (WAL)  │                                │ • Superloop cooperativo (sin FreeRTOS)        │
│ • Planificador (HH:MM e intervalo) │                                │ • Stepper 28BYJ-48 + ULN2003 (PB0..3)         │
│ • Pi Camera V2 8MP (foto / MP4)    │                                │ • HC-SR04 en plato (PD4 TRIG / PD3 ECHO-INT1) │
│ • Interfaz web LAN + API REST      │                                │ • Botón manual (PD2/INT0) + LED (PB5)         │
└────────────────────────────────────┘                                └───────────────────────────────────────────────┘
```

### Decisiones Técnicas Clave
- **Control por registros AVR:** El firmware del `ATmega328P-PU` opera directamente sobre `DDRx`, `PORTx`, `PINx`, `EICRA`, `EIFR`, `EIMSK`, `Timer1` (`TCCR1A/B`, `TCNT1`) y `Timer2` (`TCCR2A/B`, `OCR2A`, `TIMSK2`) sin usar `pinMode()`, `digitalWrite()`, `digitalRead()`, `analogRead()`, `attachInterrupt()`, `pulseIn()`, `delay()` ni `delayMicroseconds()`. El `HC-SR04` genera el pulso `TRIG` de `10 µs` mediante `TCNT1` y mide `ECHO` por interrupción `INT1` en ambos flancos (`Timer1`, `0,5 µs/tick`), sin bloquear `loop()`.
- **Compatibilidad I²C 3.3 V / 5 V:** Las resistencias *pull-up* internas de `PC4` (`A4`) y `PC5` (`A5`) se desactivan por registro al inicializar `Wire`.
- **Protocolo I²C compacto:** Tramas binarias `[LEN][TYPE][PAYLOAD]` (`LEN = 2 + len(PAYLOAD)`, máx. 14 B en el buffer `Wire` de 32 B) sobre el encuadre nativo `START`/`STOP` de I²C, con comandos no bloqueantes para dispensar por pasos con signo, destrabe (`unjam`), parada de emergencia, umbral configurable (`ultrasonic_threshold_cm`) y lectura con *latch* + distancia en vivo (`distance_cm`) del sensor ultrasónico.
- **Alimentación aislada del motor:** Fuente de 5 V dedicada exclusivamente para el módulo `ULN2003` (con `GND` común), evitando caídas de tensión o reinicios en la Raspberry Pi y el Arduino.

---

## Mapa de Conexiones (Arduino UNO)

| Periférico | Pin Arduino | Registro AVR | Función |
| :--- | :--- | :--- | :--- |
| Driver `ULN2003` (`IN1..IN4`) | `D8..D11` | `DDRB` / `PORTB` (`PB0..PB3`) | Secuencia *half-step* de 8 fases, desenergizado automático al frenar |
| Botón dispensado manual | `D2` | `PD2` (`INT0`) | Entrada con pull-up interno, interrupción por flanco de bajada + debounce |
| Sensor ultrasónico `HC-SR04` (`TRIG`) | `D4` | `DDRD` / `PORTD` (`PD4`) | Salida (`LOW` en reposo), pulso `HIGH` de `10 µs` cada `100 ms` temporizado con `TCNT1` (sin `delayMicroseconds()`) |
| Sensor ultrasónico `HC-SR04` (`ECHO`) | `D3` | `PD3` (`INT1`) + `Timer1` | Entrada sin pull-up interno, interrupción `INT1` en ambos flancos + `TCNT1` (`0,5 µs/tick`), filtro 3 muestras + histéresis 3 cm, `distance_cm` + flag *latched* |
| LED de estado | `D13` | `PB5` | Latido de sistema e indicador de actividad |
| Bus I²C (hacia RPi) | `A4` (SDA), `A5` (SCL) | `PC4`, `PC5` | Esclavo I²C en `0x08` (pull-ups internas desactivadas) |

---

## Documentación del Proyecto

- **[PLAN_IMPLEMENTACION_SOFTWARE.md](PLAN_IMPLEMENTACION_SOFTWARE.md):** Arquitectura de software, contratos de pines/registros, protocolo binario I²C, esquema SQLite, endpoints REST, plan de ejecución multi-agente y compuertas de verificación.
- **[PLAN_CONSTRUCCION_FISICA.md](PLAN_CONSTRUCCION_FISICA.md):** Lista de materiales (BOM), fabricación mecánica con materiales reciclados (tolva de botella PET, tubo PVC/cartón, hélice de PET planchado), plano eléctrico y guía de calibración.

---

## Estructura Esperada del Repositorio

```text
.
├── platformio.ini                 # Configuración PlatformIO (ATmega328P / Arduino UNO)
├── include/                       # Headers del firmware AVR
├── src/                           # Implementación bare-metal en C/C++
├── raspberry/
│   ├── run_service.py             # Entrada del servicio maestro (Flask + Worker + Cámara)
│   ├── i2c_master/                # Protocolo, transporte (smbus2 / mock) y controlador
│   ├── service/                   # API REST, SQLite, cámara, scheduler y web UI
│   └── tests/                     # Suite de tests (offline con MockArduinoTransport)
├── PLAN_IMPLEMENTACION_SOFTWARE.md
├── PLAN_CONSTRUCCION_FISICA.md
└── README.md
```

---

## Compilación y Ejecución Rápida

### Firmware (PlatformIO)
```bash
pio run                 # Compilar firmware ATmega328P
pio run -t upload       # Flashear al Arduino UNO
```

### Servicio en Raspberry Pi (o modo simulación `--mock` en PC)
```bash
pip install -r raspberry/requirements.txt
python3 -m unittest discover -s raspberry/tests -v
python3 raspberry/run_service.py --mock --port 5000
```
