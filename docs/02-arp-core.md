# 02 · ARP 核心链路（技术核心）

> 本文是全平台技术含量最高的部分。核心不是"一号一 ARP"，而是：**一张风控票必须和铸它的粘性出口、铸它的浏览器身份绑在一起用**。账号 token 另算，可多账号复用同一张未过期的票。

## 0. 408 的真相
上游 `POST https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async`（视频为 `/3p-videos/`）对不合格请求统一回：
```json
{"error_code":"timeout_error","message":"system under load"}  // HTTP 408
```
这**不是**真超时，是 Colligo / Arkose / Forter 的软拒绝。调 timeout、重试同一路、换账号都没用——必须让请求"看起来可信"。
`403 "Unauthorized to perform request"` 是权限维度拒绝（scope/账号档不够），不是 IP/指纹问题。

## 1. 六条件（同时成立才 200）

| # | 条件 | 拆开会怎样 |
|---|---|---|
| ① | 请求带有效 `x-arp-session-id` | 缺 → 408 |
| ② | ARP 由**住宅 IP** 铸出（机房 IP 铸的票几天后废） | 结构对也 408 |
| ③ | 提交出口 == 铸票出口（同一 sticky session） | 同票换 IP → 408 |
| ④ | 提交 UA / Client-Hints == 铸票那台浏览器 | 不一致 → 403/408 |
| ⑤ | `x-api-key` 与 token 的 JWT `client_id` 都是 `clio-playground-web`（勿混 FF-iOS） | 身份交叉核对失败 |
| ⑥ | 账号是付费 `planCap=HARD`（FREE 即使额度没用完也恒 408） | 账号档位墙 |

**2.5 系列额外**（gpt-image / flare / prism）：
- **A. `creative_production` scope**：FF-iOS token 没这个 scope → 403。必须用网页 clio-playground-web token（Cookie 走 `/ims/check/v6` 铸）+ Web ARP。
- body 还要带 `caiClaimVersion`，否则 403。
- 账号**不按 Normal/Alpha 分池**。同一 HARD 号：2.5 走网页，其它模型走 FF-iOS。

**成功率成簇**：实测约 15% 且成簇（好窗口能出、坏窗口全 408，同号同票这分钟 408 下分钟 200）。所以 408 要 **park+换号跨池重试**骑好窗口，判据必须按 egress 累积（`egress_scores`），不要拿单样本下结论。

## 2. `x-arp-session-id` 结构
`base64(JSON({sid, ark, bfp, ftr}))`，四件套缺一不可：
- `ark` = Arkose `state.token`（页面 reload 才重铸）。
- `sid` / `bfp` = `arp_bfp_vendor_cache`。
- `ftr` = **完整 `forter` cookie 值**（`<id>_<ts>__UDF43-…=-<n>-v2_tt`），**不是** `localStorage.forterToken` 里的裸 id。缺 `v2_tt`：**采集器丢弃、入池 422，不得进 Redis**。读错必 408。

> 参考实现里存在一种"合成 ARP"路径（`arp_session.go`：ark 半随机、Arkose 段留空），但那是降级兜底，命中率远低于真铸票。本平台**以真铸票为主**。

## 3. ARP 池记录（必须整套存，不可拆）

```json
{
  "id": "arp_01J...",
  "token": "<base64 x-arp-session-id>",
  "proxy_url": "socks5h://user-sid-XXXX-t-15:pass@host:port",
  "egress_id": "XXXX",
  "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/148.0.0.0 Safari/537.36",
  "ch": { "architecture":"x86","bitness":"64","platform":"Windows","platformVersion":"15.0.0",
          "uaFullVersion":"148.0.7778.215","mobile":false,"model":"","fullVersionList":[...] },
  "headers": { "sec-ch-ua":"...", "sec-ch-ua-full-version-list":"...", "...": "..." },
  "minted_at": 1726810000, "expires_at": 1726810120,
  "uses": 0, "max_uses": 10
}
```
字段约定：`proxy_url` 提交时**一个字符都不能改**（尤其 sticky SID）；`expires_at = minted_at + 120s`（TTL 实测 2-4min，宁早丢）；`uses` 可多账号复用，**单票 `max_uses` 硬顶 10**（配置中心可更低），408 立即作废不重试同一条。

