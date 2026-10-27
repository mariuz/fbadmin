import { request } from "../../api";

export interface KubeStatus {
  enabled: boolean; reason?: string; source?: string; server?: string; namespaces?: string[] | null;
  operator?: boolean; error?: string;
}
export interface ClusterSummary {
  namespace: string; name: string; created: number | null; phase: string; phase_reason: string | null;
  instances: number; ready: number; image: string | null; primary: string | null; replication: boolean;
  replication_mode: string | null; hibernated: boolean; suspended: boolean; database: string | null;
  fenced: string[]; superuser_secret: string | null; backup_s3: boolean; switchover_to: string | null;
  registered_server_id: string | null;
}
export interface KubePod { name: string; role: string; ready: boolean; phase: string | null; node: string | null; restarts: number; started: number | null; fenced: boolean }
export interface KubeBackup {
  name: string; type: string; level: number | null; database: string | null; phase: string; started: number | null;
  completed: number | null; location: string | null; verified: boolean | null; error: string | null;
}
export interface ClusterDetail extends ClusterSummary {
  conditions: Array<{ type: string; status: string; reason: string | null; message: string | null }>;
  pods: KubePod[]; backups: KubeBackup[];
  scheduled_backups: Array<{ name: string; schedule: string; type: string; suspended: boolean; last_successful: number | null; last_backup: string | null }>;
  databases: Array<{ name: string; file: string | null; phase: string; path: string | null }>;
  users: Array<{ name: string; username: string | null; admin: boolean; phase: string }>;
  events: Array<{ type: string; reason: string; message: string; count: number | null; time: number | null }>;
}
export interface Done { message: string; name?: string; file?: string; fenced?: string[] }

const cl = (ns: string, name: string) => `/api/kubernetes/clusters/${encodeURIComponent(ns)}/${encodeURIComponent(name)}`;

export const kapi = {
  status: () => request<KubeStatus>("GET", "/api/kubernetes/status"),
  clusters: () => request<ClusterSummary[]>("GET", "/api/kubernetes/clusters"),
  cluster: (ns: string, name: string) => request<ClusterDetail>("GET", cl(ns, name)),
  scale: (ns: string, name: string, instances: number) => request<Done>("POST", cl(ns, name) + "/scale", { instances }),
  hibernate: (ns: string, name: string, on: boolean) => request<Done>("POST", cl(ns, name) + "/hibernate", { on }),
  restart: (ns: string, name: string, instance?: string) => request<Done>("POST", cl(ns, name) + "/restart", { instance: instance ?? null }),
  switchover: (ns: string, name: string, target: string) => request<Done>("POST", cl(ns, name) + "/switchover", { target }),
  fence: (ns: string, name: string, on: boolean, instance: string) => request<Done>("POST", cl(ns, name) + "/fence", { on, instance }),
  backup: (ns: string, name: string, body: { type: "logical" | "physical"; level?: number | null }) => request<Done>("POST", cl(ns, name) + "/backups", body),
  restore: (ns: string, name: string, backup: string, target?: string) => request<Done>("POST", cl(ns, name) + "/restores", { backup, target: target || null }),
  logs: (ns: string, name: string, pod: string, tail = 300) => request<{ pod: string; text: string }>("GET", `${cl(ns, name)}/logs?pod=${encodeURIComponent(pod)}&tail=${tail}`),
  register: (ns: string, name: string, body: { host?: string | null; port?: number; password?: string | null; shared?: boolean }) =>
    request<{ server_id: string; host: string; databases: string[]; created: boolean; password_from_secret: boolean }>("POST", cl(ns, name) + "/register", body),
};
