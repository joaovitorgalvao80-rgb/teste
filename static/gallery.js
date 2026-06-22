// Galeria interativa do NWRCH Studio.

// ------------------------------------------------------------------
// Toasts (substituem alert(): nao bloqueiam e somem sozinhos)
// ------------------------------------------------------------------
function notify(message, type) {
  let host = document.getElementById("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    document.body.appendChild(host);
  }
  const toast = document.createElement("div");
  toast.className = "toast toast-" + (type || "info");
  toast.textContent = message;
  host.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("is-visible"));
  setTimeout(() => {
    toast.classList.remove("is-visible");
    setTimeout(() => toast.remove(), 400);
  }, 5000);
}

function ignoredParseError(context, error) {
  console.debug(context, error);
}

async function getJSON(url) {
  const res = await fetch(url, { headers: { accept: "application/json" } });
  if (res.status === 401) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    throw new Error("sessao expirada");
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.detail || j.error || msg; } catch (error) { ignoredParseError("Resposta nao-JSON em getJSON", error); }
    throw new Error(msg);
  }
  return res.json();
}

async function postForm(url, data) {
  const body = new URLSearchParams(data || {});
  const token = csrfToken();
  if (token && !body.has("csrf_token")) body.set("csrf_token", token);
  const headers = { accept: "application/json" };
  if (token) headers["x-csrf-token"] = token;
  const resp = await fetch(url, { method: "POST", body, headers });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { const j = await resp.json(); msg = j.detail || j.error || msg; }
    catch (jsonError) {
      ignoredParseError("Resposta nao-JSON em postForm", jsonError);
      try { msg = (await resp.text()).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 200); } catch (textError) { ignoredParseError("Corpo ilegivel em postForm", textError); }
    }
    throw new Error(msg);
  }
  return resp.json();
}

function currentProjectId() {
  if (globalThis.NWRCH_PROJECT_ID) return String(globalThis.NWRCH_PROJECT_ID);
  const pageHead = document.querySelector("[data-project-id]");
  if (pageHead?.dataset.projectId) return pageHead.dataset.projectId;
  const match = globalThis.location.pathname.match(/^\/projects\/(\d+)/);
  return match ? match[1] : "";
}

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.content : "";
}

async function setState(assetId, state) {
  const card = document.getElementById("asset-" + assetId);
  try {
    const res = await postForm(`/assets/${assetId}/state`, { state });
    card.classList.remove("is-pending", "is-selected", "is-rejected", "is-favorite", "is-accepted");
    card.classList.add("is-" + res.state);
    const selectButton = card.querySelector(".sel");
    if (selectButton) {
      selectButton.textContent = ["selected", "accepted"].includes(res.state) ? "Selecionado" : "Selecionar";
    }

    if (["selected", "accepted"].includes(res.state)) {
      // rebaixa irmãos da mesma cena
      const grid = card.closest(".takes");
      if (grid) {
        grid.querySelectorAll(".take.is-selected, .take.is-accepted").forEach((el) => {
          if (el !== card) {
            el.classList.remove("is-selected", "is-accepted");
            el.classList.add("is-pending");
            const siblingButton = el.querySelector(".sel");
            if (siblingButton) siblingButton.textContent = "Selecionar";
          }
        });
      }
    }
    updateSelectedCount();
  } catch (e) {
    notify("Falha ao atualizar: " + e.message, "error");
  }
}

function updateSelectedCount() {
  const btn = document.getElementById("btn-package");
  if (!btn) return;
  const scenes = document.querySelectorAll('section.scene[data-broll-required="1"]');
  const total = scenes.length;
  let selected = 0;
  scenes.forEach((sec) => {
    if (sec.querySelector(".take.is-selected, .take.is-accepted")) selected++;
  });
  const statusBadge = document.querySelector(".head-status .badge");
  const status = statusBadge ? (statusBadge.dataset.status || statusBadge.textContent).trim().toLowerCase() : "";
  const busy = BUSY_PROJECT_STATUSES.has(status);
  const requiresComplete = btn.dataset.missingPolicy === "block_package";
  // atualiza o texto do step no pipeline
  const nameSpan = btn.querySelector(".step-name");
  if (nameSpan) nameSpan.textContent = `Pacote (${selected}/${total})`;
  btn.disabled = busy || selected === 0 || (requiresComplete && selected < total);
}

