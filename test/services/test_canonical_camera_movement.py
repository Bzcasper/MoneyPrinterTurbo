"""Prevent visually frozen canonical-image zoompan regression."""

import subprocess

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from scripts.render_canonical_still_motion import render_canonical_motion
from scripts.render_typebeat_project import _render_image_motion


def _pattern(tmp_path):
    path = tmp_path / 'pattern.png'
    image = Image.new('RGB', (960, 540), (12, 34, 55))
    draw = ImageDraw.Draw(image)
    for i in range(0, 960, 60):
        draw.rectangle((i, (i * 7) % 410, i + 38, (i * 7) % 410 + 80), fill=((i * 3) % 256, (i * 5) % 256, (i * 11) % 256))
    image.save(path)
    return path


def _frame_difference(video, tmp_path):
    images = []
    for key, second in [('first', '0.1'), ('last', '2.9')]:
        frame = tmp_path / f'{video.stem}-{key}.png'
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', second, '-i', str(video), '-frames:v', '1', str(frame)], check=True, timeout=35)
        images.append(Image.open(frame).convert('RGB'))
    return sum(ImageStat.Stat(ImageChops.difference(*images)).mean) / 3


@pytest.mark.parametrize('engine', ['coverage', 'hero'])
def test_source_image_produces_actual_camera_motion(engine, tmp_path):
    still = _pattern(tmp_path)
    output = tmp_path / f'{engine}.mp4'
    if engine == 'coverage':
        _render_image_motion(str(still), output, 3.2, 5)
    else:
        render_canonical_motion(still, output, 3.2)
    assert _frame_difference(output, tmp_path) > 4.0
