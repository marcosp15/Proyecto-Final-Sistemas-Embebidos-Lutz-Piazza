# Plan de Construcción Física y Montaje — Dispenser Automático de Mascotas

> **Enfoque:** Fabricación práctica y económica para una comisión de 2 estudiantes usando **materiales reciclados y de ferretería** (botellas PET cortadas y termoformadas/planchadas, tubos de PVC o cartón rígido, madera/cartón prensado), sin impresora 3D. Incluye lista de materiales, diseño mecánico adaptado al torque real del `28BYJ-48`, plano eléctrico seguro (3.3 V / 5 V / GND común), calibración y checklist de defensa.

---

## 1. Lista de Materiales (BOM)

### 1.1 Electrónica (lo que ya tienen)
- 1× **Raspberry Pi 3B v1.2** + tarjeta microSD con Raspberry Pi OS + fuente oficial/buena de **5 V / 2.5 A** (micro-USB).
- 1× **Raspberry Pi Camera Board V2 (8 MP)** con su cable flex CSI.
- 1× **Arduino UNO (`ATmega328P-PU`)** + cable USB-A a USB-B (para conectar a la RPi).
- 1× **Stepper motor `28BYJ-48` (5 V)** + módulo driver **`ULN2003`**.
- Cables jumper macho-hembra y macho-macho + mini protoboard (o bornera para distribuir `GND` y `5 V`).

### 1.2 Electrónica a conseguir
- 1× **Sensor infrarrojo de obstáculos digital `FC-51` / `HW-201`** (3 pines: `VCC`, `GND`, `OUT`; con LED indicador y potenciómetro azul de ajuste de distancia; activo en `LOW`).
- 1× **Fuente de 5 V / 1 A a 2 A exclusiva para el `ULN2003`** (puede ser un cargador de celular viejo + cable USB reciclado pelado en rojo `+5 V` / negro `GND`, o un módulo adaptador USB-a-bornera).
- 1× **Pulsador táctil (push button) normalmente abierto** (opcional, para dispensado manual en `D2`).
- 1× **Capacitor electrolítico de 470 µF o 1000 µF (≥ 10 V)** *(muy recomendado)* para colocar en paralelo en los pines de alimentación del `ULN2003` y absorber picos inductivos.
- *(Opcional, buena práctica)* 1× conversor bidireccional de niveles I2C 3.3 V ↔ 5 V (`BSS138`). No es obligatorio si se mantiene el cableado probado del Lab 4 (pull-ups internas del Arduino desactivadas), pero agrega margen de seguridad.

### 1.3 Materiales Mecánicos (Reciclados y Ferretería)
- **Tolva (reservorio):** 1× botella PET grande (2 L o 2.25 L, lisa, tipo gaseosa) o bidón de 3–5 L.
- **Tubo del sinfín:** 1 tramo de **20–25 cm de caño PVC sanitario Ø 40 mm o Ø 50 mm** (recomendado Ø 40 mm por menor peso de comida sobre el motor), o tubo de cartón rígido (rollo de papel aluminio/film grueso) revestido por dentro con una lámina lisa de botella PET para bajar la fricción.
- **Eje del sinfín:** 1 varilla de madera (tarugo/palo de brochette grueso Ø 6–8 mm), tubo de lapicera rígida o varilla roscada liviana de 25 cm.
- **Hélice del sinfín:** 2–3 botellas PET limpias cortadas en láminas/discos y planchadas con cuidado (o goma EVA gruesa / cartón laminado con cinta plástica).
- **Acople al motor:** 3 cm de manguera cristal flexible Ø 5–6 mm o tubo de lapicera + 2 precintos chicos + pegamento epoxi (Poxipol) o silicona caliente.
- **Estructura / chasis:** Caja de cartón doble pared reforzada, madera MDF/fibrofácil reciclada, o base plástica estable con peso en la base (para que no vuelque).
- **Plato de comida:** Recipiente bajo (tupper/bowl) posicionado fijo respecto a la estructura para mantener constante la distancia al sensor IR y a la cámara.
- **Insumos de armado:** Pistola de silicona caliente, cinta aisladora/duct tape, precintos, cúter/tijera fuerte, regla, marcador, plancha (para alisar PET usando papel manteca o trapo encima).
- **Alimento para pruebas:** Alimento balanceado de **grano chico** (gato o perro pequeño, Ø 5–8 mm). **Clave:** el `28BYJ-48` tiene bajo torque (~34 mN·m); granos grandes de perro grande traban cualquier sinfín casero.

