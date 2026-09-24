import { withProperties } from "./properties.js";

/**
 * Role: Mounts the Save Audio modal settings workflow.
 * File Name: block_modal.js
 * Author: OpenAI Codex
 * Created Date: 2026-09-04
 */

import { mountEditor } from "./common.js";

/** Mount modal fields through the Save Audio block-owned action. */
function mountOwned(root, api) {
  mountEditor(root, api, { actionName: "modal_update_save_audio" });
}

/** Keep the block behavior and add properties-only accessibility. */
export function mount(root, ...args) {
  return withProperties(mountOwned).call(this, root, ...args);
}