## 4. 铸票（采集器 harvester）
- 浏览器：**[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium) 148**（不要用普通 Chrome / Playwright 自带 Chromium / CloakBrowser PRO 铸票）。每次换 seed：
  ```
  --fingerprint={seed} --fingerprint-platform=windows --fingerprint-brand=Chrome
  --lang=en-US --accept-lang=en-US,en;q=0.9 --window-size=1366,768
  --disable-blink-features=AutomationControlled --no-first-run
  ```
  Playwright 只驱动该 `chrome.exe`。开源二进制把 `--proxy-server=http://user:pass@host` 判成 `ERR_NO_SUPPORTED_PROXIES`，Playwright 的 username/password 也不会在 CONNECT 上带 `Proxy-Authorization`。住宅 sticky 走本机 `127.0.0.1` HTTP 中继：把浏览器 CONNECT **改写成与 curl_cffi 相同的四行**（`Host` + Basic auth + `Proxy-Connection: Keep-Alive`），ARP bundle 仍存原 `proxy_url`。入池前必须：lease URL 探到公网出口 IP，且浏览器 ipify 与该 IP **完全一致**，否则当铸票失败、不入池。
- **不登录 Adobe**，匿名打开 Firefly 即可（Sherlock 对访客也跑，ARP 账号无关）。
- 步骤：`goto https://firefly.adobe.com/generate/image` → 注入 `sherlock.min.js` → 等 `window.SherlockSdk` → 调 `initAsync({clientId:"clio-playground-web",prodEnv:true,...})` → `tokenCallback` 里解出 `sid+ftr+ark+bfp` 才算成功 → 连同 `navigator.userAgent` + `getHighEntropyValues([platform,platformVersion,fullVersionList,architecture,bitness,model,uaFullVersion])` 一起入池。
- 节奏：维持**小池**（`arp_pool_target` 常备 4–20 枚），不要堆浏览器。`mint_browser_count` 建议 **1–2 路热进程**（换 context/出口，不每票重启）。每轮 `arp_mint_batch`（默认 8）铸多枚再空闲 `mint_interval_seconds`（默认 10s，必须 `< TTL`）。停铸：`ready >= arp_pool_target`；低于 `arp_min_ready` 跳过空闲。每次铸票 lease 新 SID，`proxy_url` 原样入池、原样提交。TTL 默认 120s，Redis `PX` 到期销毁。
- 看门狗：supervisor 按配置热加载浏览器数；worker 常驻热浏览器，换 egress 时优先 `new_context`，不行再重启进程；`close()` 卡住则 `taskkill /T`；崩溃自动拉起。采集器挂掉则全站无法出图。
- 提交侧 **round-robin 取下一枚**，禁止把所有并发压在最新那一张上。408 立即 invalidate，下次重试重新抽票，不卡死票。
- 禁忌：不手搓 sid/ark/bfp/ftr；不用机房 IP 连续铸；不铸票走代理 A、提交走代理 B；不铸票 148 指纹、提交 iOS/curl 默认 UA。

## 5. 身份按模型分流（账号不分池）

号池里每张 HARD 号都能跑全部正式模型，**不要**再给账号打 Normal / Alpha。

- `gpt-image-2.5-flare` / `gpt-image-2.5-sunburst`：**网页端**。clio + 完整 Web ARP bundle，提交出口和身份与铸票一致。
- 其它正式模型（nano / gpt-image-2 / 视频）：**移动端 FF-iOS**。不发送 Web ARP、Chrome UA/CH 或 `clio-playground-web` 头。

