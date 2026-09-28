# GitHub Actions

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `offline.yml` | push / PR to `main`, `develop` | offline contract tests + CLI smoke |
| `daily.yml` | `workflow_dispatch` + cron `37 3 * * *` (UTC) | daily doctor + run + safe diagnostics artifacts |
| `collect.yml` | `workflow_dispatch` + cron `27 1 * * *` (UTC) | GitHub 代码搜索扩候选池（actions-collect profile，只 collect+score 粗筛） |

## Actions 发现候选 vs 本机测速

- **Actions（collect.yml）**：用内置 `GITHUB_TOKEN` 跑 GitHub 代码搜索，发现 CF 优选/ADDAPI 候选来源，配合本地候选清单做 **collect + score 粗筛**，扩大 IP 候选池。产物是脱敏候选清单（`run-report.json` / `scored.json`），不含 token、不含订阅原文。
- **测速口径**：Actions 机房测的是 **GitHub 网络**，不是用户本地网络。延迟/下载结果**不能**代表你本机的体验，因此 `actions-collect` 默认关闭 probe（proxy/CF 测速都关）。
- **本机测速**：真实测速请在本机跑（`local-real` / `auto-collect` 等 profile，开启 probe.cf / probe.proxy），以本机测出的延迟/速度为准。Actions 发现的候选可通过 `--import-candidates` 导入本机后再测。
- 分工：**Actions 负责“发现更多候选”，本机负责“测速优选”**。

**collect.yml notes**

- Cron `27 1 * * *` is **UTC** (= 09:27 Asia/Shanghai). GitHub schedule has no timezone field.
- Manual run: Actions → **NodeBench Collect** → *Run workflow*；可选输入 `queries`（覆盖 GitHub 搜索查询）与 `max_files`（覆盖单次抓取文件上限）。
- Built-in `GITHUB_TOKEN` 注入给 `sources.github` 做 code search（不回显、不进 artifact）。若 403/限流，源会降级并把诊断写进 `run-report.json`；可选 secret `NODEBENCH_GITHUB_TOKEN` 作为备用 token。
- Profile：`config/profiles/actions-collect.yaml`（github 开、cf 候选可配、proxy 关、probe 默认关、publish 默认关）。
- Artifacts never include `nodebench.db`, tokens, private URIs, raw subscription text, `proxy-raw.txt` / `proxy-clash.yaml` / `cf-addapi.txt`.

**daily.yml notes**

- Cron `37 3 * * *` is **UTC** (= 11:37 Asia/Shanghai). GitHub schedule has no timezone field.
- Manual run: Actions → **NodeBench Daily** → *Run workflow*.
- Optional repo **secrets**: `NODEBENCH_GITHUB_TOKEN`, `NODEBENCH_REPUTATION_API_KEY` (injected only where needed; never echoed).
- Publish is **off by default** (separate step). Enable with repo **variable** `NODEBENCH_PUBLISH_ENABLED=true`.
- Artifacts never include `nodebench.db`, tokens, private URIs, or raw subscription text.
