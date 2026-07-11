/**
 * Jarvis OS — birthday string helpers (Phase 5 Part 2).
 *
 * A birthday is stored canonically as "MM-DD" (year unknown — people say
 * "Jamil's birthday is March 4") or "YYYY-MM-DD" (year known). All parsing
 * and formatting here is manual string math: `new Date('YYYY-MM-DD')` parses
 * as UTC midnight and shifts the displayed day in western timezones.
 * validateBirthday mirrors the server rules in
 * backend/app/memory/contact_validation.py.
 */

export interface BirthdayParts {
  month: number | null;
  day: number | null;
  year: number | null;
}

export const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

// Year-less max day per month: Feb 29 is a real birthday (leap years exist).
const MAX_DAY = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

const MIN_BIRTH_YEAR = 1900;

const FULL_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const MONTH_DAY_RE = /^(\d{2})-(\d{2})$/;

function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

/** Decompose a canonical birthday string; anything else → all-null parts. */
export function parseBirthday(value: string | null | undefined): BirthdayParts {
  const empty: BirthdayParts = { month: null, day: null, year: null };
  if (!value) return empty;
  const full = FULL_RE.exec(value);
  if (full) {
    return { year: Number(full[1]), month: Number(full[2]), day: Number(full[3]) };
  }
  const monthDay = MONTH_DAY_RE.exec(value);
  if (monthDay) {
    return { year: null, month: Number(monthDay[1]), day: Number(monthDay[2]) };
  }
  return empty;
}

/** Compose parts into the canonical string; month+day incomplete → null. */
export function composeBirthday(parts: BirthdayParts): string | null {
  const { month, day, year } = parts;
  if (!month || !day) return null;
  const md = `${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  return year ? `${year}-${md}` : md;
}

/**
 * Error text for invalid parts, null when valid. All-null parts are valid
 * ("no birthday"). Mirrors normalize_birthday on the server.
 */
export function validateBirthday(parts: BirthdayParts): string | null {
  const { month, day, year } = parts;
  if (month === null && day === null && year === null) return null;
  if (!month || !day) return 'A birthday needs both a month and a day';
  if (month < 1 || month > 12) return 'Month must be between 1 and 12';
  if (day < 1 || day > MAX_DAY[month - 1]) {
    return `${MONTH_NAMES[month - 1] ?? 'That month'} has at most ${MAX_DAY[month - 1]} days`;
  }
  if (year !== null) {
    const now = new Date();
    if (year < MIN_BIRTH_YEAR || year > now.getFullYear()) {
      return `Year must be between ${MIN_BIRTH_YEAR} and ${now.getFullYear()}`;
    }
    if (month === 2 && day === 29 && !isLeapYear(year)) {
      return `${year} is not a leap year`;
    }
    const afterToday =
      year === now.getFullYear() &&
      (month > now.getMonth() + 1 || (month === now.getMonth() + 1 && day > now.getDate()));
    if (afterToday) return 'A birth date cannot be in the future';
  }
  return null;
}

/** "03-04" → "March 4"; "1990-03-04" → "March 4, 1990"; unparseable → as-is. */
export function formatBirthday(value: string): string {
  const { month, day, year } = parseBirthday(value);
  if (!month || !day || month < 1 || month > 12) return value;
  const monthDay = `${MONTH_NAMES[month - 1]} ${day}`;
  return year ? `${monthDay}, ${year}` : monthDay;
}
