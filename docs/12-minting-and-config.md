# 12 · 铸票系统与系统设置备忘

> **后续功能的运行时真源。** 出图、号池、计费、对外 API 都依赖这里。改代码前先改本文，再改 `docs/02-arp-core.md` / 面板。  
> 原理（六条件、408）见 [02-arp-core](./02-arp-core.md)。本文记录**当前已落地的约定、字段、谁消费、禁区**。

---

## 0. 两套配置，不许混

| 来源 | 存哪 | 管什么 | 谁读 |
|---|---|---|---|
| **配置中心** `system_settings.key='system_config'` | PG JSON，密钥 Fernet | 代理模板、铸票节奏、TTL、水位、计费、R2、限流、重试 | 面板、worker、采集器 bootstrap/lease、入池 TTL |
| **进程 env** `Settings` | `.env`（不入库、不提交） | `DATABASE_URL` / `REDIS_URL` / `APP_SECRET_KEY` / `FIELD_ENCRYPTION_KEY` / `ARP_INTERNAL_TOKEN` / `JWT_*` / `arp_key_prefix` / `submit_impersonate` / `TEST_COOKIE_FILE` | 启动进程 |

**铁律：铸票 TTL / 次数 / 浏览器数 / 间隔 / 代理 SID 模板，一律读配置中心，不读 env。**  
env 里的 `Settings.arp_ttl_seconds` / `arp_max_uses` 只给 smoke 和默认值；**运行时池用 ConfigStore**。配置中心 TTL 范围 30–600；env Settings 仍是 30–240，不要拿 env 当面板值。

保存：`PUT /api/admin/config` 全量文档，`version+1`，Redis `PUBLISH firefly:config:changed`。进程内缓存约 5s。密钥字段省略或回传掩码 = 保持原值。

脱敏：面板永不回代理密码、CB License 明文、R2 secret、ARP token、Cookie、AT。日志只留账号 hash / `egress_id`。

---

## 1. 三件资源怎么配在一起

出图一次同时要：

1. **ARP bundle**（铸票系统）— 风控票 + 铸票出口 + 铸票浏览器身份，不可拆。
2. **上游账号凭据**（号池）— `normal` 保存加密 FF-iOS device grant，`alpha` 保存加密
   Cookie；短期 AT 现换现用，**AT 不入库**。
3. **系统设置里的代理模板** — 采集器 lease 展开 sticky URL；提交必须用 bundle 里那条，一个字符都不能改。

```
配置中心.proxy  ──lease──►  sticky proxy_url
采集器 148 浏览器 ──铸──►  token(sid/ark/bfp/ftr) + ua/ch
                    ──POST──► Redis ARP 池（PX=TTL，max_uses≤10）
号池 Cookie ──每次请求──► clio access_token（内存即弃）
worker：checkout 票 + checkout 号 + 现换 AT + curl_cffi 提交（出口=铸票出口）
```

**铸票出口 == 提交出口 == 铸票身份。** 拆开必 408/403。

---

## 2. 系统设置字段（按面板 Tab）

### 2.1 通用

| 字段 | 默认 | 范围 | 谁用 | 语义 |
|---|---|---|---|---|
| `announcement` | `""` | ≤160 | 面板 | 公告 |
| `sync_timeout_seconds` | 180 | 10–600 | 同步生成 API | 超时 **504** + `task_id`，任务继续。不是上游 408 |
| `generation_timeout_seconds` | 300 | 30–1800 | worker 整单 | 从任务 `created_at` 起算（含排队等号/等票）。超过判失败并退款，停止重试。看门狗扫尾，**不依赖 Redis 队列里还有没有这条任务**（worker 重启后卡住的 running 也会停）。 |
| `default_rate_limit` | 60 | 1–10000 | 新用户 | 次/分钟，超 429。用户管理可覆盖 |
| `user_concurrency_default` | 2 | 1–200 | gate | 同时 queued+running 上限。与账号侧并发是两回事 |
| `worker_concurrency` | 32 | 1–200 | worker | 进程内同时 `process_job` 数。要跑满用户并发必须 ≥ 该值 |

### 2.2 代理（住宅 sticky）

供应商模板（ArxLabs 风格），**username 只存供应商前缀**，禁止已展开的 `{user}-region-CC-sid-SID-t-N`。

展开（`build_session`，每次 lease 新 SID）：

```
{scheme}://{username}-region-{CC}-sid-{SID}-t-{sticky_minutes}:{password}@{host}:{port}
```

