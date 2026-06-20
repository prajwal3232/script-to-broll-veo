const $ = (sel) => document.querySelector(sel);

function jobId() {
  return new URLSearchParams(location.search).get("job");
}

async function fetchResult() {
  const id = jobId();
  if (!id) throw new Error("No job id in the URL. Open this page from the studio.");
  const r = await fetch(`/api/result/${id}`);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "Could not load job");
  if (!data.result) throw new Error("This job has no result yet.");
  return data.result;
}

function emptyState(msg) {
  return `<div class="page-empty">${esc(msg)}</div>`;
}

function promptMeta(s, imageChars) {
  const chips = [];
  if (s.location) chips.push(`<span class="meta-chip loc">📍 ${esc(s.location)}</span>`);
  (s.characters || []).forEach((c) => {
    const ref = imageChars && imageChars.has(normName(c)) ? " 📷" : "";
    chips.push(`<span class="meta-chip char">${esc(c)}${ref}</span>`);
  });
  if (s.veo_duration) chips.push(`<span class="meta-chip dur">⏱ ${esc(s.veo_duration)}s</span>`);
  if (s.veo_seed != null) chips.push(`<span class="meta-chip seed">seed ${esc(s.veo_seed)}</span>`);
  if (!chips.length) return "";
  return `<div class="prompt-meta">${chips.join("")}</div>`;
}

/* ---------------- Prompts page ---------------- */
async function renderPrompts() {
  const list = $("#list");
  let result;
  try {
    result = await fetchResult();
  } catch (err) {
    list.innerHTML = emptyState(err.message);
    return;
  }
  const segments = result.segments || [];
  // Characters that were locked to an uploaded reference photo (image-backed).
  const imageChars = new Set(
    ((result.style || {}).characters || [])
      .filter((c) => c.has_image || c.image_slug)
      .map((c) => normName(c.name))
  );
  list.innerHTML = segments
    .map(
      (s) => `
    <div class="prompt-card">
      <div class="prompt-head">
        <span class="shot-num">${esc(s.id)}</span>
        ${s.voiceover ? `<span class="prompt-vo">“${esc(s.voiceover)}”</span>` : ""}
        <div class="spacer"></div>
        <button class="ghost-btn sm copy-btn" data-text="${escAttr(s.veo_prompt || "")}">Copy</button>
      </div>
      ${promptMeta(s, imageChars)}
      <pre class="prompt-body">${esc(s.veo_prompt || "—")}</pre>
    </div>`
    )
    .join("");

  document.querySelectorAll(".copy-btn").forEach((b) =>
    b.addEventListener("click", () => {
      navigator.clipboard.writeText(b.dataset.text);
      toast("Prompt copied.");
    })
  );
  const all = $("#copyAll");
  if (all)
    all.addEventListener("click", () => {
      const text = segments
        .map((s) => `# Shot ${s.id}\n${s.veo_prompt || ""}`)
        .join("\n\n");
      navigator.clipboard.writeText(text);
      toast(`Copied ${segments.length} prompts.`);
    });
}

/* ---------------- History page ---------------- */
function relTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)} d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function statusPill(s) {
  const map = { done: "done", running: "", error: "error" };
  const label = { done: "complete", running: "running", error: "failed" }[s] || s;
  return `<span class="pill ${map[s] ?? ""}">${esc(label)}</span>`;
}

async function renderHistory() {
  const list = $("#list");
  let jobs;
  try {
    const r = await fetch("/api/jobs");
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Could not load history");
    jobs = data.jobs || [];
  } catch (err) {
    list.innerHTML = emptyState(err.message);
    return;
  }
  if (!jobs.length) {
    list.innerHTML = emptyState("No runs yet. Generate some shots from the studio.");
    return;
  }
  list.innerHTML = `<div class="history-list">${jobs.map(historyRow).join("")}</div>`;
}

function historyRow(j) {
  const opts = j.opts || {};
  const tags = [];
  if (j.demo) tags.push(`<span class="hist-tag demo">demo</span>`);
  if (opts.aspect_ratio) tags.push(`<span class="hist-tag">${esc(opts.aspect_ratio)}</span>`);
  if (opts.number_of_videos) tags.push(`<span class="hist-tag">${esc(opts.number_of_videos)}× / shot</span>`);
  if (opts.model) tags.push(`<span class="hist-tag">${esc(opts.model)}</span>`);
  if (j.shots) tags.push(`<span class="hist-tag">${esc(j.shots)} shots</span>`);

  const done = j.status === "done";
  const actions = done
    ? `
      <a class="ghost-btn sm" href="/prompts?job=${esc(j.id)}">Prompts</a>
      <a class="ghost-btn sm" href="/videos?job=${esc(j.id)}">Videos</a>
      <a class="ghost-btn sm" href="/api/download/${esc(j.id)}">CSV</a>`
    : j.status === "error"
    ? `<span class="hist-err">${esc(j.error || "Run failed")}</span>`
    : `<span class="hist-running">Still running…</span>`;

  return `
    <div class="history-card">
      <div class="hist-main">
        <div class="hist-head">
          ${statusPill(j.status)}
          <span class="hist-title">${esc(j.title || "Untitled run")}</span>
        </div>
        <div class="hist-meta">
          <span class="hist-time">${esc(relTime(j.created_at))}</span>
          ${tags.join("")}
        </div>
      </div>
      <div class="hist-actions">${actions}</div>
    </div>`;
}

