"""NodeBench Desktop — clean launcher GUI.

Light palette, one accent, generous whitespace. First-run wizard guides
GitHub / edgetunnel / API keys; main window runs the pipeline and edits
scoring / country / export preferences.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover
    print("tkinter is required for the GUI", file=sys.stderr)
    raise

# ── Design tokens ────────────────────────────────────────────────────────
BG = "#f6f7f9"
CARD = "#ffffff"
INK = "#111827"
MUTED = "#6b7280"
LINE = "#e5e7eb"
ACCENT = "#2563eb"
ACCENT_SOFT = "#eff6ff"
OK = "#059669"
WARN = "#d97706"
DANGER = "#dc2626"

FONT = ("Segoe UI", 10)
FONT_TITLE = ("Segoe UI", 16, "bold")
FONT_H2 = ("Segoe UI", 12, "bold")
FONT_SMALL = ("Segoe UI", 9)

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config" / "default.yaml"
PROFILE_PATH = ROOT / "config" / "profiles" / "auto-collect.yaml"


def project_root() -> Path:
    """Works both from source and from a frozen exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT


# ── Small helpers ────────────────────────────────────────────────────────
def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def save_env(values: dict[str, str]) -> None:
    lines = [
        "# NodeBench local secrets — never commit this file",
        f"GITHUB_TOKEN={values.get('GITHUB_TOKEN', '')}",
        f"ABUSEIPDB_KEY={values.get('ABUSEIPDB_KEY', '')}",
        f"IPINFO_TOKEN={values.get('IPINFO_TOKEN', '')}",
    ]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def first_run_done() -> bool:
    return bool(load_env().get("GITHUB_TOKEN") or load_env().get("ABUSEIPDB_KEY"))


