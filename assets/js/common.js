/**
 * Role: Shares Save Audio settings behavior between modal and inspector.
 * File Name: common.js
 * Author: OpenAI Codex
 * Created Date: 2026-09-04
 */

(function () {
  "use strict";

  const namespace = (window.CWSaveAudioBlockUi = window.CWSaveAudioBlockUi || {});

  /**
   * Bind directory, naming, and idle settings to one explicit block UI action.
   *
   * @param {HTMLElement} root - Mounted Save Audio surface.
   * @param {object} api - Generic block UI API exposing applyAction.
   * @param {object} options - Surface-specific action name.
   * @returns {void}
   */
  namespace.mountEditor = function mountEditor(root, api, { actionName = "inspector_update_save_audio" } = {}) {
    const outputDir = root.querySelector("[data-save-audio-output-dir]");
    const filenameTemplate = root.querySelector("[data-save-audio-filename-template]");
    const idleFinalize = root.querySelector("[data-save-audio-idle-finalize-sec]");
    const applyButton = root.querySelector("[data-save-audio-apply]");
    let dirty = false;

    const markDirty = () => {
      dirty = true;
      if (applyButton instanceof HTMLButtonElement) {
        applyButton.disabled = false;
      }
    };
    const apply = async () => {
      if (!dirty) {
        return;
      }
      const result = await api.applyAction(actionName, {
        output_dir: outputDir?.value || "",
        filename_template: filenameTemplate?.value || "",
        idle_finalize_sec: Number(idleFinalize?.value || 1.5),
      });
      if (result?.error) {
        throw new Error(result.error);
      }
      dirty = false;
      if (applyButton instanceof HTMLButtonElement) {
        applyButton.disabled = true;
      }
    };

    for (const field of [outputDir, filenameTemplate, idleFinalize]) {
      field?.addEventListener("input", markDirty);
      field?.addEventListener("change", markDirty);
      field?.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          void apply().catch((error) => api.log?.(`[save-audio-error] ${error.message}`));
        }
      });
    }
    applyButton?.addEventListener("click", () => {
      void apply().catch((error) => api.log?.(`[save-audio-error] ${error.message}`));
    });
  };
})();
