/** Keys with a context: ``"Line@@chart"`` is "Line" in English, translated apart from "Line" (a line number)
 *  in the catalogs, so one English word with two meanings gets two translations. */
const CONTEXT = /@@[\w-]+$/;

/** The English text of a key: the key without its context. */
export function untranslated(key: string): string {
  return key.replace(CONTEXT, "");
}
