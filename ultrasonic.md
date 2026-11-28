# Plan de Refactorización: Sensor IR (`FC-51`) → Sensor Ultrasónico (`HC-SR04`)

> **Propósito:** Especificación ejecutable lista para entregar a un **Neon Orchestrator con swarm**. Define contratos cerrados de hardware/registros AVR, protocolo binario I²C, persistencia SQLite con migración automática, backend/frontend en Raspberry Pi, documentación y compuertas de verificación para reemplazar por completo el sensor infrarrojo por un **HC-SR04**.

---

## 1. Resumen del Cambio

Se reemplaza el sensor IR digital `FC-51` (`D3`/`INT1`, flanco de bajada) por un sensor ultrasónico **`HC-SR04`** (`D4` `TRIG` + `D3` `ECHO`/`INT1`) con medición **100% no bloqueante por registros AVR** (`Timer1` + `INT1`), umbral de distancia configurable por API (`POST /api/config`, default `20 cm`, oculto al usuario final) y telemetría de distancia en vivo (`distance_cm`) visible en el panel de **Debugging**.

El refactor cubre **todo el repositorio**:
- Firmware ATmega328P (`include/`, `src/`)
- Maestro I²C, SQLite, Worker, API Flask y Web UI (`raspberry/`)
- Suite de tests (`raspberry/tests/`)
- Documentación (`README.md`, `PLAN_IMPLEMENTACION_SOFTWARE.md`, `PLAN_CONSTRUCCION_FISICA.md`)

---

## 2. Reglas Duras (Hard Constraints)

El Coordinador del swarm rechazará cualquier cambio que viole estas reglas:

1. **Cero funciones bloqueantes ni helpers Arduino de GPIO:** Prohibido usar `pinMode()`, `digitalWrite()`, `digitalRead()`, `analogRead()`, `attachInterrupt()`, `pulseIn()`, `delay()`, `delayMicroseconds()` en `include/` y `src/`.
   - El pulso `TRIG` de `10 µs` debe temporizarse leyendo `TCNT1` (20 ticks a `0,5 µs/tick`) sin usar `delayMicroseconds()` ni `<util/delay.h>`, garantizando **0 coincidencias** en la auditoría `grep` de `delay*`.
   - `ECHO` se mide exclusivamente por interrupción `INT1` (ambos flancos) + `Timer1` (`TCNT1`), nunca bloqueando `loop()` (para no introducir jitter en los pasos de `1000 µs` del stepper `28BYJ-48`).
2. **Sin cambios en pines ya cableados:** Mantener intactos `D8..D11` (`ULN2003`), `D2` (botón manual), `D13` (LED) y `A4`/`A5` (I²C esclavo `0x08` con pull-ups internas desactivadas).
3. **Protocolo I²C directo sin *byte stuffing*:** Mantener el framing vigente `[LEN] [TYPE] [PAYLOAD...]` (`LEN = 2 + payloadLen`, Big-Endian, `<= 16` bytes, `<= 32` bytes límite `Wire.h`).
4. **Migración segura de `feeder.db`:** `init_db()` debe migrar automáticamente bases existentes con `ir_cooldown_s` → `ultrasonic_cooldown_s` y `media_captures.trigger = 'ir'` → `'ultrasonic'` de forma idempotente.
5. **UX de usuario final preservada:** `ultrasonic_threshold_cm` y `ultrasonic_cooldown_s` (igual que `step_interval_us`) **NO** se muestran ni editan desde la interfaz web; solo se ajustan por `POST /api/config`. La distancia en vivo (`distance_cm`) se muestra únicamente dentro del panel **Debugging** (apagado por defecto).
6. **Cero referencias obsoletas:** Eliminar `include/ir_sensor.h` y `src/ir_sensor.cpp` y no dejar referencias activas a `ir_sensor`, `FC-51`, `HW-201`, `ir_live`, `ir_latched`, `ir_cooldown_s` (salvo la migración en `database.py`), ni `/api/mock/ir`.

---

## 3. Contrato 1 — Hardware y Driver Bare-Metal `HC-SR04` (ATmega328P)

### 3.1 Mapa de Pines y Registros

