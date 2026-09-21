# 04 · API 规范

三类接口：**面板 API**（`/api`，JWT）、**对外生成 API**（`/v1`，KEY）、**内网 admin-api**（采集器，内网 token）。统一响应包 `{ "code": 0, "message": "ok", "data": {...} }`，错误 `code != 0`。

## 通用
- 鉴权：面板 `Authorization: Bearer <JWT>`；对外 `Authorization: Bearer sk-...`；内网 `X-Internal-Token`。
- 分页：`?page=1&page_size=20`，返回 `{items, total, page, page_size}`。
- 时间 ISO8601 UTC；金额/积分为最小单位整数。

## 1. 认证（面板）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | `{username,password}` → `{token, role, profile}` |
| POST | `/api/auth/logout` | 使当前 token 失效（黑名单） |
| GET | `/api/auth/me` | 当前用户信息 + 余额 |

## 2. 用户功能（面板，`role=user`）
### 创作中心
| POST | `/api/gen/image` | `{model,prompt,aspect_ratio,resolution,reference_blob_ids?,seeds?,idempotency_key}` → `{job_id,status}` |
| POST | `/api/gen/video` | `{model,prompt,aspect_ratio,resolution,duration,reference_mode?,...}` → `{job_id,status}` |
| POST | `/api/gen/upload` | 参考图/视频/音频上传 → `{blob_id}` |
| GET | `/api/gen/models` | 可用模型 + 单价 + 能力(尺寸/时长) |
| GET | `/api/jobs/{id}` | 任务状态/结果；支持 WS `/ws/jobs/{id}` 推进度 |
### 生成历史
| GET | `/api/jobs` | 分页筛选（kind/model/status/时间） |
| DELETE | `/api/jobs/{id}` | 删除历史（软删） |
### KEY 管理
| GET/POST/DELETE | `/api/keys` | 列表 / 创建(返回明文一次) / 吊销 |
| PATCH | `/api/keys/{id}` | 改名 / 限流 |
### 余额明细
| GET | `/api/ledger` | balance_ledger 分页 |
| GET | `/api/records/consume` | 消费记录 |
| GET | `/api/records/recharge` | 本人充值记录 |
### 个人设置
| GET/PATCH | `/api/profile` | 昵称/密码/通知偏好 |

## 3. 管理员后台（`role=admin`，`require_admin`）
### 数据统计
| GET | `/api/admin/stats/overview` | 用户数、今日生成量、成功率、收入、池水位 |
| GET | `/api/admin/stats/timeseries` | 按天/小时的生成量、成功率、耗时 P95 |
### 用户管理
| GET | `/api/admin/users` | 分页/搜索 |
| PATCH | `/api/admin/users/{id}` | 启禁用、改角色、调余额(写 ledger `adjust`) |
| POST | `/api/admin/users/{id}/recharge` | 手动充值 → recharge_record + ledger |
### 充值/消费记录
| GET | `/api/admin/records/recharge` / `/consume` | 全量分页筛选、导出 |
### 系统配置
| GET/PUT | `/api/admin/config` | 模型开关与定价、池阈值、限流、公告等（jsonb KV） |
### 请求日志
| GET | `/api/admin/logs` | request_logs 分页筛选（脱敏），按 egress/status/账号hash |
### 账号池（运营）
| GET/POST | `/api/admin/accounts` | 导入(加密存)、列表(脱敏)、状态 |
| POST | `/api/admin/accounts/{id}/refresh` | 刷 token/额度 |

## 4. 对外生成 API（KEY 鉴权）

对外同时提供 **OpenAI 端点格式** 与 **Gemini 官方格式**，每个能力都有 **同步** 与 **异步** 两种调用方式。两套格式只是"协议适配层"，内部统一进同一条 `generation` 流水线（入队 → worker → 结果），计费/限流/日志一致。

### 4.0 同步 vs 异步（通用约定）
| 模式 | 行为 | 适用 |
|---|---|---|
| **同步** | 请求等待任务终态（默认 180s，可配置）。内部仍先持久化并入队，由 Redis 事件通知 API 进程 | 图片、短任务 |
| **异步** | 立即返回任务 ID/Operation，客户端轮询、SSE 或接收 webhook | 视频、长任务、批量并发 |

