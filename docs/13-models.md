# 13 · 模型目录与上游请求参数

> **本仓库模型真源。** 计费表、`GET /api/gen/models`、创作中心下拉、`payloads.py` 都必须对齐本文。  
> 请求体只写本仓库已核实的 HAR / 已落地代码。**禁止**从其他项目抄字段。未核实的标「未接入」，不要编 `modelId`。

原理（408 / ARP）见 [02-arp-core](./02-arp-core.md)。铸票与代理见 [12-minting-and-config](./12-minting-and-config.md)。

---

## 0. 目录一览（系统正式列入）

共 **8** 个面板 `model_id`。未列入：Image5、firefly-video、luma、sora、flux。**Veo 3.1 / Fast / Ref 已下架**（现网易 408，不再接新任务）。

| 面板 id | 显示名 | 种类 | 模式 | 分辨率 | 参考图上限 | 上游提交 |
|---|---|---|---|---|---|---|
| `nano-banana` | Nano Banana | 图 | 文生图 | 1K / 2K / 4K | 8（面板上限） | **已接入** |
| `nano-banana-pro` | Nano Banana Pro | 图 | 文生图 | 1K / 2K / 4K | 8 | **已接入** |
| `gpt-image-2` | GPT Image 2 | 图 | 文生图 | 1K / 2K / 4K | 8 | **已接入** |
| `gpt-image-2.5-flare` | GPT Image 2.5 Flare | 图 | 文生图 | 1K / 2K / 4K | 8 | **已接入** |
| `gpt-image-2.5-sunburst` | GPT Image 2.5 Sunburst | 图 | 文生图 | 1K / 2K / 4K | 8 | **已接入** |
| `gemini-omni-flash` | Gemini Omni Flash | 视频 | 文生视频 / 图生视频 | 720p / 1080p | 4（面板上限） | **已接入** |
| `seedance-2.0` | Seedance 2.0 | 视频 | 文生视频 / 图生视频 | 720p / 1080p | 4 | **已接入** |
| `seedance-2.0-fast` | Seedance 2.0 Fast | 视频 | 文生视频 / 图生视频 | **仅 720p** | 4 | **已接入** |

参考图上限是**本平台 API 校验**（图 `CreateImageRequest` 最多 8，视频 `CreateVideoRequest` 最多 4），**不是**上游硬限制。Seedance 上游最多 9 张图；面板仍卡在 4。

四种模式：

| 模式 | 面板 | 当前 |
|---|---|---|
| 文生图 `t2i` | `/api/gen/image`，无参考图 | **worker 已提交** |
| 图生图 `i2i` | 同一入口；面板 multipart 落盘，或 JSON 给 URL | 有字节就上传 Adobe；**只有 URL 才 GET** |
| 文生视频 `t2v` | `/api/gen/video` | **worker 已提交** `POST .../v2/3p-videos/generate-async` |
| 图生视频 `i2v` | 同上，带参考图；`gemini-omni-flash` / `seedance-2.0` / `seedance-2.0-fast` | 同图生图：先 `/v2/storage/image` 再把 blob id 写入视频体 |

---

## 1. 图片：上游映射与请求体

端点：`POST https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async`

| 面板 id | 上游 `modelId` | `modelVersion` | 家族 |
|---|---|---|---|
| `nano-banana` | `gemini-flash` | `nano-banana-2` | nano |
| `nano-banana-pro` | `gemini-flash` | `nano-banana-3` | nano |
| `gpt-image-2` | `gpt-image` | `2` | gpt2 |
| `gpt-image-2.5-flare` | `gpt-image` | `gpt-image-2.5-flare` | gpt25 |
| `gpt-image-2.5-sunburst` | `gpt-image` | `gpt-image-2.5-prism` | gpt25 |

比例（五家共用）：`1:1` `16:9` `9:16` `4:3` `3:4` `3:2` `2:3` `5:4` `4:5` `21:9`。

公共字段：`n: 1`，`prompt` 截到 4000，`seeds: [随机]`，`output.storeInputs: true`，`generationMetadata: {module: text2image, submodule: ff-image-generate}`，`referenceBlobs: []` 或 `[{id, usage}]`。`usage` 现网必填：Nano 家族 `general`，GPT 家族 `subject`。

### 1.1 Nano Banana 两家

`size` 永远是**正方形占位**，比例走 `modelSpecificPayload.aspectRatio`（含 `1:1`；只有 `auto` 不加该字段。省略时上游会默认 16:9）。另有 `groundSearch: false`、`caiClaimVersion: 2`、`modelSpecificPayload.parameters.addWatermark: false`。

| 档 | `size.width × height` |
|---|---|
| 1K | 1024 × 1024 |
| 2K | 2048 × 2048 |
| 4K | 4096 × 4096 |

### 1.2 GPT Image 2

无顶层 `size` 对象。比例写进 `modelSpecificPayload.size` 字符串 `"宽x高"`，两边都是 16 的倍数。另有 `generationSettings.detailLevel: 3`、`caiClaimVersion: 2`。

