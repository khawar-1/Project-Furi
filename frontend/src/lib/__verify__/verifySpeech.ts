/**
 * Executable checks for the two PURE voice modules — speechDigest and
 * wakePhrase.
 *
 * ⚠️ THIS IS NOT A TEST SUITE, AND THE DIFFERENCE IS WORTH KNOWING. The frontend
 * has no test runner at all (package.json has no vitest/jest; the project's gates
 * are `tsc --noEmit` and `vite build`), so the backend's 3800 tests have no
 * counterpart here. Rather than add a framework nobody asked for in the middle of
 * a bug-fix round, these modules — which are pure functions and the two places
 * this round's behaviour actually lives — get real, executed assertions:
 *
 *     cd frontend
 *     npx tsc src/lib/__verify__/verifySpeech.ts src/lib/speechDigest.ts \
 *         src/lib/wakePhrase.ts --outDir ../.verify --module commonjs \
 *         --target es2020 --skipLibCheck --lib es2020,dom
 *     node ../.verify/__verify__/verifySpeech.js
 *
 * Excluded from the app build by living under __verify__ and importing nothing
 * from the app; nothing imports it.
 */
import {
  MAX_SPOKEN_ITEMS,
  SPOKEN_LEAD_CHARS,
  SpeechDigest,
  classifyLine,
  digestWholeText,
  isDataLine,
} from '../speechDigest';
import { SpeechGate, encodeWav, matchesWakePhrase } from '../wakePhrase';

let failures = 0;
let checks = 0;

function check(name: string, condition: boolean, detail?: string): void {
  checks++;
  if (!condition) {
    failures++;
    console.log(`  FAIL  ${name}${detail ? `\n        ${detail}` : ''}`);
  }
}

function eq(name: string, got: unknown, want: unknown): void {
  const a = JSON.stringify(got);
  const b = JSON.stringify(want);
  check(name, a === b, `got  ${a}\n        want ${b}`);
}

function section(title: string): void {
  console.log(`\n--- ${title}`);
}

/** Run a whole streamed turn through the digest and collect what is spoken. */
function speakTurn(utterances: string[]): string[] {
  const digest = new SpeechDigest();
  const out: string[] = [];
  for (const u of utterances) out.push(...digest.push(u));
  const rest = digest.flush();
  if (rest) out.push(rest);
  return out;
}

// =====================================================================
section('speechDigest — line classification');

check('a dash bullet is data', isDataLine('- report-final.pdf — 4.2 MB'));
check('a numbered item is data', isDataLine('1. notes.txt'));
check('a bullet dot is data', isDataLine('• invoice.pdf'));
check('a table row is data', isDataLine('| File | Size |'));
check('a bare filename line is data', isDataLine('cloud-computing-notes.pdf'));

// Structure is dropped WITHOUT being counted — counting it made a 7-file answer
// say "all 8 are listed on screen", contradicting its own preceding sentence.
eq('a bold-only heading is structure', classifyLine('**D:\\Downloads**'), 'skip');
eq('a hash heading is structure', classifyLine('## Summary'), 'skip');
eq('a table rule is structure', classifyLine('|------|------|'), 'skip');
eq('a bullet is an item', classifyLine('- a.pdf'), 'item');
eq('prose is prose', classifyLine('I found seven files.'), 'prose');

check('ordinary prose is NOT data', !isDataLine('Found 7 matching files in Downloads.'));
check(
  'a sentence that MENTIONS a file is NOT data',
  !isDataLine('Report.pdf is ready for you.')
);
check(
  'a question is NOT data',
  !isDataLine('Would you like me to open the first one?')
);
check(
  'a sentence with a dash is NOT data',
  !isDataLine('I checked Downloads - nothing there matches.')
);

// =====================================================================
section('speechDigest — the reported defect: 52 PDFs read aloud');

const FILE_LIST = [
  'Found 7 matching files in Downloads.',
  '',
  '**D:\\Downloads**',
  '- report-final.pdf — 4.2 MB',
  '- invoice.pdf — 1.1 MB',
  '- cloud-computing-notes.pdf — 800 KB',
  '- resume-2026.pdf — 240 KB',
  '- bank-statement.pdf — 96 KB',
  '- ticket.pdf — 61 KB',
  '- manual.pdf — 8.4 MB',
].join('\n');

