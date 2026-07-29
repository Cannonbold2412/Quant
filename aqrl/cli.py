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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, LookupError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
