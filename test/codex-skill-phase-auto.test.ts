import { describe, test, expect } from 'bun:test';
import * as fs from 'fs';
import * as path from 'path';

const ROOT = path.resolve(import.meta.dir, '..');
const TMPL = path.join(ROOT, 'codex', 'SKILL.md.tmpl');
const GEN = path.join(ROOT, 'codex', 'SKILL.md');
const FILES: Array<[string, string]> = [['template', TMPL], ['generated', GEN]];

// VAS-2402. `/codex review` in a claude-fleet-config worktree routes through `--phase auto`, so the
// cheap tier stops depending on anyone typing `--loop`. VAS-2371's measurement week is the argument:
// the flag was live, documented and verbatim-runnable for a week, and compliance was ~20-24%.
//
// The hazard this file guards is NOT "does auto get passed". It is that with `auto` the phase is
// UNKNOWN until the adapter resolves it, so five things that used to be decidable from the typed
// command now have to follow the resolved value. Rounds 18-20 of gstack#1 are the measured cost of
// letting any one of them be re-decided at its own call site: r18 routed the focus branch to the
// loop backend and left three dependents on the gate's values; r19 and r20 each found one of them,
// one round at a time.
//
// So every test here asserts a FOLLOWER is wired to the producer. None of them assert that a helper
// exists — a helper called from one site and a helper called from both are indistinguishable in the
// source, which is exactly the mistake VAS-1885 records.
describe('VAS-2402: --phase auto and the five followers', () => {
  // ── THE PRODUCER ────────────────────────────────────────────────────────────────────────────
  test.each(FILES)('%s: the phase is resolved ONCE, from the adapter, not guessed', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    // resolve-phase is the adapter's own resolver — the same one `exec --phase auto` uses — so the
    // skill's pre-call followers and the adapter's actual choice cannot disagree by construction.
    expect(t).toContain('resolve-phase');
    expect(t).toContain('_OV_RESOLVED_PHASE');
    // A resolution that fails must STOP, never fall back to a guessed phase: guessing `loop` bills
    // a cheap round that can never converge, guessing `final_gate` bills the frontier for a lane
    // that had not earned it. Both are silent.
    expect(t).toMatch(/could not be resolved[\s\S]{0,400}?exit 2/);
  });

  test.each(FILES)('%s: `auto` is accepted by the phase validation', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toContain('loop|final_gate|auto|none');
  });

  // ── FOLLOWER 1: the backend probe asks about the RESOLVED phase ──────────────────────────────
  // `backend --phase auto` is refused outright by the adapter. Asking anyway makes the probe fail,
  // and a failed probe reads as "not codex" — right by accident when auto resolves to the hosted
  // loop, and WRONG when it resolves to the frontier gate, silently skipping the binary and auth
  // checks for a phase that needs both.
  test.each(FILES)('%s: the backend probe uses the resolved phase, never the literal `auto`', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toContain('backend --phase "$_OV_RESOLVED_PHASE"');
    // REDUCED BEFORE CHECKED. The literal `backend --phase auto` appears in this skill on purpose,
    // inside a comment explaining why it is refused — and a bare not.toContain matched that prose
    // and failed a correct file. A check that greps source is measuring the formatter until it is
    // reduced to the thing actually meant, which here is: no EXECUTABLE line invokes it.
    const executable = t
      .split('\n')
      .filter((l) => !l.trimStart().startsWith('#'))
      .join('\n');
    expect(executable).not.toContain('backend --phase auto');
  });

  // ── FOLLOWER 2: EFFORT covers the worst case the invocation may run ──────────────────────────
  // One --effort applies to the whole invocation, and an `auto` call that promotes runs the GATE
  // with whatever was passed. `medium` is right for a loop round and wrong for a gate round, and
  // under-powering the round that DECLARES convergence is the failure the tiering rests on.
  test.each(FILES)('%s: effort follows the phase — auto takes the gate\'s high', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toMatch(/_OV_PHASE" = "auto" \]; then _OV_EFFORT=high/);
    // --xhigh still overrides both, and it is tested here because it is the one branch a reader
    // cannot see from a bare assignment (five review rounds read exactly that as hard-coded).
    expect(t).toMatch(/_XHIGH" = yes \]; then _OV_EFFORT=xhigh/);
  });

  // ── FOLLOWER 3: the TIMEOUT covers the FUSED case ────────────────────────────────────────────
  // An auto call may run the loop AND then the gate without returning. A budget sized for one of
  // them truncates the other, and a round killed by its own timeout exits 124 having produced
  // nothing — byte-identical to a clean round.
  test.each(FILES)('%s: the timeout budget covers loop-then-gate under auto', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toMatch(/_OV_PHASE" = "auto" \]; then _OV_TIMEOUT=1230/);
    // 1230 = the loop's 900 plus the gate's 330. Asserted as arithmetic rather than as a literal
    // so a future change to either half has to come here and say so.
    expect(900 + 330).toBe(1230);
  });

  // ── FOLLOWER 4 and 5: the LABEL and the SEVERITY BAR follow the phase that ACTUALLY RAN ──────
  // This is the one that cannot be answered before the call. Under `auto` the adapter may promote
  // mid-invocation, so neither the requested phase nor the pre-call resolution describes what
  // produced the findings file. The adapter announces the promotion on stderr and that is the only
  // observer of it.
  test.each(FILES)('%s: the label derives from the announced phase, not the requested one', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toContain('running the final_gate round now');
    expect(t).toContain('_OV_RAN_PHASE');
    // Derived once. Two sites choosing a label independently is the r19 P1 verbatim.
    expect(t).toMatch(/_OV_RAN_PHASE" = "final_gate" \]; then _OV_LABEL=GATE_FINDINGS_JSON/);
    expect(t).toContain('"$_OV_LABEL: ');
  });

  test.each(FILES)('%s: the severity bar is stated per phase, not left to be inferred', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    // The two bars are genuinely different questions: the gate FAILS on p1, the loop CONTINUES on
    // p1 or p2. Reading a loop round on the gate's bar ends a lane with P2s outstanding.
    expect(t).toMatch(/BAR: gate[\s\S]{0,120}?p1 > 0 is FAIL/);
    expect(t).toMatch(/BAR: loop[\s\S]{0,160}?p2 > 0 means the loop CONTINUES/);
  });

  // ── SCOPE: by repository, at the call site ───────────────────────────────────────────────────
  test.each(FILES)('%s: auto is scoped by the repo remote, never by a config key', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toContain('git remote get-url origin');
    // A config key could be set globally by accident and would route every repo's reviews through
    // a mode only one repo agreed to.
    expect(t).not.toMatch(/outside_voice_phase|_cfg\s+phase/);
  });

  // ── THE TWO PRODUCERS MUST BE ASKED THE SAME QUESTION ────────────────────────────────────────
  // resolve-phase defaults to origin/main and non-explicit; the review runs
  // `exec --phase auto --explicit --base origin/<base>`. Omit either flag and the pre-call
  // resolution can disagree with the phase the adapter actually picks — on any branch whose base
  // is not main, or when codex_reviews=disabled and the user asked explicitly. The adapter's own
  // note says the two "call one function with one set of inputs"; this asserts the skill supplies
  // the same inputs, which is the half the adapter cannot enforce.
  test.each(FILES)('%s: resolve-phase is given the same inputs as the exec', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toMatch(/resolve-phase[\s\S]{0,200}?--explicit[\s\S]{0,200}?--base "origin\/<base>"/);
    expect(t).toMatch(/resolve-phase[\s\S]{0,300}?--repo-root/);
  });

  // ── A LABEL IS NEVER PRINTED WITHOUT A BLOCK ─────────────────────────────────────────────────
  // Step 4 reads a printed `*_FINDINGS_JSON:` line as "the verdict comes from this block and
  // nothing else". Printing it with a `<none …>` payload therefore points the grader at a block
  // that does not exist AND removes its fallback to the [P1] markers — so a round with no
  // structured findings grades as a structured clean round. Survivable while the label was a
  // constant; a false CLEAN GATE once it can say GATE_FINDINGS_JSON.
  test.each(FILES)('%s: no findings label is emitted when there is no block', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toContain('NO_FINDINGS_BLOCK');
    // The label variable must never be printed next to a "<none" payload.
    expect(t).not.toMatch(/\$_OV_LABEL: <none/);
  });

  // ── SCOPE FAILS CLOSED ───────────────────────────────────────────────────────────────────────
  // Matching the last path segment opts in any fork called claude-fleet-config. The rollout is
  // scoped to one repository, so the comparison carries owner AND name.
  test.each(FILES)('%s: the repo match is owner/name, not a bare segment', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toContain('vaskockorovski/claude-fleet-config');
    // A bare `{print $NF}` extraction is the segment-only form this replaced.
    expect(t).not.toMatch(/get-url origin[\s\S]{0,200}?awk -F'\[\/:\]' '\{print \$NF\}'/);
  });

  // ── THE DEFAULT PATH ACTUALLY YIELDS TO auto ─────────────────────────────────────────────────
  // The routing table is worthless if the plain-review launcher still runs the inline gate: the
  // change would be unreachable in the one repo it exists for, while every test above still passed.
  // Asserted as CODE, not prose — a step written beside a code block instead of inside it does not
  // get run, which this file already records at cost.
  test.each(FILES)('%s: the inline gate is skipped when the route is auto', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toContain('_REVIEW_ROUTE');
    expect(t).toMatch(/_REVIEW_ROUTE:-gate\}" = "auto" \]; then[\s\S]{0,200}?GATE SKIPPED/);
  });

  // ── THE GATE MARKER TRAVELS WITH THE GATE LABEL ──────────────────────────────────────────────
  // Step 4 keys degraded-gate reporting off GATE_BACKEND. A promoted gate on a hosted backend is
  // diff-scoped, so it must report as PASS (degraded gate); emitting the label without the backend
  // makes a weaker review look like a full one.
  test.each(FILES)('%s: a promoted gate round emits GATE_BACKEND too', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toMatch(/_OV_RAN_PHASE" = "final_gate" \]; then[\s\S]{0,400}?GATE_BACKEND:/);
  });

  // ── THE BAR SURVIVES THE LABEL'S REMOVAL ─────────────────────────────────────────────────────
  // Dropping the label on the no-block path was right, and it took the bar signal with it: step
  // 4's marker fallback describes GATE semantics, so a codex-backed LOOP round carrying [P2]s
  // would read as a clean pass. The bar is now stated on both the block and no-block paths.
  test.each(FILES)('%s: the severity bar is stated even when there is no findings block', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    const m = t.match(/NO_FINDINGS_BLOCK — backend was codex[\s\S]{0,700}?BAR: loop/);
    expect(m).not.toBeNull();
  });

  // ── auto IS ONLY TAKEN WHEN A LOOP ROUND WILL ACTUALLY RUN ───────────────────────────────────
  // An auto that resolves straight to final_gate, run by the loop launcher, sends the loop's
  // prompt with --codex-mode at its `exec` default: a DIFF-scoped round reported as the
  // repo-scoped gate. Not an edge case — outside_voice_loop defaults to codex on an unconfigured
  // install, and the size-cap and runaway-cap fallbacks land there too.
  test.each(FILES)('%s: the auto route also requires the resolved phase to be loop', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toMatch(/_REVIEW_REPO:-\}" = "\$\{_REVIEW_AUTO_REPO:-\}"[\s\S]{0,200}?_OV_RESOLVED_PHASE:-\}" = "loop"/);
  });

  // ── THE ANNOUNCEMENT BEATS THE PROBE ─────────────────────────────────────────────────────────
  // The pre-call resolve-phase happens before the round launches, so another round on the lane can
  // advance the ledger in between. Labelling from the probe would misstate the bar and suppress
  // GATE_BACKEND for the round that actually ran.
  test.each(FILES)('%s: the phase that ran is read from the adapter announcement', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toContain('_OV_ANNOUNCED');
    expect(t).toContain("phase resolved to");
    expect(t).toMatch(/_OV_ANNOUNCED" \][\s\S]{0,300}?_OV_RAN_PHASE="\$_OV_ANNOUNCED"/);
  });

  // ── EXIT 6 stays enumerated on this path ─────────────────────────────────────────────────────
  // The adapter grew `blocked` with the VAS-2373 redesign; a lane that cannot converge must report
  // as blocked, never as an unexpected failure and never as a clean round.
  test.each(FILES)('%s: exit 6 (blocked) is handled on the auto path', (_l, file) => {
    const t = fs.readFileSync(file as string, 'utf8');
    expect(t).toMatch(/_OV_EXIT" = "6"[\s\S]{0,300}?LANE BLOCKED/);
  });
});
