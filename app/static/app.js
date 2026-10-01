/* 馆内展签管理 —— 前端（无框架、无构建） */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let STATE = null;
let currentActor = null;
let selectedExhibit = null;
let selectedVersionId = null;
let lastScan = null;

const STATUS_TEXT = { draft: "草稿", approved: "已批准", printed: "已打印", superseded: "已过期" };
const ROLE_TEXT = { curator: "策展人", editor: "编辑", keeper: "保管员" };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  setTimeout(() => t.classList.add("hidden"), 3600);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function badge(status) {
  return `<span class="badge ${status}">${STATUS_TEXT[status] || status}</span>`;
}
function yesBadge(ok, okText, badText) {
  return ok ? `<span class="badge ok">${okText}</span>` : `<span class="badge bad">${badText}</span>`;
}

// --------------------------------------------------------------- 初始化

async function refresh() {
  STATE = await api("/api/state");
  renderAll();
}

async function renderAll() {
  renderActors();
  renderCatalog();
  renderActive();
  await fillCommonSelects();
  renderVersionExhibits();
  if (selectedExhibit) renderVersions();
  renderPrintQueue();
  renderScans();
  renderInstallations();
  renderIncidents();
}

function renderActors() {
  const sel = $("#actorSel");
  if (!sel.options.length) {
    STATE.users.forEach((u) => {
      const o = document.createElement("option");
      o.value = u.id; o.textContent = `${u.display_name}（${ROLE_TEXT[u.role]}）`;
      sel.appendChild(o);
    });
    sel.value = "1";
    sel.addEventListener("change", () => { currentActor = actor(); renderAll(); });
  }
  currentActor = actor();
  const rb = $("#actorRole");
  rb.textContent = ROLE_TEXT[currentActor.role];
  rb.className = "role-badge " + currentActor.role;
}
function actor() {
  return STATE.users.find((u) => u.id === Number($("#actorSel").value)) || STATE.users[0];
}
function is(role) { return currentActor && currentActor.role === role; }

// --------------------------------------------------------------- 目录

function renderCatalog() {
  $("#catalogBody").innerHTML = STATE.exhibits.map((e) => `
    <tr>
      <td><strong>${esc(e.case_id)}</strong><br><span class="hint">${esc(e.case_name)}</span></td>
      <td>${esc(e.hall)}</td>
      <td><code>${esc(e.exhibit_id)}</code></td>
      <td>${esc(e.exhibit_title)}</td>
      <td>${e.cur_version_no ? `v${e.cur_version_no} ${badge(e.cur_status)}` : '<span class="empty">未安装</span>'}</td>
      <td>${e.pending_version_no ? `<span class="badge pending">待换 v${e.pending_version_no}</span>` : '<span class="empty">—</span>'}</td>
      <td><button class="btn small" data-goto="${esc(e.exhibit_id)}">版本与校对</button></td>
    </tr>`).join("");
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => {
    selectedExhibit = b.dataset.goto;
    switchTab("versions");
  }));
}

function renderActive() {
  $("#activeList").innerHTML = STATE.active.length
    ? STATE.active.map((a) => `
      <div class="card">
        <h4>${esc(a.title)}</h4>
        <div class="meta">${esc(a.case_id)} · ${esc(a.case_name)} · ${esc(a.exhibit_id)}</div>
        <div class="meta">有效版次 <strong>v${a.version_no}</strong> · 安装 ${esc(a.start_time)} · ${esc(a.operator)}</div>
      </div>`).join("")
    : '<p class="empty">尚无有效展签。</p>';
}

async function fillCommonSelects() {
  fill("#scanCase", STATE.cases.map((c) => [c.id, `${c.id} ${c.name}`]), null, false);
  fill("#removeCase", STATE.cases.map((c) => [c.id, `${c.id} ${c.name}`]), null, false);
  fill("#incCase", STATE.cases.map((c) => [c.id, c.id]), "", true);
  fill("#incKind", Object.entries(STATE.incident_kinds).map(([k, v]) => [k, v]), null, false);
  // 已批准/已打印才能作为现场待装展签
  await fill("#scanVersion", await pendingLabelOptions(), null, false);
}
function fill(sel, pairs, keepBlank, sort) {
  const el = $(sel);
  const prev = el.value;
  el.innerHTML = (keepBlank === "" ? '<option value="">（关联展柜，可选）</option>' : "") +
    pairs.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  if (prev && pairs.some(([v]) => String(v) === prev)) el.value = prev;
}
async function pendingLabelOptions() {
  // 取每个展品最新 approved/printed 版本
  const opts = [];
  for (const e of STATE.exhibits) {
    const versions = await api(`/api/versions?exhibit_id=${encodeURIComponent(e.exhibit_id)}`);
    const cand = versions.find((v) => ["approved", "printed"].includes(v.status));
    if (cand) opts.push([cand.id, `${e.case_id} → ${e.exhibit_title} v${cand.version_no}（${STATUS_TEXT[cand.status]}）`]);
  }
  return opts;
}

