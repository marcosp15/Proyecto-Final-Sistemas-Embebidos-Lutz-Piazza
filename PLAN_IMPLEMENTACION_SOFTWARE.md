# Plan de Implementación de Software — Dispenser Automático de Alimento para Mascotas

> **Propósito:** Especificación técnica ejecutable lista para entregar a un **Neon Orchestrator con swarm**. Define contratos cerrados (pines/registros, protocolo binario I2C, esquema SQLite, API REST), módulos reutilizables de los laboratorios previos, partición en *workers* paralelos sin conflictos de archivos y compuertas de verificación automatizadas.

---

## 1. Resumen Ejecutivo y Arquitectura

Sistema embebido distribuido en dos nodos:

1. **Nodo Esclavo — Arduino UNO (`ATmega328P-PU`, PlatformIO):**
   - Controla el motor paso a paso `28BYJ-48` + driver `ULN2003` mediante manipulación directa de registros AVR (`DDRB`/`PORTB`), sin bloqueos.
   - Detecta a la mascota mediante sensor IR digital (`FC-51` / `HW-201`) por interrupción externa `INT1` (`PD3`) con antirrebote y *flag latched* para no perder eventos entre consultas del maestro.
   - Botón físico opcional de dispensado manual en `INT0` (`PD2`) con antirrebote.
   - LED testigo de estado en `PB5` (`D13`) gobernado por `Timer2` (base de tiempo 1 ms).
   - Esclavo I2C en dirección `0x08` con *pull-ups* internas de 5 V desactivadas por registro (`DDRC`/`PORTC`) para compatibilidad eléctrica con la Raspberry Pi (3.3 V).
   - **Sin FreeRTOS** (superloop cooperativo `*_init()` / `*_loop()`).

2. **Nodo Maestro — Raspberry Pi 3B v1.2 (Python 3 + Flask + SQLite):**
   - Mantiene hora sincronizada por internet (NTP del SO).
   - Servicio único (`run_service.py` + unidad `systemd`) compuesto por:
     - Capa I2C thread-safe (`i2c_master/` + `client_sync.py`) con modo `--mock` para desarrollo y CI sin hardware.
     - Hilo de fondo (`worker.py`): consulta estado/IR al Arduino, dispara captura asíncrona de cámara con *cooldown* configurable, y ejecuta el planificador de comidas (horarios fijos `HH:MM` y/o modo intervalo cada `N` minutos).
     - Gestor de cámara (`camera.py`): autodetecta CLI disponible (`rpicam-*` → `libcamera-*` → `raspistill`/`raspivid` + `ffmpeg` para MP4), soporta foto JPEG (default) y video corto MP4 configurable, con rotación automática (máximo de archivos) y backend *mock*.
     - API REST Flask + interfaz web funcional mínima (`service/web/index.html`) para red local (LAN, sin autenticación).

---

## 2. Código Base Reutilizable (Repositorios de Referencia)

Los *workers* deben reutilizar y adaptar estos archivos ya probados en la materia (no copiar código muerto de LCD/LM35/keypad/PWM/FreeRTOS):

