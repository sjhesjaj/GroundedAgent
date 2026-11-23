// Display helpers. Times from the API are business-time ISO strings with an
// offset (2026-11-12T11:00:00+08:00); they are shown as written, never shifted
// into the browser's zone, so the page and the server agree on the day.

/** "2026-11-12 11:00" */
export function dateTime(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 16).replace('T', ' ') : ''
}

/** "11-12 11:00" */
export function shortTime(iso: string | null | undefined): string {
  return iso ? iso.slice(5, 16).replace('T', ' ') : ''
}

/** "10:02:14" */
export function clockTime(iso: string | null | undefined): string {
  return iso ? iso.slice(11, 19) : ''
}

/** Amounts are decimal strings as stored ("99.00"); they are never re-computed here. */
export function money(amount: string | null | undefined): string {
  return amount ? `¥${amount}` : '—'
}

/** A long id shortened for a label; the full value stays in the title attribute. */
export function shortId(id: string | null | undefined): string {
  if (!id) return ''
  return id.length > 14 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id
}
