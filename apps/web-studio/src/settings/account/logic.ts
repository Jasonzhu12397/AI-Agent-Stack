import { ApiError } from "../../services/http.ts";

export type ProfileForm = {
  display_name: string;
  email: string;
  timezone: string;
  locale: string;
};

export type ProfileErrors = Partial<Record<keyof ProfileForm, string>>;

export const SUPPORTED_LOCALES = ["en", "zh"] as const;

/** English source strings rendered on the Account page; keys must exist in the zh namespace. */
export const pageLabels: Record<string, string> = {
  "Account Management": "Account Management",
  "Profile details": "Profile details",
  "Display name": "Display name",
  "Email": "Email",
  "Timezone": "Timezone",
  "Locale": "Locale",
  "Sign-in methods": "Sign-in methods",
  "Password": "Password",
  "Bound identities": "Bound identities",
  "Active sessions": "Active sessions",
  "Current session": "Current session",
  "Revoke": "Revoke",
  "Revoke session": "Revoke session",
  "Confirm session revocation": "Confirm session revocation",
  "Last source IP": "Last source IP",
  "Last active": "Last active",
  "Save changes": "Save changes",
  "Saved": "Saved",
  "Identity provider": "Identity provider",
  "No identities connected.": "No identities connected.",
  "No active sessions.": "No active sessions.",
  "This session cannot be revoked.": "This session cannot be revoked.",
  "Other sessions will be signed out immediately.": "Other sessions will be signed out immediately.",
};

export function validateProfile(form: ProfileForm): ProfileErrors {
  const errors: ProfileErrors = {};
  if (!form.display_name.trim()) errors.display_name = "DISPLAY_NAME_REQUIRED";
  if (!form.email.trim()) {
    errors.email = "EMAIL_REQUIRED";
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.trim())) {
    errors.email = "EMAIL_INVALID";
  }
  if (!form.timezone.trim()) errors.timezone = "TIMEZONE_REQUIRED";
  if (!SUPPORTED_LOCALES.includes(form.locale as (typeof SUPPORTED_LOCALES)[number])) {
    errors.locale = "LOCALE_INVALID";
  }
  return errors;
}

export function profilePayload(form: ProfileForm): ProfileForm {
  return {
    display_name: form.display_name.trim(),
    email: form.email.trim(),
    timezone: form.timezone.trim(),
    locale: form.locale,
  };
}

export type Identity = {
  provider: string;
  uid?: string;
  has_password?: boolean;
  bound_at?: string;
};

export type IdentitySummary = {
  methods: string[];
  hasPassword: boolean;
  boundCount: number;
};

export function summarizeIdentities(identities: Identity[]): IdentitySummary {
  const methods = identities.map((identity) => identity.provider).filter(Boolean);
  const hasPassword =
    identities.some((identity) => identity.has_password) ||
    identities.some((identity) => identity.provider === "password");
  return { methods, hasPassword, boundCount: identities.length };
}

export type SessionInfo = {
  key: string;
  current?: boolean;
  created_at?: string;
  last_ip?: string;
  user_agent?: string;
  revoked?: boolean;
};

export function sessionCanRevoke(session: SessionInfo): boolean {
  return !session.current && !session.revoked;
}

export function fieldErrorsFromError(
  error: ApiError,
): Record<string, string[]> {
  const details = error.details ?? {};
  const out: Record<string, string[]> = {};
  for (const [key, value] of Object.entries(details)) {
    out[key] = Array.isArray(value) ? value.map(String) : [String(value)];
  }
  return out;
}

export function stateFromError(error: unknown): { kind: "error" | "denied" } {
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
    return { kind: "denied" };
  }
  return { kind: "error" };
}

const ERROR_MESSAGE_KEYS: Record<string, string> = {
  DISPLAY_NAME_REQUIRED: "Display name is required.",
  EMAIL_REQUIRED: "Email address is required.",
  EMAIL_INVALID: "Invalid email address.",
  TIMEZONE_REQUIRED: "Timezone is required.",
  LOCALE_INVALID: "Unsupported locale.",
};

export function errorMessageKey(code: string): string {
  return ERROR_MESSAGE_KEYS[code] ?? "Profile could not be saved.";
}
