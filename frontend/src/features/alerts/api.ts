import { request, type Body } from "../../api";

export type ChannelKind = "email" | "slack" | "webhook";

export interface Channel {
  id: string; name: string; kind: ChannelKind; enabled: boolean; created_at: number;
  settings: Record<string, string>;
  [k: `has_${string}`]: boolean | undefined;
}

export interface EventInfo { id: string; kind: "event" | "condition"; threshold: number | null; unit?: string; label: string }

export interface RuleStatus { checked_at: number; state: "ok" | "firing" | "error"; value: unknown; message: string | null; error: string | null }

export interface Rule {
  id: string; name: string; event: string; enabled: boolean; server_id: string | null; database_id: string | null;
  threshold: number | null; peer_server_id: string | null; peer_database_id: string | null; channels: string[];
  cooldown_minutes: number; notify_resolved: boolean; created_at: number; status: RuleStatus | null;
}

export interface Delivery { channel_id: string; channel: string; kind: ChannelKind; at: number; ok: boolean; result?: string; error?: string }

export interface AlertItem {
  id: string; event: string; level: "critical" | "warning" | "info" | "resolved"; title: string; message: string;
  server: string | null; database: string | null; time: number; rule: string | null; deliveries: Delivery[];
}

export const aapi = {
  events: () => request<EventInfo[]>("GET", "/api/alerts/events"),
  channels: () => request<Channel[]>("GET", "/api/alerts/channels"),
  createChannel: (body: Body<"/api/alerts/channels", "post">) => request<Channel>("POST", "/api/alerts/channels", body),
  updateChannel: (id: string, body: Body<"/api/alerts/channels/{channel_id}", "put">) => request<Channel>("PUT", `/api/alerts/channels/${id}`, body),
  deleteChannel: (id: string) => request<void>("DELETE", `/api/alerts/channels/${id}`),
  testChannel: (id: string) => request<{ ok: boolean; result?: string; error?: string }>("POST", `/api/alerts/channels/${id}/test`),
  rules: () => request<Rule[]>("GET", "/api/alerts/rules"),
  createRule: (body: Body<"/api/alerts/rules", "post">) => request<Rule>("POST", "/api/alerts/rules", body),
  updateRule: (id: string, body: Body<"/api/alerts/rules/{rule_id}", "put">) => request<Rule>("PUT", `/api/alerts/rules/${id}`, body),
  deleteRule: (id: string) => request<void>("DELETE", `/api/alerts/rules/${id}`),
  checkRule: (id: string) => request<Rule>("POST", `/api/alerts/rules/${id}/check`),
  history: (limit = 100) => request<AlertItem[]>("GET", `/api/alerts/history?limit=${limit}`),
  clearHistory: () => request<void>("DELETE", "/api/alerts/history"),
};
