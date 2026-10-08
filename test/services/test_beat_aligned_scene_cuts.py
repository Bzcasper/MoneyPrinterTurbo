import pytest

from scripts.assemble_typebeat_project import beat_aligned_boundaries


@pytest.mark.parametrize('duration,bpm,scenes', [(96,150,33),(131.48,140,30),(180,90,50)])
def test_monotonic_grid_with_exact_duration(duration,bpm,scenes):
    points=beat_aligned_boundaries(duration,bpm,scenes)
    assert len(points)==scenes+1
    assert points[0]==0
    assert points[-1]==duration
    assert all(a<b for a,b in zip(points,points[1:]))
    period=60/bpm
    assert all(abs(t/period-round(t/period))<0.00001 for t in points[1:-1])


@pytest.mark.parametrize('duration,bpm,scenes',[(20,60,33),(96,0,33),(96,250,33)])
def test_invalid_or_unrealistic_timeline_is_rejected(duration,bpm,scenes):
    with pytest.raises(ValueError):
        beat_aligned_boundaries(duration,bpm,scenes)
