/**
 * Jarvis OS — Chat State (Zustand)
 * Manages all conversation state: messages, streaming, session, provider.
 */
import { create } from 'zustand';
import { v4 as uuidv4 } from 'uuid';
import type { ChatMessage, LLMProviderName, StreamChunk } from '@/types';
import { chatApi } from '@/lib/api';

interface ChatState {
  // State
  messages: ChatMessage[];
  sessionId: string;
  isStreaming: boolean;
  streamingMessageId: string | null;
  activeProvider: LLMProviderName;
  error: string | null;
  draftMessage: string;

  // Actions
  sendMessage: (content: string) => Promise<void>;
  appendChunk: (chunk: StreamChunk) => void;
  clearConversation: () => void;
  setProvider: (provider: LLMProviderName) => void;
  setError: (error: string | null) => void;
  setDraftMessage: (message: string) => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  // ---- Initial State
  messages: [],
  sessionId: uuidv4(),
  isStreaming: false,
  streamingMessageId: null,
  activeProvider: 'gemini',
  error: null,
  draftMessage: '',

  // ---- Actions
  sendMessage: async (content: string) => {
    const { messages, sessionId, activeProvider } = get();

    // Add user message immediately
    const userMessage: ChatMessage = {
      id: uuidv4(),
      role: 'user',
      content,
      createdAt: new Date(),
    };

    // Add placeholder assistant message for streaming
    const assistantMessageId = uuidv4();
    const assistantPlaceholder: ChatMessage = {
      id: assistantMessageId,
      role: 'assistant',
      content: '',
      createdAt: new Date(),
      isStreaming: true,
    };

    set({
      messages: [...messages, userMessage, assistantPlaceholder],
      isStreaming: true,
      streamingMessageId: assistantMessageId,
      error: null,
    });

    // Build the message history for the request
    const history = [...messages, userMessage].map((m) => ({
      role: m.role,
      content: m.content,
    }));

    await chatApi.streamChat(
      {
        messages: history,
        session_id: sessionId,
        stream: true,
        provider: activeProvider,
      },
      // onChunk
      (chunk) => {
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantMessageId
              ? { ...m, content: m.content + chunk.delta }
              : m
          ),
        }));
      },
      // onDone
      (returnedSessionId) => {
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantMessageId
              ? { ...m, isStreaming: false }
              : m
          ),
          isStreaming: false,
          streamingMessageId: null,
          sessionId: returnedSessionId || state.sessionId,
        }));
      },
      // onError
      (error) => {
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantMessageId
              ? {
                  ...m,
                  content: m.content || `Error: ${error}`,
                  isStreaming: false,
                }
              : m
          ),
          isStreaming: false,
          streamingMessageId: null,
          error,
        }));
      }
    );
  },

  appendChunk: (chunk: StreamChunk) => {
    const { streamingMessageId } = get();
    if (!streamingMessageId) return;

    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === streamingMessageId
          ? { ...m, content: m.content + chunk.delta }
          : m
      ),
    }));
  },

  clearConversation: () => {
    set({
      messages: [],
      sessionId: uuidv4(),
      isStreaming: false,
      streamingMessageId: null,
      error: null,
    });
  },

  setProvider: (provider: LLMProviderName) => {
    set({ activeProvider: provider });
  },

  setError: (error: string | null) => {
    set({ error });
  },

  setDraftMessage: (message: string) => {
    set({ draftMessage: message });
  },
}));