### 1.3 GPT Image 2.5（flare / sunburst）

`caiClaimVersion: 2`（缺了会 403），`generationSettings.detailLevel: 3`，`modelSpecificPayload.size` 为 `"宽x高"` 字符串，同时带顶层 `size: {width,height}`。

### 1.4 GPT 像素表（2 与 2.5 共用）

| 比例 | 1K | 2K | 4K |
|---|---|---|---|
| 1:1 | 1024×1024 | 2048×2048 | **2480×2480** |
| 3:2 | 1536×1024 | 2496×1664 | 3056×2032 |
| 2:3 | 1024×1536 | 1664×2496 | 2032×3056 |
| 4:3 | 1152×864 | 2304×1728 | 2880×2160 |
| 3:4 | 864×1152 | 1728×2304 | 2160×2880 |
| 5:4 | 1120×896 | 2240×1792 | 2784×2224 |
| 4:5 | 896×1120 | 1792×2240 | 2224×2784 |
| 16:9 | 1280×720 | 2560×1440 | 3328×1872 |
| 9:16 | 720×1280 | 1440×2560 | 1872×3328 |
| 21:9 | 1456×624 | 3024×1296 | 3808×1632 |

4K 的 1:1 是 **2480** 不是 4096。Nano 的 4K 才是 4096 方图。两套不能混用。

### 1.5 图生图（怎么传图）

Adobe 提交体只要 storage 上传后的 blob id，**不会替我们拉 URL，也不收 base64**。所以：

| 来源 | 我们做什么 | 会不会 HTTP 下载 |
|---|---|---|
| 面板本地文件 | multipart 落盘成 `file:`，worker 读盘后 POST Adobe | 否 |
| data URI / raw base64 | 解码后 POST Adobe | 否 |
| `http(s)://` | **直连 GET 一次** 再 POST Adobe | 是（仅这一类） |

真正费时的是 Adobe 出图（几十秒），不是这几 MB 参考图。传图默认直连；`proxy.upload_via_proxy` 打开才会走住宅代理、明显变慢。

1. `POST https://firefly-3p.ff.adobe.io/v2/storage/image` 上传原始字节。上传头只有 `authorization`、`x-api-key: clio-playground-web`、`content-type`、`accept`、`user-agent`，不带 `x-arp-session-id`。
2. 解析返回的 `images[0].id`，写入 `referenceBlobs: [{id, usage}]`（Nano `usage=general`，GPT `usage=subject`）。缺 `usage` 会上游 422。提交体其它字段不因传图改动。

---

## 2. 视频：上游映射与请求体

端点：`POST https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async`。轮询 `outputs[].video.presignedUrl`，下载直连。参考图仍走 `/v2/storage/image`（本阶段不接视频/音频素材上传）。

