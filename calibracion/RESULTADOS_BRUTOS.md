# Calibración de SwarmLab

Tablas generadas por `swarmlab calibrate`. Se han quitado las filas de 4 ideas de negocio no
publicadas; las tablas de ruido sí las incluyen (12 ideas, 24 runs).

## Resultados por idea

| Idea (runs) | Modo | Agentes × rondas | Postura inicial → final | Adopción | Eligen la propuesta | Prob. de compra (Juster) | Compradores firmes | Clientes (n) | Clientes que pagarían | Precio máx. mediano |
|---|---|---|---|---|---|---|---|---|---|---|
| cal-c01-comic-folclore-tormentas (×2) | impresion | 20 × 5 | +0.33 ± 0.00 → +0.28 ± 0.02 | +0.42 ± 0.06 | 48% ± 4 | 42% ± 6 | 5% ± 7 | 15/15 | 50% ± 5 | ≈26 € por álbum (ref. 24) |
| cal-c02-novela-thriller-psicologico (×2) | impresion | 20 × 5 | +0.16 ± 0.09 → +0.14 ± 0.08 | +0.28 ± 0.04 | 32% ± 4 | 29% ± 3 | 0% ± 0 | 14/10 | 46% ± 5 | ≈15 € por recompensa (libro dedicado + digital + extras) (ref. 25) |
| cal-c03-juego-quimica-educativo (×2) | impresion | 20 × 5 | +0.11 ± 0.04 → +0.07 ± 0.04 | +0.25 ± 0.04 | 22% ± 4 | 26% ± 1 | 0% ± 0 | 14/16 | 30% ± 2 | ≈20.5 € por copia (ref. 25) |
| cal-c04-album-bestiario-iberico (×2) | impresion | 20 × 5 | +0.34 ± 0.04 → +0.33 ± 0.03 | +0.45 ± 0.03 | 55% ± 7 | 47% ± 2 | 2% ± 4 | 14/17 | 70% ± 9 | ≈29 € por libro (ref. 25) |
| cal-c05-juego-escape-abuela (×2) | impresion | 20 × 5 | +0.29 ± 0.05 → +0.32 ± 0.03 | +0.45 ± 0.00 | 45% ± 0 | 46% ± 0 | 20% ± 0 | 17/14 | 59% ± 8 | ≈30 € por juego (ref. 30) |
| cal-c06-libro-robot-anime (×2) | impresion | 20 × 5 | +0.26 ± 0.03 → +0.21 ± 0.02 | +0.31 ± 0.05 | 38% ± 11 | 35% ± 2 | 0% ± 0 | 12/14 | 61% ± 4 | ≈30 € por ejemplar (con envío) (ref. 28) |
| cal-c07-tebeo-nube-familiar (×2) | impresion | 20 × 5 | +0.33 ± 0.00 → +0.30 ± 0.00 | +0.39 ± 0.08 | 45% ± 14 | 44% ± 5 | 0% ± 0 | 13/15 | 61% ± 1 | ≈22 € por tebeo (ref. 20) |
| cal-c08-novela-fantasia-oscura (×2) | impresion | 20 × 5 | +0.26 ± 0.14 → +0.29 ± 0.14 | +0.41 ± 0.04 | 52% ± 4 | 44% ± 5 | 0% ± 0 | 9/14 | 71% ± 40 | ≈29.25 € por ejemplar (novela en papel con marcapáginas) (ref. 21) |

Adopción, elección forzada y probabilidad de compra (Juster) son de toda la población encuestada: en la calibración con casos reales fueron las más estables entre réplicas y las que mejor separaron éxitos de fracasos (calibracion/RESULTADOS.md); «clientes que pagarían» fue la más floja. Entre corchetes, intervalo de Wilson al 95 %; con réplicas, media ± desviación entre réplicas (en puntos para los %). «Compradores firmes»: eligen la propuesta, pagarían con seguridad ≥ 8/10 y pagarían ya. En modo `cuotas` la postura inicial la fija el plan de cuotas y no dice nada de la idea.

- **cal-c01-comic-folclore-tormentas (×2)** — Ediciones Vientoalto busca 3.500 € en 40 días (todo o nada) para publicar un álbum de cómic de 92 páginas en tapa dura con cinco historias de folclore ibérico, «libre de inteligencia artificial».
  - Mejor segmento: Coleccionistas de cómic y miniaturas (+0.66); peor: Escépticos del micromecenazgo (-0.38)
