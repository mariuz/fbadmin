/** Roadmap 2.5: weekly digest per database - the options of a "digest" schedule (features/maintenance/schedules.tsx). */
import { useEffect, useState } from "react";
import { request } from "../../api";
import { t } from "../../i18n";
import type { HistoryItem } from "../maintenance/api";

export interface DigestOptions { channels: string[]; days: number }
export const defaultDigestOptions = (): DigestOptions => ({ channels: [], days: 7 });
interface Channel { id: string; name: string; kind: string; enabled: boolean }

export function DigestOptionsForm(props: { value: DigestOptions; onChange: (v: DigestOptions) => void }) {
  const v = props.value;
  const [channels, setChannels] = useState<Channel[] | null>(null);
  useEffect(() => { request<{ channels: Channel[] }>("GET", "/api/notebooks/channels").then((r) => setChannels(r.channels)).catch(() => setChannels([])); }, []);
  return (
    <div className="form mform">
      <span className="l" />
      <div className="muted" style={{ fontSize: 12 }}>{t("One message with what happened to the database over the period: its status, the check-up, the capacity forecast, query regressions, backups, change requests, errors seen by the trace, alerts and comments. No database connection is needed.")}</div>
      <label className="l" htmlFor="dg-days">{t("Period")}</label>
      <select id="dg-days" className="text" value={v.days} onChange={(e) => props.onChange({ ...v, days: Number(e.target.value) })}>
        {[1, 7, 14, 30].map((d) => <option key={d} value={d}>{t("the last {n} day(s)", { n: d })}</option>)}
      </select>
      <span className="l">{t("Send to")}</span>
      <div role="group" aria-label={t("Send the digest to")}>
        {channels?.length === 0 && <span className="muted">{t("No alert channels yet: an fbadmin administrator adds e-mail, Slack or webhook channels under Alerts.")}</span>}
        {channels?.map((c) => (
          <label key={c.id} className="check">
            <input type="checkbox" checked={v.channels.includes(c.id)} disabled={!c.enabled}
              onChange={(e) => props.onChange({ ...v, channels: e.target.checked ? [...v.channels, c.id] : v.channels.filter((x) => x !== c.id) })} />
            {c.name} <span className="muted">({c.kind})</span>
          </label>
        ))}
      </div>
    </div>
  );
}

/** One-line result of a digest run in the history. */
export function digestResult(h: HistoryItem): string {
  const x = h as HistoryItem & { status_of_db?: string };
  const parts = [t("database status: {status}", { status: t(x.status_of_db ?? "unknown") })];
  if (h.sent?.length) parts.push(t("sent to {channels}", { channels: h.sent.join(", ") }));
  if (h.delivery_errors?.length) parts.push(t("not sent: {errors}", { errors: h.delivery_errors.join("; ") }));
  return parts.join(" · ");
}
