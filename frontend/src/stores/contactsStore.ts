/**
 * Jarvis OS — Contacts Store (Zustand)
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
    set((state) => ({
      contacts: state.contacts.map((c) => (c.id === id ? updated : c)),
      selectedContact: state.selectedContact?.id === id ? updated : state.selectedContact,
    }));
  },

  deleteContact: async (id) => {
    await contactsApi.delete(id);
    set((state) => ({
      contacts: state.contacts.filter((c) => c.id !== id),
      selectedContact: state.selectedContact?.id === id ? null : state.selectedContact,
    }));
  },

  setSearchQuery: (q) => set({ searchQuery: q }),
}));
