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
