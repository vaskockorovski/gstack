import { describe, test, expect } from 'bun:test';
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

const ROOT = path.resolve(import.meta.dir, '..');
const ADAPTER = path.join(ROOT, 'bin', 'gstack-outside-voice');
const CONFIG = path.join(ROOT, 'bin', 'gstack-config');

// The default loop model is stated in THREE places and nothing reconciled them:
//
//   1. bin/gstack-config  CONFIG_HEADER prose  — what the user reads
//   2. bin/gstack-config  lookup_default()     — what `gstack-config get` returns
//   3. bin/gstack-outside-voice exec_openrouter — the fallback when gstack-config is broken
//
// The adapter's own comment says "it is a third copy of one fact ... grep the model id before
// assuming one edit is enough". That is a remembered rule, and a remembered rule is exactly
// what this repo's history says does not hold. The failure it guards against is quiet and
// expensive in the same breath: change (2) alone and every install with a working config bills
// the new model while the docs promise the old one; change (2) and (3) but not (1) and the
// documentation lies. Nobody notices, because every path still "works".
//
// So pin the fact instead of remembering it. This test fails LOUD on drift, which is the whole
// difference between a contract and a comment.
function extract(file: string, re: RegExp): string[] {
  const text = fs.readFileSync(file, 'utf-8');
  return [...text.matchAll(re)].map((m) => m[1]);
}

describe('outside_voice_loop_model default is stated once, consistently', () => {
  test('all three copies of the default model id agree', () => {
    // All three regexes carry /g: String.matchAll THROWS on a non-global pattern. Caught by
    // the length assertions below rather than by reading — a lenient extraction would have
    // returned nothing and compared an empty set, which passes while measuring nothing. That
    // is the same "an empty result reads as agreement" failure this test exists to prevent,
    // one level up, in the test itself.
    const headerProse = extract(CONFIG, /^#\s*outside_voice_loop_model:\s*(\S+)/gm);
    const lookupDefault = extract(CONFIG, /outside_voice_loop_model\)\s*echo\s*"([^"]+)"/g);
    const adapterFallback = extract(ADAPTER, /\[\s*-n\s*"\$model"\s*\]\s*\|\|\s*model="([^"]+)"/g);

    // Each site must exist. A regex that silently matches nothing is indistinguishable from
    // "the sites agree" — the empty-result-reads-as-pass trap this suite exists to avoid.
    expect(headerProse.length).toBe(1);
    expect(lookupDefault.length).toBe(1);
    expect(adapterFallback.length).toBe(1);

    const all = [headerProse[0], lookupDefault[0], adapterFallback[0]];
    expect(new Set(all).size).toBe(1);
  });
});

describe('backend resolution refuses to guess', () => {
  function backend(value: string): { out: string; status: number } {
    // A hand-edited config is the case that matters: `gstack-config set` rejects an invalid
    // value, so the only way one reaches resolve_backend is by editing the file directly —
    // which the config header explicitly invites ("edit freely").
    const home = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'gstack-ov-test-'));
    fs.mkdirSync(path.join(home, '.gstack'), { recursive: true });
    fs.writeFileSync(path.join(home, '.gstack', 'config.yaml'), `outside_voice_loop: ${value}\n`);
    const r = spawnSync('bash', [ADAPTER, 'backend', '--phase', 'loop'], {
      env: { PATH: process.env.PATH, HOME: home, USERPROFILE: '' } as Record<string, string>,
      encoding: 'utf-8',
    });
    fs.rmSync(home, { recursive: true, force: true });
    return { out: (r.stdout || '').trim(), status: r.status ?? -1 };
  }

  test('recognised values pass through', () => {
    expect(backend('openrouter').out).toBe('openrouter');
    expect(backend('codex').out).toBe('codex');
    expect(backend('disabled').out).toBe('disabled');
  });

  // The regression that matters. This previously resolved to `codex` with a warning on a
  // stderr the caller prints only on FAILURE — so a typo'd loop backend billed every round at
  // frontier price and was indistinguishable from a correct run.
  test('a typo resolves to misconfigured, NOT to codex', () => {
    expect(backend('opnerouter').out).toBe('misconfigured');
  });

  test('unset still resolves to codex, so an unconfigured install is unchanged', () => {
    const home = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'gstack-ov-test-'));
    const r = spawnSync('bash', [ADAPTER, 'backend', '--phase', 'loop'], {
      env: { PATH: process.env.PATH, HOME: home, USERPROFILE: '' } as Record<string, string>,
      encoding: 'utf-8',
    });
    fs.rmSync(home, { recursive: true, force: true });
    expect((r.stdout || '').trim()).toBe('codex');
  });
});

