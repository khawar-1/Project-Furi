/**
 * Jarvis OS — About Me Panel
 * Displays user-specific facts extracted during conversations.
 * Facts can be searched, added manually, or deleted.
 */
import { useEffect, useState } from 'react';
import { User, Plus, Search, Trash2, Brain, RefreshCw, Tag, X } from 'lucide-react';
import { clsx } from 'clsx';
import { useMemoryStore } from '@/stores/memoryStore';
import type { MemoryCategory, SemanticMemory } from '@/types';

// ── Category colours ─────────────────────────────────────────────────────────
const CATEGORY_STYLES: Record<string, { bg: string; text: string; dot: string }> = {
  work:       { bg: 'bg-blue-500/10',    text: 'text-blue-400',    dot: 'bg-blue-400' },
  personal:   { bg: 'bg-purple-500/10',  text: 'text-purple-400',  dot: 'bg-purple-400' },
  preference: { bg: 'bg-amber-500/10',   text: 'text-amber-400',   dot: 'bg-amber-400' },
  skill:      { bg: 'bg-emerald-500/10', text: 'text-emerald-400', dot: 'bg-emerald-400' },
  fact:       { bg: 'bg-cyan-500/10',    text: 'text-cyan-400',    dot: 'bg-cyan-400' },
  other:      { bg: 'bg-slate-500/10',   text: 'text-slate-400',   dot: 'bg-slate-400' },
};

function getCategoryStyle(category: string | null) {
  return CATEGORY_STYLES[category ?? 'other'] ?? CATEGORY_STYLES.other;
}

// ── Confidence dots ───────────────────────────────────────────────────────────
function ConfidenceDots({ value }: { value: number }) {
  const filled = Math.round(value * 5);
  return (
    <div className="flex gap-0.5 items-center">
      {Array.from({ length: 5 }).map((_, i) => (
        <div
          key={i}
          className={clsx(
            'w-1 h-1 rounded-full transition-colors',
            i < filled ? 'bg-cyan-400' : 'bg-slate-700'
          )}
        />
      ))}
    </div>
  );
}

// ── Shared badge ──────────────────────────────────────────────────────────────
function SharedBadge() {
  return (
    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-500/15 text-indigo-400 border border-indigo-500/20 font-mono">
      shared
    </span>
  );
}