视频请求体授权来源：[adobe2api-plus](https://github.com/songsongQAQ/adobe2api-plus) `src/lib/adobe/payloads.ts` 的 `buildVideoPayload`（只抄 omni / seedance）。**不抄** kling / sora / veo。图片 `usage` 以现网 422 为准（见 1.5），不是从视频工程抄来的其它字段。

| 面板 id | 上游 `modelId` | `modelVersion` | 家族 |
|---|---|---|---|
| `gemini-omni-flash` | `gemini-omni` | `omni-flash` | omni |
| `seedance-2.0` | `seedance` | `seedance_2.0` | seedance |
| `seedance-2.0-fast` | `seedance` | `seedance_2.0_fast` | seedance |

分辨率：Omni / Seedance 2.0 为 **720p / 1080p**；**Seedance 2.0 Fast 仅 720p**（现网 `bad_request`：`1080p resolution is not supported for 'seedance_2.0_fast'`）。比例 `16:9` `9:16`。

| 档 | 16:9 | 9:16 |
|---|---|---|
| 720p | 1280×720 | 720×1280 |
| 1080p | 1920×1080 | 1080×1920 |

时长目录：`5 / 8 / 10`。Omni / Seedance 写顶层 `duration`。

### 2.1 Gemini Omni Flash

`generationMetadata.module = aura`，`generationSettings.aspectRatio`，参考图 `usage: style`（最多 4）。图生视频已用最新号实测通过（720p / 5s，参考图先 `/v2/storage/image` 再写入 `referenceBlobs`）。

### 2.2 Seedance 2.0 / Fast

`generateAudio: true`，默认 `negativePrompt`，`generationMetadata: {module: text2video, submodule: ff-video-generate}`。参考图 `usage: style`。Fast 不要发 1080p，gate / 目录会直接 422 `resolution_unsupported`。

| 面板 id | 计费 |
|---|---|
| `gemini-omni-flash` | 按秒 |
| `seedance-2.0` | 按秒 |
| `seedance-2.0-fast` | 按秒 |

---

## 3. 请求头与调用顺序（图片已实现）

身份路径按**模型**分流，账号不分 Normal / Alpha：

- **默认（正常出图）**：`nano-banana` / `nano-banana-pro` / `gpt-image-2` / 全部视频 → **FF-iOS 移动端**，无 ARP。
- **例外**：`gpt-image-2.5-flare` / `gpt-image-2.5-sunburst` → 网页 clio + Web ARP（FF-iOS 没有 2.5）。

任意 HARD 号都可以领两条路。

### 3.1 默认：FF-iOS 出图

1. 号池里已有加密 `device_token` / `device_id`；没有则用 Cookie 静默授权一次（`app/adobe/ff_ios.py`）。
2. 每次任务用住宅 sticky **现换** FF-iOS access_token，**不入库**。JWT `client_id` 必须是 `FF-iOS`。
3. `curl_cffi` 走当前任务的住宅代理；**不** checkout ARP，**不**带 `x-arp-session-id` / Chrome Client-Hints。
4. 提交 `POST .../v2/3p-images/generate-async`（视频走 `.../v2/3p-videos/generate-async`），轮询后下图直连 CDN。

提交 / 轮询头：

| Header | 值 |
|---|---|
| `authorization` | `Bearer <FF-iOS AT>` |
| `x-api-key` | `FF-iOS` |
| `x-ims-clientid` | `FF-iOS` |
| `user-agent` | `Firefly/26.15.0 (AdobeCreativeSDK ...; iOS; ...)` |
| `accept` | `*/*` |
| `x-nonce` | `sha256(account_id + "-" + prompt[:256])`（仅 submit） |
| `content-type` | `application/json`（仅 submit） |

不要混网页 ARP、`clio-playground-web` 或 Chrome UA。429 / 451 不 park 账号。

### 3.2 例外：2.5 网页 ARP

1. 采集器 148 + 住宅 sticky 铸 ARP（`sid+ark+bfp+ftr`），`ftr` 必须是完整 forter cookie（含 `v2_tt`），否则不入池。
2. Cookie 经**同一条 sticky** `POST /ims/check/v6/token` 换 clio AT（`client_id=clio-playground-web`，scope 含 `creative_production`）。
3. `curl_cffi` `impersonate=chrome146` + bundle 的 148 UA/CH。
4. 提交和轮询 `proxies = bundle.proxy_url` 原样。
5. 下图直连预签名 CDN。

提交头（对照包 generate-async 200，不多不少）：

| Header | 值 |
|---|---|
| `user-agent` / `sec-ch-ua` / `sec-ch-ua-mobile` / `sec-ch-ua-platform` | 铸票浏览器 overlay |
| `authorization` | `Bearer <clio AT>` |
| `x-api-key` | **`clio-playground-web`**（额度查询才是 `SunbreakWebUI1`） |
| `x-arp-session-id` | bundle.token（仅 submit，轮询不带） |
| `x-nonce` | `sha256(account_id + "-" + prompt[:256])`（仅 submit） |
| `content-type` | `application/json`（仅 submit） |
| `accept` | `*/*` |
| `origin` / `referer` | `https://firefly.adobe.com/` |
| `sec-fetch-*` | `empty` / `cors` / `cross-site` |

不要发 `x-account-id`、`x-request-id`、`accept-language` 或完整 `sec-ch-ua-full-version-list`。

额度：`GET https://firefly.adobe.io/v1/credits/balance`，`x-api-key: SunbreakWebUI1`。账号必须 **HARD**。

---

## 4. 怎样才不会 408（仅 2.5 网页路）

FF-iOS 默认路不靠 ARP，这条六条件只约束 GPT Image 2.5。408 是 Colligo 软拒（`timeout_error` / `system under load`），不是超时设短了。六条同时成立：

1. 有效 `x-arp-session-id`（四件套）
2. 住宅 IP 铸的票
3. 提交出口 == 铸票 sticky
4. UA / Client-Hints == 铸票那台 148
5. `x-api-key` 与 JWT `client_id` 都是 `clio-playground-web`（禁止 FF-iOS 混用）
6. 账号 `planCap=HARD`

2.5 额外：`creative_production` scope + body `caiClaimVersion: 2`。

网页（2.5）出错不要拿同一张票重试：408 → invalidate 该 ARP，同一账号换新票、小预算
重试（≤6），不 park 账号。移动端 408 可短 park 并换号。**429 / 451 不得冷却账号，按预算退避重试。**
任何 408、429、451、5xx 都不是账号死亡判据；只有明确长期凭据失效才置 `dead`。

---

## 5. 代码锚点

| 主题 | 路径 |
|---|---|
| 请求体 / 目录 | `backend/app/adobe/payloads.py` |
| 提交头 / 上传 / 轮询 | `backend/app/adobe/client.py` |
| 计费默认表 | `backend/app/services/config/schema.py` `default_config()` |
| 缺模型补齐 | `ensure_pricing_catalog()`（加载配置时并入缺的默认模型，不改已有价格） |
| 面板模型列表 | `GET /api/gen/models` ← `models_from_pricing()` |
| 创作中心 | `frontend/src/pages/user/Create.tsx` |

`GET /api/gen/models` 每条含：`id,name,kind,price,billing,tiers,note,aspect_ratios,resolutions,durations,supports_edit,modes,max_reference_images,submit_ready`。