同一号同时保存 Cookie（网页铸 clio）和加密 FF-iOS device grant。铸 clio 若吊销了 device grant，下次移动端任务从 Cookie 再铸，不要为此把号拆成两池。Checkout 只看 HARD + 租约；2.5 灰度用 `alpha_canary_account_id`，移动端灰度用 `normal_canary_account_id`（字段名历史遗留）。429 / 451 不 park、不冷却账号，任务继续重试。

### 5.1 网页 / clio（仅 2.5）
1. 账号需有 Firefly 网页 Cookie（至少 `ims_sid`，可加 `aux_sid`）。
2. 用**将用于提交的代理**（或任意住宅代理，token 本身不绑 ARP）调：
   ```
   POST https://adobeid-na1.services.adobe.com/ims/check/v6/token
   Content-Type: application/x-www-form-urlencoded;charset=UTF-8
   Cookie: <完整 jar>  Origin: https://firefly.adobe.com  Referer: https://firefly.adobe.com/

   client_id=clio-playground-web&guest_allowed=true&scope=AdobeID firefly_api openid ... creative_production
   ```
3. 取 `access_token`（**每次请求用 Cookie 现换，不入库**）；JWT `client_id` 必须 `clio-playground-web`。submit 不发 `x-account-id`（对照包 generate-async 200 捕获亦无此头）。
4. 只有仅含 `ims_sid` 的裸 Cookie 才发送 `guest_allowed=true`；完整登录 Cookie jar 禁止发送，
   避免 session 被降级/轮换。
5. 同一任务只 mint 一次 clio token，后续 408 重试复用该 token；服务端返回的 Cookie 轮换
   加密回写，日志和响应不得出现明文。
6. `@GuestID` / 明确 `invalid_credentials` / 不可恢复 401 才能判凭据失效；429、5xx、连接失败
   均为临时错误，不得把账号标为 `dead`。
7. **禁止**网页 ARP + `x-api-key: FF-iOS` + CFNetwork UA 混用。

### 5.2 移动端 / FF-iOS（非 2.5）

- 长期保存的是加密 `device_token` 与稳定 `device_id`；短期 access token 现换现用，不入库。
- 提交使用 FF-iOS 的 `x-api-key` / UA / scope，不注入 `x-arp-session-id` 或 Web client hints。
- device grant 未初始化时，可用账号 Cookie 完成一次静默授权；授权失败按“明确凭据失效”和
  “临时上游错误”分类，不能由单个 429/5xx 直接判死。

> 网页路径只用 clio（`clio-playground-web` + `creative_production`），不要混 `projectx_webapp`。非 2.5 用 FF-iOS。模型清单见 [13-models](./13-models.md)。

## 6. 提交（curl_cffi，纯 HTTP，不开浏览器）
- **统一身份目标为 Chrome 148**。fingerprint-chromium 铸票、UA、Client-Hints 都使用 148。
- 开源 `curl_cffi` 当前没有 `chrome148` 内置 profile（目前最新为 `chrome146`，版本缺口是正常的，只有指纹变化时才新增标签）。默认以 `impersonate="chrome146"` 提供最接近的 TLS/HTTP2 指纹，再用 bundle 覆盖真实 Chrome 148 的 UA/sec-ch-ua*；**禁止**传不存在的 `chrome148` 标签。
- `proxies=bundle.proxy_url`（原样）；header 覆盖为 bundle 的 UA/sec-ch-ua*（**不用** impersonate 自带 CH）。
- 端点（本仓库正式模型，见 [13-models](./13-models.md)）：
  - 图：`POST https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async`（五家图片已接入）
  - 视频：`POST https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async`（六家已列入计费，请求体未接入）
  - 参考图上传：`POST .../v2/storage/image`（默认直连；`proxy.upload_via_proxy` 才走 sticky）
