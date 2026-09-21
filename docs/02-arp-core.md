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

**2.5 系列额外两条**（gpt-image / flare / prism）：
- **A. `creative_production` scope**：FF-iOS token 没这个 scope → 403。必须用 clio-playground-web token（从账号 `ims_sid` + 完整 Cookie 走 `/ims/check/v6` 铸）。
- **B. 只在 alpha 组账号铸 clio**：铸 clio 会吊销该账号 FF-iOS device grant，故只对打了 `group=alpha` 的号铸。
- body 还要带 `caiClaimVersion`，否则 403。

**成功率成簇**：实测约 15% 且成簇（好窗口能出、坏窗口全 408，同号同票这分钟 408 下分钟 200）。所以 408 要 **park+换号跨池重试**骑好窗口，判据必须按 egress 累积（`egress_scores`），不要拿单样本下结论。

## 2. `x-arp-session-id` 结构
`base64(JSON({sid, ark, bfp, ftr}))`，四件套缺一不可：
- `ark` = Arkose `state.token`（页面 reload 才重铸）。
- `sid` / `bfp` = `arp_bfp_vendor_cache`。
- `ftr` = **完整 `forter` cookie 值**（`<id>_<ts>__UDF43-…=-<n>-v2_tt`），**不是** `localStorage.forterToken` 里的裸 id。**读错必 408，这是最大的坑。**

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
  "uses": 0, "max_uses": 20
}
```
字段约定：`proxy_url` 提交时**一个字符都不能改**（尤其 sticky SID）；`expires_at = minted_at + 120s`（TTL 实测 2-4min，宁早丢）；`uses` 可多账号复用，408 立即作废不重试同一条。

## 4. 铸票（采集器 harvester）
- 浏览器：**CloakBrowser PRO / fingerprint-chromium，148 指纹**（不要用普通 Chrome / Playwright 自带 Chromium 铸票）。每次换 seed：
  ```
  --fingerprint={seed} --fingerprint-platform=windows --fingerprint-brand=Chrome
  --lang=en-US --accept-lang=en-US,en;q=0.9 --window-size=1366,768
  --disable-blink-features=AutomationControlled --no-first-run --headless=new
  ```
- 代理：住宅 **sticky**（同一 SID 钉 5-15 分钟）。SOCKS5 带账密时浏览器侧走本机 HTTP CONNECT 中继再转 SOCKS。
- **不登录 Adobe**，匿名打开 Firefly 即可（Sherlock 对访客也跑，ARP 账号无关）。
- 步骤：`goto https://firefly.adobe.com/generate/image` → 注入 `sherlock.min.js` → 等 `window.SherlockSdk` → 调 `initAsync({clientId:"clio-playground-web",prodEnv:true,...})` → `tokenCallback` 里解出 `sid+ftr+ark+bfp` 才算成功 → 连同 `navigator.userAgent` + `getHighEntropyValues([platform,platformVersion,fullVersionList,architecture,bitness,model,uaFullVersion])` 一起入池。
- 节奏：`ARP_INTERVAL≈75s`（< TTL），池里保持 3-10 条未过期。并发靠 HTTP worker，不靠加浏览器。
- 禁忌：不手搓 sid/ark/bfp/ftr；不用机房 IP 连续铸；不铸票走代理 A、提交走代理 B；不铸票 148 指纹、提交 iOS/curl 默认 UA。

## 5. 账号 token（生成侧管，与 ARP 无关）
1. 账号需有 Firefly 网页 Cookie（至少 `ims_sid`，可加 `aux_sid`）。
2. 用**将用于提交的代理**（或任意住宅代理，token 本身不绑 ARP）调：
   ```
   POST https://adobeid-na1.services.adobe.com/ims/check/v6/token?jslVersion=v2-v0.48.0-1-g1e322cb
   Content-Type: application/x-www-form-urlencoded;charset=UTF-8
   Cookie: <完整 jar>  Origin: https://firefly.adobe.com  Referer: https://firefly.adobe.com/

   client_id=clio-playground-web&guest_allowed=true&scope=AdobeID firefly_api openid ... creative_production
   ```
3. 取 `access_token`；JWT `client_id` 必须 `clio-playground-web`；`user_id`/`aa_id` 作为 `x-account-id`。
4. `@GuestID` = Cookie 失效，需重新登录（微软联合登录 / EVS 收码刷 `ims_sid`）。
5. **禁止**网页 ARP + `x-api-key: FF-iOS` + CFNetwork UA 混用（另一条身份，混用必 408）。

