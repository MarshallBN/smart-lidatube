from pathlib import Path

from smart_lidatube.path_mapping import map_lidarr_music_path


def test_maps_lidarr_music_path_to_audit_mount():
    assert map_lidarr_music_path(
        "/Music/AJR/50 States Away/AJR - 01 - 50 States Away.mp3",
        "/Music",
        "/music",
    ) == Path("/music/AJR/50 States Away/AJR - 01 - 50 States Away.mp3")


def test_rejects_unmapped_paths_and_traversal():
    assert map_lidarr_music_path("/other/song.mp3", "/Music", "/music") is None
    assert map_lidarr_music_path("/Music/../secret.mp3", "/Music", "/music") is None
    assert map_lidarr_music_path("/Music/song.mp3", "Music", "/music") is None
    assert map_lidarr_music_path("/Music/song.mp3", "/Music", "music") is None


def test_rejects_missing_mapping_configuration():
    assert map_lidarr_music_path("/Music/song.mp3", "", "/music") is None
    assert map_lidarr_music_path("/Music/song.mp3", "/Music", "") is None
