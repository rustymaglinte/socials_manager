"""app.transports.slack_approval.client -- brand <-> channel routing.

The channel map is D3's enforcement at the transport layer: outbound, the brand
picks the channel; inbound, the channel picks the brand. A wrong answer either
way is a brand-isolation break, so both directions are tested, including the
"no such brand" paths that must raise rather than fall back to a default.

No socket is opened here. `start_listener` is only exercised on its validation
path, which returns before any connection is attempted.
"""

import pytest

from app.domain.brand import BrandNotFound
from app.transports.slack_approval import client


@pytest.fixture
def channels(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Both brands routed, as a working .env would have them."""
    mapping = {"derekt": "C0DEREKT", "personal": "C0PERSONAL"}
    for slug, channel_id in mapping.items():
        monkeypatch.setenv(client.channel_env_var(slug), channel_id)
    return mapping


def test_channel_env_var_names_the_variable_from_the_slug():
    assert client.channel_env_var("derekt") == "SLACK_DEREKT_CHANNEL_ID"


def test_channel_for_returns_the_configured_channel(two_brands, channels):
    assert client.channel_for("derekt") == "C0DEREKT"
    assert client.channel_for("personal") == "C0PERSONAL"


def test_channel_for_an_unrouted_brand_names_the_variable_to_set(
    two_brands, monkeypatch
):
    monkeypatch.delenv("SLACK_DEREKT_CHANNEL_ID", raising=False)

    with pytest.raises(BrandNotFound, match="SLACK_DEREKT_CHANNEL_ID"):
        client.channel_for("derekt")


def test_the_channel_an_event_arrives_in_decides_the_brand(two_brands, channels):
    assert client.brand_for_channel_id("C0DEREKT").slug == "derekt"
    assert client.brand_for_channel_id("C0PERSONAL").slug == "personal"


def test_an_unrouted_channel_raises_rather_than_picking_a_brand(two_brands, channels):
    with pytest.raises(BrandNotFound):
        client.brand_for_channel_id("C0RANDOM")


def test_the_channel_map_is_read_from_the_environment_on_each_call(
    two_brands, channels, monkeypatch
):
    """Frozen at import, a test could never repoint it -- and neither could a redeploy."""
    monkeypatch.setenv("SLACK_DEREKT_CHANNEL_ID", "C0MOVED")

    assert client.channel_for("derekt") == "C0MOVED"
    assert client.brand_for_channel_id("C0MOVED").slug == "derekt"


async def test_start_listener_refuses_to_run_with_a_brand_that_has_no_channel(
    two_brands, channels, monkeypatch
):
    """The failure that would otherwise surface hours later, at posting time."""
    monkeypatch.delenv("SLACK_PERSONAL_CHANNEL_ID")

    with pytest.raises(RuntimeError, match="SLACK_PERSONAL_CHANNEL_ID"):
        await client.start_listener()


async def test_start_listener_refuses_to_run_without_credentials(
    two_brands, channels, monkeypatch
):
    monkeypatch.setattr(client, "APP_TOKEN", None)

    with pytest.raises(RuntimeError, match="SLACK_APP_TOKEN"):
        await client.start_listener()


# --- the configuration mistake that looks like working software --------------
#
# Every other routing error announces itself: a missing variable refuses to
# boot, an unknown channel raises. Two brands sharing one channel id does
# neither. `brand_for_channel_id` scans the map and returns the first match, so
# one brand simply answers for the other -- drafts for Derekt posted into the
# personal channel, under Derekt's voice, with nothing in any log to say so.
#
# It is also the likeliest mistake to actually make. The ids are opaque C0...
# strings pasted one after another into a deployment's environment, where a
# duplicated clipboard is invisible by construction.


class _WouldConnect(Exception):
    """Raised in place of opening a socket, so 'did it get that far' is testable."""


@pytest.fixture
def no_socket(monkeypatch: pytest.MonkeyPatch):
    """`start_listener` stopped exactly where it would reach for the network.

    Not merely tidiness. Every test here is about the validation that runs
    *before* the socket, and a test asserting a refusal that has not been
    implemented yet sails straight past the missing guard and tries to reach
    Slack with conftest's fake token -- which does not fail, it hangs and
    retries. So the stub is what keeps a red test fast instead of infinite.
    """
    # A previous connection would make start_listener return early.
    monkeypatch.setattr(client, "_handler", None)

    def refuse(*_args, **_kwargs):
        raise _WouldConnect

    monkeypatch.setattr(client, "AsyncSocketModeHandler", refuse)


async def test_start_listener_refuses_two_brands_sharing_one_channel(
    two_brands, no_socket, monkeypatch
):
    """The one routing error that is otherwise silent (SPECS D3)."""
    monkeypatch.setenv("SLACK_DEREKT_CHANNEL_ID", "C0SHARED")
    monkeypatch.setenv("SLACK_PERSONAL_CHANNEL_ID", "C0SHARED")

    with pytest.raises(RuntimeError) as caught:
        await client.start_listener()

    assert not isinstance(caught.value, _WouldConnect), (
        "a duplicated channel id has to be caught before the socket opens"
    )
    message = str(caught.value)
    assert "C0SHARED" in message
    # Both slugs, because the fix is to work out which of them is wrong.
    assert "derekt" in message
    assert "personal" in message


async def test_distinct_channels_are_not_mistaken_for_a_collision(
    two_brands, channels, no_socket
):
    """The guard must not fire on a configuration that is actually correct.

    Reaching the socket is the pass condition here: it means validation had no
    complaint and handed off to the thing this test refuses to let it do.
    """
    with pytest.raises(_WouldConnect):
        await client.start_listener()


async def test_stop_listener_without_a_connection_is_a_no_op():
    await client.stop_listener()  # must not raise


async def test_update_message_rewrites_in_place_with_a_text_fallback():
    calls = []

    class FakeClient:
        async def chat_update(self, **kwargs):
            calls.append(kwargs)

    await client.update_message(
        FakeClient(), "C0DEREKT", "1234.5678", "Approved", [{"type": "divider"}]
    )

    assert calls == [
        {
            "channel": "C0DEREKT",
            "ts": "1234.5678",
            "text": "Approved",
            "blocks": [{"type": "divider"}],
        }
    ]
