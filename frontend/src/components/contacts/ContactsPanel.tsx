/**
 * Jarvis OS — Contacts Panel (Phase 2)
 * List view + detail view with interaction history.
 */
import { useEffect, useState } from 'react';
import { Users, Search, Plus, X, ChevronRight, RefreshCw, Trash2, Mail, Phone, Building, MessageSquare, Cake, Calendar, Code2 } from 'lucide-react';
import { clsx } from 'clsx';
import { useContactsStore } from '@/stores/contactsStore';
import type { ContactInteraction, RelationshipType } from '@/types';

const REL_COLORS: Record<string, string> = {
  client: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  friend: 'bg-green-500/20 text-green-300 border-green-500/30',
  colleague: 'bg-cyan-500/20 text-cyan-300 border-cyan-500/30',
  recruiter: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
  family: 'bg-pink-500/20 text-pink-300 border-pink-500/30',
  mentor: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  other: 'bg-slate-500/20 text-slate-300 border-slate-500/30',
};

function RelBadge({ type }: { type: RelationshipType | null }) {
  if (!type) return null;
  return (
    <span className={clsx('px-2 py-0.5 rounded-full text-xs font-medium border capitalize', REL_COLORS[type] ?? REL_COLORS.other)}>
      {type}
    </span>
  );
}

function Avatar({ name }: { name: string }) {
  const initials = name.split(' ').map((n) => n[0]).join('').toUpperCase().slice(0, 2);
  return (
    <div className="w-10 h-10 rounded-full bg-gradient-to-br from-cyan-500/30 to-purple-500/30 border border-cyan-500/20 flex items-center justify-center text-sm font-semibold text-cyan-300 flex-shrink-0">
      {initials}
    </div>
  );
}