| Origen | Archivo(s) | Qué reutilizar | Qué modificar / descartar |
| :--- | :--- | :--- | :--- |
| `/home/marocos/Documents/PlatformIO/Projects/pruebas proyecto final embebidos` | `include/stepper_motor.h`, `src/stepper_motor.cpp` | Driver completo `28BYJ-48` por registros `DDRB`/`PORTB` (`PB0..PB3`), secuencia *half-step* 8 fases, desenergizado automático al frenar. | Mantener intacto; agregar solo una máquina de estados mínima para secuencia `unjam` (o implementarla en un módulo coordinador sobre `stepper_move`). **Descartar** `liquid_crystal.*`, `main_musica.cpp`. |
| `/home/marocos/Documents/PlatformIO/Projects/pruebas proyecto final embebidos` | `include/timer.h`, `src/timer.cpp`, `include/boton.h`, `src/boton.cpp` | Base de tiempo `TIMER2_COMPA_vect` (1 ms, hasta 16 timers virtuales) y patrón `INT0` con debounce en `PD2`. | En `timer.cpp`, proteger `counter_ms`/`is_running` con `ATOMIC_BLOCK` si se reinicia desde fuera de ISR. En `boton.cpp`, asegurar lectura/configuración 100% por registros (`DDRD`/`PORTD`/`EICRA`/`EIMSK`). |
| `/home/marocos/Documents/PlatformIO/Projects/laboratorio4-lutz-piazza` | `include/protocol.h`, `src/protocol.cpp`, `src/i2c_handlers.cpp` | Estructura modular del protocolo I2C e `initI2C()` con desactivación de pull-ups en `PC4`/`PC5`. | Reemplazar opcodes de Lab 4 y simplificar el framing a `[LEN][TYPE][PAYLOAD]` (`LEN = 2 + payloadLen`, sin delimitadores ni escapes; I2C ya delimita transacciones por hardware). Validar `inBuf[0] == len` en `decodificarTrama()` y desacoplar ejecución de comandos fuera de la ISR `onReceive`. |
| `/home/marocos/Documents/PlatformIO/Projects/laboratorio4-lutz-piazza` | `raspberry/i2c_master/{protocol,transport,controller}.py` | Clases `SMBus2Transport`, `LinuxI2CDevTransport`, `MockArduinoTransport`, `empaquetar_trama`, `decodificar_trama`. | Actualizar opcodes/mock al protocolo del dispenser. Eliminar `sensor.py` (LM35) y lógica de brillo/ADC. |
| `/home/marocos/Documents/PlatformIO/Projects/laboratorio4-lutz-piazza` | `raspberry/service/{config,database,client_sync,collector,api}.py`, `raspberry/run_service.py`, `raspberry/monitoreo-i2c.service` | Estructura Flask + SQLite + `threading.Lock` + hilo daemon + manejo de `SIGINT`/`SIGTERM` + plantilla `systemd`. | Adaptar esquema SQLite, endpoints, `worker.py` (scheduler + polling IR), sumar `camera.py` y `web/index.html`. |
| `/home/marocos/Documents/PlatformIO/Projects/laboratorio5-lutz-piazza` | *(Ignorar)* | Nada. | **PROHIBIDO usar FreeRTOS.** |

---

## 3. Reglas Duras y Restricciones (Hard Constraints)

El Coordinador del swarm debe rechazar cualquier PR/commit de un worker que viole estas reglas:

1. **Cero funciones Arduino de GPIO:** Prohibido usar `pinMode()`, `digitalWrite()`, `digitalRead()`, `analogRead()`, `attachInterrupt()` en `include/` y `src/`. Toda configuración y operación de pines, interrupciones externas y Timer2 debe hacerse con operaciones binarias sobre registros AVR (`DDRx`, `PORTx`, `PINx`, `EICRA`, `EIMSK`, `TCCR2A/B`, `OCR2A`, `TIMSK2`) y secciones críticas con `<util/atomic.h>`.
   - *Excepciones permitidas (igual que Lab 4 y driver probado):* `<Wire.h>` para el periférico TWI esclavo, y `millis()`/`micros()` (Timer0 del core AVR) usados por `stepper_motor.cpp` y debounce.
2. **Seguridad eléctrica I2C 3.3 V:** `initI2C()` DEBE ejecutar `DDRC &= ~((1 << PC4) | (1 << PC5)); PORTC &= ~((1 << PC4) | (1 << PC5));` inmediatamente después de `Wire.begin(SLAVE_ADDR)`.
3. **No bloquear el superloop ni hacer trabajo pesado en ISR:**
   - Prohibido usar `delay()` o bucles bloqueantes en `src/`.
   - `Wire.onReceive` solo decodifica la trama, actualiza un buzón `volatile` (o responde `RSP_STATUS`/`RSP_PONG`/`RSP_ERROR`) y llena `txBuffer`. Las transiciones de motor se ejecutan de forma segura desde `loop()`.
4. **Límite de buffer I2C (`Wire.h`):** Toda trama empaquetada `[LEN][TYPE][PAYLOAD]` debe medir `<= 32` bytes (máximo 14 bytes con `PROTOCOL_MAX_PAYLOAD = 12`).
5. **Protección térmica del ULN2003:** Las 4 bobinas (`PB0..PB3`) DEBEN quedar en `0` cada vez que `s_steps_remaining == 0` o ante `CMD_STOP_MOTOR`.
6. **Cámara asíncrona y serializada en RPi:** Una captura de video de 5 s NO puede bloquear el hilo de polling I2C ni el servidor HTTP Flask. Debe ejecutarse en un `ThreadPoolExecutor(max_workers=1)` (o cola dedicada) con protección contra capturas simultáneas.
7. **Simplicidad (sin sobreingeniería):** Sin ORM (usar `sqlite3` estándar con `WAL`), sin frontend build tools (HTML + JS vanilla simple en `service/web/index.html`), sin dependencias pesadas que requieran compilación Rust/C en ARMv7.

---

## 4. Contrato 1 — Mapa de Pines y Registros ATmega328P

