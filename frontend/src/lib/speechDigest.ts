/**
 * Jarvis OS — Spoken form of a reply (what Jarvis SAYS, not what it WRITES)
 *
 * A written reply and a spoken one are different artefacts. On screen, "here are
 * the 52 PDFs" is best answered with 52 lines; read aloud it is 52 filenames in
 * a row, which nobody can hold and nobody wants. Reported live: *"if i ask it to
 * tell me all the pdf's in some directoty it literally speaks all the names of
 * pdfs, intead it should only say sir i have listed all the pdfs please see
 * them"*.
 *
 * ⚠️ THIS IS NOT sanitize_for_speech, AND THE SPLIT IS DELIBERATE.
 * `voice_tts.sanitize_for_speech` (backend) answers "how is this CHARACTER
 * spoken?" — a fence becomes "(code omitted)", a link speaks its label, a path
 * speaks its basename. It is per-utterance and stateless, which is exactly right
 * for its job and exactly wrong for this one. This module answers "does this
 * LINE belong in speech at all, and has this turn said enough?" — which needs the
 * whole turn in view, because a reply arrives one delta at a time and a list can
 * be spread over a dozen of them. Two different questions; the backend one still
 * runs afterwards on whatever survives here.
 *
 * TWO RULES, both deterministic — no LLM, no extra latency, same input → same
 * speech every time:
 *
 *   1. DATA IS COUNTED, NOT READ.  Bullets, table rows and bare filename lines
 *      are things to LOOK at. Past MAX_SPOKEN_ITEMS they collapse to one clause
 *      naming how many. At or below it they are read normally — a three-item
 *      answer IS the answer, and hiding it would be worse than reading it.
 *      (The threshold is `spoken.py`'s MAX_SPOKEN_NAMES, the same judgement the
 *      approval contract already makes about filenames.)
 *
 *   2. PROSE GETS A LEAD, NOT A LECTURE.  Past SPOKEN_LEAD_CHARS the turn stops
 *      and says so. Nothing is hidden: the full text is in the chat, and the
 *      clause says where to find it.
 *
 * FAILURE DIRECTION: every classifier here is CONSERVATIVE. A data line that
 * reads as prose is simply spoken — the behaviour before this module existed.
 * Only lines that are unmistakably not sentences are ever withheld.
 */

/** Read this many list items aloud; past it, say how many there are instead.
 *  Matches `spoken.py`'s MAX_SPOKEN_NAMES — the same call about the same kind of
 *  content, so the two must not drift. */
export const MAX_SPOKEN_ITEMS = 3;

/** A turn speaks about this much prose before handing off to the screen. ~400
 *  chars is 60-70 words — around 25 seconds aloud, which is a long turn in a
 *  conversation and a short one on a page. */
export const SPOKEN_LEAD_CHARS = 400;

/** Said once when rule 2 stops a long answer. */
export const REST_ON_SCREEN = 'The rest is on screen.';

// ------------------------------------------------------------- line shapes

/** `- item`, `* item`, `1. item`, `2) item`, `• item`. */
const BULLET_RE = /^\s{0,8}(?:[-*+•‣]|\d{1,3}[.)])\s+\S/;
/**
 * A markdown table row — LEADING PIPE REQUIRED.
 *
 * ⚠️ IT USED TO ALSO MATCH ANY LINE WITH TWO PIPES, which swallowed
 * `search_files`' own aggregate line: `Largest: report.pdf | Newest: invoice.pdf
 * | Total: 18.9 MB`. That is the most useful sentence in the whole result and it
 * was being counted as an anonymous list item — inflating the count as well as
 * losing the content. Every table this app renders leads with a pipe
 * (rendering.py writes them), so requiring one costs nothing.
 */
