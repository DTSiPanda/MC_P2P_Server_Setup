"""
test_player_sync.py – Unit and integration tests for playerdata & inventory sync.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import nbtlib
from nbtlib.tag import Compound, Int, IntArray, String, List

from client.player_sync import (
    extract_player_uuid_from_compound,
    finalize_world_after_host,
    ints_to_uuid,
    offline_uuid,
    prepare_world_for_host,
    uuid_to_ints,
)


def test_offline_uuid_matches_minecraft_java():
    # bellisarius known offline UUID from real Minecraft usercache.json
    assert offline_uuid("bellisarius") == "5897a92d-648c-368b-94ec-f619ce17eb2c"
    # ajmal_kasab known offline UUID from real Minecraft usercache.json
    assert offline_uuid("ajmal_kasab") == "2342bac7-d38d-33e3-a4b9-8b5ae5872461"


def test_uuid_ints_conversion_roundtrip():
    test_ids = [
        "34208dac-aa74-42d4-aa9e-87bd7718d849",
        "5897a92d-648c-368b-94ec-f619ce17eb2c",
        str(uuid.uuid4()),
    ]
    for uid in test_ids:
        ints = uuid_to_ints(uid)
        assert len(ints) == 4
        reconstructed = ints_to_uuid(ints)
        assert reconstructed.lower() == uid.lower()


def test_extract_player_uuid_from_int_array():
    uid = "34208dac-aa74-42d4-aa9e-87bd7718d849"
    ints = uuid_to_ints(uid)
    compound = Compound({
        "UUID": IntArray([Int(x) for x in ints]),
        "Health": Int(20),
    })
    assert extract_player_uuid_from_compound(compound) == uid


def _create_mock_world(root: Path, host_uuid: str, player_name: str = "PlayerA") -> Path:
    world_dir = root / "OurWorld"
    world_dir.mkdir(parents=True, exist_ok=True)
    pd_dir = world_dir / "playerdata"
    pd_dir.mkdir(exist_ok=True)

    ints = uuid_to_ints(host_uuid)
    player_compound = Compound({
        "UUID": IntArray([Int(x) for x in ints]),
        "Health": Int(20),
        "Score": Int(100),
        "CustomName": String(player_name),
    })

    level_nbt = nbtlib.File({
        "Data": Compound({
            "LevelName": String("OurWorld"),
            "Player": player_compound,
        })
    })
    level_nbt.save(str(world_dir / "level.dat"), gzipped=True)
    return world_dir


def test_prepare_world_same_host_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        host_uuid = "34208dac-aa74-42d4-aa9e-87bd7718d849"
        world = _create_mock_world(Path(tmp), host_uuid)

        # Running prepare_world_for_host with the same host
        res = prepare_world_for_host(world, host_uuid)
        assert res is True

        nbt = nbtlib.load(str(world / "level.dat"))
        assert extract_player_uuid_from_compound(nbt["Data"]["Player"]) == host_uuid


def test_prepare_world_different_host_swaps_inventories():
    with tempfile.TemporaryDirectory() as tmp:
        host_a_uuid = "34208dac-aa74-42d4-aa9e-87bd7718d849"
        host_b_uuid = "5897a92d-648c-368b-94ec-f619ce17eb2c"
        world = _create_mock_world(Path(tmp), host_a_uuid, player_name="PlayerA")

        # Create saved playerdata for Host B (e.g. from when B was guest)
        ints_b = uuid_to_ints(host_b_uuid)
        pd_b = nbtlib.File({
            "UUID": IntArray([Int(x) for x in ints_b]),
            "Health": Int(15),
            "Score": Int(500),
            "CustomName": String("PlayerB"),
        })
        pd_b.save(str(world / "playerdata" / f"{host_b_uuid}.dat"), gzipped=True)

        # Host B now prepares to host
        res = prepare_world_for_host(world, host_b_uuid)
        assert res is True

        # 1. Host A's data should have been backed up into playerdata/<host_a_uuid>.dat
        pd_a_file = world / "playerdata" / f"{host_a_uuid}.dat"
        assert pd_a_file.exists()
        pd_a_loaded = nbtlib.load(str(pd_a_file))
        assert extract_player_uuid_from_compound(pd_a_loaded) == host_a_uuid
        assert pd_a_loaded["CustomName"] == "PlayerA"

        # 2. Host B's data should have been injected into level.dat
        level_loaded = nbtlib.load(str(world / "level.dat"))
        active_player = level_loaded["Data"]["Player"]
        assert extract_player_uuid_from_compound(active_player) == host_b_uuid
        assert active_player["CustomName"] == "PlayerB"
        assert active_player["Score"] == 500


def test_prepare_world_brand_new_host_resets_player_in_level():
    with tempfile.TemporaryDirectory() as tmp:
        host_a_uuid = "34208dac-aa74-42d4-aa9e-87bd7718d849"
        new_host_uuid = "c80a93e7-3489-3813-9219-a0c20328f901"
        world = _create_mock_world(Path(tmp), host_a_uuid, player_name="PlayerA")

        # New host has no prior playerdata in world
        res = prepare_world_for_host(world, new_host_uuid)
        assert res is True

        # Host A's data was preserved in playerdata
        pd_a_file = world / "playerdata" / f"{host_a_uuid}.dat"
        assert pd_a_file.exists()

        # level.dat Player tag was cleanly removed for fresh spawn
        level_loaded = nbtlib.load(str(world / "level.dat"))
        assert "Player" not in level_loaded["Data"]


def test_finalize_world_after_host_mirrors_to_playerdata():
    with tempfile.TemporaryDirectory() as tmp:
        host_uuid = "5897a92d-648c-368b-94ec-f619ce17eb2c"
        world = _create_mock_world(Path(tmp), host_uuid, player_name="HostPlaying")

        # Simulate host gaining score in game
        nbt = nbtlib.load(str(world / "level.dat"))
        nbt["Data"]["Player"]["Score"] = Int(9999)
        nbt.save(str(world / "level.dat"), gzipped=True)

        res = finalize_world_after_host(world, host_uuid)
        assert res is True

        pd_file = world / "playerdata" / f"{host_uuid}.dat"
        assert pd_file.exists()
        pd_loaded = nbtlib.load(str(pd_file))
        assert extract_player_uuid_from_compound(pd_loaded) == host_uuid
        assert pd_loaded["Score"] == 9999


def test_26_1_2_singleplayer_uuid_and_dual_uuid_sync():
    from client.player_sync import sync_player_pair

    with tempfile.TemporaryDirectory() as tmp:
        world_dir = Path(tmp) / "OurWorld"
        world_dir.mkdir(parents=True)
        players_data = world_dir / "players" / "data"
        players_adv = world_dir / "players" / "advancements"
        players_stats = world_dir / "players" / "stats"
        players_data.mkdir(parents=True)
        players_adv.mkdir(parents=True)
        players_stats.mkdir(parents=True)

        host_id = "34208dac-aa74-42d4-aa9e-87bd7718d849"
        guest_id = "d1ae6bd2-27f0-387f-998f-1bb2b35f9dfa"
        new_host_id = "ad385985-99b9-4e6d-9046-a4f8e09319a5"

        # Mock 26.1.2 level.dat with singleplayer_uuid
        ints = uuid_to_ints(host_id)
        level_nbt = nbtlib.File({
            "Data": Compound({
                "LevelName": String("OurWorld"),
                "singleplayer_uuid": IntArray([Int(x) for x in ints]),
            })
        })
        level_nbt.save(str(world_dir / "level.dat"), gzipped=True)

        # Create host player data
        h_pd = nbtlib.File({
            "UUID": IntArray([Int(x) for x in ints]),
            "Score": Int(777),
        })
        h_pd.save(str(players_data / f"{host_id}.dat"), gzipped=True)
        (players_adv / f"{host_id}.json").write_text('{"adv": true}', encoding="utf-8")
        (players_stats / f"{host_id}.json").write_text('{"stats": 1}', encoding="utf-8")

        # Test sync_player_pair copies host -> guest
        sync_player_pair(world_dir, host_id, guest_id)
        assert (players_data / f"{guest_id}.dat").exists()
        assert (players_adv / f"{guest_id}.json").exists()
        assert (players_stats / f"{guest_id}.json").exists()

        guest_loaded = nbtlib.load(str(players_data / f"{guest_id}.dat"))
        assert extract_player_uuid_from_compound(guest_loaded) == guest_id

        # Test prepare_world_for_host updates singleplayer_uuid for new host
        prepare_world_for_host(world_dir, new_host_id)
        updated_level = nbtlib.load(str(world_dir / "level.dat"))
        assert extract_player_uuid_from_compound({"UUID": updated_level["Data"]["singleplayer_uuid"]}) == new_host_id


def test_shared_identity_registry():
    from client.player_sync import load_identity_registry, register_player_identity

    with tempfile.TemporaryDirectory() as tmp:
        world_dir = Path(tmp) / "OurWorld"
        world_dir.mkdir()

        # Register player 1
        register_player_identity(
            world_dir,
            "PlayerOne",
            "34208dac-aa74-42d4-aa9e-87bd7718d849",
            "d1ae6bd2-27f0-387f-998f-1bb2b35f9dfa",
        )

        reg = load_identity_registry(world_dir)
        assert "PlayerOne" in reg
        assert reg["PlayerOne"]["host_uuid"] == "34208dac-aa74-42d4-aa9e-87bd7718d849"
        assert reg["PlayerOne"]["guest_uuid"] == "d1ae6bd2-27f0-387f-998f-1bb2b35f9dfa"

        # Register player 2
        register_player_identity(
            world_dir,
            "PlayerTwo",
            "ad385985-99b9-4e6d-9046-a4f8e09319a5",
            "5897a92d-648c-368b-94ec-f619ce17eb2c",
        )

        reg2 = load_identity_registry(world_dir)
        assert len(reg2) == 2
        assert "PlayerTwo" in reg2