// ── Single fact card ──────────────────────────────────────────────────────────
function FactCard({ memory, onDelete }: { memory: SemanticMemory; onDelete: (id: string) => void }) {
  const [confirming, setConfirming] = useState(false);
  const style = getCategoryStyle(memory.category);
  const date = new Date(memory.created_at).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
  });

  return (
    <div className="group relative bg-surface-1 border border-surface-border rounded-xl p-4 hover:border-cyan-500/20 transition-all duration-200 hover:shadow-lg hover:shadow-cyan-500/5">
      {/* Left accent */}
      <div className={clsx('absolute left-0 top-3 bottom-3 w-0.5 rounded-r', style.dot)} />

      <div className="flex items-start gap-3">
        {/* Category dot */}
        <div className={clsx('flex-shrink-0 w-1.5 h-1.5 rounded-full mt-2', style.dot)} />

        <div className="flex-1 min-w-0">
          <p className="text-sm text-slate-200 leading-relaxed">{memory.content}</p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            {/* Category badge */}
            <span className={clsx('text-[10px] px-1.5 py-0.5 rounded-full font-mono border', style.bg, style.text, 'border-current/20')}>
              {memory.category ?? 'other'}
            </span>
            {/* Shared badge */}
            {memory.subject === 'shared' && <SharedBadge />}
            {/* Confidence */}
            <ConfidenceDots value={memory.confidence} />
            {/* Date */}
            <span className="text-[10px] text-muted">{date}</span>
          </div>
        </div>

        {/* Delete button */}
        <div className="flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
          {confirming ? (
            <div className="flex gap-1">
              <button
                onClick={() => { onDelete(memory.id); setConfirming(false); }}
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
              <Trash2 size={13} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Add fact form ─────────────────────────────────────────────────────────────
function AddFactForm({ onAdd, onClose }: { onAdd: (content: string, category: string) => void; onClose: () => void }) {
  const [content, setContent] = useState('');
  const [category, setCategory] = useState<MemoryCategory>('fact');
  const [saving, setSaving] = useState(false);

  const categories: MemoryCategory[] = ['fact', 'work', 'personal', 'preference', 'skill', 'other'];

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!content.trim()) return;
    setSaving(true);
    await onAdd(content.trim(), category);
    setSaving(false);
    onClose();
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-surface-1 border border-cyan-500/30 rounded-xl p-4 shadow-lg shadow-cyan-500/5"
    >
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-semibold text-cyan-400 tracking-wide uppercase">Add fact about you</span>
        <button type="button" onClick={onClose} className="text-slate-500 hover:text-slate-300 transition-colors">
          <X size={14} />
        </button>
      </div>
      <textarea
        autoFocus
        value={content}
        onChange={e => setContent(e.target.value)}
        placeholder="e.g. I prefer dark mode, I'm based in Karachi, I'm learning Rust..."
        className="w-full bg-surface-2 border border-surface-border rounded-lg p-3 text-sm text-slate-200 placeholder-slate-600 resize-none focus:outline-none focus:border-cyan-500/40 transition-colors"
        rows={3}
      />
      <div className="flex items-center gap-2 mt-3">
        <Tag size={12} className="text-slate-500" />
        <select
          value={category}
          onChange={e => setCategory(e.target.value as MemoryCategory)}
          className="text-xs bg-surface-2 border border-surface-border rounded-md px-2 py-1 text-slate-300 focus:outline-none focus:border-cyan-500/40"
        >
          {categories.map(c => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        <div className="flex-1" />
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={!content.trim() || saving}
          className="px-3 py-1.5 text-xs bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 rounded-lg hover:bg-cyan-500/30 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
      </div>
    </form>
  );
}

// ── Main component ────────────────────────────────────────────────────────────
export function MemoryExplorer() {
  const { memories, isLoading, searchQuery, error, loadFacts, searchFacts, addFact, deleteFact, setSearchQuery } = useMemoryStore();
  const [showAddForm, setShowAddForm] = useState(false);

  useEffect(() => {
    loadFacts();
  }, [loadFacts]);

  function handleSearchChange(q: string) {
    setSearchQuery(q);
    searchFacts(q);
  }

  // Group facts by category for the summary strip
  const categoryCounts = memories.reduce<Record<string, number>>((acc, m) => {
    const cat = m.category ?? 'other';
    acc[cat] = (acc[cat] ?? 0) + 1;
    return acc;
  }, {});

  const sharedCount = memories.filter(m => m.subject === 'shared').length;

  return (
    <div className="flex flex-col h-full bg-surface overflow-hidden">
      {/* ── Header ── */}
      <div className="flex-shrink-0 px-6 py-4 border-b border-surface-border bg-surface-1/50">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/20 flex items-center justify-center">
            <User size={16} className="text-cyan-400" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-200">About Me</h1>
            <p className="text-xs text-muted">
              {isLoading ? 'Loading...' : `${memories.length} fact${memories.length !== 1 ? 's' : ''} · ${sharedCount} shared`}
            </p>
          </div>
          <div className="flex-1" />
          <button
            onClick={loadFacts}
            disabled={isLoading}
            className="p-2 rounded-lg text-slate-500 hover:text-slate-300 hover:bg-surface-2 transition-colors disabled:opacity-40"
            title="Refresh"
          >
            <RefreshCw size={14} className={clsx(isLoading && 'animate-spin')} />
          </button>
          <button
            onClick={() => setShowAddForm(v => !v)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors"
          >
            <Plus size={13} />
            Add fact
          </button>
        </div>

        {/* Category summary chips */}
        {memories.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-3">
            {Object.entries(categoryCounts).map(([cat, count]) => {
              const style = getCategoryStyle(cat);
              return (
                <span
                  key={cat}
                  className={clsx('flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border border-current/20', style.bg, style.text)}
                >
                  <span className={clsx('w-1 h-1 rounded-full', style.dot)} />
                  {cat} · {count}
                </span>
              );
            })}
          </div>
        )}

        {/* Search bar */}
        <div className="relative">
          <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={e => handleSearchChange(e.target.value)}
            placeholder="Search facts..."
            className="w-full bg-surface-2 border border-surface-border rounded-lg pl-8 pr-3 py-2 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-cyan-500/30 transition-colors"
          />
          {searchQuery && (
            <button
              onClick={() => handleSearchChange('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
            >
              <X size={12} />
            </button>
          )}
        </div>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
        {/* Add form (inline) */}
        {showAddForm && (
          <AddFactForm
            onAdd={addFact}
            onClose={() => setShowAddForm(false)}
          />
        )}

        {/* Error */}
        {error && (
          <div className="p-4 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
            {error}
          </div>
        )}

        {/* Loading skeleton */}
        {isLoading && memories.length === 0 && (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-16 bg-surface-1 border border-surface-border rounded-xl animate-pulse" />
            ))}
          </div>
        )}

        {/* Empty state */}
        {!isLoading && memories.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-20 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-surface-2 border border-surface-border flex items-center justify-center">
              <Brain size={22} className="text-slate-600" />
            </div>
            <div>
              <p className="text-slate-400 text-sm font-medium mb-1">No facts yet</p>
              <p className="text-slate-600 text-xs max-w-xs">
                Chat with Jarvis and it will automatically learn things about you.<br />
                You can also add facts manually using the button above.
              </p>
            </div>
            <button
              onClick={() => setShowAddForm(true)}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 text-sm hover:bg-cyan-500/20 transition-colors"
            >
              <Plus size={14} />
              Add your first fact
            </button>
          </div>
        )}

        {/* Facts list */}
        {memories.map(memory => (
          <FactCard
            key={memory.id}
            memory={memory}
            onDelete={deleteFact}
          />
        ))}
      </div>
    </div>
  );
}
