from unittest.mock import patch

import pytest

from scripts.canonical_still_motion_bridge import render_to_media


@pytest.mark.parametrize('path', ['/tmp/image.png','/srv/data/n8n-media/store/../private.png','/srv/data/n8n-media/store/image.mp4'])
def test_rejects_untrusted_reference(path):
    with pytest.raises(ValueError,match='untrusted'):
        render_to_media(path,'safe-project',1,5)


def test_rejects_unsafe_project():
    with pytest.raises(ValueError,match='invalid project'):
        render_to_media('/srv/data/n8n-media/store/image.png','../escape',1,5)


def test_rejects_unconfigured_remote_media():
    with patch.dict('os.environ',{'TYPEBEAT_REMOTE_MEDIA_HOST':''}):
        with pytest.raises(ValueError,match='configured'):
            render_to_media('/srv/data/n8n-media/store/image.png','safe-project',1,5)
