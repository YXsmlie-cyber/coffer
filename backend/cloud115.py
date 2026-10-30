"""115网盘客户端。

登录方式：
  - QR码登录：生成二维码，用115手机App扫码授权，轮询拿到cookie
  - Cookie登录：直接传入浏览器导出的cookie字符串

主要能力：
  - 列出目录（已验证用 aps.115.com/natsort/files.php）
  - 在 VIDEO HUB 下找/建模特文件夹
  - 扫描模特文件夹所有视频文件（含 SHA1 / 时长 / 大小）
  - 上传文件（优先秒传 SHA1，失败再走普通上传）

注意：115为非官方API，接口可能变动。所有请求带 cookie。
"""
import asyncio
import os
import time
import hashlib
import base64
import random
import re
import secrets
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from importlib import import_module
from urllib.error import HTTPError
from urllib.parse import urlsplit
from weakref import WeakValueDictionary
import httpx

from .config import settings

# 各端点（仅列目录/建目录还在用；扫码登录已委托 p115client）
FILES_URL = "https://aps.115.com/natsort/files.php"
MKDIR_URL = "https://webapi.115.com/files/add"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_PLUGIN_DIRECTORY_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_PLUGIN_MAGNET_RE = re.compile(
    r"(?:^|[?&])xt=urn:btih:[0-9a-f]{40}(?:&|$)", re.IGNORECASE)


def _norm_name(name: str) -> str:
    """归一化文件夹名用于比较：折叠内部连续空白、去首尾空白、转小写。"""
    return " ".join((name or "").split()).lower()


class Cloud115Error(Exception):
    pass


class UploadTransferError(Cloud115Error):
    """p115client 上传调用本身失败（初始化、OSS 传输或回调），区别于本地读文件等前置错误。"""


_SENSITIVE_ERROR_KEYS = (
    "user_key", "cookie", "authorization", "access_token", "refresh_token",
    "token", "secret", "password", "passwd", "signature", "callback",
    "uid", "seid",
)
_SENSITIVE_KEY_PATTERN = "|".join(
    re.escape(key) for key in _SENSITIVE_ERROR_KEYS)
_QUOTED_SECRET_RE = re.compile(
    rf"(?i)(['\"]?(?:{_SENSITIVE_KEY_PATTERN})['\"]?\s*[:=]\s*)"
    r"(['\"])[^'\"]*\2"
)
_UNQUOTED_SECRET_RE = re.compile(
    rf"(?i)(['\"]?(?:{_SENSITIVE_KEY_PATTERN})['\"]?\s*[:=]\s*)"
    r"([^,;\s}\]]+)"
)


def redact_sensitive_error_text(value: object) -> str:
    """把异常/响应文本中的上传凭据替换为固定占位符。"""
    text = str(value or "")
    text = _QUOTED_SECRET_RE.sub(r"\1\2<redacted>\2", text)
    text = _UNQUOTED_SECRET_RE.sub(r"\1<redacted>", text)
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+",
        "Bearer <redacted>",
        text,
    )
    return text


def _is_truncated_upload_init_error(error: BaseException) -> bool:
    """识别 p115cipher 解码 0/1 字节初始化响应的已知瞬时失败。"""
    current: BaseException | None = error
    seen: set[int] = set()
    pre_transfer_abort = False
    truncated_response = False
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if type(current).__name__ == "MultipartUploadAbort":
            payload = current.args[0] if current.args else None
            if isinstance(payload, dict):
                pre_transfer_abort = (
                    not payload.get("upload_id") and not payload.get("callback"))
        if (isinstance(current, IndexError)
                and "index out of bounds on dimension 1" in str(current).casefold()):
            truncated_response = True
        nested = current.__cause__
        if not isinstance(nested, BaseException) and not current.__suppress_context__:
            nested = current.__context__
        current = nested if isinstance(nested, BaseException) else None
    return pre_transfer_abort and truncated_response


_OSS_ERROR_CODE_RE = re.compile(rb"<Code>\s*([A-Za-z0-9_.-]{1,64})\s*</Code>")
_OSS_ERROR_MESSAGE_RE = re.compile(rb"<Message>\s*([^<]{1,200}?)\s*</Message>")


