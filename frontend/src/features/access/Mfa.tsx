/** Two-factor sign-in: QR code, enrolment form, recovery codes and the second sign-in step. */
import { useId, useMemo, useState } from "react";
import { errMsg } from "../../context";
import { t } from "../../i18n";
import { accessApi, type Enrolment, type LoginAnswer } from "./api";
import { encodeQr, qrPath } from "./qr";

export function QrCode(props: { text: string; size?: number; label: string }) {
  const q = useMemo(() => encodeQr(props.text, "M"), [props.text]);
  const dim = q.size + 8;
  return (
    <svg className="qr-code" role="img" aria-label={props.label} width={props.size ?? 200} height={props.size ?? 200}
      viewBox={`0 0 ${dim} ${dim}`} shapeRendering="crispEdges">
      <rect width={dim} height={dim} fill="#fff" />
      <path d={qrPath(q)} fill="#000" />
    </svg>
  );
}

/** Secret as groups of four characters (easier to type). */
const grouped = (s: string) => s.replace(/(.{4})/g, "$1 ").trim();

export function EnrolmentInfo(props: { enrol: Enrolment }) {
  const e = props.enrol;
  return (
    <div className="mfa-enrol">
      <QrCode text={e.otpauth_uri} label={t("QR code for your authenticator app")} />
      <div className="mfa-enrol-text">
        <p>{t("Scan the QR code with an authenticator app (for example Aegis, FreeOTP, Google Authenticator or 1Password).")}</p>
        <p>{t("Or enter this key by hand (time-based, 6 digits, 30 seconds):")}</p>
        <code className="mfa-secret" data-testid="mfa-secret">{grouped(e.secret)}</code>
        <p className="muted small">{t("Account: {account}", { account: e.account })}</p>
      </div>
    </div>
  );
}

export function CodeField(props: { value: string; onChange: (v: string) => void; label?: string; autoFocus?: boolean; recovery?: boolean }) {
  const id = useId();
  return (
    <label className="mfa-code" htmlFor={id}>
      {props.label ?? t("Authentication code")}
      <input id={id} className="text" autoFocus={props.autoFocus} autoComplete="one-time-code" inputMode={props.recovery ? "text" : "numeric"}
        maxLength={props.recovery ? 14 : 6} value={props.value} onChange={(e) => props.onChange(e.target.value.trim())} />
    </label>
  );
}

export function RecoveryCodes(props: { codes: string[] }) {
  const copy = () => navigator.clipboard?.writeText(props.codes.join("\n")).catch(() => undefined);
  const download = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([props.codes.join("\n") + "\n"], { type: "text/plain" }));
    a.download = "fbadmin-recovery-codes.txt";
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return (
    <div className="mfa-recovery">
      <p><b>{t("Save these recovery codes now.")}</b> {t("Each one signs you in once if you lose your phone. They are not shown again.")}</p>
      <ul className="mfa-codes" data-testid="recovery-codes">{props.codes.map((c) => <li key={c}><code>{c}</code></li>)}</ul>
      <div className="mt-row">
        <button type="button" className="btn small" onClick={copy}>{t("Copy")}</button>
        <button type="button" className="btn small" onClick={download}>{t("Download")}</button>
      </div>
    </div>
  );
}

/** Second step of the login page: enter a code, or enrol first when an administrator requires 2FA. */
export function MfaLoginStep(props: { answer: LoginAnswer; onDone: (a: LoginAnswer) => void; onCancel: () => void }) {
  const { answer } = props;
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useRecovery, setUseRecovery] = useState(false);
  const [codes, setCodes] = useState<{ codes: string[]; answer: LoginAnswer } | null>(null);
  const enrol = !!answer.mfa_setup_required;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (enrol) {
        const r = await accessApi.loginMfaSetup(answer.mfa_token!, code);
        setCodes({ codes: r.recovery_codes ?? [], answer: r });
      } else {
        props.onDone(await accessApi.loginMfa(answer.mfa_token!, code));
      }
    } catch (err) {
      setError(errMsg(err));
      setCode("");
    } finally {
      setBusy(false);
    }
  };

  if (codes) {
    return (
      <div className="auth-card auth-card-wide">
        <h2>{t("Two-factor authentication is on")}</h2>
        <RecoveryCodes codes={codes.codes} />
        <button className="btn primary" autoFocus onClick={() => props.onDone(codes.answer)}>{t("Continue")}</button>
      </div>
    );
  }
  return (
    <form className={"auth-card" + (enrol ? " auth-card-wide" : "")} onSubmit={submit}>
      <h2>{t("Two-factor authentication")}</h2>
      {enrol ? (
        <>
          <p className="muted">{t("An administrator requires two-factor authentication for your account. Set it up now to sign in.")}</p>
          {answer.enrol && <EnrolmentInfo enrol={answer.enrol} />}
          <CodeField value={code} onChange={setCode} autoFocus label={t("Code shown by the app")} />
        </>
      ) : (
        <>
          <p className="muted">{useRecovery ? t("Enter one of your recovery codes. Each code works once.")
            : t("Enter the 6-digit code from your authenticator app.")}</p>
          <CodeField value={code} onChange={setCode} autoFocus recovery={useRecovery}
            label={useRecovery ? t("Recovery code") : t("Authentication code")} />
        </>
      )}
      <button className="btn primary" type="submit" disabled={busy || code.length < 6}>{busy ? t("Checking…") : enrol ? t("Turn on and sign in") : t("Verify")}</button>
      {!enrol && (
        <button type="button" className="btn link" onClick={() => { setUseRecovery(!useRecovery); setCode(""); setError(null); }}>
          {useRecovery ? t("Use the authenticator app") : t("Use a recovery code")}
        </button>
      )}
      <button type="button" className="btn link" onClick={props.onCancel}>{t("Back to sign-in")}</button>
      {error && <p className="err" role="alert">{error}</p>}
    </form>
  );
}
