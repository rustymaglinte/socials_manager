"""Post to a Facebook Page from the command line, with no agent involved.

SPECS 11 step 2: one adapter proved end-to-end from a script, before anything
imports it. If the agent layer turned out to be a mistake, this would still work.

Deliberately outside `app/`: this is the join the adapter is forbidden to make.
It reads a brand, finds that brand's Page id and token, and hands both to a
module that never learns whose Page it is. Keeping the join here is what the
"adapters are brand-blind" contract is protecting (SPECS D3).

Draft by default. Posting to a live Page is the irreversible direction, so it
takes an explicit --live; there is no way to publish by forgetting a flag.

    python -m scripts.fb_publish post pinoysing "Kumusta!"
    python -m scripts.fb_publish post pinoysing "Kumusta!" --live
    python -m scripts.fb_publish list pinoysing
    python -m scripts.fb_publish delete pinoysing 67890_111
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.domain.brand import load_brand
from app.domain.brand.context import BrandContext, BrandNotFound
from app.platforms.facebook import (
    GRAPH_URL,
    PublishFailed,
    publish,
    publish_photo,
)
from app.render.post_card import TEMPLATES, PostCard, RenderFailed, Theme, render

load_dotenv()

PLATFORM = "facebook"

# How this script is invoked, for the follow-up commands it prints. Spelled once
# so a rename does not leave the hints pointing at a command that no longer runs.
INVOCATION = "python -m scripts.fb_publish"


def token_env_var(slug: str) -> str:
    """`pinoysing` -> `FB_PAGE_TOKEN_PINOYSING`.

    Derived from the slug rather than mapped, so a second brand's Page needs a
    line in .env and no code at all -- the same shape as SLACK_*_CHANNEL_ID.
    """
    return f"FB_PAGE_TOKEN_{slug.upper()}"


def credentials(brand: BrandContext) -> tuple[str, str]:
    """(page_id, token) for this brand's Facebook Page.

    Every failure here is a configuration mistake an operator can fix, so each
    one says which file to open rather than surfacing as a Graph error later.
    """
    account = next(
        (a for a in brand.enabled_accounts if a.platform == PLATFORM), None
    )
    if account is None:
        raise SystemExit(
            f"{brand.slug} has no enabled facebook account in "
            f"brands/{brand.slug}/brand.yaml"
        )
    # `configured` is the placeholder check the adapter deliberately does not do:
    # knowing what a TODO looks like is the domain's business, not an adapter's.
    # Bound to a local as well, because `configured` narrows the Optional for a
    # reader but not for a type checker.
    page_id = account.external_id
    if not account.configured or not page_id:
        raise SystemExit(
            f"page_id for {brand.slug} is still a placeholder in "
            f"brands/{brand.slug}/brand.yaml"
        )

    token = os.getenv(token_env_var(brand.slug))
    if not token:
        raise SystemExit(f"Set {token_env_var(brand.slug)} in .env")

    return page_id, token


def brand_or_exit(slug: str) -> BrandContext:
    try:
        return load_brand(slug)
    except BrandNotFound as error:
        raise SystemExit(str(error)) from error


async def do_post(args: argparse.Namespace) -> None:
    brand = brand_or_exit(args.brand)
    page_id, token = credentials(brand)

    if args.live:
        # The one action here nobody can take back. Cheap to confirm, and the
        # Page name is printed because the mistake worth catching is the brand
        # being wrong, not the text.
        print(f"About to publish to {brand.display_name}'s live Page ({page_id}).")
        if input("Type the brand slug to confirm: ").strip() != brand.slug:
            raise SystemExit("Cancelled.")

    try:
        post = await publish(
            page_id=page_id,
            access_token=token,
            message=args.message,
            link=args.link,
            published=args.live,
        )
    except PublishFailed as error:
        # trace_id is what Meta support asks for first, so print it when there is
        # one rather than making someone re-run with logging turned up.
        detail = f" (fbtrace_id {error.trace_id})" if error.trace_id else ""
        raise SystemExit(f"Failed: {error}{detail}") from error

    if post.published:
        print(f"Published: {post.url}")
    else:
        # No permalink: an unpublished post has an id but no public page. It is
        # also absent from every listing edge and from Business Suite's Drafts
        # tab, so its id is the only handle on it -- print the commands that take
        # one rather than leaving someone to hunt for it in a UI.
        print(f"Unpublished post created: {post.id}")
        print(f"  see it:  {INVOCATION} show {brand.slug} {post.id}")
        print(f"  remove:  {INVOCATION} delete {brand.slug} {post.id}")


POST_FIELDS = "id,message,created_time,is_published"

# A photo is a different node type, not a post: its caption is `name`, it has no
# `is_published` at all, and asking for post fields on one is a hard 400 rather
# than a null. An unpublished image post is only ever addressable as a photo,
# because the feed story that would carry the post fields does not exist yet.
PHOTO_FIELDS = "id,name,created_time,link"


def check_belongs_to(post_id: str, page_id: str, slug: str) -> None:
    """Refuse a post id that is visibly from another Page.

    A feed post id is "{page_id}_{post_id}", so where there is an underscore the
    prefix settles it -- worth checking, because the ids are long and
    near-identical and the paste that lands here is the one from the other
    brand's terminal.

    A bare id has no prefix to read: an unpublished photo has no feed story yet,
    so /photos returns only the photo's own id. Those pass through, and the
    check that catches a wrong-Page id then is Graph's own -- a Page token
    cannot read or delete another Page's objects, so the worst case is an
    error rather than something happening to the wrong brand.
    """
    if "_" in post_id and not post_id.startswith(f"{page_id}_"):
        raise SystemExit(
            f"Post {post_id} does not belong to {slug}'s Page ({page_id})"
        )


def _format_post(post: dict, state: str | None = None) -> str:
    """One line per post. `state` is passed in for node types that cannot say --
    a photo has no is_published, and guessing one would be inventing a fact."""
    if state is None:
        state = "live " if post.get("is_published", True) else "DRAFT"
    message = (post.get("message") or "").replace("\n", " ")
    return f"{state}  {post['id']}  {post.get('created_time', '')}  {message[:60]}"


def render_theme(brand: BrandContext) -> Theme:
    """The brand's declared palette, in the shape the renderer takes.

    This mapping is the whole reason the domain keeps its own PostTheme: the
    renderer sits above the domain, so a brand cannot import it. Translating
    here is the same join this script already makes for credentials.
    """
    if brand.theme is None:
        raise SystemExit(
            f"{brand.slug} declares no `theme:` block in "
            f"brands/{brand.slug}/brand.yaml, so there is nothing to render."
        )
    return Theme(
        ground=brand.theme.ground,
        accent=brand.theme.accent,
        muted=brand.theme.muted,
        wordmark=brand.theme.wordmark,
        tagline=brand.theme.tagline,
    )


async def do_photo(args: argparse.Namespace) -> None:
    """Render a graphic and post it as an image with a caption.

    Draft by default, like `post`: the irreversible direction takes --live.
    """
    brand = brand_or_exit(args.brand)
    page_id, token = credentials(brand)
    theme = render_theme(brand)

    # The angle picks the look, so the same brief always produces the same
    # template -- consistency that lives in brand.yaml rather than in a habit.
    template = args.template or (
        brand.theme.template_for(args.angle) if brand.theme else "marquee"
    )

    # A shell hands "\n" through as two characters, and a deliberate line break
    # is part of the composition -- so the escape is honoured here rather than
    # forcing whoever runs this to fight their shell's quoting rules.
    card = PostCard(
        hook=args.hook.replace("\\n", "\n"),
        sub=args.sub or "",
        eyebrow=args.eyebrow or "",
    )
    try:
        image = await render(template=template, card=card, theme=theme)
    except RenderFailed as error:
        raise SystemExit(f"Could not render: {error}") from error

    if args.out:
        Path(args.out).write_bytes(image)
        print(f"Wrote {args.out} ({len(image):,} bytes, {template})")
        if not args.post:
            return

    if args.live:
        print(f"About to publish an image to {brand.display_name}'s live Page ({page_id}).")
        if input("Type the brand slug to confirm: ").strip() != brand.slug:
            raise SystemExit("Cancelled.")

    try:
        post = await publish_photo(
            page_id=page_id,
            access_token=token,
            image=image,
            message=args.caption or "",
            published=args.live,
        )
    except PublishFailed as error:
        detail = f" (fbtrace_id {error.trace_id})" if error.trace_id else ""
        raise SystemExit(f"Failed: {error}{detail}") from error

    if post.published:
        print(f"Published: {post.url}")
    else:
        print(f"Unpublished image post created: {post.id}  [{template}]")
        print(f"  see it:  {INVOCATION} show {brand.slug} {post.id}")
        print(f"  remove:  {INVOCATION} delete {brand.slug} {post.id}")


async def do_list(args: argparse.Namespace) -> None:
    """Read the Page's own recent posts.

    /posts, not /feed. /feed is the Page's whole timeline including posts other
    people made on it, and reading those needs pages_read_user_content on top --
    so /feed answers 403 for a token that can read the Page perfectly well.

    Unpublished posts do not appear on any listing edge available to a plain Page
    token: /posts and /published_posts both exclude them, and /promotable_posts
    only exists on a Page attached to an ad account. A draft is therefore
    reachable only by id, which is what `show` is for.

    Raw httpx rather than an adapter function: reading is not part of publishing,
    and app/platforms should not grow an endpoint the product does not use yet.
    """
    brand = brand_or_exit(args.brand)
    page_id, token = credentials(brand)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{GRAPH_URL}/{page_id}/posts",
            params={"fields": POST_FIELDS, "limit": 25},
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.is_error:
        raise SystemExit(f"Could not read the posts: {response.text[:300]}")

    posts = response.json().get("data", [])
    if not posts:
        print("No published posts on this Page.")
        return

    for post in posts:
        print(_format_post(post))
    print()
    print("Published posts only -- Graph has no listing edge that includes")
    print(f"unpublished ones. For a draft: {INVOCATION} show {brand.slug} <post_id>")


async def do_show(args: argparse.Namespace) -> None:
    """Fetch one post by id, published or not.

    The only way to see an unpublished post: it is absent from every listing edge
    and from Business Suite's Drafts tab, but a direct read of its id works.
    """
    brand = brand_or_exit(args.brand)
    page_id, token = credentials(brand)
    check_belongs_to(args.post_id, page_id, brand.slug)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{GRAPH_URL}/{args.post_id}", params={"fields": POST_FIELDS}, headers=headers
        )
        # Asking a photo for post fields is a 400, not an empty result, so the
        # node type has to be discovered by trying. Cheaper than a probe call on
        # the common path, where the id is a post and the first request is right.
        if response.status_code == 400 and "nonexisting field" in response.text:
            response = await client.get(
                f"{GRAPH_URL}/{args.post_id}",
                params={"fields": PHOTO_FIELDS},
                headers=headers,
            )
            if not response.is_error:
                photo = response.json()
                # `link` is the only way to actually look at an unpublished
                # image -- it has no permalink and no listing shows it.
                print(_format_post({**photo, "message": photo.get("name")}, state="PHOTO"))
                if photo.get("link"):
                    print(photo["link"])
                return

    if response.is_error:
        raise SystemExit(f"Could not read {args.post_id}: {response.text[:300]}")

    post = response.json()
    print(_format_post(post))
    if post.get("is_published", True):
        print(f"https://www.facebook.com/{post['id']}")


def _explain_check_failure(response: httpx.Response, slug: str) -> str:
    """Say what a rejected /me actually means.

    The useful case is code 100. `/me` on a user token returns the person with no
    permission at all, so being told the *object* cannot be loaded means /me
    resolved to a Page node -- which is to say the token is a real Page token and
    the scopes behind it are what is missing. That is the opposite conclusion
    from "the token is wrong", and it is worth stating rather than leaving to
    whoever reads the raw error.
    """
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}

    code = error.get("code")
    detail = error.get("message") or response.text[:300]
    variable = token_env_var(slug)

    if code == 100 and "pages_read_engagement" in detail:
        return (
            f"{variable} IS a Page token -- /me resolved to a Page, which a user\n"
            f"token never does. What it lacks is the scopes.\n\n"
            f"Page tokens inherit permissions from the user token they came from,\n"
            f"so the fix is upstream: regenerate the USER token with both\n"
            f"pages_read_engagement and pages_manage_posts granted, extend it,\n"
            f"re-run /me/accounts, and save the new Page token.\n\n"
            f"In Graph API Explorer, ticking the permission boxes does not grant\n"
            f"anything -- you have to click Generate Access Token again afterwards\n"
            f"and accept the dialog. Reusing the token from before the tick is the\n"
            f"usual way to land exactly here.\n\n"
            f"Graph said: {detail}"
        )

    if code == 190:
        return (
            f"{variable} is expired or malformed, so nothing can be concluded\n"
            f"about its scopes. Re-derive it from a long-lived user token.\n\n"
            f"Graph said: {detail}"
        )

    return f"{variable} was rejected (code {code}): {detail}"


async def do_check(args: argparse.Namespace) -> None:
    """Ask Graph who the configured token belongs to.

    Error 200 on a publish has three plausible causes needing three different
    fixes -- a user token saved where a Page token belongs, a Page token derived
    from a user token that was never granted the scopes, or a Page role without
    content permissions. The first is by far the most common and this separates
    it from the other two in one call.

    Never echoes the token: it goes out in a header, and .env stays the only
    place it is written down.
    """
    brand = brand_or_exit(args.brand)
    page_id, token = credentials(brand)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{GRAPH_URL}/me",
            params={"fields": "id,name"},
            headers={"Authorization": f"Bearer {token}"},
        )

    if response.is_error:
        raise SystemExit(_explain_check_failure(response, brand.slug))

    identity = response.json()
    print(f"{token_env_var(brand.slug)} identifies as:")
    print(f"  {identity.get('name')}  ({identity.get('id')})")
    print(f"brands/{brand.slug}/brand.yaml page_id:")
    print(f"  {page_id}")
    print()

    if identity.get("id") == page_id:
        print("This is a Page token for the right Page, so the 403 is about what")
        print("it is allowed to do, not what it is. Either the user token it came")
        print("from was never granted pages_manage_posts and pages_read_engagement,")
        print("or your Page role lacks full content permissions.")
        print()
        print("Re-generate the user token in Graph API Explorer WITH both scopes")
        print("ticked, re-run /me/accounts, and save the new Page token.")
    else:
        print("MISMATCH -- this is not a Page token for this Page.")
        print("A token naming a person is a user token; posting to a Page needs")
        print("the Page's own token from the /me/accounts response.")


async def do_delete(args: argparse.Namespace) -> None:
    brand = brand_or_exit(args.brand)
    page_id, token = credentials(brand)

    check_belongs_to(args.post_id, page_id, brand.slug)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(
            f"{GRAPH_URL}/{args.post_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.is_error:
        raise SystemExit(f"Could not delete: {response.text[:300]}")

    print(f"Deleted {args.post_id}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    post = sub.add_parser("post", help="create a post (unpublished unless --live)")
    post.add_argument("brand")
    post.add_argument("message")
    post.add_argument("--link", help="URL to attach")
    post.add_argument(
        "--live",
        action="store_true",
        help="actually publish it, where followers can see it",
    )
    post.set_defaults(run=do_post)

    photo = sub.add_parser("photo", help="render a graphic and post it as an image")
    photo.add_argument("brand")
    photo.add_argument("hook", help="the six words on the graphic; \\n breaks a line")
    photo.add_argument("--sub", help="one supporting line under the hook")
    photo.add_argument("--caption", help="the post text under the image")
    photo.add_argument("--eyebrow", help="small label above the hook (default: wordmark)")
    photo.add_argument("--angle", help="brief angle; picks the template from brand.yaml")
    photo.add_argument("--template", choices=TEMPLATES, help="override the angle's template")
    photo.add_argument("--out", help="also write the PNG here, to eyeball before posting")
    photo.add_argument(
        "--post",
        action="store_true",
        help="with --out, upload as well as writing the file",
    )
    photo.add_argument(
        "--live", action="store_true", help="publish it, where followers can see it"
    )
    photo.set_defaults(run=do_photo)

    listing = sub.add_parser("list", help="recent posts, published and not")
    listing.add_argument("brand")
    listing.set_defaults(run=do_list)

    show = sub.add_parser("show", help="one post by id, published or not")
    show.add_argument("brand")
    show.add_argument("post_id")
    show.set_defaults(run=do_show)

    check = sub.add_parser("check", help="diagnose the configured token")
    check.add_argument("brand")
    check.set_defaults(run=do_check)

    delete = sub.add_parser("delete", help="delete a post by id")
    delete.add_argument("brand")
    delete.add_argument("post_id")
    delete.set_defaults(run=do_delete)

    return parser.parse_args(argv)


def main() -> None:
    # Windows consoles default to cp1252, which cannot encode a single one of
    # this brand's posts -- Filipino text, curly quotes and emoji all raise
    # UnicodeEncodeError on print. Reading a post back should not be able to
    # crash on the content it just read.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    args = parse_args(sys.argv[1:])
    asyncio.run(args.run(args))


if __name__ == "__main__":
    main()
