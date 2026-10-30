"""组合 HLS 计划必须覆盖所有轨道和时间段，保持签名刷新与断点身份边界。"""
import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from backend.scraper import _resolve_vod_plan, HLSPlanError
from backend.downloader import _validated_plan, _prepare_plan_state, DownloadIntegrityError


class MediaClient:
    def __init__(self, mapping):
        self.mapping = mapping
    async def get(self, url):
        return SimpleNamespace(status_code=200, text=self.mapping[url], url=url)


def media(track, *, duration=2, discontinuity=True):
    return (f'#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:3\n'
            f'#EXT-X-MAP:URI="{track}-init0.mp4"\n#EXTINF:{duration},\n{track}-0.m4s\n'
            + ('#EXT-X-DISCONTINUITY\n' if discontinuity else '')
            + f'#EXT-X-MAP:URI="{track}-init1.mp4"\n#EXTINF:{duration},\n{track}-1.m4s\n#EXT-X-ENDLIST\n')


def fixtures():
    root = 'https://cdn.example/master.m3u8'
    mapping = {
        root: '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="main",DEFAULT=YES,URI="audio.m3u8"\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=1920x1080,AUDIO="audio",CODECS="avc1.640028,mp4a.40.2"\nvideo.m3u8\n',
        'https://cdn.example/video.m3u8': media('video'),
        'https://cdn.example/audio.m3u8': media('audio'),
    }
    return root, mapping


class CompositePlanTests(unittest.IsolatedAsyncioTestCase):
    async def plan(self):
        root, mapping = fixtures()
        return await _resolve_vod_plan(MediaClient(mapping), root)

    async def test_all_tracks_periods_and_initializations_are_preserved(self):
        plan = await self.plan()
        self.assertEqual(plan['schema_version'], 3)
        self.assertEqual(plan['media_duration'], 4)
        self.assertEqual(plan['unit_count'], 8)
        self.assertEqual([t['kind'] for t in plan['tracks']], ['video', 'audio'])
        for track in plan['tracks']:
            self.assertEqual(len(track['parts']), 2)
            for index, part in enumerate(track['parts']):
                self.assertEqual(part['media_sequence'], index)
                self.assertNotIn('#EXT-X-DISCONTINUITY\n', part['manifest_text'])
                self.assertEqual(part['manifest_text'].count('#EXT-X-MAP:'), 1)
        self.assertIs(_validated_plan(plan), plan)

    async def test_track_boundary_mismatch_is_rejected(self):
        root, mapping = fixtures()
        mapping['https://cdn.example/audio.m3u8'] = media('audio', duration=2.5)
        with self.assertRaisesRegex(HLSPlanError, '音视频.*边界'):
            await _resolve_vod_plan(MediaClient(mapping), root)

    async def test_map_change_without_discontinuity_still_fails_closed(self):
        root, mapping = fixtures()
        mapping['https://cdn.example/video.m3u8'] = media('video', discontinuity=False)
        with self.assertRaises(HLSPlanError):
            await _resolve_vod_plan(MediaClient(mapping), root)

    async def test_worker_rechecks_aggregate_and_rejects_nested_plans(self):
        plan = await self.plan()
        for mutate in ('duration', 'audio', 'nested'):
            changed = copy.deepcopy(plan)
            if mutate == 'duration':
                changed['media_duration'] += 10
            elif mutate == 'audio':
                changed['tracks'].pop()
            else:
                changed['tracks'][0]['parts'][0] = copy.deepcopy(plan)
            with self.assertRaises(DownloadIntegrityError):
                _validated_plan(changed)

    async def test_changed_track_plan_discards_only_owned_component_artifacts(self):
        plan = await self.plan()
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory)/'out.mp4')
            _prepare_plan_state(output, plan)
            own = Path(directory)/'out.native-v3-video-0000.mp4'
            other = Path(directory)/'other.native-v3-video-0000.mp4'
            own.write_bytes(b'old'); other.write_bytes(b'other')
            root, mapping = fixtures()
            mapping['https://cdn.example/audio.m3u8'] = media('changed-audio')
            changed = await _resolve_vod_plan(MediaClient(mapping), root)
            self.assertFalse(_prepare_plan_state(output, changed))
            self.assertFalse(own.exists())
            self.assertEqual(other.read_bytes(), b'other')
