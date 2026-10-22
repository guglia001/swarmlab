"""Plataforma social simulada: cuentas, posts, likes, reposts, citas, respuestas y follows.

``Platform`` es la base abstracta (estado + aplicación de acciones + persistencia incremental);
``TwitterPlatform`` es la red tipo X del MVP y ``ForumPlatform`` la variante tipo foro.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from swarmlab.social.actions import ActionOutcome, AgentDecision

if TYPE_CHECKING:
    from swarmlab.social.feed import FeedItem
    from swarmlab.store import RunStore

PostKind = Literal["post", "reply", "repost", "quote", "event"]
SYSTEM_PREFIX = "sys:"


class Post(BaseModel):
    id: str
    round: int
    seq: int
    author_id: str
    kind: PostKind
    text: str = ""
    parent_id: str | None = None
    likes: int = 0
    reposts: int = 0
    quotes: int = 0
    replies: int = 0
    author_stance: float | None = None

    @property
    def engagement(self) -> float:
        return self.likes + 2.0 * self.reposts + 1.5 * self.quotes + 1.0 * self.replies


class Account(BaseModel):
    id: str
    handle: str
    name: str
    kind: Literal["agent", "system"] = "agent"


@dataclass
class Changes:
    """Delta pendiente de persistir desde el último checkpoint."""

    new_posts: list[str] = field(default_factory=list)
    dirty_posts: set[str] = field(default_factory=set)
    new_likes: list[tuple[str, str, int]] = field(default_factory=list)
    follows_added: list[tuple[str, str, int]] = field(default_factory=list)
    follows_removed: list[tuple[str, str]] = field(default_factory=list)


class Platform(ABC):
    """Estado mutable de la red. Las decisiones de la ronda *r* se validan contra el estado al final de
    *r−1* (más los eventos de *r*), de modo que nadie puede reaccionar a lo publicado en su misma ronda."""

    name: str = "base"
    allowed_actions: tuple[str, ...] = (
        "post",
        "reply",
        "repost",
        "quote",
        "like",
        "follow",
        "unfollow",
        "do_nothing",
    )

    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}
        self.by_handle: dict[str, str] = {}
        self.posts: dict[str, Post] = {}
        self.likes: set[tuple[str, str]] = set()
        self.follows: set[tuple[str, str]] = set()
        self.seq = 0
        self.changes = Changes()

    # -- cuentas -----------------------------------------------------------------------------------

    def add_account(self, account: Account) -> None:
        self.accounts[account.id] = account
        self.by_handle[account.handle.lower()] = account.id

    def handle(self, account_id: str) -> str:
        acc = self.accounts.get(account_id)
        return acc.handle if acc else account_id

    def system_account(self, handle: str, name: str) -> Account:
        acc_id = f"{SYSTEM_PREFIX}{handle.lower()}"
        if acc_id not in self.accounts:
            self.add_account(Account(id=acc_id, handle=handle, name=name, kind="system"))
        return self.accounts[acc_id]

    # -- consultas ---------------------------------------------------------------------------------

    def following(self, agent_id: str) -> set[str]:
        return {b for a, b in self.follows if a == agent_id}

    def followers(self, agent_id: str) -> set[str]:
        return {a for a, b in self.follows if b == agent_id}

    def follower_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(self.accounts, 0)
        for _, b in self.follows:
            counts[b] = counts.get(b, 0) + 1
        return counts

    def posts_in_round(self, round_no: int) -> list[Post]:
        return [p for p in self.posts.values() if p.round == round_no]

    def visible(self, post_id: str, round_no: int) -> bool:
        p = self.posts.get(post_id)
        return p is not None and (p.round < round_no or (p.kind == "event" and p.round <= round_no))

    # -- mutaciones --------------------------------------------------------------------------------

    def _new_post(
        self,
        author_id: str,
        round_no: int,
        kind: PostKind,
        text: str,
        parent_id: str | None,
        stance: float | None,
    ) -> Post:
        self.seq += 1
        post = Post(
            id=f"p{self.seq:04d}",
            round=round_no,
            seq=self.seq,
            author_id=author_id,
            kind=kind,
            text=text,
            parent_id=parent_id,
            author_stance=stance,
        )
        self.posts[post.id] = post
        self.changes.new_posts.append(post.id)
        return post

    def publish_event(self, round_no: int, handle: str, name: str, text: str) -> Post:
        acc = self.system_account(handle, name)
        return self._new_post(acc.id, round_no, "event", text, None, None)

    def _bump(self, post_id: str, attr: str) -> None:
        post = self.posts[post_id]
        setattr(post, attr, getattr(post, attr) + 1)
        self.changes.dirty_posts.add(post_id)

    def add_follow(self, follower: str, followee: str, round_no: int) -> None:
        if (follower, followee) not in self.follows and follower != followee:
            self.follows.add((follower, followee))
            self.changes.follows_added.append((follower, followee, round_no))

    def apply_decision(
        self, decision: AgentDecision, round_no: int, stance: float, max_actions: int, max_chars: int
    ) -> list[ActionOutcome]:
        """Aplica las acciones de un agente; las inválidas se registran con su motivo y no tienen efecto."""
        outcomes: list[ActionOutcome] = []
        agent = decision.agent_id
        actions = [a for a in decision.actions if a.type != "do_nothing"][:max_actions]
        if not actions:
            return [ActionOutcome(round=round_no, agent_id=agent, type="do_nothing")]
        for act in actions:
            out = ActionOutcome(
                round=round_no,
                agent_id=agent,
                type=act.type,
                target_post_id=act.target_post_id,
                target_handle=act.target_handle,
            )
            reason = self._validate(
                agent, act.type, act.text, act.target_post_id, act.target_handle, round_no
            )
            if reason:
                out.valid, out.reason = False, reason
                outcomes.append(out)
                continue
            text = (act.text or "").strip()[:max_chars]
            if act.type == "post":
                out.post_id = self._new_post(agent, round_no, "post", text, None, stance).id
            elif act.type in ("reply", "quote") and act.target_post_id:
                out.post_id = self._new_post(agent, round_no, act.type, text, act.target_post_id, stance).id
                self._bump(act.target_post_id, "replies" if act.type == "reply" else "quotes")
            elif act.type == "repost" and act.target_post_id:
                out.post_id = self._new_post(agent, round_no, "repost", "", act.target_post_id, stance).id
                self._bump(act.target_post_id, "reposts")
            elif act.type == "like" and act.target_post_id:
                self.likes.add((agent, act.target_post_id))
                self.changes.new_likes.append((agent, act.target_post_id, round_no))
                self._bump(act.target_post_id, "likes")
            elif act.type == "follow" and act.target_handle:
                self.add_follow(agent, self.by_handle[act.target_handle], round_no)
            elif act.type == "unfollow" and act.target_handle:
                pair = (agent, self.by_handle[act.target_handle])
                self.follows.discard(pair)
                self.changes.follows_removed.append(pair)
            outcomes.append(out)
        return outcomes

    def _validate(
        self,
        agent: str,
        kind: str,
        text: str | None,
        target_post: str | None,
        target_handle: str | None,
        round_no: int,
    ) -> str | None:
        if kind not in self.allowed_actions:
            return f"acción {kind} no disponible en {self.name}"
        if kind in ("post", "reply", "quote") and not (text or "").strip():
            return "texto vacío"
        if kind in ("reply", "repost", "quote", "like"):
            if not target_post:
                return "falta target_post_id"
            if not self.visible(target_post, round_no):
                return f"post {target_post} inexistente o no visible en esta ronda"
            post = self.posts[target_post]
            if kind in ("like", "repost") and post.author_id == agent:
                return "no puede dar like/repost a su propio post"
            if kind == "like" and (agent, target_post) in self.likes:
                return "like duplicado"
            if kind == "repost" and any(
                p.kind == "repost" and p.author_id == agent and p.parent_id == target_post
                for p in self.posts.values()
            ):
                return "repost duplicado"
        if kind in ("follow", "unfollow"):
            if not target_handle or target_handle not in self.by_handle:
                return f"handle @{target_handle} desconocido"
            target = self.by_handle[target_handle]
            if target == agent:
                return "no puede seguirse a sí mismo"
            if kind == "follow" and (agent, target) in self.follows:
                return "ya lo seguía"
            if kind == "unfollow" and (agent, target) not in self.follows:
                return "no lo seguía"
        return None

    # -- feed --------------------------------------------------------------------------------------

    @abstractmethod
    def build_feed(
        self,
        agent_id: str,
        stances: dict[str, float],
        round_no: int,
        rng: random.Random,
        size: int,
        max_chars: int,
    ) -> list[FeedItem]: ...

    # -- persistencia ------------------------------------------------------------------------------

    def persist_changes(self, store: RunStore) -> None:
        """Escribe el delta en la transacción abierta por el llamador."""
        ch = self.changes
        for pid in ch.new_posts:
            store.insert_post(self.posts[pid].model_dump())
        for pid in ch.dirty_posts - set(ch.new_posts):
            store.update_post_counters(self.posts[pid].model_dump())
        for pid in set(ch.new_posts) & ch.dirty_posts:
            store.update_post_counters(self.posts[pid].model_dump())
        if ch.new_likes:
            store.conn.executemany(
                "INSERT OR IGNORE INTO likes(agent_id, post_id, round) VALUES(?, ?, ?)", ch.new_likes
            )
        if ch.follows_added:
            store.conn.executemany(
                "INSERT OR IGNORE INTO follows(follower_id, followee_id, since_round) VALUES(?, ?, ?)",
                ch.follows_added,
            )
        if ch.follows_removed:
            store.conn.executemany(
                "DELETE FROM follows WHERE follower_id=? AND followee_id=?", ch.follows_removed
            )
        for acc in self.accounts.values():
            if acc.kind == "system":
                store.upsert_account(acc.id, acc.handle, acc.name, acc.kind)
        self.changes = Changes()

    def discard_changes(self, store: RunStore) -> None:
        """Descarta el estado no confirmado recargando desde SQLite."""
        fresh = type(self).load(store, list(a for a in self.accounts.values() if a.kind == "agent"))
        self.__dict__.update(fresh.__dict__)

    @classmethod
    def load(cls, store: RunStore, agents: list[Account]) -> Platform:
        plat = cls()
        for acc in agents:
            plat.add_account(acc)
        for row in store.load_accounts():
            plat.add_account(Account(id=row["id"], handle=row["handle"], name=row["name"], kind=row["kind"]))
        for row in store.load_posts():
            post = Post(**{k: row[k] for k in row.keys()})  # noqa: SIM118 - sqlite3.Row
            plat.posts[post.id] = post
            plat.seq = max(plat.seq, post.seq)
        plat.likes = {(r["agent_id"], r["post_id"]) for r in store.load_likes()}
        plat.follows = {(r["follower_id"], r["followee_id"]) for r in store.load_follows()}
        plat.changes = Changes()
        return plat


class TwitterPlatform(Platform):
    """Red tipo X/Twitter: feed de seguidos + populares + exploración."""

    name = "twitter"

    def build_feed(
        self,
        agent_id: str,
        stances: dict[str, float],
        round_no: int,
        rng: random.Random,
        size: int,
        max_chars: int,
    ) -> list[FeedItem]:
        from swarmlab.social.feed import twitter_feed

        return twitter_feed(self, agent_id, stances, round_no, rng, size, max_chars)


class ForumPlatform(Platform):
    """Foro: hilos ordenados por actividad; sin reposts (la cita hace de "mención" en otro hilo)."""

    name = "forum"
    allowed_actions = ("post", "reply", "quote", "like", "follow", "unfollow", "do_nothing")

    def build_feed(
        self,
        agent_id: str,
        stances: dict[str, float],
        round_no: int,
        rng: random.Random,
        size: int,
        max_chars: int,
    ) -> list[FeedItem]:
        from swarmlab.social.feed import forum_feed

        return forum_feed(self, agent_id, stances, round_no, rng, size, max_chars)


PLATFORMS: dict[str, type[Platform]] = {"twitter": TwitterPlatform, "forum": ForumPlatform}


def make_platform(kind: str) -> Platform:
    try:
        return PLATFORMS[kind]()
    except KeyError as exc:
        raise ValueError(f"plataforma desconocida {kind!r}: {', '.join(PLATFORMS)}") from exc