- **cal-c02-novela-thriller-psicologico (×2)** — Lucía Arregui, autora independiente de Bilbao, pide 3.000 € en 40 días («todo o nada») para editar, maquetar e imprimir su primera novela de thriller psicológico, con recompensas de 10 a 25 €.
  - Mejor segmento: Seguidores de Instagram del libro (+0.45); peor: Escépticos del autopublicado y del sector editorial (-0.43)
- **cal-c03-juego-quimica-educativo (×2)** — Ediciones Abubilla lanza una campaña de 40 días, todo o nada, para financiar 5.000 € de Protocélula, un juego de mesa estratégico y educativo sobre construir moléculas, con packs para centros que incluyen talleres LOMLOE.
  - Mejor segmento: Docentes de ciencias y química (+0.43); peor: Equipos directivos y responsables de centros (-0.20)
- **cal-c04-album-bestiario-iberico (×2)** — Criaturario lanza una campaña «todo o nada» de 4.000 € en 40 días para publicar un libro ilustrado A4 de tapa dura, con estética de álbum de cromos y unos 150 criaturas del folclore y la mitología ibérica, desde 25 €.
  - Mejor segmento: Comunidad friki y de fantasía en redes (+0.60); peor: Escépticos del micromecenazgo y del precio (-0.41)
- **cal-c05-juego-escape-abuela (×2)** — Lince Rojo Juegos lanza en una campaña «todo o nada» de 5.000 € y 40 días un juego de escape y deducción con humor negro para jugar en casa, en el que hay que resolver la muerte de una abuela bingüera.
  - Mejor segmento: Mecenas habituales de Lince Rojo (+0.86); peor: Escépticos de los plazos y del micromecenazgo (-0.61)
- **cal-c06-libro-robot-anime (×2)** — Ediciones Tritón lanza una campaña todo o nada de 4.000 € en 40 días para publicar un libro documental de Luis Ferrán sobre Mazinger Z, con recompensas de 28 € a 75 €.
  - Mejor segmento: Seguidores del blog y comunidad del autor (+0.72); peor: Escépticos del micromecenazgo y del precio (-0.47)
- **cal-c07-tebeo-nube-familiar (×2)** — Ediciones Vientoalto lanza una campaña «todo o nada» de 40 días para financiar con 3.200 € un tebeo de aventuras familiar de 78 páginas en cartoné y color, «libre de inteligencia artificial».
  - Mejor segmento: Mecenas coleccionistas (+0.68); peor: Escépticos de los plazos y del «todo o nada» (-0.14)
- **cal-c08-novela-fantasia-oscura (×2)** — Un librero novel de Ponferrada busca 2.500 € en 40 días, en modalidad todo o nada, para publicar su primera novela de fantasía épica oscura con elementos grimdark, contenido explícito y representación LGTBI, con ediciones especiales y packs para librerías.
  - Mejor segmento: Lectores de fantasía oscura y grimdark (público objetivo) (+0.63); peor: Escépticos del micromecenazgo y de la autoedición (-0.58)

## Ruido entre réplicas frente a diferencias entre ideas

| Métrica | Ideas | Runs | σ entre réplicas | σ entre ideas | ICC | Dif. mínima detectable (1 run / 3 réplicas por idea) |
|---|---|---|---|---|---|---|
| Postura inicial media | 12 | 24 | 0.05 | 0.10 | 0.78 | 0.22 / 0.13 |
| % a favor al inicio | 12 | 24 | 7 pp | 11 pp | 0.70 | 29 pp / 17 pp |
| Postura final media | 12 | 24 | 0.05 | 0.10 | 0.80 | 0.20 / 0.12 |
| Cambio de postura | 12 | 24 | 0.01 | 0.03 | 0.82 | 0.05 / 0.03 |
| % a favor al final | 12 | 24 | 9 pp | 12 pp | 0.67 | 35 pp / 20 pp |
| % en contra al final | 12 | 24 | 5 pp | 7 pp | 0.65 | 22 pp / 13 pp |
| Índice de adopción | 12 | 24 | 0.04 | 0.09 | 0.84 | 0.16 / 0.09 |
| Eligen la propuesta (todos) | 12 | 24 | 9 pp | 12 pp | 0.65 | 36 pp / 21 pp |
| Compradores firmes (todos) | 12 | 24 | 2 pp | 6 pp | 0.86 | 9 pp / 5 pp |
| Probabilidad de compra (Juster, todos) | 12 | 24 | 3 pp | 13 pp | 0.93 | 13 pp / 8 pp |
| Clientes: sí + probablemente | 12 | 24 | 13 pp | 7 pp | 0.22 | 50 pp / 29 pp |
| Clientes: pagarían | 12 | 24 | 14 pp | 13 pp | 0.46 | 55 pp / 32 pp |
| Clientes: pagarían con seguridad ≥ 8 | 12 | 24 | 3 pp | 7 pp | 0.84 | 12 pp / 7 pp |
| Clientes: eligen la propuesta | 12 | 24 | 16 pp | 5 pp | 0.09 | 65 pp / 37 pp |
| Clientes: compradores firmes | 12 | 24 | 3 pp | 7 pp | 0.84 | 12 pp / 7 pp |
| Clientes: probabilidad de compra (Juster) | 12 | 24 | 5 pp | 11 pp | 0.80 | 21 pp / 12 pp |
| Clientes: pago ponderado por seguridad | 12 | 24 | 9 pp | 9 pp | 0.51 | 34 pp / 20 pp |
| Clientes que pagan el precio de referencia | 11 | 22 | 14 pp | 16 pp | 0.56 | 56 pp / 32 pp |

