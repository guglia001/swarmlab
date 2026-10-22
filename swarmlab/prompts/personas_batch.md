ESCENARIO
$brief

PREGUNTA DE LA SIMULACIÓN: $question
IDIOMA: $language

SEGMENTOS DE LA POBLACIÓN
$segments

ENTIDADES DEL ESCENARIO (puedes vincular cada persona a 0–2 de estos ids en `entity_links`):
$entity_ids

ARQUETIPOS YA CREADOS — no repitas ninguno, ni su oficio con la misma situación, ni sus nombres:
$existing

CREA EXACTAMENTE $count PERSONAS, una por cada hueco, respetando sus cuotas:
$slots

Para cada persona:
- `slot`: el número de hueco.
- `name`: nombre y apellido verosímiles para el contexto (variedad de origen, género y edad).
- `handle`: usuario de red social, minúsculas, 3–20 caracteres [a-z0-9_], sin @.
- `archetype`: etiqueta única de 4–9 palabras que la distinga de todas las demás
  (p. ej. "fontanera autónoma rural que desconfía de la tecnología").
- `bio`: bio de red social, máximo 160 caracteres, en primera persona y con detalles concretos.
- `age`, `occupation`, `values` (2–4), `traits` (Big Five, 0..1 cada uno).
- `initial_stance`: DENTRO del rango del hueco (−1 en contra de la propuesta, +1 a favor).
- `activity`: DENTRO del rango del hueco (probabilidad de participar en cada ronda).
- `writing_style`: cómo escribe en redes (registro, longitud, tics, emojis o no, faltas o no).
- `entity_links`: ids de la lista de entidades con los que tiene relación (o lista vacía).
