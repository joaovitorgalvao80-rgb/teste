function projectIdFromPage() {
  const pageHead = document.querySelector("[data-project-id]");
  return pageHead ? pageHead.dataset.projectId : "";
}

document.addEventListener("DOMContentLoaded", () => {
  const pkgForm = document.querySelector('form[action$="/package"]');
  if (!pkgForm) return;
  pkgForm.addEventListener("submit", async function qualityGuard(event) {
    if (pkgForm.dataset.qualityOk) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    let proceed = true;
    try {
      const data = await getJSON(`/projects/${projectIdFromPage()}/quality-warnings`);
      if (data.warnings && data.warnings.length > 0) {
        const lines = data.warnings
          .map((warning) => `- ${warning.scene_id}: ${warning.issues.join(", ")}`)
          .join("\n");
        const plural = data.total === 1 ? "" : "s";
        proceed = confirm(
          `Alertas de qualidade (${data.total} cena${plural}):\n\n${lines}\n\nGerar o pacote mesmo assim?`,
        );
      }
    } catch (error) {
      console.warn("Falha ao checar qualidade antes do pacote.", error);
      proceed = true;
    }
    if (proceed) {
      pkgForm.dataset.qualityOk = "1";
      pkgForm.requestSubmit();
    }
  }, true);
});

function editKeywords(sceneId, btn) {
  const panel = document.getElementById("kw-edit-" + sceneId);
  if (panel) panel.style.display = "";
  if (btn) btn.style.display = "none";
}

function cancelEditKeywords(sceneId) {
  const panel = document.getElementById("kw-edit-" + sceneId);
  if (panel) panel.style.display = "none";
  const btn = document.getElementById("btn-edit-kw-" + sceneId);
  if (btn) btn.style.display = "";
}

async function saveKeywords(sceneId) {
  const input = document.getElementById("kw-input-" + sceneId);
  const saveBtn = document.getElementById("kw-save-" + sceneId);
  if (!input) return;
  const keywords = input.value.trim();
  if (!keywords) {
    notify("Informe ao menos uma keyword.", "info");
    return;
  }
  if (saveBtn) saveBtn.disabled = true;
  try {
    const res = await postForm(`/scenes/${sceneId}/set-keywords`, { keywords });
    const scene = document.getElementById("scene-" + sceneId);
    const meta = scene ? scene.querySelector(".scene-meta") : null;
    if (meta) {
      meta.querySelectorAll(".kw").forEach((el) => el.remove());
      const tally = meta.querySelector(".tally");
      (res.keywords || []).forEach((kw) => {
        const chip = document.createElement("span");
        chip.className = "kw";
        chip.textContent = kw;
        meta.insertBefore(chip, tally || null);
      });
    }
    input.value = (res.keywords || []).join(", ");
    cancelEditKeywords(sceneId);
    notify("Keywords salvas.", "ok");
  } catch (error) {
    notify("Falha ao salvar: " + error.message, "error");
  }
  if (saveBtn) saveBtn.disabled = false;
}

globalThis.editKeywords = editKeywords;
globalThis.cancelEditKeywords = cancelEditKeywords;
globalThis.saveKeywords = saveKeywords;
