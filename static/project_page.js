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
      const blockers = Array.isArray(data.blockers) ? data.blockers : [];
      const warnings = Array.isArray(data.warnings) ? data.warnings : [];
      const problemScenes = Array.isArray(data.problem_scenes) ? data.problem_scenes : [];
      const formatIssue = (item) => {
        const label = item.scene_id || "projeto";
        const issues = item.issues || item.reasons || [];
        return `- ${label}: ${issues.join(", ")}`;
      };
      if (blockers.length > 0 || data.status === "blocked") {
        const lines = blockers.slice(0, 10).map(formatIssue).join("\n");
        alert(`Preflight bloqueou o pacote:\n\n${lines}\n\nRevise essas cenas antes de gerar o ZIP.`);
        proceed = false;
      } else if (warnings.length > 0 || problemScenes.length > 0) {
        const lines = warnings.concat(problemScenes).slice(0, 10).map(formatIssue).join("\n");
        const total = warnings.length + problemScenes.length;
        const plural = total === 1 ? "" : "s";
        proceed = confirm(
          `Preflight encontrou ${total} alerta${plural}:\n\n${lines}\n\nGerar o pacote mesmo assim?`,
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
