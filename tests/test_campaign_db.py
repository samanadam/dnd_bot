"""Campaign storage: uniqueness, channel mapping, per-campaign character names."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_db import make_db, seed_session

from dnd_bot.db import CampaignConflict


async def test_create_and_list_campaign(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        created = await db.create_campaign(name="Curse of Strahd", channel_id=555, language="tr")
        assert created["name"] == "Curse of Strahd"
        assert created["channel_id"] == "555"
        assert created["archived"] == 0
        listed = await db.list_campaigns()
        assert [c["id"] for c in listed] == [created["id"]]
        assert listed[0]["session_count"] == 0
    finally:
        await db.close()


async def test_name_is_unique_ignoring_case(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        await db.create_campaign(name="Strahd")
        with pytest.raises(CampaignConflict):
            await db.create_campaign(name="strahd")
    finally:
        await db.close()


async def test_one_channel_maps_to_one_campaign(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        await db.create_campaign(name="A", channel_id=7)
        with pytest.raises(CampaignConflict):
            await db.create_campaign(name="B", channel_id=7)
    finally:
        await db.close()


async def test_campaign_for_channel_skips_archived(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        a = await db.create_campaign(name="A", channel_id=7)
        assert (await db.campaign_for_channel(7))["id"] == a["id"]
        await db.update_campaign(a["id"], archived=1)
        assert await db.campaign_for_channel(7) is None
        assert await db.campaign_for_channel(8) is None
    finally:
        await db.close()


async def test_update_rejects_unknown_field(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        a = await db.create_campaign(name="A")
        with pytest.raises(ValueError):
            await db.update_campaign(a["id"], id="hijack")
        renamed = await db.update_campaign(a["id"], name="A2", channel_id=None)
        assert renamed["name"] == "A2"
        assert renamed["channel_id"] is None
    finally:
        await db.close()


async def test_terms_keep_order_and_replace(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        a = await db.create_campaign(name="A")
        await db.replace_terms(a["id"], ["Eldrin", "Neverwinter", "Zhentarim"])
        assert await db.campaign_terms(a["id"]) == ["Eldrin", "Neverwinter", "Zhentarim"]
        await db.replace_terms(a["id"], ["Neverwinter"])
        assert await db.campaign_terms(a["id"]) == ["Neverwinter"]
    finally:
        await db.close()


async def test_corrections_replace(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        a = await db.create_campaign(name="A")
        await db.replace_corrections(a["id"], [("el drin", "Eldrin")])
        assert await db.campaign_corrections(a["id"]) == [("el drin", "Eldrin")]
        await db.replace_corrections(a["id"], [])
        assert await db.campaign_corrections(a["id"]) == []
    finally:
        await db.close()


async def test_character_map_layers_campaign_over_global(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        a = await db.create_campaign(name="A")
        b = await db.create_campaign(name="B")
        await db.set_character(10, "Global Thorin")
        await db.set_character(11, "Global Elenya")
        await db.set_campaign_character(a["id"], 10, "Thorin of A")
        assert await db.character_map() == {"10": "Global Thorin", "11": "Global Elenya"}
        assert await db.character_map(a["id"]) == {"10": "Thorin of A", "11": "Global Elenya"}
        assert await db.character_map(b["id"]) == {"10": "Global Thorin", "11": "Global Elenya"}
        assert await db.campaign_characters(a["id"]) == {"10": "Thorin of A"}
        await db.clear_campaign_character(a["id"], 10)
        assert await db.campaign_characters(a["id"]) == {}
    finally:
        await db.close()


async def test_session_count_and_new_session_is_unassigned(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        a = await db.create_campaign(name="A")
        await seed_session(db, "s1")
        session = await db.get_session("s1")
        assert session["campaign_id"] is None
        assert session["campaign_name"] is None
        await db.update_session("s1", campaign_id=a["id"], completed=1)
        assert (await db.get_session("s1"))["campaign_name"] == "A"
        assert (await db.list_campaigns())[0]["session_count"] == 1
    finally:
        await db.close()


async def test_create_session_stores_campaign_and_base_labels(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        a = await db.create_campaign(name="A")
        await db.create_session(
            session_id="s2",
            name="T",
            guild_id=1,
            channel_id=2,
            channel_name="Table",
            text_channel_id=None,
            started_by_user_id=10,
            start_time="2026-05-01T18:00:00+00:00",
            participants={"10": "Thorin"},
            language="tr",
            campaign_id=a["id"],
            base_labels={"10": "Aylin"},
        )
        row = await db.get_session("s2")
        assert row["campaign_id"] == a["id"]
        assert row["base_labels_json"] == '{"10": "Aylin"}'
        await db.merge_participants("s2", {"11": "Elenya"}, {"11": "Deniz"})
        row = await db.get_session("s2")
        assert '"11": "Elenya"' in row["participants_json"]
        assert '"11": "Deniz"' in row["base_labels_json"]
    finally:
        await db.close()


async def test_assign_session_computes_relabel_and_can_unassign(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        a = await db.create_campaign(name="A")
        await db.set_campaign_character(a["id"], 10, "Thorin of A")
        await seed_session(db, "s1")
        await db.update_session(
            "s1",
            participants_json='{"10": "Old", "11": "Elenya"}',
            base_labels_json='{"10": "Aylin"}',
        )
        row = await db.assign_session_campaign("s1", a["id"])
        assert row["campaign_id"] == a["id"]
        assert row["relabel_json"] == '{"10": "Thorin of A", "11": "Elenya"}'
        row = await db.assign_session_campaign("s1", None)
        assert row["campaign_id"] is None
        assert row["relabel_json"] == '{"10": "Aylin", "11": "Elenya"}'
        assert await db.assign_session_campaign("missing", a["id"]) is None
        with pytest.raises(LookupError):
            await db.assign_session_campaign("s1", "nope")
    finally:
        await db.close()
