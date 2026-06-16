const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let currentJob = null;
let lastResult = null;
let isDemo = false;
let confirmStage = "videos"; // which gate the modal is currently asking about
let veoOpts = { aspect_ratio: "9:16", number_of_videos: 1, model: null };

const AVATAR_COLORS = [
  "linear-gradient(135deg,#818cf8,#38bdf8)",
  "linear-gradient(135deg,#4fd1c5,#34d399)",
  "linear-gradient(135deg,#f472b6,#fb7185)",
  "linear-gradient(135deg,#fbbf24,#f97316)",
  "linear-gradient(135deg,#a78bfa,#6366f1)",
  "linear-gradient(135deg,#22d3ee,#3b82f6)",
];

window.addEventListener("DOMContentLoaded", () => {
  checkHealth();
  $("#run").addEventListener("click", () => run(false));
  $("#demo").addEventListener("click", () => run(true));
  $("#file").addEventListener("change", (e) => loadFile(e.target.files[0]));
  $("#script").addEventListener("input", updateCount);
  $("#download").addEventListener("click", () => {
    if (currentJob) window.location = `/api/download/${currentJob}`;
  });
  $("#viewPrompts").addEventListener("click", () => {
    if (currentJob) window.open(`/prompts?job=${currentJob}`, "_blank");
  });
  $("#viewVideos").addEventListener("click", () => {
    if (currentJob) window.open(`/videos?job=${currentJob}`, "_blank");
  });
  setupSegment("#aspectSeg", "aspect", (v) => (veoOpts.aspect_ratio = v));
  setupSegment("#countSeg", "count", (v) => (veoOpts.number_of_videos = +v));
  $("#modelSelect").addEventListener("change", (e) => (veoOpts.model = e.target.value));
  $("#confirmGo").addEventListener("click", () => respondConfirm(true));
  $("#confirmCancel").addEventListener("click", () => respondConfirm(false));
  setupDropzone();
  updateCount();
});

/* ---------------- Confirm-before-prompts modal (gate 1, before step 5) ------- */
function showConfirmPrompts(ev) {
  confirmStage = "prompts";
  const n = ev.shots;
  $("#confirmTitle").innerHTML = `Write prompts for <span id="confirmCount">${n}</span> shots?`;
  $("#confirmBody").textContent =
    `The script was split into ${n} single-shot beat${n === 1 ? "" : "s"}. ` +
    `Next, each is turned into a Veo prompt. Continue when ready.`;
  $("#confirmNote").hidden = true;
  $("#confirmGo .cta-label").textContent = "Write prompts";
  $("#pipelinePill").textContent = "awaiting confirmation";
  $("#pipelinePill").className = "pill";
  $("#confirmModal").hidden = false;
}

/* ---------------- Confirm-before-generate modal (gate 2, before step 6) ------ */
function showConfirm(ev) {
  confirmStage = "videos";
  $("#confirmTitle").innerHTML = `Generate <span id="confirmCount">${ev.videos}</span> videos?`;
  const each = ev.per_shot > 1 ? ` (${ev.shots} shots × ${ev.per_shot})` : "";
  $("#confirmBody").textContent =
    `This will send ${ev.videos} clip${ev.videos === 1 ? "" : "s"}${each} to Veo for generation. ` +
    `They'll render in batches and upload to Azure when ready.`;
  const note = $("#confirmNote");
  if (ev.capped) {
    note.hidden = false;
    note.textContent =
      `Heads up: this run has ${ev.total_shots} shots, but only the first ${ev.shots} ` +
      `will be generated to stay under the ${ev.max_videos}-video per-run limit.`;
  } else {
    note.hidden = true;
  }
  $("#confirmGo .cta-label").textContent = "Generate videos";
  $("#pipelinePill").textContent = "awaiting confirmation";
  $("#pipelinePill").className = "pill";
  $("#confirmModal").hidden = false;
}