ICC = parte de la variación entre runs que se debe a la idea y no al azar de la simulación (≥ 0,5: la métrica distingue ideas; 0,2–0,5: solo con réplicas; < 0,2: casi todo es ruido). Diferencia mínima detectable: la que hay que ver entre dos ideas para tomarla en serio (α = 5 %, potencia 80 %).

## ¿Separan las métricas los éxitos de los fracasos reales?

| Métrica | Éxitos / fracasos | Media éxitos | Media fracasos | AUC | p (permutación) | Spearman con % conseguido |
|---|---|---|---|---|---|---|
| Postura inicial media | 4 / 4 | +0.31 | +0.22 | 0.84 | 0.07 | +0.48 |
| % a favor al inicio | 4 / 4 | 70% | 61% | 0.75 | 0.16 | +0.18 |
| Postura final media | 4 / 4 | +0.30 | +0.18 | 0.88 | 0.06 | +0.33 |
| Cambio de postura | 4 / 4 | -0.00 | -0.04 | 0.75 | 0.17 | +0.19 |
| % a favor al final | 4 / 4 | 68% | 54% | 0.81 | 0.11 | +0.22 |
| % en contra al final (menos es mejor) | 4 / 4 | 12% | 16% | 0.75 | 0.14 | -0.29 |
| Índice de adopción | 4 / 4 | +0.43 | +0.31 | 1.00 | 0.01 | +0.67 |
| Eligen la propuesta (todos) | 4 / 4 | 50% | 34% | 0.97 | 0.03 | +0.67 |
| Compradores firmes (todos) | 4 / 4 | 7% | 0% | 0.88 | 0.07 | +0.65 |
| Probabilidad de compra (Juster, todos) | 4 / 4 | 44% | 33% | 0.94 | 0.03 | +0.45 |
| Clientes: sí + probablemente | 4 / 4 | 56% | 43% | 0.88 | 0.06 | +0.33 |
| Clientes: pagarían | 4 / 4 | 63% | 50% | 0.75 | 0.17 | +0.17 |
| Clientes: pagarían con seguridad ≥ 8 | 4 / 4 | 9% | 0% | 0.88 | 0.07 | +0.65 |
| Clientes: eligen la propuesta | 4 / 4 | 57% | 43% | 0.88 | 0.06 | +0.33 |
| Clientes: compradores firmes | 4 / 4 | 9% | 0% | 0.88 | 0.07 | +0.65 |
| Clientes: probabilidad de compra (Juster) | 4 / 4 | 49% | 37% | 0.94 | 0.03 | +0.40 |
| Clientes: pago ponderado por seguridad | 4 / 4 | 39% | 28% | 0.88 | 0.04 | +0.29 |
| Clientes que pagan el precio de referencia | 4 / 4 | 63% | 43% | 0.75 | 0.17 | +0.14 |

AUC = probabilidad de que un caso de éxito puntúe mejor que uno de fracaso (0,5 = azar; 1 = los separa perfectamente; por debajo de 0,5, al revés de lo esperado). p = probabilidad de un AUC igual o mayor repartiendo las etiquetas al azar (unilateral). Spearman: correlación de rangos con el % del objetivo conseguido.
