"""Wan 2.2 local-provider gates. No GPU, network, or live user data required."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
from scripts import music_factory_provider_router as router
from scripts import music_factory_wan22_local as wan


def valid_evidence():
    return {
        'provider': 'wan22_local',
        'verified_at': int(time.time()),
        'motion_verified': True,
        'native_motion_verified': True,
        'production_resolution_verified': True,
        'source_image_first_verified': True,
        'no_external_api_credits': True,
        'approved_free_route': True,
        'factory_publishing_approved': False,
        'duration_seconds': 5.0,
        'native_width': 1280,
        'native_height': 704,
        'sha256': 'a' * 64,
    }


class WanLocalProviderTests(unittest.TestCase):
    def test_empty_health_holds_and_never_falls_back_to_firefly(self):
        with tempfile.TemporaryDirectory() as d:
            health = Path(d) / 'health.json'
            health.write_text('{}')
            self.assertFalse(router.factory_provider_readiness(health_file=health)['ready'])
            self.assertEqual(router.scene_provider_order(1, health_file=health), ())

    def test_production_gate_requires_every_qa_condition(self):
        with tempfile.TemporaryDirectory() as d:
            health = Path(d) / 'health.json'
            base = valid_evidence()
            def check(record):
                health.write_text(json.dumps({'wan22_local': record}))
                return router.factory_provider_readiness(health_file=health)['ready']
            self.assertTrue(check(base))
            for key, value in (
                ('native_width', 384), ('native_height', 224),
                ('duration_seconds', 2.1), ('verified_at', 0),
                ('source_image_first_verified', False),
                ('production_resolution_verified', False),
                ('no_external_api_credits', False),
                ('approved_free_route', False),
                ('factory_publishing_approved', True),
                ('sha256', 'unverified'),
            ):
                changed = dict(base)
                changed[key] = value
                self.assertFalse(check(changed), (key, value))

    def test_previously_verifiable_provider_does_not_unlock_firefly(self):
        with tempfile.TemporaryDirectory() as d:
            health = Path(d) / 'health.json'
            health.write_text(json.dumps({'metaai': {
                'provider': 'metaai', 'motion_verified': True,
                'approved_free_route': True, 'verified_at': int(time.time())
            }}))
            self.assertEqual(router.scene_provider_order(1, health_file=health), ('metaai',))

    def test_production_benchmark_never_submits_without_smoke_qa(self):
        from scripts import music_factory_wan22_production_benchmark as bench
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            with patch.object(bench, 'SMOKE', folder/'missing-smoke.json'):
                with patch.object(bench, 'BENCHMARK', folder/'uncreated-output'):
                    with self.assertRaisesRegex(wan.WanUnavailable, 'smoke output'):
                        bench.submit(1800)
                    self.assertFalse((folder/'uncreated-output').exists())

    def test_old_cloud_canary_refuses_local_wan_without_mutating_health(self):
        from scripts import music_factory_provider_canary as legacy
        self.assertNotIn('wan22_local', legacy.CLOUD_CANARY_PROVIDERS)
        with tempfile.TemporaryDirectory() as d:
            fake=Path(d)/'health.json'
            fake.write_text('{"wan22_local":{"motion_verified":true}}')
            before=fake.read_bytes()
            with patch.object(legacy, 'HEALTH', fake):
                with self.assertRaisesRegex(ValueError,'separately verified'):
                    legacy.verify_provider('wan22_local',activate=True,timeout=1)
            self.assertEqual(fake.read_bytes(),before)

    def test_wan_without_verified_receipt_does_not_submit(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(wan, 'HEALTH', Path(d) / 'empty.json'):
                with self.assertRaises(wan.WanUnavailable):
                    router.request_video(
                        'wan22_local', 'A legitimate cinematic rainy street.',
                        1, timeout=380, image_file=None
                    )

    def test_api_graph_has_cpu_encoder_and_minimum_dimensions(self):
        with tempfile.TemporaryDirectory() as d:
            stage = Path(d)
            fixture = {
                '38': {'class_type': 'CLIPLoader', 'inputs': {'device': 'cpu'}},
                '37': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'fixture'}},
                '55': {'class_type': 'Wan22ImageToVideoLatent', 'inputs': {
                    'width': 384, 'height': 224, 'length': 25, 'batch_size': 1}},
                '56': {'class_type': 'LoadImage', 'inputs': {'image': 'fixture.png'}},
                '6': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'fixture'}},
                '3': {'class_type': 'KSampler', 'inputs': {'steps': 6, 'seed': 1}},
                '57': {'class_type': 'CreateVideo', 'inputs': {'fps': 12}},
                '58': {'class_type': 'SaveVideo', 'inputs': {
                    'filename_prefix': 'fixture', 'format': 'mp4', 'codec': 'h264'}},
            }
            (stage / 'api-prompt-fp8.json').write_text(json.dumps(fixture))
            with patch.object(wan, 'STAGING', stage):
                name = 'wan22-approved-' + 'a' * 24 + '.png'
                text = 'Slow dolly through a rain-soaked city at blue hour.'
                graph = wan.build_plan(text, 1, name)
                self.assertEqual(graph['55']['inputs']['width'], 1280)
                self.assertEqual(graph['55']['inputs']['height'], 704)
                self.assertEqual(graph['55']['inputs']['length'], 61)
                self.assertEqual(graph['38']['inputs']['device'], 'cpu')
                self.assertEqual(graph['37']['inputs']['unet_name'], wan.CHECKPOINT)
                self.assertEqual(graph['8']['class_type'], 'VAEDecodeTiled')
                self.assertEqual(graph['8']['inputs']['tile_size'], 128)
                self.assertEqual(graph['8']['inputs']['temporal_size'], 16)
                with self.assertRaises(wan.WanUnavailable):
                    wan.build_plan(text, 1, name, width=384, height=224)
                with self.assertRaises(wan.WanUnavailable):
                    wan.build_plan(text, 1, name, length=25)
                with self.assertRaises(wan.WanUnavailable):
                    wan.build_plan(text, 1, '../../../bad.png')

    def test_image_stage_preserves_original_and_refuses_missing_image(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            image = root / 'image.png'
            Image.new('RGB', (640, 360), (30, 50, 70)).save(image)
            original_hash = hashlib.sha256(image.read_bytes()).hexdigest()
            with patch.object(wan, 'APPROVED_IMAGE_ROOTS', (root,)), patch.object(wan, 'INPUTS', root / 'stage'):
                name = wan.prepare_input(image)
                self.assertEqual(name, wan.prepare_input(image))
                self.assertTrue((root / 'stage' / name).is_file())
                self.assertEqual(original_hash, hashlib.sha256(image.read_bytes()).hexdigest())
                with self.assertRaises(wan.WanUnavailable):
                    wan.prepare_input(root / 'missing.png')

    def test_fp8_bundle_manifest_gate_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            files=[
                ('diffusion_models/'+wan.CHECKPOINT,37,wan.CHECKPOINT_SHA256),
                ('vae/wan2.2_vae.safetensors',21,'e'*64),
                ('text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors',27,'f'*64),
            ]
            for name,length,digest in files:
                target=root/name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(b'x'*length)
            manifest=root/'manifest.json'
            data={'model':'Wan2.2 TI2V-5B FP8 Kijai',
                  'sha256_verified':True,
                  'files':[{'rel':name,'size':length,'sha256':digest}
                           for name,length,digest in files]}
            with patch.object(wan,'MODEL_MOUNT',root),patch.object(wan,'MANIFEST',manifest):
                manifest.write_text(json.dumps(data))
                self.assertTrue(wan.verified_local_model_bundle())
                data['files'][0]['sha256']='bad'
                manifest.write_text(json.dumps(data))
                self.assertFalse(wan.verified_local_model_bundle())
                data['files'][0]['sha256']=wan.CHECKPOINT_SHA256
                data['sha256_verified']=False
                manifest.write_text(json.dumps(data))
                self.assertFalse(wan.verified_local_model_bundle())
                data['sha256_verified']=True
                data['files'][2]['size']+=1
                manifest.write_text(json.dumps(data))
                self.assertFalse(wan.verified_local_model_bundle())

    def test_local_receipt_rejects_paths_outside_approved_directory(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / 'motion.mp4'
            file.write_bytes(b'not a real mp4')
            with self.assertRaises(ValueError):
                router.validated_path('wan22_local', {
                    'success': True, 'provider': 'wan22_local',
                    'host_path': str(file), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
                    'no_external_api_credits': True, 'native_motion_verified': True,
                }, slot=1)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg unavailable')
    def test_native_motion_rejects_static_short_clip(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'static.mp4'
            subprocess.run([
                'ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi',
                '-i', 'color=c=black:s=1280x704:r=12', '-t', '2.0',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(path),
            ], capture_output=True, check=True, timeout=40)
            with self.assertRaises(wan.WanUnavailable):
                wan._moving_native(path)

if __name__ == '__main__':
    unittest.main(verbosity=2)
