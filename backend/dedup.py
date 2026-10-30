"""去重逻辑。

PH视频 -> 是否已存在于115该模特文件夹？

判定优先级：
  1. 强匹配：115文件名中包含该视频的 viewkey（如 ph5f555c258b565）-> exists
  2. 弱匹配：115存在文件时长 == PH视频时长(±tolerance秒) -> maybe（标黄，默认不下载）
  3. 否则 -> missing（需下载）

下载后新文件统一命名 "标题 [viewkey].mp4"，保证以后强匹配100%可靠。

第一次去重：加入下载队列前。
第二次去重：上传前（下载期间115可能新增了同一视频）。
"""
import re
from collections import deque
from .config import settings

# PH viewkey 历史上有三种格式：
#   老格式: ph5f555c258b565   （以 ph 开头，13+ 位 hex，2020 前后）
#   新格式hex: 64e9a350f176d   （纯 13 位 hex，无 ph 前缀）
#   新格式数字: 1935323544     （纯数字 8-13 位，PH 现行的一部分 viewkey）
# 裸出现在 115 旧文件名里的，主要是 ph 前缀格式。
# 而我们自己下载的新文件统一以 "标题 [<viewkey>].mp4" 命名——方括号是我们构造的可靠
# 锚点，里面就是完整 viewkey（可能是 hex 也可能是纯数字），所以方括号格式不限定
# hex、不限定长度下限到 13，只要求是一串字母数字（≥6 位以避免误匹配）。
# ph 前缀格式：加右边界 (?![0-9a-f])，否则贪婪匹配会把紧跟其后的 hex 字符一并吞入
# （如 ph<13hex> 后面还接着 hex），得到比真实 viewkey 更长的串，导致强匹配 miss → 重复下载。
VIEWKEY_PH_RE = re.compile(r"(ph[0-9a-f]{13,})(?![0-9a-z])", re.IGNORECASE)
# 方括号锚点里的内容限定为已知 viewkey 格式，避免把 [Brazzers]/[FullHD] 这类标签当 viewkey：
#   - ph 前缀 hex，或
#   - 纯 hex 13+ 位（新格式 hex），或
#   - 纯数字 8-13 位（新格式数字）
# 收紧后既降低污染索引，也消除"标题方括号内容恰好等于某 viewkey 而误删干净文件"的理论风险。
VIEWKEY_BRACKET_RE = re.compile(
    r"\[(ph[0-9a-f]{13,}|[0-9a-f]{13,}|[0-9]{8,13})\]", re.IGNORECASE)


def extract_viewkeys(filename: str) -> set[str]:
    """从115文件名中提取所有 viewkey（小写）。ph前缀裸格式 + 方括号锚点两种来源。"""
    keys: set[str] = set()
    keys.update(m.lower() for m in VIEWKEY_PH_RE.findall(filename))
    keys.update(m.lower() for m in VIEWKEY_BRACKET_RE.findall(filename))
    return keys


def build_cloud_index(cloud_files: list[dict]) -> dict:
    """把115文件列表构建成索引。

    cloud_files: 115 list_all_files 的原始项，含 n(名字), play_long(时长), s(大小)
    返回 viewkey、兼容旧调用的 durations，以及按整数时长分桶的文件级索引。
    duration_items / duration_buckets 都保留原目录 idx，既供批量弱匹配一对一
    占用，也让上传前的单视频二次去重只扫描 tolerance 范围内的少量桶。
    """
    viewkeys: set[str] = set()
    durations: list[tuple[int, str]] = []
    duration_items: list[dict] = []
    duration_buckets: dict[int, list[dict]] = {}
    next_duration_idx = 0
    for idx, f in enumerate(cloud_files):
        name = f.get("n", "")
        file_viewkeys = extract_viewkeys(name)
        viewkeys |= file_viewkeys
        dur = f.get("play_long") or 0
        try:
            dur = int(float(dur))
        except (ValueError, TypeError):
            dur = 0
        if dur > 0:
            durations.append((dur, name))
            item = {
                "idx": idx,
                "duration": dur,
                "name": name,
                "viewkeys": file_viewkeys,
            }
            duration_items.append(item)
            duration_buckets.setdefault(dur, []).append(item)
            next_duration_idx = idx + 1
    return {
        "viewkeys": viewkeys,
        "durations": durations,
        "duration_items": duration_items,
        "duration_buckets": duration_buckets,
        "next_duration_idx": next_duration_idx,
    }


