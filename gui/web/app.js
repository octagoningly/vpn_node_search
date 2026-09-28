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
        li.classList.remove("done", "running");
        if (s.stage_index > i) li.classList.add("done");
        else if (s.stage_index === i) li.classList.add("running");
      });
      if (!s.running) {
        $("#btnRun").disabled = false;
        $("#btnRun").textContent = "开始运行";
        toast(s.ok ? "运行完成" : "运行结束（退出码 " + s.code + "）");
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
    try {
      const r = await api("/api/results");
      const rows = r.items || [];
      const tb = $("#resultTable tbody");
      tb.innerHTML = rows
        .map(
          (it, i) =>
            `<tr>
              <td>${i + 1}</td>
              <td><code>${it.address}:${it.port}</code></td>
              <td><b>${it.speed ?? "-"}</b> MB/s</td>
              <td>${it.purity ?? "-"}</td>
              <td>${it.stability ?? "-"}</td>
              <td><span class="badge badge-ok">${it.country ?? "??"}</span></td>
              <td>${it.score ?? "-"}</td>
            </tr>`
        )
        .join("");
      const speeds = rows.map((x) => x.speed).filter((x) => typeof x === "number");
      $("#resultStats").innerHTML = `
        <div class="stat"><div class="stat-n">${rows.length}</div><div class="stat-l">入榜</div></div>
        <div class="stat"><div class="stat-n">${
          speeds.length ? (speeds.reduce((a, b) => a + b, 0) / speeds.length).toFixed(1) : "—"
        }</div><div class="stat-l">平均速度 MB/s</div></div>
        <div class="stat"><div class="stat-n">${speeds.length ? Math.max(...speeds).toFixed(1) : "—"}</div><div class="stat-l">最快 MB/s</div></div>
        <div class="stat"><div class="stat-n">${new Set(rows.map((x) => x.country)).size || "—"}</div><div class="stat-l">国家 / 地区</div></div>`;
    } catch (e) {
      console.warn(e);
    }
  }

  $("#btnRefreshResult").addEventListener("click", loadResult);
  $("#btnOpenOutput").addEventListener("click", () => api("/api/open-output", {}, "POST"));

  // ── wizard ─────────────────────────────────────
  const WIZ = [
    {
      title: "欢迎使用 NodeBench",
      html: `
        <p>三步搞定 CF 优选节点测评，并自动推送到你的 edgetunnel。</p>
        <p style="margin-top:12px">本工具会先检测本机是否已有 <code>.env</code> 密钥，避免重复填写。</p>`,
      btn: "开始",
    },
    {
      title: "1 · 创建 GitHub 仓库",
      html: `
        <p>一键创建公开仓库，用于托管优选地址文件（raw 链接给 edgetunnel 用）。</p>
        <div class="big-action">
          <div class="row">
            <input class="input" id="wizRepo" value="cf-ip-pool" style="max-width:220px" />
            <button class="btn btn-primary" id="wizCreateRepo">一键创建仓库</button>
          </div>
          <div id="wizRepoOut" class="repo-result"></div>
        </div>
        <p class="hint">仓库名可改。若已有仓库，直接下一步。</p>`,
      btn: "下一步",
    },
    {
      title: "2 · 填入密钥",
      html: `
        <p>点「前往」打开注册页，把密钥粘贴回来。只存本机。</p>
        <a class="wiz-link" href="https://github.com/settings/tokens/new?scopes=public_repo&description=NodeBench" target="_blank">
          GitHub Token <small>用于搜索候选 + 推送 public 分支 → 前往创建</small>
        </a>
        <input class="input" id="wizGithub" placeholder="粘贴 GitHub Token" style="margin:4px 0 10px" />
        <a class="wiz-link" href="https://www.abuseipdb.com/account/api/keys" target="_blank">
          AbuseIPDB Key <small>纯净度检测 → 前往获取</small>
        </a>
        <input class="input" id="wizAbuse" placeholder="粘贴 AbuseIPDB Key" style="margin:4px 0 10px" />
        <a class="wiz-link" href="https://ipinfo.io/dashboard" target="_blank">
          IPinfo Token <small>属地 / ASN → 前往获取</small>
        </a>
        <input class="input" id="wizIpinfo" placeholder="粘贴 IPinfo Token" style="margin:4px 0 0" />`,
      btn: "保存并继续",
    },
    {
      title: "3 · 对接 edgetunnel",
      html: `
        <p>到管理页「优选订阅生成 → 自定义优选」，粘贴下面链接：</p>
        <div class="code-block" id="wizUrl">https://raw.githubusercontent.com/&lt;你&gt;/&lt;仓库&gt;/public/cf-addapi.txt</div>
        <p class="hint">以后每次运行完自动更新，管理页不用再改。</p>`,
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
    if (wizStep === 1) {
      $("#wizCreateRepo").addEventListener("click", onWizRepo);
    }
  }

  async function onWizRepo() {
    const name = $("#wizRepo").value.trim() || "cf-ip-pool";
    const out = $("#wizRepoOut");
    out.textContent = "创建中…";
    try {
      const r = await api("/api/repo/create", { name });
      out.textContent = `✓ ${r.addapi}`;
      $("#wizUrl").textContent = r.addapi;
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

  $("#wizNext").addEventListener("click", async () => {
    if (wizStep === 2) {
      try {
        await api("/api/keys", {
          GITHUB_TOKEN: $("#wizGithub")?.value.trim() || "",
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
  });

  $("#wizSkip").addEventListener("click", () => {
    $("#wizard").hidden = true;
  });

  // ── boot ───────────────────────────────────────
  loadKeys();
  api("/api/status")
    .then((s) => {
      if (!s.ready) openWizard();
    })
    .catch(() => openWizard());
})();
