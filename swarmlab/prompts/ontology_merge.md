Estas son las entidades extraídas de distintos fragmentos de un mismo documento ("$doc_title").
Algunas pueden ser la misma entidad con nombres distintos: sinónimos, siglas, singular/plural, nombre
comercial frente a razón social, "la empresa" frente a su marca…

$entity_list

Devuelve los grupos de índices que se refieren a la MISMA entidad y el nombre canónico de cada grupo
(en el idioma del documento). Incluye solo grupos con 2 o más miembros. No fusiones entidades que solo
están relacionadas o que son subconjuntos con intereses distintos (p. ej. "Hacienda" y "asesores
fiscales" son distintas; "autónomos" y "autónomos jóvenes" también, salvo que el texto los trate igual).
Si no hay duplicados, devuelve una lista vacía.
