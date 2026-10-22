"""Grafos: el de conocimiento (entidades/relaciones de la ontología) y el social inicial de seguidores."""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

import networkx as nx

from swarmlab.ontology import Ontology

if TYPE_CHECKING:
    from swarmlab.personas import Persona


# --------------------------------------------------------------------------- grafo de conocimiento


def build_knowledge_graph(ont: Ontology) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph(title=ont.title)
    for e in ont.entities:
        g.add_node(
            e.id,
            name=e.name,
            type=e.type,
            stance=e.initial_stance,
            influence=e.influence,
            description=e.description,
        )
    for r in ont.relations:
        g.add_edge(r.source, r.target, key=r.type, type=r.type, weight=r.weight, description=r.description)
    for t in ont.tensions:
        for i, a in enumerate(t.parties):
            for b in t.parties[i + 1 :]:
                g.add_edge(
                    a, b, key=f"tension:{t.id}", type="tension", weight=t.intensity, description=t.description
                )
    return g


def graph_to_json(g: nx.MultiDiGraph) -> dict[str, Any]:
    return nx.node_link_data(g, edges="edges")


def graph_from_json(data: dict[str, Any]) -> nx.MultiDiGraph:
    return nx.node_link_graph(data, directed=True, multigraph=True, edges="edges")


def graph_stats(g: nx.MultiDiGraph) -> dict[str, Any]:
    if g.number_of_nodes() == 0:
        return {"nodes": 0, "edges": 0}
    simple = nx.Graph(g.to_undirected())
    centrality = nx.degree_centrality(simple)
    top = sorted(centrality.items(), key=lambda kv: -kv[1])[:5]
    return {
        "nodes": g.number_of_nodes(),
        "edges": g.number_of_edges(),
        "components": nx.number_connected_components(simple),
        "isolated": sorted(nx.isolates(simple)),
        "density": round(nx.density(simple), 3),
        "most_central": [(n, g.nodes[n].get("name", n), round(c, 3)) for n, c in top],
    }


def plot_knowledge_graph(g: nx.MultiDiGraph, path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 8))
    if g.number_of_nodes():
        simple = nx.Graph(g.to_undirected())
        pos = nx.spring_layout(simple, seed=7, k=1.4 / math.sqrt(max(simple.number_of_nodes(), 1)))
        stances = [g.nodes[n].get("stance", 0.0) for n in simple.nodes]
        sizes = [300 + 1500 * g.nodes[n].get("influence", 0.3) for n in simple.nodes]
        tension_edges = [(a, b) for a, b, d in g.edges(data=True) if d.get("type") == "tension"]
        other_edges = [(a, b) for a, b, d in g.edges(data=True) if d.get("type") != "tension"]
        nx.draw_networkx_edges(simple, pos, edgelist=other_edges, alpha=0.35, ax=ax)
        nx.draw_networkx_edges(
            simple, pos, edgelist=tension_edges, alpha=0.6, style="dashed", edge_color="red", ax=ax
        )
        nodes = nx.draw_networkx_nodes(
            simple, pos, node_color=stances, cmap="RdYlGn", vmin=-1, vmax=1, node_size=sizes, ax=ax
        )
        nx.draw_networkx_labels(
            simple, pos, labels={n: g.nodes[n].get("name", n)[:28] for n in simple.nodes}, font_size=8, ax=ax
        )
        fig.colorbar(nodes, ax=ax, label="postura inicial hacia la propuesta")
    ax.set_title(f"Grafo de entidades — {g.graph.get('title', '')} (rojo discontinuo = tensión)")
    ax.axis("off")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- grafo social inicial


def _weighted_sample(items: list[str], weights: list[float], k: int, rng: random.Random) -> list[str]:
    """Muestreo sin reemplazo proporcional al peso (Efraimidis–Spirakis)."""
    keyed = sorted(
        ((rng.random() ** (1.0 / max(w, 1e-9)), it) for it, w in zip(items, weights, strict=True)),
        reverse=True,
    )
    return [it for _, it in keyed[:k]]


def build_follow_graph(personas: list[Persona], rng: random.Random) -> list[tuple[str, str]]:
    """Seguidores iniciales: homofilia (postura, segmento, entidades) + algo de azar + atracción por influencers."""
    if len(personas) < 2:
        return []
    max_followers = max(p.followers_initial for p in personas) or 1
    edges: set[tuple[str, str]] = set()
    for p in personas:
        others = [q for q in personas if q.id != p.id]
        k = min(len(others), round(3 + 6 * p.activity + 3 * p.traits.extraversion))
        weights = []
        for q in others:
            homophily = 1 - abs(p.initial_stance - q.initial_stance) / 2
            homophily += 0.3 * (p.segment == q.segment) + 0.2 * bool(
                set(p.entity_links) & set(q.entity_links)
            )
            popularity = math.log1p(q.followers_initial) / math.log1p(max_followers)
            weights.append(math.exp(2.5 * homophily) * (1 + 2.0 * popularity))
        chosen = _weighted_sample([q.id for q in others], weights, k, rng)
        if chosen and rng.random() < 0.3:  # algo de aleatoriedad pura
            chosen[-1] = rng.choice(others).id
        edges.update((p.id, c) for c in chosen if c != p.id)
    return sorted(edges)