async function searchMore(sceneId, btn, media) {
  const original = btn.textContent;
  btn.textContent = "buscando...";
  btn.disabled = true;
  // caixa de keyword propria da cena: quando preenchida, manda a busca
  const kwInput = document.getElementById("kw-search-" + sceneId);
  const keyword = kwInput ? kwInput.value.trim() : "";
  try {
    const res = await postForm(`/scenes/${sceneId}/search-more`, { media: media || "all", keyword });
    btn.textContent = `+${res.added} novos`;
    if (res.added > 0) {
      notify(`+${res.added} takes adicionados — recarregando...`, "ok");
      setTimeout(() => location.reload(), 600);
    } else {
      notify("Nenhum take novo encontrado. Tente outras keywords.", "info");
      setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1200);
    }
  } catch (e) {
    btn.textContent = original;
    btn.disabled = false;
    notify("Falha na busca: " + e.message, "error");
  }
}

async function refreshAssets(sceneId, btn, media) {
  const original = btn.textContent;
  btn.textContent = "atualizando...";
  btn.disabled = true;
  const kwInput = document.getElementById("kw-search-" + sceneId);
  const keyword = kwInput ? kwInput.value.trim() : "";
  try {
    const res = await postForm(`/scenes/${sceneId}/refresh-assets`, { media: media || "all", keyword });
    if (res.added > 0) {
      notify(`${res.removed || 0} antigos removidos, ${res.added} novos takes - recarregando...`, "ok");
      setTimeout(() => location.reload(), 600);
    } else {
      notify("Nenhum resultado novo encontrado. Mantive os takes atuais.", "info");
      setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1200);
    }
  } catch (e) {
    btn.textContent = original;
    btn.disabled = false;
    notify("Falha ao atualizar: " + e.message, "error");
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
    script.integrity = "sha384-1cmgi3dLV4Dnuu9dMg1aR/7qlFou128Y3bxXvqFjnbRuv3+QrWQ6auJXdG5rMTRW";
    script.crossOrigin = "anonymous";
    script.src = "https://js.puter.com/v2/";
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Puter.js não carregou"));
    document.head.appendChild(script);
  });
  return _puterLoadPromise;
}

// O Puter passou a exigir `model` no txt2img ("Missing `model`").
// Tenta uma lista de modelos conhecidos até um funcionar.
const PUTER_IMAGE_MODELS = ["gpt-image-1", "dall-e-3", "gemini-2.5-flash-image-preview"];

async function puterTxt2Img(prompt, status) {
  let lastErr = null;
  for (const model of PUTER_IMAGE_MODELS) {
    try {
      return await withTimeout(
        puter.ai.txt2img(prompt, { model }),
        GEN_TIMEOUT_MS,
        "geração demorou demais — tente de novo"
      );
    } catch (e) {
      lastErr = e;
      const msg = genErrorMessage(e);
      // só tenta o próximo modelo em erro de modelo; outros erros param aqui
      if (!/model|not (found|supported|available)|unsupported|invalid/i.test(msg)) throw e;
      if (status) status.textContent = "modelo " + model + " indisponível — tentando outro...";
    }
  }
  // último recurso: assinatura antiga sem opções
  try {
    return await withTimeout(puter.ai.txt2img(prompt), GEN_TIMEOUT_MS, "geração demorou demais — tente de novo");
  } catch (e) {
    throw lastErr || e;
  }
}

function genErrorMessage(e) {
  // Puter pode rejeitar com Error, string ou objeto {error:{message}} / {message}
  if (!e) return "erro desconhecido";
  if (typeof e === "string") return e;
  if (e.message) return e.message;
  if (e.error?.message) return e.error.message;
  try { return JSON.stringify(e).slice(0, 200); } catch (error) { ignoredParseError("Erro nao serializavel", error); return String(e); }
}

