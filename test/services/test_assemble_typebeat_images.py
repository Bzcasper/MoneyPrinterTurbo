from unittest.mock import patch

import pytest

from scripts.assemble_typebeat_project import motion_ordinals, supplied_image_assets


def _assets(scene_count=33):
    motion = set(motion_ordinals(scene_count))
    return [
        {
            "ordinal": n,
            "host_path": f"/srv/data/n8n-media/store/firefly/images/scene-{n}.png",
            "provider": "firefly",
            "model": "firefly-image-5",
            "prompt": "a coherent cyan and molten-gold abstract shot",
        }
        for n in range(1, scene_count + 1)
        if n not in motion
    ]


def test_optional_assets_preserve_legacy_frame_extract():
    assert supplied_image_assets(None, 33, motion_ordinals(33)) == {}


def test_full_generated_still_set_with_provider_provenance():
    with patch("scripts.assemble_typebeat_project.remote_file_exists", return_value=True):
        result = supplied_image_assets(_assets(), 33, motion_ordinals(33))
    assert len(result) == 23
    assert all(value["billing_mode"] == "firefly_fair_use" for value in result.values())
    assert all(value["provider"] == "firefly" for value in result.values())


@pytest.mark.parametrize("modifier", [
    lambda a: a.pop(),
    lambda a: a.append(dict(a[0])),
    lambda a: a[0].update({"ordinal": 3}),
    lambda a: a[0].update({"host_path": "/tmp/untrusted.png"}),
    lambda a: a[0].update({"host_path": "/srv/data/n8n-media/store/../../bad.png"}),
    lambda a: a[0].update({"provider": "unspecified"}),
])
def test_missing_duplicate_or_unsafe_asset_is_rejected(modifier):
    assets = _assets()
    modifier(assets)
    with patch("scripts.assemble_typebeat_project.remote_file_exists", return_value=True):
        with pytest.raises(SystemExit):
            supplied_image_assets(assets, 33, motion_ordinals(33))


def test_nonexistent_asset_is_rejected():
    with patch("scripts.assemble_typebeat_project.remote_file_exists", return_value=False):
        with pytest.raises(SystemExit):
            supplied_image_assets(_assets(), 33, motion_ordinals(33))