def _http_error_detail(error: BaseException) -> str:
    """补出 HTTPError 的目标主机与阿里云 OSS 错误码。

    “HTTP Error 403: Forbidden”本身分不清是签名过期、时间偏差还是权限问题；OSS 在
    响应体里给出 <Code>/<Message>，上游请求库抛错前已把响应体读进 ``fp.data``。
    只取主机名和这两个短字段，不带查询串（含 uploadId/签名），并照常脱敏。
    """
    if not isinstance(error, HTTPError):
        return ""
    details: list[str] = []
    try:
        host = urlsplit(str(getattr(error, "url", None) or error.filename or "")).hostname or ""
    except ValueError:
        host = ""
    if host:
        details.append(host)
    body = getattr(getattr(error, "fp", None), "data", b"")
    if isinstance(body, (bytes, bytearray)):
        code = _OSS_ERROR_CODE_RE.search(body)
        if code:
            oss = f"OSS {code.group(1).decode('ascii', 'replace')}"
            message = _OSS_ERROR_MESSAGE_RE.search(body)
            if message:
                oss += f"：{message.group(1).decode('utf-8', 'replace')}"
            details.append(oss)
    headers = getattr(error, "headers", None)
    try:
        error_code = headers.get("x-oss-ec") if headers is not None else None
    except Exception:
        error_code = None
    if error_code:
        details.append(f"EC {str(error_code)[:32]}")
    return redact_sensitive_error_text("；".join(details))


def _iter_exception_chain(error: BaseException, max_depth: int = 8):
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(max_depth):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        nested = current.__cause__
        if not isinstance(nested, BaseException) and not current.__suppress_context__:
            nested = current.__context__
        current = nested if isinstance(nested, BaseException) else None


def is_bucket_policy_denial(error: BaseException) -> bool:
    """异常链里有 OSS 明确答复“Access denied by bucket policy”。"""
    for current in _iter_exception_chain(error):
        if isinstance(current, HTTPError) and current.code == 403:
            body = getattr(getattr(current, "fp", None), "data", b"")
            if (isinstance(body, (bytes, bytearray))
                    and b"AccessDenied" in body and b"bucket policy" in body):
                return True
    return False


# 与 p115client.upload_file 的默认上传目的地一致。
_OSS_UPLOAD_ENDPOINT = "https://oss-cn-shenzhen.aliyuncs.com"
_OSS_UPLOAD_BUCKET = "fhnfile"
_UPLOAD_PROBE_TIMEOUT = 20.0


def _probe_upload_access_sync() -> str:
    """申请上传临时凭证，对随机对象发起分块上传并立即取消；不在网盘留下任何文件。

    返回 ``denied``（存储桶策略拒绝本机）、``allowed``（OSS 没有按策略拒绝）或
    ``unknown``（网络或凭证接口异常，不能下结论）。
    """
    try:
        from p115oss.api import upload_token
        from p115oss.oss import oss_multipart_upload_cancel, oss_multipart_upload_init
    except ImportError:
        return "unknown"
    try:
        token = upload_token(timeout=_UPLOAD_PROBE_TIMEOUT)
    except Exception:
        return "unknown"
    if not isinstance(token, dict) or str(token.get("StatusCode")) != "200":
        return "unknown"
    key = secrets.token_hex(20)
    kwargs = {"bucket": _OSS_UPLOAD_BUCKET, "endpoint": _OSS_UPLOAD_ENDPOINT,
              "timeout": _UPLOAD_PROBE_TIMEOUT}
    try:
        upload_id = oss_multipart_upload_init(key, token, **kwargs)
    except HTTPError as error:
        # 403 以外的 HTTP 答复同样说明 OSS 可达且没有按存储桶策略拒绝，交给真实上传判断。
        return "denied" if is_bucket_policy_denial(error) else "allowed"
    except Exception:
        return "unknown"
    try:
        oss_multipart_upload_cancel(key, upload_id, token, **kwargs)
    except Exception:
        # 未完成的分块会被 OSS 生命周期规则清理，探测结论不受影响。
        pass
    return "allowed"


async def probe_upload_access() -> str:
    return await asyncio.to_thread(_probe_upload_access_sync)


def _safe_exception_chain(error: BaseException, max_depth: int = 6) -> str:
    """保留脱敏后的 cause/context 链，避免外层 MultipartUploadAbort 吞掉根因。"""
    current: BaseException | None = error
    seen: set[int] = set()
    parts: list[str] = []
    for _ in range(max_depth):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__
        if name == "MultipartUploadAbort":
            message = "上传初始化或分片流程中止"
        else:
            message = redact_sensitive_error_text(str(current)).strip()
            if (isinstance(current, IndexError)
                    and "index out of bounds on dimension 1" in message.casefold()):
                message = (
                    "115 上传初始化响应为空或截断，p115cipher 无法解码"
                )
            detail = _http_error_detail(current)
            if detail:
                message = f"{message}（{detail}）"
        parts.append(f"{name}: {message}" if message else name)
        nested = current.__cause__
        if not isinstance(nested, BaseException) and not current.__suppress_context__:
            nested = current.__context__
        current = nested if isinstance(nested, BaseException) else None
    return " <- ".join(parts) or type(error).__name__