| Periférico | Pin Arduino | Pin AVR | Registros | Configuración / Notas |
| :--- | :--- | :--- | :--- | :--- |
| **ULN2003 IN1..IN4** | `D8, D9, D10, D11` | `PB0..PB3` | `DDRB`, `PORTB` | Salidas (`DDRB |= 0x0F`), `PORTB &= ~0x0F` en reposo. |
| **Botón dispensado manual** | `D2` | `PD2` (`INT0`) | `DDRD`, `PORTD`, `PIND`, `EICRA`, `EIMSK` | Entrada con pull-up interno (`PORTD |= (1<<PD2)`), flanco de bajada (`ISC01=1, ISC00=0`), debounce 200 ms. |
| **Sensor IR (`FC-51` / `HW-201`)** | `D3` | `PD3` (`INT1`) | `DDRD`, `PORTD`, `PIND`, `EICRA`, `EIMSK` | Entrada con pull-up interno (`PORTD |= (1<<PD3)`), activo en `LOW`, flanco de bajada (`ISC11=1, ISC10=0`), debounce 150 ms + lectura en vivo `!(PIND & (1<<PD3))`. |
| **LED de Estado** | `D13` | `PB5` | `DDRB`, `PORTB` | Salida (`DDRB |= (1<<PB5)`). Parpadeo lento (1000 ms) = idle; rápido (150 ms) = girando; doble pulso = IR activo. |
| **I2C Esclavo (`0x08`)** | `A4` (SDA), `A5` (SCL) | `PC4`, `PC5` | `DDRC`, `PORTC` + `Wire` | Pull-ups internas **desactivadas** tras `Wire.begin(0x08)`. |
| **Base de tiempo 1 ms** | *(Interno)* | `Timer2` | `TCCR2A/B`, `OCR2A`, `TIMSK2` | CTC, prescaler 64, `OCR2A = 249` (`ISR(TIMER2_COMPA_vect)`). |

---

## 5. Contrato 2 — Protocolo Binario I2C (Single Source of Truth)

Framing compacto sobre transacciones I2C (sin delimitadores ni escapes, ya que el bus I2C provee `START`/`STOP` por hardware):
`[LEN] [TYPE] [PAYLOAD: 0..N bytes]`
- `LEN = 2 + len(PAYLOAD)` (longitud total de la trama en bytes; máx. 14 bytes con `PROTOCOL_MAX_PAYLOAD = 12`, `<= 32` bytes del buffer `Wire`).
- `TYPE` y `PAYLOAD` se transmiten en crudo (sin escapes).
- Todos los enteros multibyte son **Big-Endian** (complemento a dos para enteros con signo).
- Dirección esclavo: `SLAVE_ADDR = 0x08`, Bus RPi: `1`, lectura I2C del maestro: `rx_len = 16`, `delay_s = 0.03..0.05`.

### 5.1 Tabla de Comandos y Respuestas

| Comando (RPi → Arduino) | Código | Payload Request | Respuesta (Arduino → RPi) | Código | Payload Response |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `CMD_GET_STATUS` | `0x10` | 1 byte: `flags` (`0x01` = consumir latches `ir_latched` y `manual_dispense_latched`; `0x00` = solo leer sin limpiar). Si payload vacío, asumir `0x00`. | `RSP_STATUS` | `0x90` | **6 bytes:** `[0]` `status_flags`, `[1..4]` `int32 steps_remaining`, `[5]` `last_error` |
| `CMD_DISPENSE_STEPS` | `0x11` | **4 bytes:** `int32 steps` (con signo; `+` avanza, `-` retrocede). Opcionalmente **6 bytes:** `+ uint16 step_interval_us`. Rango válido: `1 <= |steps| <= 204800` (50 vueltas máx). | `RSP_DISPENSE_ACK` | `0x91` | **4 bytes:** `int32 steps` aceptados |
| `CMD_STOP_MOTOR` | `0x12` | 0 bytes | `RSP_STOP_ACK` | `0x92` | **4 bytes:** `int32 steps_cancelled` |
| `CMD_UNJAM` | `0x13` | 0 bytes (default `512` pasos = 1/8 vuelta) o **2 bytes:** `uint16 wiggle_steps` (`64..4096`) | `RSP_UNJAM_ACK` | `0x93` | **2 bytes:** `uint16 wiggle_steps` aceptados |
| `CMD_SET_CONFIG` | `0x14` | **6 bytes:** `[0..3]` `uint32 manual_steps` (`256..40960`), `[4..5]` `uint16 step_interval_us` (`1000..10000`) | `RSP_CONFIG_ACK` | `0x94` | **6 bytes:** eco de `manual_steps` y `step_interval_us` aplicados |
| `CMD_PING` | `0x1F` | 0 bytes | `RSP_PONG` | `0x9F` | **1 byte:** `protocol_version = 0x01` |
| *(Cualquier error)* | — | — | `RSP_ERROR` | `0xFE` | **2 bytes:** `[0]` `failed_cmd`, `[1]` `error_code` |

