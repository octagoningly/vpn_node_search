# NodeBench

本地节点测评与来源发现工具。

## 目录

- [`src/nodebench/`](src/nodebench/) — 源代码与模块
- `config/` — 配置文件 (YAML)
- `scripts/` — 本机一键运行与安装脚本
- `tests/` — 契约、解析、失败路径、导出样例测试
- `input/` — 本地样例（不含真实凭据）
- `output/` — 运行时产物（默认不提交）
- `.github/workflows/` — GitHub Actions 工作流

## 快速开始

```powershell
# 查看帮助
uv run nodebench --help

# 医院探测（需 Mihomo 二进制）
uv run nodebench doctor

# 运行完整流程（离线模式）
uv run nodebench run --profile local --dry-run

# 导出结果
uv run nodebench export --run-id <id>
```

## 配置

配置位于 `config/default.yaml`。主要区段：

- `probe.proxy`：Mihomo 二进制路径 (`mihomo_path`)、测速 URL (`speedtest_url`)
- `probe.cf`：CloudflareSpeedTest 二进制路径 (`cfst_path`)、目标 Host (`target_host`)
- `sources`：本地、订阅源、GitHub 搜索、CF 导入
- `scoring`：评分权重与过滤器
- `publish`：发布门禁（默认关闭）
- `scheduler`：系统定时任务

## 离线运行

本项目设计为**离线优先**。无法访问外网时可以使用内置样例：

```powershell
# 只使用本地样例运行
uv run nodebench run --profile local

# 订阅源与 GitHub 搜索在无网络时会被跳过（返回 not_run 模式）
```

## 二进制获取

Mihomo 与 CloudflareSpeedTest 为外部项目的二进制文件，需自行下载并放置。推荐放在 `tools/` 目录（已被 `.gitignore` 排除），或自定义路径配置：

```yaml
probe:
  proxy:
    mihomo_path: "tools/mihomo/mihomo.exe"
  cf:
    cfst_path: "tools/cfst/CloudflareSpeedTest.exe"
```

## 二进制获取与校验

Mihomo 与 CloudflareSpeedTest 为外部项目的二进制文件，需自行下载并放置。推荐放在 `tools/` 目录（已被 `.gitignore` 排除），或自定义路径配置：

```yaml
probe:
  proxy:
    mihomo_path: "tools/mihomo/mihomo.exe"
  cf:
    cfst_path: "tools/cfst/CloudflareSpeedTest.exe"
```

**校验哈希**：下载后请分别前往 Each 项目的官方发布页获取 SHA256 哈希，并与下载的二进制文件对比。项目不自动下载或更新二进制，校验工作由用户自行完成。

示例（PowerShell）:
```powershell
# 获取官方哈希后自行对比
Get-FileHash -Algorithm SHA256 tools/mihomo/mihomo.exe
```

## 许可证

本项目采用 MIT 许可证。第三方二进件（Mihomo、CloudflareSpeedTest 等）的许可证另行查阅，不影响本项目的使用。

## 贡献

见 `开发规则.md` 了解分阶段交付进度与贡献指南。