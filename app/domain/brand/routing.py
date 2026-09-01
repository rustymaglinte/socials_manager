"""Channel -> brand. Kept alone because this is the whole of D3's enforcement.

One function, deliberately not buried under the yaml plumbing in loader.py: a
wrong answer here puts one brand's draft in another brand's channel, which is
the failure mode SPECS D3 exists to prevent.
"""

from app.domain.brand.context import BrandContext, BrandNotFound, normalise_channel
from app.domain.brand.loader import all_brands


def brand_for_channel(channel: str) -> BrandContext:
    """Map a Slack channel to its brand.

    Takes a channel NAME -- brand.yaml declares names (`#derekt-socials`), while
    Slack events carry C0... ids. The transport resolves the id once, via
    conversations.info, and passes the name here.
    """
    wanted = normalise_channel(channel)
    for brand in all_brands():
        if brand.slack_channel == wanted:
            return brand
    raise BrandNotFound(f"No brand bound to channel {channel!r}")