const spokenList = speakTurn([FILE_LIST]);
eq('the intro is spoken, the list is counted', spokenList, [
  'Found 7 matching files in Downloads.',
  'All 7 are listed on screen.',
]);
check(
  'no filename survives into speech',
  !spokenList.join(' ').includes('.pdf'),
  spokenList.join(' ')
);

// =====================================================================
section('speechDigest — a SHORT list is still the answer');

const shortList = speakTurn(['Two matched:\n- alpha.pdf\n- beta.pdf']);
eq('two items are read, not counted', shortList, ['Two matched:', 'alpha.pdf. beta.pdf']);
check(
  `the threshold is MAX_SPOKEN_ITEMS (${MAX_SPOKEN_ITEMS})`,
  speakTurn(['- a.pdf\n- b.pdf\n- c.pdf'])[0] === 'a.pdf. b.pdf. c.pdf'
);
check(
  'one past the threshold flips to a count',
  speakTurn(['- a.pdf\n- b.pdf\n- c.pdf\n- d.pdf'])[0] === 'All 4 are listed on screen.'
);

// =====================================================================
section('speechDigest — ordinary prose is untouched');

const prose = 'Your next meeting is the standup at 10:00, sir. Nothing else until three.';
eq('a plain reply passes through verbatim', speakTurn([prose]), [prose]);

// =====================================================================
section('speechDigest — the lead budget');

const long = Array.from(
  { length: 12 },
  (_, i) => `This is sentence number ${i} of a rather long explanation.`
);
const led = speakTurn(long);
check('a long answer is cut off', led[led.length - 1] === 'The rest is on screen.');
// The hand-off clause is a claim about the world, so it is only made when
// something really was withheld. A turn that ENDS on the sentence which crossed
// the budget has no rest to point at.
check(
  'a turn that ends exactly at the budget does not invent a rest',
  !speakTurn([
    'a'.repeat(SPOKEN_LEAD_CHARS + 10) + '.',
  ]).includes('The rest is on screen.'),
  JSON.stringify(speakTurn(['a'.repeat(SPOKEN_LEAD_CHARS + 10) + '.']))
);
check(
  'but one more sentence after the budget DOES earn the clause',
  speakTurn(['a'.repeat(SPOKEN_LEAD_CHARS + 10) + '.', 'And one more thing.']).includes(
    'The rest is on screen.'
  )
);
check(
  'the clause is said at most once',
  speakTurn(['a'.repeat(SPOKEN_LEAD_CHARS + 10) + '.', 'Two.', 'Three.', 'Four.']).filter(
    (s) => s === 'The rest is on screen.'
  ).length === 1
);
check(
  'the lead is a handful of sentences, not twelve',
  led.length < long.length,
  `spoke ${led.length} of ${long.length}`
);
check(
  'the first sentence is always spoken',
  led[0] === long[0],
  led[0]
);

// =====================================================================
section('speechDigest — ordering is preserved');

const mixed = speakTurn([
  'Here is what I found.',
  '- a.pdf\n- b.pdf\n- c.pdf\n- d.pdf\n- e.pdf',
  'Would you like me to open one?',
]);
eq('count lands between the intro and the question', mixed, [
  'Here is what I found.',
  'All 5 are listed on screen.',
  'Would you like me to open one?',
]);

// =====================================================================
section('speechDigest — whole-text digest (background task outcomes)');

check(
  'digestWholeText collapses a list the same way',
  digestWholeText(FILE_LIST) ===
    'Found 7 matching files in Downloads. All 7 are listed on screen.',
  digestWholeText(FILE_LIST)
);
check(
  'the spoken count agrees with the prose that precedes it',
  digestWholeText(FILE_LIST).includes('7 matching files') &&
    digestWholeText(FILE_LIST).includes('All 7'),
  digestWholeText(FILE_LIST)
);
eq('empty text stays empty', digestWholeText('   '), '');

// =====================================================================
section('wakePhrase — matching, against MEASURED transcripts');

