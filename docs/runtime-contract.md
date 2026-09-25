# XiaoYou Runtime Contract

XiaoYou remains the business capability layer. Hermes is the model and agent
runtime, reached only through supported extension surfaces: plugin loading,
platform adapters, public agent-turn ingress, tools, skills, sessions/context,
and streaming.

## Truth boundaries

| Boundary | Authority |
| --- | --- |
| Trusted actor, tenant, role, source and continuity | Runtime Contract / trusted ingress |
| Business permission and data scope | Permission layer |
| Protected execution and idempotency | CommandBus |
| Business completion | ExecutionReceipt plus writeback verification |
| Work fact delivery and wake timing | Durable Work Runtime / Agenda |
| Natural-language business decision and tool selection | Hermes agent using XiaoYou capability |
| User-visible reply | Same Hermes final reply or same-Hermes reply-only recovery |
| Delivery | Durable Reply Outbox / channel adapter |

The Work Runtime, Agenda, Reply Outbox, and adapters are content-blind. They
do not classify a message, select a business tool, decide business success, or
invent a business reply. A delivery retry never replays a completed protected
business operation.

## Tenant and Workspace isolation

The Institution Workspace contains runtime data and is external to both Hermes
and this source tree. A service identity is created only by trusted server-side
ingress. Model text and client input cannot override the canonical identity,
tenant, role, recipient, or device binding.


## Release and persistent runtime-state topology

Code release and Hermes profile state are separate authorities.

| Layer | Contract |
| --- | --- |
| Versioned release | Immutable code, candidate-local console/venv, Hermes runtime modules, XiaoYou plugins and adapters |
| Persistent `HERMES_HOME` | Version-neutral Hermes profile state such as sessions, cron, state database and runtime logs |
| Deployment config | Stable protected environment/config paths outside the release |
| Institution Workspace | Institution business runtime data, external to both the release and Hermes Home |
| Agenda/business runtime | Governed separately from Hermes profile state and never migrated as a side effect of release deployment |

A candidate must not use a release-local `HERMES_HOME` or cross-release Home symlinks.
All runtime code origins must resolve to the active candidate release. All commands that may
initialize or mutate `HERMES_HOME` run under the same service identity as the Gateway.
A failed first switch preserves the old release and old Home unchanged; new state is never
reverse-synchronized into the rollback Home.