---

## 2. División de Trabajo Sugerida (Comisión de a 2)

| Etapa | Integrante A — Mecánica y Chasis | Integrante B — Electrónica, Cableado y Sensores |
| :--- | :--- | :--- |
| **Fase 1** | Cortar tubo de 20–25 cm, fabricar hélice de PET y eje del sinfín, hacer pruebas de giro manual con alimento chico. | Armar el cableado en protoboard/bornera (RPi + Arduino + ULN2003 con fuente separada + botón `D2`), verificar `i2cdetect -y 1` y pruebas de motor en vacío. |
| **Fase 2** | Fabricar acople al `28BYJ-48`, fijar el motor centrado en el extremo trasero del tubo y acoplar la tolva de botella invertida. | Integrar sensor `FC-51` en `D3`, montar Pi Camera V2 con soporte elevado y verificar encuadre/foco y detección IR. |
| **Fase 3 (Juntos)** | Integrar electrónica protegida en la parte superior/trasera de la carcasa, calibrar gramos por vuelta (`steps_per_portion`), probar anti-atasco (`unjam`) y ensayo continuo. |

---

## 3. Diseño Mecánico Paso a Paso (Materiales Reciclados)

### 3.1 Geometría General (Vista Lateral)

```text
                    ┌───────────────┐
                    │   TOLVA PET   │  (Botella 2L invertida,
                    │  (Alimento)   │   embudo con ángulo > 45°)
                    └───┐       ┌───┘
                        │ Boca  │
  ┌──────────┐    ┌─────┴───────┴─────────────┐
  │ 28BYJ-48 │====│=== SINFÍN (Hélice PET) ===│  Tubo PVC / Cartón Ø 40mm
  └──────────┘    └───────────────────────┐   │  (Inclinación 0° a 5° hacia abajo)
   Acople                                 │   │
   centrado                               ▼   ▼  Caída libre al plato
                                      ┌───────────┐
           [Cámara V2] ──(35 cm)──►   │   PLATO   │ ◄── [FC-51 IR] (8–12 cm)
                                      └───────────┘
```

### 3.2 Cómo Fabricar el Tornillo Sinfín con Botellas PET (Método de Discos)
Es el método casero más confiable y rígido sin impresora 3D:

1. **Preparar láminas planas de PET:** Cortar el cilindro central liso de 2 botellas PET, abrirlo en rectángulo y **plancharlo suavemente** entre dos hojas de papel (temperatura baja/media, sin derretir) para quitar la curvatura y darle firmeza.
2. **Cortar discos (arandelas):** Recortar entre **8 y 10 discos** de PET:
   - **Diámetro exterior:** `Ø_tubo - 4 mm` (ej. si el tubo es Ø 40 mm interno, cortar discos de **36 mm** para dejar 2 mm de luz alrededor y evitar rozamiento/atasco).
   - **Agujero interior:** igual al diámetro del eje (ej. **8 mm**).
