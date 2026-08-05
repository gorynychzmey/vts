# vts-delivery-contract

The contract a VTS **delivery adapter** must implement, packaged so that
plugins living in other repositories can depend on it.

VTS itself is not a distributable package — it reaches its image as a source
tree — so a plugin cannot `pip install vts` to get `DeliveryAdapter`. This
package exists to close that gap and nothing else: it ships one module, has no
dependencies, and installs the contract at its real import path.

## Install

```toml
dependencies = [
  "vts-delivery-contract @ git+https://github.com/gorynychzmey/vts.git#subdirectory=packages/vts-delivery-contract",
]
```

## Use

```python
from vts.delivery.contract import (
    CONTRACT_VERSION, DeliveryAdapter, DeliveryPayload,
    DeliveryResult, DeliveryTargetConfig, DeliveryError,
)


class MyAdapter:
    name = "my-adapter"
    contract_version = (1, 1)   # the MINIMUM core this adapter needs

    def config_schema(self) -> dict: ...
    def secret_keys(self) -> list[str]: ...
    def connection_fields(self) -> list[str]: ...
    async def deliver(self, payload, target): ...
```

Register it under the `vts.delivery` entry-point group:

```toml
[project.entry-points."vts.delivery"]
my-adapter = "my_package:MyAdapter"
```

## Versioning

This package's version tracks `CONTRACT_VERSION` in the contract module.

- **major** — something was removed, renamed, or changed meaning. Adapters
  built against the old major stop loading, deliberately and with a stated
  reason. A major bump calls for a review of plugin *code*, not just a rebuild.
- **minor** — additions only. Existing adapters keep loading.

An adapter declares the *minimum* contract it needs. The core loads it when
`plugin.major == core.major and plugin.minor <= core.minor`, so an adapter
should ask for the lowest version that actually supports what it uses.

Note that the package version is moved by hand when the contract changes
materially, so it may lag a trivial edit to the module.

## Source of truth

The module is not stored in this directory. It is force-included from
`vts/delivery/contract.py` at build time, so there is exactly one copy and it
cannot drift from the core that enforces it.
