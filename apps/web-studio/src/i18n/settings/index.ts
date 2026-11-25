import { messages as account } from "./account.zh";
import { messages as plan } from "./plan.zh";
import { messages as subscription } from "./subscription.zh";
import { messages as general } from "./general.zh";
import { messages as appearance } from "./appearance.zh";
import { messages as notifications } from "./notifications.zh";
import { messages as workMode } from "./work-mode.zh";
import { messages as shortcuts } from "./shortcuts.zh";
import { messages as dataControls } from "./data-controls.zh";
import { messages as archive } from "./archive.zh";
import { messages as privacy } from "./privacy.zh";
import { messages as help } from "./help.zh";
import { messages as about } from "./about.zh";

/** One namespace per settings page so parallel work never edits a shared dictionary. */
export const settingsNamespaceMessages: Record<string, Record<string, string>> = {
  account,
  plan,
  subscription,
  general,
  appearance,
  notifications,
  "work-mode": workMode,
  shortcuts,
  "data-controls": dataControls,
  archive,
  privacy,
  help,
  about,
};

function merge(namespaces: Record<string, Record<string, string>>): Record<string, string> {
  const merged: Record<string, string> = {};
  for (const messages of Object.values(namespaces)) {
    for (const [source, translation] of Object.entries(messages)) {
      const key = source.replace(/\s+/g, " ").trim();
      if (merged[key] !== undefined && merged[key] !== translation) {
        throw new Error(`Conflicting settings translation for "${key}"`);
      }
      merged[key] = translation;
    }
  }
  return merged;
}

export const settingsMessages: Record<string, string> = merge(settingsNamespaceMessages);
