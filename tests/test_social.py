from __future__ import annotations

import random
from pathlib import Path

import pytest

from swarmlab.social.actions import Action, AgentDecision
from swarmlab.social.platform import Account, ForumPlatform, TwitterPlatform
from swarmlab.store import RunStore


def make_platform(n: int = 5) -> TwitterPlatform:
    plat = TwitterPlatform()
    for i in range(1, n + 1):
        plat.add_account(Account(id=f"a{i:02d}", handle=f"user{i}", name=f"User {i}"))
    return plat


def act(plat: TwitterPlatform, agent: str, round_no: int, *actions: Action, stance: float = 0.0) -> list:
    dec = AgentDecision(agent_id=agent, actions=list(actions), new_stance=stance, reasoning="r")
    return plat.apply_decision(dec, round_no, stance, max_actions=4, max_chars=280)


def test_actions_update_counters_and_follows() -> None:
    plat = make_platform()
    (post,) = act(plat, "a01", 1, Action(type="post", text="hola mundo"))
    pid = post.post_id
    outs = act(
        plat,
        "a02",
        2,
        Action(type="like", target_post_id=pid),
        Action(type="reply", target_post_id=pid, text="¿precio?"),
        Action(type="repost", target_post_id=pid),
        Action(type="follow", target_handle="@User1"),
    )
    assert all(o.valid for o in outs), [o.reason for o in outs]
    p = plat.posts[pid]
    assert (p.likes, p.replies, p.reposts) == (1, 1, 1)
    assert ("a02", "a01") in plat.follows
    (quote,) = act(plat, "a03", 3, Action(type="quote", target_post_id=pid, text="ojo con esto"))
    assert quote.valid and plat.posts[pid].quotes == 1 and plat.posts[quote.post_id].parent_id == pid


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        (Action(type="like", target_post_id="p0001"), "propio"),
        (Action(type="reply", target_post_id="p9999", text="x"), "inexistente"),
        (Action(type="post", text="   "), "vacío"),
        (Action(type="follow", target_handle="nadie"), "desconocido"),
        (Action(type="follow", target_handle="user1"), "sí mismo"),
        (Action(type="unfollow", target_handle="user2"), "no lo seguía"),
        (Action(type="like"), "falta"),
    ],
)
def test_invalid_actions_are_rejected(action: Action, reason: str) -> None:
    plat = make_platform()
    act(plat, "a01", 1, Action(type="post", text="mi post"))
    (out,) = act(plat, "a01", 2, action)
    assert not out.valid and reason in (out.reason or "")


def test_frozen_state_same_round_posts_not_targetable() -> None:
    plat = make_platform()
    (post,) = act(plat, "a01", 3, Action(type="post", text="recién publicado"))
    (out,) = act(plat, "a02", 3, Action(type="like", target_post_id=post.post_id))
    assert not out.valid and "no visible" in (out.reason or "")
    event = plat.publish_event(3, "aeat", "Agencia Tributaria", "nota")
    (ok,) = act(plat, "a02", 3, Action(type="reply", target_post_id=event.id, text="vaya"))
    assert ok.valid  # los eventos de la ronda sí son visibles


def test_duplicate_like_and_max_actions() -> None:
    plat = make_platform()
    (post,) = act(plat, "a01", 1, Action(type="post", text="x"))
    outs = act(plat, "a02", 2, *[Action(type="like", target_post_id=post.post_id)] * 6)
    assert len(outs) == 4 and sum(o.valid for o in outs) == 1


def _seed_posts(plat: TwitterPlatform) -> None:
    plat.add_follow("a01", "a02", 0)
    for r in (1, 2):
        for i in range(2, 6):
            act(plat, f"a{i:02d}", r, Action(type="post", text=f"post de user{i} en r{r}"), stance=0.1 * i)
    act(plat, "a01", 1, Action(type="post", text="mío"))
    act(plat, "a03", 2, Action(type="reply", target_post_id="p0009", text="te contesto"))


def test_feed_composition() -> None:
    plat = make_platform()
    _seed_posts(plat)
    event = plat.publish_event(3, "aeat", "AEAT", "Hacienda avisa")
    stances = {f"a{i:02d}": 0.1 * i for i in range(1, 6)}
    feed = plat.build_feed("a01", stances, 3, random.Random(1), size=6, max_chars=50)
    assert len(feed) == 6
    assert feed[0].post_id == event.id and feed[0].reason == "evento"
    assert feed[1].reason == "te responde" and feed[1].parent_handle == "user1"
    assert all(item.author_id != "a01" for item in feed)
    assert all(item.round < 3 or item.kind == "event" for item in feed)
    assert any(item.author_id == "a02" and item.reason == "sigues" for item in feed)
    assert all(len(item.text) <= 50 for item in feed)
    again = plat.build_feed("a01", stances, 3, random.Random(1), size=6, max_chars=50)
    assert [i.post_id for i in again] == [i.post_id for i in feed]  # determinista con la misma semilla


def test_forum_feed_groups_threads_and_disallows_reposts() -> None:
    plat = ForumPlatform()
    for i in range(1, 4):
        plat.add_account(Account(id=f"a{i:02d}", handle=f"user{i}", name="x"))
    dec = AgentDecision(
        agent_id="a01", actions=[Action(type="post", text="hilo")], new_stance=0, reasoning="r"
    )
    root = plat.apply_decision(dec, 1, 0, 4, 280)[0].post_id
    dec2 = AgentDecision(
        agent_id="a02",
        actions=[
            Action(type="reply", target_post_id=root, text="resp"),
            Action(type="repost", target_post_id=root),
        ],
        new_stance=0,
        reasoning="r",
    )
    outs = plat.apply_decision(dec2, 2, 0, 4, 280)
    assert outs[0].valid and not outs[1].valid
    feed = plat.build_feed("a03", {}, 3, random.Random(0), 5, 280)
    assert [f.post_id for f in feed][:2] == [root, outs[0].post_id]


def test_persist_and_reload_roundtrip(tmp_path: Path) -> None:
    plat = make_platform()
    _seed_posts(plat)
    plat.publish_event(2, "aeat", "AEAT", "nota")
    store = RunStore(tmp_path / "run")
    with store.transaction():
        plat.persist_changes(store)
    agents = [a for a in plat.accounts.values() if a.kind == "agent"]
    loaded = TwitterPlatform.load(store, agents)
    assert loaded.posts == plat.posts and loaded.follows == plat.follows and loaded.likes == plat.likes
    assert loaded.seq == plat.seq and loaded.by_handle["aeat"] == "sys:aeat"
    assert [r["id"] for r in store.search_posts("contesto")] == ["p0010"]
