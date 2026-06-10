// Galeria interativa do NWRCH Studio.

async function postForm(url, data) {
  const body = new URLSearchParams(data || {});
  const resp = await fetch(url, { method: "POST", body });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { const j = await resp.json(); msg = j.detail || j.error || msg; }
    catch (_) { try { msg = (await resp.text()).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 200); } catch (_) {} }
    throw new Error(msg);
  }
  return resp.json();
}

async function setState(assetId, state) {
  const card = document.getElementById("asset-" + assetId);
  try {
    const res = await postForm(`/assets/${assetId}/state`, { state });
    // limpa classes de estado e aplica a nova
    card.classList.remove("state-pending", "state-selected", "state-rejected", "state-favorite");
    card.classList.add("state-" + res.state);
    const selectButton = card.querySelector(".sel");
    if (selectButton) selectButton.textContent = res.state === "selected" ? "Selecionado" : "Selecionar";

    if (res.state === "selected") {
      // uma cena tem 1 selecionado: rebaixa visualmente os irmaos
      const grid = card.closest(".grid");
      grid.querySelectorAll(".acard.state-selected").forEach((el) => {
        if (el !== card) {
          el.classList.remove("state-selected");
          el.classList.add("state-pending");
          const siblingButton = el.querySelector(".sel");
          if (siblingButton) siblingButton.textContent = "Selecionar";
        }
      });
      updateSelectedCount();
    } else {
      updateSelectedCount();
    }
  } catch (e) {
    alert("Falha ao atualizar: " + e.message);
  }
}

function updateSelectedCount() {
  const btn = document.querySelector("button.accent");
  if (!btn) return;
  const total = document.querySelectorAll("section.scene").length;
  // conta cenas que tem ao menos um selecionado
  let selected = 0;
  document.querySelectorAll("section.scene").forEach((sec) => {
    if (sec.querySelector(".acard.state-selected")) selected++;
  });
  btn.innerHTML = `<span>03</span> Preparar pacote (${selected}/${total})`;
  btn.disabled = total === 0 || selected !== total;
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
    dot.className = "kaggle-dot";
    if (status === "complete") dot.classList.add("ok");
    else if (status === "error") dot.classList.add("err");
    else if (status === "running") dot.classList.add("run");
    else dot.classList.add("wait");
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
  btn.textContent = "Enviando...";
  bar.style.display = "";
  renderKaggleState({ status: "queued" });
  document.getElementById("kaggle-status-text").textContent = "enviando ZIP...";
  try {
    const res = await postForm(`/projects/${projectId}/send-to-kaggle`, {});
    btn.textContent = "04 Renderizar no Kaggle";
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
    btn.textContent = "04 Renderizar no Kaggle";
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
      // falha transitoria de rede nao deve matar o polling de um job em andamento
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
  if (btn) {
    btn.disabled = true;
    btn.textContent = "validando...";
  }
  try {
    const data = await postForm(`/projects/${projectId}/validate-output`, {});
    renderValidation(data);
  } catch (e) {
    alert("Falha na validacao: " + e.message);
  } finally {
    if (btn) {
      btn.textContent = old;
      btn.disabled = false;
    }
  }
}

// Inicia polling se ja tinha kernel rodando.
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
});

async function regenKeywords(sceneId) {
  const span = document.getElementById("kw-" + sceneId);
  const old = span.textContent;
  span.textContent = "gerando...";
  try {
    const res = await postForm(`/scenes/${sceneId}/regen-keywords`, {});
    span.textContent = res.keywords.join(", ");
  } catch (e) {
    span.textContent = old;
    alert("Falha ao gerar keywords: " + e.message);
  }
}
