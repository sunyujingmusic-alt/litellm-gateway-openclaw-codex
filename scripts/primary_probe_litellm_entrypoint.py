#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

from fastapi import Header, HTTPException

import litellm.proxy.proxy_server as proxy_server
from litellm import run_server
from litellm.router_utils.cooldown_cache import CooldownCache


ADMIN_KEY = os.environ.get("PRIMARY_PROBE_ADMIN_KEY", "")


async def clear_router_cooldown(
    deployment_id: str,
    x_probe_admin_key: str = Header(default="", alias="X-Probe-Admin-Key"),
) -> dict:
    if not ADMIN_KEY or x_probe_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="unauthorized")

    llm_router = proxy_server.llm_router
    if llm_router is None:
        raise HTTPException(status_code=503, detail="llm_router is not initialized")

    if deployment_id not in set(llm_router.get_model_ids()):
        raise HTTPException(status_code=404, detail=f"unknown deployment_id: {deployment_id}")

    cache_key = CooldownCache.get_cooldown_cache_key(deployment_id)
    await llm_router.cooldown_cache.cache.async_delete_cache(cache_key)
    return {"ok": True, "deployment_id": deployment_id, "cache_key": cache_key}


proxy_server.app.add_api_route(
    "/internal/router/cooldown/{deployment_id}",
    clear_router_cooldown,
    methods=["DELETE"],
    include_in_schema=False,
)


if __name__ == "__main__":
    sys.exit(run_server())
