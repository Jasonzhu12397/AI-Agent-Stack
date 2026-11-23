import { ref, type Ref } from "vue";
import { ApiError } from "../../services/http";
import type { SettingsLoadState } from "./scope";

export type Resource<T> = {
  state: Ref<SettingsLoadState>;
  data: Ref<T | null>;
  errorCode: Ref<string>;
  errorMessage: Ref<string>;
  reload: () => Promise<void>;
};

/**
 * Turns any loader into the five states every settings page must render.
 * 401/403 map to "denied" with the server code kept for display; empty payloads map to "empty".
 */
export function useSettingsResource<T>(
  loader: () => Promise<T>,
  isEmpty?: (value: T) => boolean,
): Resource<T> {
  const state = ref<SettingsLoadState>("loading");
  const data = ref<T | null>(null) as Ref<T | null>;
  const errorCode = ref("");
  const errorMessage = ref("");

  async function reload(): Promise<void> {
    state.value = "loading";
    errorCode.value = "";
    errorMessage.value = "";
    try {
      const value = await loader();
      data.value = value;
      state.value = isEmpty?.(value) ? "empty" : "ready";
    } catch (cause) {
      data.value = null;
      if (cause instanceof ApiError) {
        errorCode.value = cause.code;
        errorMessage.value = cause.message;
        state.value = cause.status === 401 || cause.status === 403 ? "denied" : "error";
        return;
      }
      errorCode.value = "CLIENT_ERROR";
      errorMessage.value = cause instanceof Error ? cause.message : "Unexpected error.";
      state.value = "error";
    }
  }

  return { state, data, errorCode, errorMessage, reload };
}
