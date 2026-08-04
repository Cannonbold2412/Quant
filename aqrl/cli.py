"""`aqrl` — the command-line entry point.

This is what makes Stage 1's "done when" demonstrable rather than asserted: a
snapshot can be ingested, versioned, hashed and loaded by ID, and profiles
resolve and hash deterministically, all without opening a Python REPL.

Stage 9 adds `aqrl review` here for the human gates. Deliberately argparse and
plain text — the novelty budget is spent on the research loop, not the CLI.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import get_settings
from .db import connect, migrate, transaction
from .db.migrate import status as migration_status
from .db.repositories import (
    CorporateActionRepository,
    IndexMembershipRepository,
    SnapshotRepository,
    ValidationFlagRepository,
)
from .profiles import ProfileLoader


def _table(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return "(none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    rule = "  ".join("-" * widths[c] for c in columns)
    body = "\n".join("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows)
    return f"{header}\n{rule}\n{body}"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return [{k: (v.strip() if v else v) for k, v in row.items()} for row in csv.DictReader(handle)]


# -- db ------------------------------------------------------------------------


def cmd_db_migrate(args: argparse.Namespace) -> int:
    conn = connect()
    applied = migrate(conn, target=args.to)
    if not applied:
        print("Schema is up to date.")
    for migration in applied:
        print(f"applied {migration.version:04d}_{migration.name}")
    return 0


def cmd_db_status(args: argparse.Namespace) -> int:
    conn = connect()
    state = migration_status(conn)
    print(f"database: {get_settings().db_path}")
    print(f"applied migrations: {state['applied'] or 'none'}")
    print(f"pending migrations: {state['pending'] or 'none'}")
    print(f"tables: {len(state['tables'])}")
    if args.verbose:
        for table in state["tables"]:
            print(f"  {table}")
    return 0


# -- profile -------------------------------------------------------------------


def cmd_profile_list(args: argparse.Namespace) -> int:
    loader = ProfileLoader()
    print(f"markets:    {', '.join(loader.list_markets()) or '(none)'}")
    print(f"timeframes: {', '.join(loader.list_timeframes()) or '(none)'}")
    costs = [f"{m}.{a}" for m, a in loader.list_cost_models()]
    print(f"cost models: {', '.join(costs) or '(none)'}")
    return 0


def cmd_profile_show(args: argparse.Namespace) -> int:
    loader = ProfileLoader()
    if args.timeframe:
        resolved = loader.resolve(args.name, args.timeframe, args.asset_class)
        print(f"market            {resolved.market.name} v{resolved.market.version}")
        print(f"timeframe         {resolved.timeframe.name} v{resolved.timeframe.version}")
        print(f"asset class       {args.asset_class}")
        print(f"periods_per_year  {resolved.periods_per_year:,.4f}")
        print(f"annualisation     {resolved.annualisation_factor:.6f}  (sqrt)")
        print(f"round-trip cost   {resolved.cost_model.round_trip_bps():.4f} bps")
        print(f"  at 2x stress    {resolved.cost_model.round_trip_bps() * 2:.4f} bps")
        if resolved.cost_model.is_placeholder:
            print("  WARNING: placeholder cost model — not derived from a contract note")
        print(f"market hash       {resolved.market_profile_hash}")
        print(f"timeframe hash    {resolved.timeframe_profile_hash}")
        print(f"cost model hash   {resolved.cost_model_hash}")
        return 0

    if args.name in loader.list_markets():
        profile: Any = loader.load_market(args.name)
    else:
        profile = loader.load_timeframe(args.name)
    print(json.dumps(profile.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


def cmd_profile_hash(args: argparse.Namespace) -> int:
    from .profiles import hash_profile

    loader = ProfileLoader()
    if args.name in loader.list_markets():
        print(hash_profile(loader.load_market(args.name)))
    elif args.name in loader.list_timeframes():
        print(hash_profile(loader.load_timeframe(args.name)))
    else:
        print(f"no profile named {args.name!r}", file=sys.stderr)
        return 1
    return 0


def cmd_profile_register(args: argparse.Namespace) -> int:
    conn = connect()
    with transaction(conn):
        counts = ProfileLoader().register(conn)
    print(f"registered {counts['markets']} market(s), {counts['timeframes']} timeframe(s), "
          f"{counts['cost_models']} cost model(s)")
    return 0


# -- snapshot ------------------------------------------------------------------


def cmd_snapshot_ingest(args: argparse.Namespace) -> int:
    from .data import SnapshotManager

    conn = connect()
    with transaction(conn):
        snapshot_id = SnapshotManager(conn).ingest(
            args.path,
            args.market,
            args.timeframe,
            args.asset_class,
            adjustment_method=args.adjustment_method,
            in_vault=args.in_vault,
            validation_note=args.note,
        )
    snapshot = SnapshotRepository(conn).get(snapshot_id)
    pending = ValidationFlagRepository(conn).pending(snapshot_id)
    print(f"snapshot {snapshot_id}: {snapshot['bar_count']} bars, "
          f"{snapshot['instrument_count']} instrument(s), status={snapshot['validation_status']}")
    print(f"  raw_content_hash          {snapshot['raw_content_hash']}")
    print(f"  corporate_actions_version {snapshot['corporate_actions_version']}")
    if pending:
        print(f"  {len(pending)} validation flag(s) need a human decision before this can be loaded:")
        for flag in pending:
            print(f"    [{flag['id']}] {flag['flag_type']} {flag['instrument'] or ''} "
                  f"{flag['bar_date'] or ''}: {flag['detail']}")
    return 0


def cmd_snapshot_list(args: argparse.Namespace) -> int:
    conn = connect()
    rows = SnapshotRepository(conn).find(order_by="id")
    print(_table(rows, ["id", "market", "timeframe", "asset_class", "period_start", "period_end",
                        "bar_count", "validation_status", "point_in_time_membership"]))
    return 0


def cmd_snapshot_show(args: argparse.Namespace) -> int:
    conn = connect()
    snapshot = SnapshotRepository(conn).get(args.snapshot_id)
    if snapshot is None:
        print(f"no snapshot {args.snapshot_id}", file=sys.stderr)
        return 1
    for key, value in snapshot.items():
        print(f"{key:<26} {value}")
    flags = ValidationFlagRepository(conn).pending(args.snapshot_id)
    print(f"\npending flags: {len(flags)}")
    return 0


def cmd_snapshot_load(args: argparse.Namespace) -> int:
    from .data import SnapshotManager

    conn = connect()
    frame = SnapshotManager(conn).load(
        args.snapshot_id,
        instrument=args.instrument,
        start=args.start,
        end=args.end,
        adjusted=not args.raw,
    )
    print(frame.head(args.limit))
    print(f"\n{frame.height} rows  (adjusted={not args.raw})")
    return 0


# -- corporate actions ---------------------------------------------------------


def cmd_actions_import(args: argparse.Namespace) -> int:
    """Import from CSV: instrument,market,action_type,ex_date,ratio,raw_terms,source,verified_by."""
    conn = connect()
    repo = CorporateActionRepository(conn)
    imported = 0
    with transaction(conn):
        for row in _read_csv(args.path):
            repo.insert(
                instrument=row["instrument"],
                market=row["market"],
                action_type=row["action_type"],
                ex_date=row["ex_date"],
                ratio=float(row["ratio"]),
                raw_terms=row.get("raw_terms"),
                source=row.get("source"),
                verified_by=row.get("verified_by") or None,
            )
            imported += 1
    print(f"imported {imported} corporate action(s)")
    unverified = repo.count(verified_by=None)
    if unverified:
        print(f"WARNING: {unverified} action(s) are unverified and will NOT be applied to prices "
              "until a human verifies them")
    return 0


def cmd_actions_list(args: argparse.Namespace) -> int:
    conn = connect()
    repo = CorporateActionRepository(conn)
    rows = (
        repo.for_instrument(args.instrument, args.market, verified_only=False)
        if args.instrument
        else repo.find(order_by="instrument, ex_date")
    )
    print(_table(rows, ["id", "instrument", "market", "action_type", "ex_date", "ratio",
                        "raw_terms", "verified_by"]))
    return 0


# -- index membership ----------------------------------------------------------


def cmd_membership_import(args: argparse.Namespace) -> int:
    """Import from CSV: index_name,instrument,effective_from,effective_to,reason_added,..."""
    conn = connect()
    repo = IndexMembershipRepository(conn)
    imported = 0
    with transaction(conn):
        for row in _read_csv(args.path):
            repo.insert(
                index_name=row["index_name"],
                instrument=row["instrument"],
                effective_from=row["effective_from"],
                effective_to=row.get("effective_to") or None,
                reason_added=row.get("reason_added") or None,
                reason_removed=row.get("reason_removed") or None,
                successor_instrument=row.get("successor_instrument") or None,
                source=row.get("source"),
                verified_by=row.get("verified_by") or None,
            )
            imported += 1
    print(f"imported {imported} membership row(s)")
    ever = repo.ever_members(args.index or row["index_name"])
    print(f"ever-members recorded: {len(ever)}")
    if len(ever) <= 60:
        print("NOTE: NIFTY-50 had roughly 100-150 distinct members over 2000-2025. A count near 50 "
              "means departed constituents are missing — the survivorship bias this table exists to fix.")
    return 0


def cmd_membership_resolve(args: argparse.Namespace) -> int:
    conn = connect()
    members = IndexMembershipRepository(conn).resolve(args.index, args.date)
    print(f"{args.index} on {args.date}: {len(members)} member(s)")
    for instrument in members:
        print(f"  {instrument}")
    return 0


# -- validation flags ----------------------------------------------------------


def cmd_flags_list(args: argparse.Namespace) -> int:
    conn = connect()
    repo = ValidationFlagRepository(conn)
    rows = repo.pending(args.snapshot_id) if args.pending else repo.find(order_by="id")
    print(_table(rows, ["id", "snapshot_id", "flag_type", "instrument", "bar_date",
                        "observed_value", "threshold", "resolution"]))
    return 0


def cmd_flags_resolve(args: argparse.Namespace) -> int:
    from .data import SnapshotManager

    conn = connect()
    repo = ValidationFlagRepository(conn)
    flag = repo.get(args.flag_id)
    if flag is None:
        print(f"no flag {args.flag_id}", file=sys.stderr)
        return 1
    with transaction(conn):
        repo.resolve(args.flag_id, args.resolution, args.by)
        status = SnapshotManager(conn).revalidate(flag["snapshot_id"])
    print(f"flag {args.flag_id} resolved as {args.resolution} by {args.by}")
    print(f"snapshot {flag['snapshot_id']} is now {status}")
    return 0


# -- operators -----------------------------------------------------------------


def cmd_operators_list(args: argparse.Namespace) -> int:
    from .operators import all_operators

    rows = []
    for operator in all_operators():
        if args.category and operator.category != args.category:
            continue
        if args.market and operator.valid_markets and args.market not in operator.valid_markets:
            continue
        if (
            args.timeframe
            and operator.valid_timeframes
            and args.timeframe not in operator.valid_timeframes
        ):
            continue
        rows.append(
            {
                "name": operator.name,
                "version": operator.version,
                "category": operator.category,
                "inputs": ",".join(operator.inputs),
                "params": ",".join(spec.name for spec in operator.params) or "-",
                "description": operator.description,
            }
        )
    print(_table(rows, ["name", "version", "category", "inputs", "params", "description"]))
    print(f"\n{len(rows)} operator(s)")
    return 0


def cmd_operators_show(args: argparse.Namespace) -> int:
    from .operators import OperatorError, get

    try:
        operator = get(args.name, args.operator_version)
    except OperatorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(operator.descriptor(), indent=2, sort_keys=True))
    if operator.description:
        print(f"\n{operator.description}")
    if operator.references:
        print(f"reference: {operator.references}")
    return 0


def cmd_operators_version(args: argparse.Namespace) -> int:
    from .operators import all_operators, operator_library_version

    print(operator_library_version())
    print(f"{len(all_operators())} operator(s) registered", file=sys.stderr)
    return 0


def cmd_operators_sync(args: argparse.Namespace) -> int:
    from .db.repositories import OperatorRepository

    conn = connect()
    with transaction(conn):
        counts = OperatorRepository(conn).sync()
    print(
        f"inserted {counts['inserted']}, updated {counts['updated']}, "
        f"unchanged {counts['unchanged']}"
    )
    return 0


def cmd_operators_approve(args: argparse.Namespace) -> int:
    from .db.repositories import OperatorRepository

    conn = connect()
    try:
        with transaction(conn):
            count = OperatorRepository(conn).approve(args.name, args.operator_version, args.by)
    except (LookupError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"approved {count} version(s) of {args.name} by {args.by}")
    return 0


# -- specs ---------------------------------------------------------------------


def _load_spec(path: Path):
    from .operators import StrategySpec

    return StrategySpec(**json.loads(Path(path).read_text(encoding="utf-8")))


def cmd_spec_hash(args: argparse.Namespace) -> int:
    from .operators import SpecError

    try:
        spec = _load_spec(args.path)
        print(spec.spec_hash())
    except SpecError as exc:
        print(f"invalid spec: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_spec_compile(args: argparse.Namespace) -> int:
    """Compile a spec and report what it will compute, without running a backtest."""
    from .operators import SpecError, compile_spec, node_operators, spec_warmup

    try:
        spec = _load_spec(args.path)
        compiled = compile_spec(spec)
    except SpecError as exc:
        print(f"invalid spec: {exc}", file=sys.stderr)
        return 1

    print(f"spec_hash   {compiled.spec_hash}")
    print(f"warmup      {spec_warmup(spec)} bars")
    for role in ("entry", "exit", "filter", "risk"):
        roots = [node.id for node in spec.roots(role)]
        used = sorted(node_operators(spec, role))
        if used:
            print(f"{role:<11} roots={', '.join(roots)}  nodes={', '.join(used)}")
    return 0


# -- strategy / code / agents (Stage 5 — Implementation_Plan §8) ---------------


def cmd_strategy_new(args: argparse.Namespace) -> int:
    """Seed a strategy from a hand-written spec and enqueue its first IMPLEMENT.

    Everything downstream — render, static checks, git commit, `EVALUATE` —
    runs unattended from here, no LLM call involved: this is Stage 5's
    done-when (Implementation_Plan §8), made runnable rather than asserted.
    """
    from .db.repositories import JobRepository, SpecRepository, StrategyRepository
    from .operators import SpecError
    from .orchestration import states
    from .orchestration.events import Event, emit

    try:
        spec = _load_spec(args.spec)
    except SpecError as exc:
        print(f"invalid spec: {exc}", file=sys.stderr)
        return 1

    conn = connect()
    strategies = StrategyRepository(conn)
    specs = SpecRepository(conn)

    payload: dict[str, Any] = {"asset_class": args.asset_class}
    if args.snapshot_id is not None:
        payload["data_snapshot_id"] = args.snapshot_id
    if args.campaign:
        payload["campaign"] = args.campaign
    if args.cost_multiplier is not None:
        payload["cost_multiplier"] = args.cost_multiplier
    if args.seed is not None:
        payload["random_seed"] = args.seed

    try:
        with transaction(conn, immediate=True):
            strategy_id = strategies.get_or_create(args.name, args.family, args.market, args.timeframe)
            spec_id = specs.insert_spec(spec, strategy_id)
            strategy = strategies.get(strategy_id)
            if strategy["status"] == "draft":
                states.transition(conn, "strategies", strategy_id, "spec_ready", actor="cli")
            payload["spec_id"] = spec_id
            job_id = emit(conn, Event.SPEC_SAVED, strategy_id=strategy_id, payload=payload)
    except (ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    job = JobRepository(conn).get(job_id) if job_id else None
    print(f"strategy {strategy_id}  spec {spec_id}  IMPLEMENT job {job['uid'] if job else '(none — SPEC_SAVED not wired)'}")
    return 0


def cmd_goal_new(args: argparse.Namespace) -> int:
    """Create a `research_goals` row — the only way to seed one before
    Stage 9's human-gate UI exists (Stage 7, Implementation_Plan §10).
    Nothing before Stage 7 wrote to this table."""
    from .db.repositories import ResearchGoalRepository

    conn = connect()
    goals = ResearchGoalRepository(conn)
    with transaction(conn, immediate=True):
        goal_id = goals.insert(
            title=args.title,
            description=args.description,
            market=args.market,
            timeframe=args.timeframe,
            allocation_bucket=args.allocation_bucket,
            priority=args.priority,
            hypothesis_budget=args.hypothesis_budget,
            hypotheses_used=0,
            status="active",
            created_by="human",
        )
    print(f"research_goals {goal_id}  {args.title!r}")
    return 0


def cmd_goal_list(args: argparse.Namespace) -> int:
    from .db.repositories import ResearchGoalRepository

    conn = connect()
    rows = ResearchGoalRepository(conn).find(order_by="id")
    columns = ["id", "title", "market", "timeframe", "allocation_bucket", "status", "hypotheses_used", "hypothesis_budget"]
    print(_table(rows, columns))
    return 0


def cmd_code_show(args: argparse.Namespace) -> int:
    from .db.repositories import CodeVersionRepository

    conn = connect()
    versions = CodeVersionRepository(conn).for_experiment(args.experiment_id)
    if not versions:
        print(f"no code_versions for experiment {args.experiment_id}", file=sys.stderr)
        return 1

    if args.all:
        rows = [
            {
                "id": row["id"],
                "compile_ok": row["compile_ok"],
                "git_commit": (row["git_commit"] or "")[:12],
                "change_summary": row["change_summary"],
            }
            for row in versions
        ]
        print(_table(rows, ["id", "compile_ok", "git_commit", "change_summary"]))
        return 0

    row = versions[-1]
    print(f"code_version    {row['id']}")
    print(f"code_path       {row['code_path']}")
    print(f"git_commit      {row['git_commit']}")
    print(f"compile_ok      {row['compile_ok']}")
    print(f"prompt_version  {row['prompt_version']}")
    print(f"change_summary  {row['change_summary']}")
    checks = row.get("static_check_results") or []
    print("\nstatic checks:")
    print(_table(checks, ["test_name", "result", "detail"]))
    return 0


def cmd_agents_brief(args: argparse.Namespace) -> int:
    """Print the Implementation Brief (A2) or Review Brief (A3) a job would
    send to Claude — without calling it."""
    from .db.repositories import (
        CodeVersionRepository,
        EvaluationRepository,
        EvaluationTestRepository,
        ExperimentRepository,
        KnowledgeEntryRepository,
        RegimePerformanceRepository,
        ResearchPlanRepository,
        SpecRepository,
        StrategyRepository,
    )

    conn = connect()
    strategy = StrategyRepository(conn).get(args.strategy_id)
    if strategy is None:
        print(f"no strategy {args.strategy_id}", file=sys.stderr)
        return 1

    if args.agent == "a3":
        from .agents.context import assemble_review_brief
        from .eval.bar import AcceptanceBar

        if args.experiment_id is None:
            print("--experiment-id is required for --agent a3", file=sys.stderr)
            return 1
        experiments = ExperimentRepository(conn)
        experiment = experiments.get(args.experiment_id)
        if experiment is None:
            print(f"no experiment {args.experiment_id}", file=sys.stderr)
            return 1
        spec = SpecRepository(conn).load_spec(experiment["spec_id"])
        evaluation = EvaluationRepository(conn).latest_for_experiment(args.experiment_id)
        if evaluation is None:
            print(f"no evaluation for experiment {args.experiment_id}", file=sys.stderr)
            return 1
        bar = AcceptanceBar.locked(conn)
        brief = assemble_review_brief(
            strategy=strategy,
            spec=spec,
            experiment_history=experiments.find(strategy_id=args.strategy_id, order_by="iteration"),
            evaluation=evaluation,
            diagnostic_checks=EvaluationTestRepository(conn).for_evaluation(evaluation["id"]),
            regime_performance=RegimePerformanceRepository(conn).for_evaluation(evaluation["id"]),
            knowledge_entries=KnowledgeEntryRepository(conn).relevant_to(strategy),
            iteration_count=strategy["iteration_count"],
            plateau_counter=strategy["plateau_counter"],
            hard_iteration_cap=bar.hard_iteration_cap,
            plateau_patience=bar.plateau_patience,
        )
        print(brief)
        return 0

    from .agents.context import assemble_implement_brief

    specs = SpecRepository(conn)
    prior_spec = None
    prior_diff = None
    prior_evaluation = None
    if args.experiment_id is not None:
        experiment = ExperimentRepository(conn).get(args.experiment_id)
        if experiment is None:
            print(f"no experiment {args.experiment_id}", file=sys.stderr)
            return 1
        prior_spec = specs.load_spec(experiment["spec_id"])
        versions = CodeVersionRepository(conn).for_experiment(args.experiment_id)
        prior_diff = versions[-1]["diff_from_parent"] if versions else None
        prior_evaluation = EvaluationRepository(conn).latest_for_experiment(args.experiment_id)

    research_plan = None
    if args.research_plan_id is not None:
        research_plan = ResearchPlanRepository(conn).get(args.research_plan_id)
        if research_plan is None:
            print(f"no research_plans row {args.research_plan_id}", file=sys.stderr)
            return 1

    diagnostics = (
        json.loads(Path(args.diagnostics_file).read_text(encoding="utf-8")) if args.diagnostics_file else None
    )

    brief = assemble_implement_brief(
        strategy=strategy,
        prior_spec=prior_spec,
        research_plan=research_plan,
        prior_diff=prior_diff,
        prior_evaluation=prior_evaluation,
        diagnostics=diagnostics,
    )
    print(brief)
    return 0


# -- evaluate --------------------------------------------------------------------


def cmd_evaluate_run(args: argparse.Namespace) -> int:
    """Run one experiment through the Stage 3 engine and persist the report."""
    from .data import SnapshotManager
    from .db.repositories import ExperimentRepository, SnapshotRepository, StrategyRepository
    from .eval.bar import AcceptanceBar
    from .eval.engine import EvaluationInputs, evaluate_experiment
    from .eval.panel import PricePanel
    from .eval.persistence import persist_evaluation
    from .operators import StrategySpec

    conn = connect()
    resolved = ProfileLoader().resolve(args.market, args.timeframe, args.asset_class)
    snapshot = SnapshotRepository(conn).get(args.snapshot_id)
    if snapshot is None:
        print(f"no snapshot {args.snapshot_id}", file=sys.stderr)
        return 1

    frame = SnapshotManager(conn).load(args.snapshot_id)
    panel = PricePanel.from_frame(frame)
    spec = StrategySpec(**json.loads(Path(args.spec).read_text(encoding="utf-8")))

    strategies = StrategyRepository(conn)
    strategy_id = strategies.get_or_create(
        name=args.name, family=args.family, market=args.market, timeframe=args.timeframe
    )
    experiments = ExperimentRepository(conn)
    iteration = experiments.next_iteration(strategy_id)
    experiment_id = experiments.start(strategy_id, iteration)

    inputs = EvaluationInputs(
        spec=spec,
        panel=panel,
        resolved=resolved,
        snapshot=snapshot,
        acceptance_bar=AcceptanceBar.locked(conn, args.campaign),
        family_prior_trials=strategies.family_trial_count(args.family),
        code_commit=args.code_commit,
        data_snapshot_id=args.snapshot_id,
        random_seed=args.seed if args.seed is not None else iteration,
        cost_multiplier=args.cost_multiplier,
    )

    with transaction(conn):
        report = evaluate_experiment(inputs)
        evaluation_id = persist_evaluation(conn, experiment_id, report)

    print(f"experiment {experiment_id}  evaluation {evaluation_id}")
    print(f"phase_reached={report.phase_reached}  outcome={report.outcome}"
          + (f"  failure_reason={report.failure_reason}" if report.failure_reason else ""))
    if report.honest_score is not None:
        print(f"honest_score={report.honest_score.honest_score:.4f}")
    return 0 if report.outcome != "error" else 1


def cmd_evaluate_show(args: argparse.Namespace) -> int:
    from .db.repositories import EvaluationRepository, EvaluationTestRepository, ExperimentRepository

    conn = connect()
    experiment = ExperimentRepository(conn).get(args.experiment_id)
    if experiment is None:
        print(f"no experiment {args.experiment_id}", file=sys.stderr)
        return 1

    print("## experiment")
    for key in (
        "id", "strategy_id", "iteration", "status", "outcome", "phase_reached",
        "eval_engine_version", "market_profile_hash", "timeframe_profile_hash",
        "cost_model_hash", "wf_config_hash", "operator_library_version",
        "code_commit", "data_snapshot_id", "random_seed", "comparable",
    ):
        print(f"  {key:<24} {experiment.get(key)}")

    evaluation = EvaluationRepository(conn).latest_for_experiment(args.experiment_id)
    if evaluation is None:
        print("\n(no evaluation recorded)")
        return 0

    print("\n## evaluation")
    for key in (
        "phase", "result", "bar_result", "bar_failed_on", "honest_score", "sr_oos",
        "se_sr", "n_trials_used", "winning_train_years", "train_window_spread",
        "sharpe", "max_drawdown", "trade_count", "deflated_sharpe", "pbo",
        "white_rc_pvalue", "cost_breakeven_multiplier",
    ):
        if key in evaluation:
            print(f"  {key:<24} {evaluation[key]}")

    tests = EvaluationTestRepository(conn).for_evaluation(evaluation["id"])
    if tests:
        print(f"\n## checks ({len(tests)})")
        print(_table(tests, ["test_name", "category", "result", "gating", "value", "threshold"]))
    return 0


def cmd_evaluate_null_world(args: argparse.Namespace) -> int:
    """Re-run null-world calibration through the Stage 3 panel engine.

    Reuses nanoAQRL's generators (permuted returns, block bootstrap, synthetic
    fat-tailed paths) rather than duplicating them — the null-world claim is
    about the SCORING PATH, and Stage 0 already measured this generator set at
    0/40 (`Implementation_Plan.md`, M0). Re-running it here answers whether
    that result still holds now that the scoring path is the panel engine.
    """
    from .db.repositories import NullWorldRunRepository
    from .eval.bar import AcceptanceBar
    from .eval.engine import EvaluationInputs, evaluate_experiment
    from .eval.panel import PricePanel
    from .eval.version import engine_version
    from .operators import StrategySpec

    generators = _null_world_generators()
    if args.generator not in generators:
        print(f"unknown generator {args.generator!r} (choices: {', '.join(generators)})", file=sys.stderr)
        return 1
    gen_fn = generators[args.generator]

    resolved = ProfileLoader().resolve(args.market, args.timeframe, args.asset_class)
    spec = StrategySpec(**json.loads(Path(args.spec).read_text(encoding="utf-8")))
    snapshot = {
        "validation_status": "valid", "in_vault": 0, "adjusted": 1,
        "adjustment_method": "back_ratio_price", "point_in_time_membership": 1,
    }
    bar = AcceptanceBar()

    discoveries = 0
    max_score = float("-inf")
    for replication in range(args.replications):
        frame = gen_fn(args.days, seed=1000 + replication)
        panel = PricePanel.from_pandas_ohlcv(frame)
        inputs = EvaluationInputs(
            spec=spec, panel=panel, resolved=resolved, snapshot=snapshot,
            acceptance_bar=bar, family_prior_trials=0, random_seed=replication,
        )
        report = evaluate_experiment(inputs)
        if report.outcome == "passed":
            discoveries += 1
        if report.honest_score is not None:
            max_score = max(max_score, report.honest_score.honest_score)

    # `null_world_runs.null_model` uses a different vocabulary from this
    # command's `--generator` (Backend-Schema §11) — the CLI flag names the
    # nanoAQRL function reused, the column names the null-world MODEL it
    # implements. `synthetic_path` is Student-t fat-tailed, not GBM.
    null_model = {
        "permuted": "permuted_returns",
        "block_bootstrap": "block_bootstrap",
        "synthetic_path": "synthetic_fat_tail",
    }[args.generator]

    conn = connect()
    with transaction(conn):
        NullWorldRunRepository(conn).record(
            null_model=null_model,
            replications=args.replications,
            discoveries_reported=discoveries,
            max_score_observed=max_score if max_score != float("-inf") else 0.0,
            eval_engine_version=engine_version(),
        )

    print(f"generator={args.generator}  replications={args.replications}  "
          f"discoveries={discoveries}  fdr={discoveries / args.replications:.4f}")
    return 0


def _null_world_generators() -> dict[str, Any]:
    from nanoaqrl._lib.synthetic_data import (
        block_bootstrap_ohlcv,
        permuted_returns_ohlcv,
        synthetic_path_ohlcv,
    )

    return {
        "permuted": permuted_returns_ohlcv,
        "block_bootstrap": block_bootstrap_ohlcv,
        "synthetic_path": synthetic_path_ohlcv,
    }


def _null_world_default_days() -> int:
    """Six years of trading days — derived, not a bare annualisation literal,
    per the same rule `test_no_hardcoded_annualisation_constant_in_the_package`
    enforces everywhere else in this package."""
    from nanoaqrl._lib.synthetic_data import TRADING_DAYS_PER_YEAR

    return TRADING_DAYS_PER_YEAR * 6


_NULL_WORLD_DEFAULT_DAYS = _null_world_default_days()


# -- jobs (Stage 4 — Implementation_Plan §6) ------------------------------------


def cmd_jobs_list(args: argparse.Namespace) -> int:
    from .db.repositories import JobRepository

    conn = connect()
    filters: dict[str, Any] = {}
    if args.status:
        filters["status"] = args.status
    if args.type:
        filters["job_type"] = args.type
    if args.strategy_id:
        filters["strategy_id"] = args.strategy_id
    rows = JobRepository(conn).find(order_by="id DESC", limit=args.limit, **filters)
    print(_table(rows, ["id", "uid", "job_type", "status", "priority", "attempts", "strategy_id", "experiment_id", "created_at"]))
    return 0


def cmd_jobs_show(args: argparse.Namespace) -> int:
    from .db.repositories import JobRepository

    conn = connect()
    job = JobRepository(conn).get_by_uid(args.uid)
    if job is None:
        print(f"no job {args.uid}", file=sys.stderr)
        return 1
    for key, value in job.items():
        print(f"  {key:<20} {value}")
    return 0


def cmd_jobs_enqueue(args: argparse.Namespace) -> int:
    from .db.repositories import JobRepository

    conn = connect()
    payload = json.loads(args.payload) if args.payload else {}
    jobs = JobRepository(conn)
    with transaction(conn, immediate=True):
        job_id = jobs.enqueue(
            args.type,
            payload,
            strategy_id=args.strategy_id,
            experiment_id=args.experiment_id,
            priority=args.priority,
            dedupe_key=args.dedupe_key,
        )
    job = jobs.get(job_id)
    print(f"enqueued job {job_id}  uid={job['uid']}")
    return 0


def cmd_jobs_cancel(args: argparse.Namespace) -> int:
    from .db.repositories import JobRepository

    conn = connect()
    jobs = JobRepository(conn)
    job = jobs.get_by_uid(args.uid)
    if job is None:
        print(f"no job {args.uid}", file=sys.stderr)
        return 1
    jobs.cancel(job["id"], reason=args.reason or "cancelled via CLI")
    print(f"cancelled job {job['id']}")
    return 0


def cmd_jobs_retry(args: argparse.Namespace) -> int:
    """Manually return a terminally-failed job to `pending`.

    Distinct from `failures.handle_job_failure`'s automatic transient retry —
    this is a human override for a job the operator has decided is worth
    another attempt regardless of how it was classified.
    """
    from .db.repositories import JobRepository

    conn = connect()
    jobs = JobRepository(conn)
    job = jobs.get_by_uid(args.uid)
    if job is None:
        print(f"no job {args.uid}", file=sys.stderr)
        return 1
    if job["status"] not in ("failed", "timed_out", "cancelled"):
        print(f"job {args.uid} is {job['status']!r}, not a terminal failure state", file=sys.stderr)
        return 1
    jobs.update(
        job["id"], status="pending", claimed_by=None, lease_expires_at=None, heartbeat_at=None,
        error_message=None, error_trace=None, failure_class=None, completed_at=None, scheduled_for=None,
    )
    print(f"requeued job {job['id']}")
    return 0


# -- scheduler (Stage 4) ---------------------------------------------------------


def cmd_scheduler_run(args: argparse.Namespace) -> int:
    import os

    from .orchestration.dispatch import Dispatcher
    from .orchestration.scheduler import run_forever, tick

    conn = connect()
    dispatcher = Dispatcher(
        conn,
        worker_id=f"scheduler:{os.getpid()}",
        max_concurrent=args.max_workers,
        time_budget_seconds=args.time_budget_seconds,
    )
    if args.once:
        report = tick(conn, dispatcher)
        print(report)
        return 0
    run_forever(conn, dispatcher, tick_seconds=args.tick_seconds)
    return 0


def cmd_scheduler_status(args: argparse.Namespace) -> int:
    from .db.repositories import JobRepository
    from .orchestration.budgets import check_global

    conn = connect()
    jobs = JobRepository(conn)
    print(f"pending      {jobs.pending_count()}")
    print(f"running      {jobs.running_count()}")
    back_pressure = check_global(conn)
    print(f"budgets      {'ok' if back_pressure.allowed else back_pressure.reason}")
    print("by status:")
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status ORDER BY status"):
        print(f"  {row['status']:<12} {row['n']}")
    return 0


# -- budgets (Stage 4) -----------------------------------------------------------


def cmd_budgets_list(args: argparse.Namespace) -> int:
    conn = connect()
    rows = [dict(row) for row in conn.execute("SELECT * FROM budgets ORDER BY scope, budget_type, period")]
    print(_table(rows, ["scope", "scope_id", "budget_type", "period", "limit_value", "used_value", "exhausted"]))
    return 0


def cmd_budgets_set(args: argparse.Namespace) -> int:
    from .orchestration.budgets import BudgetRepository

    conn = connect()
    row_id = BudgetRepository(conn).upsert(
        args.scope, args.type, args.period, args.limit, scope_id=args.scope_id
    )
    print(f"budget {row_id} set: {args.scope}:{args.type}:{args.period} limit={args.limit}")
    return 0


# -- knowledge (Stage 8) ---------------------------------------------------------


def cmd_knowledge_rate(args: argparse.Namespace) -> int:
    """Implementation_Plan §11's done-when, observed from a terminal:
    *"rejected experiments demonstrably prevent similar future proposals ...
    measured by the repeat-failure rate trending toward zero."*
    """
    from .db.repositories import repeat_failure_rate

    conn = connect()
    result = repeat_failure_rate(conn, since=args.since)
    print(f"repeats:  {result['repeats']}")
    print(f"total:    {result['total']}")
    print(f"rate:     {result['rate']:.3f}")
    return 0


# -- review (Stage 9 — Implementation_Plan §12) --------------------------------


def cmd_review_list(args: argparse.Namespace) -> int:
    from . import gates
    from .db.repositories import StrategyRepository

    conn = connect()
    promotions = gates.pending(conn)
    strategies = StrategyRepository(conn)
    rows = []
    for promotion in promotions:
        strategy = strategies.get(promotion["strategy_id"]) or {}
        rows.append(
            {
                "uid": promotion["uid"],
                "strategy": strategy.get("name"),
                "family": strategy.get("family"),
                "market": strategy.get("market"),
                "a4_decision": promotion["decision"],
                "overfitting_risk": promotion["overfitting_risk"],
                "confidence": promotion["confidence"],
                "iterations": promotion["iterations_considered"],
                "alloc_pct": promotion["recommended_allocation_pct"],
            }
        )
    print(_table(rows, ["uid", "strategy", "family", "market", "a4_decision",
                        "overfitting_risk", "confidence", "iterations", "alloc_pct"]))
    return 0


def cmd_review_show(args: argparse.Namespace) -> int:
    from . import gates
    from .db.repositories import (
        EvaluationRepository,
        EvaluationTestRepository,
        PromotionRepository,
        RegimePerformanceRepository,
        StrategyRepository,
        VaultAccessRepository,
    )

    conn = connect()
    promotion = PromotionRepository(conn).get_by_uid(args.uid)
    if promotion is None:
        print(f"no promotion {args.uid}", file=sys.stderr)
        return 1

    print(gates.evidence(conn, promotion))

    print("\n## A4's recommendation")
    for key in (
        "decision", "rationale", "overfitting_risk", "confidence",
        "capacity_liquidity_ok", "recommended_allocation_pct", "iterations_considered",
    ):
        print(f"  {key:<26} {promotion.get(key)}")

    evaluation = EvaluationRepository(conn).latest_for_experiment(promotion["best_experiment_id"])
    if evaluation is not None:
        tests = EvaluationTestRepository(conn).for_evaluation(evaluation["id"])
        print(f"\n## checks ({len(tests)})")
        print(_table(tests, ["test_name", "category", "result", "gating", "value", "threshold"]))

        regimes = RegimePerformanceRepository(conn).for_evaluation(evaluation["id"])
        if regimes:
            print("\n## regime performance")
            print(_table(regimes, ["regime", "sharpe", "cagr", "max_drawdown", "trade_count"]))

    strategy = StrategyRepository(conn).get(promotion["strategy_id"])
    if strategy is not None:
        settings = get_settings()
        remaining = settings.vault_budget_per_family - VaultAccessRepository(conn).opens_for_family(
            strategy["family"]
        )
        print(f"\nvault budget remaining for family {strategy['family']!r}: {remaining}")
    return 0


def cmd_review_approve(args: argparse.Namespace) -> int:
    from . import gates
    from .db.repositories import PromotionRepository

    conn = connect()
    promotion = PromotionRepository(conn).get_by_uid(args.uid)
    if promotion is None:
        print(f"no promotion {args.uid}", file=sys.stderr)
        return 1
    result = gates.approve(
        conn,
        promotion["id"],
        by=args.by,
        note=args.note,
        vault_segment=args.vault_segment,
        capital_minor_units=args.capital,
    )
    print(f"promotion {args.uid} approved by {args.by}")
    print(f"  deployment {result['deployment_id']}  mode={result['mode']}  branch={result['deploy_branch']}")
    print(f"  merge commit  {result['merge_commit']}")
    print(f"  strategy status -> {result['strategy_status']}")
    return 0


def cmd_review_reject(args: argparse.Namespace) -> int:
    from . import gates
    from .db.repositories import PromotionRepository

    conn = connect()
    promotion = PromotionRepository(conn).get_by_uid(args.uid)
    if promotion is None:
        print(f"no promotion {args.uid}", file=sys.stderr)
        return 1
    result = gates.reject(
        conn,
        promotion["id"],
        by=args.by,
        note=args.note,
        reason=args.reason,
        next_questions=args.next_question,
    )
    print(f"promotion {args.uid} rejected by {args.by}")
    print(f"  knowledge_entries {result['knowledge_entry_id']}")
    print(f"  research_questions {result['research_question_ids']}")
    return 0


# -- wiring --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqrl", description="AQRL — research laboratory tooling")
    subs = parser.add_subparsers(dest="group", required=True)

    db = subs.add_parser("db", help="schema and migrations").add_subparsers(dest="cmd", required=True)
    p = db.add_parser("migrate", help="apply pending migrations")
    p.add_argument("--to", type=int, help="stop at this migration version")
    p.set_defaults(func=cmd_db_migrate)
    p = db.add_parser("status", help="show migration and table state")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_db_status)

    profile = subs.add_parser("profile", help="market/timeframe/cost profiles").add_subparsers(
        dest="cmd", required=True
    )
    profile.add_parser("list", help="list available profiles").set_defaults(func=cmd_profile_list)
    p = profile.add_parser("show", help="show a profile, or a resolved market x timeframe")
    p.add_argument("name")
    p.add_argument("--timeframe", help="resolve against this timeframe")
    p.add_argument("--asset-class", default="cash_equity")
    p.set_defaults(func=cmd_profile_show)
    p = profile.add_parser("hash", help="print a profile's content hash")
    p.add_argument("name")
    p.set_defaults(func=cmd_profile_hash)
    profile.add_parser("register", help="write profiles into the registry tables").set_defaults(
        func=cmd_profile_register
    )

    snapshot = subs.add_parser("snapshot", help="market data snapshots").add_subparsers(
        dest="cmd", required=True
    )
    p = snapshot.add_parser("ingest", help="ingest raw Parquet into an immutable snapshot")
    p.add_argument("--path", required=True, help="Parquet file or directory")
    p.add_argument("--market", required=True)
    p.add_argument("--timeframe", required=True)
    p.add_argument("--asset-class", required=True)
    p.add_argument("--adjustment-method", default="back_ratio_price",
                   choices=["back_ratio_price", "back_ratio_total_return", "none"])
    p.add_argument("--in-vault", action="store_true",
                   help="lock this data away from the research loop (TRD §15.2)")
    p.add_argument("--note")
    p.set_defaults(func=cmd_snapshot_ingest)
    snapshot.add_parser("list", help="list snapshots").set_defaults(func=cmd_snapshot_list)
    p = snapshot.add_parser("show", help="show one snapshot")
    p.add_argument("snapshot_id", type=int)
    p.set_defaults(func=cmd_snapshot_show)
    p = snapshot.add_parser("load", help="load a snapshot by ID")
    p.add_argument("snapshot_id", type=int)
    p.add_argument("--instrument")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--raw", action="store_true", help="skip adjustment (audit only, never research)")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_snapshot_load)

    actions = subs.add_parser("actions", help="corporate actions").add_subparsers(
        dest="cmd", required=True
    )
    p = actions.add_parser("import", help="import actions from CSV")
    p.add_argument("path", type=Path)
    p.set_defaults(func=cmd_actions_import)
    p = actions.add_parser("list", help="list actions")
    p.add_argument("--instrument")
    p.add_argument("--market", default="nse_equity")
    p.set_defaults(func=cmd_actions_list)

    membership = subs.add_parser("membership", help="point-in-time index membership").add_subparsers(
        dest="cmd", required=True
    )
    p = membership.add_parser("import", help="import membership history from CSV")
    p.add_argument("path", type=Path)
    p.add_argument("--index")
    p.set_defaults(func=cmd_membership_import)
    p = membership.add_parser("resolve", help="which instruments were members on a date")
    p.add_argument("--index", required=True)
    p.add_argument("--date", required=True)
    p.set_defaults(func=cmd_membership_resolve)

    operators = subs.add_parser("operators", help="the vetted operator library").add_subparsers(
        dest="cmd", required=True
    )
    p = operators.add_parser("list", help="list registered operators")
    p.add_argument("--category", choices=["transformation", "signal", "risk", "portfolio"])
    p.add_argument("--market", help="only operators valid for this market")
    p.add_argument("--timeframe", help="only operators valid for this timeframe")
    p.set_defaults(func=cmd_operators_list)
    p = operators.add_parser("show", help="show one operator's full declaration")
    p.add_argument("name")
    p.add_argument("--operator-version", help="defaults to the newest registered version")
    p.set_defaults(func=cmd_operators_show)
    operators.add_parser(
        "version", help="print operator_library_version (TRD 6.6)"
    ).set_defaults(func=cmd_operators_version)
    operators.add_parser(
        "sync", help="mirror the registry into the operators table"
    ).set_defaults(func=cmd_operators_sync)
    p = operators.add_parser("approve", help="record human sign-off on an operator")
    p.add_argument("name")
    p.add_argument("--by", required=True, help="who is approving")
    p.add_argument("--operator-version", help="defaults to the newest registered version")
    p.set_defaults(func=cmd_operators_approve)

    spec = subs.add_parser("spec", help="strategy specs (operator DAGs)").add_subparsers(
        dest="cmd", required=True
    )
    p = spec.add_parser("hash", help="print a spec's canonical hash")
    p.add_argument("path", type=Path)
    p.set_defaults(func=cmd_spec_hash)
    p = spec.add_parser("compile", help="validate a spec and describe what it computes")
    p.add_argument("path", type=Path)
    p.set_defaults(func=cmd_spec_compile)

    goal = subs.add_parser("goal", help="research goals (Stage 7 — A1)").add_subparsers(
        dest="cmd", required=True
    )
    p = goal.add_parser("new", help="create a research goal, driving A1's hypothesis budget")
    p.add_argument("--title", required=True)
    p.add_argument("--description")
    p.add_argument("--market", required=True)
    p.add_argument("--timeframe", required=True)
    p.add_argument(
        "--allocation-bucket",
        required=True,
        dest="allocation_bucket",
        choices=["incremental", "cross_market", "exploratory"],
        help="the 70/20/10 split (PRD §4.5)",
    )
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--hypothesis-budget", type=int, dest="hypothesis_budget", help="omit for unlimited")
    p.set_defaults(func=cmd_goal_new)
    p = goal.add_parser("list", help="list research goals")
    p.set_defaults(func=cmd_goal_list)

    strategy = subs.add_parser("strategy", help="strategies (Stage 5 — A2)").add_subparsers(
        dest="cmd", required=True
    )
    p = strategy.add_parser("new", help="seed a strategy from a hand-written spec, enqueue IMPLEMENT")
    p.add_argument("--spec", required=True, type=Path)
    p.add_argument("--name", required=True)
    p.add_argument("--family", required=True)
    p.add_argument("--market", required=True)
    p.add_argument("--timeframe", required=True)
    p.add_argument("--asset-class", required=True, dest="asset_class")
    p.add_argument("--snapshot-id", type=int, dest="snapshot_id", help="data_snapshot_id for EVALUATE")
    p.add_argument("--campaign", help="acceptance_bars.campaign_label to lock against")
    p.add_argument("--cost-multiplier", type=float, dest="cost_multiplier")
    p.add_argument("--seed", type=int)
    p.set_defaults(func=cmd_strategy_new)

    code = subs.add_parser("code", help="A2's generated code (Stage 5)").add_subparsers(
        dest="cmd", required=True
    )
    p = code.add_parser("show", help="show code_versions for one experiment")
    p.add_argument("experiment_id", type=int)
    p.add_argument("--all", action="store_true", help="list every attempt, not just the latest")
    p.set_defaults(func=cmd_code_show)

    agents = subs.add_parser("agents", help="the Claude session wrappers (Stages 5-6)").add_subparsers(
        dest="cmd", required=True
    )
    p = agents.add_parser("brief", help="print the Implementation (A2) or Review (A3) Brief without calling Claude")
    p.add_argument("--agent", choices=["a2", "a3"], default="a2", help="which agent's brief to print")
    p.add_argument("--strategy-id", type=int, required=True, dest="strategy_id")
    p.add_argument(
        "--experiment-id",
        type=int,
        dest="experiment_id",
        help="the experiment being iterated on/fixed (a2) or reviewed (a3, required)",
    )
    p.add_argument("--research-plan-id", type=int, dest="research_plan_id", help="a2 only")
    p.add_argument("--diagnostics-file", dest="diagnostics_file", help="JSON list of diagnostic strings (a2 only)")
    p.set_defaults(func=cmd_agents_brief)

    flags = subs.add_parser("flags", help="data validation flags").add_subparsers(
        dest="cmd", required=True
    )
    p = flags.add_parser("list", help="list flags")
    p.add_argument("--pending", action="store_true")
    p.add_argument("--snapshot-id", type=int)
    p.set_defaults(func=cmd_flags_list)
    p = flags.add_parser("resolve", help="record a human decision on a flag")
    p.add_argument("flag_id", type=int)
    p.add_argument("--resolution", required=True,
                   choices=["genuine_move", "missing_action_added", "data_error"])
    p.add_argument("--by", required=True, help="who is making this call")
    p.set_defaults(func=cmd_flags_resolve)

    evaluate = subs.add_parser("evaluate", help="the Stage 3 evaluation engine").add_subparsers(
        dest="cmd", required=True
    )
    p = evaluate.add_parser("run", help="run one experiment and persist the report")
    p.add_argument("--spec", required=True, type=Path, help="path to a StrategySpec JSON file")
    p.add_argument("--market", required=True)
    p.add_argument("--timeframe", required=True)
    p.add_argument("--asset-class", required=True)
    p.add_argument("--snapshot-id", required=True, type=int, dest="snapshot_id")
    p.add_argument("--family", default="unnamed_family")
    p.add_argument("--name", default="unnamed_strategy")
    p.add_argument("--campaign", help="acceptance_bars.campaign_label to lock against")
    p.add_argument("--seed", type=int, help="defaults to the experiment's iteration number")
    p.add_argument("--cost-multiplier", type=float, default=2.0, dest="cost_multiplier")
    p.add_argument("--code-commit", dest="code_commit")
    p.set_defaults(func=cmd_evaluate_run)

    p = evaluate.add_parser("show", help="show a stored experiment and its latest evaluation")
    p.add_argument("experiment_id", type=int)
    p.set_defaults(func=cmd_evaluate_show)

    p = evaluate.add_parser("null-world", help="re-run null-world calibration through the panel engine")
    p.add_argument("--spec", required=True, type=Path)
    p.add_argument("--market", default="nse_equity")
    p.add_argument("--timeframe", default="daily")
    p.add_argument("--asset-class", default="cash_equity")
    p.add_argument("--generator", choices=["permuted", "block_bootstrap", "synthetic_path"], required=True)
    p.add_argument("--replications", type=int, default=30)
    p.add_argument("--days", type=int, default=_NULL_WORLD_DEFAULT_DAYS)
    p.set_defaults(func=cmd_evaluate_null_world)

    jobs = subs.add_parser("jobs", help="the Stage 4 job queue").add_subparsers(dest="cmd", required=True)
    p = jobs.add_parser("list", help="list jobs")
    p.add_argument("--status")
    p.add_argument("--type")
    p.add_argument("--strategy-id", type=int, dest="strategy_id")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_jobs_list)
    p = jobs.add_parser("show", help="show one job")
    p.add_argument("uid")
    p.set_defaults(func=cmd_jobs_show)
    p = jobs.add_parser("enqueue", help="manually enqueue a job")
    p.add_argument("type")
    p.add_argument("--payload", help="JSON payload")
    p.add_argument("--strategy-id", type=int, dest="strategy_id")
    p.add_argument("--experiment-id", type=int, dest="experiment_id")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--dedupe-key", dest="dedupe_key")
    p.set_defaults(func=cmd_jobs_enqueue)
    p = jobs.add_parser("cancel", help="cancel a pending/claimed/running job")
    p.add_argument("uid")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_jobs_cancel)
    p = jobs.add_parser("retry", help="manually return a failed job to pending")
    p.add_argument("uid")
    p.set_defaults(func=cmd_jobs_retry)

    scheduler = subs.add_parser("scheduler", help="the always-on tick loop").add_subparsers(
        dest="cmd", required=True
    )
    p = scheduler.add_parser("run", help="run the scheduler")
    p.add_argument("--once", action="store_true", help="run a single tick and exit")
    p.add_argument("--tick-seconds", type=int, dest="tick_seconds")
    p.add_argument("--max-workers", type=int, dest="max_workers")
    p.add_argument("--time-budget-seconds", type=int, default=1800, dest="time_budget_seconds")
    p.set_defaults(func=cmd_scheduler_run)
    p = scheduler.add_parser("status", help="queue depth, running, idle cause, budgets")
    p.set_defaults(func=cmd_scheduler_status)

    budgets = subs.add_parser("budgets", help="back-pressure caps").add_subparsers(dest="cmd", required=True)
    budgets.add_parser("list", help="list configured budgets").set_defaults(func=cmd_budgets_list)
    p = budgets.add_parser("set", help="create or re-limit a budget")
    p.add_argument("--scope", required=True, choices=["global", "strategy", "goal"])
    p.add_argument("--scope-id", type=int, dest="scope_id")
    p.add_argument("--type", required=True, choices=["tokens", "experiments", "iterations", "compute_seconds", "usd"])
    p.add_argument("--period", required=True, choices=["day", "week", "lifetime"])
    p.add_argument("--limit", required=True, type=int)
    p.set_defaults(func=cmd_budgets_set)

    knowledge = subs.add_parser("knowledge", help="A5's knowledge base").add_subparsers(dest="cmd", required=True)
    p = knowledge.add_parser("rate", help="repeat-failure rate — Implementation_Plan §11's done-when")
    p.add_argument("--since", help="ISO-8601 timestamp; only experiments created at or after this")
    p.set_defaults(func=cmd_knowledge_rate)

    review = subs.add_parser("review", help="the human gates (Stage 9)").add_subparsers(
        dest="cmd", required=True
    )
    review.add_parser("list", help="promotions awaiting a human decision").set_defaults(func=cmd_review_list)
    p = review.add_parser("show", help="the full evidence package A4 saw, plus its recommendation")
    p.add_argument("uid", help="promotions.uid")
    p.set_defaults(func=cmd_review_show)
    p = review.add_parser("approve", help="merge to deploy/*, open the vault, create the deployment")
    p.add_argument("uid", help="promotions.uid")
    p.add_argument("--by", required=True, help="who is approving")
    p.add_argument("--note", required=True, help="mandatory — friction on purpose (Backend-Schema §7)")
    p.add_argument("--vault-segment", dest="vault_segment")
    p.add_argument("--capital", type=int, dest="capital", help="capital_minor_units")
    p.set_defaults(func=cmd_review_approve)
    p = review.add_parser("reject", help="reject, recording a structured reason as knowledge")
    p.add_argument("uid", help="promotions.uid")
    p.add_argument("--by", required=True, help="who is rejecting")
    p.add_argument("--note", required=True, help="mandatory typed note")
    p.add_argument("--reason", required=True, choices=sorted(_gates_failure_reasons()))
    p.add_argument(
        "--next-question", required=True, action="append", dest="next_question",
        help="repeatable; at least one required (future_ideas is mandatory, TRD §12.1)",
    )
    p.set_defaults(func=cmd_review_reject)

    return parser


def _gates_failure_reasons() -> frozenset[str]:
    """Deferred import — `aqrl.gates` pulls in `aqrl.vcs`, which is not
    importable on a platform without `fcntl`; nothing else in `build_parser`
    needs that module just to list `--reason`'s choices."""
    from .gates import FAILURE_REASONS

    return FAILURE_REASONS


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, LookupError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
