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
