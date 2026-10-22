# DECISIONS.md — SwarmLab

Registro de decisiones técnicas. Cada entrada: qué se decidió, por qué y cómo se verificó.

## 0. Paso cero: verificación de `claude -p` (Claude Code 2.1.306)

Verificado el 2026-10-21 con `claude --version`, `claude --help`, la documentación oficial
(<https://code.claude.com/docs/en/headless>, <https://code.claude.com/docs/en/cli-reference>,
<https://code.claude.com/docs/en/errors>) y llamadas reales desde un directorio vacío con un
`CLAUDE.md` "canario" (`/tmp/swarmprobe/sandbox`).

`claude auth status` → `authMethod: claude.ai`, `subscriptionType: max`. No hay `ANTHROPIC_API_KEY`
en el entorno; sí aparece `CLAUDECODE` (heredado de la sesión padre).

### Flags que usamos

| Necesidad | Flag | Verificación |
|---|---|---|
| Modo no interactivo | `-p` / `--print` | prompt por **stdin** (`input=` del subprocess), nunca como argumento |
| Salida JSON | `--output-format json` | un único objeto con `result`, `session_id`, `usage`, `total_cost_usd`, `duration_ms`, `duration_api_ms`, `num_turns`, `is_error`, `subtype`, `api_error_status`, `modelUsage`, `terminal_reason` |
| Modelo por alias | `--model haiku\|sonnet\|opus` | `haiku→claude-haiku-5-5`, `sonnet→claude-sonnet-5-5`, `opus→claude-opus-5-5` (también existe `fable→claude-fable-5-1`) |
| System prompt propio | `--system-prompt <texto>` (o `--system-prompt-file` si es muy largo) | **reemplaza** el prompt de Claude Code (identidad de agente de código fuera) |
| Salida estructurada | `--json-schema <schema>` | devuelve `structured_output` validado por el CLI; acepta `$defs`/`$ref` (probado con un schema anidado tipo pydantic). Schema inválido → `rc=1` y `Error: --json-schema is not a valid JSON Schema` en stderr |
| Sin herramientas | `--tools ""` | el modelo responde `NONE` al pedirle la lista de herramientas y `NO ACCESS` al pedirle leer un fichero del cwd |
| Máximo de turnos | `--max-turns 1` (3 con `--json-schema`, ver §2) | existe aunque `--help` no lo lista (la doc dice que `--help` no muestra todos los flags). Con `--json-schema` y 1 turno funciona si el JSON valida a la primera (el CLI reporta `num_turns: 2` por su herramienta interna de salida estructurada); si no valida, termina en `error_max_turns`, por eso se dan 3 |
| Sin MCP | `--strict-mcp-config` (sin `--mcp-config`) | ningún servidor MCP |
| Sin CLAUDE.md ni customizaciones | `--safe-mode` | sin él, el `CLAUDE.md` canario del cwd **sí se cargaba** (la respuesta acababa en `KIWI-CANARY`) aunque se pase `--system-prompt`. Con `--safe-mode` no se carga CLAUDE.md (ni proyecto ni usuario), skills, plugins, hooks, MCP ni auto-memory |
| Sin settings locales/usuario | `--setting-sources ""` | con solo `--safe-mode` seguía entrando el ajuste `language: spanish` de `~/.claude/settings.json`; con `--setting-sources ""` desaparece (y no corren los hooks `SessionStart`/`UserPromptSubmit` del usuario) |
| Sin slash commands/skills | `--disable-slash-commands` | defensa extra |
| Llamadas de un solo uso | `--no-session-persistence` | no escribe transcripts en `~/.claude/projects` |
| Sesiones reanudables | `--session-id <uuid>` en la 1.ª llamada, `--resume <uuid>` en las siguientes | probado: recuerda el contexto; funciona incluso desde otro `cwd` |
| Esfuerzo (opcional) | `--effort low\|medium\|high…` | configurable por nivel de modelo; por defecto no se pasa |

### Flags descartados

- `--bare`: es el modo recomendado para scripts, **pero** no lee OAuth ni el llavero: exige
  `ANTHROPIC_API_KEY` → facturaría por API. Incompatible con el requisito de usar la suscripción.
- `--max-budget-usd`: estimación de gasto de API; con suscripción no aplica (usamos un tope de
  llamadas propio, `MAX_CALLS_PER_RUN`).
- `--permission-mode` / `--allowedTools`: irrelevantes con `--tools ""`.
- `--restricted`: redundante con `--tools "" --setting-sources ""`.
- `--fallback-model`: preferimos fallar explícitamente y que lo gestione nuestro reintento.

### Entorno del subprocess

Se eliminan del `env` heredado: todas las variables `ANTHROPIC_*` (API key, auth token, base URL,
modelo…), `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY`, `AWS_BEARER_TOKEN_BEDROCK`, `CLAUDE_CODE_SIMPLE`
(activaría el modo bare), `CLAUDECODE` y `CLAUDE_CODE_ENTRYPOINT` (marcadores de sesión padre), y
`OPENAI_API_KEY`. Así el CLI siempre usa el login de claude.ai (suscripción).

### Coste de arranque medido

Llamada trivial con haiku: 2,7–3,2 s de reloj frente a 1,2–1,8 s de `duration_ms` → **~1,5 s de
arranque por proceso**. Sonnet ≈ 2,7 s y opus ≈ 3,9 s para un "ok". `swarmlab doctor` vuelve a medirlo
(secuencial vs. concurrente) y sugiere `MAX_CONCURRENCY`.

### Errores de límite de uso (doc oficial "Error reference")

Mensajes de cuota de suscripción: `You've hit your session limit · resets 3:45pm`,
`You've hit your weekly limit · resets …`, `You've hit your Opus limit`, `You've hit your Sonnet limit`,
`You've hit your … spend/usage limit`, `Request rejected (429)`. Se detectan por texto (`result`/stderr)
+ `is_error`/código de salida, y provocan **pausa con checkpoint**, no fallo.
`Server is temporarily limiting requests (not your usage limit)` y los 5xx/529 son transitorios →
reintento con backoff exponencial + jitter.

## 1. Arquitectura y convenciones

- **Layout plano** `swarmlab/` (como en el enunciado), `uv` + Python 3.12 (`requires-python ~=3.12`).
- **Prompts** en `swarmlab/prompts/*.md` con `string.Template` (`$variable`): las llaves de los
  ejemplos JSON no chocan con el motor de plantillas.
- **Idempotencia por llamada**: cada llamada al LLM de una etapa se cachea en SQLite
  (`llm_cache`, clave = etapa + hash del prompt). Los RNG se derivan de `seed:ronda:propósito`, así que
  al reanudar una ronda interrumpida se regeneran exactamente los mismos prompts y los lotes ya
  resueltos no se vuelven a pagar.
- **Rondas atómicas**: el estado de una ronda solo se confirma (una transacción SQLite) al cerrarla.
  Ctrl+C, límite de uso o tope de llamadas → la ronda en curso no se confirma (pero sus lotes ya
  pagados quedan en `llm_cache`), `status=paused`, y `swarmlab resume` la repite gratis.
- **Estado congelado**: todas las decisiones de la ronda *r* se toman con el snapshot del final de
  *r−1* (+ eventos inyectados al inicio de *r*). Se aplican al cerrar la ronda en orden determinista.
  Las respuestas solo pueden apuntar a posts existentes en ese snapshot.
- **Cambio de postura acotado**: `|Δpostura| ≤ 0.4` por ronda (configurable `MAX_STANCE_STEP`) para
  que un lote ruidoso no invierta a un agente de golpe; se registra el valor pedido por el modelo.
- **Memoria**: resúmenes cada 3 rondas con el modelo ligero (haiku) por lotes de 10: es una tarea de
  compresión, no de razonamiento; el enunciado solo fija el modelo medio para resúmenes de ronda.
- **Narrativas**: agrupación de posts por tema con 1 llamada (modelo medio) al final de la
  simulación; sin LLM (fallback) se usan términos frecuentes por postura.
- **Tope de llamadas**: cada subprocess cuenta (reintentos incluidos). Se reservan 3 llamadas para
  narrativas+informe, de modo que al llegar al tope siempre se puede generar el informe.
- **Informe sin LLM**: si el límite de uso impide generar el informe, se escribe un `report.md`
  provisional determinista (métricas + posts citados) marcado como tal; `swarmlab report` lo rehace.
- **Personas**: plan de cuotas en Python (postura × implicación × segmento) con mínimos garantizados
  (≥15 % escépticos, ≥10 % poco interesados); el LLM rellena perfiles por lotes secuenciales
  recibiendo los arquetipos ya creados. Duplicados detectados (arquetipo normalizado o similitud de bio
  alta) → se regeneran una vez.
- **Eventos** ("modo dios"): YAML o `--interactive` (al cerrar cada ronda se puede escribir un evento
  para la siguiente), o `swarmlab inject <run_id>` sobre un run pausado. Se publican como posts de una
  cuenta de sistema y se fijan arriba del feed en su ronda.
- **Concurrencia**: `asyncio.Semaphore(MAX_CONCURRENCY)` solo alrededor del subprocess (no durante
  el backoff).

## 2. Decisiones tomadas durante la construcción

- **Todas las propiedades del JSON Schema son obligatorias** (2026-10-22). En la primera simulación
  real, haiku devolvía `actions` vacío o lo omitía (el campo tenía valor por defecto y no era
  `required`) mientras su `reasoning` describía respuestas y likes: la ronda 2 quedó con 17
  `do_nothing`. `schema_for_cli` marca ahora todas las claves como `required` (anulables = `null`) y
  quita `default`; `AgentDecision` exige al menos una acción y pone `reasoning` antes de `actions`.
  Los valores por defecto de pydantic se mantienen para construir modelos en Python.
- **Schema dinámico por lote**: el `agent_id` de cada decisión es un `enum` con los ids del lote y la
  lista tiene exactamente una decisión por agente, así el CLI rechaza ids inventados o agentes
  olvidados sin gastar llamadas extra.
- **Ontología de un solo trozo**: si el documento cabe en un trozo se piden 10–22 entidades (4–15 si
  hay varios) para no quedarse corto en documentos semilla breves.
- **A/B con la misma población** (`--personas-from <run>`): se copian las personas y se reconstruye
  el mismo grafo inicial de seguidores (misma semilla); `swarmlab compare` calcula el error típico de
  la postura media de cada run y solo declara ganador si |z| ≥ 2.
- **PDF** con fpdf2 + DejaVu (la fuente que trae matplotlib, así no depende del sistema); se quitan
  los emojis fuera del plano básico Unicode.
- **Web**: el motor corre como tarea asyncio dentro del proceso de uvicorn; el progreso se sirve por
  SSE leyendo `meta` de SQLite (funciona también con runs lanzados desde la CLI).
- **Aviso de términos**: los términos de consumidor de Anthropic (versión EEE) limitan las
  suscripciones Pro/Max a uso no comercial y no automatizado fuera de Claude Code; SwarmLab usa la
  vía documentada (`claude -p`) para uso propio. Ofrecer simulaciones a terceros requeriría la API.
- **`--max-turns 3` solo en llamadas con `--json-schema`** (2026-10-22). Con 1 turno, cuando la
  salida estructurada no validaba a la primera el CLI no podía pedir la corrección (`error_max_turns`)
  y había que relanzar el proceso: en la primera simulación 30×8 fallaron 8 intentos y un lote entero.
  Con 3 turnos el CLI se autocorrige dentro del mismo proceso (no hay herramientas, así que los turnos
  extra solo pueden ser correcciones del JSON). Las llamadas de texto siguen con `--max-turns 1`.
- **`EFFORT_LIGHT=low` por defecto**: haiku pensaba ~3.000 tokens por lote; con `--effort low` el lote
  tarda ~40 % menos (14–20 s frente a 23–34 s) y, con 3 turnos, todos los lotes de prueba validaron.
- **Calibración del cambio de postura**: con la instrucción original ("los cambios de opinión son
  lentos… un anuncio no basta") el cambio medio por decisión era 0,00–0,06 y la postura media no se
  movía en 5–8 rondas, ni con eventos. El prompt da ahora rangos orientativos por tipo de agente
  (0 sin estímulo; 0,1–0,3 indecisos; 0,05–0,15 con opinión; casi 0 muy convencidos; los eventos
  mueven a casi todos). El tope duro `MAX_STANCE_STEP=0.4` se mantiene.

## 3. Medir de verdad (2026-10-22, tarde)

Diagnóstico: en 24 simulaciones de 12 ideas de negocio muy distintas, la postura inicial
media fue +0,04…+0,08, el % a favor 30–43 % y el % en contra 25–30 %. Es justo lo que impone el plan
de cuotas: con 20 agentes da +0,06, 35 % y 25 %; con 30, +0,05, 37 % y 27 %. Y la postura apenas se
movía en las rondas (Δ −0,11…+0,05). Esas métricas eran un dato de entrada, no un resultado. La
única señal que variaba era la encuesta, con 4–13 «clientes» por run.

- **La postura inicial se mide (`STANCE_MODE=impresion`, por defecto).**
  - El LLM crea personas sin opinión sobre la propuesta: segmento según el peso del brief, franja de
    edad y entorno orientativos y `current_solution` (lo que usan hoy). El prompt prohíbe insinuar la
    opinión y no recibe la «postura típica» de cada segmento.
  - Después, cada persona lee la propuesta y da su primera impresión en personaje (reacción, motivo,
    implicación y postura), antes de conversar. Es una llamada al modelo medio por cada 10 agentes.
  - La actividad sale de la implicación medida, y los influencers (~10 %) se eligen entre los muy
    implicados de todo el rango de posturas.
  - `cuotas` sigue disponible como test de estrés (garantiza objeciones). En ese modo el dossier y el
    scorecard avisan de que la distribución inicial no dice nada de la idea.
  - Los runs antiguos se leen como `cuotas`: es el valor por defecto de `RunConfig.stance_mode`.
- **A/B con la misma población en modo impresión:** se conservan las personas, el grafo social
  inicial, los influencers y los seguidores, y se vuelve a medir la primera impresión con la propuesta
  B. `compare` da la diferencia emparejada persona a persona, la medida más limpia entre versiones.
- **«Cliente» en la encuesta es el público objetivo, lo compre o no.** Antes era «la usaría o
  compraría», así que el «% de clientes que pagarían» se calculaba sobre un grupo elegido por su propia
  respuesta. Ese sesgo de selección explica, por ejemplo, un «100 % pagaría» con solo 9 clientes.
- **Encuesta v2.** Añade cinco medidas más difíciles de inflar que el «sí»:
  - elección forzada entre la propuesta, la alternativa actual y nada;
  - escala de Juster 0–10 (Juster, 1966);
  - seguridad del pago, con los «sí» de seguridad < 8 contados como «no» (Champ y Bishop, 2001);
  - compromiso inmediato (pagaría ya / se apuntaría / nada);
  - precio máximo frente al precio de referencia, que el modelo extrae una vez por lote. Con eso se
    dibuja una curva de demanda.

  Además, el prompt lleva un guion de *cheap talk* sobre el sesgo hipotético (Cummings y Taylor, 1999).
  «Compradores firmes» son los que eligen la propuesta, pagarían con seguridad ≥ 8 y pagarían ya. Cada
  proporción va con su IC de Wilson al 95 %. Las respuestas antiguas se siguen resumiendo, con «–» en
  los campos nuevos.
- **La unidad de réplica es el run.**
  - El antiguo `compare` usaba `pstdev/√n` por agente y declaraba ganador con |z| ≥ 2. Los agentes de
    un run no son independientes (lotes de 6–7 en la misma llamada, red, feeds compartidos), así que
    ese error típico era optimista.
  - Ahora, con ≥ 2 réplicas por lado, cada métrica usa una t de Welch sobre las medias de los runs.
    La t se calcula con la beta incompleta, sin scipy.
  - Con un solo run, se usa el ruido entre réplicas guardado por `calibrate`
    (`runs/_calibration/ruido.json`). Si no existe, no hay veredicto.
- **`swarmlab replicate`.**
  - Repite un run con semillas `seed+1…`: mismo documento, pregunta, eventos (también los inyectados)
    y configuración.
  - Las réplicas se llaman `<grupo>-rN` y llevan `replica_of`.
  - Si el run reutilizaba la población de otro y ese otro tiene réplicas, cada réplica se empareja con
    la del mismo número, para un A/B replicado y emparejado.
- **`swarmlab scorecard` y `swarmlab calibrate`.**
  - `scorecard` saca la tabla por idea, con las réplicas agregadas (media ± desviación) y los IC de
    Wilson de los runs sueltos.
  - `calibrate` calcula, por métrica, el ICC(1): la parte de la varianza entre runs que se debe a la
    idea. También da la desviación entre réplicas y la diferencia mínima detectable con 1 o 3 réplicas
    (α = 5 %, potencia 80 %).
  - Con `--outcomes casos.toml`, `calibrate` añade el AUC frente a los éxitos y fracasos reales (con
    p de permutación exacto) y el Spearman con el % del objetivo conseguido.
- **Anuncio de la propuesta en la ronda 1 (`LAUNCH_POST=true`).**
  - El primer run de calibración (una idea de negocio, modo impresión, sin eventos) tuvo 0 posts en las
    rondas 1 y 2: los 12 agentes activos razonaban «feed vacío, nada que comentar» y nadie tenía una
    postura extrema que le empujara a publicar.
  - Con cuotas no pasaba porque el 10 % de críticos duros y el 14 % de entusiastas arrancaban la
    conversación.
  - Ahora la ronda 1 empieza con un post de sistema `@lanzamiento` (one-liner + propuesta) fijado en
    todos los feeds, como cualquier evento: una propuesta real se anuncia.
  - No se copia a las réplicas: cada una lo rehace con su brief. Los runs antiguos no lo tienen
    (`RunConfig.launch_post=False`).
- **`REPORT_LLM=false`** para los runs de medición: sin narrativas ni informe del modelo pesado. Ahorra
  2 llamadas y unos 4 minutos por run. La encuesta sí se hace, y el informe determinista indica el
  motivo.
- **`per_agent_schema`** (llm.py) sustituye a tres copias del mismo patrón: lote con `agent_id` como
  enum y exactamente un elemento por agente. Conserva los nombres `<Lote>_<hash>`, que forman parte
  de la clave de caché.
- **`httpx2` en lugar de `httpx`** en desarrollo: Starlette 1.9 avisa de que `TestClient` con `httpx`
  está obsoleto.
- **El guion de *cheap talk* no empuja hacia el «no».**
  - La primera versión añadía «si duda, lo normal es que no compre». Con ella, las dos primeras ideas
    medidas daban seguridad de pago 4–6 en todos los «sí» y 0 % en cualquier medida con umbral.
  - Se quitó la frase, porque es un empujón sin respaldo en la literatura. Esas dos encuestas se
    repitieron con el prompt corregido (`swarmlab survey`).
  - Se añadieron dos medidas continuas, sin efecto suelo: la probabilidad de compra de Juster de los
    clientes y el pago ponderado por seguridad (cada «sí» pesa su seguridad/10).
- **Calibración con casos reales** (`calibracion/RESULTADOS.md`).
  - Banco: 8 campañas de Verkami (4 éxitos y 4 fracasos en 4 parejas emparejadas) más 4
    ideas de negocio no publicadas. Son 24 runs y 630 llamadas, todas válidas a la primera.
  - El índice de adopción, la elección forzada y Juster de toda la población separan éxitos de
    fracasos (AUC 1,00 / 0,97 / 0,94) y son estables (ICC 0,84 / 0,65 / 0,93).
  - Las métricas de «clientes» son ruidosas (ICC 0,09–0,46). El «% de clientes que pagarían» fue la
    más floja (AUC 0,75).
  - Consecuencia: `scorecard`, `compare` y el prompt del informe ponen primero las tres métricas de
    toda la población, y el dossier lo explica.
  - Las latencias estimadas de la impresión y la encuesta se ajustaron a lo medido (16–22 s por lote
    de 10 con sonnet).

## 4. Skill, plugin y panel web (2026-10-22, noche)

- **SwarmLab se distribuye como skill de Claude Code.** `skills/swarmlab/SKILL.md` enseña a Claude a
  estimar antes de gastar cuota, lanzar con la postura medida y la encuesta, leer los resultados en el
  orden de fiabilidad de la calibración y no venderlos como predicción de ventas. `.claude-plugin/`
  trae el manifiesto y un marketplace de un solo plugin para instalarlo desde el repositorio
  (`claude plugin install swarmlab --marketplace <owner>/<repo>`). En el plugin el comando es
  `uv run --project "${CLAUDE_PLUGIN_ROOT}" swarmlab`, y los runs se guardan en el `runs/` del
  directorio del usuario.
- **Panel web completo** (`swarmlab web`), con la misma pila (FastAPI + HTMX, sin build):
  - panel con KPIs, filtros y selección para comparar o hacer scorecard;
  - formulario de lanzamiento con semilla subida, pegada o de ejemplo, eventos en YAML inline,
    población reutilizada (A/B) y **estimación en vivo** de llamadas y tiempo;
  - cola de ejecución (`WEB_PARALLEL_RUNS`, 2 por defecto) para no abrir más procesos `claude -p` de
    los que la máquina aguanta; encuesta e informe también se lanzan como trabajos en cola;
  - página del run por pestañas, recargadas solas al cerrar cada ronda (SSE): resumen con gráficas SVG
    inline (postura, reparto, segmentos), feed con filtros, agentes con red de seguimiento y ficha
    lateral (trayectoria, decisiones, respuesta de encuesta, posts, entrevista), encuesta completa con
    curva de precio, informe con PDF y descargas, eventos «modo dios» inyectables, chat y registro de
    llamadas;
  - comparar, scorecard y calibración (con guardado del ruido de referencia) y ajustes con `doctor`
    sin llamadas.
  - Las gráficas son SVG generado en el servidor con clases CSS, así que siguen al tema claro/oscuro
    sin JavaScript de gráficas. Los colores de postura (azul a favor, gris indeciso, rojo en contra) se
    validaron para daltonismo y contraste.
- **`swarmlab/replicate.py`** saca la lógica de réplicas de la CLI para que el panel la comparta, y
  `estimate_seed` hace lo mismo con la estimación.
