DOCUMENTO: $doc_title
PREGUNTA DE LA SIMULACIÓN: $question
IDIOMA: $language

ENTIDADES (id | tipo | postura inicial | influencia | descripción):
$entities

HECHOS ANCLA:
$facts

TENSIONES:
$tensions

INICIO DEL DOCUMENTO:
<<<
$excerpt
>>>

Redacta el brief del escenario:
- `one_liner`: la propuesta en una frase.
- `summary`: 4–8 frases con el contexto necesario para que alguien opine con criterio.
- `proposal`: qué se lanza exactamente (oferta, precio, condiciones) según el documento.
- `key_facts`: 5–10 hechos ancla, los más relevantes para la reacción del público.
- `stakeholders`: 5–12 líneas "actor: qué gana o pierde".
- `tensions`: 3–6 conflictos principales.
- `uncertainties`: 3–6 incógnitas que la simulación debería iluminar.
- `audience_segments`: 4–8 segmentos de la población que reaccionará en redes (público objetivo,
  afectados, críticos, prescriptores, observadores poco implicados), con `weight` (peso aproximado en
  la conversación; la suma ≈ 1), `typical_stance` (−1..1) y los `entity_ids` relacionados de esta
  lista: $entity_ids.