> 注：Express 侧 `projectx_webapp` token 覆盖 image/veo/luma/sora，但对 seedance 等 Firefly-native 模型 403，需再铸 clio token（`creative_production`）。

## 6. 提交（curl_cffi，纯 HTTP，不开浏览器）
- 会话：`impersonate` 桌面 Chrome（如 `chrome131`）；`proxies=bundle.proxy_url`（原样）；header 覆盖为 bundle 的 UA/sec-ch-ua*（**不用** impersonate 自带 CH）。
- 端点：
  - 图：`/v2/3p-images/generate-async`（Image5 用 `image-v5.ff.adobe.io/v1/images/generate-async`，schema 不同）
  - 视频：`/v2/3p-videos/generate-async`（firefly-video 用 `video-v1.ff.adobe.io/v2/videos/generate`）
  - 参考图上传：`/v2/storage/image`（视频 `/video`、音频 `/audio`；seedance 上传需 clio key）
- 关键 header（顺序固定）：
  ```
  authorization: Bearer <access_token>
  x-api-key: clio-playground-web
  x-arp-session-id: <bundle.token>
  x-account-id: <JWT user_id>
  x-nonce: sha256hex(user_id + "-" + prompt[:256])
  content-type: application/json
  accept: */*
  origin/referer: https://new.express.adobe.com/     # 上游只 gate key + arp，不 gate origin
  sec-ch-ua*/user-agent: <bundle 身份>
  caiClaimVersion: 2   # (2.5 系列 body 内)
  ```
- 请求体形状按模型（gpt-image / flux / gemini / veo / luma / sora / seedance / firefly-image-5 / firefly-video）不同，详见 `backend/app/adobe/payloads`（迁移自参考实现 `payloads.go`）。

## 7. 轮询与下载
- `GET links.result.href`，间隔 ~3s，超时 120s；从 `outputs[].image.presignedUrl`（视频 `outputs[].video.presignedUrl`）取结果。
- 轮询走同一 sticky；**下载预签名 CDN 建议直连**（住宅代理拉 10MB+ 易超时，CDN 不走 Colligo），下载失败按退避重试（产物已生成，别让网络抖动废掉整单）。

## 8. 状态码处理
| 码 | 含义 | 处理 |
|---|---|---|
| 200 | 已接受 | 轮询 |
| 408 / "system under load" / "timeout_error" | Colligo 软拒（临时） | **立即 invalidate 该 ARP**，park 账号换下一个 + 换新票重试(≤2-3)，骑好窗口 |
| 401 | token/cookie 失效 | 刷账号 Cookie/token，ARP 保留 |
| 403 | 权限/身份交叉失败 | 查 iOS 混用、UA 与铸票是否一致、scope/账号档 |
| 429 | 账号限速 | 换账号，ARP 保留 |
| 451 (`*_unsafe`/`privacy_error`) | 内容安全拒绝 | 原样回用户，**不罚号、不 failover**（每个号都会拒同样内容） |
| 400 (`media_classification`) | 分类器读不到刚传的媒体 | 临时，重传 blob 重试 |
| 5xx | 上游故障 | 临时，跨池重试 |

## 9. 并发与资源
| 做法 | 本机代价 | 推荐 |
|---|---|---|
| 每个出图任务开一个浏览器 | 每路 200-400MB+1 核 / 30-90s | ❌ |
| 1-2 个采集器浏览器 + N 路 curl_cffi | 常驻 ~0.5-1GB，出图几乎只吃带宽 | ✅ |
| 无池、每次现铸 | 无法高并发 | 仅调试 |
池水位：未过期票 ≥ 在飞出图路数；`ready=0` 时出图暂停排队。

## 10. 上线检查清单
1. 能独立铸出可解码的 `sid+ark+bfp+ftr`（匿名、住宅 sticky、148 指纹）。
2. 池里每条带 `proxy_url + ua/ch`，过期自动删。
3. checkout 后提交代理 URL 与铸票完全一致（抓出口 IP 对一下）。
4. 提交 `User-Agent`/`sec-ch-ua` 与铸票 `navigator` 一致。
5. `x-api-key` 与 token JWT `client_id` 都是 `clio-playground-web`。
6. 付费 HARD 号打一次应 200；故意换 SID 再提交应 408。
7. Cookie 过期只刷账号，不丢整池 ARP。
