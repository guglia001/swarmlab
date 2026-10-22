LA PROPUESTA
$proposal

PREGUNTA DE LA ENCUESTA: $question
IDIOMA: $language

Primero, una vez para todo el lote:
- `reference_price`: el precio principal que fija la propuesta, como número (si hay varios, el de la
  oferta principal o más habitual); null si no fija ningún precio.
- `price_unit`: la unidad de ese precio (p. ej. "€/mes", "€ por pieza", "€ por proyecto").

AGENTES ($count) — responde por cada uno, en personaje:

$agents

Para cada agente:
- `role`: su papel respecto a la propuesta, decidido por quién es y no por lo que opine:
  cliente (forma parte del público al que se dirige la oferta, la compre o no), socio (podría colaborar
  o proveer), prescriptor (opina ante otros: experto, medio, creador), competidor, observador (no es
  público objetivo).
- `current_alternative`: qué usa, hace o paga hoy en lugar de esto (o "nada").
- `reason`: el motivo principal de sus respuestas, en una frase.
- `deal_breaker`: lo que más le frena.
- `would_change_mind`: qué tendría que pasar para que cambiara de respuesta.
- `choice`: si hoy tuviera que elegir UNA: propuesta (la propuesta a su precio), alternativa (seguir con
  lo que hace hoy) o nada.
- `intent`: ¿la usaría, compraría o colaboraría en los próximos meses? si / probablemente / quiza / no.
- `purchase_probability`: probabilidad de que la compre o contrate en los próximos 6 meses, de 0
  (ninguna) a 10 (segura).
- `would_pay`: ¿pagaría el precio propuesto (o aceptaría sus condiciones económicas)? si / no /
  no_aplica (no aplica: no es público objetivo o la propuesta no se paga).
- `pay_certainty`: si dijo que pagaría, cuánta seguridad tiene de que pagaría de verdad, de 1 a 10; 0
  si no pagaría.
- `max_price`: lo máximo que pagaría por la oferta principal (la de `reference_price`), como número y
  en la misma unidad que `price_unit` (0 si no pagaría nada; null si no aplica). Tiene que cuadrar con
  `would_pay`: si no pagaría el precio propuesto, su máximo es menor que ese precio.
- `fair_price`: el precio o condición que le parecería justo, en sus palabras (o "no aplica").
- `commitment`: si mañana se abriera la reserva: pagaria_ya (pagaría hoy una señal o la primera
  cuota), se_apuntaria (dejaría sus datos para que le avisen) o nada.