describe('outsideVoicePreflight renders valid bash', () => {
  // `$${m}` inside a TS template literal is a LITERAL $ followed by an interpolation, so it
  // renders `$_CODEX_MODE`. A reviewer read it as `$$` + name — bash's PID variable — and
  // filed it as a P1 twice. It is correct, but it is correct in a way that has now cost two
  // review rounds to re-establish, and the "fix" would silently break every downstream branch.
  // Assert the rendered output instead of arguing about the source: `$$` must not survive.
  test('the mode variable renders as a name, not as $$ (the PID)', async () => {
    const { outsideVoicePreflight } = await import('../scripts/resolvers/constants');
    for (const phase of ['loop', 'final_gate'] as const) {
      const out = outsideVoicePreflight({ phase, disabledBehavior: 'codex-only' });
      expect(out).toContain('_CODEX_MODE=$(');
      expect(out).toContain('echo "CODEX_MODE: $_CODEX_MODE"');
      expect(out).not.toContain('$$');
    }
  });

  // The probe grew a fifth state. The generated branch table is the only thing telling a
  // consuming skill what to do with it, and it silently lagged for four rounds.
  test('the branch table covers every state probe can emit', async () => {
    const { outsideVoicePreflight } = await import('../scripts/resolvers/constants');
    const out = outsideVoicePreflight({ phase: 'loop', disabledBehavior: 'skip-all' });
    const adapter = fs.readFileSync(ADAPTER, 'utf-8');
    const probeBody = adapter.slice(adapter.indexOf('probe() {'), adapter.indexOf('# --- usage log'));
    const emitted = new Set([...probeBody.matchAll(/echo "(disabled|not_installed|not_authed|ready|misconfigured)"/g)].map((m) => m[1]));
    expect(emitted.size).toBeGreaterThanOrEqual(5);
    for (const state of emitted) expect(out).toContain(`\`${state}\``);
  });
});

