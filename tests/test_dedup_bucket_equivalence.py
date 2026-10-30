import random
import unittest

from backend import dedup


def reference_dedup(videos, cloud_files, tolerance):
    """优化前的一对一弱匹配实现，用于逐项等价性回归。"""
    index = dedup.build_cloud_index(cloud_files)
    out = []
    used = set()
    for video in videos:
        viewkey = (video.get("viewkey", "") or "").lower()
        duration = video.get("duration", 0) or 0
        if viewkey and viewkey in index["viewkeys"]:
            out.append({**video, "dedup_status": "exists", "matched_cloud_name": ""})
            continue
        candidates = []
        if duration > 0:
            for item in index["duration_items"]:
                if item["idx"] in used or item["viewkeys"]:
                    continue
                difference = abs(item["duration"] - duration)
                if difference <= tolerance:
                    candidates.append((difference, item["idx"], item))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]))
            matched = candidates[0][2]
            used.add(matched["idx"])
            out.append({
                **video,
                "dedup_status": "maybe",
                "matched_cloud_name": matched["name"],
            })
        else:
            out.append({**video, "dedup_status": "missing", "matched_cloud_name": ""})
    return out


class DedupBucketEquivalenceTests(unittest.TestCase):
    def test_single_match_bucket_preserves_original_directory_order(self):
        index = dedup.build_cloud_index([
            {"n": "first-in-directory.mp4", "play_long": 62},
            {"n": "closer-but-later.mp4", "play_long": 60},
        ])

        self.assertEqual(
            dedup.match_video("missing00000001", 60, index, tolerance=3),
            ("maybe", "first-in-directory.mp4"),
        )

    def test_single_match_accepts_legacy_index_without_buckets(self):
        legacy = {
            "viewkeys": set(),
            "durations": [(90, "legacy.mp4")],
            "duration_items": [],
        }

        self.assertEqual(
            dedup.match_video("missing00000001", 92, legacy, tolerance=3),
            ("maybe", "legacy.mp4"),
        )

    def test_randomized_results_match_reference_algorithm(self):
        rng = random.Random(115)
        for _ in range(100):
            cloud = []
            for index in range(rng.randint(0, 80)):
                duration = rng.randint(1, 600)
                if rng.random() < 0.2:
                    key = f"{index + 0x1000000000000:013x}"
                    name = f"Cloud [{key}].mp4"
                else:
                    name = f"Legacy {index}.mp4"
                cloud.append({"n": name, "play_long": duration, "fid": str(index)})
            videos = []
            for index in range(rng.randint(0, 100)):
                if cloud and rng.random() < 0.2:
                    keys = list(dedup.extract_viewkeys(rng.choice(cloud)["n"]))
                    viewkey = keys[0] if keys else f"missing{index:08d}"
                else:
                    viewkey = f"missing{index:08d}"
                videos.append({
                    "viewkey": viewkey,
                    "duration": rng.randint(0, 600),
                    "title": str(index),
                })
            tolerance = rng.randint(0, 8)
            self.assertEqual(
                dedup.dedup_model_videos(videos, cloud, tolerance),
                reference_dedup(videos, cloud, tolerance),
            )

    def test_equal_duration_weak_files_remain_one_to_one_in_directory_order(self):
        cloud = [
            {"n": "first.mp4", "play_long": 60},
            {"n": "second.mp4", "play_long": 60},
        ]
        videos = [
            {"viewkey": "missing00000001", "duration": 60},
            {"viewkey": "missing00000002", "duration": 60},
            {"viewkey": "missing00000003", "duration": 60},
        ]
        result = dedup.dedup_model_videos(videos, cloud, tolerance=3)
        self.assertEqual(
            [(item["dedup_status"], item["matched_cloud_name"]) for item in result],
            [("maybe", "first.mp4"), ("maybe", "second.mp4"), ("missing", "")],
        )


if __name__ == "__main__":
    unittest.main()
