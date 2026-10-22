# Calibración de SwarmLab con casos reales (22-10-2026)

## En una frase

Con el método nuevo, tres medidas de la encuesta final de toda la población separaron éxitos y fracasos
reales en un banco pequeño de 8 campañas de crowdfunding:

- **índice de adopción**: AUC 1,00, p exacto 0,014;
- **% que elige la propuesta** frente a su alternativa y a nada: AUC 0,97;
- **probabilidad de compra de Juster**: AUC 0,94.

Esas tres medidas son, además, estables entre réplicas. La métrica en la que se apoyaba el informe de
ideas, el «% de clientes que pagarían», fue la más floja: AUC 0,75 y ruido alto. Es una señal
prometedora con 8 casos de una sola categoría, **no una validación general**.

## Qué se hizo

- **Banco de casos** (`casos/`, `casos.toml`). Ocho campañas reales de Verkami, «todo o nada», de 40
  días y con objetivos de 2.500–5.000 €: cuatro éxitos (160–228 % del objetivo) y cuatro fracasos
  (17–26 %). Forman cuatro parejas del mismo tipo de producto, con objetivo y precios parecidos:
  - A, cómic del mismo editor: c01 éxito / c07 fracaso;
  - B, juego de mesa: c05 / c03;
  - C, libro ilustrado de cultura popular: c04 / c06;
  - D, novela de autor novel: c08 / c02.

  Las semillas están anonimizadas (nombres inventados) y escritas como antes del lanzamiento, sin nada
  que delate el desenlace.
- **Configuración** (`calibracion.toml`):
  - 20 agentes × 5 rondas, con sonnet para ontología, personas, impresión, resúmenes y encuesta, y
    haiku para las decisiones;
  - postura inicial medida (primera impresión), anuncio de la propuesta en la ronda 1 y encuesta
    final v2;
  - sin informe del modelo pesado;
  - la misma pregunta neutra para todos y sin eventos.
- **Réplicas.** Dos runs por caso, con otra semilla y otra población (`lanzar.sh`). Se añadieron 4
  ideas de negocio no publicadas, también con 2 runs cada una, para medir el ruido con 12 ideas.
- **Coste.** 24 runs y 630 llamadas, todas válidas a la primera (sin reintentos). Unas 26 llamadas y
  5,2 minutos por run, con 2 runs en paralelo.

## 1. ¿Da lo mismo si se repite? Ruido entre réplicas (12 ideas × 2 runs)

| Métrica | σ entre réplicas | σ entre ideas | ICC | Diferencia mínima detectable con 2 réplicas por idea |
|---|---|---|---|---|
| Probabilidad de compra (Juster, todos) | 3 pp | 13 pp | **0,93** | 9 pp |
| Compradores firmes (todos) | 2 pp | 6 pp | 0,86 | 6 pp |
| Índice de adopción | 0,04 | 0,09 | **0,84** | 0,11 |
| Postura final media | 0,05 | 0,10 | 0,80 | 0,14 |
| Postura inicial media (primera impresión) | 0,05 | 0,10 | 0,78 | 0,15 |
| Eligen la propuesta (todos) | 9 pp | 12 pp | 0,65 | 25 pp |
| Clientes: pagarían | 14 pp | 13 pp | 0,46 | 39 pp |
| Clientes: sí + probablemente | 13 pp | 7 pp | 0,22 | 35 pp |
| Clientes: eligen la propuesta | 16 pp | 5 pp | 0,09 | 46 pp |

El ICC es la parte de la variación entre runs que se debe a la idea y no al azar de la simulación.

- **Las medidas de toda la población son estables.** Las de «clientes» no lo son: cada run tiene solo
  6–17 clientes potenciales y ese número también cambia de una réplica a otra.
- **La postura ya es una medida.** En el modo antiguo (cuotas) la postura inicial era un dato de
  entrada (+0,04…+0,08 en todas las ideas). Ahora tiene un ICC de 0,78 y va de −0,01 a +0,34 según la
  idea.

## 2. ¿Acierta? Éxitos frente a fracasos reales

| Métrica | Media de los éxitos | Media de los fracasos | AUC | p (permutación) | Parejas en que gana el éxito |
|---|---|---|---|---|---|
| **Índice de adopción** | +0,43 | +0,31 | **1,00** | 0,014 | 4 / 4 |
| **Eligen la propuesta (todos)** | 50 % | 34 % | **0,97** | 0,03 | 4 / 4 |
| **Probabilidad de compra (Juster, todos)** | 45 % | 33 % | **0,94** | 0,03 | 3 / 4 |
| Clientes: probabilidad de compra (Juster) | 49 % | 37 % | 0,94 | 0,03 | 3 / 4 |
| Clientes: pago ponderado por seguridad | 39 % | 28 % | 0,88 | 0,04 | 3 / 4 |
| Postura final media | +0,30 | +0,18 | 0,88 | 0,06 | 3 / 4 |
| Compradores firmes (todos) | 7 % | 0 % | 0,88 | 0,07 | 3 / 4 (una empatada a 0) |
| Postura inicial media | +0,31 | +0,22 | 0,84 | 0,07 | 4 / 4 (en A, por 0,004) |
| Clientes: pagarían | 63 % | 50 % | 0,75 | 0,17 | 3 / 4 |
| Clientes que pagan el precio de referencia | 63 % | 43 % | 0,75 | 0,17 | 3 / 4 |

