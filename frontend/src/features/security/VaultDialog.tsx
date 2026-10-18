/** Master password: unlock, set up, change, or reset (forgotten password). */
import { useEffect, useState } from "react";
import { errMsg } from "../../context";
import { Modal } from "../../components/Modal";
import { secApi } from "./api";
import { refreshAuth, type VaultStatus } from "./state";
import { t } from "../../i18n";

export function VaultDialog(props: { onDone: (ok: boolean) => void; manage?: boolean }) {
  const [st, setSt] = useState<VaultStatus | null>(null);
  const [mode, setMode] = useState<"unlock" | "setup" | "change" | "reset" | "disable">("unlock");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [old, setOld] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  useEffect(() => {
    secApi.vault().then((s) => {
      setSt(s);
      setMode(!s.enabled ? "setup" : s.unlocked && props.manage ? "change" : "unlock");
    }).catch((e) => setError(errMsg(e)));
  }, []);

  const run = async (fn: () => Promise<boolean | void>) => {
    setBusy(true);
    setError(null);
    try {
      const done = await fn();
      await refreshAuth().catch(() => undefined);
      if (done !== false) props.onDone(true);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = () => {
    if (mode === "unlock") return run(() => secApi.vaultUnlock(pw).then(() => undefined));
    if (mode === "setup") {
      if (pw !== pw2) return setError(t("The passwords do not match"));
      return run(async () => {
        const r = await secApi.vaultSetup(pw);
        if (r.migrated) setInfo(t("{n} saved password(s) are now encrypted", { n: r.migrated }));
      });
    }
    if (mode === "change") {
      if (pw !== pw2) return setError(t("The passwords do not match"));
      return run(() => secApi.vaultChange(old, pw).then(() => undefined));
    }
    if (mode === "disable") return run(() => secApi.vaultDisable(pw).then(() => undefined));
    return run(() => secApi.vaultReset().then(() => undefined));
  };

  const title = { unlock: t("Unlock saved passwords"), setup: t("Set a master password"), change: t("Change master password"),
    reset: t("Reset master password"), disable: t("Remove master password") }[mode];
  const loginBound = !!st?.login_bound;

  return (
    <Modal
      title={title}
      onClose={() => props.onDone(false)}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          {info && <span className="ok">{info}</span>}
          <button className="btn" onClick={() => props.onDone(false)}>{t("Cancel")}</button>
          <button className={"btn primary" + (mode === "reset" ? " danger solid" : "")} disabled={busy || (mode !== "reset" && !pw)} onClick={submit}>
            {mode === "unlock" ? t("Unlock") : mode === "reset" ? t("Forget saved passwords") : mode === "disable" ? t("Remove") : t("Save")}
          </button>
        </>
      }
    >
      {st === null && !error && <div className="muted">{t("Loading…")}</div>}
      {st && (
        <form className="vault-form" onSubmit={(e) => { e.preventDefault(); submit(); }}>
          {mode === "unlock" && (
            <p className="muted">{t("Saved Firebird passwords are encrypted (AES-256-GCM). Enter your master password{hint} to use them in this session.", { hint: loginBound ? " " + t("(usually your previous fbadmin login password)") : "" })}</p>
          )}
          {mode === "setup" && (
            <p className="muted">{t("Saved Firebird passwords will be encrypted with a key derived from this password (Argon2id). It is never stored; if you forget it, the saved passwords are lost.")}{st.plaintext_passwords ? " " + t("{n} password(s) saved in plain text will be encrypted now.", { n: st.plaintext_passwords }) : ""}</p>
          )}
          {mode === "reset" && (
            <p className="warn">{t("This forgets every encrypted saved password and removes the master password. You will be asked for the Firebird passwords again.")}</p>
          )}
          {mode === "change" && (
            <label className="vault-field">{t("Current master password")}<input className="text" type="password" autoFocus value={old} onChange={(e) => setOld(e.target.value)} /></label>
          )}
          {mode !== "reset" && (
            <label className="vault-field">{mode === "change" ? t("New master password") : t("Master password")}
              <input className="text" type="password" autoFocus={mode !== "change"} value={pw} onChange={(e) => setPw(e.target.value)} />
            </label>
          )}
          {(mode === "setup" || mode === "change") && (
            <label className="vault-field">{t("Confirm")}<input className="text" type="password" value={pw2} onChange={(e) => setPw2(e.target.value)} /></label>
          )}
          <div className="vault-links">
            {st.enabled && mode !== "unlock" && <a onClick={() => setMode("unlock")}>{t("Unlock")}</a>}
            {st.enabled && st.unlocked && !loginBound && mode !== "change" && <a onClick={() => setMode("change")}>{t("Change master password")}</a>}
            {st.enabled && !st.login_bound && props.manage && mode !== "disable" && <a onClick={() => setMode("disable")}>{t("Remove master password")}</a>}
            {st.enabled && mode !== "reset" && <a onClick={() => setMode("reset")}>{t("Forgot it? Reset")}</a>}
          </div>
          <button type="submit" hidden />
        </form>
      )}
    </Modal>
  );
}