3. **Abrir y formar la hélice:** Hacer un corte radial recto en cada disco (del borde al centro), desfasar los dos labios unos 20–25 mm para darle forma helicoidal y **pegar el borde final de un disco con el inicio del siguiente** usando pegamento de contacto/epoxi o superglue + bicarbonato (o cinta de empaque resistente por ambos lados).
4. **Fijar al eje:** Enhebrar la espiral continua en la varilla/eje con un paso uniforme de **20–25 mm por vuelta** (suficiente para 2–3 granos chicos sin comprimirse) y fijar cada media vuelta al eje con un punto de silicona caliente o epoxi.
5. **Apoyo delantero (opcional pero recomendado):** Para evitar que el eje cuelgue y raspe el tubo, colocar un travesaño fino de alambre rígido en la boca de salida con un aro central donde apoye la punta del eje (sin obstruir la caída de alimento).

### 3.3 Acople al Eje del `28BYJ-48` (Punto Crítico de Torque)
El eje del `28BYJ-48` tiene **5 mm de diámetro con dos caras planas** (3 mm entre caras):
1. Cortar 3 cm de manguera cristal o tubo de lapicera que entre muy justo en el eje del motor.
2. Hacer un pequeño orificio pasante transversal (o rellenar las caras planas con epoxi moldeado sobre el eje engrasado con vaselina para que sea desmontable) y asegurar con un precinto bien apretado.
3. Unir el otro extremo al eje del sinfín cuidando la **alineación axial** (si gira chueco, la fricción lateral frena al `28BYJ-48`).
4. Fijar el motor usando sus dos orejas con tornillos chicos o precintos a una tapa de cartón prensado/madera pegada al fondo del tubo. **Importante:** dejar 2 mm de espacio libre detrás de la primera espira para que no raspe la tapa trasera.

### 3.4 Unión Tolva–Tubo (Evitar "Puentes" y Atascos)
- Practicar una ventana ovalada en la parte superior del tubo (unos 35 × 35 mm), ubicada a ~3–4 cm del motor.
- **Truco anti-atasco:** redondear o biselar el borde delantero de la ventana del tubo (o pegar una lengüeta flexible de goma EVA/PET blando justo en el borde superior donde pasan los granos). Así, si un grano queda atrapado entre la hélice y el borde del tubo, la lengüeta cede en lugar de clavar el motor.
- Encajar el pico/embudo de la botella tolva sobre esa ventana y sellar por fuera con silicona caliente. Dejar la parte superior de la botella con tapa desmontable para recargar el alimento.

### 3.5 Soporte de Cámara V2 y Sensor IR `FC-51`
- **Pi Camera V2 (8 MP):**
  - Es de **foco fijo** (nitidez óptima a partir de **35–40 cm**). Montarla en un brazo/mástil de cartón rígido o PVC a **30–40 cm de altura**, inclinada ~35° hacia el plato.
  - Cuidar el cable flex CSI: no doblarlo a 90° filoso ni pegarlo junto a los cables de bobinas del stepper (ruido EMI).
- **Sensor IR `FC-51`:**
  - Montar en un lateral del plato a la altura del hocico/pecho de la mascota (**8–15 cm del centro del plato**), apuntando **horizontalmente** sobre el borde del plato (nunca apuntando hacia la comida dentro del plato, porque dispararía todo el tiempo).
  - Evitar luz solar directa sobre el fotodiodo receptor (si hay ventana cerca, agregar una pequeña visera/tubito de cartón negro de 1–2 cm alrededor de los LEDs del `FC-51`).
- **Compartimento electrónico:** Ubicar RPi, Arduino y `ULN2003` en un compartimento superior o trasero **separado físicamente del polvo del alimento** y con ranuras de ventilación (especialmente sobre el `ULN2003` y el SoC de la RPi).

---

## 4. Plano Eléctrico y Tabla de Conexiones

### 4.1 Diagrama de Alimentación y Señales

