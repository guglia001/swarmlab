---
name: swarmlab
description: Estresa una idea de negocio, un lanzamiento o una política con una población de agentes LLM antes de ejecutarla (SwarmLab). Úsala cuando el usuario quiera saber cómo reaccionaría el público a una propuesta, encontrar objeciones y segmentos receptivos u hostiles, comparar dos versiones de una idea (A/B con la misma población), medir intención de compra con una encuesta en personaje, repetir simulaciones para medir el ruido o calibrar las métricas con casos reales. Cada simulación consume cuota de la suscripción de Claude Code, así que estima antes de lanzar.
argument-hint: "[documento-semilla] [pregunta]"
allowed-tools: Bash(uv run swarmlab:*), Bash(uv run --project *:*), Bash(uv sync:*), Read, Glob, Grep
---

# SwarmLab: estresar una idea con un enjambre de agentes

SwarmLab lee un documento semilla (la idea, el plan, la nota de prensa), genera una población de agentes
con oficio, entorno y personalidad, **mide la primera impresión** de cada uno, los hace conversar varias
rondas en una red social simulada y les hace una **encuesta de compra** con medidas difíciles de inflar.
Todo corre con `claude -p` y la suscripción del usuario: sin API key.

Entrada: `$ARGUMENTS` (ruta del documento semilla y, opcionalmente, la pregunta que debe responder la
simulación). Si falta el documento, pide al usuario la idea en 10–30 líneas (qué es, para quién, a qué
precio y qué hace hoy esa gente en su lugar) y guárdala como `idea.md`.

## Dónde está el comando

- Repositorio clonado (estás dentro de él): `uv run swarmlab …`
- Instalado como plugin: `uv run --project "${CLAUDE_PLUGIN_ROOT}" swarmlab …`. Los runs se guardan en
  `runs/` del directorio actual.

Comprueba la instalación una vez por sesión: `… swarmlab doctor --offline`. Si falla, el usuario necesita
[uv](https://docs.astral.sh/uv/) y Claude Code logueado con su suscripción (`claude auth status`).

## Procedimiento

1. **Lee el documento semilla** y resume en dos líneas qué se va a estresar. Si no dice precio ni
   público, pregunta: sin precio no hay curva de demanda y sin público la población sale genérica.
2. **Estima antes de gastar cuota** y dilo al usuario:
   `… swarmlab estimate --seed <doc> --agents 20 --rounds 5 --survey`.
   Cribado: 20 agentes × 5 rondas (≈ 25 llamadas, ≈ 5 min). Profundidad: 30 × 8 (≈ 45 llamadas, ≈ 9 min).
   Si la estimación supera 60 llamadas, confirma con el usuario antes de lanzar.
3. **Lanza** con la postura inicial medida y la encuesta, que son las dos cosas que hacen la medición útil:
   `… swarmlab run --seed <doc> --prompt "<pregunta>" --agents 20 --rounds 5 --survey --run-id <id> -y`.
   Para cribar muchas ideas añade `--no-report-llm` (solo métricas y encuesta). Para probar sin gastar,
   `--dry-run`. No lances más de 2 simulaciones a la vez.
4. **Lee los resultados** con `… swarmlab show <id>` y `… swarmlab scorecard '<id>*'`, y abre
   `runs/<id>/report.md`. Lo más accionable es lo cualitativo: la objeción con más eco, el mejor y el peor
   segmento, los malentendidos y las narrativas. Cita los posts por su id (`p0034`) y a los agentes por su
   handle, nunca inventes citas.
5. **Antes de comparar ideas o versiones, replica**: `… swarmlab replicate <id> -n 2` y
   `… swarmlab compare '<a>*' '<b>*'`. Los agentes de un run no son independientes; la unidad de réplica
   es el run. Para una versión B con la misma población: `run … --personas-from <id-de-A>`.
6. **Profundiza si el usuario quiere**: `… swarmlab interview <id> <handle>` (entrevista en personaje),
   `… swarmlab chat <id>` (preguntas al analista), `… swarmlab web` (panel en http://127.0.0.1:8008 con
   progreso en vivo, feed, fichas de agentes, encuesta, comparación y calibración).

## Cómo leer las cifras (y qué no decir)

Orden de fiabilidad medido con 8 campañas reales de crowdfunding (AUC éxito/fracaso) y 12 ideas × 2
réplicas (estabilidad):

| Señal (toda la población) | Fiabilidad | Cómo usarla |
|---|---|---|
| Índice de adopción | AUC 1,00 · ICC 0,84 | métrica principal |
| Elige la propuesta frente a su alternativa | AUC 0,97 · ICC 0,65 | métrica principal |
| Probabilidad de compra (Juster 0–10) | AUC 0,94 · ICC 0,93 | métrica principal |
| Compradores firmes / pago con seguridad ≥ 8 | casi siempre 0 % | si sale > 0, señal rara y buena |
| «Clientes que pagarían» y demás métricas de clientes | ICC 0,09–0,46 | ruidosas: no ordenes ideas con ellas |
| % a favor / en contra en modo `cuotas` | – | no es un resultado: lo fija el plan de cuotas |

Reglas al informar:

- Da siempre el **n y el intervalo** (el scorecard los imprime). Con 2 réplicas, solo merecen atención
  diferencias de ≈ 9 puntos de Juster, 0,11 de adopción o 25 puntos de elección.
- El banco de calibración es de productos culturales de 20–30 €. En B2B, tickets altos o servicios, las
  cifras absolutas no están calibradas: compara ideas parecidas entre sí.
- **Nunca presentes la salida como una predicción de ventas.** Son agentes LLM interpretando personajes,
  más generosos que la gente real. Di qué objeciones aparecieron, qué segmento se movió y por qué, y qué
  piloto barato comprobaría la cifra en la realidad.
- Si el usuario va a decidir algo caro con esto, propón apuntar la predicción (qué, cuánto, para cuándo)
  y comprobarla después: ese registro es lo que dice cuánto pesar la herramienta.