# ── Widgets ──────────────────────────────────────────────────────────────
class Card(ttk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, style="Card.TFrame", padding=18, **kw)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("NodeBench · 优选节点测评")
        self.geometry("960x640")
        self.minsize(880, 560)
        self.configure(bg=BG)
        self._setup_style()
        self._build()
        if not first_run_done():
            self.after(200, self.open_wizard)

    # ── style ──
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=FONT, background=BG, foreground=INK)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Card.TLabel", background=CARD, foreground=INK)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=FONT_SMALL)
        style.configure("Title.TLabel", background=BG, foreground=INK, font=FONT_TITLE)
        style.configure("H2.TLabel", background=CARD, foreground=INK, font=FONT_H2)
        style.configure("Accent.TButton", background=ACCENT, foreground="white", padding=(16, 10))
        style.map("Accent.TButton", background=[("active", "#1d4ed8")])
        style.configure("Ghost.TButton", padding=(12, 8))
        style.configure("TEntry", padding=6)
        style.configure("TCheckbutton", background=CARD, foreground=INK)
        style.configure("Horizontal.TProgressbar", troughcolor=LINE, background=ACCENT)

    # ── layout ──
    def _build(self) -> None:
        # header
        head = tk.Frame(self, bg=BG, padx=28, pady=18)
        head.pack(fill="x")
        tk.Label(head, text="NodeBench", bg=BG, fg=INK, font=FONT_TITLE).pack(side="left")
        tk.Label(
            head,
            text="自动搜集 · 实测 · 评分 · 发布到 edgetunnel",
            bg=BG, fg=MUTED, font=FONT_SMALL,
        ).pack(side="left", padx=14)

        ttk.Button(head, text="首次设置向导", style="Ghost.TButton",
                   command=self.open_wizard).pack(side="right")
        ttk.Button(head, text="打开输出目录", style="Ghost.TButton",
                   command=self.open_output).pack(side="right", padx=8)

        # body: left nav + content
        body = tk.Frame(self, bg=BG, padx=28, pady=24)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        nav = tk.Frame(body, bg=BG, width=180)
        nav.grid(row=0, column=0, sticky="ns", padx=(0, 20))
        self._nav_btns: dict[str, tk.Button] = {}
        for key, label in (
            ("run", "运行"),
            ("settings", "评分与筛选"),
            ("export", "导出格式"),
            ("keys", "密钥与账号"),
        ):
            b = tk.Button(
                nav, text=label, anchor="w", bd=0, padx=14, pady=12,
                bg=BG, fg=MUTED, activebackground=ACCENT_SOFT,
                activeforeground=ACCENT, font=FONT, cursor="hand2",
                command=lambda k=key: self.show(k),
            )
            b.pack(fill="x", pady=2)
            self._nav_btns[key] = b

        self.content = tk.Frame(body, bg=BG)
        self.content.grid(row=0, column=1, sticky="nsew")

        self.views: dict[str, tk.Frame] = {}
        self._build_run_view()
        self._build_settings_view()
        self._build_export_view()
        self._build_keys_view()
        self.show("run")

    def show(self, key: str) -> None:
        for name, frame in self.views.items():
            frame.pack_forget()
            self._nav_btns[name].configure(bg=BG, fg=MUTED)
        self.views[key].pack(fill="both", expand=True)
        self._nav_btns[key].configure(bg=ACCENT_SOFT, fg=ACCENT)

    # ── Run view ──
    def _build_run_view(self) -> None:
        frame = tk.Frame(self.content, bg=BG)
        self.views["run"] = frame

        card = Card(frame)
        card.pack(fill="x", pady=12)
        tk.Label(card, text="本地实测", background=CARD, fg=INK, font=FONT_H2).pack(anchor="w")
        tk.Label(
            card,
            text="联网搜集候选 → CFST 实测 → 评分 → 发布到 public 分支，edgetunnel 自动拉取。",
            background=CARD, fg=MUTED, font=FONT_SMALL, wraplength=640, justify="left",
        ).pack(anchor="w", pady=8)

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x")
        self.run_btn = ttk.Button(row, text="开始运行", style="Accent.TButton",
                                  command=self.start_run)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(row, mode="indeterminate", length=220)
        self.progress.pack(side="left", padx=16)

        self.run_status = tk.Label(card, text="就绪", background=CARD, fg=MUTED,
                                   font=FONT_SMALL, anchor="w", justify="left")
        self.run_status.pack(fill="x", pady=12)

        log_card = Card(frame)
        log_card.pack(fill="both", expand=True, pady=12)
        tk.Label(log_card, text="运行日志", background=CARD, fg=INK, font=FONT_H2).pack(anchor="w")
        self.log = tk.Text(log_card, height=14, bg="#fafafa", fg=INK, relief="flat",
                           font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True, pady=8)

    # ── Settings view ──
    def _build_settings_view(self) -> None:
        frame = tk.Frame(self.content, bg=BG)
        self.views["settings"] = frame

        card = Card(frame)
        card.pack(fill="both", expand=True)
        tk.Label(card, text="评分权重（0–1，可只填几项）", font=FONT_H2, fg=INK, background=CARD).pack(anchor="w")
        tk.Label(card, text="速度是硬性门槛，低于阈值直接淘汰。", font=FONT_SMALL, fg=MUTED, background=CARD).pack(anchor="w", pady=8)

        self.weight_vars: dict[str, tk.StringVar] = {}
        defaults = {
            "compatibility": "0.25", "latency": "0.17", "speed": "0.17",
            "purity": "0.15", "stability": "0.15", "loss": "0.11",
        }
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x")
        for i, (key, default) in enumerate(defaults.items()):
            tk.Label(grid, text=key, background=CARD, width=14, anchor="w").grid(
                row=i // 3, column=(i % 3) * 2, sticky="w", pady=4)
            var = tk.StringVar(value=default)
            self.weight_vars[key] = var
            tk.Entry(grid, textvariable=var, width=8).grid(
                row=i // 3, column=(i % 3) * 2 + 1, padx=(0, 18), pady=4)

        sep = tk.Frame(card, bg=LINE, height=1)
        sep.pack(fill="x", pady=16)

        tk.Label(card, text="硬性门槛", background=CARD, fg=INK, font=FONT_H2).pack(anchor="w")
        gate = tk.Frame(card, bg=CARD)
        gate.pack(fill="x", pady=8)
        tk.Label(gate, text="最小速度 (MB/s)", background=CARD, width=18, anchor="w").grid(
            row=0, column=0, sticky="w", pady=4)
        self.min_speed = tk.StringVar(value="0.5")
        tk.Entry(gate, textvariable=self.min_speed, width=10).grid(row=0, column=1, sticky="w")
        tk.Label(gate, text="最大延迟 (ms)", background=CARD, width=18, anchor="w").grid(
            row=1, column=0, sticky="w", pady=4)
        self.max_latency = tk.StringVar(value="800")
        tk.Entry(gate, textvariable=self.max_latency, width=10).grid(row=1, column=1, sticky="w")

        tk.Label(card, text="国家筛选（留空=全部；逗号分隔，如 US,JP,SG）",
                 background=CARD).pack(anchor="w", pady=8)
        self.allowed_countries = tk.StringVar(value="")
        tk.Entry(card, textvariable=self.allowed_countries).pack(fill="x")

        ttk.Button(card, text="保存评分设置", style="Accent.TButton",
                   command=self.save_settings).pack(anchor="w", pady=16)

    # ── Export view ──
    def _build_export_view(self) -> None:
        frame = tk.Frame(self.content, bg=BG)
        self.views["export"] = frame

        card = Card(frame)
        card.pack(fill="both", expand=True)
        tk.Label(card, text="导出格式", background=CARD, fg=INK, font=FONT_H2).pack(anchor="w")
        tk.Label(
            card,
            text="ADDAPI 备注模板 — 字段：{speed} {purity} {stability} {country}",
            background=CARD, fg=MUTED, font=FONT_SMALL,
        ).pack(anchor="w", pady=8)

        self.remark_template = tk.StringVar(value="speed-purity-stability-country")
        tk.Entry(card, textvariable=self.remark_template).pack(fill="x")

        tk.Label(card, text="预览", background=CARD, fg=MUTED, font=FONT_SMALL).pack(
            anchor="w", pady=8)
        self.remark_preview = tk.Label(
            card,
            text="104.17.29.227:8443#3.9-1.00-1.00-美国",
            background="#fafafa", fg=INK, font=("Consolas", 10),
            anchor="w", padx=10, pady=8,
        )
        self.remark_preview.pack(fill="x")
        self.remark_template.trace_add("write", lambda *_: self._update_preview())

        ttk.Button(card, text="保存导出设置", style="Accent.TButton",
                   command=self.save_export).pack(anchor="w", pady=16)

        info = tk.Label(
            card,
            text=(
                "发布目标：仓库 public 分支 raw 链接\n"
                "https://raw.githubusercontent.com/<你>/<仓库>/public/cf-addapi.txt\n"
                "把该链接填进 edgetunnel「自定义优选」即可，之后自动更新。"
            ),
            background=CARD, fg=MUTED, font=FONT_SMALL, justify="left",
        )
        info.pack(anchor="w", pady=8)

    def _update_preview(self) -> None:
        tpl = self.remark_template.get().strip() or "speed-purity-stability-country"
        sample = {
            "speed": "3.9", "purity": "1.00",
            "stability": "1.00", "country": "美国",
        }
        out = tpl
        for k, v in sample.items():
            out = out.replace("{" + k + "}", v)
        if "{speed}" not in tpl and "speed" in tpl.split("-"):
            parts = tpl.split("-")
            parts = [sample.get(p, p) for p in parts]
            out = "-".join(parts)
        self.remark_preview.configure(text=f"104.17.29.227:8443#{out}")

    # ── Keys view ──
    def _build_keys_view(self) -> None:
        frame = tk.Frame(self.content, bg=BG)
        self.views["keys"] = frame

        card = Card(frame)
        card.pack(fill="both", expand=True)
        tk.Label(card, text="密钥与账号", background=CARD, fg=INK, font=FONT_H2).pack(anchor="w")
        tk.Label(
            card,
            text="密钥只保存在本机 .env，不会上传。留空表示该项不启用。",
            background=CARD, fg=MUTED, font=FONT_SMALL,
        ).pack(anchor="w", pady=8)

        self.key_vars: dict[str, tk.StringVar] = {}
        env = load_env()
        rows = (
            ("GITHUB_TOKEN", "GitHub Token（搜候选源）",
             "https://github.com/settings/tokens"),
            ("ABUSEIPDB_KEY", "AbuseIPDB Key（纯净度）",
             "https://www.abuseipdb.com/account/api"),
            ("IPINFO_TOKEN", "IPinfo Token（属地）",
             "https://ipinfo.io/signup"),
        )
        for name, label, url in rows:
            rowf = tk.Frame(card, bg=CARD)
            rowf.pack(fill="x", pady=6)
            tk.Label(rowf, text=label, background=CARD, width=28, anchor="w").pack(side="left")
            var = tk.StringVar(value=env.get(name, ""))
            self.key_vars[name] = var
            tk.Entry(rowf, textvariable=var, show="*").pack(side="left", fill="x", expand=True)
            tk.Button(rowf, text="注册", bd=0, fg=ACCENT, bg=CARD, cursor="hand2",
                      command=lambda u=url: webbrowser.open(u)).pack(side="left", padx=8)

        ttk.Button(card, text="保存密钥", style="Accent.TButton",
                   command=self.save_keys).pack(anchor="w", pady=16)

    # ── Wizard ──
    def open_wizard(self) -> None:
        Wizard(self)

    # ── actions ──
    def open_output(self) -> None:
        out = project_root() / "output"
        out.mkdir(parents=True, exist_ok=True)
        os.startfile(out) if os.name == "nt" else webbrowser.open(out.as_uri())

    def save_keys(self) -> None:
        save_env({k: v.get().strip() for k, v in self.key_vars.items()})
        messagebox.showinfo("已保存", "密钥已写入本机 .env")

    def save_settings(self) -> None:
        try:
            import yaml

            data: dict = {}
            if CONFIG_PATH.is_file():
                data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            scoring = data.setdefault("scoring", {})
            weights = scoring.setdefault("cf_weights", {})
            for key, var in self.weight_vars.items():
                try:
                    weights[key] = float(var.get())
                except ValueError:
                    pass
            filters = scoring.setdefault("filters", {})
            try:
                filters["min_speed_mb_s"] = float(self.min_speed.get())
            except ValueError:
                pass
            try:
                filters["max_latency_ms"] = float(self.max_latency.get())
            except ValueError:
                pass
            countries = [c.strip().upper() for c in self.allowed_countries.get().split(",") if c.strip()]
            filters["allowed_countries"] = countries
            CONFIG_PATH.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            messagebox.showinfo("已保存", "评分与筛选已写入 config/default.yaml")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    def save_export(self) -> None:
        try:
            import yaml

            data: dict = {}
            if CONFIG_PATH.is_file():
                data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            data["addapi_remark_template"] = self.remark_template.get().strip()
            CONFIG_PATH.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            messagebox.showinfo("已保存", "导出模板已写入 config/default.yaml")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    def start_run(self) -> None:
        self.run_btn.configure(state="disabled")
        self.progress.start(12)
        self.log.delete("1.0", "end")
        self.run_status.configure(text="运行中，请稍候 3–10 分钟…")
        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self) -> None:
        root = project_root()
        try:
            proc = subprocess.run(
                ["uv", "run", "nodebench", "run", "--profile", "auto-collect"],
                cwd=root, capture_output=True, text=True, encoding="utf-8",
                errors="replace", shell=(os.name == "nt"),
            )
            output = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:  # noqa: BLE001
            output = f"启动失败：{exc}"
            proc = None
        self.after(0, self._run_done, output, None if proc is None else proc.returncode)

    def _run_done(self, output: str, code: int | None) -> None:
        self.progress.stop()
        self.run_btn.configure(state="normal")
        self.log.insert("end", output[-8000:])
        self.log.see("end")
        if code == 0:
            self.run_status.configure(text="完成 · 结果已生成", fg=OK)
        elif code is None:
            self.run_status.configure(text="启动失败", fg=DANGER)
        else:
            self.run_status.configure(text=f"结束，退出码 {code}", fg=WARN)


class Wizard(tk.Toplevel):
    """Three-step first-run guide."""

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master)
        self.title("首次设置向导")
        self.geometry("620x520")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.step = 0
        self._build()

    def _build(self) -> None:
        self.header = tk.Label(self, text="", bg=BG, fg=INK, font=FONT_H2, anchor="w")
        self.header.pack(fill="x", padx=28, pady=16)
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=28, pady=12)
        nav = tk.Frame(self, bg=BG)
        nav.pack(fill="x", padx=28, pady=18)
        ttk.Button(nav, text="上一步", style="Ghost.TButton", command=self.prev).pack(side="left")
        self.next_btn = ttk.Button(nav, text="下一步", style="Accent.TButton", command=self.next)
        self.next_btn.pack(side="right")
        ttk.Button(nav, text="跳过", style="Ghost.TButton", command=self.destroy).pack(
            side="right", padx=8)
        self.render()

    def render(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        titles = [
            "1 / 4　创建 GitHub 仓库",
            "2 / 4　填写密钥",
            "3 / 4　edgetunnel 对接",
            "4 / 4　探测二进制",
        ]
        self.header.configure(text=titles[self.step])
        if self.step == 0:
            self._step_repo()
        elif self.step == 1:
            self._step_keys()
        elif self.step == 2:
            self._step_edgetunnel()
        else:
            self._step_binaries()

    def _text(self, s: str) -> None:
        tk.Label(self.body, text=s, bg=BG, fg=MUTED, font=FONT,
                 justify="left", wraplength=560, anchor="w").pack(fill="x", pady=4)

    def _step_repo(self) -> None:
        self._text(
            "推荐在 GitHub 新建一个公开仓库（例如 cf-ip-pool），用来托管优选地址文件。\n\n"
            "1. 打开 https://github.com/new\n"
            "2. 名称随意，选 Public\n"
            "3. 创建后无需添加文件\n\n"
            "本工具会把测速结果推到仓库的 public 分支，得到 raw 链接。"
        )
        tk.Button(self.body, text="打开 GitHub 新建仓库", bd=0, fg=ACCENT, bg=BG,
                  cursor="hand2",
                  command=lambda: webbrowser.open("https://github.com/new")).pack(
            anchor="w", pady=8)

    def _step_keys(self) -> None:
        self._text("把申请到的密钥粘贴进来。密钥只存本机 .env。")
        self.wiz_vars: dict[str, tk.StringVar] = {}
        env = load_env()
        for name, label, url in (
            ("GITHUB_TOKEN", "GitHub Token", "https://github.com/settings/tokens"),
            ("ABUSEIPDB_KEY", "AbuseIPDB Key", "https://www.abuseipdb.com/account/api"),
            ("IPINFO_TOKEN", "IPinfo Token", "https://ipinfo.io/signup"),
        ):
            row = tk.Frame(self.body, bg=BG)
            row.pack(fill="x", pady=6)
            tk.Label(row, text=label, bg=BG, width=18, anchor="w").pack(side="left")
            var = tk.StringVar(value=env.get(name, ""))
            self.wiz_vars[name] = var
            tk.Entry(row, textvariable=var, show="*").pack(side="left", fill="x", expand=True)
            tk.Button(row, text="注册", bd=0, fg=ACCENT, bg=BG, cursor="hand2",
                      command=lambda u=url: webbrowser.open(u)).pack(side="left", padx=8)

    def _step_edgetunnel(self) -> None:
        self._text(
            "到你的 edgetunnel / WorkerVless2sub 管理页（优选订阅生成）：\n\n"
            "1. 「自定义优选」框里填 raw 链接（格式如下，把 <你>/<仓库> 换掉）\n"
            "   https://raw.githubusercontent.com/<你>/<仓库>/public/cf-addapi.txt\n"
            "2. 保存后点「开始优选」\n\n"
            "以后每次本工具跑完，链接内容会自动更新，无需再改管理页。"
        )
        tk.Button(self.body, text="打开填写说明文档", bd=0, fg=ACCENT, bg=BG,
                  cursor="hand2",
                  command=lambda: webbrowser.open(
                      "https://github.com/octagoningly/vpn_node_search/blob/main/docs/WorkerVless2sub填写说明.md"
                  )).pack(anchor="w", pady=8)

    def _step_binaries(self) -> None:
        self._text(
            "测速需要两个外部二进制（放到 tools/ 目录）：\n\n"
            "· Mihomo — 代理探测\n"
            "· CloudflareSpeedTest — CF 优选测速\n\n"
            "如果只是测 CF 优选 IP，只放 CloudflareSpeedTest 也可以。\n"
            "项目不会自动下载二进制，请自行下载并核对哈希。"
        )
        self.next_btn.configure(text="完成")

    def next(self) -> None:
        if self.step == 1 and hasattr(self, "wiz_vars"):
            save_env({k: v.get().strip() for k, v in self.wiz_vars.items()})
        if self.step >= 3:
            self.destroy()
            messagebox.showinfo("完成", "设置已保存，可以点击「开始运行」了。")
            return
        self.step += 1
        self.render()

    def prev(self) -> None:
        if self.step <= 0:
            return
        self.step -= 1
        self.render()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
