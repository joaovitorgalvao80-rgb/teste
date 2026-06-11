// Galeria interativa do NWRCH Studio.

async function postForm(url, data) {
  const body = new URLSearchParams(data || {});
  const token = csrfToken();
  if (token && !body.has("csrf_token")) body.set("csrf_token", token);
  const resp = await fetch(url, { method: "POST", body, headers: token ? { "x-csrf-token": token } : {} });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { const j = await resp.json(); msg = j.detail || j.error || msg; }
    catch (_) { try { msg = (await resp.text()).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 200); } catch (_) {} }
    throw new Error(msg);
  }
  return resp.json();
}

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.content : "";
}

async function setState(assetId, state) {
  const card = document.getElementById("asset-" + assetId);
  try {
    const res = await postForm(`/assets/${assetId}/state`, { state });
    card.classList.remove("is-pending", "is-selected", "is-rejected", "is-favorite");
    card.classList.add("is-" + res.state);
    const selectButton = card.querySelector(".sel");
    if (selectButton) selectButton.textContent = res.state === "selected" ? "Selecionado" : "Selecionar";

    if (res.state === "selected") {
      // rebaixa irmãos da mesma cena
      const grid = card.closest(".takes");
      if (grid) {
        grid.querySelectorAll(".take.is-selected").forEach((el) => {
          if (el !== card) {
            el.classList.remove("is-selected");
            const siblingButton = el.querySelector(".sel");
            if (siblingButton) siblingButton.textContent = "Selecionar";
          }
        });
      }
      updateSelectedCount();
    } else {
      updateSelectedCount();
    }
  } catch (e) {
    alert("Falha ao atualizar: " + e.message);
  }
}

function updateSelectedCount() {
  const btn = document.getElementById("btn-package");
  if (!btn) return;
  const total = document.querySelectorAll("section.scene").length;
  let selected = 0;
  document.querySelectorAll("section.scene").forEach((sec) => {
    if (sec.querySelector(".take.is-selected")) selected++;
  });
  const statusBadge = document.querySelector(".head-status .badge");
  const status = statusBadge ? statusBadge.textContent.trim().toLowerCase() : "";
  const busy = ["mapping", "searching", "packaging"].includes(status);
  // atualiza o texto do step no pipeline
  const nameSpan = btn.querySelector(".step-name");
  if (nameSpan) nameSpan.textContent = `Pacote (${selected}/${total})`;
  btn.disabled = busy || total === 0 || selected !== total;
}

async function searchMore(sceneId, btn, media) {
  const original = btn.textContent;
  btn.textContent = "buscando...";
  btn.disabled = true;
  try {
    const res = await postForm(`/scenes/${sceneId}/search-more`, { media: media || "all" });
    btn.textContent = `+${res.added} novos`;
    if (res.added > 0) setTimeout(() => location.reload(), 600);
    else setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1200);
  } catch (e) {
    btn.textContent = original;
    btn.disabled = false;
    alert("Falha na busca: " + e.message);
  }
}

// ------------------------------------------------------------------
// Geração de imagem por IA (Puter.js no browser)
// ------------------------------------------------------------------
const GEN_TIMEOUT_MS = 180000; // 3 min: geração + possível popup de login
const GEN_MAX_BYTES = 15 * 1024 * 1024;
let _puterLoadPromise = null;

function toggleGenPanel(sceneId) {
  const panel = document.getElementById("gen-panel-" + sceneId);
  if (!panel) return;
  const show = panel.style.display === "none";
  panel.style.display = show ? "" : "none";
  if (show) {
    const ta = document.getElementById("gen-prompt-" + sceneId);
    if (ta) ta.focus();
  }
}

function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(label || "tempo esgotado — tente de novo")), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function ensurePuterLoaded() {
  if (typeof puter !== "undefined" && puter.ai && typeof puter.ai.txt2img === "function") {
    return Promise.resolve();
  }
  if (_puterLoadPromise) return _puterLoadPromise;
  _puterLoadPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://js.puter.com/v2/";
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Puter.js não carregou"));
    document.head.appendChild(script);
  });
  return _puterLoadPromise;
}

