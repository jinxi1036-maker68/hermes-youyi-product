# Production-source provenance map

This map records the source assets imported into the 1.2.19 candidate from the
running XiaoYou capability layer. It is intentionally path-level: runtime
state, configuration, records, recipients, credentials, and logs are absent.

| Current capability | Candidate source assets |
| --- | --- |
| Trusted ingress, identity, object continuity | `tuoguan_core/runtime_contract.py`, `identity.py`, `business_object_context.py`, `domain_runtime.py` |
| Permission, command execution, receipts and writeback | `permissions.py`, `tool_service.py`, `execution_receipts.py`, `write_guard.py`, `work_runtime_receipts.py` |
| WeCom primary channel | `platforms/wecom/`, `platforms/http_policy.py`, `wecom_outbox_port.py` |
| Work Inbox, trusted partition and durable work execution | `work_runtime.py`, `work_runtime_channels.py`, `agenda_service_work/` |
| Agenda, Wake and continuation | `agenda_runtime.py`, `agenda_service_policy.py`, `agenda_task_lifecycle.py` |
| Reply Truth and recovery | `reply_recovery/`, `direct_reply_recovery.py`, `runtime_foundation.py` |
| Personnel/service governance and progressive claims | `personnel_service_governance_v1.py`, `personnel_service_tool_port_v1.py`, `governance_claims_v1.py`, `governance_claims_tool_surface_v1.py`, `student_directory.py` |
| Proactive delivery authority | `proactive_delivery_authority.py`, `outbound_policy.py` |
| Dashboard | `dashboard_server.py`, `dashboard_http.py`, `dashboard_builder.py` |
| Controlled Robot channel | `robot_poc/` |
| Local model-visible XiaoYou skills | `runtime/skills/xiaoyou-core-contract/`, `runtime/skills/xiaoyou-observation-evidence/` |
| Focused non-live regressions | `runtime/tests/production/` |

The `runtime/plugins/tuoguan_core` package contains additional supporting
modules from the current production capability layer. Paths in the table are
the high-value entrypoints, not an exclusive import list.

## Not evidence of current runtime use

- `work/` and historical `systemd/` assets are excluded from this candidate.
- Historical routing files inherited from `main` are retained only as source
  history. They are not described as the active WeCom or Work Runtime path.
- Scripts under `scripts/` are retained as historical operator utilities and
  are not service entrypoints; see `scripts/README.md`.