| Señal `HC-SR04` | Pin Arduino | Pin AVR | Registros | Configuración |
| :--- | :--- | :--- | :--- | :--- |
| **`VCC`** | `5V` | — | — | Alimentación 5 V del Arduino (~15 mA). |
| **`GND`** | `GND` | — | — | Masa común. |
| **`TRIG`** | **`D4`** | `PD4` | `DDRD`, `PORTD` | Salida (`DDRD |= (1 << DDD4)`), reposo `LOW` (`PORTD &= ~(1 << PORTD4)`). Pulso `HIGH` de `10 µs` cada `100 ms`. |
| **`ECHO`** | **`D3`** | `PD3` (`INT1`) | `DDRD`, `PORTD`, `PIND`, `EICRA`, `EIFR`, `EIMSK` | Entrada **sin pull-up interno** (`DDRD &= ~(1 << DDD3); PORTD &= ~(1 << PORTD3);`), salida push-pull 0–5 V del `HC-SR04`. Interrupción `INT1` en **cualquier cambio lógico** (`ISC11 = 0, ISC10 = 1`). |
| **Contador de pulso** | *(Interno)* | `Timer1` (16 bits) | `TCCR1A`, `TCCR1B`, `TCNT1` | Modo normal (`TCCR1A = 0`), prescaler 8 (`TCCR1B = (1 << CS11)`): `16 MHz / 8 = 2 MHz` → **`0,5 µs/tick`**, rango máximo `32,768 ms` (~565 cm). |
| **Disparo periódico** | *(Interno)* | `Timer2` (`timer_create`) | `timer.h` | Timer virtual de **`100 ms`** (> 60 ms mínimo del datasheet para evitar ecos residuales). |

### 3.2 Algoritmo de Medición y Filtrado (`include/ultrasonic_sensor.h` + `src/ultrasonic_sensor.cpp`)

**API pública:**
```cpp
static const uint16_t ULTRASONIC_NO_ECHO_CM          = 0xFFFFU;
static const uint16_t ULTRASONIC_DEFAULT_THRESHOLD_CM = 20U;
static const uint16_t ULTRASONIC_MIN_THRESHOLD_CM     = 5U;
static const uint16_t ULTRASONIC_MAX_THRESHOLD_CM     = 200U;

void ultrasonic_sensor_init(uint16_t threshold_cm);
void ultrasonic_sensor_set_threshold_cm(uint16_t threshold_cm);
uint16_t ultrasonic_sensor_get_threshold_cm(void);
void ultrasonic_sensor_loop(void);
uint16_t ultrasonic_sensor_get_distance_cm(void);
bool ultrasonic_sensor_is_live(void);
bool ultrasonic_sensor_is_latched(void);
bool ultrasonic_sensor_consume_latched(void);
```

**Flujo exacto:**
1. **Inicialización (`ultrasonic_sensor_init`):**
   - Configura `Timer1` (`TCCR1A = 0; TCCR1B = (1 << CS11); TCNT1 = 0;`).
   - Configura `PD4` como salida `LOW`, `PD3` como entrada sin pull-up, e `INT1` por cualquier flanco (`EICRA = (EICRA & ~(1 << ISC11)) | (1 << ISC10); EIFR |= (1 << INTF1); EIMSK |= (1 << INT1);`).
   - Crea e inicia un timer virtual de `100 ms` (`timer_create`) cuyo callback marca una bandera `s_trigger_due = true`.
2. **Disparo en `ultrasonic_sensor_loop()` (cada 100 ms, siempre activo incluso si el motor gira):**
   - Si hay una medición pendiente cuya edad supera `30 ms` (`(uint32_t)(millis() - s_trig_ms) >= 30UL`) sin eco recibido: procesa muestra como timeout (`ULTRASONIC_NO_ECHO_CM = 0xFFFFU`) y libera el estado de espera.
   - Si `s_echo_ready` (la ISR midió un pulso completo): lee atómicamente `ticks = s_echo_ticks`, limpia `s_echo_ready`, valida `ticks >= 232U && ticks <= 46400U` (`2 cm .. 400 cm`, donde `1 cm = 58 µs = 116 ticks` a `0,5 µs/tick`). Si válido: `dist_cm = ticks / 116U`; si fuera de rango: `ULTRASONIC_NO_ECHO_CM`. Actualiza `s_distance_cm` y alimenta el filtro de confirmación/histéresis.
   - Si `s_trigger_due` y no hay medición en curso:
     - Limpia `s_trigger_due`, registra `s_trig_ms = millis()`, marca `s_waiting_echo = true` y `s_echo_rising_seen = false`.
     - Genera pulso `TRIG` de `10 µs` usando `TCNT1`:
       ```cpp
       uint16_t t0 = TCNT1;
       PORTD |= (1 << PORTD4);
       while ((uint16_t)(TCNT1 - t0) < 20U) {}
       PORTD &= ~(1 << PORTD4);
       ```
