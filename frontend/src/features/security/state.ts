/** Login / server-mode status shared by the auth gate, the user menu and dialogs. */
import { useEffect, useState } from "react";
import { request } from "../../api";

export interface AuthUser {
  id: number;
  username: string;
  role: "admin" | "user";
  email: string | null;
  auth_source: string;
}

export interface VaultStatus {
  enabled: boolean;
  unlocked: boolean;
  login_bound: boolean;
  plaintext_passwords: number;
}

export interface AuthStatus {
  server_mode: boolean;
  authenticated: boolean;
  needs_setup: boolean;
  auth_sources: Array<{ type: "password" | "oidc" | "proxy"; name: string; title?: string }>;
  user: AuthUser | null;
  vault?: VaultStatus | null;
  audit: boolean;
}

let current: AuthStatus | null = null;
const listeners = new Set<(s: AuthStatus | null) => void>();

export function getAuth(): AuthStatus | null {
  return current;
}

export function setAuth(s: AuthStatus | null) {
  current = s;
  listeners.forEach((l) => l(s));
}

export async function refreshAuth(): Promise<AuthStatus> {
  const s = await request<AuthStatus>("GET", "/api/auth/status", undefined, false);
  setAuth(s);
  return s;
}

export function useAuth(): AuthStatus | null {
  const [s, setS] = useState(current);
  useEffect(() => {
    listeners.add(setS);
    setS(current);
    return () => { listeners.delete(setS); };
  }, []);
  return s;
}

export const isAdmin = (s: AuthStatus | null = current) => !!s && (!s.server_mode || s.user?.role === "admin");
/** Shared servers exist only in server mode and are managed by admins. */
export const canShareServers = (s: AuthStatus | null = current) => !!s?.server_mode && s.user?.role === "admin";
