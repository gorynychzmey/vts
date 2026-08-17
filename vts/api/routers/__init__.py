"""Domain routers split out of the former monolithic `create_app()`.

Each module here owns one path prefix and exposes a module-level `router`
that `vts.api.main` mounts. See docs/plans/main-py-split.md.
"""
