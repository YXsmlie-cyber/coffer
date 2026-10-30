"""组合 HLS 计划的纯结构约束；不访问网络、文件或数据库。"""
import hashlib
import json
import math

MAX_PERIODS = 128


def compose_plan(video_parts, audio_parts=None):
    tracks = [{"kind": "video", "parts": video_parts}]
    if audio_parts is not None:
        tracks.append({"kind": "audio", "parts": audio_parts})
    if not video_parts or any(not 0 < len(t["parts"]) <= MAX_PERIODS for t in tracks):
        raise ValueError("HLS 轨道时间段数量无效或超限")
    if audio_parts is not None and len(video_parts) != len(audio_parts):
        raise ValueError("音视频时间段边界数量不一致")
    ends = []
    for track in tracks:
        end = 0.0
        boundaries = []
        for part in track["parts"]:
            if part.get("schema_version") != 2:
                raise ValueError("组合 HLS 只接受已冻结的连续叶子计划")
            duration = float(part["media_duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("HLS 时间段时长无效")
            end += duration
            boundaries.append(end)
        ends.append(boundaries)
    if audio_parts is not None and any(abs(v-a) > 0.1 for v, a in zip(*ends)):
        raise ValueError("音视频清单时间段边界相差超过 0.1 秒")
    identity = [{"kind": t["kind"], "parts": [p["fingerprint"] for p in t["parts"]]}
                for t in tracks]
    fingerprint = hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    all_parts = [p for t in tracks for p in t["parts"]]
    return {
        "schema_version": 3, "tracks": tracks,
        "m3u8_url": video_parts[0]["m3u8_url"], "fingerprint": fingerprint,
        "media_duration": round(ends[0][-1], 6),
        "target_duration": max(p["target_duration"] for p in all_parts),
        "segment_count": sum(p["segment_count"] for p in all_parts),
        "unit_count": sum(p["unit_count"] for p in all_parts),
        "quality": video_parts[0].get("quality", 0),
        "expect_audio": audio_parts is not None or any(p.get("expect_audio") for p in video_parts),
        "encrypted": any(p.get("encrypted") for p in all_parts),
    }