function genErrorMessage(e) {
  // Puter pode rejeitar com Error, string ou objeto {error:{message}} / {message}
  if (!e) return "erro desconhecido";
  if (typeof e === "string") return e;
  if (e.message) return e.message;
  if (e.error && e.error.message) return e.error.message;
  try { return JSON.stringify(e).slice(0, 200); } catch (_) { return String(e); }
}

async function generateImage(sceneId, btn) {
  const promptEl = document.getElementById("gen-prompt-" + sceneId);
  const status = document.getElementById("gen-status-" + sceneId);
  const prompt = (promptEl ? promptEl.value : "").trim();
  if (!prompt) {
    alert("Escreva um prompt antes de gerar.");
    return;
  }
  if (btn.disabled) return; // protege contra clique duplo
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "gerando...";
  if (status) status.textContent = "carregando gerador externo...";
  try {
    await ensurePuterLoaded();
    if (typeof puter === "undefined" || !puter.ai || typeof puter.ai.txt2img !== "function") {
      throw new Error("Puter.js não inicializou");
    }
    if (status) status.textContent = "gerando imagem (pode levar até 1 min; na primeira vez o Puter pede login)...";
    const img = await withTimeout(puter.ai.txt2img(prompt), GEN_TIMEOUT_MS, "geração demorou demais — tente de novo");
    const src = img && img.src;
    if (!src) throw new Error("o gerador não retornou imagem");
    // garante naturalWidth/Height preenchidos antes de ler
    if (img.decode) { try { await img.decode(); } catch (_) {} }
    const blob = await (await fetch(src)).blob();
    if (!blob || blob.size === 0) throw new Error("imagem retornou vazia");
    if (blob.size > GEN_MAX_BYTES) throw new Error("imagem gerada grande demais (>15 MB)");

    if (status) status.textContent = "salvando no projeto...";
    const fd = new FormData();
    fd.append("image", blob, "generated.png");
    fd.append("prompt", prompt);
    fd.append("width", String(img.naturalWidth || 0));
    fd.append("height", String(img.naturalHeight || 0));
    const token = csrfToken();
    if (token) fd.append("csrf_token", token);
    const resp = await fetch(
      `/scenes/${sceneId}/generated-image`,
      { method: "POST", body: fd, headers: token ? { "x-csrf-token": token } : {} }
    );
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try { const j = await resp.json(); msg = j.detail || j.error || msg; } catch (_) {}
      throw new Error(msg);
    }
    if (status) status.textContent = "imagem adicionada — recarregando...";
    setTimeout(() => location.reload(), 600);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = original;
    if (status) status.textContent = "";
    let msg = genErrorMessage(e);
    if (/popup|blocked|window/i.test(msg)) {
      msg += " — permita popups neste site para o login do Puter.";
    }
    alert("Falha ao gerar imagem: " + msg);
  }
}

// ------------------------------------------------------------------
// Kaggle
// ------------------------------------------------------------------
let _kagglePolling = null;

const KAGGLE_LABELS = {
  queued: "na fila...",
  uploading: "enviando...",
  running: "renderizando...",
  complete: "pronto",
  error: "erro",
  cancelacknowledged: "cancelado",
  none: "-",
};