| 字段 | 默认 | 范围 | 语义 |
|---|---|---|---|
| `proxy.scheme` | `http` | `http` / `socks5` | 落库可以是 `socks5`；**写出 URL 时变成 `socks5h`**（DNS 必须走代理，否则 sticky 是假的） |
| `proxy.host` / `port` | `""` / 3010 | port 1–65535 | |
| `proxy.username` | `""` | 前缀 | 校验拒绝二次展开 |
| `proxy.password` | 只写 | Fernet → `password_enc` | 面板只见 `password_set` |
| `proxy.country` | `SG` | 自动大写 | 进 username 的 `-region-` |
| `proxy.sticky_minutes` | 10 | 1–120 | **必须 `×60 ≥ arp_ttl_seconds + 60`**，否则票还在池里 IP 已漂 |
| `proxy.sid_length` | 8 | 4–32 | SID 字母表不含 0/O/1/I/l |
| `proxy.upload_via_proxy` | `false` | bool | **传图**（参考图 `POST /v2/storage/image`）是否走住宅 sticky。默认直连；开启后用当前 ARP 的 `proxy_url`。**下图始终直连** |

`proxy_ready` = host + username + 已存密码。未就绪：采集器不能 lease，铸票页提示「面板代理未配置」。  
探测：`POST /api/admin/config/proxy/test`，服务端自己 `build_session`，返回 `proxy_masked` / `egress_ip`，不回密码。

采集器 **禁止自己拼 username**。文件 `ARP_PROXY_FILE` 仅面板代理未配时的紧急 override，且必须是**已展开** URL。

### 2.3 上游调度 · 铸币工厂（铸票核心）

| 字段 | 默认 | 范围 | 谁用 | 语义 |
|---|---|---|---|---|
| `mint_browser_count` | 2 | **1–8** | harvester supervisor | 并行 **热浏览器路数**，不是票数。20 并发 1–2 路就够，不要堆到 8 |
| `mint_interval_seconds` | 10 | 1–300 | harvester | **一轮 BATCH 铸完之后**的空闲等待。**必须 `< arp_ttl_seconds`** |
| `arp_ttl_seconds` | 120 | 30–600 | Redis `PX`、lease 校验 sticky | 到期自动销毁。宁早丢（实测票寿命 2–4min） |
| `arp_max_uses` | 10 | **1–10 硬顶** | 入池 / checkout / 面板 | 同一张票可被多账号复用次数。DB 里旧值 >10 加载时钳到 10；入池再钳一次 |
| `arp_min_ready` | 3 | 1–100 | harvester 加速阈值 | 低于则跳过空闲、马上再铸一批 |
| `arp_pool_target` | 8 | **4–20** | harvester 停铸 | 常备池目标。`ready >= target` **停铸**。与浏览器数无关 |
| `arp_mint_batch` | 8 | 1–20 | harvester | 每个热 worker 一轮铸几枚（各带独立住宅出口）再空闲 |
| `cloakbrowser_license_key` | 只写 | Fernet | bootstrap 仍回传 | 开源 fingerprint-chromium **不需要**。字段保留兼容，采集器忽略 |

水位公式（`harvester/arp_harvester/rules.py`）：

- 停铸：`ready >= arp_pool_target`
- 加速（不等间隔）：`ready < arp_min_ready`
- 本轮批大小：`min(arp_mint_batch, target - ready)`

预计并发（铸票页）：`可用票据 × min(arp_max_uses, 10)`。真正喂饱 20 路靠「池里几枚新鲜票 + round-robin」，不是浏览器数。

### 2.4 上游调度 · 重试与容错（生成 worker / 号池）