3. **ISR (`ISR(INT1_vect)`):**
   - Si `(PIND & (1 << PD3)) != 0` (flanco de subida): guarda `s_echo_start = TCNT1; s_echo_rising_seen = true;`.
   - Si `(PIND & (1 << PD3)) == 0` y `s_echo_rising_seen` (flanco de bajada): calcula `s_echo_ticks = (uint16_t)(TCNT1 - s_echo_start); s_echo_rising_seen = false; s_waiting_echo = false; s_echo_ready = true;`.
4. **Filtro anti-ruido e histéresis (`HYSTERESIS_CM = 3U`, `CONFIRM_COUNT = 3U`):**
   - **Cuando `s_live == false`:**
     - Si `dist_cm != ULTRASONIC_NO_ECHO_CM && dist_cm <= s_threshold_cm`: incrementa `s_detect_streak`; al llegar a `3` (~300 ms consecutivos), establece `s_live = true`, `s_latched = true` (atómico) y resetea `s_clear_streak = 0`.
     - En caso contrario: `s_detect_streak = 0`.
   - **Cuando `s_live == true`:**
     - Si `dist_cm == ULTRASONIC_NO_ECHO_CM || dist_cm > (uint16_t)(s_threshold_cm + 3U)`: incrementa `s_clear_streak`; al llegar a `3`, establece `s_live = false` y resetea `s_detect_streak = 0`.
     - En caso contrario: `s_clear_streak = 0`.
5. **LED de estado (`include/status_led.h`, `src/status_led.cpp`, `src/main.cpp`):**
   - Renombrar `STATUS_LED_IR_ACTIVE` → `STATUS_LED_ULTRASONIC_ACTIVE` y `IR_CYCLE_TICKS` → `ULTRASONIC_CYCLE_TICKS`.
   - En `main.cpp`, reemplazar `ir_sensor_*` por `ultrasonic_sensor_*` (inicializando con `ULTRASONIC_DEFAULT_THRESHOLD_CM`).

---

## 4. Contrato 2 — Protocolo Binario I²C (`[LEN][TYPE][PAYLOAD]`)

Se mantiene el framing directo vigente (`LEN = 2 + payloadLen`, Big-Endian, sin *byte stuffing*, `DEFAULT_RX_LEN = 16`, `PROTOCOL_MAX_PAYLOAD = 12`, `PROTOCOL_MAX_FRAME = 32`).

### 4.1 Constantes actualizadas (paridad exacta C++ ↔ Python)

| C++ (`include/protocol.h`) | Python (`raspberry/i2c_master/protocol.py`) | Valor |
| :--- | :--- | :--- |
| `STATUS_ULTRASONIC_LATCHED` | `FLAG_ULTRASONIC_LATCHED` | `0x02` |
| `STATUS_ULTRASONIC_LIVE` | `FLAG_ULTRASONIC_LIVE` | `0x04` |
| `ULTRASONIC_NO_ECHO_CM` | `ULTRASONIC_NO_ECHO_CM` | `0xFFFF` |
| `CONFIG_DEFAULT_THRESHOLD_CM` | `DEFAULT_ULTRASONIC_THRESHOLD_CM` | `20` |
| `CONFIG_MIN_THRESHOLD_CM` | `MIN_ULTRASONIC_THRESHOLD_CM` | `5` |
| `CONFIG_MAX_THRESHOLD_CM` | `MAX_ULTRASONIC_THRESHOLD_CM` | `200` |

*(Eliminar `STATUS_IR_LATCHED`, `STATUS_IR_LIVE`, `FLAG_IR_LATCHED`, `FLAG_IR_LIVE`.)*

### 4.2 Tramas modificadas

