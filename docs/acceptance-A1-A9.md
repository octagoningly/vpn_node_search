# A1–A9 验收报告

审计基准：`开发规则.md` §5.1 验收表。
审计方式：离线验收测试 `tests/test_acceptance_A1_A9.py`（40 项）+ 既有套件回归。
全量测试：**679 passed**（含本批 40 项验收测试）。

---

## A1 安装与诊断

| 项 | 结论 |
| --- | --- |
| 状态 | **通过**（平台差异待真机） |
| 证据 | `test_a1_doctor_missing_mihomo_exits_nonzero_with_fix_path`、`test_a1_doctor_missing_config_reports_specific_item`、`test_a1_doctor_token_absent_is_info_not_failure`、`test_a1_doctor_token_set_never_prints_value`、`test_a1_doctor_ok_when_prerequisites_resolve`、`test_a1_doctor_platform_python_check_present`；既有 `test_cli.py` 同类 6 项 |
| 覆盖 | 缺 Mihomo → exit 2 + `(fix: …)`；缺 `probe.cf.target_host` → exit 2 + 具体配置项；Token 缺失仅 INFO 不拉闸；Token 值不出现在输出；前置齐全 → exit 0 + `core ready`；无 Traceback |
| 待真机 | macOS/Linux 上 `nodebench doctor` 对 launchd/systemd 依赖的实际识别（本机仅 Windows） |

## A2 多源采集

| 项 | 结论 |
| --- | --- |
| 状态 | **通过**（真实订阅/GitHub 限额待真机） |
| 证据 | `test_a2_candidates_carry_source_id_and_license_tag`、`test_a2_local_source_enforces_file_limit`、`test_a2_one_source_failure_does_not_block_others`、`test_a2_diagnostics_do_not_leak_full_uri`、`test_a2_subscription_budget_limit_is_recorded`；既有 `test_sources_budget.py`、`test_sources_remote.py` |
| 覆盖 | RawItem 带 `source_id`/`license_tag`；`MAX_SOURCE_FILES` 截断产生 `file_limit_exceeded` 且记入 `truncated_sources`；单源失败不阻断其他源；诊断与序列化输出中 UUID/密码被 `redact`；budget 键写入 `limits` |
| 待真机 | 真实获准订阅 URL 的 `max_files_per_run` 与 GitHub 搜索限额在网侧的表现 |

## A3 解析与去重

| 项 | 结论 |
| --- | --- |
| 状态 | **通过** |
| 证据 | `test_a3_same_connection_params_merge_and_keep_sources`、`test_a3_different_transport_keeps_two_items`、`test_a3_cf_endpoints_counted_independently`、`test_a3_unsupported_items_give_reason`；既有 `test_normalize_dedupe.py` 10 项（含跨源 `source_ids` 合并） |
| 覆盖 | 相同连接参数合并（`proxy_nodes=1`、`dupes_merged≥1`）；不同 transport 保留 2 项；CF 端点独立计数且同 IP 不同端口为 2 条；不支持行产出 `ParseIssue`（含 `code`/`message_redacted`） |
| 待办 | 无 |

## A4 真探测

| 项 | 结论 |
| --- | --- |
| 状态 | **通过**（真实二进制联调待真机） |
| 证据 | `test_a4_tcp_success_is_not_proxy_available`、`test_a4_cf_incompatible_is_not_exported`、`test_a4_measurements_carry_units_time_and_runner_id`、`test_a4_simulated_stand_in_is_not_counted_available`、`test_a4_scoring_rejects_simulated_when_real_required`、`test_a4_missing_binary_marks_not_available`；既有 `test_probe_contract.py` 25 项 |
| 覆盖 | FAIL 结果 `usable_real=0` 且 scoring `filtered`（TCP 成功≠可用）；`host_compatible=False` 不进入 `cf-addapi`/`cf-addcsv`；`speed_unit`/`measured_at`/`runner_id` 齐备；SIMULATED 不计入 `usable_real` 且 `require_real_probe_success` 过滤；缺二进制 → exit 4 |
| 待真机 | 真实 Mihomo / CloudflareSpeedTest 二进制下 `nodebench run --profile local` 的可用/不可用/CF 不兼容三类样本 |

## A5 情报与历史

| 项 | 结论 |
| --- | --- |
| 状态 | **通过**（真实信誉服务待真机） |
| 证据 | `test_a5_exit_ip_from_proxy_echo`、`test_a5_history_sample_counts_across_runs`、`test_a5_reputation_failure_is_unknown_and_keeps_history`；既有 `test_intelligence_service.py` 20 项、`test_history_db.py` 12 项 |
| 覆盖 | 出口 IP 从代理回显取得并计 `exit_ok`；三轮历史 `executed=3 / succeeded=2 / sample_count=2`；信誉服务抛错 → `status=failed`、`risk_level=unknown`、`risk=None`，`probe_observations` 与 `history_for_items` 不被清空 |
| 待真机 | 真实信誉 API 故障时的行为与缓存 TTL |