async function generatePuterBlob(prompt, status) {
  await ensurePuterLoaded();
  if (typeof puter === "undefined" || !puter.ai || typeof puter.ai.txt2img !== "function") {
    throw new Error("Puter.js não inicializou");
  }
  if (status) status.textContent = "gerando imagem (pode levar até 1 min; na primeira vez o Puter pede login)...";
  const img = await puterTxt2Img(prompt, status);
  const src = img?.src;
  if (!src) throw new Error("o gerador não retornou imagem");
  // garante naturalWidth/Height preenchidos antes de ler
  if (img.decode) { try { await img.decode(); } catch (error) { ignoredParseError("Decode best-effort falhou", error); } }
  const blob = await (await fetch(src)).blob();
  if (!blob || blob.size === 0) throw new Error("imagem retornou vazia");
  if (blob.size > GEN_MAX_BYTES) throw new Error("imagem gerada grande demais (>15 MB)");
  return { img, blob };
}

async function saveGeneratedImage(sceneId, blob, prompt, img) {
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
    try { const j = await resp.json(); msg = j.detail || j.error || msg; } catch (error) { ignoredParseError("Resposta nao-JSON ao salvar imagem", error); }
    throw new Error(msg);
  }
}

async function generateImage(sceneId, btn) {
  const promptEl = document.getElementById("gen-prompt-" + sceneId);
  const status = document.getElementById("gen-status-" + sceneId);
  const prompt = (promptEl ? promptEl.value : "").trim();
  if (!prompt) {
    notify("Escreva um prompt antes de gerar.", "info");
    return;
  }
  if (btn.disabled) return; // protege contra clique duplo
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "gerando...";
  if (status) status.textContent = "carregando gerador externo...";
  try {
    const { img, blob } = await generatePuterBlob(prompt, status);
    if (status) status.textContent = "salvando no projeto...";
    await saveGeneratedImage(sceneId, blob, prompt, img);
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
    notify("Falha ao gerar imagem: " + msg, "error");
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

function renderKaggleDot(dot, status) {
  dot.className = "tally";
  if (status === "complete")      dot.classList.add("tally-ok");
  else if (status === "error")    dot.classList.add("tally-err");
  else if (status === "running")  dot.classList.add("tally-rec");
  else                            dot.classList.add("tally-warn");
}

function setDownloadLink(el, url) {
  if (!el) return;
  if (url) el.href = url;
  el.style.display = url ? "" : "none";
}

function renderHyperframes(hfState, hf) {
  const extras = [];
  if (hf.audio) extras.push("com narração");
  if (hf.avatar) extras.push("com avatar");
  if (hf.requested_avatar && !hf.avatar) {
    hfState.textContent = "erro no master - avatar solicitado e ausente";
  } else if (hf.status === "complete") {
    hfState.textContent = "master pronto (" + (hf.render_mode || "mp4") + (extras.length ? ", " + extras.join(", ") : "") + ")";
  } else if (hf.status === "fallback_complete") {
    hfState.textContent = "master via fallback FFmpeg" + (extras.length ? " (" + extras.join(", ") + ")" : "") + " — HyperFrames falhou";
  } else if (hf.status === "error") {
    hfState.textContent = "erro no refino — base preservada: " + String(hf.error || "").slice(0, 160);
  }
}

function renderKaggleState(data) {
  const txt = document.getElementById("kaggle-status-text");
  const dot = document.getElementById("kaggle-dot");
  const link = document.getElementById("kaggle-link");
  const hfState = document.getElementById("hyperframes-state");
  const status = (data.status || "").toLowerCase();
  let label = KAGGLE_LABELS[status] || status || "verificando...";
  if (status === "error" && data.error) label = "erro: " + data.error.slice(0, 200);
  if (txt) txt.textContent = label;
  if (dot) renderKaggleDot(dot, status);
  if (link && data.url) link.href = data.url;
  setDownloadLink(document.getElementById("kaggle-master"), data.master_video_url || "");
  setDownloadLink(document.getElementById("kaggle-base"), data.base_video_url || "");
  if (hfState && data.hyperframes) renderHyperframes(hfState, data.hyperframes);
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
  const msg = job.status === "error"
    ? (job.error || job.message || job.status || "job")
    : (job.message || job.error || job.status || "job");
  txt.textContent = `${job.kind}: ${job.status} - ${msg}`;
}

const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "canceling"]);