// --------------------------------------------------------------- 版本与校对

function renderVersionExhibits() {
  const el = $("#versionExhibit");
  if (!selectedExhibit) selectedExhibit = STATE.exhibits[0]?.exhibit_id;
  el.innerHTML = STATE.exhibits
    .map((e) => `<option value="${esc(e.exhibit_id)}">${esc(e.case_id)} · ${esc(e.exhibit_id)} · ${esc(e.exhibit_title)}</option>`)
    .join("");
  el.value = selectedExhibit || "";
}

async function renderVersions() {
  const versions = await api(`/api/versions?exhibit_id=${encodeURIComponent(selectedExhibit)}`);
  $("#versionList").innerHTML = versions.map((v) => `
    <tr data-vid="${v.id}" class="${v.id === selectedVersionId ? "sel" : ""}" style="cursor:pointer">
      <td>v${v.version_no}</td>
      <td>${badge(v.status)}</td>
      <td>${v.proofread_ok ? yesBadge(true, "有效", "") : yesBadge(false, "", "失效/缺")}</td>
      <td>${v.fact_check ? yesBadge(v.fact_check.exhibit_id_ok && v.fact_check.anchor_hash === v.content_hash, "已核对", "已失效") : '<span class="empty">未核</span>'}</td>
    </tr>`).join("");
  $$("#versionList tr").forEach((tr) => tr.addEventListener("click", () => {
    selectedVersionId = Number(tr.dataset.vid);
    renderVersions();
    renderVersionDetail();
  }));
  if (!selectedVersionId || !versions.some((v) => v.id === selectedVersionId)) {
    selectedVersionId = versions[0]?.id ?? null;
  }
  if (selectedVersionId) renderVersionDetail(versions.find((v) => v.id === selectedVersionId));
}

async function renderVersionDetail(prefetched) {
  const v = prefetched || await api(`/api/version?id=${selectedVersionId}`);
  if (!v) return;
  const comments = await api(`/api/proofreads?version_id=${v.id}`);
  const rev = v.revision;
  const sameAuthor = v.created_by === currentActor.id;

  const diffBlock = rev ? `
    <div class="diff-head"><div>${rev.base_version_id === v.id ? "修改前（原文）" : `基线 v${(await baselineNo(rev.base_version_id))}`}</div><div>当前版（修订）</div></div>
    ${diffField("名称", rev.old_title, v.title)}
    ${diffField("年代", rev.old_era, v.era)}
    ${diffField("材质", rev.old_material, v.material)}
    ${diffField("说明文字", rev.old_body, v.body)}
    ${diffField("二维码链接", rev.old_qr_url, v.qr_url)}
    ${rev.note ? `<p class="hint">修订说明：${esc(rev.note)}</p>` : ""}
  ` : `
    <div class="field-label">名称</div><div>${esc(v.title)}</div>
    <div class="field-label">年代 · 材质</div><div>${esc(v.era)} · ${esc(v.material)}</div>
    <div class="field-label">说明文字</div><div style="white-space:pre-wrap;font-family:sans-serif;font-size:13px">${esc(v.body)}</div>
    <div class="field-label">二维码链接</div><div class="hint">${esc(v.qr_url)}</div>
  `;

  $("#versionDetail").innerHTML = `
    <div class="row" style="justify-content:space-between">
      <h3 style="margin:0">${esc(v.exhibit_title)} · v${v.version_no} ${badge(v.status)}</h3>
      <span class="hint">提交：${esc(v.author_name)} · ${esc(v.created_at)}</span>
    </div>
    ${diffBlock}
    <div class="field-label">校对批注（编辑用语校对）</div>
    <div class="comments">
      ${comments.length ? comments.map((c) => `
        <div class="comment ${c.valid ? "" : "invalid"}">
          ${esc(c.comment)}
          <div class="who">${esc(c.editor_name)} · ${c.valid ? "锚定当前正文，有效" : "⚠ 正文已修改，该校对结论失效"}</div>
        </div>`).join("") : '<p class="empty">尚无校对批注。</p>'}
    </div>
    <div class="field-label">保管员编号核对</div>
    <div style="font-family:sans-serif;font-size:13px">
      ${v.fact_check
        ? `${yesBadge(v.fact_check.exhibit_id_ok && v.fact_check.anchor_hash === v.content_hash, "编号与展柜一致 · 有效", "核对已随正文修改失效")}
           <span class="hint">${esc(v.fact_check.keeper_name)}${v.fact_check.note ? " · " + esc(v.fact_check.note) : ""}</span>`
        : '<span class="empty">尚未核对展品编号。</span>'}
    </div>
    <div class="actions">
      ${is("curator") && v.status === "draft" && sameAuthor
        ? `<button class="btn" id="editDraftBtn">修改草稿正文（旧校对将失效）</button>
           <button class="btn copper" id="newVerBtn">基于此版提交新版</button>` : ""}
      ${is("editor") ? `<button class="btn" id="proofBtn">＋ 校对批注</button>` : ""}
      ${is("keeper")
        ? `<button class="btn" id="fcOkBtn">编号核对：一致</button>
           <button class="btn ghost" id="fcBadBtn">编号不符</button>` : ""}
      <button class="btn" id="previewBtn">真实比例预览</button>
      <button class="btn copper" id="printBtn" ${v.ready_to_print ? "" : "disabled"}>打印展签样图</button>
    </div>
    ${!v.proofread_ok && v.status === "draft" ? '<p class="hint" style="margin-top:8px">⚠ 无有效校对结论时不能打印 / 安装。</p>' : ""}
    ${is("keeper") && sameAuthor ? '<p class="hint" style="margin-top:8px">注：策展人提交的版本不可由本人核对——当前操作人即提交人时系统将拒绝。</p>' : ""}
    <div id="detailMsg"></div>`;

  bindDetailButtons(v);
}