async function respondConfirm(proceed) {
  $("#confirmModal").hidden = true;
  const stage = confirmStage;
  if (proceed) {
    $("#pipelinePill").textContent = stage === "prompts" ? "writing prompts" : "rendering";
  } else {
    toast(
      stage === "prompts"
        ? "Stopped before prompts — shot breakdown is saved."
        : "Video generation cancelled — prompts are still available."
    );
  }
  try {
    await fetch(`/api/confirm/${currentJob}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proceed, stage }),
    });
  } catch {
    /* the stream will surface any failure */
  }
}

function setupSegment(sel, attr, onPick) {
  const seg = $(sel);
  if (!seg) return;
  seg.addEventListener("click", (e) => {
    const btn = e.target.closest(".seg-btn");
    if (!btn) return;
    seg.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    onPick(btn.dataset[attr]);
  });
}

/* ---------------- Health ---------------- */
async function checkHealth() {
  const el = $("#health");
  try {
    const r = await fetch("/api/health");
    const h = await r.json();
    populateModels(h);
    el.innerHTML = `
      ${chip(h.gemini_key_set, h.gemini_key_set ? "Gemini connected" : "Gemini key missing")}
      <span class="chip"><span class="led" style="background:var(--violet)"></span>${h.gemini_model}</span>
      <span class="chip"><span class="led" style="background:var(--cyan)"></span>${h.veo_model || "veo"}</span>
      ${chip(h.azure_configured, h.azure_configured ? "Azure ready" : "Azure idle")}
    `;
  } catch {
    el.innerHTML = chip(false, "Backend unreachable");
  }
}
function chip(ok, label) {
  return `<span class="chip ${ok ? "ok" : "bad"}"><span class="led"></span>${label}</span>`;
}

function populateModels(h) {
  const sel = $("#modelSelect");
  const models = h.veo_models || [{ id: h.veo_model, label: h.veo_model }];
  sel.innerHTML = models
    .map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`)
    .join("");
  const def = h.veo_model && models.some((m) => m.id === h.veo_model) ? h.veo_model : models[0].id;
  sel.value = def;
  veoOpts.model = def;
  if (h.veo_default_aspect_ratio) {
    veoOpts.aspect_ratio = h.veo_default_aspect_ratio;
    const seg = $("#aspectSeg");
    seg.querySelectorAll(".seg-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.aspect === h.veo_default_aspect_ratio)
    );
  }
}

/* ---------------- Input ---------------- */
function updateCount() {
  const n = $("#script").value.length;
  $("#charcount").textContent = `${n.toLocaleString()} character${n === 1 ? "" : "s"}`;
}

function loadFile(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    $("#script").value = reader.result;
    $("#filename").textContent = file.name;
    updateCount();
  };
  reader.readAsText(file);
}

function setupDropzone() {
  const dz = $("#dropzone");
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === "dragleave" && dz.contains(e.relatedTarget)) return;
      dz.classList.remove("dragover");
    })
  );
  dz.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) loadFile(file);
  });
}

