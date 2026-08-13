"""Compatibility entry point for Xiaoyou stale-state repair.

The former one-off implementation matched a fixed date, focus key and sentence.
Current repair is lifecycle-based: only an open work item whose own material says
it was merged or stopped is eligible, and dry-run remains the default.
"""

from __future__ import annotations

from repair_semantically_retired_work_items import main


if __name__ == "__main__":
    raise SystemExit(main())