### 5.2 Bitmask `status_flags` (byte 0 de `RSP_STATUS`)
- `bit 0 (0x01)`: `MOTOR_BUSY` — `1` si el stepper está girando o ejecutando una secuencia `unjam`.
- `bit 1 (0x02)`: `IR_LATCHED` — `1` si el sensor IR detectó un flanco de bajada desde la última lectura consumida.
- `bit 2 (0x04)`: `IR_LIVE` — `1` si el sensor IR está detectando presencia en este instante (`!(PIND & (1 << PD3))`).
- `bit 3 (0x08)`: `MANUAL_DISPENSE_LATCHED` — `1` si el botón físico `D2` disparó un dispensado desde la última lectura consumida.

### 5.3 Códigos `error_code` (`RSP_ERROR`)
- `0x00`: `ERR_NONE`
- `0x01`: `ERR_BUSY` (se pidió `CMD_DISPENSE_STEPS` o `CMD_UNJAM` mientras `MOTOR_BUSY == 1`)
- `0x02`: `ERR_INVALID_ARG` (`steps == 0`, fuera de rango, o longitud de payload incorrecta)
- `0x03`: `ERR_UNKNOWN_CMD`
- `0x04`: `ERR_INVALID_FRAME`

### 5.4 Secuencia No Bloqueante de `CMD_UNJAM` en Arduino
Al recibir `CMD_UNJAM(wiggle_steps)` con motor libre:
1. Estado `UNJAM_REV`: `stepper_move(-wiggle_steps)`.
2. Al terminar (`!stepper_is_moving()`): estado `UNJAM_PAUSE` de 150 ms (via timestamp `millis()`).
3. Al cumplir pausa: estado `UNJAM_FWD` → `stepper_move(+wiggle_steps)`.
4. Al terminar: estado `UNJAM_IDLE`.
`CMD_STOP_MOTOR` cancela cualquier fase de `UNJAM` inmediatamente y desenergiza bobinas.

---

## 6. Contrato 3 — Esquema SQLite (`raspberry/service/database.py`)

Activar `PRAGMA journal_mode=WAL;` y `PRAGMA foreign_keys=ON;` en cada conexión.

```sql
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time_hhmm TEXT NOT NULL,            -- Formato estricto 'HH:MM' (00:00..23:59)
    portions INTEGER NOT NULL,          -- 1..10
    enabled INTEGER NOT NULL DEFAULT 1, -- 0 o 1
    last_triggered_date TEXT            -- 'YYYY-MM-DD' local para evitar doble disparo
);

CREATE TABLE IF NOT EXISTS dispense_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,            -- ISO-8601 con timezone local
    source TEXT NOT NULL,               -- 'schedule' | 'interval' | 'manual_web' | 'manual_button' | 'unjam'
    portions REAL NOT NULL,
    steps INTEGER NOT NULL,
    status TEXT NOT NULL,               -- 'ok' | 'busy' | 'error'
    detail TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS media_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,            -- ISO-8601 con timezone local
    trigger TEXT NOT NULL,              -- 'ir' | 'manual'
    media_type TEXT NOT NULL,           -- 'photo' | 'video'
    filename TEXT NOT NULL UNIQUE,      -- Nombre plano sin rutas (ej. '20261125_143012_ir.jpg')
    size_bytes INTEGER NOT NULL DEFAULT 0,
    duration_s REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dispense_ts ON dispense_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_media_ts ON media_captures(timestamp DESC);
```

### Valores por Defecto en `settings` (seed idempotente en `init_db`)
| Clave | Default | Rango válido / Descripción |
| :--- | :--- | :--- |
| `steps_per_portion` | `4096` | `256..40960` (calibrable desde web; 4096 = 1 vuelta del 28BYJ-48) |
| `step_interval_us` | `2000` | `1000..10000` (µs entre medios pasos) |
| `capture_mode` | `photo` | `photo` \| `video` |
| `video_duration_s` | `5` | `2..15` segundos |
| `ir_cooldown_s` | `30` | `5..600` segundos mínimos entre capturas disparadas por IR |
| `max_media_files` | `100` | `10..500` archivos máximos retenidos en disco/BD (borra los más viejos) |
| `interval_enabled` | `0` | `0` \| `1` (habilita modo intervalo repetitivo) |
| `interval_minutes` | `240` | `1..1440` (ej. 240 = cada 4 horas; permite 1 min para pruebas en vivo) |
| `interval_portions` | `1` | `1..10` |
| `last_interval_run_at` | `""` | ISO-8601 de la última ejecución por intervalo |

