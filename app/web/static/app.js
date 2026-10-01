/* 展签更换管理 · 前端逻辑（无框架、无依赖） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(
  /[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let STATE = null;
let selectedVersion = null;

const api = async (path, body) => {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || "请求失败");
  return data;
};

const toast = (msg, kind = "") => {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, 3200);
};

const currentUser = () =>
  STATE.staff.find((s) => s.id === Number($("#role-user").value));
const roleName = (r) => STATE.labels.role[r] || r;
const issueName = (k) => STATE.labels.issue[k] || k;

// ---------------- 加载与渲染 ----------------
async function loadState() {
  STATE = await (await fetch("/api/state")).json();
  renderAll();
}

function renderAll() {
  const u = currentUser();
  $("#role-badge").textContent = u ? roleName(u.role) : "";
  renderExhibits();
  renderCases();
  renderVersionList();
  if (selectedVersion) renderVersionDetail(selectedVersion);
  renderPrintList();
  renderScanTab();
  renderRecords();
}

function renderExhibits() {
  const byEx = new Map();
  STATE.versions.forEach((v) => {
    if (!byEx.has(v.exhibit_id)) byEx.set(v.exhibit_id, []);
    byEx.get(v.exhibit_id).push(v);
  });
  $("#exhibit-table").querySelector("tbody").innerHTML = STATE.exhibits.map((e) => {
    const vs = byEx.get(e.id) || [];
    const langs = [...new Set(vs.map((v) => v.lang))].sort()
      .map((l) => `<span class="badge muted">${esc(l)}</span>`).join(" ");
    return `<tr><td>${esc(e.catalog_no)}</td><td>${esc(e.name)}</td>
      <td>${langs || "—"}</td></tr>`;
  }).join("");
}

function renderCases() {
  $("#case-table").querySelector("tbody").innerHTML = STATE.cases.map((c) => {
    const pending = c.pending
      ? `<span class="badge warn">${esc(c.pending)} 待换</span>`
      : (c.current ? '<span class="badge muted">无待换</span>' : '<span class="badge bad">空柜</span>');
    const btn = c.pending
      ? `<button data-pending-case="${esc(c.code)}">去扫码</button>` : "";
    return `<tr>
      <td><b>${esc(c.code)}</b></td><td>${esc(c.hall)}</td>
      <td>${esc(c.exhibit_catalog || "—")}<br><span class="hint">${esc(c.exhibit_name || "")}</span></td>
      <td>${c.current ? `<span class="badge ok">${esc(c.current)}</span>` : '<span class="badge bad">未布展</span>'}</td>
      <td>${pending}</td><td>${btn}</td></tr>`;
  }).join("");
  $$("[data-pending-case]").forEach((b) =>
    b.addEventListener("click", () => {
      switchTab("scan");
      const code = b.dataset.pendingCase;
      $("#scan-case").value = STATE.cases.find((c) => c.code === code).id;
      fillScanJobs();
    }));
}

// ---------------- 版本列表与对照 ----------------
function renderVersionList() {
  const exName = Object.fromEntries(STATE.exhibits.map((e) => [e.id, e]));
  const groups = new Map();
  STATE.versions.forEach((v) => {
    if (!groups.has(v.exhibit_id)) groups.set(v.exhibit_id, []);
    groups.get(v.exhibit_id).push(v);
  });
  let html = "";
  for (const [eid, vs] of groups) {
    const e = exName[eid];
    html += `<div class="hint" style="margin:10px 0 4px">${esc(e.catalog_no)} · ${esc(e.name)}</div>`;
    html += vs.map((v) => {
      const state = versionState(v);
      const cls = selectedVersion === v.id ? "v-item sel" : "v-item";
      return `<div class="${cls}" data-vid="${v.id}">
        <div class="t">v${v.version_no} <span class="badge muted">${esc(v.lang)}</span>
          <span class="badge ${state.cls}">${state.text}</span></div>
        <div class="m">${esc(v.title)} · ${esc(v.created_name)} · ${esc(v.created_at)}</div>
      </div>`;
    }).join("");
  }
  $("#version-list").innerHTML = html || '<p class="empty">暂无版本</p>';
  $$("[data-vid]", $("#version-list")).forEach((el) =>
    el.addEventListener("click", () => renderVersionDetail(Number(el.dataset.vid))));
}

function versionState(v) {
  const revs = STATE.reviews.filter((r) => r.version_id === v.id && r.status === "valid");
  const pr = revs.some((r) => r.kind === "proofread" && r.passed);
  const fc = revs.some((r) => r.kind === "factcheck" && r.passed);
  if (!v.is_latest) return { text: "已过期", cls: "muted" };
  if (pr && fc) return { text: "双签通过", cls: "ok" };
  if (pr || fc) return { text: "待另一签", cls: "warn" };
  return { text: "待校对", cls: "warn" };
}

function prevVersion(v) {
  const same = STATE.versions
    .filter((x) => x.exhibit_id === v.exhibit_id && x.lang === v.lang)
    .sort((a, b) => a.version_no - b.version_no);
  const i = same.findIndex((x) => x.id === v.id);
  return i > 0 ? same[i - 1] : null;
}

function renderVersionDetail(vid) {
  selectedVersion = vid;
  const v = STATE.versions.find((x) => x.id === vid);
  if (!v) return;
  renderVersionList();
  const prev = prevVersion(v);
  const u = currentUser();

  const field = (k, label, obj) =>
    `<div class="field"><span class="k">${label}</span>${esc(obj[k])}</div>`;

  const changedKeys = ["title", "dynasty", "material", "qr_url"];
  const col = (obj, isNew) => `<div class="diff-col ${isNew ? "next" : "prev"}">
    <h4>${isNew ? "修订（v" + v.version_no + "·" + esc(v.lang) + "）"
      : (prev ? "原文（v" + prev.version_no + "）" : "首版，无原文")}</h4>
    ${["title", "dynasty", "material"].map((k) => {
      const labels = { title: "名称", dynasty: "年代", material: "材质" };
      const ch = isNew && prev && obj[k] !== prev[k];
      return `<div class="field"><span class="k">${labels[k]}</span>${
        ch ? `<span class="changed">${esc(obj[k])}</span>` : esc(obj[k])}</div>`;
    }).join("")}
    <div class="field"><span class="k">正文</span>
      <span class="body-text">${bodyDiffHtml(prev ? prev.body : null, obj.body, isNew)}</span>
    </div>
    <div class="field"><span class="k">链接</span>${
      isNew && prev && obj.qr_url !== prev.qr_url
        ? `<span class="changed">${esc(obj.qr_url)}</span>` : esc(obj.qr_url)}</div>
  </div>`;

  const annos = STATE.annotations.filter((a) => a.version_id === vid);
  const reviews = STATE.reviews.filter((r) => r.version_id === vid);
  const state = versionState(v);
  const isOwn = v.created_by === u.id;

  $("#version-detail").innerHTML = `
    <div class="review-row">
      <b>${esc(v.exhibit_name)}（${esc(v.catalog_no)}）v${v.version_no}
        <span class="badge muted">${esc(v.lang)}</span></b>
      <span class="badge ${state.cls}">${state.text}</span>
      ${!v.is_latest ? '<span class="badge bad">修改正文后旧结论失效 · 禁止安装此版</span>' : ""}
    </div>
    <div class="diff-grid">${col(prev || v, false)}${col(v, true)}</div>

    <div class="section-label">校对批注</div>
    ${annos.length ? annos.map((a) => `
      <div class="anno ${a.status === "stale" ? "stale" : ""}">
        <b>${esc(a.author_name)}</b>
        ${a.quote ? ` 引：「${esc(a.quote)}」` : ""}<br>${esc(a.comment)}
        ${a.status === "stale" ? ' <span class="badge muted">正文已改 · 失效</span>' : ""}
      </div>`).join("") : '<p class="empty">暂无批注</p>'}
    ${u.role === "editor" && v.is_latest ? `
      <div class="form-row" style="margin-top:8px">
        <input id="anno-quote" placeholder="引用原文（可空）" style="max-width:180px">
        <input id="anno-text" placeholder="校对意见">
        <button id="anno-btn">提交批注</button>
      </div>` : ""}

    <div class="section-label">双签结论（编辑校对用语 / 保管员核对编号与事实）</div>
    ${reviews.map((r) => `
      <div class="review-row">
        <span class="badge ${r.passed ? "ok" : "warn"}">${
          r.kind === "proofread" ? "用语校对" : "事实核对"} · ${r.passed ? "通过" : "存疑"}</span>
        ${r.status === "superseded" ? '<span class="badge bad">已失效（正文修改）</span>'
          : '<span class="badge ok">有效</span>'}
        <span>${esc(r.reviewer_name)} · ${esc(r.note)}</span>
        <span class="hint">${esc(r.created_at)}</span>
      </div>`).join("") || '<p class="empty">尚无结论</p>'}
    <div class="review-row">
      ${u.role === "editor" && v.is_latest ? `
        <button data-review="proofread-1">编辑：校对通过</button>
        <button data-review="proofread-0" class="ghost-danger">校对存疑</button>` : ""}
      ${u.role === "keeper" && v.is_latest ? (isOwn
        ? '<span class="badge bad">角色分离：提交人不能事实核对（此版非您提交才可签）</span>'
        : `
        <input id="fc-catalog" placeholder="现场核对展品编号" value="${esc(v.catalog_no)}" style="max-width:180px">
        <button data-review="factcheck-1">保管员：编号无误，事实通过</button>
        <button data-review="factcheck-0" class="ghost-danger">事实存疑</button>`) : ""}
      ${u.role === "curator"
        ? '<span class="hint">策展人提交内容；事实核对须由保管员（非提交人）完成</span>' : ""}
    </div>

    <div class="section-label">生成修订版（仅策展人）</div>
    ${u.role === "curator" ? newVersionForm(v) : '<p class="empty">当前角色不能提交版本</p>'}
  `;

  $("#anno-btn")?.addEventListener("click", async () => {
    try {
      await api("/api/annotate", {
        version_id: vid, staff_id: u.id,
        quote: $("#anno-quote").value, comment: $("#anno-text").value,
      });
      toast("批注已记录", "ok");
      await loadState();
    } catch (e) { toast(e.message, "bad"); }
  });

  $$("[data-review]", $("#version-detail")).forEach((b) =>
    b.addEventListener("click", async () => {
      const [kind, passed] = b.dataset.review.split("-");
      try {
        const body = {
          version_id: vid, staff_id: u.id,
          kind, passed: passed === "1",
          note: passed === "1" ? "界面签核通过" : "界面登记存疑",
        };
        if (kind === "factcheck") body.catalog_no = $("#fc-catalog").value;
        await api("/api/review", body);
        toast("签核完成", "ok");
        await loadState();
      } catch (e) { toast(e.message, "bad"); }
    }));

  $("#newver-btn")?.addEventListener("click", submitNewVersion);
}

function bodyDiffHtml(oldBody, newBody, isNew) {
  if (!isNew) return esc(oldBody ?? "");
  if (oldBody === null) return esc(newBody);
  const a = oldBody.split("\n"), b = newBody.split("\n");
  // 极简 LCS，行级增删标色
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { out.push(esc(b[j])); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { i++; }
    else { out.push(`<span class="changed">${esc(b[j])}</span>`); j++; }
  }
  while (j < b.length) out.push(`<span class="changed">${esc(b[j])}</span>`), j++;
  return out.join("\n");
}

function newVersionForm(v) {
  const ex = STATE.exhibits.find((e) => e.id === v.exhibit_id);
  return `
    <div class="scan-box">
      <div class="form-row">
        <label style="margin:0">语言
          <select id="nv-lang">
            <option value="zh" ${v.lang === "zh" ? "selected" : ""}>zh 中文</option>
            <option value="en" ${v.lang === "en" ? "selected" : ""}>en English</option>
          </select></label>
        <input id="nv-title" value="${esc(v.title)}" placeholder="名称">
        <input id="nv-dynasty" value="${esc(v.dynasty)}" placeholder="年代">
        <input id="nv-material" value="${esc(v.material)}" placeholder="材质">
      </div>
      <textarea id="nv-body" rows="3">${esc(v.body)}</textarea>
      <input id="nv-url" value="${esc(v.qr_url)}" style="width:100%;margin-top:6px">
      <input type="hidden" id="nv-exhibit" value="${ex.id}">
      <div style="margin-top:8px"><button class="primary" id="newver-btn">提交新版本（旧版校对结论随即失效）</button></div>
    </div>`;
}

async function submitNewVersion() {
  const u = currentUser();
  try {
    const data = await api("/api/submit", {
      exhibit_id: Number($("#nv-exhibit").value),
      title: $("#nv-title").value, dynasty: $("#nv-dynasty").value,
      material: $("#nv-material").value, body: $("#nv-body").value,
      qr_url: $("#nv-url").value, lang: $("#nv-lang").value,
      staff_id: u.id,
    });
    toast("新版本已创建，等待双签", "ok");
    selectedVersion = data.version_id;
    await loadState();
  } catch (e) { toast(e.message, "bad"); }
}

// ---------------- 打印清单 ----------------
function renderPrintList() {
  const latestKey = new Set(STATE.versions.filter((v) => v.is_latest).map((v) => v.id));
  const ready = STATE.versions.filter((v) => v.is_latest && v.review_ready);
  const printedIds = new Set(STATE.jobs.filter((j) => j.status !== "void").map((j) => j.version_id));

  let html = `<div class="section-label">可打印（最新版 + 双签通过）</div>`;
  html += ready.map((v) => `
    <div class="job-card">
      <div class="meta"><b>${esc(v.exhibit_name)}</b> v${v.version_no}
        <span class="badge muted">${esc(v.lang)}</span>
        <div>${esc(v.catalog_no)} · ${esc(v.dynasty)} · ${esc(v.material)}</div></div>
      <button data-print="${v.id}" ${printedIds.has(v.id) ? "" : ""}>
        ${printedIds.has(v.id) ? "重新打印" : "生成可打印版本"}</button>
    </div>`).join("") || '<p class="empty">暂无可打印版本</p>';

  html += `<div class="section-label">打印任务与样图（120mm × 80mm）</div>`;
  html += STATE.jobs.slice().reverse().map((j) => `
    <div class="job-card">
      <div class="meta"><b>${esc(j.title)}</b> v${j.version_no}
        <span class="badge muted">${esc(j.lang)}</span>
        <span class="badge ${j.status === "installed" ? "ok"
          : j.status === "void" ? "muted" : "warn"}">${
          { printed: "待安装", installed: "已入柜", void: "已作废" }[j.status]}</span>
        ${j.color_delta > 3
          ? '<span class="badge bad">色差 ΔE ' + j.color_delta.toFixed(1) + " 超差</span>"
          : '<span class="badge ok">ΔE ' + j.color_delta.toFixed(1) + "</span>"}
        <div>${esc(j.catalog_no)} · ${esc(j.printer_name)} 于 ${esc(j.printed_at)}</div></div>
      <button data-preview="${j.version_id}">真实比例预览</button>
    </div>`).join("") || '<p class="empty">尚未打印</p>';

  $("#print-list").innerHTML = html;
  $$("[data-print]").forEach((b) => b.addEventListener("click", async () => {
    try {
      const r = await api("/api/print", {
        version_id: Number(b.dataset.print), staff_id: currentUser().id });
      toast(r.color_delta > 3
        ? `已打印，但色差 ΔE=${r.color_delta.toFixed(1)} 超差，印件已作废并登记问题`
        : `可打印版本已生成（ΔE=${r.color_delta.toFixed(1)}）`,
        r.color_delta > 3 ? "bad" : "ok");
      await loadState();
      openPreview(Number(b.dataset.print));
    } catch (e) { toast(e.message, "bad"); }
  }));
  $$("[data-preview]").forEach((b) =>
    b.addEventListener("click", () => openPreview(Number(b.dataset.preview))));
}

async function openPreview(vid) {
  try {
    const r = await (await fetch(`/api/preview?vid=${vid}`)).json();
    if (r.error) throw new Error(r.error);
    $("#preview-img").src = r.url + "?t=" + Date.now();
    $("#preview-size").textContent = `${r.width_mm}mm × ${r.height_mm}mm`;
    $("#preview-mask").hidden = false;
  } catch (e) { toast(e.message, "bad"); }
}

// ---------------- 现场扫码 ----------------
function renderScanTab() {
  const caseSel = $("#scan-case");
  if (!caseSel.options.length) {
    caseSel.innerHTML = STATE.cases.map((c) =>
      `<option value="${c.id}">${esc(c.code)} · ${esc(c.exhibit_catalog || "空柜")}</option>`
    ).join("");
    caseSel.addEventListener("change", fillScanJobs);
  }
  fillScanJobs();

  $("#scan-table").querySelector("tbody").innerHTML =
    STATE.scans.slice().reverse().map((g) => `
      <tr><td class="hint">${esc(g.created_at)}</td>
        <td><b>${esc(g.case_code)}</b>${g.expected_code && g.expected_code !== g.case_code
          ? `<br><span class="hint">应为 ${esc(g.expected_code)}</span>` : ""}</td>
        <td><span class="badge ${g.accepted ? "ok" : "bad"}">${
          g.accepted ? "通过" : "拒绝"}</span></td>
        <td>${esc(g.reason)}<br><span class="hint">${esc(g.scanner_name)}</span></td>
      </tr>`).join("");
}

function fillScanJobs() {
  const jobSel = $("#scan-job");
  const pendingJobs = STATE.jobs.filter((j) => j.status === "printed");
  jobSel.innerHTML = pendingJobs.map((j) =>
    `<option value="${esc(j.qr_payload)}">${esc(j.catalog_no)} v${j.version_no}（${esc(j.lang)}）标签</option>`
  ).join("");
  if (!pendingJobs.length)
    jobSel.innerHTML = '<option value="">（无待安装标签）</option>';
  $("#scan-payload").value = jobSel.value || "";
}

// ---------------- 撤换与问题 ----------------
function renderRecords() {
  $("#issue-list").innerHTML = STATE.issues.slice().reverse().map((i) => `
    <div class="issue-card ${i.status === "resolved" ? "resolved" : ""}">
      <span class="badge ${i.status === "resolved" ? "ok" : "warn"}">${
        i.status === "resolved" ? "已处理" : "待处理"}</span>
      <div class="body">
        <b>${issueName(i.kind)}</b>
        ${i.exhibit_name ? ` · ${esc(i.exhibit_name)}` : ""}
        ${i.case_code ? ` · 展柜 ${esc(i.case_code)}` : ""}
        ${i.version_no ? ` · v${i.version_no}（${esc(i.lang)}）` : ""}
        <div>${esc(i.detail)}</div>
        <div class="foot">${esc(i.reporter_name || "")} 登记 · ${esc(i.created_at)}</div>
      </div>
      ${i.status === "open"
        ? `<button data-resolve-issue="${i.id}">销案</button>` : ""}
    </div>`).join("");
  $$("[data-resolve-issue]").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await api("/api/issue-resolve", { issue_id: Number(b.dataset.resolveIssue) });
        toast("问题已销案", "ok");
        await loadState();
      } catch (e) { toast(e.message, "bad"); }
    }));

  $("#binding-table").querySelector("tbody").innerHTML =
    STATE.bindings.slice().reverse().map((b) => `
      <tr><td><b>${esc(b.case_code)}</b></td>
        <td>v${b.version_no}（${esc(b.lang)}）${esc(b.title)}
          <br><span class="hint">${esc(b.catalog_no)}</span></td>
        <td>${esc(b.installed_at || "")}</td>
        <td>${b.valid ? '<span class="badge ok">在展</span>'
          : esc(b.removed_at || "")}</td>
        <td class="hint">${esc(b.note)}</td></tr>`).join("");

  const sel = $("#remove-case");
  if (!sel.options.length) {
    sel.innerHTML = STATE.cases.map((c) =>
      `<option value="${esc(c.code)}">${esc(c.code)}（${esc(c.exhibit_name || "空柜")}）</option>`
    ).join("");
  }
}

// ---------------- 事件绑定 ----------------
function switchTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
}

function bindStatic() {
  $("#tabs").addEventListener("click", (e) => {
    if (e.target.dataset.tab) switchTab(e.target.dataset.tab);
  });
  $("#role-user").addEventListener("change", renderAll);
  $("#scan-job").addEventListener("change", () => {
    $("#scan-payload").value = $("#scan-job").value;
  });
  $("#scan-btn").addEventListener("click", async () => {
    const caseId = Number($("#scan-case").value);
    const code = STATE.cases.find((c) => c.id === caseId).code;
    const box = $("#scan-result");
    try {
      const r = await api("/api/scan", {
        qr_payload: $("#scan-payload").value.trim(),
        case_code: code, staff_id: currentUser().id,
      });
      box.className = "scan-result " + (r.accepted ? "ok" : "bad");
      box.textContent = (r.accepted ? "✓ " : "✗ ") + r.reason;
      toast(r.reason, r.accepted ? "ok" : "bad");
      await loadState();
    } catch (e) {
      box.className = "scan-result bad";
      box.textContent = "✗ " + e.message;
    }
  });
  $("#remove-btn").addEventListener("click", async () => {
    try {
      await api("/api/remove", {
        case_code: $("#remove-case").value,
        reason: $("#remove-reason").value || "临时撤展",
        staff_id: currentUser().id,
      });
      toast("已撤下并登记临时撤展", "ok");
      $("#remove-reason").value = "";
      await loadState();
    } catch (e) { toast(e.message, "bad"); }
  });
  $("#preview-close").addEventListener("click", () => $("#preview-mask").hidden = true);
  $("#preview-mask").addEventListener("click", (e) => {
    if (e.target.id === "preview-mask") $("#preview-mask").hidden = true;
  });
}

async function init() {
  bindStatic();
  await loadState();
  $("#role-user").innerHTML = STATE.staff.map((s) =>
    `<option value="${s.id}">${esc(s.name)}（${roleName(s.role)}）</option>`).join("");
  // 默认以现场更换人员进入，便于直接演示装错柜扫码
  const inst = STATE.staff.find((s) => s.role === "installer");
  if (inst) $("#role-user").value = inst.id;
  renderAll();
}

init();
