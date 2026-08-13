"""Home Assistant runtime configuration, and the intent index built from it.

Shared by start-up (which applies whatever the admin saved last) and by the admin route that saves
it, which is the whole reason it is a module: the two must do the same work in the same order, and
that order is load-bearing — see _refresh_names_and_router.
"""
import json
import threading

import deps
import ha
import intent_router
import memory
from config import HA_TOKEN_FROM_ENV, HA_URL_FROM_ENV, logger
from db import PRIMARY_HOUSEHOLD_ID, get_household_setting


def load_settings():
    """Apply the DB-stored (admin-UI-managed) Home Assistant settings at startup. Environment vars
    win — a field set via env stays as config.py resolved it and the UI shows it read-only."""
    try:
        deps.set_ha_household(PRIMARY_HOUSEHOLD_ID)
        url = None if HA_URL_FROM_ENV else get_household_setting(deps.HA_HOUSEHOLD_ID, "ha_url")
        token = None if HA_TOKEN_FROM_ENV else get_household_setting(deps.HA_HOUSEHOLD_ID, "ha_token")
        ents_raw = get_household_setting(deps.HA_HOUSEHOLD_ID, "ha_allowed_entities")
        allowed = None
        if ents_raw is not None:
            try:
                allowed = json.loads(ents_raw)
            except (ValueError, TypeError):
                allowed = []
        ha.configure(url=url, token=token, allowed=allowed, household_id=deps.HA_HOUSEHOLD_ID)
    except Exception as e:
        logger.warning("Could not load Home Assistant settings from DB: %s", e)


def _refresh_names_and_router():
    """Cache the entities' friendly names, then embed the router exemplars built FROM them.

    Ordering is the point: exemplars and device resolution both key on the display name, so the
    names have to land first or the router indexes machine ids ("turn on the 4node smart switch
    switch 3") for the lifetime of the process. Both are network/CPU work, so this runs off-request
    — at startup and whenever an admin saves the smart-home config.
    """
    try:
        cached = ha.refresh_names()
        if cached:
            logger.info("Home Assistant: cached %d entity names", cached)
    except Exception as e:
        logger.warning("Home Assistant name refresh failed (%s) — resolution falls back to ids", e)
    if memory.vectors_available():
        intent_router.rebuild(memory.embed_documents)


def rebuild_intent_router():
    """Kick the name+exemplar refresh in the background (startup + whenever the allowlist changes)."""
    if not (ha.configured() and ha.HA_ALLOWED_ENTITIES):
        return
    threading.Thread(target=_refresh_names_and_router, daemon=True).start()
