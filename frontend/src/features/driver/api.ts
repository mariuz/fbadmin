/** API client: the server-wide database driver setting (GET/PUT /api/driver). */
import { request } from "../../api";

/** The setting: "auto" (default) resolves to native or firebirdsql, see DriverStatus.effective. */
export type DriverName = "auto" | "native" | "firebirdsql";
export type EffectiveDriver = "native" | "firebirdsql";
export type Source = "environment" | "settings" | "default";

export interface NativeStatus {
  package: string | null;
  available: boolean;
  reason: string;
  library: string | null;
  client_version: string | null;
  client_major: number | null;
  restart_required: boolean;
  embedded_library: string | null;
  /** Firebird 4+ client: can it load ICU (time zone names)? null when not checked */
  icu: boolean | null;
  icu_error: string | null;
}

export interface DriverStatus {
  driver: DriverName;
  source: Source;
  /** the driver new connections use, and why (auto: which rule decided) */
  effective: EffectiveDriver;
  reason: string;
  /** set when auto fell back to firebirdsql because the native client library failed */
  fallback: string | null;
  fbclient: string | null;
  fbclient_source: Source;
  stored: { driver?: string; fbclient?: string };
  env: Record<string, string | null>;
  native: NativeStatus;
  drivers: DriverName[];
  closed_connections?: number;
}

export const driverApi = {
  get: () => request<DriverStatus>("GET", "/api/driver"),
  save: (body: { driver?: DriverName; fbclient?: string }) => request<DriverStatus>("PUT", "/api/driver", body),
};
