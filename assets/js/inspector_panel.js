/**
 * Role: Mounts the Save Audio inspector settings workflow.
 * File Name: inspector_panel.js
 * Author: OpenAI Codex
 * Created Date: 2026-09-04
 */

(function () {
  "use strict";

  const registry = (window.CWBlockUiBlocks = window.CWBlockUiBlocks || {});

  registry.save_audioInspectorPanel = {
    /** Mount inspector fields through the Save Audio block-owned action. */
    mount(root, api) {
      window.CWSaveAudioBlockUi?.mountEditor?.(root, api, { actionName: "inspector_update_save_audio" });
    },
  };
})();
