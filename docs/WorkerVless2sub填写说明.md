# WorkerVless2sub / edgetunnel 填写说明

适用站点：https://octagoningly-vpn.pages.dev （edgetunnel）  
数据来源：NodeBench 发布的 CF 优选端点（本仓库 `public` 分支）

## 一、复制下面两个 URL

在你的 **WorkerVless2sub / edgetunnel 管理页** 里找到对应变量（通常在「优选地址 / 机场源 / 环境变量」区域）：

| 变量 | 填写内容 |
| ---|---|
| **ADDAPI**（优选地址 API） | `https://raw.githubusercontent.com/octagoningly/vpn_node_search/public/cf-addapi.txt` |
| **ADDCSV**（优选地址 CSV） | `https://raw.githubusercontent.com/octagoningly/vpn_node_search/public/cf-addcsv.csv` |

> 若管理页使用「订阅地址 / 自定义优选」等名称：凡是**接收一行一个 `IP:PORT` 列表**的填 ADDAPI；凡是**接收 CSV 表格**的填 ADDCSV。

## 二、DLS（下载速度筛选）

ADDCSV 的 `速度(MB/s)` 列是实测下载速度（单位 **MB/s**）。

| 建议 | 值 |
|---|---|
| 放宽（更多节点） | `DLS=2` |
| 均衡（推荐起步） | `DLS=4` |
| 严格（只要快的） | `DLS=5` |

当前一版数据约 36 个入榜端点，速度大致在 4–5.5 MB/s，起步可用 `DLS=4`。

## 三、你还必须自己配置的字段（NodeBench 不会生成订阅）

WorkerVless2sub 只用优选地址**替换落地 IP**，下面这些必须是**你自己**的：

| 字段 | 含义 |
|---|---|
| **HOST** | 你的 Worker/Pages 域名，例如 `octagoningly-vpn.pages.dev` |
| **UUID** | 你的 VLESS UUID |
| **PATH** | 你的 WS/gRPC 路径 |
| **SNI / 节点名** | 通常与 HOST 相同 |

填完后，订阅链接由 WorkerVless2sub 生成；本项目**故意不伪造**这些字段。

## 四、更新数据

本机重新测速并发布：

```powershell
uv run nodebench run --profile cf-user-publish
# 或对已有 run：
uv run nodebench publish --run-id <run_id> --profile cf-user-publish
```

然后把 `output/publish-staging/` 里的四个文件推到仓库 `public` 分支（覆盖同名文件即可）。  
WorkerVless2sub 侧无需改 URL，下次拉取自动用新数据。

## 五、文件说明

| 文件 | 用途 |
|---|---|
| `cf-addapi.txt` | ADDAPI：一行 `HOST:PORT` |
| `cf-addcsv.csv` | ADDCSV：九列 iptest 风格 |
| `report.json` | 脱敏报告（含 `consumer_hints`） |
| `manifest.json` | SHA-256 清单 |

**不会上传**代理节点 URI、私有凭据或数据库。