def _upload_file_id(resp: dict) -> str:
    """从 p115client 的普通上传/秒传响应中提取权威文件 ID。"""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    value = data.get("file_id") or data.get("id") or resp.get("file_id")
    return str(value or "").strip()


class Cloud115Client:
    def __init__(self):
        self._cookie: str = ""
        self._client: httpx.AsyncClient | None = None
        self._read_sem = asyncio.Semaphore(settings.max_concurrent_115_reads)
        self._model_dir_locks: WeakValueDictionary[
            tuple[str, str], asyncio.Lock
        ] = WeakValueDictionary()
        self._load_cookie()

    # ---------- cookie 管理 ----------
    def _load_cookie(self):
        if os.path.exists(settings.cookie_file):
            with open(settings.cookie_file, "r", encoding="utf-8") as f:
                self._cookie = f.read().strip()

    def _save_cookie(self, cookie: str):
        directory = os.path.dirname(settings.cookie_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = settings.cookie_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(cookie)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, settings.cookie_file)
        self._cookie = cookie
        _reset_p115()  # cookie 变了，p115client 实例需重建

    @property
    def logged_in(self) -> bool:
        return bool(self._cookie)

    def set_cookie(self, cookie: str):
        """Cookie登录：直接设置并持久化。"""
        self._save_cookie(cookie.strip())

    @staticmethod
    def _new_http() -> httpx.AsyncClient:
        """创建 HTTP 客户端；候选 Cookie 校验使用独立实例，避免污染正式会话。"""
        return httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=8),
        )

    def _shared_http(self) -> httpx.AsyncClient:
        """返回生命周期内复用的 115 元数据客户端。"""
        if self._client is None or self._client.is_closed:
            self._client = self._new_http()
        return self._client

    def _cookie_headers(self, cookie: str | None = None) -> dict[str, str]:
        return {"Cookie": self._cookie if cookie is None else cookie}

    async def close(self):
        """关闭 keep-alive 连接池；由 FastAPI lifespan 调用。"""
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            await client.aclose()

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        """计算目录 GET 的有限退避，优先遵守 Retry-After。"""
        header = response.headers.get("Retry-After", "").strip() if response else ""
        if header:
            try:
                return max(0.0, min(60.0, float(header)))
            except ValueError:
                try:
                    when = parsedate_to_datetime(header)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    return max(0.0, min(
                        60.0, (when - datetime.now(timezone.utc)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(4.0, float(2 ** attempt)) + random.uniform(0.0, 0.25)

    async def check_cookie(self, cookie: str) -> bool:
        """在不覆盖当前有效登录的前提下验证候选 Cookie。"""
        candidate = str(cookie or "").strip()
        if not candidate:
            return False
        params = {
            "aid": 1, "cid": "0", "limit": 1, "offset": 0,
            "show_dir": 1, "o": "user_ptime", "asc": 0, "natsort": 1,
        }
        try:
            async with self._new_http() as c:
                r = await c.get(
                    FILES_URL, params=params,
                    headers=self._cookie_headers(candidate),
                )
                if r.status_code != 200:
                    return False
                data = r.json()
        except Exception:
            return False
        return data.get("state") is not False and "data" in data

    async def check_login(self) -> bool:
        """通过列根目录验证 cookie 是否有效。"""
        if not self._cookie:
            return False
        try:
            await self.list_dir("0", limit=1)
            return True
        except Exception:
            return False

    async def plugin_directory_ready(self, directory_id: str) -> dict[str, str]:
        """Verify one plugin-provided directory without exposing remote metadata."""
        if not isinstance(directory_id, str) or not _PLUGIN_DIRECTORY_ID_RE.fullmatch(directory_id):
            return {"status": "REJECTED_DIRECTORY"}
        if not self.logged_in:
            return {"status": "REJECTED_LOGIN"}
        try:
            response = await self.list_dir(directory_id, limit=1)
        except Exception:
            return {"status": "DIRECTORY_VALIDATION_UNKNOWN"}
        if isinstance(response, dict) and response.get("state") is not False and "data" in response:
            return {"status": "READY"}
        return {"status": "REJECTED_DIRECTORY"}

    async def plugin_submit_magnet(self, magnet: str, directory_id: str) -> dict[str, str]:
        """Create one explicit cloud task; unknown remote results are never retried."""
        if (
            not isinstance(magnet, str)
            or not 20 <= len(magnet) <= 4096
            or not magnet.startswith("magnet:?")
            or _PLUGIN_MAGNET_RE.search(magnet) is None
        ):
            return {"status": "CONFIRMED_FAILURE"}
        readiness = await self.plugin_directory_ready(directory_id)
        if readiness.get("status") != "READY":
            return {"status": "UNKNOWN_RESULT"}
        try:
            response = await asyncio.to_thread(
                _get_p115().clouddownload_task_add_url,
                {"url": magnet, "wp_path_id": directory_id},
            )
        except Exception:
            return {"status": "UNKNOWN_RESULT"}
        if isinstance(response, dict) and response.get("state") is True:
            return {"status": "CONFIRMED_SUCCESS"}
        if isinstance(response, dict) and response.get("state") is False:
            return {"status": "CONFIRMED_FAILURE"}
        return {"status": "UNKNOWN_RESULT"}

    # ---------- QR码登录（委托 p115client，比手写私有接口稳） ----------
    async def qrcode_start(self) -> dict:
        """获取二维码，返回 {uid, qr_image, time, sign}。

        实测 p115client 的 login_qrcode(uid) 只返回一张115自用的小预览图（~165x165，
        379字节，扫不出登录负载）。真正要扫的内容在 token 响应里：
            data.qrcode = "https://115.com/scan/dg-<uid>"
        本地把这个 URL 自己渲染成二维码即可（115 手机 App 用这个 URL 识别）。
        """
        from p115client import P115Client
        import qrcode
        from io import BytesIO

        def _do():
            tok = P115Client.login_qrcode_token(app="web")
            d = tok.get("data") or {}
            uid = d.get("uid")
            if not uid:
                raise Cloud115Error(f"获取二维码 token 失败: {tok}")
            scan_url = d.get("qrcode") or f"https://115.com/scan/dg-{uid}"
            # 本地生成可扫的二维码
            img = qrcode.make(scan_url)
            buf = BytesIO()
            img.save(buf, format="PNG")
            return uid, d.get("time"), d.get("sign"), buf.getvalue()

        try:
            uid, time_, sign, img_bytes = await asyncio.to_thread(_do)
        except Exception as e:
            raise Cloud115Error(f"获取二维码失败: {e}")
        return {
            "uid": uid,
            "time": time_,
            "sign": sign,
            "qr_image": f"data:image/png;base64,{base64.b64encode(img_bytes).decode()}",
        }

    async def qrcode_poll(self, uid: str, time_: int, sign: str) -> dict:
        """轮询扫码状态。只返回状态；Cookie 仅在服务端持久化。
        status: -2 已取消 / -1 已过期 / 0 待扫描 / 1 已扫描待确认 / 2 已确认（登录成功）
        """
        from p115client import P115Client
        payload = {"uid": uid, "time": time_, "sign": sign}

        def _status():
            return P115Client.login_qrcode_scan_status(payload)

        try:
            data = await asyncio.to_thread(_status)
        except Exception as e:
            raise Cloud115Error(f"查询扫码状态失败: {e}")
        status = (data.get("data") or {}).get("status", 0)
        if status == 2:
            await self._qrcode_result(uid)
            # Cookie 只保存在服务端；前端只需要知道登录已完成。
            return {"status": 2}
        return {"status": status}

    async def _qrcode_result(self, uid: str) -> str:
        """扫码确认后，用 uid 换取登录 cookie 并保存。"""
        from p115client import P115Client

        def _do():
            return P115Client.login_qrcode_scan_result(uid, app="web")

        try:
            data = await asyncio.to_thread(_do)
        except Exception as e:
            raise Cloud115Error(f"换取 cookie 失败: {e}")
        d = data.get("data") or {}
        cookies = d.get("cookie") or {}
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if not cookie_str:
            raise Cloud115Error(f"未能从响应中解析 cookie: {data}")
        self._save_cookie(cookie_str)
        return cookie_str

    # ---------- 目录 / 文件 ----------
    async def list_dir(self, cid: str, limit: int = 1000, offset: int = 0,
                       only_video: bool = False) -> dict:
        """列出目录内容。返回原始115响应。"""
        params = {
            "aid": 1, "cid": cid, "limit": limit, "offset": offset,
            "show_dir": 1, "o": "user_ptime", "asc": 0, "natsort": 1,
        }
        if only_video:
            params["type"] = 4
        client = self._shared_http()
        last_error: BaseException | None = None
        for attempt in range(3):
            response: httpx.Response | None = None
            try:
                async with self._read_sem:
                    response = await client.get(
                        FILES_URL,
                        params=params,
                        headers=self._cookie_headers(),
                    )
            except httpx.TransportError as error:
                last_error = error
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = Cloud115Error(
                        f"列目录暂时失败(HTTP {response.status_code})")
                elif response.status_code != 200:
                    raise Cloud115Error(
                        f"列目录失败(HTTP {response.status_code})，cookie可能失效")
                else:
                    try:
                        data = response.json()
                    except Exception as error:
                        raise Cloud115Error(
                            "列目录返回非JSON，cookie可能失效") from error
                    # state=False 是 115 的错误信号（cookie 半失效/受限时可能返回它且带空 data）。
                    # 必须显式当作错误抛出，否则会退化成"该目录为空"，进而让去重把该模特全部
                    # 视频判成 missing → 整库重复下载。
                    if data.get("state") is False:
                        raise Cloud115Error(
                            f"列目录失败(state=False): {data.get('error') or data}")
                    if "data" not in data:
                        raise Cloud115Error(f"列目录失败: {data}")
                    return data
            if attempt < 2:
                await asyncio.sleep(self._retry_delay(response, attempt))
        message = str(last_error).strip() if last_error else "未知网络错误"
        raise Cloud115Error(f"列目录重试后仍失败: {message}") from last_error

    async def list_all_files(self, cid: str, only_video: bool = True) -> list[dict]:
        """分页列出某目录下全部文件（自动翻页）。"""
        results: list[dict] = []
        offset = 0
        page = 1000
        while True:
            data = await self.list_dir(cid, limit=page, offset=offset, only_video=only_video)
            batch = data.get("data", [])
            results.extend(batch)
            total = data.get("count", 0)
            offset += len(batch)
            if not batch or offset >= total:
                break
            await asyncio.sleep(0.3)  # 轻微限速
        return results

    async def get_file_info(self, fid: str) -> dict | None:
        """按 115 文件 ID 查询权威元数据；尚未可读时返回 None。"""
        fid = str(fid or "").strip()
        if not fid:
            raise Cloud115Error("查询文件信息失败：fid 为空")
        try:
            client = _get_p115()
            resp = await client.fs_file(fid, async_=True)
        except Exception as e:
            raise Cloud115Error(f"查询 115 文件信息失败: {e}") from e
        if not isinstance(resp, dict) or resp.get("state") is False:
            raise Cloud115Error(f"查询 115 文件信息失败: {resp}")
        rows = resp.get("data")
        if not isinstance(rows, list) or not rows:
            return None
        return rows[0] if isinstance(rows[0], dict) else None

    async def delete_files(self, fids: list) -> dict:
        """删除 115 上的文件（按 fid，可批量）。委托 p115client.fs_delete。

        115 删除接口要求**不可并发**（见 p115client 文档），故一次调用即传入整批 fid，
        不要在外层并发调用本方法。文件（非目录）删除在接口响应时即已完成。
        用于清理历史污染文件——务必只传经污染清单核对过的 fid。
        """
        if not fids:
            return {"state": True, "deleted": 0}
        try:
            client = _get_p115()
        except Exception as e:
            raise Cloud115Error(f"p115client 初始化失败: {e}")
        # fs_delete 支持一次传多个 fid（逗号分隔或可迭代）
        res = await client.fs_delete([str(f) for f in fids], async_=True)
        # 校验返回：115 可能不抛异常而返回 {"state": False, ...}（fid 无效/无权限）。
        # 若不校验，调用方会误以为删除成功而继续清库，造成"网盘还在、库记录已删"的
        # 不一致。故 state 非真时抛错，让调用方据此不清库。
        if isinstance(res, dict) and res.get("state") is False:
            raise Cloud115Error(f"115 删除失败: {res.get('error') or res}")
        return res

    async def find_subdir(self, parent_cid: str, name: str) -> str | None:
        """在父目录下按名字查找子文件夹，返回cid或None。

        归一化匹配：忽略大小写与首尾空白、并把内部连续空白折叠为单空格。
        PH 爬到的模特名与 115 文件夹名常有大小写/空格差异，精确匹配会漏判，
        导致整个模特被当成"115 一个都没有"——去重静默失准，故这里放宽匹配。
        """
        parent_cid = str(parent_cid or "").strip()
        if not parent_cid:
            raise Cloud115Error("未配置 VIDEO_HUB_CID，拒绝从115根目录查找模特文件夹")
        target = _norm_name(name)
        offset = 0
        while True:
            data = await self.list_dir(parent_cid, limit=1000, offset=offset, only_video=False)
            for item in data.get("data", []):
                # 文件夹有 cid 且无 fid
                if item.get("cid") and not item.get("fid") and _norm_name(item.get("n", "")) == target:
                    return str(item["cid"])
            total = data.get("count", 0)
            offset += len(data.get("data", []))
            if offset >= total or not data.get("data"):
                break
        return None

    async def list_model_dirs(self) -> list[dict]:
        """列出 VIDEO HUB 下所有模特文件夹 [{name, cid}]。"""
        if not str(settings.video_hub_cid or "").strip():
            raise Cloud115Error("未配置 VIDEO_HUB_CID")
        results = []
        offset = 0
        while True:
            data = await self.list_dir(settings.video_hub_cid, limit=1000, offset=offset, only_video=False)
            for item in data.get("data", []):
                if item.get("cid") and not item.get("fid"):
                    results.append({"name": item["n"], "cid": str(item["cid"])})
            total = data.get("count", 0)
            offset += len(data.get("data", []))
            if offset >= total or not data.get("data"):
                break
        return results

    async def mkdir(self, parent_cid: str, name: str) -> str:
        """在父目录下创建文件夹，返回新cid。"""
        parent_cid = str(parent_cid or "").strip()
        if not parent_cid:
            raise Cloud115Error("未配置 VIDEO_HUB_CID，拒绝在115根目录创建文件夹")
        try:
            response = await self._shared_http().post(
                MKDIR_URL,
                data={"pid": parent_cid, "cname": name},
                headers=self._cookie_headers(),
            )
            data = response.json()
        except (httpx.TransportError, ValueError) as error:
            # POST 可能已到达 115，只是响应丢失。先查目录，禁止盲目重试创建。
            try:
                existing = await self.find_subdir(parent_cid, name)
            except Exception as confirm_error:
                raise Cloud115Error(
                    f"创建目录结果不确定，且复查失败: {confirm_error}") from error
            if existing:
                return existing
            raise Cloud115Error(f"创建目录结果不确定: {error}") from error
        if response.status_code >= 500:
            existing = await self.find_subdir(parent_cid, name)
            if existing:
                return existing
            raise Cloud115Error(f"创建目录失败(HTTP {response.status_code})")
        if not 200 <= response.status_code < 300 or not data.get("state"):
            raise Cloud115Error(f"创建目录失败: {data}")
        cid = str(data.get("cid") or data.get("file_id") or "").strip()
        if cid:
            return cid
        existing = await self.find_subdir(parent_cid, name)
        if existing:
            return existing
        raise Cloud115Error("创建目录返回成功但缺少 CID")

    async def ensure_model_dir(self, model_name: str) -> str:
        """确保 VIDEO HUB 下存在该模特文件夹，返回cid。"""
        parent_cid = str(settings.video_hub_cid or "").strip()
        if not parent_cid:
            raise Cloud115Error("未配置 VIDEO_HUB_CID")
        key = (parent_cid, _norm_name(model_name))
        lock = self._model_dir_locks.setdefault(key, asyncio.Lock())
        async with lock:
            # 在锁内再次查找，避免两个并发上传同时创建同名目录。
            cid = await self.find_subdir(parent_cid, model_name)
            if cid:
                return cid
            return await self.mkdir(parent_cid, model_name)

    # ---------- 上传 ----------
    @staticmethod
    def file_sha1(path: str) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest().upper()

    async def probe_upload_access(self) -> str:
        """无副作用地确认 115 是否按存储桶策略拒绝本机上传，见 ``probe_upload_access``。"""
        return await probe_upload_access()

    async def upload_file(self, local_path: str, target_cid: str, filename: str | None = None,
                          on_progress=None) -> dict:
        """上传文件到指定目录。

        实际上传由 p115client 处理：
          - 自动算 SHA1，先走 init 接口尝试秒传
          - 命中秒传 -> 立即完成；未命中 -> OSS 分片上传
        本方法包一层 asyncio.to_thread 把同步调用挪出事件循环。

        on_progress: async 回调 (sent_bytes, total_bytes) -> 用于上报上传字节进度。
                     秒传命中时不会触发（直接返回结果）。
        """
        filename = filename or os.path.basename(local_path)
        try:
            size = await asyncio.to_thread(os.path.getsize, local_path)
        except (FileNotFoundError, NotADirectoryError):
            raise Cloud115Error(f"本地文件不存在: {local_path}")
        except OSError as error:
            raise Cloud115Error(f"无法读取本地文件大小: {error}") from error
        try:
            client = _get_p115()
        except Exception as e:
            raise Cloud115Error(f"p115client 初始化失败: {e}")

        # 把异步回调桥到同步 reporthook。
        # 注意：p115oss 的 reporthook(n) 传的是**每个分片的字节数（增量）**，
        # 不是累计值，所以这里要自己累加。生产者侧最多每 200ms 安排一次
        # call_soon_threadsafe，避免 64KiB 分片把事件循环灌入数万条 callback。
        loop = asyncio.get_running_loop()
        progress_q: asyncio.Queue | None = (
            asyncio.Queue(maxsize=1) if on_progress else None)
        progress_lock = threading.Lock()
        sent_total = 0
        signal_pending = False
        last_signal_at = 0.0

        def _enqueue_latest():
            nonlocal signal_pending
            with progress_lock:
                sent = sent_total
                signal_pending = False
            if progress_q is None:
                return
            if progress_q.full():
                try:
                    progress_q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            progress_q.put_nowait(sent)

        def _reporthook(chunk: int):
            nonlocal sent_total, signal_pending, last_signal_at
            if progress_q is None:
                return
            now = time.monotonic()
            should_signal = False
            with progress_lock:
                sent_total += max(0, int(chunk or 0))
                if (not signal_pending and (
                        now - last_signal_at >= 0.2 or sent_total >= size)):
                    signal_pending = True
                    last_signal_at = now
                    should_signal = True
            if not should_signal:
                return
            try:
                loop.call_soon_threadsafe(_enqueue_latest)
            except RuntimeError:
                with progress_lock:
                    signal_pending = False

        def _do():
            pid: int | str = target_cid
            try:
                pid = int(target_cid)
            except (TypeError, ValueError):
                pass
            for init_attempt in range(2):
                try:
                    upload_kwargs = {
                        "pid": pid,
                        "filename": filename,
                    }
                    if on_progress:
                        upload_kwargs["reporthook"] = _reporthook
                    return client.upload_file(
                        local_path,
                        **upload_kwargs,
                    )
                except Exception as error:
                    # 已确认失败发生在上传初始化响应解码，且还没有 upload_id、callback
                    # 或已发送字节时，重放一次初始化是安全的；其它异常继续交给任务级
                    # 有限重试和二次去重，避免重复 OSS 传输。
                    if (init_attempt == 0 and sent_total == 0
                            and _is_truncated_upload_init_error(error)):
                        time.sleep(1)
                        continue
                    raise
            raise RuntimeError("上传初始化重试流程异常结束")

        last_pct = -1.0
        last_delivered = 0
        delivery_lock = asyncio.Lock()

        async def _deliver_progress(sent: int, *, force: bool = False) -> None:
            nonlocal last_pct, last_delivered
            if not on_progress or size <= 0:
                return
            sent = min(size, max(0, int(sent)))
            async with delivery_lock:
                if sent <= last_delivered:
                    return
                pct = round(min(100.0, sent / size * 100), 1)
                if not force and pct - last_pct < 1.0:
                    return
                last_pct = pct
                last_delivered = sent
                try:
                    await on_progress(sent, size)
                except Exception:
                    pass

        async def _drain_progress():
            """消费单槽进度，只转发最新累计值。"""
            assert progress_q is not None
            while True:
                try:
                    sent = await asyncio.wait_for(progress_q.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    if upload_task.done():
                        return
                    continue
                await _deliver_progress(sent)

        upload_task = asyncio.create_task(asyncio.to_thread(_do))
        drain_task = asyncio.create_task(_drain_progress()) if on_progress else None
        try:
            while True:
                try:
                    resp = await asyncio.shield(upload_task)
                    break
                except asyncio.CancelledError:
                    # 同步上传线程不能被 asyncio 取消。取消可能在关机或重复操作时
                    # 到达多次；每次都消费本次取消并继续 shield，直到线程真实返回。
                    current = asyncio.current_task()
                    if current is not None and hasattr(current, "uncancel"):
                        current.uncancel()
        except Exception as e:
            raise UploadTransferError(f"上传失败: {_safe_exception_chain(e)}") from e
        finally:
            if on_progress:
                with progress_lock:
                    final_sent = sent_total
                # 即使最后一个分片落在 200ms 节流窗口内，也必须交付最终累计值。
                await _deliver_progress(final_sent, force=True)
            if drain_task:
                drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)

        # 实测两种成功响应：
        #   普通上传(OSS): {'state': True, 'code': 0, 'data': {file_id, sha1, ...}}
        #   秒传命中     : {'state': True, 'status': 2, 'reuse': True, 'data': {...}}
        #   失败        : {'state': False, 'error': '...', ...}
        if not isinstance(resp, dict) or not resp.get("state"):
            raise Cloud115Error(
                f"115返回上传失败: {redact_sensitive_error_text(resp)}")
        file_id = _upload_file_id(resp)
        if not file_id:
            raise Cloud115Error(
                "115上传成功响应缺少文件ID，拒绝误判完成: "
                f"{redact_sensitive_error_text(resp)}")
        rapid = bool(resp.get("reuse"))
        return {
            "ok": True,
            "mode": "rapid" if rapid else "oss",
            "file_id": file_id,
            "raw": resp,
        }


# ---------- p115client 单例 ----------
_p115_singleton = None
_upload_init_patched = False


def _p115_client_class():
    """加载上传客户端，并把“没安装”和“依赖版本打架”分成可诊断错误。"""
    try:
        module = import_module("p115client")
    except ModuleNotFoundError as e:
        if e.name == "p115client":
            raise Cloud115Error(
                "未安装 p115client，请按 requirements.txt 安装依赖") from e
        raise Cloud115Error(
            f"p115client 依赖缺失: {e.name or type(e).__name__}") from e
    except ImportError as e:
        # 旧逻辑把任何 ImportError 都说成“未安装 p115client”，会掩盖同作者
        # 子包 API 不兼容（例如 asynctools.to_list 被移除）的真实根因。
        raise Cloud115Error(f"p115client 依赖版本不兼容: {e}") from e
    try:
        return module.P115Client
    except AttributeError as e:
        raise Cloud115Error("p115client 版本不兼容: 缺少 P115Client") from e


def _patch_p115oss_upload_init():
    """修正 p115oss 0.1.0.3 upload_init 的 appversion bug。

    上游源码里 upload_init 先把默认 appversion 合入 data，却又用
    `appversion = payload["appversion"]` 从原始 payload 取值——payload 通常没有该键，
    直接 KeyError 抛 MultipartUploadAbort，导致所有 OSS 上传（非秒传）初始化即失败。
    这里包一层，缺失时给 payload 补默认 appversion 后再调原函数。幂等，只打一次。
    """
    global _upload_init_patched
    if _upload_init_patched:
        return
    try:
        import functools
        import p115oss.api as _api
        import p115oss.upload as _up
    except ImportError:
        return   # 未装 p115oss 时跳过（上传本就不可用）
    _orig = _api.upload_init

    @functools.wraps(_orig)
    def _patched(payload, *, async_=False, **kw):
        if isinstance(payload, dict) and "appversion" not in payload:
            payload = {**payload, "appversion": "36.2.28"}
        return _orig(payload, async_=async_, **kw)

    _api.upload_init = _patched
    # upload.py 里以 `from .api import upload_init` 绑定了名字，需同步替换
    if hasattr(_up, "upload_init"):
        _up.upload_init = _patched
    _upload_init_patched = True


def _get_p115():
    """惰性创建 P115Client；按当前 cookie 文件复用一个实例。"""
    global _p115_singleton
    P115Client = _p115_client_class()
    _patch_p115oss_upload_init()      # 应用上传初始化补丁（幂等）
    if _p115_singleton is not None:
        return _p115_singleton
    if not os.path.exists(settings.cookie_file):
        raise Cloud115Error("未登录115：找不到 cookie 文件")
    # P115Client 的 cookies 参数：传 str 会当成 cookie 字符串解析；要传 PathLike
    # 才会被识别成文件路径。包成 pathlib.Path。
    from pathlib import Path
    _p115_singleton = P115Client(Path(settings.cookie_file), console_qrcode=False)
    return _p115_singleton


def _reset_p115():
    """cookie 变更后重置单例。"""
    global _p115_singleton
    _p115_singleton = None


# 单例
cloud = Cloud115Client()
