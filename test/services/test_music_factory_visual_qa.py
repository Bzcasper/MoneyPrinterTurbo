"""Visual transition evidence must not be mistaken for release approval."""
import pytest

from scripts.music_factory_visual_qa import pair_metrics, audit_scenes, FRAME_BYTES


def test_identical_frames_do_not_flag_discontinuity():
    frame = bytes([24, 40, 130]) * (FRAME_BYTES // 3)
    result = pair_metrics(frame, frame)
    assert result["rgb_mae"] == 0
    assert result["luma_ssim"] == pytest.approx(1.0, abs=.0001)
    assert not result["high_visual_discontinuity"]


def test_hard_discontinuity_flags_review():
    a = bytes([0]) * FRAME_BYTES
    b = bytes([255]) * FRAME_BYTES
    result = pair_metrics(a, b)
    assert result["rgb_mae"] == 1
    assert result["high_visual_discontinuity"]


def test_missing_thirty_scenes_fail_closed():
    with pytest.raises(ValueError, match="thirty"):
        audit_scenes([])
    with pytest.raises(ValueError):
        pair_metrics(b"", b"")


def test_small_color_change_is_not_marked_extreme():
    a = bytes([120]) * FRAME_BYTES
    b = bytes([132]) * FRAME_BYTES
    result = pair_metrics(a, b)
    assert not result["high_visual_discontinuity"]
