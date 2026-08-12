/**
 * Furi OS — Contacts Store (Zustand)
 */
import { create } from 'zustand';
import type { Contact } from '@/types';
import { contactsApi } from '@/lib/api';

interface ContactsState {
  contacts: Contact[];
  selectedContact: Contact | null;
  isLoading: boolean;
  error: string | null;
  searchQuery: string;

  loadContacts: () => Promise<void>;
  selectContact: (id: string) => Promise<void>;
  clearSelected: () => void;
  createContact: (payload: Partial<Contact> & { name: string }) => Promise<void>;
  updateContact: (id: string, payload: Partial<Contact>) => Promise<void>;
  deleteContact: (id: string) => Promise<void>;
  deleteInteraction: (contactId: string, interactionId: string) => Promise<void>;
  setSearchQuery: (q: string) => void;
}

export const useContactsStore = create<ContactsState>((set, get) => ({
  contacts: [],
  selectedContact: null,
  isLoading: false,
  error: null,
  searchQuery: '',

  loadContacts: async () => {
    set({ isLoading: true, error: null });
    try {
      const contacts = await contactsApi.list();
      set({ contacts, isLoading: false });
    } catch (e) {
      set({ error: String(e), isLoading: false });
    }
  },

  selectContact: async (id: string) => {
    set({ isLoading: true });
    try {
      const contact = await contactsApi.get(id);
      set({ selectedContact: contact, isLoading: false });
    } catch (e) {
      set({ error: String(e), isLoading: false });
    }
  },

  clearSelected: () => set({ selectedContact: null }),

  createContact: async (payload) => {
    await contactsApi.create(payload);
    await get().loadContacts();
  },

  updateContact: async (id, payload) => {
    const updated = await contactsApi.update(id, payload);
    // Merge, don't replace: the PUT response has no `interactions`, and a
    // straight swap would blank the selected contact's fact log.
    set((state) => ({
      contacts: state.contacts.map((c) => (c.id === id ? { ...c, ...updated } : c)),
      selectedContact:
        state.selectedContact?.id === id
          ? { ...state.selectedContact, ...updated }
          : state.selectedContact,
    }));
  },

  deleteContact: async (id) => {
    await contactsApi.delete(id);
    set((state) => ({
      contacts: state.contacts.filter((c) => c.id !== id),
      selectedContact: state.selectedContact?.id === id ? null : state.selectedContact,
    }));
  },

  deleteInteraction: async (contactId, interactionId) => {
    await contactsApi.deleteInteraction(contactId, interactionId);
    set((state) => {
      const sel = state.selectedContact;
      if (!sel || sel.id !== contactId) return {};
      const updated = {
        ...sel,
        interactions: (sel.interactions ?? []).filter((i) => i.id !== interactionId),
        interaction_count: Math.max(0, sel.interaction_count - 1),
      };
      return {
        selectedContact: updated,
        contacts: state.contacts.map((c) => (c.id === contactId ? { ...c, interaction_count: updated.interaction_count } : c)),
      };
    });
  },

  setSearchQuery: (q) => set({ searchQuery: q }),
}));
