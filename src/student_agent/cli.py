from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .gateway_compat import connect_compat_gateway as connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import CaseTraceBuffer, TraceWriter
from .workflow import solve_case

MAX_RECONNECTS = 8  # consecutive session drops tolerated before giving up


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _resume_state(trace_path: Path, output_root: Path) -> tuple[set[str], str | None]:
    """Cases already finalized (trace + output) by an interrupted run, and that run's id.

    The trace is rewritten to keep only those cases, so a case cut off mid-write is redone.
    """
    if not trace_path.exists():
        return set(), None
    events = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # torn last line
    finalized = {e["case_id"] for e in events if e.get("event_type") == "case_finalized"}
    # Cases that only produced the fallback answer (e.g. the gateway was erroring) are redone.
    fallback = {
        e["case_id"]
        for e in events
        if e.get("event_type") == "verification_completed"
        and str(e.get("decision_code") or "").startswith("FALLBACK")
    }
    done = {
        case_id
        for case_id in finalized - fallback
        if (output_root / f"{case_id}.json").exists()
    }
    run_ids = [
        (e.get("attributes") or {}).get("run_id") for e in events if e.get("case_id") in done
    ]
    kept = [e for e in events if e.get("case_id") in done]
    lines = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in kept]
    trace_path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return done, next((r for r in run_ids if r), None)


async def _run(root: Path, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    run_id = None
    if resume:
        done, run_id = _resume_state(trace_path, output_root)
    else:
        trace_path.unlink(missing_ok=True)
    for stale in output_root.glob("*.json"):
        if stale.stem not in done:
            stale.unlink()
    trace = TraceWriter(trace_path, contracts)
    if run_id:
        trace.run_id = run_id  # continue the interrupted run under the same correlation id
    if resume:
        print(f"RESUME: {len(done)} cases kept, {len(case_set.case_ids) - len(done)} pending",
              file=sys.stderr)

    pending = [case_id for case_id in case_set.case_ids if case_id not in done]
    failures = 0
    while pending:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                discovered_tools = await gateway.list_tools()
                if not discovered_tools:
                    raise RuntimeError("MCP Gateway returned no tools")
                while pending:
                    await _run_case(case_set.cases[pending[0]], gateway, trace, output_root)
                    pending.pop(0)
                    failures = 0
        except Exception as exc:  # dropped MCP session: reconnect and resume the pending case
            failures += 1
            if failures > MAX_RECONNECTS:
                raise RuntimeError(
                    f"MCP session failed {failures} times in a row at {pending[0]}; "
                    "rerun with `day09 run --resume` to continue"
                ) from exc
            print(
                f"WARN: MCP session dropped at {pending[0]} ({type(exc).__name__}); "
                f"reconnecting {failures}/{MAX_RECONNECTS}",
                file=sys.stderr,
            )
            await asyncio.sleep(min(30, 3 * failures))


async def _run_case(case: dict, gateway, trace: TraceWriter, output_root: Path) -> None:
    case_id = case["case_id"]
    buffer = CaseTraceBuffer(trace)
    run = {"run_id": trace.run_id}
    buffer.emit(case_id=case_id, event_type="case_received", actor="coordinator", attributes=run)
    output = await solve_case(case, gateway, buffer)
    contracts = trace.contracts
    contracts.validate_output(output, f"outputs/{case_id}.json")
    if output.get("case_id") != case_id:
        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
    buffer.emit(case_id=case_id, event_type="case_finalized", actor="coordinator", attributes=run)
    target = output_root / f"{case_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    buffer.commit()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume",
        action="store_true",
        help="keep cases finalized by an interrupted run and only run the remaining ones",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