- OpenAI 端点可用 `?async=true` 或 `Prefer: respond-async`；Gemini 使用 `:predictLongRunning` / Operation 语义。
- 统一任务查询：`GET /v1/tasks/{task_id}`；产物：`GET /v1/tasks/{task_id}/content`。
- 可选 `callback_url`；终态回调使用 `X-Signature: sha256=<HMAC>`，失败指数退避且有最大次数。
- 同步等待超时返回 **504**（不是 408，避免与上游风控 408 混淆），错误 metadata 带 `task_id`；后台任务继续执行。
- `Idempotency-Key` 唯一约束；重复请求返回原任务，不重复生成和扣费。

### 4.1 OpenAI 端点格式（`/v1`，`Authorization: Bearer sk-...`）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/images/generations` | 文生图：`{model,prompt,n,size,quality,response_format}`；默认同步，`?async=true` 转异步 |
| POST | `/v1/images/edits` | **图片编辑** multipart：`image`/`image[]`、可选 `mask`、`prompt`、`model`、`n`、`size`。先上传为 reference blob，再按模型映射 `general/subject/style/frame` |
| POST | `/v1/videos/generations` | 文生视频/图生视频；默认异步，可 `?wait=true` 同步等待 |
| GET | `/v1/videos/{task_id}` | 查询视频任务；`/content` 下载 |
| GET | `/v1/tasks/{task_id}` | 统一查询图片/视频异步任务 |
| GET | `/v1/models` | 模型、能力、尺寸、价格 |

同步图片响应兼容 `{created,data:[{url|b64_json,revised_prompt}]}`；异步响应为 `{id,object:"generation.task",status,created_at}`。错误采用 OpenAI `{error:{message,type,param,code}}`。

### 4.2 Gemini 官方格式（`/v1beta`，`x-goog-api-key` 或 Bearer KEY）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1beta/models/{model}:generateContent` | 同步生成；解析 `contents[].parts[]` 的 `text`、`inlineData`、`fileData` |
| POST | `/v1beta/models/{model}:streamGenerateContent` | SSE；先发状态事件，终态返回 Gemini `GenerateContentResponse` |
| POST | `/v1beta/models/{model}:predict` | Imagen 风格同步图片生成/编辑 |
| POST | `/v1beta/models/{model}:predictLongRunning` | 异步图片/视频，返回 `{name:"operations/{id}",done:false}` |
| GET | `/v1beta/operations/{id}` | 查询 Operation；完成后返回 `response` 或 `error` |
| POST | `/upload/v1beta/files` | 可选兼容文件上传，返回可在 `fileData.fileUri` 引用的资源 |

- 图片编辑通过 `contents.parts` 同时传文本和 `inlineData`/`fileData`；多图顺序保留，mask 通过扩展字段 `generationConfig.mask`。
- 返回图片放在 `candidates[].content.parts[].inlineData`（小图/b64）或 `fileData`（大图/视频 URL）。
- Gemini 风格错误采用 `{error:{code,message,status,details}}`；内部错误码映射到标准 HTTP/Google RPC status。
- 两套协议共用同一任务、幂等、计费、内容审核和资源归属校验，不维护两套业务逻辑。

## 5. 内网 admin-api（采集器 → ARP 池）
| POST | `/admin-api/adobe/arp` | body `{tokens:[{t,ua,ch,proxy_url,egress_id}...]}`；入池并按请求 round-robin |
| GET | `/admin-api/adobe/arp` | 池健康：`{present,ready,minting,oldest_age_sec,paired_with_egress}` |
| POST | `/admin-api/adobe/arp/invalidate` | `{id}` 作废（408 后）|
- 鉴权 `X-Internal-Token=ARP_INTERNAL_TOKEN` + IP 白名单，**绝不暴露公网**。

## 错误码约定（节选）
| code | 含义 |
|---|---|
| 0 | ok |
| 1001 | 未登录/token 失效 |
| 1003 | 无权限 |
| 2001 | 余额不足 |
| 3001 | 无可用 ARP（排队重试）|
| 3002 | 上游临时不可用（408/5xx，已重试）|
| 3003 | 内容被拒绝（451）|
| 3004 | 账号额度耗尽/限速 |
