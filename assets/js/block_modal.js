/**
 * Role: Mounts the Save Audio modal settings workflow.
 * File Name: block_modal.js
 * Author: OpenAI Codex
 * Created Date: 2026-09-04
 */

(function () {
  "use strict";

  const registry = (window.CWBlockUiBlocks = window.CWBlockUiBlocks || {});

  registry.save_audio = {
    /** Mount modal fields through the Save Audio block-owned action. */
    mount(root, api) {
      window.CWSaveAudioBlockUi?.mountEditor?.(root, api, { actionName: "modal_update_save_audio" });
    },
  };
})();
