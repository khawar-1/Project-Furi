/**
 * Furi OS — Spoken approval, client side (2026-08-03, Tier 2 item 8)
 *
 * ⚠️ THIS FILE DECIDES NOTHING ABOUT CONSENT. It remembers which contract was
 * read aloud, and it forwards what the user said. The BACKEND decides whether
 * the words are consent (`spoken.is_spoken_approval`), whether the setting
 * allows it (`plan_needs_screen`), and whether the approval is bound to the
 * steps still pending (`AgentPlan.contract_hash`). All three run server-side
 * on purpose: a word set living here would be untestable and unfalsifiable,
 * and a client-side gate is not a gate.
 *
 * What it DOES guarantee is the honest half of the binding: the hash it sends
 * is one it was GIVEN, with the contract, moments ago. A stale one is refused
 * by the server with the plan untouched.
 */
import { agentApi } from '@/lib/api';
import type { AgentPlan } from '@/types';

/**
 * How long a spoken contract stays answerable. Short on purpose — "approve"
 * said two minutes after hearing what it applies to is not consent to that
 * thing any more, it is a coincidence.
 */
const CONTRACT_TTL_MS = 90_000;

type SpokenContract = { planId: string; hash: string; at: number };

let current: SpokenContract | null = null;

/** Called when a contract is actually READ ALOUD — never merely received. */
export function rememberSpokenContract(planId: string, hash: string): void {
  if (!planId || !hash) return;
  current = { planId, hash, at: Date.now() };
}

export function clearSpokenContract(): void {
  current = null;
}

function live(): SpokenContract | null {
  if (!current) return null;
  if (Date.now() - current.at > CONTRACT_TTL_MS) {
    current = null;
    return null;
  }
  return current;
}

export type SpokenApprovalResult =
  /** No contract was spoken recently — this utterance is an ordinary message. */
  | { kind: 'none' }
  /** The server accepted it; the plan ran. The plan is returned so the caller
   *  can apply it to the chat store and speak its outcome — a voice approval
   *  has no card to fall back on. */
  | { kind: 'approved'; plan: AgentPlan }
  /** The server refused (not consent / not allowed / stale). The caller must
   *  fall through to the normal chat path, where the words become a steer or
   *  the typed-approval nudge — never silently swallowed. */
  | { kind: 'refused'; detail: string };

/**
 * Offer a VOICE transcript as approval of the contract just read aloud.
 *
 * Only ever called for voice-originated turns: a typed "approve" keeps going
 * to the chat path and its nudge, because typing happens at a screen where the
 * card is right there.
 */
export async function tryApproveByVoice(
  utterance: string
): Promise<SpokenApprovalResult> {
  const contract = live();
  if (!contract) return { kind: 'none' };
  try {
    const plan = await agentApi.approveSpoken(
      contract.planId,
      contract.hash,
      utterance
    );
    current = null; // consumed — one contract, one approval
    return { kind: 'approved', plan };
  } catch (e) {
    // Deliberately NOT cleared: a refusal means nothing ran and the contract
    // is still the live one, so the user can simply say it properly.
    return { kind: 'refused', detail: e instanceof Error ? e.message : String(e) };
  }
}