| 字段 | 默认 | 范围 | 语义 |
|---|---|---|---|
| `retry_max_attempts` | 10 | 1–30 | worker 单任务**上游请求**上限。等号/等票排队不计入，跨重排队累计，到顶失败 |
| `retry_backoff_base_seconds` | 0.1 | 0.05–10 | `base * 2^attempt` |
| `account_park_seconds` | 300 | 30–3600 | **第 1 次** 408 冷却（测试 5 分钟）。到期看门狗改回可用，**不标 dead** |
| `account_park_second_seconds` | 600 | 30–7200 | **第 2 次** 408 冷却（测试 10 分钟） |
| `account_park_disable_after` | 3 | 2–10 | 连续几次 408 进入长休。成功 200 清零计数 |
| `account_park_disable_seconds` | 1800 | 120–86400 | 第 N 次起长休（测试 30 分钟）。到期看门狗拉回可用 |
| `account_408_observe_seconds` | 1800 | 0–86400 | 冷却后再观察这么久，单号只跑 1 路。0 关闭。成功出图结束观察 |
| `account_408_block_after` | 1 | 1–10 | 连续 408 达到次数后，禁止领「敏感模型」 |
| `account_408_block_models` | `gpt-image-2.5-flare,gpt-image-2.5-sunburst` | 面板 model_id 列表 | 有 408 计数时这些模型不领该号，先用其它模型探活 |
| `arp_bad_window_count` | 3 | 2–50 | 统计窗口内 408 次数达到则降速 |
| `arp_bad_window_seconds` | 60 | 10–600 | 坏窗口统计时长 |
| `arp_bad_window_pause_seconds` | 120 | 0–3600 | 降速时长：不再换号连打，单号 1 路。0 关闭 |
| `token_rotation` | `random` | random / round_robin / least_used | 号池取号策略（规划中的轮换；checkout 当前按 `last_used_at`） |
| `account_concurrency_normal` | 1 | 1–16 | 单号并发（普通分辨率）。实测安全值 4。全站并发 ≈ 可用号 × 此值 |
| `account_concurrency_advanced` | 1 | 1–8 | 单号并发（4K/高级） |
| `account_daily_image_limit` | 5 | 1–1000 | 号池 checkout 日内上限 |
| `disabled_auto_enable_waterline` | 2 | 0–100 | 水位过低时是否拉起 dead/disabled 号（规划） |
| `retryable_status_codes` | `429,451,500,502,503,422` | HTTP 码列表 | |
| `retryable_error_types` | `UpstreamTemporaryError` | 领域错误名 | |

身份分流补充约定：
- 账号**不分** Normal / Alpha。2.5 走网页 clio + ARP；其它正式模型走 FF-iOS。
- `normal_canary_account_id`：非 0 时移动端（非 2.5）只调度该号。`alpha_canary_account_id`：非 0 时网页 2.5 只调度该号。改回 0 放开全池。
- 新导入立刻落 `UNKNOWN/unverified` 并返回。后台用配置中心代理验积分写成 `HARD/FREE`；只调度 `HARD`。未配代理时保持待验证。
- 网页 408：同号换新 ARP 重试≤6，不 park。移动端 408：短 park + 换号。
- **429 / 451 不得 park / 冷却账号，按 `retry_max_attempts` 退避重试。** 408/429/451/5xx 都不能单独置 `dead`。

### 2.5 存储 / 计费

| 字段 | 默认 | 语义 |
|---|---|---|
| `storage_backend` | `r2` | r2 / s3 / minio。产物存储 |
| `gemini_image_response_format` | `url` | 仅 Gemini `/v1beta` 图片：url 或 inline(b64) |
| `r2_accounts[]` | `[]` | 多账号，每号默认 `quota_bytes=10GB`；secret Fernet；列表以本次提交为准（缺的删除）。上传 Redis `firefly:r2:rr` **轮询**，跳过禁用 / 无密钥 / `over_quota`，失败试下一个，全失败落本地 `/api/media`。对象 **1 天过期**：连通测试与保存配置时 `PutBucketLifecycleConfiguration`（Expire 1d + 未完成分片 1d 中止） |
| `image_pricing` / `video_pricing` | 内置 5 图 + 6 视频 | **价格只来自这里**。无定价或 0 元 → 422，禁止默认白嫖。金额整数 **分**。正式模型清单、尺寸与请求体见 [13-models](./13-models.md)。加载时 `ensure_pricing_catalog` 只**补缺**默认模型，不覆盖已改价格 |

---

## 3. 铸票系统（运行时）

### 3.1 进程与鉴权

| 角色 | 进程 | 接口 |
|---|---|---|
| 配置 + 池 | FastAPI | 面板 `/api/admin/*`；内网 `/admin-api/adobe/*` |
| 采集器 | `harvester` 独立进程 | **只 HTTP**，不碰 PG。`X-Internal-Token` |
| 生成 | FastAPI worker 或独立 worker | checkout 票 + 号，curl_cffi 提交 |

内网口 **禁止公网**：token + IP 白名单 + 反代。采集器挂掉 = 池枯竭 = 全站不能出图，必须常驻看门狗。

### 3.2 采集器循环（已实现）