---

## 7. Contrato 4 — API REST Flask y Web Funcional (`raspberry/service/api.py`)

El diseño visual no es prioritario: `GET /` sirve un `index.html` limpio y utilitario (HTML + JS `fetch`) que expone controles para probar todos los endpoints y muestra la galería de fotos (`<img>`) y videos (`<video controls>`) con fecha/hora.

| Método y Ruta | Request Body / Query | Respuesta JSON (`200`/`201`) | Errores esperados |
| :--- | :--- | :--- | :--- |
| `GET /` | — | HTML funcional mínimo (`service/web/index.html`) | — |
| `GET /api/status` | — | `{status, mode, arduino: {connected, motor_busy, steps_remaining, ir_live, last_error}, worker: {running, polls, errors, last_poll_at, next_schedule}, camera: {backend, capturing}, server_time}` | `500` |
| `GET /api/config` | — | `{status: "ok", config: {...settings tipados...}}` | `500` |
| `PUT /api/config` | JSON parcial con cualquier subconjunto de `settings` | `{status: "ok", config: {...}, arduino_synced: bool}`. Si cambia `steps_per_portion` o `step_interval_us`, envía `CMD_SET_CONFIG` al Arduino. | `400` validación fuera de rango |
| `GET /api/schedules` | — | `{status: "ok", schedules: [{id, time_hhmm, portions, enabled, last_triggered_date}]}` | `500` |
| `POST /api/schedules` | `{"time_hhmm": "08:30", "portions": 2, "enabled": true}` | `201` `{status: "ok", schedule: {...}}` | `400` formato `HH:MM` o porciones inválidas |
| `PUT /api/schedules/<id>` | Campos opcionales `time_hhmm`, `portions`, `enabled` | `200` `{status: "ok", schedule: {...}}` | `400`, `404` |
| `DELETE /api/schedules/<id>` | — | `200` `{status: "ok", deleted_id: id}` | `404` |
| `POST /api/dispense` | `{"portions": 1}` (1..10) o `{"steps": 4096}` (`-204800..204800`, ≠ 0) | `200` `{status: "ok", steps, portions, event_id}` | `400` inválido, `409` Arduino `BUSY`, `503` error I2C |
| `POST /api/unjam` | `{}` o `{"wiggle_steps": 512}` | `200` `{status: "ok", wiggle_steps, event_id}` | `400`, `409` `BUSY`, `503` |
| `POST /api/stop` | `{}` | `200` `{status: "ok", steps_cancelled}` | `503` |
| `GET /api/history` | `?limit=50&offset=0` | `{status: "ok", total, events: [...]}` | `500` |
| `GET /api/media` | `?limit=50&offset=0&type=photo\|video` | `{status: "ok", total, items: [{id, timestamp, trigger, media_type, filename, url, size_bytes, duration_s}]}` | `500` |
| `POST /api/media/capture` | `{}` o `{"mode": "photo"\|"video", "duration_s": 5}` | `201` `{status: "ok", item: {...}}` | `400`, `409` cámara ocupada, `503` fallo de cámara |
| `GET /media/<filename>` | — | Archivo JPEG o MP4 servido desde `media_dir` (`send_from_directory`) | `404` |
| `DELETE /api/media/<id>` | — | `200` `{status: "ok", deleted_id: id}` (borra registro BD y archivo en disco si existe) | `404` |
| `POST /api/mock/ir` | *(Solo habilitado con `--mock`)* `{}` | `200` `{status: "ok", simulated: true}` — inyecta detección IR en `MockArduinoTransport` para probar el flujo completo sin hardware. | `404` si no es `--mock` |

---

## 8. Estructura de Archivos Destino del Repositorio