async function baselineNo(baseId) {
  if (!baseId) return "?";
  const b = await api(`/api/version?id=${baseId}`);
  return b.version_no;
}

function diffField(label, oldV, newV) {
  const changed = (oldV ?? "") !== (newV ?? "");
  return `
    <div class="field-label">${label}${changed ? ' <span class="badge pending">已改</span>' : ""}</div>
    <div class="diff">
      <div class="col old ${changed ? "changed" : ""}">${esc(oldV ?? "—")}</div>
      <div class="col new ${changed ? "changed" : ""}">${esc(newV ?? "—")}</div>
    </div>`;
}

function bindDetailButtons(v) {
  const E = (id, fn) => { const b = $(id); if (b) b.addEventListener("click", fn); };
  E("#proofBtn", () => openCommentModal(v));
  E("#fcOkBtn", () => factCheck(v, true));
  E("#fcBadBtn", () => factCheck(v, false));
  E("#previewBtn", () => { switchTab("print"); showPreview(v.id); });
  E("#printBtn", () => doPrint(v));
  E("#newVerBtn", () => openVersionModal(v));
  E("#editDraftBtn", () => openEditDraftModal(v));
}

// --------------------------------------------------------------- 模态框

function modal(html) {
  $("#modalCard").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

function versionFormFields(prefill) {
  const f = prefill || {};
  return `
    <div class="fld"><label>名称</label><input id="fTitle" value="${esc(f.title || "")}"></div>
    <div class="fld"><label>年代</label><input id="fEra" value="${esc(f.era || "")}"></div>
    <div class="fld"><label>材质</label><input id="fMaterial" value="${esc(f.material || "")}"></div>
    <div class="fld"><label>说明文字</label><textarea id="fBody" rows="4">${esc(f.body || "")}</textarea></div>
    <div class="fld"><label>二维码链接</label><input id="fQr" value="${esc(f.qr_url || "")}"></div>
    <div class="fld"><label>修订说明（可选）</label><input id="fNote"></div>`;
}

function openVersionModal(base) {
  modal(`<h3>${base ? `基于 v${base.version_no} 提交新版` : "策展人提交展签新版"}</h3>
    ${versionFormFields(base)}
    <div class="modal-foot">
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn copper" id="submitVer">提交</button>
    </div>`);
  $("#submitVer").addEventListener("click", async () => {
    try {
      const r = await api("/api/versions", { method: "POST", body: {
        actor_id: currentActor.id, exhibit_id: selectedExhibit,
        title: $("#fTitle").value, era: $("#fEra").value, material: $("#fMaterial").value,
        body: $("#fBody").value, qr_url: $("#fQr").value, note: $("#fNote").value,
        base_version_id: base ? base.id : null,
      }});
      closeModal();
      toast(`已生成 v${r.version_no} 草稿；如正文有改动，旧版批准状态已过期`, "ok");
      selectedVersionId = r.id;
      await refresh();
    } catch (e) { toast(e.message, "bad"); }
  });
}

function openEditDraftModal(v) {
  modal(`<h3>修改 v${v.version_no} 草稿正文</h3>
    <p class="hint">版本号不变，但正文指纹改变 → 既有校对批注与编号核对立即失效，须重新校对。</p>
    ${versionFormFields(v)}
    <div class="modal-foot">
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn copper" id="saveDraft">保存修改</button>
    </div>`);
  $("#saveDraft").addEventListener("click", async () => {
    try {
      await api(`/api/versions/${v.id}/edit`, { method: "POST", body: {
        actor_id: currentActor.id,
        title: $("#fTitle").value, era: $("#fEra").value, material: $("#fMaterial").value,
        body: $("#fBody").value, qr_url: $("#fQr").value, note: $("#fNote").value,
      }});
      closeModal();
      toast("正文已修改，先前校对结论已失效", "bad");
      await refresh();
    } catch (e) { toast(e.message, "bad"); }
  });
}

function openCommentModal(v) {
  modal(`<h3>编辑校对批注 — v${v.version_no}</h3>
    <div class="fld"><label>用语校对意见</label>
      <textarea id="cText" rows="3" placeholder="如：年代表述建议统一为「北宋」；「纹路」改为「纹饰」"></textarea></div>
    <div class="modal-foot">
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn copper" id="cSave">提交批注</button>
    </div>`);
  $("#cSave").addEventListener("click", async () => {
    try {
      await api(`/api/versions/${v.id}/proofread`, { method: "POST", body: {
        actor_id: currentActor.id, comment: $("#cText").value }});
      closeModal(); toast("批注已提交，锚定当前正文", "ok"); await refresh();
    } catch (e) { toast(e.message, "bad"); }
  });
}

async function factCheck(v, ok) {
  try {
    const r = await api(`/api/versions/${v.id}/factcheck`, { method: "POST", body: {
      actor_id: currentActor.id, exhibit_id_ok: ok,
      note: ok ? "展品编号与展柜绑定一致" : "编号/展柜不符，待更正" }});
    toast(ok ? "编号核对通过" : "已标记编号不符", ok ? "ok" : "bad");
    if (ok && r.status === "approved") toast("校对齐全：版本已批准，可打印", "ok");
    await refresh();
  } catch (e) { toast(e.message, "bad"); }
}

async function doPrint(v) {
  try {
    const r = await api(`/api/versions/${v.id}/print`, { method: "POST", body: { actor_id: currentActor.id }});
    toast(`已打印 v${r.version_no}，样图存入 data/samples/`, "ok");
    await refresh();
    switchTab("print");
    showPreview(v.id);
  } catch (e) { toast(e.message, "bad"); }
}

// --------------------------------------------------------------- 打印

function renderPrintQueue() {
  $("#printBody").innerHTML = STATE.prints.length ? STATE.prints.map((p) => `
    <tr>
      <td>${p.id}</td><td>${esc(p.exhibit_id)}</td><td>v${p.version_no}</td>
      <td>${esc(p.who)}</td><td class="hint">${esc(p.created_at)}</td>
      <td><button class="btn small" data-pvid="${p.version_id}">查看样图</button></td>
    </tr>`).join("")
    : '<tr><td colspan="6" class="empty">尚无打印任务。</td></tr>';
  $$("[data-pvid]").forEach((b) => b.addEventListener("click", () => showPreview(Number(b.dataset.pvid))));
}

async function showPreview(versionId) {
  const svg = await (await fetch(`/api/preview?id=${versionId}`)).text();
  $("#previewStage").innerHTML = svg;
}

// --------------------------------------------------------------- 扫码

$("#doScanBtn").addEventListener("click", async () => {
  try {
    const r = await api("/api/scan", { method: "POST", body: {
      version_id: Number($("#scanVersion").value),
      case_id: $("#scanCase").value,
      operator: $("#scanOperator").value || "现场人员" }});
    lastScan = r;
    $("#scanResult").innerHTML = r.matched
      ? `<p class="scan-ok">✓ ${esc(r.detail)}</p>
         <button class="btn copper" id="installBtn">凭此扫码记录安装到 ${esc(r.scanned_case_id)}</button>`
      : `<p class="scan-bad">✗ ${esc(r.detail)}</p>
         <p class="hint">装错柜：系统禁止安装。请改扫正确展柜码，或登记「旧展签未撤 / 二维码指向错误」事件。</p>`;
    const ib = $("#installBtn");
    if (ib) ib.addEventListener("click", doInstall);
    await refresh();
  } catch (e) { toast(e.message, "bad"); }
});

async function doInstall() {
  // 找到刚生成的扫码记录 id
  const scans = await api("/api/scans");
  const s = scans[0];
  try {
    const r = await api("/api/install", { method: "POST", body: {
      actor_name: $("#scanOperator").value || "现场人员",
      version_id: Number($("#scanVersion").value), scan_id: s.id }});
    toast(r.replaced ? `已安装，原 v 展签已自动撤换（每柜唯一）` : "安装成功", "ok");
    $("#scanResult").innerHTML = '<p class="scan-ok">✓ 展签已安装并生效。</p>';
    await refresh();
  } catch (e) { toast(e.message, "bad"); }
}

function renderScans() {
  $("#scanBody").innerHTML = STATE.scans.length ? STATE.scans.map((s) => `
    <tr>
      <td>${s.id}</td><td>#${s.label_version_id}</td>
      <td><code>${esc(s.expected_case_id)}</code></td>
      <td><code class="${s.matched ? "" : "scan-bad"}">${esc(s.scanned_case_id)}</code></td>
      <td>${s.matched ? yesBadge(true, "匹配", "") : yesBadge(false, "", "装错柜")}</td>
      <td>${esc(s.operator)}</td><td class="hint">${esc(s.detail)}</td>
      <td class="hint">${esc(s.created_at)}</td>
    </tr>`).join("")
    : '<tr><td colspan="8" class="empty">暂无扫码记录。</td></tr>';
}

// --------------------------------------------------------------- 撤换 / 事件

function renderInstallations() {
  // 全量流水在另一个接口
  api("/api/installations").then((rows) => {
    $("#installBody").innerHTML = rows.map((r) => `
      <tr>
        <td>${esc(r.case_id)}<br><span class="hint">${esc(r.case_name)}</span></td>
        <td>${esc(r.exhibit_id)}</td><td>v${r.version_no}</td>
        <td class="hint">${esc(r.start_time)}</td>
        <td class="hint">${r.end_time ? esc(r.end_time) : '<span class="badge ok">有效中</span>'}</td>
        <td>${esc(r.reason || "")}</td><td>${esc(r.operator)}</td>
      </tr>`).join("");
  });
}

$("#removeBtn").addEventListener("click", async () => {
  try {
    await api("/api/remove", { method: "POST", body: {
      case_id: $("#removeCase").value, reason: $("#removeReason").value || "临时撤展",
      actor_name: currentActor.display_name }});
    toast("已撤下并记录", "ok");
    $("#removeReason").value = "";
    await refresh();
  } catch (e) { toast(e.message, "bad"); }
});

$("#incBtn").addEventListener("click", async () => {
  try {
    await api("/api/incidents", { method: "POST", body: {
      actor_id: currentActor.id, kind: $("#incKind").value,
      description: $("#incDesc").value, case_id: $("#incCase").value }});
    toast("异常事件已记录", "ok");
    $("#incDesc").value = "";
    await refresh();
  } catch (e) { toast(e.message, "bad"); }
});

function renderIncidents() {
  $("#incidentList").innerHTML = STATE.incidents.length ? STATE.incidents.map((x) => `
    <div class="incident">
      <span class="k">${esc(x.kind_label)}</span>
      ${x.case_id ? ` · <code>${esc(x.case_id)}</code>` : ""}
      <div>${esc(x.description)}</div>
      <div class="t">${esc(x.who)} · ${esc(x.created_at)}</div>
    </div>`).join("")
    : '<p class="empty">暂无异常事件。</p>';
}

// --------------------------------------------------------------- 标签页

async function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "scan") await fill("#scanVersion", await pendingLabelOptions(), null, false);
}
$$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

$("#newVersionBtn").addEventListener("click", () => {
  if (!is("curator")) return toast("只有策展人可以提交新版展签（角色分离）", "bad");
  openVersionModal(null);
});
$("#versionExhibit").addEventListener("change", (e) => {
  selectedExhibit = e.target.value; selectedVersionId = null; renderVersions();
});

// 启动
refresh().catch((e) => toast("初始化失败：" + e.message, "bad"));
