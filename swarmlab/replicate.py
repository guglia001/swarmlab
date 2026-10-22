"""Réplicas de un run: mismo documento, pregunta, eventos y configuración con otra semilla aleatoria.

Las réplicas se llaman ``<grupo>-r2``, ``<grupo>-r3``… y llevan ``replica_of`` para que ``scorecard``,
``compare`` y ``calibrate`` las agrupen. Si el run reutilizaba la población de otro (``--personas-from X``)
y X tiene réplicas, cada réplica se empareja con la del mismo número de X (A/B replicado y emparejado).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from swarmlab.config import Settings
from swarmlab.engine import Engine, RunConfig
from swarmlab.events import EventSpec
from swarmlab.store import RunStore


@dataclass
class ReplicaPlan:
    group: str
    cfg: RunConfig
    seed: Path
    events: list[EventSpec]
    indexes: list[int]
    stance_mode: str
    report_llm: bool
    rng_base: int
    same_population: bool
    source_group: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def personas_from(self, settings: Settings, k: int) -> str | None:
        """Población que reutiliza la réplica ``k``: la del run (misma población) o su pareja de A/B."""
        if self.same_population:
            return self.cfg.run_id
        if self.source_group and (settings.runs_dir / f"{self.source_group}-r{k}" / "swarm.db").is_file():
            return f"{self.source_group}-r{k}"
        return self.cfg.personas_from


def plan_replicas(
    settings: Settings,
    run_id: str,
    n: int,
    *,
    same_population: bool = False,
    stance_mode: str | None = None,
    report_llm: bool | None = None,
) -> ReplicaPlan:
    """Lee el run de referencia y decide nombres, semillas y población de ``n`` réplicas nuevas."""
    base = RunStore.open_existing(settings.runs_dir / run_id)
    try:
        cfg = RunConfig.model_validate(base.get_meta("config"))
        events = [
            ev
            for ev in (EventSpec.model_validate(json.loads(r["data"])) for r in base.load_events())
            if ev.origin != "auto"  # el anuncio se rehace con el brief de cada réplica
        ]
        seed = base.paths.root / cfg.seed_file
    finally:
        base.close()
    group = cfg.replica_of or run_id
    rng_base = cfg.rng_seed
    if group != run_id and (settings.runs_dir / group / "swarm.db").is_file():
        g = RunStore.open_existing(settings.runs_dir / group)
        rng_base = RunConfig.model_validate(g.get_meta("config")).rng_seed
        g.close()
    taken = {p.name for p in settings.runs_dir.iterdir()} if settings.runs_dir.is_dir() else set()
    indexes: list[int] = []
    k = 2
    while len(indexes) < n:
        if f"{group}-r{k}" not in taken:
            indexes.append(k)
        k += 1
    source_group = cfg.personas_from
    if cfg.personas_from and not same_population:
        src = RunStore.open_existing(settings.runs_dir / cfg.personas_from)
        source_group = src.get_meta("config", {}).get("replica_of") or cfg.personas_from
        src.close()
    return ReplicaPlan(
        group=group,
        cfg=cfg,
        seed=seed,
        events=events,
        indexes=indexes,
        stance_mode=stance_mode or cfg.stance_mode,
        report_llm=cfg.report_llm if report_llm is None else report_llm,
        rng_base=rng_base,
        same_population=same_population,
        source_group=source_group,
    )


def create_replica(settings: Settings, plan: ReplicaPlan, k: int, **engine_kwargs: object) -> Engine:
    """Crea (sin ejecutar) la réplica ``<grupo>-rk`` con la semilla ``rng_base + k - 1``."""
    cfg = plan.cfg
    engine = Engine.create(
        settings,
        seed=plan.seed,
        question=cfg.question,
        n_agents=cfg.n_agents,
        rounds=cfg.rounds,
        agents_per_call=cfg.agents_per_call,
        dry_run=cfg.dry_run,
        rng_seed=plan.rng_base + k - 1,
        platform=cfg.platform,
        language=cfg.language,
        max_calls=cfg.max_calls,
        personas_from=plan.personas_from(settings, k),
        survey=cfg.survey,
        round_interval=cfg.round_interval,
        run_id=f"{plan.group}-r{k}",
        stance_mode=plan.stance_mode,
        report_llm=plan.report_llm,
        replica_of=plan.group,
        **engine_kwargs,  # type: ignore[arg-type]
    )
    for ev in plan.events:
        engine.store.save_event(ev)
    return engine