/* ---------------- Run ---------------- */
async function run(demo = false) {
  const script = $("#script").value.trim();
  if (!script && !demo) return toast("Paste or upload a script first.", true);

  isDemo = demo;
  resetUI();
  const btn = $("#run");
  btn.disabled = true;
  $("#demo").disabled = true;
  btn.classList.add("loading");
  if (demo) toast("Demo mode — sample data, no API key used.");
  $("#progressPanel").hidden = false;
  $("#progressPanel").classList.add("reveal");

  try {
    const r = await fetch(`/api/upload${demo ? "?demo=1" : ""}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ script, demo, ...veoOpts }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Upload failed");
    currentJob = data.job_id;
    listen(currentJob);
  } catch (err) {
    fail(err.message);
  }
}

function listen(jobId) {
  const es = new EventSource(`/api/stream/${jobId}`);
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "step") handleStep(ev);
    else if (ev.type === "confirm_prompts") {
      showConfirmPrompts(ev);
    } else if (ev.type === "confirm") {
      showConfirm(ev);
    } else if (ev.type === "complete") {
      lastResult = ev.result;
      renderResult();
      const stoppedEarly = ev.cancelled || ev.cancelled_prompts;
      $("#pipelinePill").textContent = ev.cancelled_prompts
        ? "shots only"
        : ev.cancelled
        ? "prompts only"
        : "complete";
      $("#pipelinePill").className = `pill ${stoppedEarly ? "" : "done"}`;
      es.close();
      unlock();
      toast(
        ev.cancelled_prompts
          ? "Stopped before prompts — shot breakdown saved."
          : ev.cancelled
          ? "Prompts ready — videos skipped."
          : "Shots generated."
      );
    } else if (ev.type === "error") {
      fail(ev.message);
      es.close();
    }
  };
  es.onerror = () => {
    es.close();
    unlock();
  };
}

function handleStep(ev) {
  const li = document.querySelector(`.tl-step[data-step="${ev.step}"]`);
  if (!li) return;
  if (ev.status === "start") {
    li.classList.add("active");
    setStepMsg(li, ev.message);
  } else if (ev.status === "progress") {
    li.classList.add("active");
    setStepMsg(li, ev.message);
  } else if (ev.status === "done") {
    li.classList.remove("active");
    li.classList.add("done");
    setStepMsg(li, ev.message);
  } else if (ev.status === "error") {
    li.classList.add("error");
    setStepMsg(li, ev.message);
  }
}

function setStepMsg(li, msg) {
  if (!msg) return;
  const el = li.querySelector(".tl-msg");
  if (el) el.textContent = msg;
}

function fail(msg) {
  $("#pipelinePill").textContent = "error";
  $("#pipelinePill").className = "pill error";
  document.querySelector(".tl-step.active")?.classList.add("error");
  unlock();
  toast(msg, true);
}

function unlock() {
  const btn = $("#run");
  btn.disabled = false;
  btn.classList.remove("loading");
  $("#demo").disabled = false;
}

/* ---------------- Render ---------------- */
function renderResult() {
  // Keep the studio page focused on the action buttons (View prompts / View
  // videos / Download CSV). The full concept, style bible and per-shot detail
  // live on the Prompts and Videos pages, so we don't repeat any of it here.
  $("#conceptPanel").hidden = true;
  $("#resultsPanel").hidden = false;
  $("#resultsPanel").classList.add("reveal");
}

function kv(label, value, extra = "") {
  if (!value) return "";
  return `<div class="kv ${extra}"><div class="label">${label}</div><div class="value">${esc(value)}</div></div>`;
}

function renderChars(chars) {
  if (!chars || !chars.length) return "";
  const cards = chars
    .map((c, i) => {
      const initials = (c.name || "?").trim().slice(0, 2).toUpperCase();
      const color = AVATAR_COLORS[i % AVATAR_COLORS.length];
      return `
      <div class="char">
        <div class="avatar" style="background:${color}">${esc(initials)}</div>
        <div>
          <div class="name">${esc(c.name)}</div>
          <div class="role">${esc(c.description)}</div>
          <div class="meta"><b>Appearance</b> · ${esc(c.appearance)}</div>
          <div class="meta"><b>Wardrobe</b> · ${esc(c.wardrobe)}</div>
        </div>
      </div>`;
    })
    .join("");
  return `<div class="chars-head">Character bible</div><div class="chars">${cards}</div>`;
}

function field(label, value, extra = "") {
  if (!value) return "";
  return `<div class="field ${extra}"><div class="label">${label}</div><div class="text">${esc(value)}</div></div>`;
}

/* ---------------- Misc ---------------- */
function resetUI() {
  $$(".tl-step").forEach((li) => li.classList.remove("active", "done", "error"));
  $("#pipelinePill").textContent = "running";
  $("#pipelinePill").className = "pill";
  ["#conceptPanel", "#resultsPanel"].forEach((s) => {
    $(s).hidden = true;
    $(s).classList.remove("reveal");
  });
  $("#concept").innerHTML = "";
}

let toastTimer;
function toast(msg, bad = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast show ${bad ? "bad" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), 2600);
}

function esc(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escAttr(s) {
  return esc(s).replace(/"/g, "&quot;");
}