function renderKaggleState(data) {
  const txt = document.getElementById("kaggle-status-text");
  const dot = document.getElementById("kaggle-dot");
  const link = document.getElementById("kaggle-link");
  const dlMaster = document.getElementById("kaggle-master");
  const dlBase = document.getElementById("kaggle-base");
  const hfState = document.getElementById("hyperframes-state");
  const status = (data.status || "").toLowerCase();
  let label = KAGGLE_LABELS[status] || status || "verificando...";
  if (status === "error" && data.error) label = "erro: " + data.error.slice(0, 200);
  if (txt) txt.textContent = label;

  if (dot) {
    dot.className = "tally";
    if (status === "complete")      dot.classList.add("tally-ok");
    else if (status === "error")    dot.classList.add("tally-err");
    else if (status === "running")  dot.classList.add("tally-rec");
    else                            dot.classList.add("tally-warn");
  }

  if (link && data.url) link.href = data.url;
  if (dlMaster) {
    const url = data.master_video_url || "";
    if (url) dlMaster.href = url;
    dlMaster.style.display = url ? "" : "none";
  }
  if (dlBase) {
    const url = data.base_video_url || "";
    if (url) dlBase.href = url;
    dlBase.style.display = url ? "" : "none";
  }
  if (hfState && data.hyperframes) {
    const hf = data.hyperframes;
    const extras = [];
    if (hf.audio) extras.push("com narração");
    if (hf.avatar) extras.push("com avatar");
    if (hf.status === "complete") {
      hfState.textContent = "master pronto (" + (hf.render_mode || "mp4") + (extras.length ? ", " + extras.join(", ") : "") + ")";
    } else if (hf.status === "fallback_complete") {
      hfState.textContent = "master via fallback FFmpeg" + (extras.length ? " (" + extras.join(", ") + ")" : "") + " — HyperFrames falhou";
    } else if (hf.status === "error") {
      hfState.textContent = "erro no refino — base preservada: " + String(hf.error || "").slice(0, 160);
    }
  }
  if (data.validation) renderValidation(data.validation);
}

async function sendToKaggle(projectId) {
  const btn = document.getElementById("btn-kaggle");
  const bar = document.getElementById("kaggle-status-bar");
  btn.disabled = true;
  const nameSpan = btn.querySelector(".step-name");
  if (nameSpan) nameSpan.textContent = "Enviando...";
  bar.style.display = "";
  renderKaggleState({ status: "queued" });
  document.getElementById("kaggle-status-text").textContent = "enviando ZIP...";
  try {
    const res = await postForm(`/projects/${projectId}/send-to-kaggle`, {});
    if (nameSpan) nameSpan.textContent = "Render Kaggle";
    renderKaggleState({ status: res.status, url: res.kernel_url });
    if (res.job_id) {
      startJobPolling(res.job_id, projectId, btn);
    } else {
      startKagglePolling(projectId);
    }
  } catch (e) {
    renderKaggleState({ status: "error", error: e.message });
    document.getElementById("kaggle-status-text").textContent = "erro: " + e.message;
    btn.disabled = false;
    if (nameSpan) nameSpan.textContent = "Render Kaggle";
  }
}

function renderJob(job) {
  const txt = document.getElementById("kaggle-status-text");
  if (!txt || !job) return;
  const msg = job.message || job.error || job.status || "job";
  txt.textContent = `${job.kind}: ${job.status} - ${msg}`;
}

function startJobPolling(jobId, projectId, btn) {
  let failures = 0;
  const tick = async () => {
    try {
      const res = await fetch(`/jobs/${jobId}`);
      const job = await res.json();
      failures = 0;
      renderJob(job);
      if (job.status === "complete") {
        const kernelUrl = job.result && job.result.kernel_url;
        renderKaggleState({ status: "queued", url: kernelUrl });
        startKagglePolling(projectId);
        return;
      }
      if (job.status === "error") {
        renderKaggleState({ status: "error", error: job.error || job.message || "job falhou" });
        if (btn) btn.disabled = false;
        return;
      }
      setTimeout(tick, 2500);
    } catch (e) {
      failures += 1;
      if (failures < 4) {
        setTimeout(tick, 4000);
        return;
      }
      renderKaggleState({ status: "error", error: e.message });
      if (btn) btn.disabled = false;
    }
  };
  tick();
}