Valores por caso (media de 2 runs):

| Caso | Real | Adopción | Eligen | Juster | Postura inicial |
|---|---|---|---|---|---|
| c05 juego de escape | éxito 160 % | 0,45 | 45 % | 46 % | +0,29 |
| c04 álbum de monstruos | éxito 225 % | 0,45 | 55 % | 47 % | +0,34 |
| c01 cómic de folclore | éxito 228 % | 0,42 | 48 % | 42 % | +0,33 |
| c08 novela de fantasía | éxito 169 % | 0,41 | 52 % | 44 % | +0,26 |
| c07 tebeo familiar | fracaso 17 % | 0,39 | 45 % | 44 % | +0,33 |
| c06 libro de anime | fracaso 18 % | 0,31 | 38 % | 35 % | +0,26 |
| c02 novela de thriller | fracaso 26 % | 0,28 | 32 % | 29 % | +0,16 |
| c03 juego educativo | fracaso 24 % | 0,25 | 22 % | 26 % | +0,11 |

Cómo leerlo:

- **Tres de las cuatro parejas se separan con claridad** (B, C y D): 13–23 puntos en elección y
  0,13–0,20 en adopción.
- **La pareja A apenas se separa.** Son dos cómics del mismo editor, y el fracaso (c07) puntúa casi como
  el éxito (c01). Lo que hundió a c07 parece estar fuera de la propuesta: público familiar frente a
  adulto, dos autores difundiendo en vez de seis. La simulación no ve audiencias ni esfuerzo de
  difusión.
- **La separación perfecta de la adopción depende del margen entre c08 (0,41) y c07 (0,39).** Esa
  diferencia es menor que el ruido entre réplicas (σ 0,04). Con más casos, el AUC bajará casi seguro.
- **Las medidas con umbral casi nunca se encienden.** «Pagaría con seguridad ≥ 8» y «compradores
  firmes» solo salieron por encima de 0 en tres de los cuatro éxitos (c05 llegó al 20 %). Sirven como
  señal rara y buena, no para ordenar ideas.

## Limitaciones

- **Solo 8 casos.** El intervalo del AUC es ancho y se probaron 18 métricas, aunque muy correlacionadas.
  Lo convincente no es un AUC suelto, sino que todas las métricas apunten en la misma dirección
  (AUC ≥ 0,75).
- **Una sola categoría:** productos culturales de 20–30 € en crowdfunding. No está calibrado para B2B,
  tickets altos, servicios ni suscripciones.
- **Confusores del mundo real:**
  - audiencia previa de los creadores (c01 y c07 son del mismo editor; c05 y c08 tenían comunidad);
  - poca difusión en c02 y c06 (sin actualizaciones);
  - los éxitos son de 2023–2024 y los fracasos de 2025.

  Es posible que la simulación premie, en parte, la calidad del texto de la campaña. Eso también
  cuenta en la realidad, pero no es lo mismo que «la idea».
- **Memoria del modelo.** Son campañas poco conocidas y anonimizadas, pero c06 conserva «Mazinger Z»
  porque es el argumento de venta. El riesgo es bajo, no nulo.

## Qué cambia en el uso de SwarmLab

1. **Métricas principales:** el índice de adopción, la elección forzada y la probabilidad de compra
   (Juster) de toda la población. Es lo que `scorecard` y `compare` enseñan ahora primero.
2. **Al menos 2 réplicas por idea.** Con 2 réplicas, las diferencias mínimas que merece la pena tomar en
   serio son unos 9 puntos de Juster, 0,11 de adopción y 25 puntos de elección.
3. **No ordenar ideas por el «% de clientes que pagarían».** Es la más ruidosa y la que menos acierta.
4. **Referencias del banco**, válidas solo para productos de esta categoría:
   - éxitos: adopción ≈ 0,43, elección ≈ 50 %, Juster ≈ 45 %;
   - fracasos: ≈ 0,31, 34 % y 33 %.

## Reproducir

```bash
bash calibracion/lanzar.sh cal 2 calibracion/casos/c0*.md     # 16 runs
uv run swarmlab calibrate 'cal-*' --outcomes calibracion/casos.toml \
  --out calibracion/RESULTADOS_BRUTOS.md
```

`RESULTADOS_BRUTOS.md` tiene las tablas completas (18 métricas). Las de ruido incluyen las 4 ideas no
publicadas; con solo el banco salen de 8 ideas y 16 runs. `runs/_calibration/ruido.json` es el
ruido de referencia que usa `swarmlab compare` con runs sueltos.

## Siguientes pasos

- **Ampliar el banco a 20 o más casos**, para estrechar el intervalo del AUC y cubrir otras categorías.
  Hacen falta casos de ticket alto, de B2B, de servicios y de suscripciones.
- **Medir el modo antiguo (cuotas) con el mismo banco**, para cuantificar cuánto ha mejorado.
- **Llevar un registro de predicciones:** lo que dice SwarmLab antes de cada piloto y lo que pasó.