function jobText(job) {
  if (job?.status === "error") {
    return job.error || job.message || "-";
  }
  return job?.message || job?.error || "-";
}

function jobMetaText(job) {
  const elapsed = job.elapsed_label || "";
  const updated = job.updated_label ? "atualizado " + job.updated_label : "";
  return [elapsed, updated].filter(Boolean).join(" - ");
}

function renderProjectJobs(jobs) {
  const list = document.getElementById("job-list");
  if (!list) return;
  list.innerHTML = "";
  (jobs || []).forEach((job) => {
    const item = document.createElement("li");
    item.className = "job-" + job.status;
    item.dataset.jobId = String(job.id);

    const kind = document.createElement("b");
    kind.textContent = job.kind || "";
    const status = document.createElement("span");
    status.textContent = job.status || "";
    const message = document.createElement("small");
    const messageText = document.createElement("span");
    messageText.className = "job-message";
    messageText.textContent = jobText(job);
    const meta = document.createElement("span");
    meta.className = "job-meta";
    meta.textContent = jobMetaText(job);
    message.append(messageText, meta);
    item.append(kind, status, message);

    if (ACTIVE_JOB_STATUSES.has(job.status)) {
      const stop = document.createElement("button");
      stop.className = "btn btn-danger btn-sm";
      stop.type = "button";
      stop.textContent = job.status === "canceling" ? "Parando..." : "Parar";
      stop.disabled = job.status === "canceling";
      stop.addEventListener("click", () => cancelJob(job.id, stop));
      item.appendChild(stop);
    } else {
      item.appendChild(document.createElement("i"));
    }
    list.appendChild(item);
  });
}

async function refreshProjectJobs(projectId) {
  const data = await getJSON(`/projects/${projectId}/jobs`);
  renderProjectJobs(data.jobs || []);
  return data;
}

async function cancelJob(jobId, btn) {
  if (!confirm("Parar esta tarefa?")) return;
  const old = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Parando...";
  }
  try {
    const job = await postForm(`/jobs/${jobId}/cancel`, {});
    const projectId = currentProjectId();
    if (projectId) {
      await refreshProjectJobs(projectId);
      startProjectJobRefresh(true);
    }
    notify((job.message || "Cancelamento solicitado") + ".", "info");
  } catch (e) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = old || "Parar";
    }
    notify("Falha ao parar tarefa: " + e.message, "error");
  }
}

