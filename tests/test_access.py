"""Gating the commands that reach past the current session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dnd_bot.access import DENIED_MESSAGE, ctx_is_privileged, is_privileged, require_privileged


def test_the_configured_admin_always_gets_through():
    assert is_privileged(user_id=999, admin_user_id=999)


def test_manage_guild_is_the_fallback_so_a_fresh_install_is_usable():
    assert is_privileged(user_id=1, has_manage_guild=True)


def test_the_privileged_role_grants_access():
    assert is_privileged(user_id=1, role_ids=[50, 60], privileged_role_id=60)


def test_an_ordinary_member_is_refused():
    assert not is_privileged(user_id=1, admin_user_id=999, role_ids=[50], privileged_role_id=60)


def test_no_configuration_at_all_refuses_ordinary_members():
    assert not is_privileged(user_id=1)


def test_role_ids_compare_numerically_not_by_identity():
    """Discord snowflakes arrive as ints or strings depending on the source."""
    assert is_privileged(user_id=1, role_ids=["60"], privileged_role_id=60)


# -- the context adapter ---------------------------------------------------


def make_ctx(user_id=1, manage_guild=False, role_ids=()):
    responses = []
    return (
        SimpleNamespace(
            author=SimpleNamespace(
                id=user_id,
                guild_permissions=SimpleNamespace(manage_guild=manage_guild),
                roles=[SimpleNamespace(id=r) for r in role_ids],
            ),
            command=SimpleNamespace(qualified_name="session export"),
            respond=lambda *args, **kwargs: responses.append((args, kwargs)),
        ),
        responses,
    )


def test_ctx_adapter_reads_permissions_and_roles(config):
    config = config.__class__(**{**config.__dict__, "session_admin_role_id": 77})
    ctx, _ = make_ctx(role_ids=(77,))
    assert ctx_is_privileged(ctx, config)


def test_ctx_adapter_refuses_a_member_with_neither(config):
    ctx, _ = make_ctx(user_id=1, role_ids=(5,))
    assert not ctx_is_privileged(ctx, config)


async def test_require_privileged_explains_the_refusal(config):
    ctx, responses = make_ctx(user_id=1)

    async def respond(*args, **kwargs):
        responses.append((args, kwargs))

    ctx.respond = respond

    assert await require_privileged(ctx, config) is False
    ((args, kwargs),) = responses
    assert args[0] == DENIED_MESSAGE
    assert kwargs["ephemeral"] is True, "a refusal should not be broadcast to the channel"


async def test_require_privileged_stays_quiet_when_allowed(config):
    ctx, responses = make_ctx(user_id=config.admin_user_id)
    assert await require_privileged(ctx, config) is True
    assert responses == []


@pytest.mark.parametrize("command", ["export", "transcript", "recover"])
def test_the_gated_commands_are_the_ones_that_reach_backwards(command):
    """Guard against someone quietly ungating these."""
    import inspect

    from dnd_bot.cogs.session import SessionCog

    source = inspect.getsource(getattr(SessionCog, command).callback)
    assert "require_privileged" in source