/* ---------------- Videos page ---------------- */
async function renderVideos() {
  const list = $("#list");
  let result;
  try {
    result = await fetchResult();
  } catch (err) {
    list.innerHTML = emptyState(err.message);
    return;
  }
  const segments = result.segments || [];
  list.innerHTML = `<div class="video-grid">${segments.map(videoCard).join("")}</div>`;
  list.addEventListener("click", (e) => {
    const btn = e.target.closest(".retry-btn");
    if (btn) retryShot(btn.dataset.shot);
  });
}

async function retryShot(shotId) {
  const id = jobId();
  const card = document.querySelector(`.video-card[data-shot="${cssEsc(shotId)}"]`);
  const media = card && card.querySelector(".video-card-media");
  const btn = card && card.querySelector(".retry-btn");
  if (btn) { btn.disabled = true; btn.innerHTML = "↻ Retrying…"; }
  if (media) media.innerHTML = retryingMedia();
  try {
    const r = await fetch(`/api/retry/${id}/${encodeURIComponent(shotId)}`, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Retry failed");
    toast("Re-generating clip…");
    await pollShot(shotId);
  } catch (err) {
    toast(err.message, true);
    if (btn) { btn.disabled = false; btn.innerHTML = "↻ Retry"; }
  }
}

async function pollShot(shotId) {
  const id = jobId();
  const deadline = Date.now() + 12 * 60 * 1000; // give Veo up to 12 minutes
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 4000));
    let data;
    try {
      data = await (await fetch(`/api/result/${id}`)).json();
    } catch {
      continue;
    }
    const seg = ((data.result || {}).segments || []).find(
      (s) => String(s.id) === String(shotId)
    );
    if (seg && !seg.video_retrying) {
      const card = document.querySelector(`.video-card[data-shot="${cssEsc(String(seg.id))}"]`);
      if (card) card.outerHTML = videoCard(seg);
      toast(seg.video_error ? "Clip failed again." : "Clip ready.", !!seg.video_error);
      return;
    }
  }
  toast("Retry is taking too long — try again.", true);
}

function retryingMedia() {
  return `<div class="vid-placeholder retrying"><div class="vid-ph-msg"><span class="mini-spin"></span> Re-generating…</div></div>`;
}

function cssEsc(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
}

function demoPlaceholder() {
  return `<div class="vid-placeholder"><div class="vid-ph-badge">DEMO</div><div class="vid-ph-art"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M10 8l6 4-6 4z"/><rect x="3" y="4" width="18" height="16" rx="2"/></svg></div><div class="vid-ph-msg">Demo mode — connect a Veo-enabled key to render a real clip.</div></div>`;
}

function clipBlock(v, label) {
  const actions = [`<a class="vid-link" href="${esc(v.local)}" download>↓ Download MP4</a>`];
  if (v.azure)
    actions.push(`<a class="vid-link" href="${esc(v.azure)}" target="_blank" rel="noopener">☁ Azure URL</a>`);
  return `
    <div class="clip">
      ${label ? `<div class="clip-label">${esc(label)}</div>` : ""}
      <video class="vid-el" src="${esc(v.local)}" controls playsinline preload="metadata"></video>
      <div class="vid-actions">${actions.join("")}</div>
    </div>`;
}

function videoCard(s) {
  let media;
  if (s.video_retrying) {
    media = retryingMedia();
  } else if (s.video_demo) {
    const n = s.video_count || 1;
    media = Array.from({ length: n }, demoPlaceholder).join("");
  } else if (s.videos && s.videos.length) {
    media = s.videos
      .map((v, i) => clipBlock(v, s.videos.length > 1 ? `Take ${i + 1}` : ""))
      .join("");
  } else if (s.video_local) {
    media = clipBlock({ local: s.video_local, azure: s.azure_path }, "");
  } else if (s.video_error) {
    media = `<div class="vid-error">⚠ ${esc(s.video_error)}</div>`;
  } else if (s.video_skipped) {
    media = `<div class="vid-placeholder skipped"><div class="vid-ph-msg">Skipped — over the 50-video per-run limit.</div></div>`;
  } else {
    media = `<div class="vid-placeholder"><div class="vid-ph-msg">No video for this shot.</div></div>`;
  }

  const retrying = !!s.video_retrying;
  return `
    <div class="video-card" data-shot="${escAttr(s.id)}">
      <div class="video-card-head">
        <span class="shot-num">${esc(s.id)}</span>
        ${s.voiceover ? `<span class="prompt-vo">“${esc(s.voiceover)}”</span>` : ""}
        <button class="ghost-btn sm retry-btn" data-shot="${escAttr(s.id)}" ${retrying ? "disabled" : ""}>${retrying ? "↻ Retrying…" : "↻ Retry"}</button>
      </div>
      <div class="video-card-media">${media}</div>
    </div>`;
}

/* ---------------- Misc ---------------- */
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
function normName(s) {
  const n = String(s || "").trim().toLowerCase();
  return n.startsWith("the ") ? n.slice(4) : n;
}