function startJobPolling(jobId, projectId, btn) {
  let failures = 0;
  const tick = async () => {
    try {
      const job = await getJSON(`/jobs/${jobId}`);
      failures = 0;
      renderJob(job);
      if (job.status === "complete") {
        const kernelUrl = job.result?.kernel_url;
        renderKaggleState({ status: "queued", url: kernelUrl });
        startKagglePolling(projectId);
        return;
      }
      if (job.status === "canceled") {
        renderKaggleState({ status: "cancelacknowledged" });
        if (btn) btn.disabled = false;
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
  let failures = 0;
  const tick = async () => {
    try {
      const data = await getJSON(`/projects/${projectId}/kaggle-status`);
      failures = 0;
      renderKaggleState(data);
      if (["complete", "error", "cancelacknowledged"].includes((data.status || "").toLowerCase())) {
        clearInterval(_kagglePolling);
        _kagglePolling = null;
      }
    } catch (e) {
      failures += 1;
      if (failures >= 5) {
        renderKaggleState({ status: "error", error: "monitor falhou: " + e.message });
        clearInterval(_kagglePolling);
        _kagglePolling = null;
      }
    }
  };
  tick();
  _kagglePolling = setInterval(tick, 20000);
}

// ------------------------------------------------------------------
// Video longo: render por partes + concatenacao
// ------------------------------------------------------------------
let _partsPolling = null;

async function renderParts(projectId, btn) {
  btn.disabled = true;
  const name = btn.querySelector(".step-name");
  if (name) name.textContent = "Enviando...";
  try {
    const res = await postForm(`/projects/${projectId}/render-parts`, {});
    notify(res.message + ` (${res.parts} parte(s)). Mantenha o servidor aberto.`, "ok");
    if (name) name.textContent = "Renderizando...";
    startPartsPolling(projectId);
  } catch (e) {
    btn.disabled = false;
    if (name) name.textContent = "Render partes";
    notify("Falha ao iniciar render: " + e.message, "error");
  }
}

async function concatParts(projectId, btn) {
  btn.disabled = true;
  const name = btn.querySelector(".step-name");
  if (name) name.textContent = "Concatenando...";
  try {
    const res = await postForm(`/projects/${projectId}/concat-parts`, {});
    notify(res.message, "ok");
    startPartsPolling(projectId);
  } catch (e) {
    btn.disabled = false;
    if (name) name.textContent = "Concatenar final";
    notify("Falha na concatenacao: " + e.message, "error");
  }
}

function renderPartsTable(data) {
  (data.parts || []).forEach((p) => {
    const row = document.querySelector(`#parts-table tr[data-part="${p.part_idx}"]`);
    if (!row) return;
    const statusCell = row.querySelector(".part-status");
    if (statusCell) {
      statusCell.className = "part-status part-status-" + p.status;
      statusCell.textContent = p.status + (p.error ? " — " + p.error.slice(0, 80) : "");
    }
    const kernelCell = row.querySelector(".part-kernel");
    if (kernelCell && p.kernel_slug && data.kaggle_username) {
      kernelCell.innerHTML =
        `<a href="https://www.kaggle.com/code/${data.kaggle_username}/${p.kernel_slug}" target="_blank" rel="noopener">abrir ↗</a>`;
    }
  });
  const msg = document.getElementById("parts-job-msg");
  if (msg) {
    msg.textContent = data.active_job
      ? (data.active_job.message || "processando...") + " — mantenha o servidor aberto"
      : "render sequencial; o servidor precisa ficar aberto";
  }
  const dlBase = document.getElementById("parts-dl-base");
  if (dlBase) dlBase.style.display = data.has_base_video ? "" : "none";
  const dlMaster = document.getElementById("parts-dl-master");
  if (dlMaster) dlMaster.style.display = data.has_master_video ? "" : "none";
}

function startPartsPolling(projectId) {
  if (_partsPolling) clearInterval(_partsPolling);
  let failures = 0;
  let hadJob = false;
  const tick = async () => {
    try {
      const data = await getJSON(`/projects/${projectId}/parts-status`);
      failures = 0;
      renderPartsTable(data);
      if (data.active_job) {
        hadJob = true;
      } else if (hadJob) {
        clearInterval(_partsPolling);
        _partsPolling = null;
        notify("Processamento das partes terminou — recarregando...", "ok");
        setTimeout(() => location.reload(), 800);
      }
    } catch (e) {
      failures += 1;
      if (failures >= 5) {
        clearInterval(_partsPolling);
        _partsPolling = null;
        notify("Perdi contato com o monitor de partes: " + e.message, "error");
      }
    }
  };
  tick();
  _partsPolling = setInterval(tick, 15000);
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
  const issue = data.issues?.length ? data.issues[0].message : "outputs coerentes";
  detail.textContent = issue;
}

async function validateOutput(projectId, btn) {
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "validando..."; }
  try {
    const data = await postForm(`/projects/${projectId}/validate-output`, {});
    renderValidation(data);
  } catch (e) {
    notify("Falha na validação: " + e.message, "error");
  } finally {
    if (btn) { btn.textContent = old; btn.disabled = false; }
  }
}

// Evita duplo submit nos forms do pipeline (data-busy-submit).
function initBusySubmitForms() {
  document.querySelectorAll("form[data-busy-submit]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (form.dataset.busy) { e.preventDefault(); return; }
      form.dataset.busy = "1";
      const btn = form.querySelector('button[type="submit"]');
      if (btn) {
        btn.classList.add("is-busy");
        const name = btn.querySelector(".step-name");
        if (name) name.textContent = "Processando...";
        // desabilitar sincronamente cancelaria o submit em alguns browsers
        setTimeout(() => { btn.disabled = true; }, 0);
      }
    });
  });
}

