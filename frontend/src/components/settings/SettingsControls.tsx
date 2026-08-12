/**
 * Jarvis OS — Settings design vocabulary
 *
 * ONE definition each for the switch, the status chip, the card shell and the
 * labelled control row. Before this file the panel carried SIX hand-copied
 * switches, six-plus hand-copied status chips and twelve hand-copied card
 * headers — the "a second copy of a fact is a hole" defect this project has
 * recorded seven times in the backend, reproduced in the UI. Two of those six
 * switches had drifted out of `role="switch"` entirely, so they were invisible
 * to a screen reader while looking identical on screen.
 *
 * Every control here is presentational: it owns no data and issues no request.
 * The cards keep their own state and their own PUTs.
 */
import { useId, useState } from 'react';
import { clsx } from 'clsx';
import { ChevronDown, Loader2 } from 'lucide-react';

/* ============================================================
   Toggle
   ============================================================ */

/**
 * ⚠️ THE OFF STATE MUST NOT LOOK LIT, AND THAT IS THE WHOLE POINT OF THIS
 * COMPONENT. Every previous copy drew the knob `bg-white` in BOTH states and
 * relied on the track alone to carry on/off. On a near-black surface (#0F0F17)
 * a pure-white dot is the highest-contrast thing on the control and reads as an
 * indicator LAMP, so a switch that was off still looked on — which is what the
 * user reported.
 *
 * Three cues now move together, so no single one has to carry the meaning:
 *   knob COLOUR    dim slate  ->  white
 *   knob POSITION  left       ->  right
 *   track          recessed   ->  lit cyan with a glow
 *
 * Geometry note: the off track uses `ring-inset` rather than `border`. A border
 * that exists in one state only changes the box by a pixel, so the knob would
 * visibly jump as it slid.
 */
export function Toggle({
  checked,
  disabled,
  onToggle,
  label,
}: {
  checked: boolean;
  disabled?: boolean;
  onToggle: () => void;
  /** Announced to screen readers — the visible text is often a sibling. */
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={onToggle}
      className={clsx(
        'group relative h-6 w-[42px] flex-shrink-0 rounded-full outline-none',
        'transition-colors duration-200 ease-out',
        'focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-surface-1',
        'disabled:cursor-not-allowed disabled:opacity-40',
        checked
          ? 'bg-cyan-500 shadow-[0_0_10px_rgba(6,182,212,0.45)]'
          : 'bg-surface-3 shadow-[inset_0_1px_2px_rgba(0,0,0,0.55)] ring-1 ring-inset ring-white/[0.07]'
      )}
    >
      <span
        className={clsx(
          'absolute top-1/2 h-4 w-4 -translate-y-1/2 rounded-full',
          'transition-[left,background-color,box-shadow] duration-200 ease-out',
          checked
            ? 'left-[22px] bg-white shadow-[0_1px_3px_rgba(0,0,0,0.45)]'
            : 'left-1 bg-slate-600 group-enabled:group-hover:bg-slate-500'
        )}
      />
    </button>
  );
}

/* ============================================================
   StatusPill
   ============================================================ */

export type PillTone = 'neutral' | 'on' | 'good' | 'busy' | 'warn' | 'bad';

const PILL_TONES: Record<PillTone, string> = {
  neutral: 'bg-surface-2 text-slate-500 ring-surface-border',
  on: 'bg-cyan-500/10 text-cyan-400 ring-cyan-500/25',
  good: 'bg-emerald-500/10 text-emerald-400 ring-emerald-500/25',
  busy: 'bg-amber-500/10 text-amber-400 ring-amber-500/25',
  warn: 'bg-amber-500/10 text-amber-400 ring-amber-500/25',
  bad: 'bg-red-500/10 text-red-400 ring-red-500/25',
};

/** The one status chip. `busy` carries its own spinner so no caller has to
 *  remember to add one. */
export function StatusPill({
  tone,
  children,
  className,
}: {
  tone: PillTone;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={clsx(
        'inline-flex flex-shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5',
        'font-mono text-[10px] leading-4 ring-1 ring-inset',
        PILL_TONES[tone],
        className
      )}
    >
      {tone === 'busy' && <Loader2 size={10} className="animate-spin" />}
      {children}
    </span>
  );
}