1. `GET /admin-api/adobe/harvester/bootstrap` → 浏览器数、BATCH、常备池目标、间隔、TTL、水位、`proxy_ready`、License、`version`。顺带写 Redis 心跳。
2. `GET /admin-api/adobe/arp` 看 `ready`。`ready >= arp_pool_target` 则睡一个 interval。
3. `POST /admin-api/adobe/harvester/lease` → `{sid, egress_id, proxy_url, proxy_masked, sticky_minutes}`。
4. [fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium) **148**（版本写死 `148.0.7778.215`，不能 env 改）。二进制默认 `vendor/fingerprint-chromium/chrome.exe`，可用 `ADOBE_CHROME_BINARY` 覆盖。每轮新 `--fingerprint={seed}`。Playwright 只驱动；HTTP 住宅代理不走 Playwright `proxy=`（该二进制 CONNECT 不带账密），而是 `--proxy-server=http://127.0.0.1:<relay>`，中继按 curl_cffi 形状转发。**没有探到公网出口、或浏览器出口与 lease URL 不一致的票不得入池。**
5. 匿名打开 Firefly（不登录）→ Sherlock `initAsync({clientId:"clio-playground-web"})` → 解出 **sid+ark+bfp+ftr** 才算成功。`ftr` 必须是完整 forter cookie（含 `v2_tt`），不是裸 id；缺 `v2_tt` 当铸票失败，**不 POST 入池**。入池口同样校验，短票 422。
6. 抓 `navigator.userAgent` + high-entropy CH，与 `proxy_url` **原样** `POST /admin-api/adobe/arp`。
7. 关掉当前 page；换 sticky 出口必须重启浏览器（`--proxy-server` 是进程级）。`close()` 卡住 → `taskkill /T` 杀进程树。
8. 若 `ready < arp_min_ready` 立即下一轮 BATCH；否则等 `mint_interval_seconds`。CONNECT / 出口校验失败则**全员暂停 ~45s**，禁止 5 路 Chrome 空转。
9. 硬超时 `ARP_MINT_TIMEOUT`（默认 120s）。supervisor 每 3s 对齐 `mint_browser_count`（建议 1–2），死 worker 拉起。一轮 `arp_mint_batch` 枚，满 `arp_pool_target` 停铸。

`HARVESTER_ONCE=1` 只铸一张，用于冒烟，**生产必须 0**。

### 3.3 Redis 池

- key：`{arp_key_prefix}:item:{id}` PX 毫秒 TTL；`:ready` zset by `expires_at`；`:rr` round-robin 游标；`:events`；`:last_publish`；`:harvester:heartbeat`。
- publish 时写入 `max_uses = cap(配置值, 10)`。
- checkout：**round-robin** 原子 `uses++`；禁止总拿最新那张。`uses >= min(blob.max_uses, 当前帽)` 则踢出 ready。旧票 blob 里若是 20，展示和扣次仍按 10。
- 408 / 身份失败：`POST /admin-api/adobe/arp/invalidate`，**同一张票不再重试**（下次重试重新抽一枚）。
- 面板 `GET /api/admin/arp`：**不含** token / proxy_url。`harvester_online` 看心跳，不是池里有没有票。心跳 TTL = `max(interval, 8) + 180s`。

票据页：当前票据 / 出口统计每页 5；最近入池每页 5、简易翻页；住宅出口统计默认 3。

### 3.4 提交侧必须遵守

- `proxies = bundle.proxy_url` 原样。
- TLS：`submit_impersonate=chrome146`（没有 chrome148 标签，禁止瞎配）。Header 用 bundle 的 148 UA / sec-ch-ua*。
- alpha 的 `x-api-key` 与 JWT `client_id` 都是 `clio-playground-web`；normal 为 FF-iOS。
- alpha 同一任务只 mint 一次 clio AT；normal 用加密 device grant 换短 AT。短 AT均不入库。
- 账号必须 `plan_cap=HARD`。FREE 恒 408。
- CDN 预签名下载 **直连**，不走住宅代理。

状态码（生成路径）：

| 码 | 处理 |
|---|---|
| 200 | 轮询 |
| 408 | alpha：invalidate ARP、同号新票≤6、不 park；normal：短 park + 换号；永不直接 dead |
| 401 | 先刷新短 AT；只有明确长期凭据失效才 `dead`；**ARP 保留** |
| 403 | 查 UA/key/iOS 混用 |
| 451 | 退避重试，可换号；**不得 park / 不得 dead**；用尽重试再回用户 |
| 429 | 退避重试，可换号；ARP 保留；**不得 park 账号**；不得 dead |

---

## 4. 号池（和铸票配套，但不混）

