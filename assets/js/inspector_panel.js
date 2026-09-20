/**
 * Role: Mounts the Save Audio inspector settings workflow.
 * File Name: inspector_panel.js
 * Author: OpenAI Codex
 * Created Date: 2026-09-04
 */

import { mountEditor } from "./common.js";

/** Mount inspector fields through the Save Audio block-owned action. */
export function mount(root, api) {
  mountEditor(root, api, { actionName: "inspector_update_save_audio" });
}
