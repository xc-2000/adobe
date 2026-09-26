# harvester · ARP 采集器（独立进程）

[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium)（148 指纹）+ Playwright 驱动 + 住宅 sticky 出口，持续铸 `sid/ark/bfp/ftr` 并抓 UA/Client-Hints，POST 到平台 `admin-api/adobe/arp` 入池。**与业务库零耦合，只走 HTTP。禁止用 Playwright 自带 Chromium 铸票。**

**技术支持 QQ 群：19302577**

原理与不变量见 [`../docs/02-arp-core.md`](../docs/02-arp-core.md)、[`../docs/12-minting-and-config.md`](../docs/12-minting-and-config.md)。运行见 [`../docs/07-dev-setup.md`](../docs/07-dev-setup.md)。采集器只服务 **GPT Image 2.5** 网页票；香蕉 / Image 2 / 视频走 FF-iOS，不经过本进程。

采集器**不自己拼代理串**。每次铸票向面板租一枚新 sticky SID（`POST /admin-api/adobe/harvester/lease`），把返回的 `proxy_url` 原样交给本机 HTTP 中继（Chrome 只连 `127.0.0.1`），再原样写入 ARP bundle。入池前浏览器出口 IP 必须与 lease URL 探到的 IP 一致。铸票规则（浏览器数、间隔、TTL、水位、代理模板）全部读配置中心。进程内有看门狗：按 `mint_browser_count` 热加载 worker、CONNECT 失败全员暂停、崩溃自动拉起、铸票硬超时、浏览器 `close()` 卡住则杀掉进程树，避免僵尸 Chrome 把机器打满。

票据 TTL 由 Redis `PX` 到期自动销毁，采集器不负责扫库。间隔必须小于 TTL，否则池会空窗、全站无法出图。

## 配置（进程级 env）

- `HARVESTER_TARGET` / `ARP_INTERNAL_TOKEN`：内网 admin-api。
- `ADOBE_CHROME_BINARY`：fingerprint-chromium 148 的 `chrome.exe`。未设则用仓库 `vendor/fingerprint-chromium/chrome.exe`。
- Chrome 版本写死 `148.0.7778.215`，不能用 env 改。
- `HARVESTER_ONCE=1`：只铸一枚，用于受控冒烟。
- `HEADLESS`、`ARP_MINT_TIMEOUT`、`HARVESTER_BLOCK_RESOURCE_TYPES`。
- `ARP_PROXY_FILE`：仅当面板代理未配置时的紧急 override；文件里必须是**已展开**的 sticky URL，禁止再套一层模板。面板代理就绪后忽略该文件。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
$env:HARVESTER_ONCE = "1"
.\.venv\Scripts\python -m arp_harvester.main
```

日志只输出 ARP id、egress id、source（panel/file）和掩码后的出口 IP，不输出代理账密、ARP token 或浏览器路径里的无关密钥。
