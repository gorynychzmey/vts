"""Helpers shared by the domain routers in `vts.api.routers`.

These lived in `vts.api.main` while the routers were being split out of
`create_app()`, and the routers reached them through a late-bound `_main()`
accessor to dodge an import cycle. They are here now because most are used by
more than one router — `serialize_task` by four — so parking them in any single
router would have made the others import it sideways.

Nothing here imports `vts.api.main` or `vts.api.routers.*`: the dependency
edges only ever point inwards, which is what lets the routers import these at
module scope.
"""
