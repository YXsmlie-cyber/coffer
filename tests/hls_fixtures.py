"""源码与冻结包共用的本地合成媒体；不读取应用配置或访问网络。"""
import subprocess


def build_composite_fixture(directory, ffmpeg):
    for kind in ('video', 'audio'):
        playlists = []
        for period in range(2):
            name = f'{kind}{period}'
            source = (f'testsrc2=size={320 if period == 0 else 306}x180:rate=24:duration=3'
                      if kind == 'video' else 'sine=frequency=1000:sample_rate=48000:duration=3')
            codec = (['-c:v', 'libx264', '-preset', 'ultrafast', '-g', '24', '-sc_threshold', '0', '-an']
                     if kind == 'video' else ['-c:a', 'aac', '-ac', '2', '-vn'])
            subprocess.run([str(ffmpeg), '-y', '-f', 'lavfi', '-i', source, *codec,
                            '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod',
                            '-hls_segment_type', 'fmp4', '-hls_fmp4_init_filename', name+'-init.mp4',
                            '-hls_segment_filename', str(directory/(name+'-%03d.m4s')),
                            str(directory/(name+'.m3u8'))], check=True, capture_output=True, cwd=directory)
            text = (directory/(name+'.m3u8')).read_text()
            body = text[text.index('#EXT-X-MAP:'):].replace('#EXT-X-ENDLIST', '').strip()
            playlists.append(body)
        (directory/(kind+'.m3u8')).write_text(
            '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:2\n'
            + '\n#EXT-X-DISCONTINUITY\n'.join(playlists)+'\n#EXT-X-ENDLIST\n', encoding='utf-8')
    master = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="main",DEFAULT=YES,URI="audio.m3u8"\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=320x180,AUDIO="a",CODECS="avc1.42e01e,mp4a.40.2"\nvideo.m3u8\n')
    (directory/'composite.m3u8').write_text(master, encoding='utf-8')
    bad = (directory/'video.m3u8').read_text().replace('video1-001.m4s', 'video1-000.m4s')
    (directory/'bad-video.m3u8').write_text(bad, encoding='utf-8')
    (directory/'bad-composite.m3u8').write_text(master.replace('\nvideo.m3u8', '\nbad-video.m3u8'), encoding='utf-8')
