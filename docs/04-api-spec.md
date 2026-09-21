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

## 4. 对外生成 API（`/v1`，OpenAI 兼容风格，KEY 鉴权）
| POST | `/v1/images/generations` | `{model,prompt,size,n,...}` → 兼容返回 `{data:[{url}]}` |
| POST | `/v1/videos/generations` | 异步：返回 `{id,status}`，`GET /v1/videos/{id}` 轮询，`/content` 取产物 |
| GET | `/v1/models` | 模型列表 |
- 按 KEY 限流 + 计费；错误映射为兼容错误体。内容拒绝(451)如实返回 `content_policy_violation`。

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