```text
Proyecto-Final-Sistemas-Embebidos-Lutz-Piazza/
├── platformio.ini
├── PLAN_IMPLEMENTACION_SOFTWARE.md
├── PLAN_CONSTRUCCION_FISICA.md
├── README.md
├── include/
│   ├── protocol.h
│   ├── timer.h
│   ├── stepper_motor.h
│   ├── boton.h
│   ├── ir_sensor.h
│   ├── status_led.h
│   └── i2c_handlers.h
├── src/
│   ├── protocol.cpp
│   ├── timer.cpp
│   ├── stepper_motor.cpp
│   ├── boton.cpp
│   ├── ir_sensor.cpp
│   ├── status_led.cpp
│   ├── i2c_handlers.cpp
│   └── main.cpp
└── raspberry/
    ├── requirements.txt
    ├── run_service.py
    ├── pet-feeder.service
    ├── i2c_master/
    │   ├── __init__.py
    │   ├── protocol.py
    │   ├── transport.py
    │   └── controller.py
    ├── service/
    │   ├── __init__.py
    │   ├── config.py
    │   ├── database.py
    │   ├── client_sync.py
    │   ├── camera.py
    │   ├── worker.py
    │   ├── api.py
    │   └── web/
    │       └── index.html
    └── tests/
        ├── test_protocol.py
        ├── test_database.py
        ├── test_camera.py
        ├── test_worker.py
        └── test_api.py
```

---

## 9. Orquestación para Neon Orchestrator con Swarm

### Reglas de Ejecución del Coordinador
- Los *workers* de cada ola poseen **propiedad exclusiva de archivos** (sin solapamientos) para ejecutarse en paralelo sin conflictos de merge.
- Ningún *worker* puede cambiar códigos de operación, nombres de columnas SQLite ni rutas REST respecto a las Secciones 4–7 sin aprobación del Coordinador.
- Usar `workspace: "share"` para workers autores que compilan/editan en paralelo, o fusionar ordenadamente por olas.

### Ola 0 — Preparación (Coordinador)
- Verificar que `pio` (PlatformIO) y `python3` / `pytest` estén disponibles en el entorno.
- Crear estructura base de carpetas si fuera necesario y anunciar a los workers las rutas de referencia de `laboratorio4-lutz-piazza` y `pruebas proyecto final embebidos`.

### Ola 1 — Implementación Paralela de Núcleos (3 Workers Simultáneos)

#### Worker A: `firmware-avr`
- **Archivos propios:** `platformio.ini`, `include/*.h`, `src/*.cpp`.
- **Tareas:**
  1. Configurar `platformio.ini` para `board = uno`, `platform = atmelavr`, `framework = arduino` (SIN `lib_deps` de `hd44780` ni FreeRTOS).
  2. Copiar y adaptar `stepper_motor.{h,cpp}` y `timer.{h,cpp}` desde `pruebas proyecto final embebidos`.
  3. Implementar `boton.{h,cpp}` (`PD2`/`INT0`, debounce 200 ms, pull-up interno por registros) e `ir_sensor.{h,cpp}` (`PD3`/`INT1`, activo en LOW, debounce 150 ms, `ir_sensor_is_live()`, `ir_sensor_consume_latched()`, `ir_sensor_is_latched()`).
  4. Implementar `status_led.{h,cpp}` (`PB5`/`DDRB`/`PORTB`) usando `timer_create()`.
  5. Implementar `protocol.{h,cpp}` con el Contrato 2 (`[LEN][TYPE][PAYLOAD]`, validando `inBuf[0] == len`) e `i2c_handlers.{h,cpp}` (`Wire.begin(0x08)`, pull-ups `PC4`/`PC5` desactivados por registro, buzón `volatile` de comandos para `loop()`).
  6. Implementar `main.cpp` integrando superloop cooperativo (`timer_loop`, `stepper_loop`, `boton_loop`, `ir_sensor_loop`, `i2c_loop` con máquina de estados de `unjam` y manejo de `manual_dispense_latched`).
- **Criterio de terminado (`done`):**
  - `pio run` compila con código de salida `0` y sin warnings críticos.
  - `grep -rnE '\b(pinMode|digitalWrite|digitalRead|analogRead|attachInterrupt)\b' include/ src/` devuelve **0 coincidencias**.
  - Uso de RAM reportado por PlatformIO `< 50%` (< 1024 bytes de 2048 bytes).

