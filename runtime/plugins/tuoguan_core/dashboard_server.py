"""XiaoYou-owned HTTP entry point for the read-only institution dashboard.

The dashboard is deliberately a small sidecar rather than a route patched into
Hermes' bundled WeCom callback adapter.  It serves only the existing dashboard
views and their authenticated read APIs from the current Institution Workspace;
it has no Agent, Tool, Reply Outbox, or delivery responsibilities.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web

from .dashboard_http import TuoguanDashboardHttp, dashboard_enabled


logger = logging.getLogger(__name__)


def build_dashboard_app() -> web.Application:
    """Build the public read-only dashboard application.

    Refusing to start when the feature is disabled prevents a stale service
    from silently exposing a dashboard after the owning XiaoYou runtime has
    been deliberately disabled.
    """

    if not dashboard_enabled():
        raise RuntimeError("XiaoYou dashboard is disabled by runtime configuration")

    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "xiaoyou-dashboard"})

    app.router.add_get("/health", health)
    TuoguanDashboardHttp().register(app)
    return app


def main() -> None:
    host = str(os.getenv("XIAOYOU_DASHBOARD_BIND_HOST") or "127.0.0.1").strip()
    try:
        port = int(str(os.getenv("XIAOYOU_DASHBOARD_PORT") or "8877"))
    except ValueError as exc:
        raise RuntimeError("XIAOYOU_DASHBOARD_PORT must be an integer") from exc
    if not host or not 1 <= port <= 65535:
        raise RuntimeError("Invalid XiaoYou dashboard bind address")

    logger.info("Starting XiaoYou dashboard sidecar on %s:%d", host, port)
    web.run_app(build_dashboard_app(), host=host, port=port, access_log=logger)


if __name__ == "__main__":
    main()
