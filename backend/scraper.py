"""PornHub 爬虫。

三类抓取：
  1. 榜单页 -> 模特列表（名字/头像/链接）
  2. 模特视频列表 -> 视频（标题/viewkey/时长/缩略图），自动翻页
  3. 视频页 -> 最高画质 M3U8 链接

所有请求走本地代理，带常见反爬headers。PH视频页内嵌 flashvars JSON，
其中 mediaDefinitions / media_definitions 含各清晰度的m3u8地址。
"""
import re
import json
import hashlib
import math
import asyncio
import inspect
import copy
import html as html_lib
import ipaddress
import time
import unicodedata
from urllib.parse import (
    parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse,
)
import httpx
from bs4 import BeautifulSoup

from .config import settings
from .hls_composite import compose_plan, MAX_PERIODS

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": settings.ph_base_url + "/",
}


class ScrapeError(Exception):
    """页面或媒体解析失败；默认属于确定性错误，不做整条下载重试。"""

    retryable = False


class InvalidScrapeURL(ScrapeError):
    """请求地址不在允许的抓取边界内。"""


class InvalidModelQuery(ScrapeError):
    """精确模特查找的名字/账号格式无效。"""


class ModelLookupNotFound(ScrapeError):
    """已成功访问候选页面，但没有找到有效模特主页。"""


class ModelLookupUnavailable(ScrapeError):
    """候选页面因网络、限流或反爬暂时无法确认。"""


class ModelProfileUnavailable(ScrapeError):
    """目标主页被站点明确标记为不可用或已删除。"""


class ScrapeFetchError(ScrapeError):
    """页面或媒体暂时不可达，允许刷新会话后重新尝试。"""

    retryable = True


class VideoUnavailableError(ScrapeError):
    """目标视频已被站点明确下架或删除，不应继续消耗网络重试。"""


class HLSManifestFetchError(ScrapeFetchError):
    """媒体清单暂时无法获取，可刷新签名后重试。"""


class HLSSourceUnavailableError(HLSManifestFetchError):
    """页面仍可用，但当前 CDN 清单已失效；即时重试后进入有限冷却。"""


class HLSRateLimitedError(HLSManifestFetchError):
    """清单请求返回 429：CDN 按本机出口限流，与分片 429 共用全局限流冷却。"""


class HLSPlanError(ScrapeError):
    """HLS 清单不满足可证明完整下载的确定性约束。"""


_MAX_HLS_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_HLS_DEPTH = 8
# PH 每次加载视频页随机把整组 HLS 地址分到 ev-h / hv-h 等 CDN 主机。2026-10 实测
# hv-h 对本机出口的清单请求全部 410（842/842），且占页面分配的 55%–75%；同一视频
# 重新加载页面即可分到可用主机。10 次加载全落在 hv-h 的概率约 0.6%–6%。
_CDN_REROLL_PAGE_LOADS = 10
_CDN_REROLL_DELAY_SECONDS = 1.0
_HLS_PLAN_VERSION = 2
_VOLATILE_MEDIA_QUERY_KEYS = {
    "auth", "e", "expire", "expires", "f", "h", "hash", "hdl", "hdnea",
    "hdnts", "ipa", "policy", "signature", "sig", "token", "validfrom",
    "validto",
}


def _effective_port(parts) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme.lower() == "https" else 80


def validate_ph_url(url: str) -> str:
    """验证页面抓取 URL，仅允许配置的 PH 站点及其官方子域。

    PH_BASE_URL 可在测试/私有部署中显式改成其它主机；此时只允许该精确
    主机、协议和端口。默认 pornhub.com 配置允许其官方语言子域之间跳转。
    """
    try:
        parts = urlparse(str(url).strip())
        base = urlparse(settings.ph_base_url)
        host = (parts.hostname or "").lower().rstrip(".")
        base_host = (base.hostname or "").lower().rstrip(".")
        port = _effective_port(parts)
        base_port = _effective_port(base)
    except (TypeError, ValueError) as exc:
        raise InvalidScrapeURL("无效的榜单 URL") from exc

    if parts.scheme.lower() not in {"http", "https"} or not host:
        raise InvalidScrapeURL("榜单 URL 只允许 http/https")
    if parts.username or parts.password:
        raise InvalidScrapeURL("榜单 URL 不允许包含账号信息")

    if base_host == "pornhub.com" or base_host.endswith(".pornhub.com"):
        allowed_host = host == "pornhub.com" or host.endswith(".pornhub.com")
        allowed_port = port in {80, 443}
    else:
        allowed_host = host == base_host
        allowed_port = port == base_port and parts.scheme.lower() == base.scheme.lower()
    if not allowed_host or not allowed_port:
        raise InvalidScrapeURL("榜单 URL 必须属于配置的 PH 站点")
    return str(url).strip()


