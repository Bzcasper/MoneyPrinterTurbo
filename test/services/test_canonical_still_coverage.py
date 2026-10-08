from unittest.mock import patch

import pytest

from scripts.canonical_still_motion_bridge import derive_scene_still


@pytest.mark.parametrize('source', [
    '/tmp/untrusted.png',
    '/srv/data/n8n-media/store/../../wrong.png',
    '/srv/data/n8n-media/store/video.mp4',
])
def test_rejects_unsafe_coverage_source(source):
    with pytest.raises(ValueError, match='untrusted'):
        derive_scene_still(source, 'safe-run', 2)


@pytest.mark.parametrize('project,ordinal,variant', [
    ('../escape', 2, 0), ('valid', 0, 0), ('valid', 51, 0),
    ('valid', 1, 10), ('valid', True, 0),
])
def test_rejects_invalid_identifier_or_crop(project, ordinal, variant):
    with pytest.raises(ValueError, match='invalid'):
        derive_scene_still('/srv/data/n8n-media/store/firefly/images/a.png', project, ordinal, variant)


def test_requires_configured_media_host():
    with patch.dict('os.environ', {'TYPEBEAT_REMOTE_MEDIA_HOST': ''}):
        with pytest.raises(ValueError, match='configured'):
            derive_scene_still('/srv/data/n8n-media/store/firefly/images/a.png', 'valid', 2)