describe('the usage log never reports an unknown cost as zero', () => {
  // The codex CLI reports no token counts, so those rows carried a hard 0. That is not
  // "unknown", it is "free" — and it is the wrong answer in the one analysis this log exists
  // to feed: a cost comparison between a cheap loop and a frontier gate comes out backwards if
  // every frontier row sums to nothing. null cannot be summed by accident.
  function emit(args: string): Record<string, unknown>[] {
    const state = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'gstack-ov-log-'));
    const adapter = fs.readFileSync(ADAPTER, 'utf-8');
    const fn = adapter.slice(adapter.indexOf('log_usage() {'));
    const body = fn.slice(0, fn.indexOf('\n}\n') + 3);
    const harness = path.join(state, 'h.sh');
    fs.writeFileSync(harness, `set -uo pipefail\n_STATE_DIR="${state}"\n_cfg() { echo community; }\n${body}`);
    spawnSync('bash', ['-c', `source '${harness}'\nlog_usage ${args}`], { encoding: 'utf-8' });
    const log = path.join(state, 'analytics', 'outside-voice.jsonl');
    const rows = fs.existsSync(log)
      ? fs
          .readFileSync(log, 'utf-8')
          .split('\n')
          .filter(Boolean)
          .map((l) => JSON.parse(l))
      : [];
    fs.rmSync(state, { recursive: true, force: true });
    return rows;
  }

  // Pins the CALL SITES, not just the function. Round 10 changed the codex rows to log
  // `unknown` via a replace whose old text ended in a space and whose new text did not, fusing
  // the token argument to the status: `log_usage … unknown unknowntimeout`. $6 became
  // "unknowntimeout" (coerced to 0 — the exact bug being fixed) and $7 went empty. The
  // unit test above passed throughout, because it hand-writes correct arguments: a test that
  // constructs its own inputs can say nothing about whether the real callers pass valid ones.
  test('every exec_codex call site passes 7 args and yields null tokens with a real status', () => {
    const adapter = fs.readFileSync(ADAPTER, 'utf-8');
    const sites = [...adapter.matchAll(/log_usage codex .*$/gm)].map((m) => m[0]);
    expect(sites.length).toBeGreaterThan(0);
    for (const site of sites) {
      const rows = emit(
        site.replace(/^log_usage /, '').replace(/\$phase/g, 'final_gate').replace(/\$label/g, 'gate').replace(/\$rc/g, '7'),
      );
      expect(rows).toHaveLength(1);
      expect(rows[0].prompt_tokens).toBeNull();
      expect(rows[0].completion_tokens).toBeNull();
      expect(rows[0].status).toBeTruthy();
    }
  });

  test('an unknown count is null, not 0', () => {
    const [row] = emit("codex '' final_gate gate unknown unknown ok");
    expect(row).toBeDefined();
    expect(row.prompt_tokens).toBeNull();
    expect(row.completion_tokens).toBeNull();
  });

  test('a real count is preserved as a number', () => {
    const [row] = emit("openrouter minimax/minimax-m3 loop loop 38428 7876 ok");
    expect(row.prompt_tokens).toBe(38428);
    expect(row.completion_tokens).toBe(7876);
  });

  test('a non-numeric count still yields valid JSON rather than a broken row', () => {
    const [row] = emit("openrouter m loop loop 'not-a-number' '' ok");
    expect(row.prompt_tokens).toBe(0);
    expect(row.completion_tokens).toBe(0);
  });
});

describe('the base-url guard allowlists schemes rather than blocklisting http', () => {
  const REQUEST = path.join(ROOT, 'bin', 'gstack-outside-voice-request.py');

  function attempt(baseUrl: string): string {
    const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'gstack-ov-url-'));
    const prompt = path.join(dir, 'p.txt');
    fs.writeFileSync(prompt, 'review this\n');
    const r = spawnSync('python3', [REQUEST], {
      env: {
        PATH: process.env.PATH,
        OR_MODEL: 'x',
        OR_PROMPT_FILE: prompt,
        OR_RESP: path.join(dir, 'r.json'),
        OR_TIMEOUT: '30',
        OPENROUTER_API_KEY: 'dummy-never-sent',
        GSTACK_OUTSIDE_VOICE_BASE_URL: baseUrl,
      } as Record<string, string>,
      encoding: 'utf-8',
    });
    fs.rmSync(dir, { recursive: true, force: true });
    return r.stderr || '';
  }

  // Each of these previously reached the network with the API key attached, because the guard
  // tested for the literal string "http" instead of allowlisting what is permitted.
  test.each([
    ['uppercase scheme', 'HTTP://evil.example.com/v1'],
    ['plain http', 'http://evil.example.com/v1'],
    ['userinfo pointing at loopback', 'http://127.0.0.1:9999@evil.example/v1'],
    ['ftp', 'ftp://evil.example.com/v1'],
    ['file', 'file:///etc/passwd'],
  ])('refuses %s before sending the key', (_label, url) => {
    expect(attempt(url as string)).toMatch(/refusing to send the API key/);
  });

  // Loopback over http is the one permitted plaintext case (local test stubs). It must get
  // PAST the guard — proven by failing at the socket instead, on a port nothing listens on.
  test.each([
    ['IPv4 loopback', 'http://127.0.0.1:9/v1'],
    ['IPv6 loopback', 'http://[::1]:9/v1'],
  ])('allows %s through to the socket', (_label, url) => {
    const err = attempt(url as string);
    expect(err).not.toMatch(/refusing to send the API key/);
    expect(err).toMatch(/request failed/);
  });
});
