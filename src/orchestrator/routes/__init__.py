"""HTTP routers.

main.py was ~3,500 lines and 81 routes; the route handlers now live here, grouped by the surface
they serve, and main.py keeps what is genuinely global: the app, the auth/rate-limit middleware,
the security headers, start-up, and the static mounts.

Every module here imports `deps` and calls the guards through it (`deps.require_admin(request)`)
rather than importing the names — see deps.py for why that matters.
"""
