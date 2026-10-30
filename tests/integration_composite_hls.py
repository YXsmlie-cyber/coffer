"""本地 fMP4 分轨、分辨率切换、暂停后段级恢复与损坏拒绝验收。"""
import asyncio
from collections import Counter
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import settings
from backend.downloader import M3U8Downloader, DownloadError, ffmpeg_path, validate_completed_artifact
from backend.scraper import resolve_hls_plan
from hls_fixtures import build_composite_fixture


class CompositeHandler(SimpleHTTPRequestHandler):
    counts = Counter()
    second_period = threading.Event()
    delay_second = False
    def log_message(self, *_args):
        pass
    def do_GET(self):
        name = self.path.split('?', 1)[0]
        self.counts[name] += 1
        if name.endswith('video1-000.m4s') and self.delay_second:
            self.second_period.set()
            time.sleep(2)
        try:
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass




async def check_composite(base, directory):
    engine = M3U8Downloader()
    try:
        plan = await resolve_hls_plan(base+'/composite.m3u8')
        assert plan['schema_version'] == 3
        output = directory/'normal.mp4'
        try:
            await engine.download(plan, str(output), task_key='composite-normal')
        except Exception:
            print('fixture requests', dict(CompositeHandler.counts), flush=True)
            print('fixture files', sorted(p.name for p in directory.iterdir()), flush=True)
            raise
        evidence = validate_completed_artifact(str(output))
        assert abs(evidence['video_duration']-6) < 0.1, evidence
        assert evidence['media_proof']['video_packet_count'] == 144
        assert evidence['media_proof']['audio_packet_count'] > 250
        print('composite: 正常分轨、分辨率切换及逐包验证通过', flush=True)

        CompositeHandler.delay_second = True
        CompositeHandler.second_period.clear()
        resumed = directory/'resumed.mp4'
        job = asyncio.create_task(engine.download(plan, str(resumed), task_key='composite-pause'))
        reached = await asyncio.to_thread(CompositeHandler.second_period.wait, 15)
        assert reached, '第二时间段没有开始'
        job.cancel()
        try:
            await job
        except asyncio.CancelledError:
            pass
        CompositeHandler.delay_second = False
        before = {k: v for k, v in CompositeHandler.counts.items() if '/video0-' in k}
        await engine.download(plan, str(resumed), task_key='composite-resume')
        after = {k: v for k, v in CompositeHandler.counts.items() if '/video0-' in k}
        assert after == before, (before, after)
        validate_completed_artifact(str(resumed))
        assert not engine._procs
        print('composite: 暂停恢复保留已完成时间段，通过', flush=True)

        bad_plan = await resolve_hls_plan(base+'/bad-composite.m3u8')
        bad_output = directory/'bad.mp4'
        try:
            await engine.download(bad_plan, str(bad_output), task_key='composite-bad')
        except DownloadError:
            pass
        else:
            raise AssertionError('重复分片未被拒绝')
        assert not bad_output.exists()
        assert not list(directory.glob('*.download-v2.lock'))
        return {'composite_tracks': True, 'resolution_change_decoded': True,
                'completed_period_reused': True, 'corrupt_period_rejected': True}
    finally:
        engine.shutdown()


def main():
    with tempfile.TemporaryDirectory(prefix='ph115-composite-') as tmp:
        directory = Path(tmp)
        build_composite_fixture(directory, ffmpeg_path())
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(CompositeHandler, directory=str(directory)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_address[1]}'
        previous = (settings.ph_base_url, settings.proxy, settings.download_proxy)
        settings.ph_base_url, settings.proxy, settings.download_proxy = base, '', ''
        try:
            print(json.dumps(asyncio.run(check_composite(base, directory))))
        finally:
            settings.ph_base_url, settings.proxy, settings.download_proxy = previous
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