// ============================================================
// Fact Log Row (with inline delete confirm)
// ============================================================
function FactLogRow({ interaction, onDelete }: { interaction: ContactInteraction; onDelete: (id: string) => void }) {
  const [confirming, setConfirming] = useState(false);
  const i = interaction;

  return (
    <div className="group flex gap-2 text-sm bg-surface-2 rounded-lg p-3 border border-surface-border hover:border-red-500/20 transition-colors">
      <div className="flex-1 min-w-0">
        <p className="text-slate-300 leading-relaxed">{i.description}</p>
        <div className="flex items-center gap-2 mt-1">
          <span className="text-[10px] uppercase font-bold text-cyan-500 bg-cyan-500/10 px-1.5 py-0.5 rounded">{i.category || 'other'}</span>
          <span className="text-xs text-muted">{new Date(i.interaction_date).toLocaleDateString()}</span>
        </div>
      </div>
      <div className="flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
        {confirming ? (
          <div className="flex gap-1">
            <button
              onClick={() => { onDelete(i.id); setConfirming(false); }}
              className="px-2 py-1 text-[10px] bg-red-500/20 text-red-400 rounded hover:bg-red-500/30 transition-colors"
            >
              Delete
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="px-2 py-1 text-[10px] bg-surface-2 text-slate-400 rounded hover:bg-surface-3 transition-colors"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            className="p-1.5 rounded-lg text-slate-600 hover:text-red-400 hover:bg-red-500/10 transition-colors"
            title="Delete fact"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}

// ============================================================
// Contact Detail View
// ============================================================
function ContactDetail() {
  const { selectedContact, clearSelected, deleteContact, deleteInteraction, isLoading } = useContactsStore();
  if (!selectedContact) return null;
  const c = selectedContact;

  const handleDelete = async () => {
    if (!confirm(`Delete contact ${c.name}?`)) return;
    await deleteContact(c.id);
    clearSelected();
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden border-l border-surface-border">
      {/* Header */}
      <div className="flex items-center gap-3 p-4 border-b border-surface-border">
        <button onClick={clearSelected} className="p-1 text-muted hover:text-slate-300 transition-colors">
          <ChevronRight className="w-4 h-4 rotate-180" />
        </button>
        <Avatar name={c.name} />
        <div className="flex-1 min-w-0">
          <h2 className="text-base font-semibold text-slate-200 truncate">{c.name}</h2>
          {c.organization && <p className="text-xs text-muted truncate">{c.organization}</p>}
        </div>
        <RelBadge type={c.relationship_type} />
        <button onClick={handleDelete} className="p-1.5 text-red-400/50 hover:text-red-400 transition-colors">
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      {/* Details */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Contact info */}
        <div className="space-y-2">
          {c.email && (
            <div className="flex items-center gap-2 text-sm">
              <Mail className="w-4 h-4 text-muted" />
              <span className="text-slate-300">{c.email}</span>
            </div>
          )}
          {c.phone && (
            <div className="flex items-center gap-2 text-sm">
              <Phone className="w-4 h-4 text-muted" />
              <span className="text-slate-300">{c.phone}</span>
            </div>
          )}
          {c.organization && (
            <div className="flex items-center gap-2 text-sm">
              <Building className="w-4 h-4 text-muted" />
              <span className="text-slate-300">{c.organization}</span>
            </div>
          )}
          {c.birthday && (
            <div className="flex items-center gap-2 text-sm">
              <Cake className="w-4 h-4 text-muted" />
              <span className="text-slate-300">Birthday: {c.birthday}</span>
            </div>
          )}
        </div>

        {/* Skills */}
        {c.skills && c.skills.length > 0 && (
          <div>
            <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-2 flex items-center gap-1.5">
              <Code2 className="w-3.5 h-3.5" /> Skills
            </h3>
            <div className="flex flex-wrap gap-1.5">
              {c.skills.map((s) => (
                <span key={s} className="px-2 py-0.5 rounded-md bg-blue-500/10 border border-blue-500/20 text-blue-300 text-xs font-mono">
                  {s}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Important Dates */}
        {c.important_dates && Object.keys(c.important_dates).length > 0 && (
          <div>
            <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-2 flex items-center gap-1.5">
              <Calendar className="w-3.5 h-3.5" /> Important Dates
            </h3>
            <div className="space-y-1">
              {Object.entries(c.important_dates).map(([k, v]) => (
                <div key={k} className="text-sm">
                  <span className="text-muted capitalize">{k}:</span> <span className="text-slate-300">{v as string}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Stats */}
        <div className="grid grid-cols-2 gap-3">
          <div className="bg-surface-2 rounded-lg p-3 border border-surface-border text-center">
            <p className="text-2xl font-bold text-cyan-400">{c.interaction_count}</p>
            <p className="text-xs text-muted mt-0.5">Interactions</p>
          </div>
          <div className="bg-surface-2 rounded-lg p-3 border border-surface-border text-center">
            <p className="text-sm font-medium text-slate-300">
              {c.last_interaction ? new Date(c.last_interaction).toLocaleDateString() : '—'}
            </p>
            <p className="text-xs text-muted mt-0.5">Last Seen</p>
          </div>
        </div>

        {/* Facts Log */}
        {c.interactions && c.interactions.length > 0 && (
          <div>
            <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-2">Facts Log</h3>
            <div className="space-y-2">
              {c.interactions.map((i) => (
                <FactLogRow
                  key={i.id}
                  interaction={i}
                  onDelete={(interactionId) => deleteInteraction(c.id, interactionId)}
                />
              ))}
            </div>
          </div>
        )}

        <p className="text-xs text-muted text-center pt-2">
          Added {new Date(c.created_at).toLocaleDateString()}
        </p>
      </div>
    </div>
  );
}

// ============================================================
// New Contact Form
// ============================================================
function AddContactModal({ onClose }: { onClose: () => void }) {
  const { createContact } = useContactsStore();
  const [form, setForm] = useState({ name: '', email: '', organization: '', relationship_type: 'other' as RelationshipType, birthday: '' });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.name.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await createContact(form);
      onClose();
    } catch (err: any) {
      const msg = err.response?.data?.detail || err.message || 'Failed to create contact';
      setError(msg);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-surface border border-surface-border rounded-2xl p-6 w-full max-w-md shadow-2xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-slate-200">Add Contact</h2>
          <button onClick={onClose} className="p-1 text-muted hover:text-slate-300"><X className="w-4 h-4" /></button>
        </div>
        {error && (
          <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
            {error}
          </div>
        )}
        <form onSubmit={handleSubmit} className="space-y-3">
          {[
            { field: 'name', label: 'Name *', placeholder: 'Full name' },
            { field: 'email', label: 'Email', placeholder: 'email@example.com' },
            { field: 'organization', label: 'Organization', placeholder: 'Company or team' },
          ].map(({ field, label, placeholder }) => (
            <div key={field}>
              <label className="block text-xs text-muted mb-1">{label}</label>
              <input
                value={(form as any)[field]}
                onChange={(e) => setForm({ ...form, [field]: e.target.value })}
                placeholder={placeholder}
                className="w-full bg-surface-2 border border-surface-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50"
              />
            </div>
          ))}
          <div>
            <label className="block text-xs text-muted mb-1">Birthday</label>
            <input
              type="date"
              value={form.birthday}
              onChange={(e) => setForm({ ...form, birthday: e.target.value })}
              className="w-full bg-surface-2 border border-surface-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50"
            />
          </div>
          <div>
            <label className="block text-xs text-muted mb-1">Relationship</label>
            <select
              value={form.relationship_type}
              onChange={(e) => setForm({ ...form, relationship_type: e.target.value as RelationshipType })}
              className="w-full bg-surface-2 border border-surface-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none"
            >
              {['friend', 'colleague', 'client', 'recruiter', 'mentor', 'family', 'other'].map(r => (
                <option key={r} value={r} className="capitalize">{r}</option>
              ))}
            </select>
          </div>
          <div className="flex gap-2 pt-2">
            <button
              type="submit"
              disabled={saving || !form.name.trim()}
              className="flex-1 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white text-sm rounded-lg transition-colors"
            >
              {saving ? 'Saving...' : 'Add Contact'}
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

// ============================================================
// Main Contacts Panel
// ============================================================
export function ContactsPanel() {
  const { contacts, selectedContact, isLoading, searchQuery, loadContacts, selectContact, setSearchQuery } = useContactsStore();
  const [showAdd, setShowAdd] = useState(false);

  useEffect(() => {
    loadContacts();
  }, [loadContacts]);

  const filtered = contacts.filter((c) =>
    c.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    c.organization?.toLowerCase().includes(searchQuery.toLowerCase()) ||
    c.relationship_type?.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-surface-border">
        <div className="flex items-center gap-2">
          <Users className="w-5 h-5 text-cyan-400" />
          <h1 className="text-lg font-semibold text-slate-200">Contacts</h1>
          <span className="text-xs text-muted bg-surface-2 border border-surface-border rounded-full px-2 py-0.5">
            {contacts.length}
          </span>
        </div>
        <div className="flex gap-2">
          <button onClick={loadContacts} className={clsx('p-1.5 text-muted hover:text-slate-300', isLoading && 'animate-spin')}>
            <RefreshCw className="w-4 h-4" />
          </button>
          <button
            onClick={() => setShowAdd(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-cyan-500/10 border border-cyan-500/30 text-cyan-400 rounded-lg text-sm hover:bg-cyan-500/20 transition-colors"
          >
            <Plus className="w-4 h-4" /> Add
          </button>
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* Contact List */}
        <div className={clsx('flex flex-col overflow-hidden', selectedContact ? 'w-72 flex-shrink-0' : 'flex-1')}>
          {/* Search */}
          <div className="p-3 border-b border-surface-border">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted" />
              <input
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search contacts..."
                className="w-full bg-surface-2 border border-surface-border rounded-lg pl-9 pr-3 py-2 text-sm text-slate-200 placeholder:text-muted focus:outline-none focus:border-cyan-500/50"
              />
            </div>
          </div>

          {/* List */}
          <div className="flex-1 overflow-y-auto p-3 space-y-1">
            {isLoading && <div className="text-center text-muted py-8 text-sm">Loading...</div>}
            {!isLoading && filtered.length === 0 && (
              <div className="text-center text-muted py-12">
                <Users className="w-10 h-10 mx-auto mb-3 opacity-30" />
                <p className="text-sm">No contacts yet.</p>
                <p className="text-xs mt-1">Mention people in chat to auto-add them.</p>
              </div>
            )}
            {filtered.map((c) => (
              <button
                key={c.id}
                onClick={() => selectContact(c.id)}
                className={clsx(
                  'w-full flex items-center gap-3 p-3 rounded-xl transition-all text-left',
                  selectedContact?.id === c.id
                    ? 'bg-cyan-500/10 border border-cyan-500/20'
                    : 'hover:bg-surface-2 border border-transparent'
                )}
              >
                <Avatar name={c.name} />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-slate-200 truncate">{c.name}</p>
                  <div className="flex items-center gap-1.5 mt-0.5">
                    {c.relationship_type && <RelBadge type={c.relationship_type} />}
                    {c.organization && <span className="text-xs text-muted truncate">{c.organization}</span>}
                  </div>
                </div>
                <ChevronRight className="w-4 h-4 text-muted flex-shrink-0" />
              </button>
            ))}
          </div>
        </div>

        {/* Detail Panel */}
        {selectedContact && <ContactDetail />}
      </div>

      {showAdd && <AddContactModal onClose={() => setShowAdd(false)} />}
    </div>
  );
}
