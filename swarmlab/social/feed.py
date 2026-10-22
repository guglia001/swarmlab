"""Algoritmo de feed por agente.

Twitter: eventos de la ronda fijados arriba, notificaciones (respuestas/citas/reposts a lo suyo) y
después una mezcla de ~50 % seguidos, ~30 % populares y ~20 % exploración, ordenada por
recencia + engagement + afinidad de postura. Foro: hilos calientes con sus respuestas recientes.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from swarmlab.social.platform import Platform, Post


class FeedItem(BaseModel):
    post_id: str
    author_id: str
    author_handle: str
    kind: str
    round: int
    text: str
    likes: int
    reposts: int
    replies: int
    quotes: int
    reason: str
    parent_handle: str | None = None
    parent_excerpt: str | None = None

    def render(self) -> str:
        counts = f"♥{self.likes} ↻{self.reposts} 💬{self.replies}" + (
            f" ❝{self.quotes}" if self.quotes else ""
        )
        context = ""
        if self.parent_handle:
            verb = "responde a" if self.kind == "reply" else "cita a"
            context = f" {verb} @{self.parent_handle} «{self.parent_excerpt}»:"
        label = "EVENTO " if self.kind == "event" else ""
        return f"[{self.post_id}] {label}@{self.author_handle} (r{self.round} · {self.reason} · {counts}){context} {self.text}"


def _truncate(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _item(plat: Platform, post: Post, reason: str, max_chars: int) -> FeedItem:
    parent = plat.posts.get(post.parent_id) if post.parent_id else None
    return FeedItem(
        post_id=post.id,
        author_id=post.author_id,
        author_handle=plat.handle(post.author_id),
        kind=post.kind,
        round=post.round,
        text=_truncate(post.text, max_chars),
        likes=post.likes,
        reposts=post.reposts,
        replies=post.replies,
        quotes=post.quotes,
        reason=reason,
        parent_handle=plat.handle(parent.author_id) if parent else None,
        parent_excerpt=_truncate(parent.text, 70) if parent and post.kind in ("reply", "quote") else None,
    )


def _affinity(stances: dict[str, float], a: str, b: str) -> float:
    if a not in stances or b not in stances:
        return 0.5
    return 1 - abs(stances[a] - stances[b]) / 2


def twitter_feed(
    plat: Platform,
    agent_id: str,
    stances: dict[str, float],
    round_no: int,
    rng: random.Random,
    size: int,
    max_chars: int,
) -> list[FeedItem]:
    following = plat.following(agent_id)
    liked = {pid for a, pid in plat.likes if a == agent_id}
    visible = [p for p in sorted(plat.posts.values(), key=lambda p: p.seq) if plat.visible(p.id, round_no)]
    pinned = [p for p in visible if p.kind == "event" and p.round == round_no][:2]
    notifications = [
        p
        for p in visible
        if p.parent_id
        and p.author_id != agent_id
        and p.kind in ("reply", "quote")
        and plat.posts[p.parent_id].author_id == agent_id
        and p.round >= round_no - 2
    ][-2:]
    taken = {p.id for p in pinned + notifications}

    # candidatos: originales (los reposts aportan su original con el motivo "compartido por")
    shared_by: dict[str, str] = {}
    for p in visible:
        if p.kind == "repost" and p.parent_id and p.author_id in following and p.author_id != agent_id:
            shared_by.setdefault(p.parent_id, plat.handle(p.author_id))
    cands = [p for p in visible if p.kind != "repost" and p.author_id != agent_id and p.id not in taken]
    if not cands and not pinned and not notifications:
        return []
    max_eng = max((p.engagement for p in cands), default=0.0)

    def score(p: Post) -> float:
        age = max(round_no - p.round, 0)
        recency = math.exp(-0.6 * max(age - 1, 0))
        eng = math.log1p(p.engagement) / math.log1p(max_eng) if max_eng > 0 else 0.0
        followed = p.author_id in following or p.id in shared_by
        s = (
            0.45 * recency
            + 0.35 * eng
            + 0.2 * _affinity(stances, agent_id, p.author_id)
            + (0.5 if followed else 0.0)
        )
        return s - (0.4 if p.id in liked else 0.0)

    scored = sorted(cands, key=lambda p: (-score(p), p.seq))
    slots = max(size - len(pinned) - len(notifications), 0)
    followed_pool = [p for p in scored if p.author_id in following or p.id in shared_by]
    popular_pool = sorted(
        cands, key=lambda p: (-(p.engagement + 1) * math.exp(-0.4 * (round_no - p.round)), p.seq)
    )
    explore_pool = [p for p in cands if p.author_id not in following]
    rng.shuffle(explore_pool)
    chosen: list[tuple[Post, str]] = []
    seen: set[str] = set()

    def take(pool: list[Post], n: int, reason: str) -> None:
        for p in pool:
            if len([c for c in chosen if c[1] == reason]) >= n or len(chosen) >= slots:
                return
            if p.id not in seen:
                seen.add(p.id)
                chosen.append(
                    (
                        p,
                        f"compartido por @{shared_by[p.id]}"
                        if p.id in shared_by and reason == "sigues"
                        else reason,
                    )
                )

    take(followed_pool, round(slots * 0.5), "sigues")
    take(popular_pool, round(slots * 0.3), "popular")
    take(explore_pool, slots, "descubrir")
    take(scored, slots, "recomendado")
    chosen.sort(key=lambda c: (-score(c[0]), c[0].seq))
    items = [_item(plat, p, "evento", max_chars) for p in pinned]
    items += [_item(plat, p, "te responde", max_chars) for p in notifications]
    items += [_item(plat, p, reason, max_chars) for p, reason in chosen]
    return items[:size]


def forum_feed(
    plat: Platform,
    agent_id: str,
    stances: dict[str, float],
    round_no: int,
    rng: random.Random,
    size: int,
    max_chars: int,
) -> list[FeedItem]:
    visible = [p for p in sorted(plat.posts.values(), key=lambda p: p.seq) if plat.visible(p.id, round_no)]
    root_of: dict[str, str] = {}
    for p in visible:
        parent = p.parent_id
        root_of[p.id] = root_of.get(parent, parent) if parent and p.kind == "reply" else p.id
    threads: dict[str, list[Post]] = {}
    for p in visible:
        threads.setdefault(root_of[p.id], []).append(p)

    def hot(root: str) -> float:
        posts = threads[root]
        last = max(p.round for p in posts)
        activity = sum(1 + p.likes for p in posts)
        pinned = 5.0 if plat.posts[root].kind == "event" and plat.posts[root].round == round_no else 0.0
        return pinned + activity / (1 + max(round_no - last, 0)) ** 1.2 + rng.random() * 0.3

    items: list[FeedItem] = []
    for root in sorted(threads, key=lambda r: -hot(r)):
        posts = threads[root]
        head = plat.posts[root]
        if head.author_id != agent_id or len(posts) > 1:
            items.append(_item(plat, head, "hilo activo", max_chars))
        for reply in [p for p in posts if p.id != root and p.author_id != agent_id][-2:]:
            items.append(_item(plat, reply, "en el hilo", max_chars))
        if len(items) >= size:
            break
    return items[:size]