// Inicia polling se já tinha kernel rodando ao carregar a página.
document.addEventListener("DOMContentLoaded", () => {
  initBusySubmitForms();
  const partsPanel = document.getElementById("parts-panel");
  const projectId = currentProjectId();
  if (partsPanel?.dataset.active === "1" && projectId) {
    startPartsPolling(projectId);
  }
  const bar = document.getElementById("kaggle-status-bar");
  const txt = document.getElementById("kaggle-status-text");
  if (bar && txt && bar.style.display !== "none") {
    const pid = globalThis.location.pathname.split("/").pop();
    const status = txt.textContent.trim().toLowerCase();
    if (status && !["complete", "pronto", "none", "-"].includes(status)) {
      startKagglePolling(pid);
    }
  }
  const hasActiveJob = document.querySelector(".job-queued, .job-running, .job-canceling");
  startProjectJobRefresh(Boolean(hasActiveJob));
});

const BUSY_PROJECT_STATUSES = new Set(["mapping", "searching", "packaging", "auto_selecting", "researching"]);
let _projectJobRefreshActive = false;

function startProjectJobRefresh(force) {
  const projectId = currentProjectId();
  if (!projectId) return;
  if (_projectJobRefreshActive) return;
  const statusBadge = document.querySelector(".head-status .badge");
  const current = statusBadge ? (statusBadge.dataset.status || statusBadge.textContent).trim().toLowerCase() : "";
  if (!force && !BUSY_PROJECT_STATUSES.has(current)) return;
  _projectJobRefreshActive = true;
  let ticks = 0;
  let failures = 0;
  const poll = async () => {
    ticks += 1;
    try {
      const data = await refreshProjectJobs(projectId);
      failures = 0;
      const status = (data.project_status || "").toLowerCase();
      const activeJob = (data.jobs || []).find((job) => ACTIVE_JOB_STATUSES.has(job.status));
      if (statusBadge && activeJob?.message) {
        statusBadge.textContent = activeJob.message;
      }
      const busy = BUSY_PROJECT_STATUSES.has(status);
      const hasActiveJob = (data.jobs || []).some((job) => ACTIVE_JOB_STATUSES.has(job.status));
      if ((!busy && !hasActiveJob) || ticks > 240) location.reload();
      else setTimeout(poll, 2500);
    } catch (e) {
      failures += 1;
      if (failures < 5) setTimeout(poll, 4000);
      else {
        _projectJobRefreshActive = false;
        notify("Perdi contato com o servidor durante o processamento: " + e.message, "error");
      }
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
    notify("Falha ao gerar keywords: " + e.message, "error");
  }
}

// ------------------------------------------------------------------
// Override manual de avatar/b-roll por cena
// ------------------------------------------------------------------
const AVATAR_OVERRIDE_LABELS = {
  no_avatar: "cena marcada como SEM avatar (b-roll em tela cheia)",
  no_broll: "cena marcada como SEM b-roll (só o avatar)",
  auto: "cena voltou para a decisão automática",
};

async function setAvatarOverride(sceneId, mode, btn) {
  const group = document.getElementById("avatar-override-" + sceneId);
  if (btn?.classList.contains("is-active")) return; // ja esta nesse modo
  try {
    const res = await postForm(`/scenes/${sceneId}/avatar-override`, { mode });
    if (group) {
      group.dataset.mode = String(res.broll_override);
      group.querySelectorAll(".btn").forEach((b) => b.classList.remove("is-active"));
      if (btn) btn.classList.add("is-active");
    }
    // a decisao muda quais cenas exigem take e como o render compoe; recarrega
    // para a galeria refletir o estado real calculado no servidor.
    notify((AVATAR_OVERRIDE_LABELS[mode] || "preferência salva") + " — recarregando...", "ok");
    globalThis.location.hash = "scene-" + sceneId;
    setTimeout(() => location.reload(), 600);
  } catch (e) {
    notify("Falha ao mudar o avatar da cena: " + e.message, "error");
  }
}
