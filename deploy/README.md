# Deployment templates

The files in this directory are non-secret templates for a Hermes 0.21 and
XiaoYou deployment. They must not be copied over a live service unit. Replace
all `${...}` placeholders outside Git, keep the Workspace and credential files
outside the checkout, review the effective systemd unit with `systemctl cat`,
and run the candidate certification suite before enabling services.

The gateway, Agenda/Wake coordinator, and dashboard are separate processes.
They share the same capability source and the same external Institution
Workspace, but no production data belongs in this repository.
