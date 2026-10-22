DOCUMENTO: $doc_title
PREGUNTA QUE QUEREMOS RESPONDER CON LA SIMULACIÓN: $question
IDIOMA DE SALIDA: $language
FRAGMENTO $chunk_pos de $chunk_total — "$chunk_title"

<<<FRAGMENTO
$chunk_text
FRAGMENTO>>>

Extrae del fragmento entre $min_entities y $max_entities entidades, sus relaciones, los hechos ancla y
las tensiones.

- Tipos de entidad permitidos: $entity_types.
- En `relations` y en `tensions.parties` usa exactamente el `name` de las entidades que has extraído.
- `facts`: 3–10 hechos concretos del fragmento, con `importance` 0..1.
- `tensions`: 1–5 conflictos de intereses, con `intensity` 0..1.
