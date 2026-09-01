"""Brands, loaded from brands/<slug>/.

Was one module; split when the brief catalog pushed it past the point where the
D3 routing rule could be found in it. The cut is by reason-to-change:

- `context.py` -- the shapes and the exceptions. No I/O, no yaml.
- `loader.py`  -- reading brands/<slug>/ off disk. The only module here that
                  touches the filesystem.
- `routing.py` -- channel -> brand, alone because it is all of SPECS D3.

Import from this package, not from the submodules: the layout above is ours to
change, `from app.domain.brand import load_brand` is not.

One exception, for tests: `BRANDS_DIR` must be patched on `loader` itself
(`app.domain.brand.loader.BRANDS_DIR`). The name re-exported below is a copy
made at import, so rebinding it here would not redirect the loader.

Framework-free by contract: stdlib and PyYAML only (SPECS D1).
"""

from app.domain.brand.context import (
    DEFAULT_TIMEZONE,
    PLACEHOLDER,
    Account,
    BrandContext,
    BrandMisconfigured,
    BrandNotFound,
    BriefCatalog,
    normalise_channel,
    normalise_hashtag,
)
from app.domain.brand.loader import BRANDS_DIR, all_brands, load_brand
from app.domain.brand.routing import brand_for_channel

__all__ = [
    "BRANDS_DIR",
    "DEFAULT_TIMEZONE",
    "PLACEHOLDER",
    "Account",
    "BrandContext",
    "BrandMisconfigured",
    "BrandNotFound",
    "BriefCatalog",
    "all_brands",
    "brand_for_channel",
    "load_brand",
    "normalise_channel",
    "normalise_hashtag",
]