```text
  [Fuente 5V 2.5A]                                   [Fuente 5V 1A–2A]
   (micro-USB)                                     (EXCLUSIVA para motor)
        │                                                │         │
        ▼                                               +5V       GND
 ┌──────────────────────┐                                │         │
 │  Raspberry Pi 3B     │                                ├──[470uF]┤ (electrolítico)
 │                      │                                ▼         ▼
 │  USB-A ──────────────┼───(Cable USB)───► [Arduino UNO]    [Módulo ULN2003]
 │                      │                   │  D8..D11  ├──► IN1..IN4 ──► [28BYJ-48]
 │  Pin 3 (GPIO2 / SDA) ├───────────────────┤ A4 (SDA)  │
 │  Pin 5 (GPIO3 / SCL) ├───────────────────┤ A5 (SCL)  │
 │  Pin 6 (GND)         ├──────────┬────────┤ GND       ├───────────────── GND (COMÚN)
 │                      │          │        │           │
 │  CSI Ribbon ──► [Pi Camera V2]  │        │  D3 (INT1)│◄── OUT [Sensor FC-51]
 └──────────────────────┘          │        │  5V / GND ├──► VCC / GND [FC-51]
                                   │        │           │
                                   │        │  D2 (INT0)│◄── [Botón manual] ── GND
                                   │        │  D13 (LED)│ (LED integrado en placa)
                                   └────────┴───────────┘
```

### 4.2 Tabla Exacta de Cableado

| Señal | Origen | Destino | Notas Críticas |
| :--- | :--- | :--- | :--- |
| **I2C SDA** | RPi Pin 3 (`GPIO2`) | Arduino `A4` (`PC4`) | Pull-ups internas del Arduino desactivadas en firmware. |
| **I2C SCL** | RPi Pin 5 (`GPIO3`) | Arduino `A5` (`PC5`) | Cables cortos (< 25 cm) para evitar ruido. |
| **Masa común (`GND`)** | RPi Pin 6 (`GND`) | Arduino `GND` + `ULN2003 (-)` + `FC-51 GND` + Botón | **Obligatorio** unir todas las masas en un punto común; sin GND común I2C y el ULN2003 fallan. |
| **Stepper IN1..IN4** | Arduino `D8, D9, D10, D11` | `ULN2003 IN1, IN2, IN3, IN4` | Mantiene compatibilidad exacta con `PORTB` (`PB0..PB3`). |
| **Potencia Motor** | Fuente 5 V externa (`+` / `-`) | `ULN2003 (+5V / -)` | Verificar que el jumper del módulo `ULN2003` esté puesto. **NO conectar `+5V` de esta fuente al `5V` de la RPi ni del Arduino.** |
| **Sensor IR `FC-51`** | Arduino `5V`, `GND`, `D3` | `FC-51 VCC, GND, OUT` | Consumo bajo (~20 mA), puede tomar 5 V del Arduino. |
| **Botón manual** | Arduino `D2` (`INT0`) | Un terminal del pulsador; el otro a `GND` | Usa pull-up interno de `PD2` (no requiere resistencia externa). |
| **Cámara V2** | Conector `CAMERA` (CSI) de RPi | Pi Camera V2 | Instalar con la RPi **apagada**. Contactos plateados hacia el puerto HDMI (lado azul hacia los puertos USB/Ethernet). |

> **Advertencias de seguridad eléctrica:**
> 1. Nunca conectes el pin `5V` del `ULN2003` a la salida `5V` de la Raspberry Pi ni al pin `5V` del Arduino: la corriente de arranque (~400 mA) genera caídas de tensión y reinicios.
> 2. Nunca alimentes un pin GPIO de la Raspberry Pi con 5 V. Antes de conectar `A4`/`A5` a la RPi, asegurate de tener flasheado en el Arduino el firmware del proyecto (que apaga las pull-ups internas de `PC4`/`PC5`), y nunca corras un sketch viejo que active `Wire` sin apagarlas.

---

## 5. Pruebas de Banco y Calibración (En Orden Estricto)

### Paso 1 — Verificación Eléctrica y Bus I2C (Sin motor cargado)
1. Flashear el firmware del Arduino desde la PC (o desde la RPi por USB) con `pio run -t upload`.
2. Con todo apagado, conectar `SDA`, `SCL` y `GND` entre RPi y Arduino.
3. Encender la RPi y ejecutar:
   ```bash
   i2cdetect -y 1
   ```
   Debe aparecer `08`.
