import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

/** Detect Arabic script so chat bubbles / text blocks can flip direction. */
export function hasArabic(s: string): boolean {
  return /[؀-ۿ]/.test(s);
}

/** "2026-08-19T09:00:00" -> "09:00" (safe for null). */
export function isoTime(iso: string | null | undefined): string {
  return iso ? iso.slice(11, 16) : '';
}

/** Compact localized date-time, e.g. "18 Aug, 14:05". */
export function fmtDateTime(iso: string | null | undefined, lang: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(lang === 'ar' ? 'ar' : 'en-GB', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** YYYY-MM-DD key for a Date, in local time. */
export function dateKey(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** Local ISO datetime "YYYY-MM-DDTHH:MM:SS" from a Date. */
export function isoLocal(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${dateKey(d)}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
