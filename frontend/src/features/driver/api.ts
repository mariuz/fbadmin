/** API client: the server-wide database driver setting (GET/PUT /api/driver). */
import { request } from "../../api";

export type DriverName = "firebirdsql" | "native";
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
}

export interface DriverStatus {
  driver: DriverName;
  source: Source;
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