1. **`RSP_STATUS (0x90)` — Payload de 8 bytes (`LEN = 10`):**
   - `[0]` `uint8 status_flags`: `bit0=MOTOR_BUSY`, `bit1=ULTRASONIC_LATCHED`, `bit2=ULTRASONIC_LIVE`, `bit3=MANUAL_DISPENSE_LATCHED`.
   - `[1..4]` `int32 steps_remaining` (Big-Endian).
   - `[5]` `uint8 last_error`.
   - `[6..7]` `uint16 distance_cm` (Big-Endian; `0xFFFF` = sin lectura / fuera de rango).
   - Struct Python: `struct.pack/unpack(">BiBH", ...)`.

2. **`CMD_SET_CONFIG (0x14)` y `RSP_CONFIG_ACK (0x94)` — Payload de 8 bytes (`LEN = 10`):**
   - `[0..3]` `uint32 manual_steps` (`256..40960`).
   - `[4..5]` `uint16 step_interval_us` (`1000..10000`).
   - `[6..7]` `uint16 ultrasonic_threshold_cm` (`5..200`).
   - Validación estricta en ambos lados (`pkt.payloadLen == 8`, rango `5..200` para `ultrasonic_threshold_cm`). En `i2c_loop()` de Arduino, aplica `ultrasonic_sensor_set_threshold_cm(snap.cfg_ultrasonic_threshold_cm)`.

---

## 5. Contrato 3 — SQLite y Migración Automática (`raspberry/service/database.py`)

- En `DEFAULT_SETTINGS`:
  - Reemplazar `"ir_cooldown_s": 30` por `"ultrasonic_cooldown_s": 30`.
  - Agregar `"ultrasonic_threshold_cm": 20`.
- En `_INT_SETTING_BOUNDS`:
  - `"ultrasonic_cooldown_s": (5, 600)`
  - `"ultrasonic_threshold_cm": (5, 200)`
- En `VALID_MEDIA_TRIGGERS`:
  - `frozenset({"ultrasonic", "manual"})`
- **Migración idempotente en `init_db(db_path)`** (antes del `INSERT OR IGNORE` de defaults):
  1. Si existe `settings.key = 'ir_cooldown_s'`:
     - Si NO existe `'ultrasonic_cooldown_s'`, copiar el valor (validado) a `'ultrasonic_cooldown_s'`.
     - Borrar la fila vieja `'ir_cooldown_s'`.
  2. Ejecutar `UPDATE media_captures SET trigger = 'ultrasonic' WHERE trigger = 'ir';`.

---

## 6. Contrato 4 — Servicio Python, API REST y Web UI (`raspberry/`)

1. **`raspberry/i2c_master/protocol.py`:**
   - `StatusData`: campos `motor_busy`, `ultrasonic_latched`, `ultrasonic_live`, `manual_dispense_latched`, `steps_remaining`, `last_error`, `distance_cm: Optional[int]` (`None` si raw es `0xFFFF`), `raw_flags`.
   - `encode_status_payload(flags, steps_remaining, last_error, distance_cm=None)` y `decode_status_payload(payload)` (8 bytes `>BiBH`).

2. **`raspberry/i2c_master/transport.py` (`MockArduinoTransport`):**
   - Estado: `ultrasonic_threshold_cm = 20`, `distance_cm = ULTRASONIC_NO_ECHO_CM`, `ultrasonic_latched = False`, `ultrasonic_live = False`.
   - `simulate_ultrasonic_detection(distance_cm: int = 12, live_duration_s: float = 1.0)`: setea `ultrasonic_latched = True`, `ultrasonic_live = True`, `distance_cm` temporal durante `live_duration_s` (luego vuelve a `ULTRASONIC_NO_ECHO_CM`).
   - `CMD_SET_CONFIG` valida y aplica 8 bytes (`manual_steps`, `step_interval_us`, `ultrasonic_threshold_cm`).
   - `CMD_GET_STATUS` devuelve 8 bytes con `distance_cm` actual.

3. **`raspberry/i2c_master/controller.py` y `raspberry/service/client_sync.py`:**
   - `set_config(manual_steps: int, step_interval_us: int, ultrasonic_threshold_cm: int = DEFAULT_ULTRASONIC_THRESHOLD_CM) -> Tuple[int, int, int]`.
   - `simulate_ultrasonic_detection(distance_cm: int = 12, live_duration_s: float = 1.0) -> bool`.