const TABLE_RE = /^\s*\|/;
/** A table's `|---|:--:|` rule. */
const TABLE_RULE_RE = /^\s*\|?[\s:|-]+\|[\s:|-]*$/;
/** A line that is ONLY a heading in bold or hashes — a group label, not speech. */
const HEADING_ONLY_RE = /^\s*(?:#{1,6}\s+.*|\*\*[^*]+\*\*|__[^_]+__)\s*$/;
/** A path, or a bare `name.ext`, optionally trailed by a size/date/count. A
 *  filename on a line of its own is a thing to look at, not a sentence. */
const FILE_LINE_RE =
  /^\s*(?:[-*+•]\s*)?(?:[A-Za-z]:[\\/]|~[\\/]|\.{0,2}[\\/])?[\w .\-()[\]]+\.[A-Za-z0-9]{1,6}\s*(?:[—–\-|,(]\s*[\w .,:/]{0,40}\)?)?\s*$/;
/** Sentence-shaped: contains a verb-ish gap and ends like a sentence. Used only
 *  as a VETO on the filename rule, so "Report.pdf is ready." stays prose. */
const SENTENCE_TAIL_RE = /[.!?]["')\]]?\s*$/;

/**
 * What is this line, for speech?
 *
 *   'prose' — say it.
 *   'item'  — one of a list; counted, and read only if the list is short.
 *   'skip'  — structure (a heading, a table rule). Neither said nor counted.
 *
 * ⚠️ 'skip' EXISTS BECAUSE COUNTING STRUCTURE PRODUCES A WRONG NUMBER. Treating
 * the group heading `**D:\Downloads**` as an item made a 7-file answer say "all
 * 8 are listed on screen", one line after prose that had just said seven — the
 * kind of small contradiction that makes someone stop trusting the whole thing.
 *
 * Deliberately narrow overall. Anything it is unsure about is prose, because
 * speaking a line that could have been withheld costs a few seconds, while
 * withholding a line that mattered costs the answer.
 */
export function classifyLine(line: string): 'prose' | 'item' | 'skip' {
  const text = line.trim();
  if (!text) return 'skip';
  // Structure first: a heading that happens to look like a filename
  // ("**notes.txt**") must read as structure, not as an item.
  if (TABLE_RULE_RE.test(text)) return 'skip';
  if (HEADING_ONLY_RE.test(text)) return 'skip';
  if (TABLE_RE.test(text)) return 'item';
  if (BULLET_RE.test(text)) return 'item';
  // A bare filename/path line, but never a sentence that merely mentions one.
  if (FILE_LINE_RE.test(text) && !SENTENCE_TAIL_RE.test(text)) return 'item';
  return 'prose';
}

/** Convenience for callers that only care whether a line is speech. */
export function isDataLine(line: string): boolean {
  return classifyLine(line) !== 'prose';
}

/** The words of a data line, for the ≤MAX_SPOKEN_ITEMS case where they ARE read.
 *  Strips the bullet marker so "- notes.txt" is spoken "notes.txt". */
export function dataLineText(line: string): string {
  return line.trim().replace(/^\s{0,8}(?:[-*+•‣]|\d{1,3}[.)])\s+/, '').trim();
}

/**
 * The leading whole sentences of `text` that fit in `budget` — at least one,
 * always, so a single long sentence is never cut mid-word.
 *
 * ⚠️ NOT A SECOND COPY OF `SentenceSegmenter`, and the difference is the job.
 * That class exists for LATENCY: it decides the earliest point at which speech
 * can start, which is why it has a first-clause fast path, a soft cut and a
 * rule about never splitting an open code fence. This decides where to STOP, on
 * text that has already fully arrived. Sharing one would mean giving the
 * streaming path a budget it must not have, or giving this one a fast path that
 * means nothing here.
 */
function leadingSentences(text: string, budget: number): string {
  const parts = text.match(/[^.!?]+(?:[.!?]+["')\]]*|$)/g);
  if (!parts || parts.length === 0) return text;
  let out = '';
  for (const part of parts) {
    if (out && out.length + part.length > budget) break;
    out += part;
  }
  return (out || parts[0]).trim();
}

/** "3 items are listed on screen." — the clause that replaces a long list.
 *  Never repeats a count the surrounding prose has already given; it just names
 *  where to look, which is the part the user actually asked for. */
export function itemsClause(count: number): string {
  return count === 1
    ? 'One more item is on screen.'
    : `All ${count} are listed on screen.`;
}

// ---------------------------------------------------------------- the digest

/**
 * Turn-scoped filter between the sentence segmenter and the speech queue.
 *
 * Mirrors `SentenceSegmenter`'s shape on purpose — `push()` returns zero or more
 * utterances to speak, `flush()` returns whatever the end of the turn owes — so
 * `voiceOutput` composes the two without either knowing the other's internals.
 *
 * ⚠️ ORDER IS PRESERVED. Pending items are emitted BEFORE the next prose, so a
 * reply that reads "here are the files … <list> … want me to open one?" speaks
 * the intro, then the count, then the question, in that order.
 */
export class SpeechDigest {
  /** Data lines seen since the last flush of them. Text is kept only while the
   *  run could still be read verbatim (≤ MAX_SPOKEN_ITEMS); past that a count
   *  is all that will be said, so the text is dropped rather than accumulated. */
  private pending: string[] = [];
  private pendingCount = 0;
  /** Prose characters spoken this turn — rule 2's budget. */
  private spokenChars = 0;
  /** The lead budget is spent; nothing further is spoken this turn. */
  private stopped = false;
  /**
   * ⚠️ THE HAND-OFF CLAUSE IS SAID ONLY WHEN SOMETHING WAS REALLY WITHHELD.
   * Announcing it the moment the budget is spent means a turn that happens to
   * END on the sentence that crossed the line says "the rest is on screen" about
   * a rest that does not exist — a small lie, and the kind this codebase spends
   * most of its length preventing. So it is emitted lazily: either when a block
   * is genuinely cut short, or when the NEXT piece of the turn is suppressed.
   */
  private restAnnounced = false;

  push(utterance: string): string[] {
    if (this.stopped) {
      // Something more did arrive, so there really is a rest.
      if (!this.restAnnounced && utterance.trim()) {
        this.restAnnounced = true;
        return [REST_ON_SCREEN];
      }
      return [];
    }
    const out: string[] = [];
    const lines = utterance.split('\n');
    let prose: string[] = [];

    const flushProse = () => {
      const text = prose.join(' ').trim();
      prose = [];
      if (!text) return;
      // Pending data comes first: it belongs to the text ALREADY spoken.
      const items = this.takePending();
      if (items) out.push(items);
      if (this.stopped) return;
      const remaining = SPOKEN_LEAD_CHARS - this.spokenChars;
      // ⚠️ CLIP INSIDE THE BLOCK, not just between blocks. A STREAMED turn
      // arrives one sentence at a time, so checking the budget after each was
      // enough — but a background task's outcome arrives WHOLE, and that path
      // spoke a 939-character explanation in full and then politely added "the
      // rest is on screen". The budget has to be able to cut the block it is
      // given, or it only ever applies to text that was already short.
      const spoken = text.length > remaining ? leadingSentences(text, remaining) : text;
      out.push(spoken);
      this.spokenChars += spoken.length;
      if (spoken.length < text.length) {
        // This block really was cut — say so now, in the right place.
        this.stopped = true;
        this.restAnnounced = true;
        out.push(REST_ON_SCREEN);
      } else if (this.spokenChars >= SPOKEN_LEAD_CHARS) {
        // Budget spent on a whole block. Whether there IS a rest depends on
        // what arrives next, so the clause waits (see `restAnnounced`).
        this.stopped = true;
      }
    };

    for (const line of lines) {
      if (!line.trim()) continue;
      const kind = classifyLine(line);
      if (kind === 'skip') continue; // structure: neither said nor counted
      if (kind === 'item') {
        // A run of items closes the prose before it, so ordering survives.
        flushProse();
        if (this.stopped) return out;
        this.pendingCount += 1;
        if (this.pendingCount <= MAX_SPOKEN_ITEMS) {
          this.pending.push(dataLineText(line));
        } else {
          this.pending = []; // a count is all that will be said now
        }
      } else {
        prose.push(line.trim());
      }
    }
    flushProse();
    return out;
  }

  /** End of turn: whatever the pending data owes. */
  flush(): string | null {
    if (this.stopped) return null;
    return this.takePending();
  }

  /** Short run → read the items; long run → say how many. */
  private takePending(): string | null {
    if (this.pendingCount === 0) return null;
    const count = this.pendingCount;
    const items = this.pending;
    this.pendingCount = 0;
    this.pending = [];
    if (count <= MAX_SPOKEN_ITEMS && items.length === count) {
      // Few enough to be the answer itself — read them as one utterance.
      return items.join('. ');
    }
    return itemsClause(count);
  }
}

/**
 * One-shot digest for text that arrives whole rather than streamed — a task
 * outcome pushed from the backend, a proactive announcement. Same two rules; the
 * budget simply has no future deltas to apply to.
 */
export function digestWholeText(text: string): string {
  const digest = new SpeechDigest();
  const parts = digest.push(text);
  const rest = digest.flush();
  if (rest) parts.push(rest);
  return parts.join(' ').trim();
}
