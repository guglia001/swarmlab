ESCENARIO (contexto para que las personas sean relevantes; no lo juzgues)
$brief

PREGUNTA DE LA SIMULACIÓN: $question
IDIOMA: $language

SEGMENTOS DE LA POBLACIÓN
$segments

ENTIDADES DEL ESCENARIO (puedes vincular cada persona a 0–2 de estos ids en `entity_links`):
$entity_ids

ARQUETIPOS YA CREADOS — no repitas ninguno, ni su oficio con la misma situación, ni sus nombres:
$existing

CREA EXACTAMENTE $count PERSONAS, una por cada hueco:
$slots

Para cada persona:
- `slot`: el número de hueco.
- `name`: nombre y apellido verosímiles para el contexto (variedad de origen, género y edad).
- `handle`: usuario de red social, minúsculas, 3–20 caracteres [a-z0-9_], sin @.
- `archetype`: etiqueta única de 4–9 palabras sobre su vida y su situación, no sobre lo que opina de
  la propuesta (p. ej. "fontanera autónoma rural con dos hijos y poco tiempo").
- `bio`: bio de red social, máximo 160 caracteres, en primera persona y con detalles concretos. Que no
  mencione la propuesta.
- `age` (dentro de la franja orientativa del hueco), `occupation`, `values` (2–4), `traits` (Big Five,
  0..1 cada uno).
- `current_solution`: qué hace, usa o paga hoy para lo que la propuesta pretende resolver, en una
  frase ("nada: no lo necesita" es una respuesta válida y frecuente).
- `writing_style`: cómo escribe en redes (registro, longitud, tics, emojis o no, faltas o no).
- `entity_links`: ids de la lista de entidades con los que tiene relación (o lista vacía).