#### Worker B: `rpi-i2c-db`
- **Archivos propios:** `raspberry/requirements.txt`, `raspberry/i2c_master/*.py`, `raspberry/service/{__init__,config,database,client_sync}.py`.
- **Tareas:**
  1. Crear `requirements.txt` (`flask>=2.0.0,<3.2.0`, `smbus2>=0.4.0`, `pytest>=7.0.0`).
  2. Adaptar `i2c_master/protocol.py` al Contrato 2 (mismos opcodes, big-endian helpers y validación estricta de `LEN`).
  3. Adaptar `i2c_master/transport.py`: `SMBus2Transport`, `LinuxI2CDevTransport` y `MockArduinoTransport` realista (simula tiempo de giro del stepper en pasos según `time.monotonic()`, estado `BUSY`, `unjam`, `stop`, `set_config` y método `simulate_ir_detection()`).
  4. Implementar `i2c_master/controller.py` (`FeederMaster` con métodos `ping`, `get_status`, `dispense_steps`, `stop_motor`, `unjam`, `set_config`) y `service/client_sync.py` (`ThreadSafeFeederMaster` con `threading.Lock`).
  5. Implementar `service/config.py` (`ServiceConfig`) y `service/database.py` (esquema completo del Contrato 3, `WAL`, seed idempotente de `settings`, CRUD completo de `schedules`, `dispense_events`, `media_captures`, y helper de poda de medios excedentes).
- **Criterio de terminado (`done`):**
  - Importación limpia de todos los módulos en Python 3 sin hardware I2C.
  - Auto-check con `MockArduinoTransport` pasando todos los comandos y casos `BUSY`/error.

#### Worker C: `rpi-camera`
- **Archivos propios:** `raspberry/service/camera.py`.
- **Tareas:**
  1. Implementar `CameraManager(media_dir: str, mock: bool = False)` con detección automática de backend en orden:
     - `rpicam-still` / `rpicam-vid` (Bookworm+)
     - `libcamera-still` / `libcamera-vid` (Bullseye)
     - `raspistill` / `raspivid` (Legacy Buster)
     - `mock` (si `mock=True` o forzado en tests).
  2. Foto JPEG: captura a `1280x720` (configurable), timeout de subproceso (ej. 15 s), retorna metadatos (`filename`, `media_type="photo"`, `size_bytes`, `duration_s=0`).
  3. Video corto MP4: captura H.264 durante `duration_s` segundos (`1280x720` @ 24 fps) y remuxea con `ffmpeg -y -framerate 24 -i temp.h264 -c copy out.mp4` (borrando el `.h264` temporal) para que sea reproducible nativamente en `<video>` HTML5. Si `rpicam-vid --codec libav` está disponible o falta `ffmpeg`, degradar con mensaje claro sin crashear.
  4. Backend `mock`: genera un JPEG mínimo válido (cabecera `FF D8 ... FF D9`) o un archivo `.mp4` dummy mínimo en `media_dir` sin depender de binarios externos.
  5. Serializar capturas con `threading.Lock` (rechazando con excepción controlada `CameraBusyError` si ya está capturando) y método `prune_old_files(filenames_to_delete)` seguro contra *path traversal*.
- **Criterio de terminado (`done`):**
  - Pruebas rápidas de autodetección, captura mock photo/video, concurrencia (`CameraBusyError`) y borrado seguro.

---

### Ola 2 — Integración de Servicio, Web y Suite de Tests (2 Workers Simultáneos, tras completar Ola 1)

#### Worker D: `rpi-service-web`
- **Archivos propios:** `raspberry/service/{worker,api}.py`, `raspberry/service/web/index.html`, `raspberry/run_service.py`, `raspberry/pet-feeder.service`, `README.md`.
- **Tareas:**
  1. Implementar `service/worker.py` (`FeederWorkerThread`):
     - Polling cada `poll_interval` s (default `0.5 s`): llama `client.get_status(consume_latch=True)`.
     - Sincroniza `steps_per_portion` y `step_interval_us` al Arduino al arrancar (o al reconectar tras fallos I2C).
     - Si `ir_latched`: verifica `ir_cooldown_s`; si pasó el cooldown, encola captura asíncrona en `ThreadPoolExecutor(max_workers=1)` según `capture_mode` (`photo` o `video`), guarda registro en `media_captures` y ejecuta rotación `max_media_files`.
     - Si `manual_dispense_latched`: inserta evento `source='manual_button'` en `dispense_events`.
     - Scheduler de horarios fijos (`schedules`): en cada tick compara `datetime.now().strftime("%H:%M")` y fecha `YYYY-MM-DD`; si coincide con un horario habilitado aún no disparado hoy, marca `last_triggered_date` y dispensa `portions * steps_per_portion` registrando el resultado en `dispense_events`.
     - Scheduler por intervalo (`interval_enabled`): si transcurrieron `>= interval_minutes` desde `last_interval_run_at` (o si nunca corrió, inicializar referencia al habilitar o ejecutar según corresponda — documentar que al habilitar se agenda para `now + interval_minutes`), dispensa `interval_portions * steps_per_portion` y actualiza `last_interval_run_at`.
  2. Implementar `service/api.py` con TODOS los endpoints del Contrato 4 y validaciones de entrada en todos los límites de confianza (formato `HH:MM`, rangos numéricos, saneo de nombres de archivo).
  3. Crear `service/web/index.html`: página simple sin frameworks externos con 5 bloques funcionales:
     - Estado en vivo (autorefresh cada 3 s) + botones **Dispensar ahora**, **Destrabar**, **Parada de emergencia**, **Capturar ahora**.
     - Configuración (pasos por porción, velocidad, modo foto/video, duración video, cooldown IR, retención máxima, modo intervalo).
     - Tabla CRUD de horarios fijos (`HH:MM`, porciones, activar/desactivar, eliminar).
     - Historial reciente de dispensados.
     - Galería de fotos/videos con fecha/hora, disparador (`ir`/`manual`), reproductor inline (`<img>`/`<video controls>`) y botón eliminar.
  4. Crear `run_service.py` (CLI con `--mock`, `--bus`, `--addr`, `--db`, `--media-dir`, `--host`, `--port`, `--poll-interval`, `--debug`, cierre limpio con `SIGINT`/`SIGTERM`), `pet-feeder.service` y `README.md` con guía rápida de compilación/despliegue.
