# Personnel identity authority rollout

This runbook activates a narrow, reversible-by-deployment identity cutover.
It is not a student, service-relationship, delivery, Agenda, or data-cleanup
migration.

## Authority after activation

For an authenticated WeCom callback, the verified `userid` is the sole lookup
key. `personnel_service_governance_v1.json` then provides the current role and
lifecycle state. A person is approved only when their person row is `active`
and they have exactly one current active employment. The aggregate must contain
exactly one active `boss` employment. Display names remain presentation only.

The old `wecom_whitelist.json`, `staff.json`, aliases and historical data stay
on disk. They are not deleted or rewritten by this rollout. Once authority is
enforced, they no longer decide inbound approval or role.

## Preconditions

- Candidate code has passed its isolated identity-authority tests.
- The production Workspace has been backed up by the normal operator process.
- The operator knows the server tenant identifier.
- The preflight has no blockers. In particular, it must find exactly one
  approved active boss and no approved identity that legacy staff data marks as
  suspended or left.

## Two-step operator procedure

Run the preflight first; it only reads data:

```bash
python deploy/prepare_identity_authority_migration.py \
  --data-dir /path/to/workspace/data --tenant-id "$HERMES_TENANT_ID"
```

Review the count-only JSON result and resolve every listed blocker through an
explicit owner decision. Do not edit the generated aggregate by hand.

After approval, run the same command with `--apply`. The command creates a new
authority aggregate and verifies its writeback. It refuses to overwrite an
existing governance aggregate.

The deployed gateway must then be restarted through the normal reviewed
deployment procedure so future turns load the new candidate source. This
repository task does not perform that production step.

## Required real WeCom acceptance

After deployment, test with real, already-bound accounts:

1. An active teacher can use an allowed read operation.
2. Suspend that teacher through the authority workflow; the next WeCom message
   must be denied without a gateway session reset.
3. Restore the person; the next message must use the current role.
4. Change a teacher/manager role through the authority workflow; the next
   message must reflect the new role.
5. Start a new Hermes session and restart the gateway through the normal
   release procedure; the same WeCom `userid` must retain its identity,
   tenant, role and Permission boundary.
6. Confirm that a second boss cannot be activated and that a current boss
   cannot be suspended/offboarded until a valid successor role change exists.

If any result differs, roll back the deployed code through the normal release
path and preserve the Workspace aggregate for audit; do not restore old roles
by editing histories or deleting the new authority document.
