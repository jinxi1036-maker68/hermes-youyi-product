# Historical operator scripts

Scripts in this directory are retained from the historical repository for
review and possible future adaptation. They are not the active 1.2.19 service
entrypoints and must not be run against a live institution by default.

The active topology for this baseline is represented only by `deploy/` and
`runtime/xiaoyou_capability_manifest.yaml`. Any script that refers to an old
runtime path, legacy notification worker, legacy Workspace, historical tenant
fixture, or deprecated service unit requires a separately reviewed migration
before use.