/* ============================================================
   SettingsCard
   ============================================================ */

/**
 * A collapsible card. THE BODY IS NOT RENDERED WHILE CLOSED, which is what
 * makes the panel legible: twelve features fit on one screen as twelve rows,
 * and the one you came for opens on a click.
 *
 * ⚠️ THE CARD COMPONENT ITSELF STAYS MOUNTED — only the body is conditional.
 * Every card polls for the status shown in its OWN header (files indexed, next
 * briefing, devices paired), so moving that work behind the open state would
 * leave the collapsed rows reporting nothing. The effects live in the card and
 * are untouched by this.
 *
 * The accessory (a Toggle, usually) is a SIBLING of the expand button, never a
 * child: a button inside a button is invalid HTML, and flipping a switch must
 * not also expand the card.
 */
export function SettingsCard({
  icon,
  title,
  summary,
  accessory,
  accent = 'text-cyan-400/80',
  defaultOpen = false,
  children,
}: {
  icon: React.ReactNode;
  title: React.ReactNode;
  /** One line under the title — say the current state, not the feature's pitch.
   *  It is the only thing visible while the card is closed. */
  summary: React.ReactNode;
  accessory?: React.ReactNode;
  accent?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();

  return (
    <div
      className={clsx(
        'overflow-hidden rounded-xl border bg-surface-1 transition-colors duration-200',
        open ? 'border-surface-border/90' : 'border-surface-border/60 hover:border-surface-border'
      )}
    >
      {/* ⚠️ THE CHEVRON IS LAST, AFTER THE ACCESSORY, and that ordering is
          measured rather than aesthetic. With the chevron at the end of the
          flex-1 button its position depended on the accessory's width, so the
          twelve chevrons landed across a 48px spread — a visibly ragged right
          edge. Chevron last pins both columns: accessories right-align against
          it, and every chevron sits the same distance from the card edge. */}
      <div className="flex items-center gap-2 pr-3">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={bodyId}
          className={clsx(
            'flex min-w-0 flex-1 items-center gap-3 rounded-xl py-3 pl-4 text-left outline-none',
            'transition-colors focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-400/50'
          )}
        >
          <div
            className={clsx(
              'flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg',
              'border border-surface-border bg-surface-2',
              accent
            )}
          >
            {icon}
          </div>

          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
            <p className="truncate text-xs text-muted">{summary}</p>
          </div>
        </button>

        {accessory && <div className="flex-shrink-0">{accessory}</div>}

        {/* A second expand target, because people do click the chevron. It is
            hidden from assistive tech and out of the tab order so it is not a
            duplicate control — the titled button above is the real one. */}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          tabIndex={-1}
          aria-hidden="true"
          className="flex h-7 w-6 flex-shrink-0 items-center justify-center rounded text-slate-600 transition-colors hover:text-slate-400"
        >
          <ChevronDown
            size={15}
            className={clsx('transition-transform duration-200', open && 'rotate-180')}
          />
        </button>
      </div>

      {open && (
        <div
          id={bodyId}
          className="animate-slide-up border-t border-surface-border px-4 py-4"
        >
          {children}
        </div>
      )}
    </div>
  );
}

/* ============================================================
   SettingsRow / SettingsSection
   ============================================================ */

/** A labelled control line: name + hint on the left, the control on the right.
 *  Generalised from the old `SubToggle` so it can host a switch, a select or a
 *  button without a second copy of the layout. */
export function SettingsRow({
  label,
  hint,
  control,
}: {
  label: React.ReactNode;
  hint?: React.ReactNode;
  control: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-1">
      <div className="min-w-0">
        <div className="text-xs text-slate-300">{label}</div>
        {hint && <div className="text-[11px] leading-relaxed text-slate-500">{hint}</div>}
      </div>
      <div className="flex-shrink-0">{control}</div>
    </div>
  );
}

/** A titled group of cards. The rule after the label gives the eye somewhere to
 *  stop — twelve cards in one undifferentiated column is most of why the panel
 *  read as cluttered. */
export function SettingsSection({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-3 px-1">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
          {label}
        </h2>
        <div className="h-px flex-1 bg-gradient-to-r from-surface-border to-transparent" />
      </div>
      <div className="space-y-2">{children}</div>
    </section>
  );
}
