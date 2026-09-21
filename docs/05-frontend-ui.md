# 05 · 前端 UI 规范

## 风格定位
企业级 SaaS 后台管理系统（Enterprise Admin Dashboard）· 现代管理控制台 · 数据驱动后台 · 极简商务 UI · 云管理平台 · **蓝色科技感**。

- **布局**：经典左右布局 —— 左侧固定 `Sider`（深色或浅色可切，含 Logo + 菜单）+ 顶部 `Header`（面包屑 / 全局搜索 / 余额 / 消息 / 用户下拉 / 主题切换）+ 右侧 `Content`。用 Ant Design `ProLayout`。
- **配色**：主色 `#1677ff`；成功 `#52c41a`、警告 `#faad14`、错误 `#ff4d4f`；背景 `#f0f2f5`；卡片白底、圆角 8px、`box-shadow: 0 1px 2px rgba(0,0,0,.06)`。可选深色侧边栏 `#001529`。
- **字体/密度**：系统无衬线；信息密度适中，留白充足；数字用等宽或 `tabular-nums`。
- **组件**：指标卡（Statistic + 迷你趋势）、`ProTable`（分页/筛选/导出）、`ProForm`、`@ant-design/charts`（折线/柱/环形）。

## 导航（按角色渲染）
### 管理员菜单
```
数据统计        /admin/dashboard      指标卡 + 趋势图 + 池水位/成功率
用户管理        /admin/users          ProTable：搜索、启禁用、调余额、手动充值
充值记录        /admin/recharge       流水表 + 导出
消费记录        /admin/consume        流水表 + 按模型/用户筛选
系统配置        /admin/config         模型开关与定价、池阈值、限流、公告
请求日志        /admin/logs           脱敏审计表：egress/status/耗时/重试
（账号池        /admin/accounts       运营：导入/状态，敏感脱敏）
```
### 用户菜单
```
创作中心        /create               左选模型/参数，右预览；文生图/图生图/文生视频/图生视频
生成历史        /history              网格/列表 + 状态标签 + 重新生成/下载
KEY 管理        /keys                 创建(明文一次)/吊销/限流
调用说明        /docs                 API 文档 + 示例(curl/python)
余额明细        /billing              余额卡 + ledger 流水 + 消费/充值
个人设置        /settings             资料/改密/通知
```

## 关键页面交互
- **创作中心**：模型下拉（含单价与能力提示）→ 参数区（宽高比、分辨率、时长、参考图上传、seed）→ 提交后进入任务卡，WS/轮询显示 `queued→running→succeeded`，出结果内联预览 + 下载。余额不足前置拦截。
- **数据统计**：顶部 4 指标卡（今日生成/成功率/收入/在线用户）+ 主趋势折线 + ARP 池水位实时（ready/minting/oldest_age）+ 按 egress 成功率环形。
- **请求日志**：只读、脱敏、可按 status/egress/账号hash/时间筛，用于排障（配合 [02 §8](./02-arp-core.md) 状态码语义）。

## 工程约定
- Vite + React 18 + TS 严格模式；`react-router` v6 角色分区路由；`@tanstack/react-query` 管服务端态；`zustand` 管轻量全局态。
- `services/http.ts` 统一 axios：注入 JWT、401 跳登录、全局 `message.error`。
- 主题用 antd v5 `ConfigProvider` token，不散落魔法色值；响应式 `xs~xxl`。
- 金额/积分展示由后端最小单位换算显示，前端不做计费判断；不存任何上游密钥。