4. **`raspberry/service/worker.py`:**
   - Sincroniza los 3 valores en `set_config(settings["steps_per_portion"], settings["step_interval_us"], settings["ultrasonic_threshold_cm"])`.
   - Reemplaza `status.ir_latched` → `status.ultrasonic_latched`, `ir_cooldown_s` → `ultrasonic_cooldown_s`, `trigger="ir"` → `trigger="ultrasonic"` (tanto en `camera.capture_*` como en `database.add_media_capture`).

5. **`raspberry/service/api.py`:**
   - `GET /api/status`: objeto `arduino` con `ultrasonic_live` y `distance_cm` (`int | None`, `None` si sin lectura o desconectado).
   - `PUT`/`POST /api/config`: soporta `ultrasonic_cooldown_s` y `ultrasonic_threshold_cm`; sincroniza Arduino via `client.set_config(...)` cuando cambia cualquiera de `steps_per_portion`, `turns_per_portion`, `step_interval_us` o `ultrasonic_threshold_cm`.
   - `POST /api/mock/ultrasonic`: reemplaza `/api/mock/ir` (404 si no es `--mock`; opcionalmente acepta `{"distance_cm": 12}` con validación `2..400`).

6. **`raspberry/service/web/index.html`:**
   - En el panel **Debugging** (oculto por defecto):
     - Reemplaza la tarjeta `Sensor IR (vivo)` por **`Sensor ultrasónico`** mostrando:
       - Si `a.distance_cm !== null && a.distance_cm !== undefined`: `${a.distance_cm} cm — ${a.ultrasonic_live ? "Presencia detectada" : "Sin presencia"}`
       - Si `a.distance_cm === null`: `a.ultrasonic_live ? "Presencia detectada" : "Sin lectura"`
     - Reemplaza `Simular IR (mock)` por **`Simular detección ultrasónica (mock)`** llamando a `POST /api/mock/ultrasonic`.
   - En **Galería**: muestra `"Mascota detectada"` para `item.trigger === "ultrasonic"` (y `"Manual"` para `"manual"`).
   - Mantener **ocultos** en toda la interfaz web: `step_interval_us`, `ultrasonic_cooldown_s` y `ultrasonic_threshold_cm` (nunca mostrarlos ni enviarlos desde la web).

---

## 7. Contrato 5 — Actualización de Documentación

Actualizar estos 3 archivos para reflejar el `HC-SR04` de punta a punta:

1. **`README.md`:**
   - Diagrama de arquitectura (`HC-SR04 en plato (PD4 TRIG / PD3 ECHO-INT1)`).
   - Tabla de conexiones (`D4` `TRIG`, `D3` `ECHO/INT1`, `Timer1` + `INT1`).
   - Explicación técnica de registros AVR (`Timer1` + `INT1` ambos flancos).
2. **`PLAN_IMPLEMENTACION_SOFTWARE.md`:**
   - Reemplazar referencias al `FC-51` / `ir_sensor` / `ir_cooldown_s` / `/api/mock/ir` por el diseño final `HC-SR04` (`ultrasonic_sensor`, `Timer1` + `INT1`, `RSP_STATUS` 8 B, `CMD_SET_CONFIG` 8 B, `ultrasonic_cooldown_s`, `ultrasonic_threshold_cm`, `/api/mock/ultrasonic`).
3. **`PLAN_CONSTRUCCION_FISICA.md`:**
   - BOM: reemplazar `FC-51` por `HC-SR04` (4 pines: `VCC`, `TRIG`, `ECHO`, `GND`).
   - Montaje físico junto al plato: altura del hocico/pecho de la mascota, apuntando horizontalmente sobre la zona de alimentación, manteniendo despejado un cono libre (~15°) sin paredes/objetos fijos a menos de `threshold_cm`, y aclarando la ventaja frente al IR (inmune a luz solar directa).
   - Diagrama eléctrico y tabla de cableado (`VCC→5V`, `GND→GND`, `TRIG→D4`, `ECHO→D3`).
   - Paso 4 de calibración: en vez de girar potenciómetro, activar **Debugging** en la web para leer `distance_cm` en vivo y ajustar `ultrasonic_threshold_cm` vía `POST /api/config`.
   - Checklist de defensa técnica actualizado (`Timer1`, `INT1` ambos flancos, pulso `TRIG` de 10 µs sin `delay()`, filtro de 3 lecturas + histéresis de 3 cm).

---

## 8. Orquestación para Neon Orchestrator con Swarm

