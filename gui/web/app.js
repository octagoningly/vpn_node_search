/* NodeBench web UI */
(function () {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  // ── toast ──────────────────────────────────────
  function toast(msg, ms = 2400) {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(t._h);
    t._h = setTimeout(() => (t.hidden = true), ms);
  }

  // ── API ────────────────────────────────────────
  async function api(path, body, method) {
    const opt = {
      method: method || (body ? "POST" : "GET"),
      headers: { "Content-Type": "application/json" },
    };
    if (body) opt.body = JSON.stringify(body);
    const res = await fetch(path, opt);
    if (!res.ok) {
      const t = await res.text();
      throw new Error(t || res.statusText);
    }
    return res.json();
  }

  // ── nav ────────────────────────────────────────
  $$(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".nav-item").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      $$(".page").forEach((p) => p.classList.remove("active"));
      const page = btn.dataset.page;
      const el = $("#page-" + page);
      if (el) el.classList.add("active");
      if (page === "result") loadResult();
      if (page === "keys") loadKeys();
    });
  });

  // ── sliders ────────────────────────────────────
  $$(".slider-row").forEach((row) => {
    const input = row.querySelector("input");
    const out = row.querySelector("b");
    input.addEventListener("input", () => (out.textContent = input.value));
  });

  // ── country chips ──────────────────────────────
  $$("#countryChips .chip-toggle").forEach((c) => {
    c.addEventListener("click", () => {
      if (c.dataset.v === "") {
        $$("#countryChips .chip-toggle").forEach((x) => x.classList.remove("active"));
        c.classList.add("active");
      } else {
        $('#countryChips .chip-toggle[data-v=""]').classList.remove("active");
        c.classList.toggle("active");
        if (!$$("#countryChips .chip-toggle.active").length) {
          $('#countryChips .chip-toggle[data-v=""]').classList.add("active");
        }
      }
    });
  });

  // ── remark preview ─────────────────────────────
  const tplInput = $("#remarkTpl");
  function updatePreview() {
    const tpl = (tplInput.value || "speed-purity-stability-country").trim();
    const sample = { speed: "3.9", purity: "1.00", stability: "1.00", country: "美国" };
    let out = tpl;
    for (const [k, v] of Object.entries(sample)) out = out.split("{" + k + "}").join(v);
    $("#remarkPreview").textContent = "104.17.29.227:8443#" + out;
  }
  tplInput.addEventListener("input", updatePreview);

  // ── keys page ──────────────────────────────────
  async function loadKeys() {
    try {
      const s = await api("/api/status");
      if (s.keys) {
        if (s.keys.GITHUB_TOKEN) $("#keyGithub").value = s.keys.GITHUB_TOKEN;
        if (s.keys.ABUSEIPDB_KEY) $("#keyAbuse").value = s.keys.ABUSEIPDB_KEY;
        if (s.keys.IPINFO_TOKEN) $("#keyIpinfo").value = s.keys.IPINFO_TOKEN;
      }
      const det = $("#envDetect");
      if (det && s.env) {
        det.innerHTML = s.env
          .map(
            (e) =>
              `<div class="env-tag ${e.ok ? "" : "miss"}">
                 <span class="ok-dot"></span>${e.name} · ${e.ok ? "已配置" : "未填写"}
               </div>`
          )
          .join("");
      }
      if (s.publish_url) {
        $("#publishUrl").textContent = s.publish_url;
      }
      const pill = $("#readyPill");
      const txt = $("#readyText");
      if (s.ready) {
        pill.classList.add("ok");
        txt.textContent = "已就绪";
      } else {
        pill.classList.add("warn");
        txt.textContent = "待配置密钥";
      }
    } catch (e) {
      console.warn(e);
    }
  }

  $("#btnSaveKeys").addEventListener("click", async () => {
    try {
      await api("/api/keys", {
        GITHUB_TOKEN: $("#keyGithub").value.trim(),
        ABUSEIPDB_KEY: $("#keyAbuse").value.trim(),
        IPINFO_TOKEN: $("#keyIpinfo").value.trim(),
      });
      toast("密钥已保存");
      loadKeys();
    } catch (e) {
      toast("保存失败：" + e.message);
    }
  });

  // ── create repo ────────────────────────────────
  $("#btnCreateRepo").addEventListener("click", async () => {
    const name = $("#repoName").value.trim() || "cf-ip-pool";
    const out = $("#repoResult");
    out.textContent = "创建中…";
    try {
      const r = await api("/api/repo/create", { name });
      out.textContent = `✓ 仓库已创建：${r.url}\n\nADDAPI:\n${r.addapi}\n\nADDCSV:\n${r.addcsv}`;
      $("#publishUrl").textContent = r.addapi;
      toast("仓库创建成功");
    } catch (e) {
      out.textContent = "";
      toast(e.message, 3600);
    }
  });

  // ── run ────────────────────────────────────────
  $("#btnRun").addEventListener("click", async () => {
    const btn = $("#btnRun");
    btn.disabled = true;
    btn.textContent = "运行中…";
    $("#runLog").textContent = "启动流程…\n";
    $$("#runSteps li").forEach((li) => li.classList.remove("done", "running"));
    try {
      await api("/api/run", {});
      pollRun();
    } catch (e) {
      $("#runLog").textContent += "启动失败：" + e.message + "\n";
      btn.disabled = false;
      btn.textContent = "开始运行";
    }
  });

  async function pollRun() {
    try {
      const s = await api("/api/run/status");
      const log = $("#runLog");
      if (s.log && s.log !== log.dataset.last) {
        log.textContent = s.log;
        log.dataset.last = s.log;
        log.scrollTop = log.scrollHeight;
      }
      const order = ["collect", "probe", "score", "publish"];
      order.forEach((k, i) => {
        const li = $(`#runSteps li[data-step="${k}"]`);
        if (!li) return;
        li.classList.remove("done", "running", "fail");
        if (s.stage_index > i) li.classList.add("done");
        else if (s.stage_index === i) li.classList.add(s.running ? "running" : "done");
      });
      // publish step honest status once finished
      if (!s.running) {
        const pub = $('#runSteps li[data-step="publish"]');
        if (pub) {
          pub.classList.remove("done", "running", "fail");
          if (s.published) pub.classList.add("done");
          else if (s.ranked > 0) pub.classList.add("fail");
          // 0 ranked → leave neutral
        }
        $("#btnRun").disabled = false;
        $("#btnRun").textContent = "开始运行";
        const banner = $("#runSummary");
        if (banner) {
          const msg = s.summary || (s.ok ? "运行完成" : "运行结束（退出码 " + s.code + "）");
          banner.textContent = msg;
          banner.hidden = false;
          banner.classList.toggle("warn", !s.published);
          banner.classList.toggle("ok", !!s.published);
        }
        toast(s.summary || (s.ok ? "运行完成" : "运行结束（退出码 " + s.code + "）"), 3600);
        loadResult();
        return;
      }
    } catch (e) {
      console.warn(e);
    }
    setTimeout(pollRun, 1200);
  }

  $("#btnCopyUrl").addEventListener("click", () => {
    const text = $("#publishUrl").textContent;
    navigator.clipboard.writeText(text).then(() => toast("已复制"));
  });

  // ── pool ───────────────────────────────────────
  $("#btnImportPool").addEventListener("click", async () => {
    const text = $("#poolInput").value;
    if (!text.trim()) return toast("请先粘贴内容");
    try {
      const r = await api("/api/pool/import", { text });
      toast(`已导入 ${r.count} 条候选`);
      $("#poolInput").value = "";
    } catch (e) {
      toast("导入失败：" + e.message);
    }
  });

  $("#poolFile").addEventListener("change", async (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    const text = await file.text();
    try {
      const r = await api("/api/pool/import", { text, filename: file.name });
      toast(`已导入 ${r.count} 条候选`);
    } catch (e) {
      toast("导入失败：" + e.message);
    }
  });

  $("#btnSearchGh").addEventListener("click", async () => {
    toast("搜索中…");
    try {
      const r = await api("/api/pool/search", { query: $("#ghQuery").value });
      renderPool(r.items || []);
      toast(`发现 ${r.items ? r.items.length : 0} 条`);
    } catch (e) {
      toast("搜索失败：" + e.message, 3600);
    }
  });

  $("#btnSubs").addEventListener("click", async () => {
    toast("拉取订阅源…");
    try {
      const r = await api("/api/pool/subs", {});
      renderPool(r.items || []);
      toast(`拉取 ${r.count || 0} 条`);
    } catch (e) {
      toast(e.message, 3600);
    }
  });

  function renderPool(items) {
    const tb = $("#poolTable tbody");
    tb.innerHTML = items
      .map(
        (it) =>
          `<tr>
            <td>${it.address || it.host || ""}</td>
            <td>${it.port || ""}</td>
            <td><span class="badge badge-accent">${it.source || "-"}</span></td>
            <td>${it.remarks || ""}</td>
          </tr>`
      )
      .join("");
  }

  // ── filter save ────────────────────────────────
  $("#btnSaveFilter").addEventListener("click", async () => {
    const weights = {};
    $$(".slider-row").forEach((r) => {
      weights[r.dataset.key] = parseFloat(r.querySelector("input").value);
    });
    const countries = $$("#countryChips .chip-toggle.active")
      .map((c) => c.dataset.v)
      .filter(Boolean);
    try {
      await api("/api/settings", {
        weights,
        filters: {
          min_speed_mb_s: parseFloat($("#minSpeed").value || "0.5"),
          max_latency_ms: parseFloat($("#maxLatency").value || "800"),
          max_risk: parseFloat($("#maxRisk").value || "50"),
          allowed_countries: countries,
        },
        addapi_remark_template: $("#remarkTpl").value.trim(),
      });
      toast("设置已保存");
    } catch (e) {
      toast("保存失败：" + e.message);
    }
  });

  // ── result ─────────────────────────────────────
  async function loadResult() {
    const tb = $("#resultTable tbody");
    const note = $("#resultNote");
    try {
      const r = await api("/api/results");
      const rows = r.items || [];
      if (note) {
        if (r.note) {
          note.textContent = r.note;
          note.hidden = false;
          note.classList.toggle("warn", !r.published || !!r.stale);
          note.classList.toggle("ok", !!r.published && !r.stale);
        } else if (r.published) {
          note.textContent = "已发布到 output/latest";
          note.hidden = false;
          note.classList.remove("warn");
          note.classList.add("ok");
        } else {
          note.hidden = true;
        }
      }
      if (!rows.length) {
        tb.innerHTML = `<tr><td colspan="7" class="empty-cell">${
          r.note || "暂无结果 — 点「开始运行」跑一轮即可看到入榜节点"
        }</td></tr>`;
        $("#resultStats").innerHTML = `
          <div class="stat"><div class="stat-n">0</div><div class="stat-l">入榜</div></div>
          <div class="stat"><div class="stat-n">—</div><div class="stat-l">平均速度 MB/s</div></div>
          <div class="stat"><div class="stat-n">—</div><div class="stat-l">最快 MB/s</div></div>
          <div class="stat"><div class="stat-n">—</div><div class="stat-l">国家 / 地区</div></div>`;
        return;
      }
      tb.innerHTML = rows
        .map(
          (it, i) =>
            `<tr>
              <td>${it.rank ?? i + 1}</td>
              <td><code>${it.address}:${it.port}</code></td>
              <td><b>${it.speed || "-"}</b> MB/s</td>
              <td>${it.purity || "-"}</td>
              <td>${it.stability || "-"}</td>
              <td><span class="badge badge-ok">${it.country || "??"}</span></td>
              <td>${it.score || "-"}</td>
            </tr>`
        )
        .join("");
      const speeds = rows.map((x) => parseFloat(x.speed)).filter((x) => !isNaN(x));
      $("#resultStats").innerHTML = `
        <div class="stat"><div class="stat-n">${rows.length}</div><div class="stat-l">入榜</div></div>
        <div class="stat"><div class="stat-n">${
          speeds.length ? (speeds.reduce((a, b) => a + b, 0) / speeds.length).toFixed(1) : "—"
        }</div><div class="stat-l">平均速度 MB/s</div></div>
        <div class="stat"><div class="stat-n">${speeds.length ? Math.max(...speeds).toFixed(1) : "—"}</div><div class="stat-l">最快 MB/s</div></div>
        <div class="stat"><div class="stat-n">${new Set(rows.map((x) => x.country)).size || "—"}</div><div class="stat-l">国家 / 地区</div></div>`;
    } catch (e) {
      console.warn(e);
      if (note) {
        note.textContent = "加载结果失败：" + e.message;
        note.hidden = false;
        note.classList.add("warn");
      }
      if (tb) {
        tb.innerHTML = `<tr><td colspan="7" class="empty-cell">加载失败：${e.message}</td></tr>`;
      }
    }
  }

  $("#btnRefreshResult").addEventListener("click", loadResult);
  $("#btnOpenOutput").addEventListener("click", () => api("/api/open-output", {}, "POST"));

  // ── wizard ─────────────────────────────────────
  // GitHub auth FIRST → username known → repo created → raw URL auto-filled.
  const WIZ = [
    {
      title: "欢迎使用 NodeBench",
      html: `
        <p>4 步搞定 CF 优选节点测评，并自动推送到你的 edgetunnel。</p>
        <p style="margin-top:12px">会自动识别本机 <code>.env</code>，避免重复填写。</p>`,
      btn: "开始",
    },
    {
      title: "1 · GitHub 认证",
      html: `
        <p>先连 GitHub —— 认证后自动拿到用户名，raw 链接就不用手填了。</p>

        <a class="wiz-link" href="https://github.com/settings/tokens/new?scopes=public_repo&description=NodeBench&expires=0" target="_blank">
          ① 点击前往创建 Token <small>打开 GitHub 创建令牌页 →</small>
        </a>
        <div class="howto">
          <div class="howto-step">② 在新页面找到 <b>Note</b>，随便填（如 <code>NodeBench</code>）</div>
          <div class="howto-step">③ <b>Expiration</b>（过期时间）选 <b>No expiration</b>（永久有效）</div>
          <div class="howto-step">④ <b>勾选 repo → public_repo</b>（只要这一个权限就够）</div>
          <div class="howto-step">⑤ 点页面底部 <b>Generate token</b>（生成令牌）</div>
          <div class="howto-step">⑥ <b>复制</b> 生成的 ghp_ 开头令牌，回到这里粘贴 ↓</div>
        </div>

        <input class="input" id="wizGithub" placeholder="粘贴 GitHub Token（ghp_… 或 github_pat_…）" style="margin:10px 0" />
        <div class="row">
          <button class="btn btn-primary" id="wizAuth">⑦ 认证并获取用户名</button>
        </div>
        <div id="wizAuthOut" class="repo-result"></div>`,
      btn: "下一步",
    },
    {
      title: "2 · 选择 / 创建仓库",
      html: `
        <p>选择<strong>已有仓库</strong>，或一键<strong>新建</strong>一个，用来托管优选地址。</p>

        <div class="card" style="margin:12px 0;padding:14px">
          <div class="card-label">已有仓库</div>
          <div id="wizRepoList" class="repo-list">加载中…</div>
        </div>

        <div class="howto">
          <div class="howto-step">或新建一个：</div>
        </div>
        <div class="row" style="margin-top:8px">
          <input class="input" id="wizRepo" value="cf-ip-pool" style="max-width:220px" />
          <button class="btn btn-primary" id="wizCreateRepo">一键创建仓库</button>
        </div>
        <div id="wizRepoOut" class="repo-result"></div>`,
      btn: "下一步",
    },
    {
      title: "3 · 其它密钥",
      html: `
        <p>纯净度 + 属地。可稍后再填。</p>

        <a class="wiz-link" href="https://www.abuseipdb.com/account/api/keys" target="_blank">
          ① 点击前往 AbuseIPDB <small>打开密钥页 →</small>
        </a>
        <div class="howto">
          <div class="howto-step">② 登录后点 <b>My Account → API Keys</b></div>
          <div class="howto-step">③ 点 <b>Create New Key</b>，名字随意</div>
          <div class="howto-step">④ <b>复制</b> 生成的 Key，粘贴到下面 ↓</div>
        </div>
        <input class="input" id="wizAbuse" placeholder="粘贴 AbuseIPDB Key" style="margin:6px 0 14px" />

        <a class="wiz-link" href="https://ipinfo.io/dashboard" target="_blank">
          ① 点击前往 IPinfo <small>打开仪表盘，右上角就是 Token →</small>
        </a>
        <div class="howto">
          <div class="howto-step">② 登录后在 <b>API Tokens</b> 页面 <b>复制</b> 令牌</div>
          <div class="howto-step">③ 粘贴到下面 ↓</div>
        </div>
        <input class="input" id="wizIpinfo" placeholder="粘贴 IPinfo Token" style="margin:6px 0 0" />`,
      btn: "保存并继续",
    },
    {
      title: "4 · 对接 edgetunnel",
      html: `
        <p>到管理页「优选订阅生成 → 自定义优选」，粘贴<strong>下面这一条</strong>链接：</p>

        <div class="code-block" id="wizUrl" style="font-weight:600;color:var(--accent)">
          https://raw.githubusercontent.com/&lt;user&gt;/&lt;repo&gt;/public/cf-addapi.txt
        </div>
        <button class="btn btn-primary" id="wizCopy" style="margin:8px 0 4px">复制链接</button>

        <div class="howto" style="margin-top:14px">
          <div class="howto-step">① 打开 edgetunnel 管理页 →「优选订阅生成」</div>
          <div class="howto-step">② 「自定义优选」文本框里<strong>粘贴上面这条链接</strong></div>
          <div class="howto-step">③ 点「保存」→「开始优选」</div>
          <div class="howto-step">以后本工具运行完会自动更新，<strong>这个框不用再改</strong></div>
        </div>

        <details style="margin-top:14px">
          <summary style="cursor:pointer;color:var(--muted);font-size:12.5px">
            可选：ADDCSV 链接（如果你的部署有 ADDCSV 变量）
          </summary>
          <div class="code-block" id="wizUrlCsv" style="margin-top:8px;opacity:.75">
            https://raw.githubusercontent.com/&lt;user&gt;/&lt;repo&gt;/public/cf-addcsv.csv
          </div>
        </details>`,
      btn: "完成",
    },
  ];
  let wizStep = 0;

  function renderWiz() {
    const w = WIZ[wizStep];
    $("#wizTitle").textContent = w.title;
    $("#wizBody").innerHTML = w.html;
    $("#wizNext").textContent = w.btn;
    $("#wizBar").style.width = ((wizStep + 1) / WIZ.length) * 100 + "%";
    // prev only after first screen
    $("#wizPrev").style.visibility = wizStep === 0 ? "hidden" : "visible";

    if (wizStep === 1) {
      // prefill existing token
      api("/api/status").then((s) => {
        const el = $("#wizGithub");
        if (el && s.keys && s.keys.GITHUB_TOKEN && !el.value) {
          el.value = s.keys.GITHUB_TOKEN;
        }
        if (s.github_login) {
          $("#wizAuthOut").textContent = `✓ 已登录：${s.github_login}`;
        }
      }).catch(() => {});
      $("#wizAuth")?.addEventListener("click", onWizAuth);
    }
    if (wizStep === 2) {
      $("#wizCreateRepo").addEventListener("click", onWizRepo);
      // show login state so user knows if auth stuck
      api("/api/status").then((s) => {
        const out = $("#wizRepoOut");
        if (!out) return;
        if (s.github_login) {
          out.textContent = `当前登录：${s.github_login}`;
        } else {
          out.textContent = "⚠ 尚未登录 GitHub — 请返回上一步完成认证";
        }
      }).catch(() => {});
      loadRepoList();
    }
    if (wizStep === 4) {
      api("/api/status").then((s) => {
        if (s.publish_addapi) $("#wizUrl").textContent = s.publish_addapi;
        if (s.publish_addcsv) $("#wizUrlCsv").textContent = s.publish_addcsv;
      }).catch(() => {});
      $("#wizCopy")?.addEventListener("click", () => {
        const text = $("#wizUrl").textContent.trim();
        navigator.clipboard.writeText(text).then(() => toast("已复制链接"));
      });
    }
  }

  async function onWizAuth() {
    const out = $("#wizAuthOut");
    const token = $("#wizGithub").value.trim();
    if (!token) {
      out.textContent = "请先粘贴 GitHub Token";
      return null;
    }
    out.textContent = "认证中…";
    try {
      const r = await api("/api/github/whoami", { GITHUB_TOKEN: token });
      out.textContent = `✓ 已登录：${r.login}\n${r.addapi}`;
      toast("GitHub 认证成功");
      return r;
    } catch (e) {
      out.textContent = "✗ " + e.message;
      return null;
    }
  }

  async function loadRepoList() {
    const box = $("#wizRepoList");
    if (!box) return;
    box.innerHTML = "加载中…";
    try {
      const r = await api("/api/github/repos", {});
      const items = (r.items || []).slice(0, 30);
      if (!items.length) {
        box.innerHTML = '<div class="hint">没有找到公开仓库，用下面的输入框新建一个吧</div>';
        return;
      }
      box.innerHTML = items
        .map(
          (it) => `
          <button class="repo-item" data-name="${it.name}">
            <span class="repo-name">${it.name}</span>
            <span class="repo-meta">${it.private ? "私有" : "公开"} · ${it.updated_at.slice(0, 10)}</span>
          </button>`
        )
        .join("");
      box.querySelectorAll(".repo-item").forEach((btn) => {
        btn.addEventListener("click", () => selectRepo(btn.dataset.name));
      });
    } catch (e) {
      box.innerHTML = `<div class="hint">加载失败：${e.message}</div>`;
    }
  }

  async function selectRepo(name) {
    const out = $("#wizRepoOut");
    if (out) out.textContent = "保存中…";
    try {
      const r = await api("/api/repo/select", { name });
      if (out) out.textContent = `✓ 已选择：${r.login}/${r.name}\n${r.addapi}`;
      toast(`已选择仓库 ${name}`);
    } catch (e) {
      if (out) out.textContent = e.message;
    }
  }

  async function onWizRepo() {
    const name = $("#wizRepo").value.trim() || "cf-ip-pool";
    const out = $("#wizRepoOut");
    out.textContent = "创建中…";
    try {
      const r = await api("/api/repo/create", { name });
      out.textContent = `✓ 仓库 ${r.login}/${r.name}\n${r.addapi}`;
    } catch (e) {
      out.textContent = e.message;
    }
  }

  function openWizard() {
    wizStep = 0;
    renderWiz();
    $("#wizard").hidden = false;
    loadKeys();
  }

  async function goWizNext() {
    // Leaving GitHub auth step: always authenticate with whatever token is there.
    if (wizStep === 1) {
      const token = $("#wizGithub")?.value.trim() || "";
      if (token) {
        await onWizAuth();
      }
      // re-check after auth
      const st = await api("/api/status").catch(() => null);
      if (st && st.keys && st.keys.GITHUB_TOKEN && !st.github_login) {
        // token saved but login not resolved — try once more silently
        await api("/api/github/whoami", { GITHUB_TOKEN: token }).catch(() => {});
      }
    }
    if (wizStep === 3) {
      try {
        const token = $("#wizGithub")?.value.trim() || "";
        await api("/api/keys", {
          GITHUB_TOKEN: token,
          ABUSEIPDB_KEY: $("#wizAbuse")?.value.trim() || "",
          IPINFO_TOKEN: $("#wizIpinfo")?.value.trim() || "",
        });
      } catch (e) {
        /* keep going */
      }
    }
    if (wizStep >= WIZ.length - 1) {
      $("#wizard").hidden = true;
      toast("设置完成，可以开始运行");
      loadKeys();
      return;
    }
    wizStep += 1;
    renderWiz();
  }

  function goWizPrev() {
    if (wizStep <= 0) return;
    wizStep -= 1;
    renderWiz();
  }

  $("#wizNext").addEventListener("click", () => {
    goWizNext().catch((e) => toast(e.message));
  });
  $("#wizPrev").addEventListener("click", goWizPrev);
  // no skip: wizard must be completed

  // ── theme ──────────────────────────────────────
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("nb-theme", t); } catch (_) {}
  }
  const savedTheme = (() => {
    try { return localStorage.getItem("nb-theme"); } catch (_) { return null; }
  })();
  applyTheme(savedTheme || "light");
  $("#themeToggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || "light";
    applyTheme(cur === "dark" ? "light" : "dark");
  });

  // ── autostart / tray ───────────────────────────
  $("#btnAutostart")?.addEventListener("click", async () => {
    try {
      const r = await api("/api/autostart", {
        enable: $("#btnAutostart").textContent.trim() !== "开启",
      });
      $("#btnAutostart").textContent = r.enabled ? "关闭" : "开启";
      toast(r.enabled ? "已加入开机自启" : "已取消开机自启");
    } catch (e) {
      toast("设置失败：" + e.message);
    }
  });

  $("#btnTray")?.addEventListener("click", () => {
    const el = $("#btnTray");
    const on = el.textContent.trim() === "已开启";
    el.textContent = on ? "已关闭" : "已开启";
    toast(on ? "关闭窗口将直接退出" : "关闭窗口会最小化到托盘");
  });

  // ── boot ───────────────────────────────────────
  loadKeys();
  loadResult();
  api("/api/autostart-status").then((r) => {
    const el = $("#btnAutostart");
    if (el) el.textContent = r.enabled ? "关闭" : "开启";
  }).catch(() => {});
  api("/api/status")
    .then((s) => {
      if (!s.ready) openWizard();
    })
    .catch(() => openWizard());
})();
