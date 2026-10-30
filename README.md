# Coffer

Coffer（宝匣）是一个自托管的本地媒体同步工具：从站点榜单或发现页收集视频清单 → 排除网盘里已有的上传者 → 对比目录 → 只下载缺失部分 → 二次去重 → 上传归档，并持续巡查补齐。

> 内部代号 PH-115：代码里的 `PH115_*` 前缀与 `data/` 目录布局保持原名，不影响使用。

## 开发者文档

- [架构说明](./ARCHITECTURE.md)：总览图、目录与模块职责、任务状态机、数据模型、改哪里
- [设计系统](./design-system/MASTER.md)：视觉层次、样式顺序与弹窗可访问性约束

## 功能

- **115模特页**：`/library` 是115 `VIDEO_HUB` 的独立权威视图，提供已绑定、待绑定和异常目录；自动巡查、自动下载与Bark通知只在此页设置。绑定和解绑不会删除115文件、模特、视频或历史任务。
- **模特榜单与精确查找**：恢复传统PH榜单工作流。输入榜单URL可批量获取并完整同步；输入精确名字或账号可添加未入榜模特。服务端依据115目录索引、可信目录名和历史 `cloud_cid` 硬过滤，115已有模特不会进入普通榜单。每张卡显示头像和最多4张清晰视频封面，并保留同步、下载缺失、选择视频和删除操作。
- **视频发现页**：覆盖主页、推荐视频、最热门、最多次观看、最高分、热门自制、短片、片单、频道、随机、最新和观众之选。获取后按旧版流程保存上传者并同步完整作品，但服务端会先排除115已有模特，防止重复同步和下载。
- **审美探索页**：不需要手动选择。以你 115 里已收藏的模特为正例、还没收藏的候选为背景，用本地 SigLIP2 图像塔提取头像与首张封面特征，按“比起普通候选更像你的收藏”的方向排序；打开页面时若还没有结果、收藏有增减或结果超过一天，就自动在后台重新计算（约一分钟）。发布前用你过去的“符合 / 不符合”评价做留出检验（评价过的人不参与计算方向），AUC ≥ 0.65 才展示；评价每类不足 8 位时照常展示但标为尚未检验。每批保存前 96 位，每组 24 位，可“换一批”；每张卡说明它最像你收藏的哪一位。“符合 / 不符合”是可选的：符合的进入“我的符合”工作区，不符合的以后不再推荐，二者都不改变排序方向。封面补齐单张最多等 20 秒、超时冷却 6 小时；推荐发布后才进入 115 的模特会从当前结果隐藏。早期的两两比较模式仍保留在后端供脚本与测试使用，页面不再显示。名单加载和 JSON/CSV 导出纯只读；只有用户勾选模特、看到明确缺失总数并通过确认后才加入下载，完成后沿用既有自动上传和上传前二次去重。
- **清晰四封面**：普通模特、115模特和视频发现共用轮播组件，支持按钮、键盘、鼠标拖拽、触摸滑动、懒加载、时长标签和失败占位。优先读取本地640×360 WebP，失败时回退历史缩略图；列表读取物化预览，不随页面加载扫描全量Video。
- **后台补封面不打扰浏览**：缺封面的模特会进入一个全局低并发队列，同一模特不会因刷新页面或WebSocket消息重复抓取；失败后进入冷却，不会让页面长期显示刷新或持续叠加网络请求。
- **头像本地资产化**：发现榜单缺少头像时必须读取模特个人主页；头像由后端下载、解码校验并统一转换为 256×256 WebP，按 SHA-256 保存在 `data/avatars/`。前端只访问本地头像 API，不直连带时效签名的 PH CDN。
- **下载任务页**：把选中的视频加入下载队列（最多 5 个并发），支持全部下载/停止/继续、删除任务，实时进度。“可重试”表示允许再次尝试，不保证来源已经恢复。清单或分片 410 在即时重试后按 15 分钟、1 小时、6 小时间隔有限复查，等待期间不占下载槽；额度耗尽后留待逐项处理。其它确定性的完整性异常保留供人工审查。任务按 `model_id + viewkey` 隔离，合作视频可分别进入不同模特目录。
- **上传任务页**：下载完成自动流转，上传前**二次去重**，115 已有则跳过，否则上传（最多 2 个并发，优先秒传）。同步上传一旦开始不能安全中断，删除/暂停会明确保留运行项直到完成。
- **Bark 主动通知**：可在115模特页保存官方或自建 Bark 地址，按需接收巡查汇总、新缺失、批次完成和异常。逐条下载/上传通知默认关闭；开启后也会在短窗口内自动合并，避免大批任务同时完成时轰炸手机。

