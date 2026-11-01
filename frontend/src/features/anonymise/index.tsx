/** Roadmap 2.3: anonymised copies of production - masking rules per column, consistent across foreign keys,
 *  copied to a development or test server. */
import { useEffect, useMemo, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { N_, t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { Check, Section } from "../maintenance/common";
import { fmtTime, waitJob } from "../maintenance/api";
import "../maintenance/maintenance.css";
import "../monitoring/monitoring.css";
import "./anonymise.css";
import { anonApi, type Column, type Preview, type Profile, type Rule, type Rules, type Schema, type Strategy, type Target } from "./api";

const LABELS: Record<Strategy, string> = {
  keep: N_("keep"), null: N_("empty (NULL)"), fixed: N_("fixed value"), hash: N_("pseudonym (hash)"), email: N_("fake e-mail"),
  name: N_("fake full name"), first_name: N_("fake first name"), last_name: N_("fake last name"), city: N_("fake city"),
  phone: N_("scrambled digits"), words: N_("placeholder words"), redact: N_("redact (keep the end)"),
  number_noise: N_("number ± percent"), date_shift: N_("date ± days"), shuffle: N_("shuffle between rows"),
};
const show = (v: unknown) => (v === null || v === undefined ? "NULL" : typeof v === "object" ? JSON.stringify(v) : String(v));

function RuleCell(props: { col: Column; rule: Rule | undefined; via: string | null; onChange: (r: Rule | null) => void }) {
  const { col, rule } = props;
  const st = rule?.strategy ?? "keep";
  const id = `an-${col.name}`;
  return (
    <div className="an-rule">
      <select className="text" aria-label={t("Masking of {column}", { column: col.name })} value={st}
        onChange={(e) => {
          const s = e.target.value as Strategy;
          props.onChange(s === "keep" ? null : { ...(col.suggested?.strategy === s ? col.suggested : {}), strategy: s });
        }}>
        {col.allowed.map((s) => <option key={s} value={s}>{t(LABELS[s])}</option>)}
      </select>
      {st === "fixed" && <input id={id + "-v"} className="text an-param" aria-label={t("Fixed value")} value={String(rule?.value ?? "")}
        onChange={(e) => props.onChange({ ...rule!, value: e.target.value })} />}
      {st === "redact" && <input className="text an-param" type="number" min={0} max={100} aria-label={t("Characters kept at the end")}
        value={rule?.keep ?? 4} onChange={(e) => props.onChange({ ...rule!, keep: Number(e.target.value) })} />}
      {st === "number_noise" && <input className="text an-param" type="number" min={0} max={1000} aria-label={t("Percent")}
        value={rule?.percent ?? 10} onChange={(e) => props.onChange({ ...rule!, percent: Number(e.target.value) })} />}
      {st === "date_shift" && <input className="text an-param" type="number" min={0} max={36500} aria-label={t("Days")}
        value={rule?.days ?? 30} onChange={(e) => props.onChange({ ...rule!, days: Number(e.target.value) })} />}
      {st === "keep" && props.via && <span className="muted an-via">{t("masked through the foreign key from {column}", { column: props.via })}</span>}
    </div>
  );
}

function PreviewTable(props: { p: Preview }) {
  const { p } = props;
  return (
    <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Preview of the masked rows")} style={{ maxHeight: 260 }}>
      <table className="grid">
        <thead><tr>{p.columns.map((c) => <th key={c.name}>{c.name}{c.strategy !== "keep" && <span className="pill running an-pill">{t(LABELS[c.strategy])}</span>}</th>)}</tr></thead>
        <tbody>{p.masked.map((r, i) => (
          <tr key={i}>{r.map((v, j) => {
            const changed = p.columns[j].strategy !== "keep";
            return <td key={j} className={changed ? "an-masked" : ""} title={changed ? t("was: {value}", { value: show(p.original[i][j]) }) : undefined}>{show(v)}</td>;
          })}</tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Editor(props: { sid: string; dbid: string; schema: Schema; profile: Profile | null; onSaved: (p: Profile) => void; onDeleted: () => void }) {
  const app = useApp();
  const { sid, dbid, schema } = props;
  const srcDb = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid);
  const [name, setName] = useState(props.profile?.name ?? t("Development copy"));
  const [rules, setRules] = useState<Rules>(props.profile?.rules ?? {});
  const [target, setTarget] = useState<Target>(props.profile?.target ?? { server_id: sid, path: "", register_as: `${srcDb?.name ?? "db"} (anonymised)`, replace: false });
  const [scratch, setScratch] = useState(props.profile?.scratch_path ?? "");
  const [filter, setFilter] = useState("");
  const [onlyMasked, setOnlyMasked] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const own = (tb: string, c: string) => rules[tb]?.[c];
  const masked = useMemo(() => {
    const out = new Map<string, string>(); // "T.C" -> "U.D" that masks it through a foreign key
    for (const tb of schema.tables) for (const c of tb.columns) {
      const key = `${tb.name}.${c.name}`;
      const src = c.linked.find((l) => { const [lt, lc] = l.split("."); const r = rules[lt]?.[lc]; return r && r.strategy !== "keep"; });
      if (src && !(own(tb.name, c.name)?.strategy && own(tb.name, c.name)!.strategy !== "keep")) out.set(key, src);
    }
    return out;
  }, [rules, schema]);
  const count = Object.values(rules).reduce((n, cr) => n + Object.values(cr).filter((r) => r.strategy !== "keep").length, 0);
  const setRule = (tb: string, c: string, r: Rule | null) => setRules((rs) => {
    const next = { ...rs, [tb]: { ...(rs[tb] ?? {}) } };
    if (r) next[tb][c] = r; else delete next[tb][c];
    if (!Object.keys(next[tb]).length) delete next[tb];
    return next;
  });
  const suggestAll = () => setRules((rs) => {
    const next: Rules = JSON.parse(JSON.stringify(rs));
    for (const tb of schema.tables) for (const c of tb.columns) {
      if (c.suggested && !next[tb.name]?.[c.name]) (next[tb.name] ??= {})[c.name] = c.suggested;
    }
    return next;
  });
  const suggested = schema.tables.reduce((n, tb) => n + tb.columns.filter((c) => c.suggested && !own(tb.name, c.name)).length, 0);
  const body = () => ({ name: name.trim() || t("Development copy"), rules, target: target.path.trim() ? { ...target, path: target.path.trim(), register_as: target.register_as?.trim() || null } : null, scratch_path: scratch.trim() || null });

  const save = async (): Promise<Profile | null> => {
    setError(null);
    try {
      const p = props.profile ? await anonApi.update(sid, dbid, props.profile.id, body()) : await anonApi.create(sid, dbid, body());
      props.onSaved(p);
      return p;
    } catch (e) { setError(errMsg(e)); return null; }
  };
  const run = async () => {
    if (!target.path.trim()) { setError(t("Choose the target server and the file of the copy first")); return; }
    if (!confirm(target.replace ? t("Make the anonymised copy and replace {path} on the target server?", { path: target.path }) : t("Make the anonymised copy at {path}?", { path: target.path }))) return;
    const p = await save();
    if (!p) return;
    try {
      await anonApi.check(sid, dbid, p.id);
      const job = await anonApi.run(sid, dbid, p.id);
      setBusy(t("Starting…"));
      const end = await waitJob(job.id, (j) => setBusy(j.log?.[j.log.length - 1] ?? t("Running…")));
      if (end.status === "failed") setError(end.error ?? t("The copy failed"));
      else if (end.status === "finished") { app.toast(t("Anonymised copy ready"), "ok"); app.reloadServers(); }
      const fresh = (await anonApi.list(sid, dbid)).profiles.find((x) => x.id === p.id);
      if (fresh) props.onSaved(fresh);
    } catch (e) { setError(errMsg(e)); }
    setBusy(null);
  };
  const doPreview = async (tb: string) => {
    setPreview(null);
    try { setPreview(await anonApi.preview(sid, dbid, tb, rules, props.profile?.id)); } catch (e) { setError(errMsg(e)); }
  };
  const last = props.profile?.last_run;
  const targetSrv = app.servers.find((s) => s.id === target.server_id);

  return (
    <>
      <Section wide title={t("Copy")} right={<>
        <button className="btn small" onClick={save} disabled={!!busy}>{t("Save")}</button>
        <button className="btn small primary" onClick={run} disabled={!!busy}>{t("Make the copy")}</button>
        {props.profile && <button className="btn small danger" disabled={!!busy} onClick={async () => {
          if (!confirm(t("Delete the profile {name}? Copies already made are kept.", { name: props.profile!.name }))) return;
          try { await anonApi.remove(sid, dbid, props.profile!.id); props.onDeleted(); } catch (e) { setError(errMsg(e)); }
        }}>{t("Delete")}</button>}
      </>}>
        <div className="form mform">
          <label className="l" htmlFor="an-name">{t("Profile name")}</label>
          <input id="an-name" className="text" value={name} onChange={(e) => setName(e.target.value)} />
          <label className="l" htmlFor="an-srv">{t("Target server")}</label>
          <select id="an-srv" className="text" value={target.server_id} onChange={(e) => setTarget({ ...target, server_id: e.target.value })}>
            {app.servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          <label className="l" htmlFor="an-path">{t("Database file on the target server")}</label>
          <input id="an-path" className="text" placeholder="/var/lib/firebird/data/dev_copy.fdb" value={target.path} onChange={(e) => setTarget({ ...target, path: e.target.value })} />
          <label className="l" htmlFor="an-reg">{t("Register it as")}</label>
          <input id="an-reg" className="text" value={target.register_as ?? ""} onChange={(e) => setTarget({ ...target, register_as: e.target.value })} />
          <span />
          <Check label={t("Replace the file if it exists (refresh an earlier copy)")} checked={!!target.replace} onChange={(v) => setTarget({ ...target, replace: v })} />
          <label className="l" htmlFor="an-scratch">{t("Scratch copy on the source server (optional)")}</label>
          <input id="an-scratch" className="text" placeholder={t("next to the source database")} value={scratch} onChange={(e) => setScratch(e.target.value)} />
        </div>
        {targetSrv?.id === sid && <div className="muted">{t("The copy goes to the same server as the source.")}</div>}
        {busy && <div className="muted" role="status">{busy}</div>}
        {error && <div className="err" role="alert">{error}</div>}
        {last && (
          <div className={"alert " + (last.ok ? (last.leaks?.length ? "warning" : "good") : "critical")} role="status">
            <span className="lvl">{last.ok ? t("done") : t("failed")}</span>
            <span>
              {last.ok
                ? t("Last copy {time}: {columns} column(s) masked in {rows} row(s), {fks} foreign key(s) checked, in {s} s → {path}", {
                  time: fmtTime(last.at), columns: last.columns ?? 0, rows: last.rows ?? 0, fks: last.foreign_keys ?? 0, s: last.seconds ?? 0, path: last.target ?? "" })
                : t("Last copy {time} failed: {error}", { time: fmtTime(last.at), error: last.error ?? "" })}
              {last.ok && (last.leaks?.length
                ? " " + t("Original values were still found in: {columns}", { columns: last.leaks.map((l) => `${l.table}.${l.column} (${l.rows})`).join(", ") })
                : " " + t("No original value was found in the masked columns."))}
            </span>
          </div>
        )}
      </Section>
      <Section wide title={t("Masking rules")}
        hint={t("Pick how each column is masked. Masking is deterministic: the same value becomes the same fake value in every table and every refresh, so foreign keys keep matching; a rule on a key column applies to every column linked to it. Hover a masked value in the preview to see the original.")}
        right={<>
          {suggested > 0 && <button className="btn small" onClick={suggestAll}>{t("Apply {n} suggestion(s)", { n: suggested })}</button>}
          <span className="muted">{t("{n} column(s) masked", { n: count + masked.size })}</span>
        </>}>
        <div className="mt-row">
          <input className="text" type="search" aria-label={t("Filter tables and columns")} placeholder={t("Filter tables and columns…")} value={filter} onChange={(e) => setFilter(e.target.value)} />
          <Check label={t("Only masked columns")} checked={onlyMasked} onChange={setOnlyMasked} />
        </div>
        {schema.tables.map((tb) => {
          const f = filter.trim().toUpperCase();
          const cols = tb.columns.filter((c) => (!f || tb.name.includes(f) || c.name.includes(f))
            && (!onlyMasked || (own(tb.name, c.name)?.strategy ?? "keep") !== "keep" || masked.has(`${tb.name}.${c.name}`)));
          if (!cols.length) return null;
          const n = tb.columns.filter((c) => (own(tb.name, c.name)?.strategy ?? "keep") !== "keep" || masked.has(`${tb.name}.${c.name}`)).length;
          const isOpen = open === tb.name || !!f || onlyMasked;
          return (
            <div key={tb.name} className="an-table">
              <button className="an-thead" aria-expanded={isOpen} onClick={() => { setOpen(open === tb.name ? null : tb.name); setPreview(null); }}>
                <span>{isOpen ? "▾" : "▸"} {tb.name}</span>
                <span className="muted">{n ? t("{n} masked", { n }) : t("unchanged")}</span>
              </button>
              {isOpen && <>
                <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Columns of {table}", { table: tb.name })}>
                  <table className="grid">
                    <thead><tr><th>{t("Column")}</th><th>{t("Type")}</th><th>{t("Masking")}</th><th>{t("Suggested")}</th></tr></thead>
                    <tbody>{cols.map((c) => (
                      <tr key={c.name}>
                        <td>{c.name}{c.key && <span className="pill cancelled an-pill">{t("key")}</span>}{c.linked.length > 0 && <span className="pill cancelled an-pill" title={c.linked.join(", ")}>{t("FK")}</span>}</td>
                        <td className="mono-cell">{c.data_type}{c.not_null ? " NOT NULL" : ""}</td>
                        <td><RuleCell col={c} rule={own(tb.name, c.name)} via={masked.get(`${tb.name}.${c.name}`) ?? null} onChange={(r) => setRule(tb.name, c.name, r)} /></td>
                        <td>{c.suggested ? t(LABELS[c.suggested.strategy]) : <span className="muted">—</span>}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
                <div className="mt-row"><button className="btn small" onClick={() => doPreview(tb.name)}>{t("Preview masked rows")}</button></div>
                {preview && open === tb.name && <PreviewTable p={preview} />}
              </>}
            </div>
          );
        })}
      </Section>
    </>
  );
}

function AnonymiseTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const { serverId: sid, dbId: dbid } = props;
  const [schema, setSchema] = useState<Schema | null>(null);
  const [profiles, setProfiles] = useState<Profile[] | null>(null);
  const [sel, setSel] = useState<string>("new");
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!props.visible || schema) return;
    Promise.all([anonApi.columns(sid, dbid), anonApi.list(sid, dbid)])
      .then(([s, l]) => { setSchema(s); setProfiles(l.profiles); if (l.profiles.length) setSel(l.profiles[0].id); })
      .catch((e) => setError(errMsg(e)));
  }, [props.visible]);
  const profile = profiles?.find((p) => p.id === sel) ?? null;
  return (
    <div className="mt-body anon">
      <Section wide title={t("Anonymised copy")}
        hint={t("Copies this database to a development or test server with personal data masked. The source is backed up and restored as a scratch copy on the source server, masked there (triggers off, foreign keys checked), then copied to the target; unmasked data never reaches the target server and the scratch copy is dropped.")}
        right={profiles && <>
          <label className="an-pick">{t("Masking profile")}{" "}
            <select className="text" value={sel} onChange={(e) => { setSel(e.target.value); setNonce((n) => n + 1); }}>
              {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              <option value="new">{t("New profile…")}</option>
            </select>
          </label>
        </>}>
        {error && <div className="err">{error}</div>}
        {!schema && !error && <div className="muted">{t("Loading…")}</div>}
      </Section>
      {schema && profiles && <Editor key={nonce} sid={sid} dbid={dbid} schema={schema} profile={profile}
        onSaved={(p) => { setProfiles((ps) => [...(ps ?? []).filter((x) => x.id !== p.id), p]); setSel(p.id); }}
        onDeleted={() => { setProfiles((ps) => (ps ?? []).filter((x) => x.id !== sel)); setSel("new"); setNonce((n) => n + 1); }} />}
    </div>
  );
}

export function openAnonymise(app: AppApi, sid: string, dbid: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "anonymise", title: t("{db} · anonymised copy", { db }), serverId: sid, dbId: dbid });
}

registerTabKind("anonymise", {
  icon: <span aria-hidden="true" style={{ fontSize: 12 }}>◐</span>,
  render: (tab, visible) => <AnonymiseTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Anonymised copy…"), onClick: () => openAnonymise(app, sid, dbid) }];
});
