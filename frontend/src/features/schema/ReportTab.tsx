import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { WorkTab } from "../../context";
import { IconRefresh, IconSpinner } from "../../components/Icons";
import { schemaApi, type ReportSection } from "./api";
import { t } from "../../i18n";

export function ReportTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const sid = tab.serverId!, dbid = tab.dbId!;
  const [secs, setSecs] = useState<ReportSection[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setBusy(true);
    setError(null);
    try {
      setSecs((await schemaApi.report(sid, dbid)).sections);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => { load(); }, []);
  const total = (secs ?? []).reduce((n, s) => n + s.items.length, 0);

  return (
    <div className="sch-col sch-scroll">
      <div className="toolbar">
        <button className="btn" onClick={load} disabled={busy}>{busy ? <IconSpinner /> : <IconRefresh />} {t("Refresh")}</button>
        <span className="muted">{secs ? (total ? t("{n} finding(s)", { n: total }) : t("Nothing to report")) : ""}</span>
      </div>
      {error && <div className="pad err">{error}</div>}
      <div className="sch-report">
        {(secs ?? []).map((s) => (
          <section key={s.id} className={s.items.length ? "has" : "none"}>
            <h3>{s.title} <span className={"sch-count" + (s.items.length ? " warn" : "")}>{s.items.length}</span></h3>
            {s.description && <div className="muted">{s.description}</div>}
            {s.items.length > 0 && (
              <ul>
                {s.items.map((it) => (
                  <li key={it.type + it.name + (it.parent ?? "")}>
                    <a href="#" onClick={(e) => {
                      e.preventDefault();
                      app.select(it.parent && (it.type === "procedures" || it.type === "functions") && it.detail?.startsWith("in package")
                        ? { kind: "object", serverId: sid, dbId: dbid, otype: "packages", name: it.parent }
                        : { kind: "object", serverId: sid, dbId: dbid, otype: it.type, name: it.name });
                    }}>{it.name}</a>
                    {it.detail && <span className="muted"> {it.detail}</span>}
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