4. Verificar el LED `D13` del Arduino parpadeando lento (1 Hz = superloop vivo).

### Paso 2 — Prueba del Stepper y `ULN2003` (Sin alimento)
1. Conectar la fuente 5 V dedicada al `ULN2003` (con GND común).
2. Desde la web (`http://<IP_RPI>:5000`), probar **Dispensar 1 porción** (4096 pasos = 1 vuelta) y **Destrabar**.
3. Observar durante 2 minutos:
   - Que el giro sea suave y en el sentido correcto hacia la salida (si gira al revés, invertir el orden de los 4 cables `D8..D11` o el signo de pasos).
   - Que al terminar, los 4 LEDs rojos del módulo `ULN2003` se **apaguen por completo** (confirmando que las bobinas se desenergizaron y no se recalienta).

### Paso 3 — Calibración de Porciones con Alimento Real
1. Llenar la tolva con alimento balanceado de grano chico y hacer 2 vueltas iniciales para purgar/llenar el sinfín.
2. Colocar un vaso/plato sobre una balanza de cocina (o medir con vaso medidor).
3. Disparar **5 veces** `1 porción` (4096 pasos) desde la web, anotando los gramos caídos en cada vuelta y si hubo algún atascamiento:
   - Ejemplo: si 1 vuelta entrega ~12 g y querés porciones de ~30 g → `steps_per_portion = round(4096 * 30 / 12) = 10240`.
   - Si el motor pierde pasos bajo carga, subir `step_interval_us` de `2000` a `2500` o `3000` µs en `/api/config` (más lento = más torque efectivo).
4. Probar el botón **Destrabar** y el botón físico `D2`.

### Paso 4 — Ajuste del Sensor IR `FC-51` y Cámara V2
1. Con la mano (o un objeto del tamaño de la mascota) a ~10–15 cm frente al plato, girar suavemente el potenciómetro azul del `FC-51` hasta que el LED de detección verde/rojo se encienda **solo** cuando el objeto entra en la zona del plato y se apague al retirarlo.
2. Verificar en la web que al pasar la mano:
   - Aparece la foto (o video de 5 s si cambiaste a modo `video`) en la galería con fecha y hora correctas.
   - Si pasás la mano de nuevo antes del `ir_cooldown_s` (30 s), NO dispara otra captura repetida.
3. Revisar la foto capturada para corregir el ángulo y distancia de la cámara (confirmar buena iluminación y nitidez).

---

## 6. Checklist Pre-Defensa (Sistemas Embebidos)

- [ ] **Hora real (NTP):** verificar con `date` en la RPi que la fecha/hora local sea correcta antes de la demo.
- [ ] **Demostración en vivo rápida:** crear un horario fijo 2 minutos adelante (o modo intervalo de 1 min) para mostrar el dispensado automático sin esperar horas.
- [ ] **Simulación de mascota:** acercar mano/peluche al `FC-51` y mostrar la foto/video recién capturado en la galería web con timestamp.
- [ ] **Control de atasco y emergencia:** mostrar botón manual `D2`, "Destrabar" y "Parada de emergencia" en la web.
- [ ] **Respaldo de red:** llevar configurado hotspot del celular en la RPi por si el Wi-Fi de la facultad bloquea conexiones locales entre dispositivos (AP isolation).
- [ ] **Defensa técnica lista:** saber explicar manejo de registros AVR (`DDRB/PORTB`, `EICRA/EIMSK`, `TCCR2A/B`), por qué se desactivan pull-ups en `PC4/PC5`, por qué el superloop cooperativo entra sobrado en 2 KB de SRAM sin FreeRTOS, y cómo el *byte stuffing* + *mutex* + *latch* de IR garantizan robustez en I2C.
