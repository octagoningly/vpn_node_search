# GitHub Actions

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `offline.yml` | push / PR to `main`, `develop` | offline contract tests + CLI smoke |
| `daily.yml` | `workflow_dispatch` + cron `37 3 * * *` (UTC) | daily doctor + run + safe diagnostics artifacts |

**daily.yml notes**

- Cron `37 3 * * *` is **UTC** (= 11:37 Asia/Shanghai). GitHub schedule has no timezone field.
- Manual run: Actions → **NodeBench Daily** → *Run workflow*.
- Optional repo **secrets**: `NODEBENCH_GITHUB_TOKEN`, `NODEBENCH_REPUTATION_API_KEY` (injected only where needed; never echoed).
- Publish is **off by default** (separate step). Enable with repo **variable** `NODEBENCH_PUBLISH_ENABLED=true`.
- Artifacts never include `nodebench.db`, tokens, private URIs, or raw subscription text.
