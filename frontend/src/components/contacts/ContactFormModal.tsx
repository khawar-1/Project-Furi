/**
 * Furi OS — Contact create/edit modal (Phase 5 Part 2).
 *
 * One form for both modes. Name is create-only: the engine's update path has
 * no rename support (a rename needs the create-path duplicate check), so edit
 * mode shows it read-only. Birthday is year-optional — month + day selects
 * plus an optional year — because people say "March 4" without a year;
 * composed/validated via lib/birthday.ts, mirroring the server rules.
 * In edit mode an emptied field is sent as "" — the API's clear semantics.
 */
import { useMemo, useState } from 'react';
import { X } from 'lucide-react';
import { useContactsStore } from '@/stores/contactsStore';
import type { Contact, RelationshipType } from '@/types';
import {
  BirthdayParts,
  MONTH_NAMES,
  composeBirthday,
  parseBirthday,
  validateBirthday,
} from '@/lib/birthday';

const RELATIONSHIPS: RelationshipType[] = [
  'friend', 'colleague', 'client', 'recruiter', 'mentor', 'family', 'other',
];

/** apiFetch throws Error("API Error 400: {json}") — surface the detail. */
function apiErrorDetail(err: unknown, fallback: string): string {
  const msg = err instanceof Error ? err.message : String(err);
  const jsonStart = msg.indexOf('{');
  if (jsonStart !== -1) {
    try {
      const detail = JSON.parse(msg.slice(jsonStart)).detail;
      if (typeof detail === 'string' && detail) return detail;
    } catch {
      // not a JSON body — fall through to the raw message
    }
  }
  return msg || fallback;
}

function BirthdayInput({
  parts,
  onChange,
  error,
}: {
  parts: BirthdayParts;
  onChange: (parts: BirthdayParts) => void;
  error: string | null;
}) {
  const toNumber = (raw: string): number | null => {
    if (raw.trim() === '') return null;
    const n = Number(raw);
    return Number.isInteger(n) ? n : null;
  };

  const inputClass =
    'bg-surface-2 border border-surface-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50';

  return (
    <div>
      <label className="block text-xs text-muted mb-1">Birthday</label>
      <div className="flex gap-2">
        <select
          value={parts.month ?? ''}
          onChange={(e) => onChange({ ...parts, month: toNumber(e.target.value) })}
          className={`flex-1 min-w-0 ${inputClass}`}
        >
          <option value="">Month</option>
          {MONTH_NAMES.map((name, i) => (
            <option key={name} value={i + 1}>{name}</option>
          ))}
        </select>
        <input
          value={parts.day ?? ''}
          onChange={(e) => onChange({ ...parts, day: toNumber(e.target.value.replace(/\D/g, '')) })}
          placeholder="Day"
          inputMode="numeric"
          maxLength={2}
          className={`w-16 ${inputClass}`}
        />
        <input
          value={parts.year ?? ''}
          onChange={(e) => onChange({ ...parts, year: toNumber(e.target.value.replace(/\D/g, '')) })}
          placeholder="Year (optional)"
          inputMode="numeric"
          maxLength={4}
          className={`w-28 ${inputClass}`}
        />
      </div>
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </div>
  );
}

export function ContactFormModal({
  mode,
  initial,
  onClose,
}: {
  mode: 'create' | 'edit';
  initial?: Contact;
  onClose: () => void;
}) {
  const { createContact, updateContact } = useContactsStore();
  const [form, setForm] = useState({
    name: initial?.name ?? '',
    email: initial?.email ?? '',
    phone: initial?.phone ?? '',
    organization: initial?.organization ?? '',
    relationship_type: (initial?.relationship_type ?? 'other') as RelationshipType,
  });
  const [birthdayParts, setBirthdayParts] = useState<BirthdayParts>(() =>
    parseBirthday(initial?.birthday)
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const birthdayError = useMemo(() => validateBirthday(birthdayParts), [birthdayParts]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mode === 'create' && !form.name.trim()) return;
    if (birthdayError) return;
    setSaving(true);
    setError(null);
    const birthday = composeBirthday(birthdayParts) ?? '';
    try {
      if (mode === 'create') {
        await createContact({ ...form, birthday });
      } else {
        // Name excluded — no rename path; "" clears a field server-side
        const { name: _name, ...editable } = form;
        await updateContact(initial!.id, { ...editable, birthday });
      }
      onClose();
    } catch (err) {
      setError(apiErrorDetail(err, `Failed to ${mode === 'create' ? 'create' : 'update'} contact`));
    } finally {
      setSaving(false);
    }
  };

  const textFields: { field: 'name' | 'email' | 'phone' | 'organization'; label: string; placeholder: string }[] = [
    { field: 'name', label: 'Name *', placeholder: 'Full name' },
    { field: 'email', label: 'Email', placeholder: 'email@example.com' },
    { field: 'phone', label: 'Phone', placeholder: 'Phone number' },
    { field: 'organization', label: 'Organization', placeholder: 'Company or team' },
  ];

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-surface border border-surface-border rounded-2xl p-6 w-full max-w-md shadow-2xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-slate-200">
            {mode === 'create' ? 'Add Contact' : `Edit ${initial?.name ?? 'Contact'}`}
          </h2>
          <button onClick={onClose} className="p-1 text-muted hover:text-slate-300"><X className="w-4 h-4" /></button>
        </div>
        {error && (
          <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
            {error}
          </div>
        )}
        <form onSubmit={handleSubmit} className="space-y-3">
          {textFields.map(({ field, label, placeholder }) => (
            <div key={field}>
              <label className="block text-xs text-muted mb-1">{label}</label>
              <input
                value={form[field]}
                onChange={(e) => setForm({ ...form, [field]: e.target.value })}
                placeholder={placeholder}
                disabled={field === 'name' && mode === 'edit'}
                title={field === 'name' && mode === 'edit' ? 'Renaming is not supported yet' : undefined}
                className="w-full bg-surface-2 border border-surface-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50 disabled:opacity-50 disabled:cursor-not-allowed"
              />
            </div>
          ))}
          <BirthdayInput parts={birthdayParts} onChange={setBirthdayParts} error={birthdayError} />
          <div>
            <label className="block text-xs text-muted mb-1">Relationship</label>
            <select
              value={form.relationship_type}
              onChange={(e) => setForm({ ...form, relationship_type: e.target.value as RelationshipType })}
              className="w-full bg-surface-2 border border-surface-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none"
            >
              {RELATIONSHIPS.map((r) => (
                <option key={r} value={r} className="capitalize">{r}</option>
              ))}
            </select>
          </div>
          <div className="flex gap-2 pt-2">
            <button
              type="submit"
              disabled={saving || (mode === 'create' && !form.name.trim()) || !!birthdayError}
              className="flex-1 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white text-sm rounded-lg transition-colors"
            >
              {saving ? 'Saving...' : mode === 'create' ? 'Add Contact' : 'Save Changes'}
            </button>
            <button type="button" onClick={onClose} className="px-4 py-2 text-muted hover:text-slate-300 text-sm">
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