## A6 输出兼容

| 项 | 结论 |
| --- | --- |
| 状态 | **通过** |
| 证据 | `test_a6_report_json_schema_parseable`、`test_a6_cf_csv_has_nine_columns_with_units`、`test_a6_proxy_uri_does_not_mix_cf_output`、`test_a6_manifest_sha256_matches_files`、`test_a6_workervless2sub_csv_import_roundtrip`、`test_a6_addapi_line_pattern`；既有 `test_exporters.py` 31 项 |
| 覆盖 | `report.json` 可解析且含 `schema_version`/`run_id`/`counts`；`cf-addcsv.csv` 九列与 `TCP延迟(ms)`/`速度(MB/s)` 表头一致；代理 URI 不混入 CF 输出且 CF 文件不含 `vless://`；manifest 每个文件 SHA-256/size 与磁盘一致；九列 CSV 经 `parse_endpoint_csv` 往返导入字段保持 |
| 待真机 | 将 `cf-addapi.txt` / `cf-addcsv.csv` 真实填入 WorkerVless2sub `ADDAPI`/`ADDCSV` 的一次导入验证 |

## A7 一键与定时

| 项 | 结论 |
| --- | --- |
| 状态 | **通过**（Actions 手动触发待真机） |
| 证据 | `test_a7_run_scripts_invoke_shared_cli`、`test_a7_scheduler_owns_only_project_tasks`、`test_a7_scheduler_uninstall_is_reversible_plan`、`test_a7_run_id_marks_distinct_run_points`；既有 `test_scripts.py` 18 项、`test_scheduler_*.py` 84 项 |
| 覆盖 | `run.ps1`/`run.sh`/`run.bat` 调用同一 `nodebench run`；`ensure_owned_name` 拒绝非 `nodebench-*` 任务；schtasks `plan(install)` 与 `plan(uninstall)` 成对可撤销；每次运行生成独立 `run_id` 并携带 `runner_id` |
| 待真机 | `nodebench scheduler install/status/uninstall` 在 Windows/macOS/Linux 实机执行；GitHub Actions `workflow_dispatch` 手动运行一次并确认运行点标记 |

## A8 发布与失败

| 项 | 结论 |
| --- | --- |
| 状态 | **通过**（真实无效凭据/过期缓存待真机） |
| 证据 | `test_a8_empty_source_yields_failed_status_and_exit`、`test_a8_error_csv_produces_diagnostics`、`test_a8_publish_failure_keeps_previous_public`、`test_a8_empty_content_files_do_not_overwrite_public`、`test_a8_no_secrets_in_public_report`；既有 `test_publish.py` 17 项、`test_publish_gates.py` 10 项 |
| 覆盖 | 空源 → `status=failed` + exit 3；错误 CSV → `parse_issues≥1` + diagnostics；发布失败/门禁阻断时 `output/latest` 旧文件保留；`entry_count=0` 的空内容文件被排除不覆盖；公开 report 经 `scan_text` 零泄密 |
| 待真机 | 无效 GitHub Token / 过期缓存触发的真实诊断与 `output/latest` 保护 |

## A9 回归

| 项 | 结论 |
| --- | --- |
| 状态 | **通过** |
| 证据 | `uv run pytest tests/` → `679 passed`；`test_a9_offline_suite_collects_expected_modules` 断言解析/去重/契约/评分/导出/发布/编排/历史模块齐备 |
| 覆盖 | 真实外网测试仍隔离在 `test_sources_remote.py` 等套件中，CI 默认不依赖公开免费节点 |
| 待办 | 无 |

---

## 实现缺口与处理

本次审计**未发现需修复的实现缺口**。下列行为经测试验证已符合 §5.1：

- `usable_real` 仅统计 `status=ok` 且 `probe_mode=real`，替身不计可用（`orchestrator._probe_summary`）。
- CF `host_compatible=False` → scoring `filters_failed=["compatibility"]`，`build_export` 仅导出 `status=ranked` 项。
- 信誉失败写 `Status.FAILED` + `risk_level=unknown`，`record_intelligence` 不删除 `probe_observations`。
- 发布使用 staging + 原子替换，失败时恢复 `previous`；空 `entry_count` 文件经 `_exclusions` 排除。

## 待真机清单汇总

1. macOS/Linux `nodebench doctor` 平台依赖识别（A1）。
2. 真实订阅 / GitHub 限额与失败隔离（A2）。
3. 真实 Mihomo / CFST 二进制三类样本（A4）。
4. 真实信誉 API 故障（A5）。
5. WorkerVless2sub 真实导入（A6）。
6. 三平台 scheduler 实机安装/卸载 + Actions 手动触发（A7）。
7. 无效凭据 / 过期缓存真实诊断（A8）。