// ⚠️ THE NUMBERS THIS RULE EXISTS FOR. Whisper transcribed the spoken word
// "Furi" as "Fury" in 9 of 9 measured passes across three model/language arms
// (2026-08-12) — a proper noun no speech model has been trained on comes back as
// its nearest real word. Exact matching on "furi" would NEVER have fired.
check('MEASURED: "Fury," matches the phrase "furi"', matchesWakePhrase('Fury,', 'furi'));
check(
  'MEASURED: the full sentence Whisper returned matches',
  matchesWakePhrase('Fury, turn the volume down a bit please.', 'furi')
);
check('the exact word matches', matchesWakePhrase('Furi', 'furi'));
check('punctuation and case are irrelevant', matchesWakePhrase('  hey, FURI!  ', 'furi'));
check('mid-sentence matches', matchesWakePhrase('um hey furi are you there', 'hey furi'));
check('a two-word phrase matches fuzzily', matchesWakePhrase('hey fury', 'hey furi'));
// HONEST LIMIT, asserted rather than hoped for: "furry" is TWO edits from
// "furi" (substitute r→i, delete y), so it does not fire. One edit is the
// deliberate bound — widening it to two would also admit "for" and "free".
check('"furry" is two edits away and does NOT match', !matchesWakePhrase('furry blanket', 'furi'));

check('an unrelated sentence does NOT match', !matchesWakePhrase('open the downloads folder', 'furi'));
check('"free" does NOT match "furi"', !matchesWakePhrase('are you free tomorrow', 'furi'));
check('"for" does NOT match "furi"', !matchesWakePhrase('this is for you', 'furi'));
check('an empty transcript does not match', !matchesWakePhrase('', 'furi'));
check('an empty phrase never matches', !matchesWakePhrase('anything at all', ''));
check(
  'a SHORT phrase must match exactly (one edit reaches a real word)',
  !matchesWakePhrase('can you see', 'cat')
);
check('order matters in a two-word phrase', !matchesWakePhrase('furi hey', 'hey furi'));

// =====================================================================
section('wakePhrase — WAV encoding');

const wav = encodeWav(new Float32Array([0, 0.5, -0.5, 1, -1]), 16000);
check('blob is audio/wav', wav.type === 'audio/wav');
check('44-byte header + 2 bytes per sample', wav.size === 44 + 5 * 2, `${wav.size}`);

// =====================================================================
section('wakePhrase — the speech gate');

function frames(rms: number, count: number, size = 320): Float32Array[] {
  // 320 samples @16k = 20ms per frame.
  return Array.from({ length: count }, () => {
    const f = new Float32Array(size);
    for (let i = 0; i < size; i++) f[i] = i % 2 === 0 ? rms : -rms;
    return f;
  });
}

function runGate(sequence: Float32Array[]): Float32Array[] {
  const got: Float32Array[] = [];
  const g = new SpeechGate(16000, (s) => got.push(s));
  for (const f of sequence) g.push(f);
  return got;
}

// 500ms of speech then 500ms of quiet → one utterance.
const spoken = runGate([...frames(0.2, 25), ...frames(0.0005, 30)]);
check('a short utterance is emitted once', spoken.length === 1, `${spoken.length}`);
check(
  'the pre-roll is included, so the first consonant survives',
  spoken.length === 1 && spoken[0].length > 25 * 320,
  spoken.length ? `${spoken[0].length} samples` : 'none'
);

check('silence alone emits nothing', runGate(frames(0.0005, 200)).length === 0);
check(
  'a single loud click emits nothing',
  runGate([...frames(0.4, 2), ...frames(0.0005, 40)]).length === 0
);
check(
  'a long burst (someone talking, not summoning) is dropped',
  runGate([...frames(0.2, 250), ...frames(0.0005, 40)]).length === 0
);
// Found by this check on its first run: dropping the over-long utterance only
// reset the counters, so the REST of the same sentence re-opened one and its
// tail was transcribed — a wake check every ~3s of continuous speech.
check(
  'the tail of a long burst is not chopped into a second utterance',
  runGate([
    ...frames(0.2, 250), // 5s of talking — over the cap
    ...frames(0.2, 40), //  ...still talking after the drop
    ...frames(0.0005, 40),
  ]).length === 0
);
check(
  'but the gate recovers: a real phrase after the pause still fires',
  runGate([
    ...frames(0.2, 250),
    ...frames(0.0005, 40), // they stop
    ...frames(0.2, 25), //   then say the wake word
    ...frames(0.0005, 30),
  ]).length === 1
);
check(
  'reset() drops a half-captured utterance',
  (() => {
    const got: Float32Array[] = [];
    const g = new SpeechGate(16000, (s) => got.push(s));
    for (const f of frames(0.2, 25)) g.push(f);
    g.reset();
    for (const f of frames(0.0005, 40)) g.push(f);
    return got.length === 0;
  })()
);

// =====================================================================
console.log(
  `\n${failures === 0 ? 'PASS' : 'FAIL'} — ${checks - failures}/${checks} checks passed`
);
process.exit(failures === 0 ? 0 : 1);