def validate_media_url(url: str) -> str:
    """拒绝媒体清单中的本机/私网字面地址，降低上游内容触发 SSRF 的风险。"""
    value = str(url or "").strip()
    try:
        # 本地集成测试可把 PH_BASE_URL 显式设为本机夹具，此时精确放行。
        validate_ph_url(value)
        return value
    except InvalidScrapeURL:
        pass

    try:
        parts = urlparse(value)
        host = (parts.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError) as exc:
        raise InvalidScrapeURL("无效的媒体 URL") from exc
    if parts.scheme.lower() not in {"http", "https"} or not host:
        raise InvalidScrapeURL("媒体 URL 只允许 http/https")
    if parts.username or parts.password:
        raise InvalidScrapeURL("媒体 URL 不允许包含账号信息")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise InvalidScrapeURL("媒体 URL 不允许访问本机地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return value
    if not address.is_global:
        raise InvalidScrapeURL("媒体 URL 不允许访问本机或私网地址")
    return value


async def _guard_ph_request(request: httpx.Request):
    validate_ph_url(str(request.url))


async def _guard_media_request(request: httpx.Request):
    validate_media_url(str(request.url))


def _client(*, ph_only: bool = True, media_path: bool = False,
            personalized_recommendations: bool = False) -> httpx.AsyncClient:
    # 页面抓取与媒体下载是两条独立网络路径。最终 master/leaf 清单必须和
    # 分片使用同一个 DOWNLOAD_PROXY 出口，避免签名被出口 IP/地区绑定后稳定 403。
    proxy_value = settings.download_proxy if media_path else settings.proxy
    proxy = proxy_value or None
    hook = _guard_ph_request if ph_only else _guard_media_request
    cookies = {"age_verified": "1", "platform": "pc"}
    if personalized_recommendations:
        # PH 会在只允许必要 Cookie（cookieConsent=2）的匿名会话中返回一个
        # HTTP 200 的空推荐容器，并显示“您已经关闭了推荐”。值 3 是站点当前
        # 用于允许个性化 Cookie 的选择；只给短生命周期的推荐来源会话，避免
        # 让其余榜单、主页、视频页抓取无谓地携带非必要 Cookie。
        cookies["cookieConsent"] = "3"
    return httpx.AsyncClient(headers=HEADERS, timeout=40.0, follow_redirects=True,
                             proxy=proxy, cookies=cookies,
                             event_hooks={"request": [hook]})


def create_page_client() -> httpx.AsyncClient:
    """给头像/预览提供独立页面池；仍使用相同来源校验、代理与 Cookie。"""
    return _client()


async def fetch_profile_page(client: httpx.AsyncClient, url: str):
    """维护工具读取权威主页的稳定入口，保留页面重试预算。"""
    return await _fetch_ok(client, url)


def creator_identity_from_url(url: str) -> dict | None:
    """返回已识别的创作者身份，内部 URL 解析规则由抓取层维护。"""
    return _creator_identity_from_url(url)


def model_slug_from_url(url: str) -> str:
    return _model_slug_from_url(url)


async def resolve_video_poster(client: httpx.AsyncClient, viewkey: str) -> tuple[str, str] | None:
    """从对应视频页取得已校验封面和来源；调用方无需解析 flashvars。"""
    page_url = validate_ph_url(f"{settings.ph_base_url}/view_video.php?viewkey={viewkey}")

    def valid_page(response):
        if _viewkey_from_url(str(response.url)) != viewkey:
            return False
        html = str(getattr(response, "text", "") or "")
        if _extract_flashvars(html) is not None:
            return True
        # 已下架视频返回 200 和站点外壳；按拦截页退避会白等整个抓取预算。
        # 目标 viewkey 未变且站点给出专用下架容器时立即结束，由调用方记为不可用。
        if _video_unavailable_reason(html):
            raise VideoUnavailableError("视频已下架或不可播放")
        return False

    try:
        response = await _fetch_ok(client, page_url, page_validator=valid_page)
        if response is None or response.status_code != 200 or not valid_page(response):
            return None
    except VideoUnavailableError:
        return None
    source = str((_extract_flashvars(response.text) or {}).get("image_url") or "").strip()
    if not source:
        return "", ""
    return validate_media_url(source), validate_ph_url(str(response.url))


def normalize_model_query(query: str) -> str:
    """把精确名字/账号规范化为 PH 主页 slug。

    第一版只接受名字或账号，不接受任意 URL。空白会转换为连字符，方便
    ``Riley Reid`` 这类显示名回退到 ``riley-reid`` 主页。
    """
    value = unicodedata.normalize("NFKC", str(query or "")).strip()
    if value.startswith("@"):
        value = value[1:].strip()
    value = re.sub(r"[\s-]+", "-", value).strip("-")
    value = value.casefold()
    if not value or len(value) > 80 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        raise InvalidModelQuery("请输入有效的模特名字或账号（仅支持字母、数字、空格、- 和 _）")
    return value


def _parse_model_profile(response, expected_url: str, expected_kind: str,
                         expected_slug: str) -> dict | None:
    """严格解析个人主页；目录/榜单兜底页必须返回 None。"""
    final_url = str(getattr(response, "url", expected_url))
    try:
        validate_ph_url(final_url)
        path = unquote(urlparse(final_url).path).rstrip("/")
    except InvalidScrapeURL:
        return None
    match = re.fullmatch(r"/(model|pornstar)/([^/]+)", path, re.IGNORECASE)
    if (not match or match.group(1).casefold() != expected_kind
            or match.group(2).casefold() != expected_slug):
        return None

    soup = BeautifulSoup(response.text, "lxml")
    # topProfileHeader 是两类真实个人页共有的稳定边界；榜单兜底页没有它。
    header = soup.select_one("section.topProfileHeader, .topProfileHeader")
    if not header:
        return None
    name_element = header.select_one(".nameSubscribe h1, h1")
    name = " ".join(name_element.stripped_strings).strip() if name_element else ""
    if not name or len(name) > 120:
        return None

    avatar = (header.select_one("img#getAvatar") or header.select_one("img.avatar")
              or soup.select_one("img#getAvatar"))
    thumbnail = ""
    if avatar:
        thumbnail = (avatar.get("data-src") or avatar.get("data-thumb_url")
                     or avatar.get("data-image") or avatar.get("src") or "").strip()
    if not thumbnail:
        return None
    thumbnail = urljoin(final_url, thumbnail)
    try:
        validate_media_url(thumbnail)
    except InvalidScrapeURL:
        return None

    return {
        "ph_id": expected_slug,
        "name": name,
        "url": expected_url,
        "thumbnail": thumbnail,
        "rank": 0,
        "ph_video_count": 0,
        "views_text": "",
    }


async def lookup_model_profile(query: str) -> dict:
    """按 /model/、/pornstar/ 顺序精确查找一个真实个人主页。"""
    slug = normalize_model_query(query)
    unavailable = False
    async with _client() as client:
        for kind in ("model", "pornstar"):
            candidate = validate_ph_url(urljoin(
                settings.ph_base_url.rstrip("/") + "/", f"{kind}/{slug}"))
            response = await _fetch_ok(client, candidate)
            if response is None:
                unavailable = True
                continue
            profile = await asyncio.to_thread(
                _parse_model_profile, response, candidate, kind, slug)
            if profile:
                return profile
    # 任一候选页未能穿透时无法可靠断言“不存在”，应允许用户稍后重试。
    if unavailable:
        raise ModelLookupUnavailable("PornHub 暂时不可访问或仍在拦截请求，请稍后重试")
    raise ModelLookupNotFound("未找到该模特，请输入精确名字或账号")


def _model_profile_identity(model_url: str) -> tuple[str, str, str]:
    """从已验证的个人主页 URL 提取类型和 slug。"""
    candidate = validate_ph_url(model_url).split("?", 1)[0].rstrip("/")
    path = unquote(urlparse(candidate).path).rstrip("/")
    match = re.fullmatch(r"/(model|pornstar)/([^/]+)", path, re.IGNORECASE)
    if not match:
        raise InvalidScrapeURL("模特主页 URL 必须精确指向 /model/ 或 /pornstar/")
    return match.group(1).casefold(), match.group(2).casefold(), candidate


async def fetch_model_profile_url(model_url: str, *, client=None) -> dict:
    """读取一个已知个人主页并返回权威资料，禁止目录页或空头像通过。

    允许站点在 `/model/` 与 `/pornstar/` 间对同一 slug 做规范化跳转，
    但跳转后的 slug 必须保持完全一致。
    """
    _kind, expected_slug, candidate = _model_profile_identity(model_url)

    async def fetch(active_client):
        response = await _fetch_ok(active_client, candidate)
        if response is None:
            raise ModelLookupUnavailable("模特主页暂时不可访问或仍在拦截请求")
        status_code = int(getattr(response, "status_code", 200) or 0)
        if status_code in {404, 410}:
            raise ModelLookupNotFound("模特主页已不存在")
        final_url = str(getattr(response, "url", candidate))
        try:
            final_path = unquote(urlparse(validate_ph_url(final_url)).path).rstrip("/")
        except InvalidScrapeURL as exc:
            raise ModelLookupUnavailable("模特主页跳转到了不受信任的地址") from exc
        match = re.fullmatch(r"/(model|pornstar)/([^/]+)", final_path, re.IGNORECASE)
        if not match or match.group(2).casefold() != expected_slug:
            target = match.group(2).casefold() if match else final_path
            raise ModelLookupUnavailable(
                f"模特主页从 {expected_slug} 跳转到 {target}，拒绝自动合并身份")
        canonical = final_url.split("?", 1)[0].rstrip("/")
        profile = await asyncio.to_thread(
            _parse_model_profile, response, canonical, match.group(1).casefold(), expected_slug)
        if not profile:
            raise ModelLookupUnavailable(
                f"模特主页 {canonical} 缺少可验证的姓名或头像，可能已改版")
        profile["url"] = canonical
        return profile

    if client is not None:
        return await fetch(client)
    async with _client() as owned_client:
        return await fetch(owned_client)


# ---------- PH 反爬（JS 计算挑战 + 限流退避）----------
# PH 对爬虫请求返回三类响应：
#   1) ~1.5KB 的 JS 计算挑战页（含 leastFactor 函数）：要求算出一个大整数 p 的最小质因子 n，
#      据此设置 KEY=<n>*<p/n>:<s>:<const>:1 cookie 后重新请求，服务端才放行。此处用纯 Python
#      复现该计算（_solve_challenge）。挑战里的 p 由初值经若干基于 s 位的加减、末尾常数得到，
#      /* */ 是干扰注释，需先剔除。
#   2) 2955 字节的 "Down for Maintenance 403" 限流页（无挑战）：请求过密的软封锁信号，指数退避重试。
#   3) ≥50KB 的真实内容页：成功。
# 真实列表/视频页均 >100KB，故以长度阈值区分成功与拦截页。
_REAL_PAGE_MIN_BYTES = 50_000
_FETCH_MAX_TRIES = 12
_CIRCUIT_MAX_COOLDOWN = 20.0
_circuit_lock = asyncio.Lock()
_circuit_failures = 0
_circuit_blocked_until = 0.0


class _FetchBudget:
    """一次抓取阶段共享的有限重试/无进展预算。

    调用方可以在已验证的业务进展后为下一阶段续期，但续期不会恢复已经消耗的
    重试次数。这样长分页不会仅因累计墙钟时间失败，真正卡住的单页仍然有界。
    """

    def __init__(self, *, retries: int | None = None,
                 timeout_seconds: float | None = None):
        max_tries = _FETCH_MAX_TRIES if retries is None else int(retries)
        self.retries_remaining = max(0, max_tries - 1)
        timeout = (float(timeout_seconds) if timeout_seconds is not None
                   else min(180.0, float(settings.model_sync_timeout_seconds)))
        self._timeout_seconds = max(1.0, timeout)
        self.deadline = time.monotonic() + self._timeout_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def take_retry(self) -> bool:
        if self.retries_remaining <= 0 or self.remaining_seconds() <= 0:
            return False
        self.retries_remaining -= 1
        return True

    def renew_after_progress(self) -> None:
        """在一个可信阶段完成后续期；不得从重试循环内部调用。"""
        self.deadline = time.monotonic() + self._timeout_seconds


async def _wait_for_circuit(budget: _FetchBudget) -> bool:
    async with _circuit_lock:
        delay = _circuit_blocked_until - time.monotonic()
    remaining = budget.remaining_seconds()
    if remaining <= 0:
        return False
    if delay > 0:
        await asyncio.sleep(min(delay, remaining))
    return budget.remaining_seconds() > 0


async def _record_circuit_failure():
    global _circuit_failures, _circuit_blocked_until
    async with _circuit_lock:
        _circuit_failures = min(_circuit_failures + 1, 8)
        cooldown = min(_CIRCUIT_MAX_COOLDOWN, 0.5 * (2 ** (_circuit_failures - 1)))
        _circuit_blocked_until = max(
            _circuit_blocked_until, time.monotonic() + cooldown)


async def _record_circuit_success():
    global _circuit_failures, _circuit_blocked_until
    async with _circuit_lock:
        _circuit_failures = 0
        _circuit_blocked_until = 0.0

_TRANSIENT_BLOCK_MARKERS = (
    "leastfactor(",
    "challenge-platform",
    "cf-chl-",
    "down for maintenance",
    "temporarily unavailable",
    "service unavailable",
    "too many requests",
    "verify you are human",
    "please enable cookies",
    "<title>access denied",
    "request blocked",
)

_VIDEO_UNAVAILABLE_TITLES = {
    "取消播放视频",
    "video disabled",
    "video has been removed",
    "video not found",
    "video removed",
    "video unavailable",
}


def _looks_like_block_page(text: str) -> bool:
    """识别会伪装成大页面的挑战/限流/维护响应。"""
    lowered = str(text or "").casefold()
    # 挑战 DOM 有唯一结构，可全页检查；普通错误短语只看 title/首屏，避免视频
    # 标题、评论或站点脚本里的同名文案把真实页面误判为拦截页。
    if any(marker in lowered for marker in (
            "leastfactor(", "challenge-platform", "cf-chl-")):
        return True
    title_match = re.search(r"<title[^>]*>(.*?)</title>", lowered, re.DOTALL)
    title = title_match.group(1) if title_match else ""
    title_markers = tuple(
        marker for marker in _TRANSIENT_BLOCK_MARKERS
        if marker not in {"leastfactor(", "challenge-platform", "cf-chl-"}
    )
    if any(marker.replace("<title>", "") in title for marker in title_markers):
        return True
    prefix = lowered[:2048]
    return any(marker in prefix for marker in (
        "verify you are human", "please enable cookies", "request blocked"))


GEO_BLOCKED_VIDEO_TEXT = "此页面不可用（可能限制了当前出口地区）"


def _video_unavailable_reason(html: str) -> str:
    """识别 PH 对目标视频给出的确定性下架页，不依赖响应状态码。"""
    soup = BeautifulSoup(str(html or ""), "lxml")
    removed = soup.select_one(".removed")
    if removed is not None:
        detail = " ".join(removed.stripped_strings).strip()
        return detail[:120] or "站点已将视频标记为下架"

    # 与模特主页相同的“此页面不可用”外壳（div.geoBlocked > h1）：视频限制了当前出口
    # 地区、转为私密或已删除。调用方只在没有 flashvars 时才判断，所以这是站点的明确
    # 答复而不是反爬页；以前会按拦截页退避约 3 分钟才放弃。只认结构，不认文案。
    blocked = soup.select_one("div.geoBlocked")
    if blocked is not None and blocked.select_one("h1") is not None:
        return GEO_BLOCKED_VIDEO_TEXT

    title = " ".join(soup.title.stripped_strings).strip() if soup.title else ""
    title_stem = re.sub(
        r"\s*-\s*pornhub(?:\.com)?\s*$", "", title, flags=re.IGNORECASE
    ).strip().casefold()
    og_title = soup.select_one('meta[property="og:title"]')
    og_value = str(og_title.get("content", "") if og_title else "").strip()
    if (
        title_stem in _VIDEO_UNAVAILABLE_TITLES
        or og_value.casefold() in _VIDEO_UNAVAILABLE_TITLES
    ):
        return og_value[:120] or title[:120] or "站点已将视频标记为不可播放"
    return ""


def _least_factor(n: int) -> int:
    """返回 n 的最小质因子（复现挑战页的 leastFactor）。"""
    if n <= 0:
        return n
    for f in (2, 3, 5):
        if n % f == 0:
            return f
    m = int(math.isqrt(n))
    i = 7
    while i <= m:
        for off in (0, 4, 6, 10, 12, 16, 22, 24):
            if n % (i + off) == 0:
                return i + off
        i += 30
    return n


def _solve_challenge(html: str) -> str | None:
    """解析 JS 计算挑战页，返回应设置的 KEY cookie 值；非挑战页或解析失败返回 None。"""
    m = re.search(r"function go\(\)\s*\{(.*?)n=leastFactor", html, re.DOTALL)
    if not m:
        return None
    # 剔除 /* */ 干扰注释后压缩空白，得到规整语句序列
    body = re.sub(r"/\*.*?\*/", " ", m.group(1), flags=re.DOTALL)
    body = re.sub(r"\s+", " ", body).strip()
    pm = re.search(r"var p=(\d+)", body)
    sm = re.search(r"var s=(\d+)", body)
    if not (pm and sm):
        return None
    p = int(pm.group(1))
    s = int(sm.group(1))
    # 各 if ((s>>K)&1) p±=A*B; else p±=C*D;
    for mm in re.finditer(
            r"if \(\(s >> (\d+)\) & 1\)\s*p([+-])=\s*(\d+)\*\s*(\d+);"
            r"\s*else\s*p([+-])=\s*(\d+)\*\s*(\d+);", body):
        if (s >> int(mm.group(1))) & 1:
            v = int(mm.group(3)) * int(mm.group(4))
            p += v if mm.group(2) == "+" else -v
        else:
            v = int(mm.group(6)) * int(mm.group(7))
            p += v if mm.group(5) == "+" else -v
    # 末尾独立的 p±=常数（取最后一个纯数字项）
    tail = re.findall(r"\bp([+-])=(\d+);", body)
    if not tail:
        return None
    sign, val = tail[-1]
    p += int(val) if sign == "+" else -int(val)
    # cookie 模板 ..."+s+":<const>:1"
    c3 = re.search(r'\+s\+":(\d+):1', html)
    if not c3:
        return None
    n = _least_factor(p)
    if n <= 0 or p % n != 0:
        return None
    return "{}*{}:{}:{}:1".format(n, p // n, s, c3.group(1))


async def _fetch_ok(client: httpx.AsyncClient, url: str, *, page_validator=None,
                    budget: _FetchBudget | None = None):
    """请求 url 并自动穿透反爬，返回放行后的真实内容响应；始终无法放行返回 None。

    同一 client 的 cookie jar 在整个抓取会话内复用，故一次穿透后续翻页无需重复求解。
    ``page_validator`` 用于视频页等需要正向语义证明的响应，避免把同域登录页或
    未知大体积拦截页误认成目标页面。
    """
    active_budget = budget or _FetchBudget()
    backoff = 2.0
    while active_budget.remaining_seconds() > 0:
        if not await _wait_for_circuit(active_budget):
            return None
        try:
            r = await asyncio.wait_for(
                client.get(url), timeout=max(0.001, active_budget.remaining_seconds()))
        except (httpx.TransportError, httpx.HTTPError):
            await _record_circuit_failure()
            if not active_budget.take_retry():
                return None
            await asyncio.sleep(min(backoff, active_budget.remaining_seconds()))
            backoff = min(backoff * 1.6, 20.0)
            continue
        except asyncio.TimeoutError:
            await _record_circuit_failure()
            return None
        text = str(getattr(r, "text", "") or "")
        # 挑战响应可能是 200，也可能是大体积 403；必须先求解，不能先按长度放行。
        key = _solve_challenge(text)
        if key:
            client.cookies.set("KEY", key, domain=".pornhub.com")
            await _record_circuit_failure()
            if not active_budget.take_retry():
                return None
            await asyncio.sleep(min(0.3, active_budget.remaining_seconds()))
            continue
        try:
            status_code = int(getattr(r, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status_code = 0
        # 404/410 是服务器对目标不存在的确定答复，交给调用方按页面语义处理。
        if status_code in {404, 410}:
            await _record_circuit_success()
            return r
        if status_code == 200 and len(text) >= _REAL_PAGE_MIN_BYTES:
            # 页面专属的正向语义证据强于泛化文本；例如真实视频标题可能恰好
            # 包含“too many requests”，但可解析且 viewkey 一致的 flashvars 仍是真页。
            if page_validator is not None:
                if inspect.iscoroutinefunction(page_validator):
                    validated = await page_validator(r)
                else:
                    validated = await asyncio.to_thread(page_validator, r)
                    if inspect.isawaitable(validated):
                        validated = await validated
                if bool(validated):
                    await _record_circuit_success()
                    return r
            elif not _looks_like_block_page(text):
                await _record_circuit_success()
                return r
        # 限流/维护页：指数退避（上限 20s）
        await _record_circuit_failure()
        if not active_budget.take_retry():
            return None
        await asyncio.sleep(min(backoff, active_budget.remaining_seconds()))
        backoff = min(backoff * 1.6, 20.0)
    return None


async def _fetch_with_budget(client, url: str, *, page_validator=None,
                             budget: _FetchBudget):
    """调用可注入/旧测试替身兼容的 fetch，同时让生产实现共享预算。"""
    try:
        parameters = inspect.signature(_fetch_ok).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs = {"page_validator": page_validator} if page_validator is not None else {}
    if "budget" in parameters:
        kwargs["budget"] = budget
    return await _fetch_ok(client, url, **kwargs)


def _viewkey_from_url(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    if "viewkey" in q:
        return q["viewkey"][0]
    m = re.search(r"viewkey=([a-z0-9]+)", url, re.IGNORECASE)
    return m.group(1) if m else ""


def _duration_to_seconds(text: str) -> int:
    """'12:34' / '1:02:03' -> 秒。"""
    parts = [p for p in text.strip().split(":") if p.isdigit()]
    if not parts:
        return 0
    parts = [int(p) for p in parts]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _model_unavailable_reason(html) -> str:
    """识别 PH 用 HTTP 200 返回的“主页不可用”外壳。

    中文站的标题文本可能因响应编码变成乱码，因此只依赖稳定的结构标记，
    不按本地化文案猜测。该外壳仍含顶部推荐视频，不能被当成本人视频页。
    """
    soup = html if hasattr(html, "select_one") else BeautifulSoup(str(html or ""), "lxml")
    panel = soup.select_one("div.geoBlocked")
    if panel is not None and panel.select_one("h1") is not None:
        return "主页不可用或已删除"
    return ""


# PH 把已删除账号的主页 301 到通用模特列表；这是源站的明确答复，不是限流。
_REMOVED_PROFILE_LANDING_PATHS = frozenset({"/pornstars", "/models"})


def _redirected_to_profile_directory(response) -> bool:
    """主页请求经跳转落到通用模特列表，说明目标账号已被删除。"""
    if not getattr(response, "history", None):
        return False
    try:
        path = unquote(urlparse(str(getattr(response, "url", ""))).path)
    except ValueError:
        return False
    return path.rstrip("/").casefold() in _REMOVED_PROFILE_LANDING_PATHS


def _profile_has_no_videos(soup) -> bool:
    """本人视频区只有站点的“没有视频……”空状态（model 与 pornstar 标记不同）。"""
    panel = soup.select_one("div.profileVids")
    if panel is None or panel.select_one("li.pcVideoListItem") is not None:
        return False
    return panel.select_one("div.emptyBoxWrapper, p.empty") is not None


def _listing_past_last_page(soup, container_selector: str, page: int) -> bool:
    """PH 对 pornstar 上传列表的越界页返回 200 + 空主体容器，而不是 404。

    只有主体容器本身存在且为空、并且分页控件列出的最大页码小于当前页时，才算列表到底；
    挑战壳或 DOM 漂移连容器都没有，仍按异常处理，不发布部分快照。
    """
    container = soup.select_one(container_selector)
    if container is None or container.select_one("li.pcVideoListItem") is not None:
        return False
    pages = [
        int(text) for text in (
            node.get_text(strip=True)
            for node in soup.select(
                "div.pagination3 li.page_number a, div.pagination3 li.page_current")
        )
        if text.isdigit()
    ]
    return bool(pages) and max(pages) < page


# ---------- 榜单页 ----------
async def scrape_ranking(url: str, max_models: int = 100) -> list[dict]:
    """爬榜单页，返回模特列表 [{ph_id, name, url, thumbnail}]。

    支持分页（?page=N）。url 例: https://cn.pornhub.com/pornstars?gender=female
    若 URL 未指定 gender，则默认补上 gender=female（只爬女性模特）；
    若调用方显式带了 gender，则尊重其取值，不覆盖。
    """
    url = _ensure_female(validate_ph_url(url))
    is_discover = "/user/discover/" in url   # discover 页是"视频流"，需提取去重上传者
    models: list[dict] = []
    seen: set[str] = set()
    page = 1
    empty_pages = 0   # 连续"有卡片但无新增"的页数，用于在站点回环时安全终止
    budget = _FetchBudget()
    async with _client() as c:
        while len(models) < max_models:
            page_url = _with_page(url, page)
            r = await _fetch_with_budget(
                c, page_url, budget=budget)     # 自动穿透反爬（JS挑战/限流退避）
            if r is None:
                break                            # 多次重试仍被拦截，安全终止
            found, page_models = await asyncio.to_thread(
                _parse_ranking_page, r.text, is_discover, seen, max_models - len(models))
            models.extend(page_models)
            seen.update(info["ph_id"] for info in page_models)
            if found is None:
                break   # 本页无任何卡片 = 真到底了
            # 注意：found==0（本页有卡片但全是已见过的）不终止，继续翻下一页；
            # 仅当某页解析不出任何卡片时才停。但要防止无限翻页（站点对超范围页码
            # 可能一直回同一页），故对"连续多页无新增"设上限。
            if found == 0:
                empty_pages += 1
                if empty_pages >= 2:
                    break
            else:
                empty_pages = 0
            if len(models) >= max_models:
                break
            page += 1
            await asyncio.sleep(min(1.0, budget.remaining_seconds()))
    # 附加爬取顺序：rank 缺失时作兜底排序键
    for i, info in enumerate(models):
        info["scrape_order"] = i
    return models


def _parse_ranking_page(html: str, is_discover: bool, seen: set[str],
                        remaining: int) -> tuple[int | None, list[dict]]:
    """在线程内完成整页解析，局部去重集合避免修改调用方状态。"""
    soup = BeautifulSoup(html, "lxml")
    models: list[dict] = []
    page_seen = set(seen)
    collector = _collect_discover if is_discover else _collect_pornstars
    found = collector(soup, models, page_seen, remaining)
    return found, models


def _collect_pornstars(soup, models: list, seen: set, max_models: int) -> int | None:
    """解析标准 pornstars/models 榜单页的模特卡片。返回本页新增数；无卡片返回 None。"""
    # 选择器必须按可信度逐级回退，不能用逗号并集。页面侧栏/推荐区也可能位于
    # ul.pornstarsSort；旧并集会把男性推荐卡混进带 gender=female 的主榜。
    cards = soup.select("ul.pornstarsList li.pornstarLi, ul.performerList li.pornstarLi")
    if not cards:
        cards = soup.select("li.pornstarLi")
    if not cards:
        cards = soup.select(".performerCard")
    if not cards:
        cards = soup.select("ul.pornstarsSort li")
    if not cards:
        # 退化选择器：找指向 /pornstar/ 或 /model/ 的链接
        cards = soup.select("a[href*='/pornstar/'], a[href*='/model/']")
    if not cards:
        return None
    found = 0
    for card in cards:
        info = _parse_model_card(card)
        if info and info["ph_id"] not in seen:
            seen.add(info["ph_id"])
            models.append(info)
            found += 1
            if len(models) >= max_models:
                break
    return found


def _collect_discover(soup, models: list, seen: set, max_models: int) -> int | None:
    """解析 discover 视频流页：提取去重的模特上传者（/model/、/pornstar/）。

    discover 页（most_subscribed_models 等）本质是按某指标排序的视频列表，每个视频
    带上传者链接。这里提取所有指向模特详情页的上传者并去重；排除 /users/、/channels/
    （非模特）以及纯数字/异常文本。注意：这些页不支持 gender 过滤，结果会含男性。
    返回本页新增数；无任何上传者链接返回 None。
    """
    anchors = soup.select("a[href*='/model/'], a[href*='/pornstar/']")
    if not anchors:
        return None
    found = 0
    for a in anchors:
        href = a.get("href", "")
        path = urlparse(urljoin(settings.ph_base_url, href)).path.rstrip("/")
        # 只要模特详情页本身，排除 /model/x/videos 这类子路径
        if not re.match(r"^/(model|pornstar)/[^/]+$", path):
            continue
        ph_id = path.split("/")[-1]
        if ph_id in seen:
            continue
        name = a.get_text(strip=True)
        # 过滤：空 / 纯数字（视频数等标签）/ 过短 / 过长
        if not name or name.isdigit() or len(name) < 2 or len(name) > 40:
            continue
        # 头像：discover 页缩略图是视频图而非模特头像，留空（同步时不依赖头像）
        seen.add(ph_id)
        models.append({
            "ph_id": ph_id, "name": name,
            "url": urljoin(settings.ph_base_url, path), "thumbnail": "",
        })
        found += 1
        if len(models) >= max_models:
            break
    return found


def _with_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page}"


def _ensure_female(url: str) -> str:
    """确保榜单 URL 带 gender=female（只爬女性模特）。

    URL 已显式包含 gender 参数时尊重原值不覆盖；否则补 gender=female。
    """
    parts = urlparse(url)
    q = parse_qs(parts.query)
    if "gender" in q:
        return url
    sep = "&" if parts.query else "?"
    return f"{url}{sep}gender=female"


def _parse_model_card(card) -> dict | None:
    a = card if card.name == "a" else card.select_one("a[href*='/pornstar/'], a[href*='/model/']")
    if not a or not a.get("href"):
        return None
    href = urljoin(settings.ph_base_url, a["href"])
    path = urlparse(href).path.rstrip("/")
    ph_id = path.split("/")[-1]
    if not ph_id:
        return None
    # 名字：优先 .pornStarName / .performerCardName。注意里面会嵌套 .lastName + 徽章 span，
    # 必须在取文本前把徽章节点剔掉，并用空格 join 才能拿到 "Sara Jay" 而不是 "SaraJay"
    name = ""
    name_el = (card.select_one(".pornStarName, .performerCardName, .performerName")
               or card.select_one(".title"))
    if name_el:
        # 复制一份，移除徽章/订阅按钮等装饰
        from copy import copy
        node = copy(name_el)
        for junk in node.select(".modelBadges, .performerBadges, .verifiedPornstar, "
                                ".performerIcon, .modelIcon, .subscribe-to-pornstar-icon, "
                                ".rank_number, .icon, .videosNumber, .viewsNumber, .pornstar_label"):
            junk.decompose()
        name = " ".join(node.stripped_strings).strip()
    if not name:
        # 兜底用 img alt（已经是 "Sara Jay" 这种带空格形式）
        img = card.select_one("img")
        if img and img.get("alt"):
            name = img["alt"].strip()
    if not name:
        name = a.get("title", "").strip() or ph_id
    # 头像：PH 懒加载，属性可能是 data-thumb_url / data-image / data-src / src
    thumb = ""
    img = card.select_one("img")
    if img:
        thumb = (img.get("data-thumb_url") or img.get("data-image")
                 or img.get("data-src") or img.get("data-mediumthumb")
                 or img.get("src") or "")
    # 榜单名次：.rank_number 内是数字（如 "5"），非连续（被过滤项会留空档）
    rank = 0
    rank_el = card.select_one(".rank_number")
    if rank_el:
        digits = re.sub(r"\D", "", rank_el.get_text())
        rank = int(digits) if digits else 0
    # 视频数："769 视频" -> 769
    ph_video_count = 0
    vn = card.select_one(".videosNumber")
    if vn:
        digits = re.sub(r"\D", "", vn.get_text())
        ph_video_count = int(digits) if digits else 0
    # 观看数：保留数值部分，如 "2.2B 次观看" -> "2.2B"
    views_text = ""
    vw = card.select_one(".viewsNumber")
    if vw:
        raw = vw.get_text(strip=True)
        mv = re.match(r"([\d.]+\s*[KMBkmb]?)", raw)
        views_text = (mv.group(1).strip() if mv else raw)
    return {"ph_id": ph_id, "name": name, "url": href.split("?")[0], "thumbnail": thumb,
            "rank": rank, "ph_video_count": ph_video_count, "views_text": views_text}


# ---------- 视频发现页 ----------
_DISCOVERY_SOURCE_PATHS = (
    ("home", "主页", "/", "videos"),
    ("recommended", "推荐视频", "/recommended", "videos"),
    ("hot", "最热门", "/video?o=ht", "videos"),
    ("most_viewed", "最多次观看", "/video?o=mv", "videos"),
    ("top_rated", "最高分", "/video?o=tr", "videos"),
    ("homemade", "热门自制", "/video?p=homemade&o=tr", "videos"),
    ("shorties", "短片", "/shorties", "shorties"),
    ("playlists", "片单", "/playlists", "playlists"),
    ("channels", "频道", "/channels", "channels"),
    ("random", "随机", "/video/random", "random"),
    ("latest", "最新", "/video?o=cm", "videos"),
    ("viewers_choice", "观众之选", "/contest_hub", "viewers_choice"),
)


def discovery_source_catalog() -> list[dict]:
    """返回前后端共用的受控来源列表，避免客户端提交任意抓取 URL。"""
    base = settings.ph_base_url.rstrip("/") + "/"
    return [{
        "key": key,
        "label": label,
        "url": urljoin(base, path.lstrip("/")),
        "parser": parser,
    } for key, label, path, parser in _DISCOVERY_SOURCE_PATHS]


def _creator_identity_from_url(url: str) -> dict | None:
    """把四类可同步主页规范化为稳定身份。"""
    try:
        candidate = validate_ph_url(urljoin(settings.ph_base_url, str(url or "")))
        path = unquote(urlparse(candidate).path).rstrip("/")
    except InvalidScrapeURL:
        return None
    match = re.fullmatch(
        r"/(model|pornstar|channels|users)/([^/]+)", path, re.IGNORECASE)
    if not match:
        return None
    raw_type = match.group(1).casefold()
    profile_type = {"channels": "channel", "users": "user"}.get(raw_type, raw_type)
    slug = match.group(2).casefold()
    storage_id = slug if profile_type in {"model", "pornstar"} else f"{profile_type}:{slug}"
    return {
        "profile_type": profile_type,
        "profile_slug": slug,
        "ph_id": storage_id,
        "url": urljoin(settings.ph_base_url, path),
    }


def _creator_from_anchor(anchor) -> dict | None:
    if not anchor or not anchor.get("href"):
        return None
    identity = _creator_identity_from_url(anchor["href"])
    if not identity:
        return None
    name = " ".join(anchor.stripped_strings).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = str(anchor.get("title") or "").strip()
    if not name:
        name = identity["profile_slug"]
    if len(name) > 120:
        return None
    return {**identity, "name": name}


def _creator_from_video_card(card) -> dict | None:
    selectors = (
        ".videoUploaderBlock .usernameWrap a[href]",
        ".videoUploaderBlock a[href]",
        ".usernameBadgesWrapper a[href]",
        "a.usernameLink[href]",
        ".usernameWrap a[href]",
    )
    for selector in selectors:
        for anchor in card.select(selector):
            creator = _creator_from_anchor(anchor)
            if creator:
                return creator
    # DOM 小改版时仍只接受精确的四类主页链接，不读取推荐区外部节点。
    for anchor in card.select(
            "a[href*='/model/'], a[href*='/pornstar/'], "
            "a[href*='/channels/'], a[href*='/users/']"):
        creator = _creator_from_anchor(anchor)
        if creator:
            return creator
    return None


def _safe_discovery_thumbnail(value: str, base_url: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = urljoin(base_url, raw)
    try:
        return validate_media_url(candidate)
    except InvalidScrapeURL:
        return ""


def _main_video_cards(soup, source: str) -> list:
    if source == "viewers_choice":
        return soup.select(
            ".contestHub--viewersChoice li.pcVideoListItem, "
            ".viewersChoice--monthContest li.pcVideoListItem")
    cards = soup.select("ul#videoCategory li.pcVideoListItem")
    if cards:
        return cards
    # 主页由多个主体视频区组成；明确排除顶部下拉菜单里的四张推荐卡。
    return [card for card in soup.select("li.pcVideoListItem")
            if not card.find_parent(class_="headerSubMenu")]


def _parse_discovery_video_card(card, base_url: str) -> dict | None:
    video = _parse_video_item(card)
    creator = _creator_from_video_card(card)
    if not video or not creator:
        return None
    video["thumbnail"] = _safe_discovery_thumbnail(video.get("thumbnail", ""), base_url)
    return {"creator": creator, **video}


def _parse_playlist_cards(soup, base_url: str) -> list[dict]:
    items = []
    for card in soup.select("li.full-width[id^='playlist_']"):
        creator = None
        for anchor in card.select(
                "a.usernameLink[href], a.userLink[href], "
                "a[href*='/users/'], a[href*='/model/'], "
                "a[href*='/pornstar/'], a[href*='/channels/']"):
            creator = _creator_from_anchor(anchor)
            if creator:
                break
        if not creator:
            continue
        play = card.select_one("a[href*='viewkey=']")
        playlist = card.select_one("a.viewPlaylistLink[href], a[href*='/playlist/']")
        viewkey = _viewkey_from_url(urljoin(base_url, play.get("href", ""))) if play else ""
        title_node = card.select_one("a.playAllLink.title, .title a, span.title")
        title = " ".join(title_node.stripped_strings).strip() if title_node else viewkey
        image = card.select_one("img")
        thumb = ""
        if image:
            thumb = (image.get("data-src") or image.get("data-thumb_url")
                     or image.get("data-image") or image.get("src") or "")
        items.append({
            "creator": creator,
            "viewkey": viewkey,
            "title": title or viewkey,
            "url": urljoin(base_url, (playlist or play).get("href", ""))
            if (playlist or play) else "",
            "duration": 0,
            "thumbnail": _safe_discovery_thumbnail(thumb, base_url),
        })
    return items


def _parse_channel_cards(soup, base_url: str) -> list[dict]:
    items = []
    for card in soup.select("ul#allChannelsSection li.mainChannels"):
        creator = None
        anchors = card.select("a.usernameLink[href]") or card.select("a[href*='/channels/']")
        for anchor in anchors:
            creator = _creator_from_anchor(anchor)
            if creator and creator["profile_type"] == "channel":
                break
        if not creator or creator["profile_type"] != "channel":
            continue
        # 频道目录只提供头像，不冒充视频封面；完成自动同步后由 API 用首批作品补图。
        items.append({
            "creator": creator,
            "viewkey": "",
            "title": "",
            "url": creator["url"],
            "duration": 0,
            "thumbnail": "",
        })
    return items


def _shorties_thumbnail(item: dict, base_url: str) -> str:
    for key in ("largePreviewUrl", "imageUrl"):
        if item.get(key):
            return _safe_discovery_thumbnail(item[key], base_url)
    thumbs = item.get("thumbs")
    values = list(thumbs.values()) if isinstance(thumbs, dict) else thumbs
    if isinstance(values, (list, tuple)):
        for value in values:
            if isinstance(value, str):
                candidate = _safe_discovery_thumbnail(value, base_url)
                if candidate:
                    return candidate
    return ""


def _parse_shorties(html: str, base_url: str, *, soup=None) -> list[dict]:
    if soup is None:
        soup = BeautifulSoup(html, "lxml")
    marker = "JSON_SHORTIES = insertAfterNthPosition("
    script = next((node.get_text() for node in soup.find_all("script")
                   if marker in node.get_text()), "")
    start = script.find(marker)
    if start < 0:
        raise ScrapeError("短片页缺少 JSON_SHORTIES 数据，PH 页面结构可能已变化")
    try:
        # raw_decode 不会主动跳过前导空白；兼容脚本格式化后在左括号后换行的情况。
        payload = script[start + len(marker):].lstrip()
        raw, _ = json.JSONDecoder().raw_decode(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ScrapeError("短片页内嵌数据无法解析") from exc
    items = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        creator = _creator_identity_from_url(entry.get("profileUrl", ""))
        if not creator:
            continue
        creator["name"] = str(entry.get("name") or creator["profile_slug"]).strip()
        viewkey = str(entry.get("vkey") or "").strip()
        if not viewkey:
            continue
        items.append({
            "creator": creator,
            "viewkey": viewkey,
            "title": str(entry.get("videoTitle") or viewkey).strip(),
            "url": f"{settings.ph_base_url}/view_video.php?viewkey={viewkey}",
            "duration": 0,
            "thumbnail": _shorties_thumbnail(entry, base_url),
        })
    return items


def _parse_random_video(response, *, soup=None) -> list[dict]:
    final_url = str(response.url)
    if soup is None:
        soup = BeautifulSoup(response.text, "lxml")
    creator = None
    for anchor in soup.select(".userInfo a[href]"):
        creator = _creator_from_anchor(anchor)
        if creator:
            break
    flashvars = _extract_flashvars(response.text)
    viewkey = _viewkey_from_url(final_url)
    if not creator or not flashvars or not viewkey:
        raise ScrapeError("随机视频页缺少可验证的上传者或视频数据")
    return [{
        "creator": creator,
        "viewkey": viewkey,
        "title": str(flashvars.get("video_title") or viewkey).strip(),
        "url": f"{settings.ph_base_url}/view_video.php?viewkey={viewkey}",
        "duration": _display_duration(flashvars.get("video_duration", 0)),
        "thumbnail": _safe_discovery_thumbnail(
            str(flashvars.get("image_url") or ""), final_url),
    }]


def _parse_discovery_page(response, source: str, parser: str) -> tuple[list[dict], str | None]:
    """复用同一 DOM 完成来源解析和首页翻页链接提取，不执行网络请求。"""
    soup = BeautifulSoup(response.text, "lxml")
    next_home_url = None
    if source == "home":
        # 主页首屏的后续内容实际跳到 /video?page=2，而不是 /?page=2。
        # 跟随页面权威 rel=next，避免硬编码随站点改版失效的分页地址。
        next_link = soup.select_one(
            "link[rel~='next'][href], a[rel~='next'][href]")
        if next_link:
            try:
                next_home_url = validate_ph_url(urljoin(
                    str(response.url), next_link.get("href", "")))
            except InvalidScrapeURL:
                next_home_url = None
    if parser == "shorties":
        page_items = _parse_shorties(response.text, str(response.url), soup=soup)
    elif parser == "playlists":
        page_items = _parse_playlist_cards(soup, str(response.url))
    elif parser == "channels":
        page_items = _parse_channel_cards(soup, str(response.url))
    elif parser == "random":
        page_items = _parse_random_video(response, soup=soup)
    else:
        cards = _main_video_cards(soup, source)
        page_items = [parsed for card in cards
                      if (parsed := _parse_discovery_video_card(card, str(response.url)))]
    return page_items, next_home_url


async def scrape_discovery(source: str, max_creators: int = 50) -> dict:
    """抓取受控视频来源，返回按源顺序排列的上传者与代表视频。"""
    definitions = {item["key"]: item for item in discovery_source_catalog()}
    definition = definitions.get(str(source or ""))
    if definition is None:
        raise InvalidScrapeURL("未知的视频发现来源")
    max_creators = max(1, min(int(max_creators), 500))
    parser = definition["parser"]
    base_url = definition["url"]
    collected: list[dict] = []
    creator_ids: set[str] = set()
    no_new_pages = 0
    page_count = 0
    paginated = parser in {"videos", "playlists", "channels"}
    page_url = base_url
    visited_pages: set[str] = set()

    client_options = (
        {"personalized_recommendations": True}
        if source == "recommended" else {}
    )
    budget = _FetchBudget()
    async with _client(**client_options) as client:
        page = 1
        while True:
            if page_url in visited_pages:
                break
            visited_pages.add(page_url)
            response = await _fetch_with_budget(client, page_url, budget=budget)
            if response is None:
                raise ScrapeFetchError(f"视频来源暂时无法访问：{page_url}")
            page_count += 1
            page_items, next_home_url = await asyncio.to_thread(
                _parse_discovery_page, response, source, parser)

            if not page_items:
                if parser == "viewers_choice":
                    break  # 当前无进行中的观众之选是正常空状态。
                if page == 1:
                    raise ScrapeError(
                        f"{definition['label']}未解析到上传者，PH 页面结构可能已变化")
                break

            remaining = max_creators - len(creator_ids)
            if remaining <= 0:
                break
            new_ids = []
            new_id_set = set()
            for item in page_items:
                identity = item["creator"]["ph_id"]
                if identity not in creator_ids and identity not in new_id_set:
                    new_id_set.add(identity)
                    new_ids.append(identity)
            allowed_new = set(new_ids[:remaining])
            accepted = [item for item in page_items
                        if item["creator"]["ph_id"] in creator_ids
                        or item["creator"]["ph_id"] in allowed_new]
            collected.extend(accepted)
            creator_ids.update(allowed_new)
            no_new_pages = 0 if allowed_new else no_new_pages + 1
            if (len(creator_ids) >= max_creators or not paginated
                    or no_new_pages >= 2):
                break
            page += 1
            if page > 50:
                break
            if source == "home":
                if not next_home_url:
                    break
                page_url = next_home_url
            else:
                page_url = _with_page(base_url, page)
            # 视频发现最多会读取 50 个大页面；500 位上传者在正常网络下也可能
            # 超过单模特同步使用的 180 秒墙钟预算。当前页已经成功解析，允许
            # 下一页获得完整的无进展窗口，同时保留全程有限的共享重试次数。
            budget.renew_after_progress()
            await asyncio.sleep(min(1.0, budget.remaining_seconds()))

    for order, item in enumerate(collected):
        item["source_order"] = order
    return {
        "source": source,
        "label": definition["label"],
        "url": definition["url"],
        "items": collected,
        "creator_count": len(creator_ids),
        "requested_max": max_creators,
        "reached_limit": len(creator_ids) >= max_creators,
        "page_count": page_count,
    }


def validate_trusted_source_url(url: str) -> str:
    """只允许用户维护的PH上传者/发布者主页，拒绝任意榜单和视频地址。"""
    validated = validate_ph_url(url)
    path = urlparse(validated).path.rstrip("/")
    if not re.match(r"^/(?:channels|users|model|pornstar)/[^/]+(?:/videos)?$", path, re.I):
        raise InvalidScrapeURL(
            "可信来源只允许PH频道、用户、工作室或模特主页（可带 /videos）")
    return validated


async def scrape_trusted_source_url(
    url: str, *, source: str = "custom_trusted", max_creators: int = 50,
) -> dict:
    """读取一个受信PH发布来源的公开视频卡；只返回来源命中，不推断表演者性别。"""
    source_url = validate_trusted_source_url(url)
    max_creators = max(1, min(int(max_creators), 100))
    collected: list[dict] = []
    creator_ids: set[str] = set()
    async with _client() as client:
        response = await _fetch_ok(client, source_url)
        if response is None:
            raise ScrapeFetchError(f"可信PH来源暂时无法访问：{source_url}")
        page_items, _ = await asyncio.to_thread(
            _parse_discovery_page, response, "videos", "videos")
        for item in page_items:
            identity = str(item["creator"]["ph_id"])
            if identity not in creator_ids and len(creator_ids) >= max_creators:
                continue
            creator_ids.add(identity)
            collected.append(item)
    for order, item in enumerate(collected):
        item["source_order"] = order
    return {
        "source": source,
        "label": "自定义可信PH发布来源",
        "url": source_url,
        "items": collected,
        "creator_count": len(creator_ids),
        "requested_max": max_creators,
        "reached_limit": len(creator_ids) >= max_creators,
        "page_count": 1,
    }


# ---------- 上传者视频列表 ----------
def _model_slug_from_url(model_url: str) -> str:
    """从模特详情页 URL 提取归属基准 slug，如 'natasha-nice'（小写）。

    这是判定"视频是否由该模特本人上传"的唯一权威标识：卡片里上传者链接的 slug
    与之相等才算本人上传。只比较末段 slug、忽略 /pornstar 与 /model 前缀差异——
    同一表演者在详情页与卡片徽章里可能一处是 /pornstar/x、另一处是 /model/x，
    若带前缀全等比较会把本人视频误判成"别人"而整体漏抓。
    """
    path = urlparse(model_url).path.rstrip("/").lower()
    m = re.match(r"^/(?:pornstar|model)/([^/]+)", path)
    return m.group(1) if m else path.rsplit("/", 1)[-1]


def _uploader_slug(it) -> str | None:
    """提取视频卡片上传者的 slug（小写）；无法解析返回 None。

    PH 卡片里上传者链接位于用户名徽章区，指向 /pornstar/、/model/、/channels/、/users/。
    只有指向 /pornstar/ 或 /model/ 的才可能是"本人"，/channels、/users 一定不是本人上传。
    返回 slug（末段），供归属白名单比较。
    """
    a = it.select_one(
        ".usernameBadgesWrapper a[href], .videoUploaderBlock a[href], "
        "a.usernameLink[href], .usernameWrap a[href]"
    )
    if not a or not a.get("href"):
        return None
    path = urlparse(urljoin(settings.ph_base_url, a["href"])).path.rstrip("/").lower()
    m = re.match(r"^/(?:pornstar|model)/([^/]+)", path)
    if m:
        return m.group(1)
    # /channels/... /users/... 等：明确不是本人上传，返回特殊标记以便调用方拒绝
    return "" if path else None


async def _scrape_nonperformer_videos(
    profile_type: str,
    profile_slug: str,
    max_videos: int,
) -> list[dict]:
    """从频道/普通用户的权威作品容器抓取上传视频。"""
    if profile_type == "channel":
        videos_url = f"{settings.ph_base_url.rstrip('/')}/channels/{profile_slug}/videos"
        selectors = ("ul#showAllChanelVideos li.pcVideoListItem",)
        container_selectors = ("ul#showAllChanelVideos",)
        expected_segment = "channels"
    elif profile_type == "user":
        videos_url = f"{settings.ph_base_url.rstrip('/')}/users/{profile_slug}/videos"
        selectors = (
            "div.profileVids ul#mostRecentVideosSection li.pcVideoListItem",
            "div.profileVids ul#uploadedVideosSection li.pcVideoListItem",
            "div.container.user-videos_show-page ul.videos.full-row-thumbs li.pcVideoListItem",
        )
        container_selectors = (
            "div.profileVids ul#mostRecentVideosSection",
            "div.profileVids ul#uploadedVideosSection",
            "div.container.user-videos_show-page ul.videos.full-row-thumbs",
        )
        expected_segment = "users"
    else:
        raise InvalidScrapeURL("仅频道或普通用户可使用此抓取路径")

    videos: list[dict] = []
    seen: set[str] = set()
    empty_pages = 0
    first_page_soup = None
    budget = _FetchBudget()

    def first_page_validator(response) -> bool:
        nonlocal first_page_soup
        soup = BeautifulSoup(response.text, "lxml")
        unavailable_reason = _model_unavailable_reason(soup)
        if unavailable_reason:
            raise ModelProfileUnavailable(unavailable_reason)
        try:
            final_path = unquote(urlparse(
                validate_ph_url(str(getattr(response, "url", videos_url)))
            ).path).rstrip("/")
        except InvalidScrapeURL:
            return False
        valid = final_path.casefold() == f"/{expected_segment}/{profile_slug}/videos".casefold()
        if valid:
            first_page_soup = soup
        return valid

    async with _client() as client:
        page = 1
        while len(videos) < max_videos:
            page_url = _with_page(videos_url, page)
            response = await _fetch_with_budget(
                client,
                page_url,
                page_validator=first_page_validator if page == 1 else None,
                budget=budget,
            )
            if response is None:
                if page == 1:
                    raise ScrapeFetchError(f"上传者作品页暂时不可访问：{videos_url}")
                raise ScrapeFetchError(
                    f"上传者作品第 {page} 页未在统一重试/时间预算内完成；"
                    "已拒绝返回部分列表")
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in {404, 410}:
                if page == 1:
                    raise ModelProfileUnavailable("上传者主页不存在或已删除")
                break
            soup = first_page_soup if page == 1 and first_page_soup is not None \
                else await asyncio.to_thread(BeautifulSoup, response.text, "lxml")
            first_page_soup = None
            cards = []
            for selector in selectors:
                cards.extend(soup.select(selector))
            if not cards:
                # 频道/普通用户可以合法地没有上传作品；不得退化到全页推荐卡。
                # 但第2+页若连已验证的空主体容器都不存在，是未知200壳/DOM漂移，
                # 不能把前几页当完整快照发布。
                if page > 1 and not any(
                        soup.select_one(selector) is not None
                        for selector in container_selectors):
                    raise ScrapeFetchError(
                        f"上传者作品第 {page} 页缺少权威主体容器；"
                        "已拒绝返回部分列表")
                break
            found = 0
            for card in cards:
                video = _parse_video_item(card)
                if not video or video["viewkey"] in seen:
                    continue
                seen.add(video["viewkey"])
                videos.append(video)
                found += 1
                if len(videos) >= max_videos:
                    break
            empty_pages = 0 if found else empty_pages + 1
            if empty_pages >= 2:
                break
            if len(videos) >= max_videos:
                break
            page += 1
            await asyncio.sleep(min(1.0, budget.remaining_seconds()))
    return videos


async def scrape_model_videos(
    model_url: str,
    max_videos: int = 1000,
    *,
    include_playlist: bool = True,
) -> list[dict]:
    """爬上传者【本人上传】的视频，返回 [{viewkey, title, url, duration, thumbnail}]。

    归属规则按页面类型区分，来自对 PH 现行 DOM 的实测：
    - /model/{slug}/videos：主体列表是 `div.profileVids #mostRecentVideosSection`，就是该
      model 自己上传的视频；卡片通常隐藏上传者链接（_uploader_slug 为 None），这是正常现象。
    - /pornstar/{slug}/videos：`#mostRecentVideosSection` 是参演/频道/其他模特混合列表，
      不能作为本人上传来源；本人上传应取 /videos/upload 页的 `div.profileVids #moreData`。
    - 页面顶部 dropdown（#hottestMenuSection/#recommMenuSection）永远不是主体列表，必须排除。

    因此这里永远只从 `div.profileVids` 下的指定容器取卡片，不再使用全页
    `li.pcVideoListItem` 兜底，避免再次抓进推荐位污染。
    """
    model_url = validate_ph_url(model_url)
    identity = _creator_identity_from_url(model_url)
    if not identity:
        raise InvalidScrapeURL(
            "上传者主页必须精确指向 /model/、/pornstar/、/channels/ 或 /users/")
    if identity["profile_type"] in {"channel", "user"}:
        return await _scrape_nonperformer_videos(
            identity["profile_type"],
            identity["profile_slug"],
            max_videos,
        )
    videos: list[dict] = []
    seen: set[str] = set()
    owner_slug = _model_slug_from_url(model_url)
    parts = urlparse(model_url)
    base_path = re.sub(r"/videos(/upload)?$", "", parts.path.rstrip("/"))
    is_pornstar = base_path.lower().startswith("/pornstar/")

    if is_pornstar:
        # pornstar 的 /videos 是参演混合列表；必须进 upload 页，主体容器为 #moreData。
        videos_url = urljoin(settings.ph_base_url, base_path) + "/videos/upload"
        container_selector = "div.profileVids ul#moreData"
        allow_hidden_uploader = False
    else:
        # model/amateur 的 /videos 与 /videos/upload 主体一致，#mostRecentVideosSection 即本人上传列表；
        # 上传者链接通常隐藏，所以允许 None，但仍只限 div.profileVids 主体区。
        videos_url = urljoin(settings.ph_base_url, base_path) + "/videos"
        container_selector = "div.profileVids ul#mostRecentVideosSection"
        allow_hidden_uploader = True
    scope_selector = f"{container_selector} li.pcVideoListItem"

    page = 1
    empty_pages = 0   # 连续"有页面但无本人新增"的页数，防止站点页码回环导致死循环
    total_cards = 0
    owner_hits = 0
    hidden_uploader_hits = 0
    first_page_scopes = None
    budget = _FetchBudget()

    def first_page_validator(response) -> bool:
        """只接受可证明属于目标账号的本人上传页，拒绝大体积软封锁页。"""
        nonlocal first_page_scopes
        soup = BeautifulSoup(response.text, "lxml")
        unavailable_reason = _model_unavailable_reason(soup)
        if unavailable_reason:
            raise ModelProfileUnavailable(unavailable_reason)
        try:
            final_url = validate_ph_url(str(getattr(response, "url", videos_url)))
            final_path = unquote(urlparse(final_url).path).rstrip("/")
        except InvalidScrapeURL:
            return False
        match = re.fullmatch(
            r"/(?:model|pornstar)/([^/]+)/videos(?:/upload)?",
            final_path,
            re.IGNORECASE,
        )
        if not match or match.group(1).casefold() != owner_slug:
            if _redirected_to_profile_directory(response):
                raise ModelProfileUnavailable("主页已删除（PH 跳转到模特列表）")
            return False
        scopes = soup.select(scope_selector)
        if scopes:
            first_page_scopes = scopes
            return True
        if _profile_has_no_videos(soup):
            # 站点明确显示“没有视频……”：不重试、不清空既有快照，交给调用方按源站状态处理。
            raise ModelProfileUnavailable("主页没有公开视频")
        return False

    async with _client() as c:
        while len(videos) < max_videos:
            page_url = _with_page(videos_url, page)
            r = await _fetch_with_budget(
                c,
                page_url,
                page_validator=first_page_validator if page == 1 else None,
                budget=budget,
            )
            if r is None:
                if page == 1:
                    raise ScrapeError(
                        f"本人上传页暂时仍被 PH 拦截，未写入空结果：{page_url}")
                raise ScrapeFetchError(
                    f"本人上传第 {page} 页未在统一重试/时间预算内完成；"
                    "已拒绝返回部分列表以保护既有快照")
            if int(getattr(r, "status_code", 0) or 0) in {404, 410}:
                # PH 会对超出实际页数的分页返回 404；这只表示列表已经到底，
                # 不能据此把仍然正常的模特主页标成“不可用”。只有第一页的
                # 404/410 才能证明目标主页本身不存在。
                if page == 1:
                    raise ModelProfileUnavailable("主页不存在或已删除")
                break
            if page == 1 and first_page_scopes is not None:
                scopes = first_page_scopes
                first_page_scopes = None
            else:
                soup = await asyncio.to_thread(BeautifulSoup, r.text, "lxml")
                scopes = soup.select(scope_selector)
            if not scopes:
                if page > 1 and _listing_past_last_page(soup, container_selector, page):
                    break
                # 主体容器消失说明 PH DOM 结构变了；不要退化到全页抓取，否则会重新吃进 dropdown 推荐位。
                if page == 1:
                    raise ScrapeError(
                        f"未找到本人上传主体容器 {scope_selector}，PH 页面结构可能已变化：{page_url}")
                # 正常列表到底由明确 404/410、权威空容器或连续无新增表示；
                # 200 页面连容器都消失更像挑战壳/DOM 漂移，不能发布部分快照。
                raise ScrapeFetchError(
                    f"本人上传第 {page} 页缺少权威主体容器；"
                    "已拒绝返回部分列表以保护既有快照")
            found = 0
            for it in scopes:
                total_cards += 1
                up = _uploader_slug(it)
                if up == owner_slug:
                    owner_hits += 1
                elif up is None and allow_hidden_uploader:
                    hidden_uploader_hits += 1
                else:
                    # 主体容器中若出现显式其他上传者，宁可跳过也不污染。
                    continue
                info = _parse_video_item(it)
                if info and info["viewkey"] and info["viewkey"] not in seen:
                    seen.add(info["viewkey"])
                    videos.append(info)
                    found += 1
                    if len(videos) >= max_videos:
                        break
            if page == 1 and videos and include_playlist:
                # 首屏已经提供本人归属与高分辨率封面；立即验证 public playlist。
                # PID 必须严格等于 owner_slug 且与 DOM 高度重合，否则继续完整 DOM
                # 分页作为保守回退。可信 playlist 可避免再抓几十页。
                first_viewkey = videos[0]["viewkey"]
                pkey = f"sys%3Ausers%3A{owner_slug}%3Apublic%3Amr"
                play_url = (
                    f"{settings.ph_base_url}/view_video.php?viewkey={first_viewkey}&pkey={pkey}")
                playlist_videos = await _fetch_public_playlist_videos(
                    c, play_url, owner_slug, budget=budget)
                if playlist_videos:
                    playlist_keys = {video["viewkey"] for video in playlist_videos}
                    dom_keys = {video["viewkey"] for video in videos}
                    if len(playlist_keys & dom_keys) >= min(len(dom_keys), 20):
                        merged = _merge_playlist_with_dom_videos(
                            playlist_videos, videos, max_videos=max_videos)
                        merged_seen = {video["viewkey"] for video in merged}
                        merged.extend(video for video in videos
                                      if video["viewkey"] not in merged_seen)
                        videos = merged[:max_videos]
                        break
            if found == 0:
                empty_pages += 1
                if empty_pages >= 2:
                    break
            else:
                empty_pages = 0
            if len(videos) >= max_videos:
                break
            page += 1
            await asyncio.sleep(min(1.0, budget.remaining_seconds()))

    if total_cards >= 20 and owner_hits == 0 and not allow_hidden_uploader:
        raise ScrapeError(
            f"本人上传归属校验失败：{total_cards} 张主体卡片中没有一个上传者命中 {owner_slug}，"
            f"PH 可能已改版，请重新解析页面容器。")
    return videos


def _parse_video_item(it) -> dict | None:
    a = it.select_one("a[href*='view_video.php'], a.linkVideoThumb, a[href*='viewkey=']")
    if not a or not a.get("href"):
        return None
    href = urljoin(settings.ph_base_url, a["href"])
    viewkey = _viewkey_from_url(href)
    if not viewkey:
        return None
    title = (a.get("title") or "").strip()
    if not title:
        t = it.select_one(".title a, span.title")
        title = t.get_text(strip=True) if t else viewkey
    # 时长
    dur = 0
    d = it.select_one(".duration, var.duration, .marker-overlays var")
    if d:
        dur = _duration_to_seconds(d.get_text())
    # 缩略图
    thumb = ""
    img = it.select_one("img")
    if img:
        thumb = img.get("data-src") or img.get("data-thumb_url") or img.get("src") or ""
    return {"viewkey": viewkey, "title": title,
            "url": f"{settings.ph_base_url}/view_video.php?viewkey={viewkey}",
            "duration": dur, "thumbnail": thumb}


def _decode_js_string(value: str) -> str:
    """解码页面脚本里的 JSON 字符串片段。"""
    return bytes(value, "utf-8").decode("unicode_escape").replace("\\/", "/")


def _extract_json_string_var(html: str, name: str) -> str:
    """从脚本对象里提取 `\"name\":\"...\"` 形式的字符串值。"""
    m = re.search(rf'"{re.escape(name)}"\s*:\s*"((?:\\.|[^"\\])*)"', html)
    return _decode_js_string(m.group(1)) if m else ""


def _parse_playlist_api_video(raw: dict) -> dict | None:
    """把播放列表 API 的视频对象转成项目内部视频结构。"""
    viewkey = str(raw.get("vkey") or raw.get("viewkey") or "").strip()
    if not viewkey:
        return None
    title = html_lib.unescape(str(raw.get("title") or viewkey)).strip() or viewkey
    duration = raw.get("duration") or 0
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = _duration_to_seconds(str(raw.get("durationFormatted") or ""))
    thumb = (raw.get("xSmallThumbNew") or raw.get("thumbnail") or raw.get("thumb")
             or raw.get("image") or "")
    url = raw.get("urlWithPkey") or f"/view_video.php?viewkey={viewkey}"
    return {"viewkey": viewkey, "title": title,
            "url": urljoin(settings.ph_base_url, url),
            "duration": duration, "thumbnail": thumb}


def _merge_playlist_with_dom_videos(
        playlist_videos: list[dict], dom_videos: list[dict], *, max_videos: int) -> list[dict]:
    """保留播放列表的完整顺序，同时用 DOM 中更清晰的封面补强同一 viewkey。

    PH 的 public playlist 通常只给 150x84 的 ``xSmallThumbNew``，而模特视频页
    当前会给签名的 320x180 图。过去直接以 playlist 覆盖 DOM，导致封面被降级。
    """
    dom_by_key = {item.get("viewkey"): item for item in dom_videos if item.get("viewkey")}
    result: list[dict] = []
    for source in playlist_videos[:max_videos]:
        item = dict(source)
        dom = dom_by_key.get(item.get("viewkey"))
        if dom:
            if dom.get("thumbnail"):
                item["thumbnail"] = dom["thumbnail"]
            for key in ("title", "url", "duration"):
                if not item.get(key) and dom.get(key):
                    item[key] = dom[key]
        result.append(item)
    return result


async def _fetch_public_playlist_videos(
        client: httpx.AsyncClient, first_video_url: str, owner_slug: str, *,
        budget: _FetchBudget | None = None) -> list[dict]:
    """读取本人 public playlist API，补齐普通分页漏渲染的视频。

    PH 的 `/model/{slug}/videos` 普通分页可能漏掉个别卡片；但点击播放全部后，
    页面脚本会暴露 `WATCHPAGE_PLAYLISTS.fakeVideosUrl`，其 `pid` 形如
    `sys:users:{slug}:public:mr`，是该 model 本人的公开视频列表。这里仅在
    pid 与 owner_slug 完全一致时才请求并合并，避免误用推荐/随机播放列表。
    """
    if budget is None:
        active_budget = _FetchBudget()
    else:
        # playlist 是首屏后的吞吐优化，不是完整性唯一来源。给它独立的小预算，
        # 仍受父 deadline 剩余墙钟时间约束，但不能耗尽父抓取的全部重试次数，
        # 否则一次可选接口抖动会让后续权威 DOM 分页无机会回退。
        remaining = budget.remaining_seconds()
        if remaining <= 0:
            return []
        active_budget = _FetchBudget(
            retries=2,
            timeout_seconds=min(15.0, remaining),
        )
    r = await _fetch_with_budget(client, first_video_url, budget=active_budget)
    if r is None:
        return []
    fake_url = _extract_json_string_var(r.text, "fakeVideosUrl")
    if not fake_url:
        return []
    q = parse_qs(urlparse(fake_url).query)
    pid = unquote((q.get("pid") or [""])[0])
    if pid != f"sys:users:{owner_slug}:public:mr":
        return []
    url = urljoin(str(r.url), fake_url)
    try:
        api = await asyncio.wait_for(client.get(
            url, headers={"Referer": str(r.url), "X-Requested-With": "XMLHttpRequest"}
        ), timeout=max(0.001, active_budget.remaining_seconds()))
        api.raise_for_status()
        data = api.json()
    except (asyncio.TimeoutError, httpx.HTTPError, json.JSONDecodeError, ValueError):
        return []
    videos = data.get("videos") if isinstance(data, dict) else None
    if not isinstance(videos, list):
        return []
    parsed: list[dict] = []
    seen: set[str] = set()
    for item in videos:
        if not isinstance(item, dict):
            continue
        info = _parse_playlist_api_video(item)
        if info and info["viewkey"] not in seen:
            seen.add(info["viewkey"])
            parsed.append(info)
    return parsed


# ---------- 视频页 -> M3U8 ----------
async def get_best_m3u8(
    video_url: str,
    *,
    max_quality: int | None = None,
) -> dict:
    """解析最高可用画质的最终 VOD 媒体清单并冻结本次下载计划。

    页面 ``video_duration`` 只作为展示元数据返回；下载完整性只由
    ``hls_plan`` 中的有限媒体清单、分片序列和时间轴决定。``max_quality``
    只用于已证明最高档源损坏后的有限降级，普通下载始终从最高画质开始。
    """
    video_url = validate_ph_url(video_url)
    expected_viewkey = _viewkey_from_url(video_url)

    def is_expected_video_page(response) -> bool:
        final_url = str(getattr(response, "url", video_url) or video_url)
        try:
            validate_ph_url(final_url)
        except InvalidScrapeURL:
            return False
        final_viewkey = _viewkey_from_url(final_url)
        if expected_viewkey and final_viewkey != expected_viewkey:
            return False
        response_html = str(getattr(response, "text", "") or "")
        if _extract_flashvars(response_html) is not None:
            return True

        # PH 对已下架视频返回 200 和完整站点外壳，旧逻辑会把这种页面当作
        # “大型未知拦截页”连续退避，最终误报网络超时。只有目标 viewkey 未变且
        # 站点给出专用 removed 容器时才做永久判断；未知页面继续按暂态失败处理。
        unavailable_reason = _video_unavailable_reason(response_html)
        if unavailable_reason:
            raise VideoUnavailableError(
                f"视频已下架或不可播放：{unavailable_reason}"
            )
        return False

    async def load_flashvars(client) -> dict:
        r = await _fetch_ok(
            client, video_url, page_validator=is_expected_video_page,
        )                                   # 自动穿透反爬并正向确认目标视频页
        if r is None:
            raise ScrapeFetchError("视频页请求失败：反爬拦截，多次重试仍未放行")
        if int(getattr(r, "status_code", 0) or 0) in {404, 410}:
            raise VideoUnavailableError("视频页不存在或已下架")
        flashvars = _extract_flashvars(r.text)
        if not flashvars:
            # 正常路径已由 page_validator 证明；保留硬检查，防止未来调用约束被绕开。
            raise ScrapeError("视频页结构异常：未找到 flashvars")
        return flashvars

    # 每次页面加载都会把整组 HLS 地址分到同一个 CDN 主机。某主机清单返回 410 时
    # 重新加载页面换主机，而不是改用同页更低画质的候选；重抽次数耗尽仍抛出
    # HLSSourceUnavailableError，交给任务层原有的来源冷却。
    gone_hosts: set[str] = set()
    source_error: HLSSourceUnavailableError | None = None
    async with _client() as c:
        for page_load in range(_CDN_REROLL_PAGE_LOADS):
            if page_load:
                await asyncio.sleep(_CDN_REROLL_DELAY_SECONDS)
            flashvars = await load_flashvars(c)
            candidates = _hls_candidates(flashvars, max_quality)

            # 优先最高画质；若该档使用当前链路无法安全合并的外置音轨或其它确定性
            # 不支持特性，才降到下一个候选。网络/签名错误仍直接上抛，不静默降画质。
            plan = None
            reroll = False
            plan_errors: list[str] = []
            for quality, candidate_url in candidates:
                cdn_host = _ph_cdn_host(candidate_url)
                if cdn_host in gone_hosts:
                    # 本次页面仍分到已确认 410 的主机，不再重复请求清单。
                    reroll = True
                    break
                try:
                    # 直接 leaf 没有 CODECS，音频状态只能记为 unknown；master 若声明
                    # mp4a/AAC 等则会在递归中把 expect_audio 提升为 True。
                    plan = await resolve_hls_plan(
                        candidate_url, quality_hint=quality, expect_audio=False,
                        max_quality=max_quality)
                    break
                except HLSPlanError as error:
                    plan_errors.append(str(error))
                except HLSSourceUnavailableError as error:
                    if not cdn_host:
                        raise
                    gone_hosts.add(cdn_host)
                    if len(gone_hosts) > 1:
                        # 不同 CDN 主机都返回 410，已不是主机分配问题，交给来源冷却。
                        raise
                    source_error = error
                    reroll = True
                    break
            if reroll:
                continue
            if plan is None:
                detail = " | ".join(plan_errors[:3]) or "没有可用 HLS 候选"
                raise HLSPlanError(f"所有 HLS 候选均无法安全下载：{detail}")
            page_duration = _display_duration(flashvars.get("video_duration", 0))
            return {
                "m3u8_url": plan["m3u8_url"],
                "quality": plan["quality"],
                "title": flashvars.get("video_title", ""),
                "page_duration": page_duration,
                "hls_plan": plan,
            }
    raise source_error


def _hls_candidates(flashvars: dict, max_quality: int | None) -> list[tuple[int, str]]:
    """按画质从高到低返回页面声明的 HLS 地址。"""
    media_defs = flashvars.get("mediaDefinitions") or flashvars.get("media_definitions") or []
    # mediaDefinitions 的 quality 字段不能证明 videoUrl 已是媒体清单；PH 当前会把
    # 标为 1080 的 URL 继续指向一层 master，必须递归解析到 ENDLIST 媒体层。
    candidates = []
    for md in media_defs:
        if md.get("format") == "hls" and md.get("videoUrl"):
            q = md.get("quality")
            quality = _quality_to_int(q)
            # quality=0 通常表示 URL 仍是 master，具体高度要在递归解析后过滤。
            if (max_quality is None or quality <= 0
                    or quality <= max_quality):
                candidates.append((quality, md["videoUrl"]))
    if not candidates:
        if max_quality is not None:
            raise HLSPlanError(f"没有不高于 {max_quality}p 的 HLS 源")
        raise ScrapeError("未找到 hls m3u8 源")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates


def _ph_cdn_host(url: str) -> str:
    """PH 自有 CDN 返回小写主机名，其他来源返回空串。"""
    host = (urlparse(str(url)).hostname or "").lower().rstrip(".")
    return host if host == "phncdn.com" or host.endswith(".phncdn.com") else ""


def _extract_flashvars(html: str) -> dict | None:
    m = re.search(r"var\s+flashvars_\d+\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not m:
        m = re.search(r"flashvars\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _quality_to_int(q) -> int:
    """quality 可能是 '1080' / ['1080','720'](master标记) / 数字。"""
    if isinstance(q, list):
        return 0  # master playlist
    try:
        return int(q)
    except (ValueError, TypeError):
        return 0


def _display_duration(value) -> int:
    """解析纯展示时长；脏元数据无权阻断 HLS 下载计划。"""
    try:
        duration = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(duration) if math.isfinite(duration) and duration >= 0 else 0


def _stable_media_identity(url: str) -> str:
    """只对已知 PH CDN 移除轮换签名，其他来源的 URL 全量进入指纹。"""
    parts = urlparse(str(url))
    host = (parts.hostname or "").lower().rstrip(".")
    is_ph_cdn = host == "phncdn.com" or host.endswith(".phncdn.com")
    path = parts.path or "/"
    marker = path.find("/hls/")
    if is_ph_cdn and marker >= 0:
        # PH 把 ``签名,过期时间`` 放在 /hls/ 之前的路径段；CDN 主机也会轮换。
        prefix = ""
        path = path[marker:]
    else:
        prefix = host + ":" + str(_effective_port(parts))
    stable_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.casefold()
        if (is_ph_cdn and (lowered in _VOLATILE_MEDIA_QUERY_KEYS
                or lowered.startswith("x-amz-")
                or lowered.startswith("cdn-token"))):
            continue
        stable_query.append((key, value))
    query = urlencode(stable_query, doseq=True)
    return f"{prefix}{path}" + (f"?{query}" if query else "")


def _codecs_expect_audio(codecs: str | None) -> bool:
    value = str(codecs or "").casefold()
    return any(marker in value for marker in (
        "mp4a", "ac-3", "ec-3", "opus", "vorbis", "aac",
    ))


def _stream_rank(playlist) -> tuple:
    info = playlist.stream_info
    width, height = info.resolution or (0, 0)
    bandwidth = (
        getattr(info, "average_bandwidth", None)
        or getattr(info, "bandwidth", None)
        or 0
    )
    frame_rate = getattr(info, "frame_rate", None) or 0
    return int(height or 0), int(width or 0), int(bandwidth or 0), float(frame_rate or 0)


def _manifest_fingerprint(playlist_url: str, playlist) -> tuple[str, int]:
    """对有序媒体计划做语义哈希；返回 fingerprint 与 HlsFD 下载单元数。"""
    segments = []
    init_identities: list[tuple[str, str]] = []
    for segment in playlist.segments:
        segment_uri = str(getattr(segment, "uri", "") or "").strip()
        if not segment_uri:
            raise HLSPlanError("HLS 媒体清单包含空分片 URI")
        segment_url = validate_media_url(urljoin(playlist_url, segment_uri))
        init = getattr(segment, "init_section", None)
        init_identity = None
        if init and getattr(init, "uri", None):
            init_url = validate_media_url(urljoin(playlist_url, init.uri))
            init_identity = (
                _stable_media_identity(init_url),
                str(getattr(init, "byterange", None) or ""),
            )
            init_identities.append(init_identity)
        key = getattr(segment, "key", None)
        key_identity = None
        if key and str(getattr(key, "method", "") or "").upper() != "NONE":
            method = str(getattr(key, "method", "") or "").upper()
            if method != "AES-128":
                raise HLSPlanError(f"原生 HLS 不支持加密方法 {method or '空'}")
            key_format = str(getattr(key, "keyformat", "") or "identity")
            if key_format.casefold() != "identity":
                raise HLSPlanError(f"原生 HLS 不支持 KEYFORMAT={key_format}")
            key_uri = getattr(key, "uri", None)
            if not key_uri:
                raise HLSPlanError("AES-128 HLS 密钥缺少 URI")
            key_url = validate_media_url(urljoin(playlist_url, key_uri))
            key_identity = {
                "method": method,
                "uri": _stable_media_identity(key_url),
                "iv": str(getattr(key, "iv", "") or ""),
            }
        segments.append({
            "uri": _stable_media_identity(segment_url),
            "duration": round(float(segment.duration or 0), 6),
            "byterange": str(getattr(segment, "byterange", None) or ""),
            "discontinuity": bool(getattr(segment, "discontinuity", False)),
            "init": init_identity,
            "key": key_identity,
        })

    unique_inits = list(dict.fromkeys(init_identities))
    if len(unique_inits) > 1:
        raise HLSPlanError("HLS 媒体清单包含变化的初始化段，无法保证原生拼接完整")
    if init_identities and not getattr(playlist.segments[0], "init_section", None):
        raise HLSPlanError("HLS 初始化段出现在媒体分片之后，原生下载器无法安全处理")

    payload = {
        "schema": _HLS_PLAN_VERSION,
        "playlist": _stable_media_identity(playlist_url),
        "media_sequence": int(getattr(playlist, "media_sequence", 0) or 0),
        "discontinuity_sequence": int(
            getattr(playlist, "discontinuity_sequence", 0) or 0),
        "target_duration": float(getattr(playlist, "target_duration", 0) or 0),
        "segments": segments,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(segments) + bool(unique_inits)


def _media_plan_from_playlist(
    playlist_url: str,
    manifest_text: str,
    playlist,
    *,
    quality: int,
    expect_audio: bool,
) -> dict:
    if playlist.playlists:
        raise HLSPlanError("HLS 解析未落到最终媒体清单")
    if not playlist.segments:
        raise HLSPlanError("HLS 最终媒体清单没有可下载分片")
    if not playlist.is_endlist:
        raise HLSPlanError("HLS 最终媒体清单缺少 ENDLIST，不是有限 VOD")
    if any(bool(getattr(segment, "discontinuity", False))
           for segment in playlist.segments):
        raise HLSPlanError("HLS 媒体清单包含时间轴 discontinuity，无法证明单一成品完整")
    map_count = 0
    media_seen = False
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXT-X-MAP:"):
            map_count += 1
            if media_seen or map_count > 1:
                raise HLSPlanError(
                    "HLS 媒体清单包含重复或后置 EXT-X-MAP，原生下载器无法安全处理")
        elif not line.startswith("#"):
            media_seen = True
    durations = [float(segment.duration or 0) for segment in playlist.segments]
    if any(not math.isfinite(duration) or duration <= 0 for duration in durations):
        raise HLSPlanError("HLS 媒体清单包含无效分片时长")
    if playlist.target_duration is None:
        raise HLSPlanError("HLS 媒体清单缺少协议必需的 TARGETDURATION")
    target_duration = float(playlist.target_duration)
    if not math.isfinite(target_duration) or target_duration <= 0:
        raise HLSPlanError("HLS 媒体清单 TARGETDURATION 无效")
    if max(durations) > target_duration + 0.5:
        raise HLSPlanError("HLS 分片时长超过 TARGETDURATION 协议边界")
    fingerprint, unit_count = _manifest_fingerprint(playlist_url, playlist)
    encrypted = any(
        str(getattr(getattr(segment, "key", None), "method", "") or "").upper()
        not in {"", "NONE"}
        for segment in playlist.segments
    )
    return {
        "schema_version": _HLS_PLAN_VERSION,
        "m3u8_url": playlist_url,
        # 仅在当前尝试内传给固定版本 HlsFD，确保下载的就是已审计的同一份清单。
        "manifest_text": manifest_text,
        "fingerprint": fingerprint,
        "segment_count": len(playlist.segments),
        "unit_count": int(unit_count),
        "media_duration": round(sum(durations), 6),
        "target_duration": target_duration,
        "media_sequence": int(playlist.media_sequence or 0),
        "quality": int(quality or 0),
        "expect_audio": bool(expect_audio),
        "encrypted": encrypted,
    }


def _media_parts(playlist_url, manifest_text, playlist, *, quality, expect_audio):
    """只在声明的 discontinuity 边界拆段，不能靠切段掩盖未知时间戳错误。"""
    import m3u8 as m3u8lib
    boundaries = [i for i, seg in enumerate(playlist.segments) if i and seg.discontinuity]
    if not boundaries:
        return [_media_plan_from_playlist(playlist_url, manifest_text, playlist,
                                         quality=quality, expect_audio=expect_audio)]
    if not playlist.is_endlist or playlist.playlists or len(boundaries) >= MAX_PERIODS:
        raise HLSPlanError("HLS discontinuity 时间段超限或不是有限媒体清单")
    # 隐式 BYTERANGE 依赖前一段偏移；尚未实现跨段重基准，保守拒绝。
    if any(seg.byterange or seg.gap_tag for seg in playlist.segments):
        raise HLSPlanError("分段 HLS 暂不支持 BYTERANGE 或缺失分片")
    parts = []
    starts = [0, *boundaries, len(playlist.segments)]
    for index, (start, end) in enumerate(zip(starts, starts[1:])):
        section = copy.copy(playlist)
        section.segments = m3u8lib.SegmentList(copy.deepcopy(playlist.segments[start:end]))
        section.segments[0].discontinuity = False
        section.media_sequence = int(playlist.media_sequence or 0) + start
        section.discontinuity_sequence = int(playlist.discontinuity_sequence or 0) + index
        text = section.dumps()
        parts.append(_media_plan_from_playlist(playlist_url, text, section,
                                               quality=quality, expect_audio=expect_audio))
    return parts


def _compose_plan(video_parts, audio_parts=None):
    try:
        return compose_plan(video_parts, audio_parts)
    except (ValueError, TypeError, KeyError) as exc:
        raise HLSPlanError(str(exc)) from exc


async def _fetch_hls_playlist(client, media_url):
    import m3u8 as m3u8lib
    media_url = validate_media_url(media_url)
    try:
        response = await client.get(media_url)
    except (httpx.TransportError, httpx.HTTPError) as exc:
        # httpx 的超时异常常常没有文字，只留类名，避免任务里只剩“请求失败:”。
        detail = str(exc).strip() or type(exc).__name__
        raise HLSManifestFetchError(f"HLS 清单请求失败: {detail}") from exc
    if response.status_code == 410:
        raise HLSSourceUnavailableError("HLS 清单请求返回 HTTP 410")
    if response.status_code == 429:
        raise HLSRateLimitedError("HLS 清单请求返回 HTTP 429")
    if response.status_code != 200:
        raise HLSManifestFetchError(
            f"HLS 清单请求返回 HTTP {response.status_code}")
    manifest_text = response.text
    if len(manifest_text.encode("utf-8")) > _MAX_HLS_MANIFEST_BYTES:
        raise HLSPlanError("HLS 清单体积异常，已拒绝解析")
    resolved_url = validate_media_url(str(getattr(response, "url", media_url)))
    try:
        playlist = m3u8lib.loads(manifest_text)
    except Exception as exc:
        raise HLSPlanError(f"HLS 清单解析失败: {exc}") from exc
    return resolved_url, manifest_text, playlist


async def _resolve_vod_plan(
    client: httpx.AsyncClient, media_url: str, *, quality_hint: int = 0,
    depth: int = 0, visited: set[str] | None = None, expect_audio: bool = False,
    max_quality: int | None = None,
) -> dict:
    """递归冻结清单；音视频分离及声明过的时间段组成有界组合计划。"""
    if depth > _MAX_HLS_DEPTH:
        raise HLSPlanError(f"HLS master 嵌套超过 {_MAX_HLS_DEPTH} 层")
    media_url = validate_media_url(media_url)
    visited = visited if visited is not None else set()
    if media_url in visited:
        raise HLSPlanError("HLS master 清单存在循环引用")
    visited.add(media_url)
    resolved_url, manifest_text, playlist = await _fetch_hls_playlist(client, media_url)
    if not playlist.playlists:
        if (max_quality is not None and quality_hint > 0
                and quality_hint > max_quality):
            raise HLSPlanError(
                f"{quality_hint}p 媒体清单超过 {max_quality}p 降级上限")
        parts = _media_parts(
            resolved_url, manifest_text, playlist,
            quality=quality_hint, expect_audio=expect_audio,
        )
        return parts[0] if len(parts) == 1 else _compose_plan(parts)

    variant_errors: list[str] = []
    eligible_count = 0
    for variant in sorted(playlist.playlists, key=_stream_rank, reverse=True):
        info = variant.stream_info
        resolution = info.resolution or (0, 0)
        quality = int(resolution[1] or quality_hint or 0)
        if (max_quality is not None and quality > 0
                and quality > max_quality):
            continue
        eligible_count += 1
        audio_group = str(getattr(info, "audio", "") or "")
        external_audio = []
        if audio_group:
            external_audio = [
                media for media in playlist.media
                if str(getattr(media, "type", "") or "").upper() == "AUDIO"
                and str(getattr(media, "group_id", "") or "") == audio_group
                and getattr(media, "uri", None)
            ]
        child_url = validate_media_url(urljoin(resolved_url, variant.uri))
        try:
            if external_audio:
                defaults = [a for a in external_audio if str(a.default).upper() == "YES"]
                choices = defaults or external_audio
                if len(choices) != 1:
                    raise HLSPlanError("外置音轨没有唯一默认选择，需人工指定")
            result = await _resolve_vod_plan(
                client, child_url, quality_hint=quality, depth=depth + 1,
                visited=set(visited),
                expect_audio=(False if external_audio else expect_audio or _codecs_expect_audio(info.codecs)),
                max_quality=max_quality,
            )
            if not external_audio:
                return result
            audio_url = validate_media_url(urljoin(resolved_url, choices[0].uri))
            audio_url, audio_text, audio = await _fetch_hls_playlist(client, audio_url)
            audio_parts = _media_parts(audio_url, audio_text, audio, quality=0, expect_audio=True)
            if result["schema_version"] == 3:
                if len(result["tracks"]) != 1:
                    raise HLSPlanError("HLS 嵌套了多组外置音轨")
                video_parts = result["tracks"][0]["parts"]
            else:
                video_parts = [result]
            return _compose_plan(video_parts, audio_parts)
        except HLSPlanError as error:
            variant_errors.append(f"{quality or '未知'}p: {error}")
    if eligible_count == 0 and max_quality is not None:
        raise HLSPlanError(f"HLS master 没有不高于 {max_quality}p 的变体")
    detail = " | ".join(variant_errors[:3]) or "没有可用媒体变体"
    raise HLSPlanError(f"HLS master 的所有变体均不可用：{detail}")


async def resolve_hls_plan(
    media_url: str,
    quality_hint: int = 0,
    *,
    expect_audio: bool = False,
    max_quality: int | None = None,
) -> dict:
    """公开给下载链路与本地集成测试使用的 HLS 计划解析入口。"""
    media_url = validate_media_url(media_url)
    async with _client(ph_only=False, media_path=True) as client:
        return await _resolve_vod_plan(
            client, media_url, quality_hint=quality_hint,
            expect_audio=expect_audio,
            max_quality=max_quality,
        )
