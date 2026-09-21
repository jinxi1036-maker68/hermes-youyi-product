# Preserved historical assets

This candidate intentionally does not perform a broad deletion of code that
exists on the historical `main` branch. Some files such as legacy routing and
dashboard refresh helpers remain in the tree because historical retention is
not evidence of current runtime use.

The 1.2.19 production source import adds the current Work Runtime, Agenda,
trusted-turn, WeCom callback, durable reply recovery, governance, and current
dashboard components. The active deployment chain is represented by the
manifest and `deploy/` templates. A later, separately reviewed retirement
change may delete or archive obsolete assets after direct call-graph and
deployment review; this baseline does not silently do so.

The inherited `systemd/` units are not carried forward because deployment
inspection showed that they describe superseded 0.19/0.20 services and legacy
notification/autonomous paths. Their production-specific paths are unsuitable
as a reusable source baseline. The clean, version-neutral templates in
`deploy/systemd/` document the current three-process topology instead.
