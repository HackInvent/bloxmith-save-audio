/**
 * Role: Mounts the Save Audio modal settings workflow.
 * File Name: block_modal.js
 * Author: OpenAI Codex
 * Created Date: 2026-09-04
 */

import { mountEditor } from "./common.js";

/** Mount modal fields through the Save Audio block-owned action. */
export function mount(root, api) {
  mountEditor(root, api, { actionName: "modal_update_save_audio" });
}