## 去重策略

对每个 PH 视频，判断是否已存在于 115 该模特文件夹：

1. **强匹配**：115 文件名包含该视频的 viewkey（如 `ph5f555c258b565`）→ 已存在
2. **弱匹配**：115 存在文件时长 == PH 视频时长（±3 秒）→ 可能已存在（标黄，默认不下载）
3. 否则 → 缺失，需下载

下载后新文件统一命名为 `标题 [viewkey].mp4`，保证以后强匹配 100% 可靠。
- 第一次去重：加入下载队列前
- 第二次去重：上传前（下载期间 115 可能已新增同一视频）

## 技术栈

- 后端：Python + FastAPI + SQLAlchemy（异步 SQLite）+ httpx + yt-dlp
- 前端：Vue 3 + Vite
- 本地视觉：ONNX Runtime + SigLIP2 INT8 图像塔；只读取本机头像/封面缓存
- 115：QR 码扫码登录 / Cookie 登录（p115client 处理上传签名；其兼容依赖 `python-concurrenttools==0.1.4` 已固定）
- 下载：固定使用 `yt-dlp==2026.6.9` 原生 HLS（最高画质）；ffmpeg 仅做下载后封装，ffprobe 校验成品

## 准备

1. 安装 [ffmpeg](https://ffmpeg.org/)（需同时包含 ffprobe）并加入 PATH；二者仅用于后处理与校验
2. 本地代理（爬 PH 用），默认 `http://127.0.0.1:7890`，改 `.env`
3. 已验收的源码/发行环境为 Windows x64、Python 3.14、Node 24；完整依赖版本由 lock 文件固定

审美探索还需要 `models/visual/siglip2-vision-int8.onnx`。该文件约 101 MiB，受 GitHub
单文件限制不会进入仓库；同目录的 `siglip2-int8.json` 固定了文件名、SHA-256 和量化
质量门槛。源码运行与本地测试都会使用同一份资产，哈希不一致时功能会明确失败关闭。

## 一键部署（Windows）

装好 Python 3.14、Node 24 以及 `ffmpeg`/`ffprobe` 之后，把下面整行粘进 PowerShell，一次完成克隆、建虚拟环境、装依赖、生成 `.env` 并启动后端：

```powershell
git clone <仓库地址> coffer; cd coffer; python -m venv .venv; .\.venv\Scripts\python -m pip install --no-deps -r requirements-release.lock; .\.venv\Scripts\python -m pip check; Copy-Item .env.example .env; .\.venv\Scripts\python run.py
```

没有装 Git 时，用这行直接下载解压：

```powershell
Invoke-WebRequest -Uri <仓库地址>/archive/refs/heads/main.zip -OutFile coffer.zip; Expand-Archive coffer.zip -DestinationPath .; Rename-Item coffer-main coffer; cd coffer
```

之后每次启动只要：

```powershell
cd coffer; .\.venv\Scripts\python run.py
```

要连前端开发服务器一起起，或者需要 start/stop/status：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev_launcher.ps1 -Action start
```

## 运行

后端：
```bash
cd <repo>
python -m venv .venv && .venv\Scripts\activate
pip install --no-deps -r requirements-release.lock
python -m pip check
copy .env.example .env   # 按需修改代理/路径
python run.py            # http://127.0.0.1:8000
```

前端：
```bash
cd <repo>\frontend
npm ci
npm run dev              # http://127.0.0.1:5173
```

浏览器打开 `http://127.0.0.1:5173`，先点右上角「登录115」（扫码或粘贴 Cookie），再从「模特」榜单或「视频」发现页开始。

右上角显示实时连接状态。断线重连或从后台返回后，当前页面会自动补读最新状态；下载字节进度不会被更旧的数据库快照覆盖。点击「诊断」可下载脱敏运行指标，包含路由/SQL 类型耗时、事件循环延迟与资源计数，不包含 Cookie、媒体记录或日志正文。日志保留最多四个约 5 MiB 文件。

前端回归：`npm test`、`npx playwright install chromium`、`npm run test:browser`、`npm run build:check`。浏览器回归使用隔离数据，桌面与 390px 布局同时验收。需要用本机数据检查生产前端时，先构建，再设置 `PH115_READONLY_QA=1` 运行 `npm run test:browser -- live-readonly`；该模式拒绝写入并替换媒体图片和展示名称。

### 手机远程访问

远程部署应通过 HTTPS 反向代理；本机应用仍只监听 `127.0.0.1`，SSH 远端转发也只绑定服务器回环地址。外网使用应用内登录页，Nginx 只在登录接口用既有 `.htpasswd` 校验一次，成功后由 HttpOnly 签名 Cookie 维持会话。域名、账号和访问凭据属于私有运维配置，不得提交到仓库。

本机自动化隧道（可选）：Windows 计划任务用 `wscript.exe` 包装器后台拉起 SSH 反向隧道，断线自动重连、不弹出终端窗口；对应的 Nginx 片段见 `deploy/remote-access/nginx-ph115-location.conf`，顶部注释写了用法与安全前提。隧道脚本、密钥路径、服务器地址属于私有运维配置，按需自行编写，不随仓库分发。

下载/上传任务列表默认只加载 200 条并返回完整状态统计，历史任务用状态+任务 ID 游标加载，避免队列状态变化导致 offset 分页漏项；API 同时启用 gzip。旧客户端不传 `limit` 时仍可获得完整列表。

模特列表默认每页加载 60 位，搜索与“榜单 / 缺失 / 进度 / 名称”排序都在服务端完成，并使用不透明游标翻页；页面 DOM 不再随全部模特数量增长。兼容调用不传 `limit` 时仍返回旧版全量结构。

## 测试

```powershell
python -m unittest discover -s tests -v
python tests\integration_native_hls.py
python -X utf8 tests\integration_composite_hls.py
Push-Location frontend
npm test
npm run build:check
Pop-Location
```

前端测试覆盖二维码请求生命周期、弹窗焦点、显式保存和按钮对比度；`build:check` 同时执行生产构建与 gzip 预算：入口 JavaScript ≤ 80 KiB、单个按需 chunk ≤ 130 KiB、全部 CSS 合计 ≤ 20 KiB。CI 还会执行已有的本地 HLS fixture 验收，不连接真实 PH/115 服务。

`integration_native_hls.py --full-stall` 会按默认 3 秒/60 秒阈值执行一次完整停滞验收，耗时约 1 分钟。

UI 维护先读 `design-system/MASTER.md`：颜色与尺寸令牌由 `frontend/src/styles/tokens.css` 定义，`style.css` 固定顺序导入九个样式文件。弹窗焦点和 Escape 共用 `modalStack`，只处理最上层可见弹窗；按钮顺序、禁用条件、下载确认与自动上传语义保持原有业务组件的约束。

历史污染清理必须走两阶段人工批准：先运行 `python -m scripts.scan_pollution`，再运行
`python -m scripts.clean_pollution` 生成候选计划；逐项核对
`data/private/pollution_delete_plan.json` 后，把明确批准的 fid 填入
`approved_fids`，最后才可运行 `python -m scripts.clean_pollution --execute`。旧版扫描报告、
`skipped` 任务、仅按模特名或 viewkey 猜测出的文件都会被拒绝。
扫描进度先按批追加到同目录 journal，完整结束后才原子压实正式 JSON；因此长扫描
中断可续跑，又不会随着历史结果增长反复重写整份报告。

历史版本回退若在 SQLite 中遗留当前代码已不认识的表或列，可先生成一份受支持
Schema 的独立副本：

```powershell
python -m scripts.rebuild_database --source data/data.db --output data/data.rebuilt.db
```

该命令通过 SQLite backup API 纳入 WAL，只复制当前 Schema 认识的表/列，并执行
行数、外键和完整性校验，同时输出 `.report.json`；它拒绝覆盖现有文件，也绝不会
替换源库。只有停止应用、另做可恢复备份并人工审阅报告后，才可在维护窗口手工切换。

## 配置（.env）

| 项 | 说明 | 默认 |
|---|---|---|
| `PROXY` | 爬 PH 的本地代理，空则不用 | `http://127.0.0.1:7890` |
| `DOWNLOAD_DIR` | 主下载目录 | `<repo>/downloads` |
| `DOWNLOAD_FALLBACK_DIRS` | 备用下载目录，多个路径用英文分号分隔；主目录空间不足时自动切换 | 空 |
| `AVATAR_DIR` | 校验并转换后的本地头像目录 | `data/avatars` |
| `AVATAR_FETCH_CONCURRENCY` | 头像抓取/回填并发 | 3 |
| `AVATAR_MAX_BYTES` | 单张头像源文件大小上限（字节） | 5242880 |
| `DOWNLOAD_PROXY` | 下载专用代理，空则直连；不会自动切换 | 空 |
| `DOWNLOAD_MAX_CONNECTIONS` | 所有 HLS 下载合计连接预算 | 15 |
| `DOWNLOAD_MAX_FRAGMENT_WORKERS_PER_TASK` | 单任务首轮分片线程上限 | 12 |
| `DOWNLOAD_MIN_FREE_SPACE_GB` | 每个下载盘的保留空间；所有目录都不足时只跑上传 | 20 |
| `DOWNLOAD_SOCKET_TIMEOUT` | 单次网络请求超时（秒） | 10 |
| `DOWNLOAD_WAITING_TIMEOUT` | 无字节后进入“等待数据”的阈值（秒） | 3 |
| `DOWNLOAD_STALL_TIMEOUT` | 无字节后终止本次尝试的阈值（秒） | 60 |
| `DOWNLOAD_FRAGMENT_RETRIES` | 单个 HLS 分片重试次数（间隔 1/2/4/8/8 秒，约能撑过 20 秒的短暂断流） | 5 |
| `DOWNLOAD_RESOLVE_TIMEOUT` | 重新解析媒体地址的超时（秒） | 90 |
| `MAX_CONCURRENT_DOWNLOADS` | 下载并发 | 5 |
| `MAX_CONCURRENT_UPLOADS` | 上传并发 | 2 |
| `MEDIA_PROCESSING_CONCURRENCY` | 本地 ffprobe / ffmpeg 处理并发；与网络下载槽分离 | 2 |
| `MODEL_SYNC_CONCURRENCY` | 榜单/发现页完整同步并发；跨批次共享上限 | 4 |
| `PATROL_SYNC_CONCURRENCY` | 自动巡查同时处理的上传者数 | 2 |
| `MAX_CONCURRENT_115_READS` | 全应用同时进行的 115 目录读取上限 | 6 |
| `MODEL_SYNC_TIMEOUT_SECONDS` | 单个已开始执行的完整模特同步总预算（秒）；排队时间不计入 | 180 |
| `VIDEO_HUB_CID` | 115 VIDEO HUB 目录 ID，必须填写自己的目录 | 空 |
| `DURATION_TOLERANCE` | 时长模糊匹配容差（秒） | 3 |
| `PATROL_ENABLED` | 首次启动时是否开启自动巡查 | true |
| `PATROL_INTERVAL_HOURS` | 首次启动时的巡查间隔（小时） | 6 |
| `PATROL_AUTO_DOWNLOAD` | 巡查发现缺失后是否自动进入流水线 | true |
| `NOTIFY_BARK_ENABLED` | 尚无页面运行时设置时是否启用 Bark | false |
| `NOTIFY_BARK_URL` | 官方或自建 Bark 完整推送地址，含私密 device_key | 空 |
| `TASTE_VISUAL_ENABLED` | 是否启用探索页本地视觉校准与结果生成 | true |
| `TASTE_VISUAL_MAX_THREADS` | ONNX 工作进程的 CPU 线程数 | 4 |
| `TASTE_VISUAL_BATCH_SIZE` | 单次视觉推理批量；限制为 8–16 | 12 |
| `TASTE_VISUAL_COVER_TIMEOUT` | 探索批处理中单张缺失封面的最长等待秒数 | 90 |

页面修改的巡查开关、间隔和 Bark 设置会保存到数据库的 `runtime_settings`，后续重启优先使用；
`.env` 中对应值只作为尚无运行时设置时的默认值。Bark 完整地址不会回显到浏览器或日志，
只显示末尾四位提示；测试推送无视事件开关，但仍需先保存有效地址。

下载任务在启动 worker 前会把选中的绝对路径写入数据库。同一任务的断点续传、上传和
上传后清理始终使用该路径；已有媒体断点不会被静默切到另一个盘。

榜单和视频发现页的批量同步由后端任务统一调度。同一批次会实时读取一次
`VIDEO HUB` 根目录并供未绑定上传者共同匹配，随后仍会逐个实时读取已匹配的上传者
目录；根目录结果不会跨批次缓存。进度同时通过 WebSocket 推送和任务状态接口恢复。

## 说明

- 网络下载只有 yt-dlp 原生 HLS 一条链路，不探测系统 yt-dlp/aria2，也不自动切换下载器或代理。
- 支持有明确时间段边界的 HLS 和独立音轨：逐段下载、校验后本地合并，验证媒体包数、时长及完整解码；不兼容的编码/音轨变化仍明确失败。fMP4 暂停后复用已完成时间段，未完成时间段重新下载。
- 首轮按总连接预算并行下载分片；第二次完整尝试减半，第三次降到最多 2 个分片线程，以兼顾吞吐和不稳定 CDN。
- 主下载盘空闲低于保留值时，新任务自动选择备用盘；所有下载目录都不足时才保持排队，上传成功释放空间后调度器会自动恢复。
- 非 HLS 响应会在下载前拒绝；成品必须经 ffmpeg 封装，并由 ffprobe 同时通过 MP4 格式、视频流和页面时长校验。
- 运行中的下载始终返回明确阶段：解析地址、下载中、等待数据、封装校验或重试；速度由真实字节增量计算。
- SQLite 使用 WAL 与有限写锁重试；下载字节进度实时走 WebSocket，数据库只按约 5 秒快照或阶段切换持久化，避免高频写入挤占任务状态。
- 视频快照使用版本化 SHA-256 摘要；内容未变化时只更新时间与计数，不再删除并重插全部视频。任务状态排序、调度、视频计数和115成员过滤均有对应索引；结构升级只在迁移版本变化时运行 `ANALYZE`，启动过程不执行 `VACUUM`。
- WebSocket 为每个客户端使用有界队列：高频进度只保留同一对象的最新值，终态可靠排队；慢客户端不会反向阻塞下载、上传或同步状态机。
- ffprobe 逐行扫描媒体包，不把完整包时间轴载入内存；本地扫描、封装、文件清理和进程回收移出事件循环，并由独立媒体并发门限保护磁盘与内存带宽。
- 115 为非官方 API，接口可能变动；列目录已用 `aps.115.com/natsort/files.php` 验证可用。
- 115 元数据请求复用 keep-alive 连接并受全局读取并发限制；`state=false`、非 JSON 和登录失效始终失败关闭，绝不会按空目录处理。
- PH 视频页解析依赖内嵌 `flashvars` JSON，站点改版可能需要调整 `scraper.py`。
- 榜单抓取只接受配置的 PH 域名；重定向和媒体清单会再次校验，拒绝本机/私网地址。
- 模特头像必须先成功落盘并通过哈希校验，榜单或精确查找才允许写入数据库；存量修复可重复执行 `python -m backend.avatar_repair`。
- 审美推荐默认是收藏模式：用历史评价做留出检验（AUC ≥ 0.65），候选特征覆盖率需 ≥ 80%，收藏里至少 20 位有完整特征；低于门槛只显示诊断，不发布结果。旧的比较模式保留“整组比较留出”验证（AUC ≥ 0.65、配对命中率 ≥ 0.65、至少 10 个正样本/10 组明确比较）。
- 探索页只写入比较、视觉特征、模型代次、排序结果和结果反馈；它不调用同步、下载或上传任务接口。候选封面首次缓存较慢，但进度与中间资产可续跑。正式 AUC/配对命中门禁始终只用成对样本计算；结果反馈仅在门禁通过后参与最终训练方向，避免把自我推荐结果混入验证集。
- 存量修复后仍无头像的记录可用 `python -m backend.avatar_audit --apply-status` 审计；前端只显示“源站无头像 / 主页已失效 / 源头像失效”等事实状态，不生成或热链兜底头像。
- 已人工确认旧账号跳转到规范主页时，使用 `python -m backend.avatar_repair --canonical-model-id <ID> --canonical-url <URL>` 修复展示名、主页和头像；历史 `ph_id` 保持不变，避免视频与任务关联断裂。
- QR 登录和 Cookie 登录结果均设置 `no-store`；115 Cookie 只保存在服务端本机文件，不返回浏览器。
- 启动器只管理自身记录且身份匹配的进程，不会按端口杀死其它程序。

## 许可

MIT，见 [LICENSE](./LICENSE)。本仓库只包含工具代码，不附带任何媒体内容；使用者需自行确保对处理内容的权利。
