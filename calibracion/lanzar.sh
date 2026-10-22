#!/usr/bin/env bash
# Runs de medición de SwarmLab: cada semilla se simula dos veces (run + 1 réplica con otra semilla
# aleatoria), con la misma pregunta neutra, la postura inicial medida, encuesta y sin informe del LLM.
#
# Uso:   bash calibracion/lanzar.sh PREFIJO PARALELO SEMILLA...
#   bash calibracion/lanzar.sh cal 2 calibracion/casos/*.md
#   bash calibracion/lanzar.sh idea 2 mi-idea.md otra-idea.md
# Luego: uv run swarmlab calibrate 'cal-*' 'idea-*' --outcomes calibracion/casos.toml
#
# Es idempotente: un run completo no se repite; uno pausado (límite de uso) se reanuda.
set -u
cd "$(dirname "$0")/.."
PREFIJO="$1"
PARALELO="$2"
shift 2
CONFIG=calibracion/calibracion.toml
PREGUNTA="¿Cómo reaccionará el público al que se dirige esta propuesta y cuánta gente la apoyaría con su dinero?"
export CONFIG PREGUNTA PREFIJO

estado() {
  .venv/bin/python - "$1" <<'PY'
import sys, sqlite3, json, pathlib
db = pathlib.Path("runs") / sys.argv[1] / "swarm.db"
if not db.is_file():
    print("none"); raise SystemExit
row = sqlite3.connect(db).execute("SELECT value FROM meta WHERE key='status'").fetchone()
print(json.loads(row[0]) if row else "none")
PY
}

uno() {
  local id="$1" seed="$2" st
  st=$(estado "$id")
  if [ "$st" = "none" ]; then
    .venv/bin/swarmlab run -c "$CONFIG" --seed "$seed" --prompt "$PREGUNTA" --run-id "$id" -y >>"runs/.$id.log" 2>&1
  elif [ "$st" != "completed" ]; then
    .venv/bin/swarmlab resume "$id" -c "$CONFIG" >>"runs/.$id.log" 2>&1
  fi
}

caso() {
  local seed="$1" id
  id="$PREFIJO-$(basename "$seed" .md)"
  uno "$id" "$seed"
  if [ "$(estado "$id")" = "completed" ]; then
    if [ "$(estado "$id-r2")" = "none" ]; then
      .venv/bin/swarmlab replicate "$id" -n 1 -c "$CONFIG" >>"runs/.$id-r2.log" 2>&1
    elif [ "$(estado "$id-r2")" != "completed" ]; then
      .venv/bin/swarmlab resume "$id-r2" -c "$CONFIG" >>"runs/.$id-r2.log" 2>&1
    fi
  fi
  echo "$(date +%H:%M) $id: $(estado "$id") / r2: $(estado "$id-r2")"
}
export -f estado uno caso
mkdir -p runs
printf '%s\n' "$@" | xargs -P "$PARALELO" -I{} bash -c 'caso "$@"' _ {}
echo "=== terminado ($(date +%H:%M))"
