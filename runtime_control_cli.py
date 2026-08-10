"""
runtime_control_cli.py -- Milestone 12, Phase 2 Foundation: the
operator-facing kill switch and agent scheduling controls this
milestone's own planning review flagged as a hard prerequisite before
any Phase 2A activation work ("neither policy_engine.set_policy() nor
orchestrator.enable_agent()/disable_agent() has an operator-facing CLI
or route -- invoking either requires a raw Python shell call today").

A thin CLI wrapper, following the exact precedent approve_cli.py already
established: every action here calls straight into agents.runtime.
policy_engine / agents.runtime.scheduling_control -- the exact same
functions a future dashboard/API channel would call, so every channel
produces the identical, durable, audited state.

Usage:
    python3 runtime_control_cli.py pause --by "you" --reason "..."
        # global kill switch: sets policy to emergency_stop -- the
        # scheduler stops running ANY agent from its very next tick,
        # no restart needed (agents.runtime.scheduler.tick() checks
        # this first, before anything else).

    python3 runtime_control_cli.py resume --by "you" --reason "..." [--policy recommendation_only]
        # clears emergency_stop, restoring the given policy (default:
        # agents.config.RUNTIME_DEFAULT_POLICY).

    python3 runtime_control_cli.py enable-agent <agent> --by "you" --reason "..."
    python3 runtime_control_cli.py disable-agent <agent> --by "you" --reason "..."
    python3 runtime_control_cli.py dry-run-agent <agent> --by "you" --reason "..."
        # per-agent scheduling mode. Refuses (prints an error, exits
        # non-zero) for quant_researcher/shadow_mode under any mode,
        # including "enable" -- see agents.runtime.scheduling_control.
        # NEVER_SCHEDULABLE_AGENTS's own docstring. trading_intelligence
        # is no longer in that set as of Milestone 17 -- this command
        # now accepts any mode for it, same as any other agent.

    python3 runtime_control_cli.py status
        # read-only: active policy, emergency-stop state, and every
        # agent's schedulability + current mode. The exact same data
        # /api/runtime/status (agents.runtime.lifecycle.get_runtime_status())
        # exposes over HTTP, for a human at a terminal instead of a
        # dashboard.

Never touches a live broker session, never starts or stops the scheduler
process itself (that remains config.RUNTIME_SCHEDULER_ENABLED, a
restart-required setting, deliberately outside this CLI's scope --
pause/resume here is the LIVE, no-restart-needed lever).
"""
import argparse
import sys

from agents import config
from agents.runtime import lifecycle, policy_engine, scheduling_control


def _cmd_pause(args):
    policy_engine.set_policy(policy_engine.EMERGENCY_STOP, changed_by=args.by, reason=args.reason)
    print(f"PAUSED. All schedulable agents stop from the scheduler's next tick. (by={args.by})")


def _cmd_resume(args):
    policy = args.policy or config.RUNTIME_DEFAULT_POLICY
    policy_engine.set_policy(policy, changed_by=args.by, reason=args.reason)
    print(f"RESUMED. Active policy is now {policy!r}. (by={args.by})")


def _cmd_enable_agent(args):
    try:
        scheduling_control.set_mode(args.agent, scheduling_control.ENABLED, changed_by=args.by, reason=args.reason)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"{args.agent}: schedule_mode=enabled (by={args.by})")


def _cmd_disable_agent(args):
    try:
        scheduling_control.set_mode(args.agent, scheduling_control.DISABLED, changed_by=args.by, reason=args.reason)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"{args.agent}: schedule_mode=disabled (by={args.by})")


def _cmd_dry_run_agent(args):
    try:
        scheduling_control.set_mode(args.agent, scheduling_control.DRY_RUN, changed_by=args.by, reason=args.reason)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"{args.agent}: schedule_mode=dry_run (by={args.by})")


def _cmd_status(args):
    status = lifecycle.get_runtime_status()
    control = status.get("control")
    print(f"scheduler_state:  {status['scheduler_state']}")
    print(f"cycles_executed:  {status['cycles_executed']}")
    if control is None:
        print("control-plane state: unavailable (see logs)")
        return
    print(f"active_policy:    {control['active_policy']}")
    print(f"emergency_stop:   {control['emergency_stop']}")
    print("agents:")
    for agent, info in control["agents"].items():
        schedulable = "schedulable" if info["schedulable"] else "NEVER SCHEDULABLE"
        print(f"  {agent:<20} {schedulable:<18} mode={info['mode']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_pause = sub.add_parser("pause", help="global kill switch: emergency_stop, effective immediately")
    p_pause.add_argument("--by", required=True, help="your name, for the audit trail")
    p_pause.add_argument("--reason", required=True)
    p_pause.set_defaults(func=_cmd_pause)

    p_resume = sub.add_parser("resume", help="clear emergency_stop and restore a policy")
    p_resume.add_argument("--by", required=True)
    p_resume.add_argument("--reason", required=True)
    p_resume.add_argument("--policy", default=None, help=f"default: {config.RUNTIME_DEFAULT_POLICY!r}")
    p_resume.set_defaults(func=_cmd_resume)

    p_enable = sub.add_parser("enable-agent", help="set one agent's schedule_mode to enabled")
    p_enable.add_argument("agent")
    p_enable.add_argument("--by", required=True)
    p_enable.add_argument("--reason", required=True)
    p_enable.set_defaults(func=_cmd_enable_agent)

    p_disable = sub.add_parser("disable-agent", help="set one agent's schedule_mode to disabled")
    p_disable.add_argument("agent")
    p_disable.add_argument("--by", required=True)
    p_disable.add_argument("--reason", required=True)
    p_disable.set_defaults(func=_cmd_disable_agent)

    p_dry_run = sub.add_parser("dry-run-agent", help="set one agent's schedule_mode to dry_run")
    p_dry_run.add_argument("agent")
    p_dry_run.add_argument("--by", required=True)
    p_dry_run.add_argument("--reason", required=True)
    p_dry_run.set_defaults(func=_cmd_dry_run_agent)

    p_status = sub.add_parser("status", help="read-only: policy + every agent's schedulability/mode")
    p_status.set_defaults(func=_cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