- 关键 header（对照包 generate-async 200 捕获，不多不少；以 `backend/app/adobe/client.py` 为准）：
  ```
  authorization: Bearer <access_token>
  x-api-key: clio-playground-web
  x-arp-session-id: <bundle.token>
  x-nonce: sha256hex(user_id + "-" + prompt[:256])
  content-type: application/json
  accept: */*
  origin/referer: https://firefly.adobe.com/
  sec-fetch-dest/mode/site: empty / cors / cross-site
  user-agent / sec-ch-ua / sec-ch-ua-mobile / sec-ch-ua-platform: 铸票浏览器 overlay
  caiClaimVersion: 2   # (body，不是 header)
  ```
  不要发 `x-account-id`、`x-request-id`、`accept-language`、完整 `sec-ch-ua-full-version-list` 等铸票附属头。轮询复用同一身份 overlay，但去掉 `content-type` / `x-nonce` / `x-arp-session-id`。
- 请求体形状见 [13-models](./13-models.md) 与 `backend/app/adobe/payloads.py`。未核实的模型不要编字段。

## 7. 轮询与下载
- `GET links.result.href`，间隔 ~3s，超时 120s；从 `outputs[].image.presignedUrl`（视频 `outputs[].video.presignedUrl`）取结果。
- 轮询走同一 sticky；**下载预签名 CDN 建议直连**（住宅代理拉 10MB+ 易超时，CDN 不走 Colligo），下载失败按退避重试（产物已生成，别让网络抖动废掉整单）。

## 8. 状态码处理
| 码 | 含义 | 处理 |
|---|---|---|
| 200 | 已接受 | 轮询 |
| 408 / "system under load" / "timeout_error" | 临时拒绝，不是账号死亡判据 | alpha：invalidate 当前 ARP、同号换新票重试≤6，不 park；normal：短 park 当前号并换号。两者都不得置 `dead` |
| 401 | access token 或长期凭据问题 | 先刷新短 token；只有明确长期凭据失效才置 `dead` |
| 403 | 权限/身份交叉失败 | 查 iOS 混用、UA 与铸票是否一致、scope/账号档 |
| 429 | 账号/IMS 限速 | 退避后继续重试（可换号）；ARP 保留；**不得 park / 不得 dead** |
| 451 (`*_unsafe`/`privacy_error`) | 内容安全拒绝 | 退避后继续重试（可换号）；**不罚号、不得 park / 不得 dead**。用尽重试再回用户 |
| 400 (`media_classification`) | 分类器读不到刚传的媒体 | 临时，重传 blob 重试 |
| 5xx | 上游故障 | 临时，跨池重试 |

账号套餐状态另分为 `UNKNOWN / HARD / FREE`。导入时必须是 `UNKNOWN`，额度查询明确返回后才改成
`HARD` 或 `FREE`；只有 `HARD` 可进入生成调度。禁止把未验证账号默认写成 `HARD`。

## 9. 并发与资源
| 做法 | 本机代价 | 推荐 |
|---|---|---|
| 每个出图任务开一个浏览器 | 每路 200-400MB+1 核 / 30-90s | ❌ |
| 1-2 个采集器浏览器 + N 路 curl_cffi | 常驻 ~0.5-1GB，出图几乎只吃带宽 | ✅ |
| 无池、每次现铸 | 无法高并发 | 仅调试 |
池水位：未过期票 ≥ 在飞出图路数；`ready=0` 时出图暂停排队。

## 10. 上线检查清单
1. 能独立铸出可解码的 `sid+ark+bfp+ftr`（匿名、住宅 sticky、148 指纹），且 `ftr` 含 `v2_tt`，否则不得入池。
2. 池里每条带 `proxy_url + ua/ch`，过期自动删。
3. checkout 后提交代理 URL 与铸票完全一致（抓出口 IP 对一下）。
4. 提交 `User-Agent`/`sec-ch-ua` 与铸票 `navigator` 一致。
5. `x-api-key` 与 token JWT `client_id` 都是 `clio-playground-web`。
6. 付费 HARD 号打一次应 200；故意换 SID 再提交应 408。
7. Cookie 过期只刷账号，不丢整池 ARP。
