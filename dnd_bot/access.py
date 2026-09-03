"""Who may run the commands that reach past the current session.

Starting and stopping a recording stays open to whoever is in the voice channel
- that is the design, and the people in the channel are the people being
recorded. Reaching *backwards* is different: `/session export` pulls a past
session's audio into whatever channel the caller is standing in, and
`/session transcript` does the same for its text. Those are the commands that
turn any guild member into a distributor of everyone else's recorded voice.

Discord applies `default_member_permissions` to a whole command group, so it
cannot gate `/session export` while leaving `/session start` open. Hence the
runtime check here.

The predicate is deliberately pure: no discord types, so it can be tested
directly, with a thin adapter over `ApplicationContext` below.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

log = logging.getLogger(__name__)

DENIED_MESSAGE = (
    "That command is restricted: it can expose a past session's audio or "
    "transcript to this channel. Ask a server admin, or have them grant your "
    "role access via `SESSION_ADMIN_ROLE_ID`."
)


def is_privileged(
    *,
    user_id: int | None,
    admin_user_id: int | None = None,
    has_manage_guild: bool = False,
    role_ids: Iterable[int] = (),
    privileged_role_id: int | None = None,
) -> bool:
    """Three independent ways in, checked cheapest first.

    `has_manage_guild` is the fallback so a fresh install is usable by the
    server owner without configuring anything at all.
    """
    if admin_user_id is not None and user_id is not None and int(user_id) == int(admin_user_id):
        return True
    if privileged_role_id is not None and int(privileged_role_id) in {int(r) for r in role_ids}:
        return True
    return bool(has_manage_guild)


def ctx_is_privileged(ctx, config) -> bool:  # noqa: ANN001 - discord types, Config
    """Adapt a slash-command context onto `is_privileged`."""
    author = getattr(ctx, "author", None)
    permissions = getattr(author, "guild_permissions", None)
    return is_privileged(
        user_id=getattr(author, "id", None),
        admin_user_id=config.admin_user_id,
        has_manage_guild=bool(getattr(permissions, "manage_guild", False)),
        role_ids=[role.id for role in getattr(author, "roles", [])],
        privileged_role_id=config.session_admin_role_id,
    )


async def require_privileged(ctx, config) -> bool:  # noqa: ANN001 - discord types, Config
    """Gate a command. Responds with the refusal and returns False when denied."""
    if ctx_is_privileged(ctx, config):
        return True
    log.info(
        "Refused %s for unprivileged user %s",
        getattr(getattr(ctx, "command", None), "qualified_name", "?"),
        getattr(getattr(ctx, "author", None), "id", "?"),
    )
    await ctx.respond(DENIED_MESSAGE, ephemeral=True)
    return False