def match_video(viewkey: str, duration: int, cloud_index: dict,
                tolerance: int | None = None) -> tuple[str, str]:
    """判定单个PH视频在115中的状态。

    返回 (status, matched_name)
      status: exists / maybe / missing
    """
    tol = settings.duration_tolerance if tolerance is None else tolerance
    vk = (viewkey or "").lower()

    # 1. 强匹配
    if vk and vk in cloud_index["viewkeys"]:
        return "exists", ""

    # 2. 弱匹配（时长）
    if duration and duration > 0:
        buckets = cloud_index.get("duration_buckets")
        if isinstance(buckets, dict):
            # 旧实现按 115 原目录顺序返回第一个落在 tolerance 内的文件，
            # 而不是优先选择“时长最接近”的文件。每个桶内部 idx 递增，取各桶
            # 首项后再选最小 idx，能在 O(tolerance) 内严格保持该语义。
            candidates = []
            for candidate_duration in range(
                max(1, int(duration) - int(tol)),
                int(duration) + int(tol) + 1,
            ):
                bucket = buckets.get(candidate_duration)
                if bucket:
                    candidates.append(bucket[0])
            if candidates:
                matched = min(candidates, key=lambda item: int(item["idx"]))
                return "maybe", str(matched["name"])
        else:
            # 兼容测试、旧缓存或外部调用者手工构造的老格式索引。
            for dur, name in cloud_index.get("durations", []):
                if abs(dur - duration) <= tol:
                    return "maybe", name

    # 3. 缺失
    return "missing", ""


def dedup_model_videos(videos: list[dict], cloud_files: list[dict],
                       tolerance: int | None = None) -> list[dict]:
    """批量去重。

    videos: [{viewkey, duration, ...}]
    cloud_files: 115该模特文件夹的文件列表
    返回带 dedup_status / matched_cloud_name 的列表。

    弱匹配必须一对一占用：旧文件名缺少 viewkey 时只能用时长做兜底，但同一个
    115 文件不能让多个 PH 视频都变成 maybe，否则自动巡查会误以为大量视频已存在。
    """
    index = build_cloud_index(cloud_files)
    tol = settings.duration_tolerance if tolerance is None else tolerance
    out = []
    # 旧实现为每个 PH 视频遍历全部 115 文件，复杂度接近 O(PH×115)。
    # 弱匹配只会命中 duration±tol，因此按整数时长分桶；每个桶保持原目录 idx 顺序，
    # 桶首就是该时长仍未被占用且 idx 最小的文件，可严格保持旧选择规则。
    duration_buckets: dict[int, deque[dict]] = {}
    for item in index["duration_items"]:
        if item["viewkeys"]:
            continue
        duration_buckets.setdefault(item["duration"], deque()).append(item)

    for v in videos:
        viewkey = (v.get("viewkey", "") or "").lower()
        duration = v.get("duration", 0) or 0

        if viewkey and viewkey in index["viewkeys"]:
            out.append({**v, "dedup_status": "exists", "matched_cloud_name": ""})
            continue

        matched_item = None
        if duration and duration > 0:
            candidates = []
            for candidate_duration in range(max(1, duration - tol), duration + tol + 1):
                bucket = duration_buckets.get(candidate_duration)
                if not bucket:
                    continue
                item = bucket[0]
                candidates.append((abs(candidate_duration - duration), item["idx"], item))
            if candidates:
                candidates.sort(key=lambda x: (x[0], x[1]))
                matched_item = candidates[0][2]
                duration_buckets[matched_item["duration"]].popleft()

        if matched_item:
            out.append({**v, "dedup_status": "maybe", "matched_cloud_name": matched_item["name"]})
        else:
            out.append({**v, "dedup_status": "missing", "matched_cloud_name": ""})
    return out


def count_missing(deduped: list[dict]) -> dict:
    """统计 exists / maybe / missing 数量。"""
    c = {"exists": 0, "maybe": 0, "missing": 0}
    for v in deduped:
        c[v.get("dedup_status", "missing")] += 1
    return c