### Reglas del Coordinador
- Propiedad estrictamente disjunta de archivos por worker en cada ola.
- Dejar `pio run` (con `BypassSandbox: true`) para la Compuerta Final del Coordinador en Ola 3.

### Ola 1 — Refactorización Paralela (3 Workers Simultáneos)

#### Worker A: `ultrasonic-firmware`
- **Archivos propios:**
  - Eliminar: `include/ir_sensor.h`, `src/ir_sensor.cpp`
  - Crear: `include/ultrasonic_sensor.h`, `src/ultrasonic_sensor.cpp`
  - Editar: `include/protocol.h`, `include/status_led.h`, `src/status_led.cpp`, `src/i2c_handlers.cpp`, `src/main.cpp`
- **Tareas:**
  1. Implementar `ultrasonic_sensor.{h,cpp}` según el Contrato 1 (`Timer1` prescaler 8, `INT1` ambos flancos en `PD3`, `TRIG` 10 µs por `TCNT1` en `PD4`, timer virtual `100 ms`, timeout `30 ms`, rango `2..400 cm`, 3 lecturas consecutivas + histéresis `+3 cm`, midiendo siempre).
  2. Actualizar `protocol.h`, `status_led.{h,cpp}`, `i2c_handlers.cpp` (8 bytes en `RSP_STATUS` y `CMD_SET_CONFIG`, sincronizando `ultrasonic_threshold_cm` en `i2c_loop()`) y `main.cpp`.
  3. Eliminar `include/ir_sensor.h` y `src/ir_sensor.cpp`.
- **Criterio de terminado (`done`):**
  - `grep -rnE '\b(pinMode|digitalWrite|digitalRead|analogRead|attachInterrupt|pulseIn|delay|delayMicroseconds)\b' include/ src/` → **0 coincidencias**.
  - `grep -rnE 'ir_sensor|STATUS_IR_|STATUS_LED_IR' include/ src/` → **0 coincidencias**.
  - Compilación host de chequeo (sin errores ni warnings) o listo para `pio run` en Ola 3.

#### Worker B: `ultrasonic-rpi`
- **Archivos propios:**
  - `raspberry/i2c_master/{__init__,protocol,transport,controller}.py`
  - `raspberry/service/{client_sync,database,worker,api}.py`
  - `raspberry/service/web/index.html`
- **Tareas:**
  1. Actualizar `i2c_master/` al Contrato 2 (8 bytes en `RSP_STATUS` y `CMD_SET_CONFIG`, `StatusData.distance_cm`, `MockArduinoTransport.simulate_ultrasonic_detection`).
  2. Actualizar `database.py` al Contrato 3 (`ultrasonic_cooldown_s`, `ultrasonic_threshold_cm`, `VALID_MEDIA_TRIGGERS`, migración automática idempotente en `init_db()`).
  3. Actualizar `worker.py`, `api.py` (`/api/status` con `ultrasonic_live` y `distance_cm`, `PUT`/`POST /api/config`, `POST /api/mock/ultrasonic`) y `web/index.html` (Debugging con distancia en cm + botón mock ultrasónico + badge galería, manteniendo `ultrasonic_threshold_cm` y `ultrasonic_cooldown_s` ocultos en la web).
- **Criterio de terminado (`done`):**
  - `python3 -m py_compile` en todos los `.py` de `raspberry/`.
  - `node --check` sobre el `<script>` de `raspberry/service/web/index.html`.
  - Auto-check rápido en `/tmp` de migración de `feeder.db` vieja (`ir_cooldown_s` + `trigger='ir'`).

#### Worker C: `ultrasonic-docs`
- **Archivos propios:**
  - `README.md`
  - `PLAN_IMPLEMENTACION_SOFTWARE.md`
  - `PLAN_CONSTRUCCION_FISICA.md`
- **Tareas:**
  1. Actualizar los 3 documentos según el Contrato 5 (cableado `HC-SR04`, `Timer1`+`INT1`, `RSP_STATUS`/`CMD_SET_CONFIG` 8 bytes, montaje, calibración con Debugging + `POST /api/config`, checklist de defensa).
- **Criterio de terminado (`done`):**
  - `grep -rnE 'FC-51|HW-201|ir_sensor|ir_cooldown_s|ir_live|ir_latched|/api/mock/ir' README.md PLAN_IMPLEMENTACION_SOFTWARE.md PLAN_CONSTRUCCION_FISICA.md` → **0 coincidencias**.