- 导入：每行 `email----Cookie` **或** 纯 Cookie Header，必须含 `ims_sid`。
- 只加密存 Cookie。同一 Cookie 带不带邮箱 fingerprint 相同。
- 管理员列表/编辑**明文回显 Cookie**；AT/device grant 永不回。日志仍脱敏。账号不再按身份分组。
- 额度查询：`POST /api/admin/accounts/{id}/refresh` **必须走配置中心 sticky 代理**（未配代理 422）。Cookie 经代理现换 clio AT，再经**同一条代理** `GET https://firefly.adobe.io/v1/credits/balance`（`x-api-key: SunbreakWebUI1`，`x-account-id`）。快照写入 `credits_remaining` / `credits_total` / `plan_cap`。AT 内存即弃。空代理禁止直连。
- 出图：checkout 任意 HARD 号。identity 跟**模型**：2.5 → 网页 clio + ARP；其它 → FF-iOS。

ARP 与账号解耦：一张未过期票可被多个 HARD 号复用，次数受 `arp_max_uses`（≤10）。

---

## 5. 下游功能接入清单（不许绕开）

做生成 / OpenAI / Gemini / 内部工具时：

1. 必须进 `services/generation/gate.submit_job()`（计费预扣）。
2. 必须 `ArpStore.checkout()` 拿完整 bundle，禁止手搓 sid/ark。
3. 必须用 bundle 的 `proxy_url` + ua/ch 提交、轮询；换 AT / 查额度同样必须走代理。下图直连。传图看 `proxy.upload_via_proxy`。
4. 必须从号池 Cookie 现换 AT，禁止把 AT 写入 DB 或配置。
5. 价格只读配置中心计费表。
6. 超时/间隔/水位/代理一律读 ConfigStore，不要新造 env。
7. 日志：账号 hash、egress_id；禁止 token、Cookie、proxy 账密、CB Key。

---

## 6. 禁区（违反即 bug）

1. 拆 ARP bundle，或铸票代理 A、提交代理 B。
2. 采集器自己拼 sticky username / 二次展开。
3. `socks5` 不转 `socks5h`。
4. `mint_interval_seconds >= arp_ttl_seconds`。
5. `sticky_minutes×60 < TTL+60`。
6. `arp_max_uses > 10` 或把 AT/Cookie 明文入库、回前端、写日志、进 git。
7. 用普通 Chrome / Playwright 铸票，或改采集器 Chrome 版本离开 148。
8. `impersonate="chrome148"`（标签不存在）。
9. 把 `localStorage.forterToken` 裸 id 当 `ftr`，或让缺 `v2_tt` 的票入池。
10. `admin-api` 暴露公网。
11. 绕过 gate 直接建任务或调上游。
12. 金额用 float。
13. 换 AT / 查额度 / 提交 / 轮询 不走代理（空 proxy_url 当直连）。下图直连不在此列。

---

## 7. 代码锚点

| 主题 | 路径 |
|---|---|
| 配置 schema / 校验 | `backend/app/services/config/schema.py` |
| 配置存取 | `backend/app/services/config/store.py` |
| sticky 展开 | `backend/app/services/config/proxy.py` `build_session` |
| 面板配置 API | `backend/app/api/admin_config.py` |
| 铸票页 API | `backend/app/api/admin_ops.py` `GET /api/admin/arp` |
| 内网 bootstrap/lease | `backend/app/admin_api/harvester.py` |
| 入池 / checkout | `backend/app/admin_api/arp.py` `backend/app/arp/store.py` |
| max_uses 硬顶 | `backend/app/arp/models.py` `ARP_MAX_USES_CAP=10` |
| 采集器看门狗 | `harvester/arp_harvester/supervisor.py` |
| 号池 Cookie | `backend/app/accounts/cookie_loader.py` `backend/app/services/accounts.py` |
| 号池额度 | `backend/app/adobe/credits.py` `GET firefly.adobe.io/v1/credits/balance` |
| Adobe HTTP 代理 | `backend/app/adobe/http.py` 换 AT/额度/提交/轮询拒空代理；下图直连；传图看 `upload_via_proxy` |
| 现换 AT | `backend/app/adobe/auth.py` `exchange_cookie_for_clio` |
| 提交 | `backend/app/adobe/client.py` |
| 模型目录 / 请求体 | `docs/13-models.md` · `backend/app/adobe/payloads.py` |
| 面板 | `frontend/src/pages/admin/Config.tsx` `Minting.tsx` `Accounts.tsx` |