- **Criterio de terminado (`done`):**
  - `python3 raspberry/run_service.py --mock --port 5055` levanta sin errores y responde en `/` y `/api/status`.

#### Worker E: `qa-verification`
- **Archivos propios:** `raspberry/tests/*.py`.
- **Tareas:**
  1. `test_protocol.py`: round-trip de todos los comandos/respuestas (con relleno `0xFF`), paso sin modificaciones de bytes binarios crudos en payload, validación de `LEN` inválido/truncado/exacto, y **test de consistencia C++ vs Python** que parsea `include/protocol.h` y verifica que todos los opcodes coincidan exactamente con `raspberry/i2c_master/protocol.py`.
  2. `test_database.py`: init idempotente, settings defaults y updates, CRUD de `schedules`, historial `dispense_events`, `media_captures` y poda por `max_media_files`.
  3. `test_camera.py`: autodetección de binarios (con `monkeypatch` sobre `shutil.which`), captura `photo`/`video` en modo mock, `CameraBusyError` concurrente y borrado seguro.
  4. `test_worker.py`: simulación de evento IR + verificación de captura y respeto de `ir_cooldown_s`, disparo único de horario fijo `HH:MM`, disparo por intervalo y registro de `manual_button`.
  5. `test_api.py`: tests Flask `test_client()` cubriendo todos los endpoints del Contrato 4 (happy paths, `400`, `404`, `409 BUSY`, `GET /media/<filename>`, `POST /api/mock/ir`).
- **Criterio de terminado (`done`):**
  - `pytest raspberry/tests -v` pasa 100% en modo offline/mock.

---

### Ola 3 — Compuerta Final de Verificación (Coordinador)

Ejecutar y confirmar todos los ítems antes de dar por concluida la implementación:

```bash
# 1. Compilación limpia del firmware ATmega328P
pio run

# 2. Auditoría de registros AVR (debe devolver vacío / exit 1 de grep)
grep -rnE '\b(pinMode|digitalWrite|digitalRead|analogRead|attachInterrupt)\b' include/ src/

# 3. Suite completa de tests Python + consistencia C++/Python
pytest raspberry/tests -v

# 4. Smoke test end-to-end en modo mock
python3 raspberry/run_service.py --mock --port 5055 --db /tmp/test_feeder.db --media-dir /tmp/test_media
# Verificar en otra terminal:
#   curl -s http://localhost:5055/api/status
#   curl -s -X POST http://localhost:5055/api/dispense -H 'Content-Type: application/json' -d '{"portions":1}'
#   curl -s -X POST http://localhost:5055/api/mock/ir
#   curl -s http://localhost:5055/api/media
```

---

## 10. Notas de Despliegue en Raspberry Pi 3B v1.2

```bash
# En la Raspberry Pi:
sudo apt update
sudo apt install -y i2c-tools python3-flask python3-smbus2 ffmpeg

# Habilitar I2C (y cámara en Legacy si corresponde):
sudo raspi-config

# Verificar Arduino en bus I2C (debe aparecer '08'):
i2cdetect -y 1

# Verificar cámara V2 8MP:
rpicam-still -o /tmp/test.jpg   # (o libcamera-still / raspistill según OS)

# Instalar y activar servicio systemd:
sudo cp raspberry/pet-feeder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pet-feeder.service
journalctl -u pet-feeder.service -f
```
