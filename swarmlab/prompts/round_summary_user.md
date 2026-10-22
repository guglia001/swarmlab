ESCENARIO: $brief
IDIOMA: $language

RONDA $round de $rounds (cada ronda ≈ $interval)
Eventos de la ronda: $events
Agentes activos: $active de $population
Acciones válidas: $actions
Postura media: $mean_before → $mean_after (escala −1 en contra … +1 a favor); distribución: $distribution

POSTS DE LA RONDA (los de más impacto primero):
$posts

CAMBIOS DE POSTURA NOTABLES:
$shifts

Devuelve:
- `narrative`: 3–6 frases con lo que pasó en la ronda.
- `key_post_ids`: 2–5 ids de los posts más influyentes de la lista.
- `emerging_narratives`: 2–4 etiquetas cortas de los temas o argumentos que dominan.
- `notable_agents`: 0–5 handles (sin @) de quienes marcaron la ronda.
- `mood`: una palabra o expresión breve con el clima de la conversación.
