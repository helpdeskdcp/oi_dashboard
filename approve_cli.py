"""
approve_cli.py -- the human approval mechanism referenced by name in
three agent module docstrings since Milestone 1 (agents/audit_log.py,
agents/dev_agent/approval_engine.py, agents/quant_researcher/
research_engine.py) but never built until Milestone 9 -- the #1 finding
of AUTONOMOUS_READINESS_REPORT.md. A thin CLI wrapper: every action here
calls straight into agents.runtime.approval_engine, the exact same
functions a future dashboard/API/mobile approval channel would call, so
every channel produces the identical audit trail.

Usage:
    python3 approve_cli.py list                    # pending code/strategy proposals
    python3 approve_cli.py show <id>
    python3 approve_cli.py approve <id> --by "you"
    python3 approve_cli.py reject <id> --by "you"
    python3 approve_cli.py apply <id> --by "you"    # actually git-merges an approved proposal
    python3 approve_cli.py workflows                # workflows waiting on a runtime-level approval
    python3 approve_cli.py workflow-approve <id> --by "you" [--reason "..."]
    python3 approve_cli.py workflow-reject <id> --by "you" --reason "..."

Never touches a live broker session, never merges anything without an
explicit `apply` call, and `apply` only ever merges the exact branch
already recorded on an already-`approve`d proposal (see
agents/runtime/approval_engine.py's own docstring for the full safety
reasoning).
"""
import argparse
import json
import sys

from agents.runtime import approval_engine as ae


def _cmd_list(args):
    rows = ae.list_pending_proposals(agent=args.agent)
    if not rows:
        print("No pending proposals.")
        return
    for r in rows:
        # agents.audit_log.list_pending() returns payload_json as a raw
        # JSON string (unlike audit_log.get(), which parses it) -- see
        # that module's own list_pending()/get() docstrings.
        payload = json.loads(r["payload_json"]) if r.get("payload_json") else {}
        branch = payload.get("branch", "-")
        print(f"[{r['id']:>4}] {r['ts']}  agent={r['agent']:<18} branch={branch:<30} {r['description']}")


def _cmd_show(args):
    row = ae.audit_log.get(args.id)
    if row is None:
        print(f"No proposal with id={args.id}", file=sys.stderr)
        sys.exit(1)
    for key, value in row.items():
        print(f"{key}: {value}")


def _cmd_approve(args):
    row = ae.approve_proposal(args.id, approved_by=args.by)
    print(f"Proposal {args.id} approved by {args.by}. outcome={row['outcome']}")
    print(f"Run `python3 approve_cli.py apply {args.id}` to merge it.")


def _cmd_reject(args):
    row = ae.reject_proposal(args.id, rejected_by=args.by)
    print(f"Proposal {args.id} rejected by {args.by}. outcome={row['outcome']}")


def _cmd_apply(args):
    result = ae.apply_proposal(args.id, applied_by=args.by, repo_dir=args.repo_dir)
    print(f"Merged. commit={result.merge_commit_sha}")
    print(result.detail)


def _cmd_workflows(args):
    rows = ae.list_pending_workflows()
    if not rows:
        print("No workflows waiting for approval.")
        return
    for w in rows:
        print(f"[{w['id']:>4}] {w['symbol']:<12} stage={w['current_stage']:<16} created={w['created_ts']}")


def _cmd_workflow_approve(args):
    w = ae.approve_workflow(args.id, approved_by=args.by, reason=args.reason or "")
    print(f"Workflow {args.id} approved by {args.by}. status={w['status']} stage={w['current_stage']}")


def _cmd_workflow_reject(args):
    w = ae.reject_workflow(args.id, rejected_by=args.by, reason=args.reason)
    print(f"Workflow {args.id} rejected by {args.by}. status={w['status']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list pending code/strategy proposals")
    p_list.add_argument("--agent", default=None)
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="show full detail for one proposal")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(func=_cmd_show)

    p_approve = sub.add_parser("approve", help="mark a proposal approved (does not merge yet)")
    p_approve.add_argument("id", type=int)
    p_approve.add_argument("--by", required=True, help="your name, for the audit trail")
    p_approve.set_defaults(func=_cmd_approve)

    p_reject = sub.add_parser("reject", help="mark a proposal rejected")
    p_reject.add_argument("id", type=int)
    p_reject.add_argument("--by", required=True)
    p_reject.set_defaults(func=_cmd_reject)

    p_apply = sub.add_parser("apply", help="git-merge an already-approved proposal's branch")
    p_apply.add_argument("id", type=int)
    p_apply.add_argument("--by", required=True)
    p_apply.add_argument("--repo-dir", default=".")
    p_apply.set_defaults(func=_cmd_apply)

    p_workflows = sub.add_parser("workflows", help="list runtime workflows waiting for approval")
    p_workflows.set_defaults(func=_cmd_workflows)

    p_wf_approve = sub.add_parser("workflow-approve", help="approve a runtime workflow's Human Approval checkpoint")
    p_wf_approve.add_argument("id", type=int)
    p_wf_approve.add_argument("--by", required=True)
    p_wf_approve.add_argument("--reason", default="")
    p_wf_approve.set_defaults(func=_cmd_workflow_approve)

    p_wf_reject = sub.add_parser("workflow-reject", help="reject a runtime workflow's Human Approval checkpoint")
    p_wf_reject.add_argument("id", type=int)
    p_wf_reject.add_argument("--by", required=True)
    p_wf_reject.add_argument("--reason", required=True)
    p_wf_reject.set_defaults(func=_cmd_workflow_reject)

    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, ae.workflow_engine.WorkflowError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