function startKagglePolling(projectId) {
  if (_kagglePolling) clearInterval(_kagglePolling);
  const tick = async () => {
    try {
      const res = await fetch(`/projects/${projectId}/kaggle-status`);
      const data = await res.json();
      renderKaggleState(data);
      if (["complete", "error", "cancelacknowledged"].includes((data.status || "").toLowerCase())) {
        clearInterval(_kagglePolling);
        _kagglePolling = null;
      }
    } catch (_) {}
  };
  tick();
  _kagglePolling = setInterval(tick, 20000);
}

function renderValidation(data) {
  const status = document.getElementById("validation-status");
  const detail = document.getElementById("validation-detail");
  if (!status || !detail || !data) return;
  status.classList.remove("ok-text", "warn-text", "bad-text", "muted");
  if (data.status === "ok") status.classList.add("ok-text");
  else if (data.status === "error") status.classList.add("bad-text");
  else status.classList.add("warn-text");
  status.textContent = data.status || "pendente";
  const issue = data.issues && data.issues.length ? data.issues[0].message : "outputs coerentes";
  detail.textContent = issue;
}

async function validateOutput(projectId, btn) {
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "validando..."; }
  try {
    const data = await postForm(`/projects/${projectId}/validate-output`, {});
    renderValidation(data);
  } catch (e) {
    alert("Falha na validacao: " + e.message);
  } finally {
    if (btn) { btn.textContent = old; btn.disabled = false; }
  }
}

// Inicia polling se já tinha kernel rodando ao carregar a página.
document.addEventListener("DOMContentLoaded", () => {
  const bar = document.getElementById("kaggle-status-bar");
  const txt = document.getElementById("kaggle-status-text");
  if (bar && txt && bar.style.display !== "none") {
    const pid = window.location.pathname.split("/").pop();
    const status = txt.textContent.trim().toLowerCase();
    if (status && !["complete", "pronto", "none", "-"].includes(status)) {
      startKagglePolling(pid);
    }
  }
  startProjectJobRefresh();
});

function startProjectJobRefresh() {
  const projectId = window.NWRCH_PROJECT_ID;
  if (!projectId) return;
  const statusBadge = document.querySelector(".head-status .badge");
  const current = statusBadge ? statusBadge.textContent.trim().toLowerCase() : "";
  if (!["mapping", "searching", "packaging"].includes(current)) return;
  let ticks = 0;
  const poll = async () => {
    ticks += 1;
    try {
      const res = await fetch(`/projects/${projectId}/jobs`);
      const data = await res.json();
      const active = (data.jobs || []).some((job) => ["queued", "running"].includes(job.status));
      if (!active || ticks > 240) location.reload();
      else setTimeout(poll, 2500);
    } catch (_) {
      if (ticks < 8) setTimeout(poll, 4000);
    }
  };
  setTimeout(poll, 1200);
}

async function regenKeywords(sceneId) {
  // hidden span armazena os keywords atuais para restaurar em caso de erro
  const span = document.getElementById("kw-" + sceneId);
  const old = span ? span.textContent : "";
  // atualiza visualmente as chips de keyword da cena
  const scene = document.getElementById("scene-" + sceneId);
  const metaKws = scene ? scene.querySelectorAll(".scene-meta .kw") : [];
  metaKws.forEach(el => el.textContent = "gerando...");
  try {
    const res = await postForm(`/scenes/${sceneId}/regen-keywords`, {});
    if (span) span.textContent = res.keywords.join(", ");
    // rebuild keyword chips
    const meta = scene ? scene.querySelector(".scene-meta") : null;
    if (meta) {
      const existing = meta.querySelectorAll(".kw");
      existing.forEach(el => el.remove());
      const tally = meta.querySelector(".tally");
      res.keywords.forEach(kw => {
        const chip = document.createElement("span");
        chip.className = "kw";
        chip.textContent = kw;
        meta.insertBefore(chip, tally || null);
      });
    }
  } catch (e) {
    metaKws.forEach((el, i) => { el.textContent = old.split(", ")[i] || el.textContent; });
    alert("Falha ao gerar keywords: " + e.message);
  }
}
