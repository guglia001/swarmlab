LO QUE SE ESTÁ DISCUTIENDO
$brief

RONDA $round de $rounds (cada ronda ≈ $interval).
$events

AGENTES DE ESTE LOTE ($count) — decide para cada uno por separado:

$agents

Devuelve exactamente una decisión por agente (con su `agent_id`), con 0–$max_actions acciones cada una.
Tipos de acción: post (texto nuevo), reply (responder a un post de su feed), quote (citar un post de su
feed con un comentario), repost (compartir sin comentario), like, follow / unfollow (por handle, sin @),
do_nothing. Usa `target_post_id` para reply/quote/repost/like y `target_handle` para follow/unfollow.