---

### Ola 2 — Auditoría Cruzada y Suite de Tests (1 Worker Verifier, tras Ola 1)

#### Worker D: `ultrasonic-verifier`
- **Archivos propios:**
  - `raspberry/tests/{test_protocol,test_database,test_worker,test_api}.py`
- **No editar** código de producción ni docs; ante cualquier bug o desalineación, reportar peer-to-peer a `@ultrasonic-firmware`, `@ultrasonic-rpi` o `@ultrasonic-docs` y re-verificar tras el fix.
- **Tareas:**
  1. Auditar paridad completa: `include/protocol.h` ↔ `raspberry/i2c_master/protocol.py`, `src/ultrasonic_sensor.cpp` (0 bloqueos/GPIO/`delay*`, `Timer1`+`INT1`, timeout, filtro 3 lecturas + histéresis +3 cm), `database.py` (migración legacy), `worker.py`, `api.py` ↔ `index.html`, y los 3 documentos `.md`.
  2. Actualizar y ampliar `raspberry/tests/`:
     - `test_protocol.py`: paridad de constantes nuevas (`STATUS_ULTRASONIC_*`, `ULTRASONIC_NO_ECHO_CM`, `CONFIG_*_THRESHOLD_CM`), round-trip de `RSP_STATUS` (8 B con `distance_cm` válido y `0xFFFF`) y `CMD_SET_CONFIG` (8 B), y `simulate_ultrasonic_detection`.
     - `test_database.py`: defaults (`ultrasonic_cooldown_s=30`, `ultrasonic_threshold_cm=20`), rangos, `trigger='ultrasonic'`, y test dedicado de migración automática desde una BD vieja con `ir_cooldown_s=45` y una captura con `trigger='ir'`.
     - `test_worker.py`: detección ultrasónica asíncrona + `ultrasonic_cooldown_s` + sync de los 3 parámetros en `set_config`.
     - `test_api.py`: `GET /api/status` (`ultrasonic_live`, `distance_cm`), `POST /api/config` (`ultrasonic_threshold_cm`, `ultrasonic_cooldown_s`), `POST /api/mock/ultrasonic`, y paridad estática de `index.html` (sin inputs de `ultrasonic_threshold_cm`/`ultrasonic_cooldown_s`, sin referencias viejas a IR).
  3. Ejecutar `python3 -m unittest discover -s raspberry/tests -v`.

---

### Ola 3 — Compuerta Final de Verificación (Coordinador)

```bash
# 1. Compilación real ATmega328P y verificación de Flash/RAM (< 50% SRAM)
pio run

# 2. Auditoría de registros AVR (0 coincidencias)
grep -rnE '\b(pinMode|digitalWrite|digitalRead|analogRead|attachInterrupt|pulseIn|delay|delayMicroseconds)\b' include/ src/

# 3. Auditoría de restos obsoletos de IR (solo debe aparecer la migración en database.py y su test)
grep -rnE 'ir_sensor|FC-51|HW-201|ir_live|ir_latched|STATUS_IR_|FLAG_IR_|/api/mock/ir' include/ src/ raspberry/ README.md PLAN_IMPLEMENTACION_SOFTWARE.md PLAN_CONSTRUCCION_FISICA.md

# 4. Suite completa Python
python3 -m unittest discover -s raspberry/tests -v
```

---

## 9. Pasos de Despliegue tras la Implementación

1. **Cablear el `HC-SR04`** (con todo apagado): `VCC → 5V`, `GND → GND`, `TRIG → D4`, `ECHO → D3`.
2. **Reflashear Arduino UNO** (`pio run -t upload` o `scp .pio/build/uno/firmware.hex` + `avrdude`).
3. **Actualizar `raspberry/` en la Raspberry Pi** y reiniciar `pet-feeder.service` (`sudo systemctl restart pet-feeder.service`). La migración de `feeder.db` corre automáticamente al iniciar.
4. **Calibrar el umbral en vivo:** activar el toggle **Debugging** en la web para leer `distance_cm` frente al plato y ajustar el umbral si hace falta:
   ```bash
   curl -X POST http://<IP_RPI>:5000/api/config \
     -H "Content-Type: application/json" \
     -d '{"ultrasonic_threshold_cm": 20, "ultrasonic_cooldown_s": 30}'
   ```
