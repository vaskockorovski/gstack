#!/usr/bin/env python3
"""HTTP leg of gstack-outside-voice: one chat-completion call, with a validated findings block.

Lives in its own file rather than inline `python3 -c '...'` on purpose. Inlined, every single
quote in this code terminates the shell's quoting — a message like "'findings' is missing"
silently turned the program into shell words and produced exit 127. A separate file makes the
quoting hazard structurally impossible instead of something to remember.

Reads its inputs from the environment (set by the calling shell function):
  OR_MODEL, OR_PROMPT_FILE, OR_DIFF_FILE, OR_TRUNCATED, OR_RESP, OR_TIMEOUT,
  OR_FINDINGS (optional), OPENROUTER_API_KEY, GSTACK_OUTSIDE_VOICE_BASE_URL (optional)

Exit codes THIS FILE produces (the calling shell adds its own — 2 refused input, 3 disabled,
5 no pre-flight sweep, 124 timeout — so this list is not the adapter's full set):
  0  ok
  1  transport / API / empty-response failure
  4  the findings block could not be parsed after one retry — DISTINCT from 1 so a caller can
     tell "the reviewer could not be parsed" from "the reviewer could not be reached", and so
     that neither can ever be mistaken for "the reviewer found nothing".
"""
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request

# Per-request nonce in the fence marker.
#
# The diff is inlined into the prompt, and this reviewer reviews its own adapter — so the diff
# now literally contains the contract text and example blocks. A fixed marker means the parser
# cannot distinguish the model's ANSWER from an example it echoed out of the material under
# review, and "last block wins" would happily pick the echo. More generally: any repository
# whose diff happens to contain a fenced findings block could steer the count, which is a
# prompt-injection channel straight into the stop condition.
#
# The nonce is generated per request, so nothing already in the diff can predict it.
# 8 hex chars, not 16: the model has to reproduce this marker verbatim, and the first live
# attempt at 16 failed to copy it at all. 4 bytes of entropy is ample here — the threat is a
# fenced block already sitting in the diff, and the diff is fixed before the nonce is drawn,
# so it cannot contain a value it had no way to predict.
NONCE = secrets.token_hex(4)
FENCE = "findings-json-" + NONCE

model = os.environ["OR_MODEL"]
prompt = open(os.environ["OR_PROMPT_FILE"], encoding="utf-8", errors="replace").read()
diff_path = os.environ.get("OR_DIFF_FILE") or ""
truncated = os.environ.get("OR_TRUNCATED") == "yes"
findings_path = os.environ.get("OR_FINDINGS") or ""

# Remove any pre-existing findings file BEFORE the call. On the exit-4 path no file is written,
# so a stale file left by an earlier round would still be sitting there — and a caller that
# reads it would silently score this round with the PREVIOUS round's counts. A failed round
# reporting last round's numbers is the worst available outcome: it looks like data.
if findings_path:
    try:
        os.unlink(findings_path)
    except FileNotFoundError:
        pass

if diff_path:
    diff = open(diff_path, encoding="utf-8", errors="replace").read()
    note = ""
    if truncated:
        # Never a silent cap: the model is told, and so is the reader (stderr, caller side).
        note = ("\n\n!! NOTE: the diff below was TRUNCATED to fit. Findings cannot be "
                "assumed complete for the omitted tail.\n")
    prompt = (prompt + note + "\n\nYou have NO filesystem or tool access. Review only "
              "the diff between the delimiters.\n\nDIFF_START\n" + diff + "\nDIFF_END\n")

# The stop condition ("no P1, no P2") is only as trustworthy as the count feeding it. Parsing
# severities out of free-form markdown made the count depend on a heading style the model is
# free to vary between rounds — and it did: one round emitted "### P1-1", the next "### P1.1",
# and a grep for the first form returned ZERO while three P1s sat in the output. A count that
# reads a formatting change as "no findings" ends the loop early, which is the one failure the
# stop condition exists to prevent. So demand a machine-readable block and validate it.
FINDINGS_CONTRACT_TMPL = """

---
MANDATORY OUTPUT CONTRACT — your response is parsed by a program.

After your prose review, emit EXACTLY ONE fenced block, last in your response.
The fence marker below is unique to this request — copy it CHARACTER FOR CHARACTER,
including the trailing hex. A block with any other fence is ignored entirely.

```%FENCE%
{"p1": <int>, "p2": <int>, "p3": <int>,
 "findings": [{"severity": "P1", "title": "<short>", "location": "<file:line>"}]}
```

Rules, all enforced by the parser:
- severity is exactly one of P1, P2, P3.
- The counts MUST equal the number of findings of each severity in the array.
- If you found nothing, emit zeros and an empty array. Do NOT omit the block.
- Emit the block even if your prose already lists the findings.
A missing or malformed block is treated as a FAILED review, not as a clean one.
"""

if findings_path:
    prompt = prompt + FINDINGS_CONTRACT_TMPL.replace("%FENCE%", FENCE)

base_url = os.environ.get("GSTACK_OUTSIDE_VOICE_BASE_URL") or "https://openrouter.ai/api/v1"

# The Authorization header rides on this request. A plain-http override would put the API key
# on the wire in clear text, and the override exists mainly so tests can point at a local stub
# — so allow http ONLY for loopback, and refuse it anywhere else rather than warn. A warning
# here would be printed to a stderr nobody reads until after the key has already leaked.
_parts = base_url.split("://", 1)
if len(_parts) == 2 and _parts[0] == "http":
    _host = _parts[1].split("/", 1)[0].split(":", 1)[0]
    if _host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        sys.stderr.write("refusing to send the API key over plain http to %r — use https, "
                         "or point at loopback for testing\n" % _host)
        sys.exit(1)


def ask(message):
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": message}]}).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
            "Content-Type": "application/json",
            "X-Title": "gstack-outside-voice",
        },
    )
    try:
        # A per-socket timeout, not just the shell wrapper. Without it a server that completes
        # the handshake and then stalls holds the read open until the outer timeout kills the
        # process group — by which point the request is billed and nothing is returned.
        sock_timeout = max(30, int(os.environ.get("OR_TIMEOUT", "330")) - 30)
        with urllib.request.urlopen(req, timeout=sock_timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Print the API error WITHOUT echoing the request. Never print the Authorization header.
        sys.stderr.write("openrouter HTTP %s: %s\n"
                         % (e.code, e.read().decode("utf-8", "replace")[:2000]))
        sys.exit(1)
    except Exception as e:
        sys.stderr.write("openrouter request failed: %s\n" % e)
        sys.exit(1)


def content_of(payload):
    if payload.get("error"):
        sys.stderr.write("openrouter error: %s\n" % json.dumps(payload["error"])[:2000])
        sys.exit(1)
    choices = payload.get("choices") or []
    text = (choices[0].get("message", {}).get("content") if choices else "") or ""
    if not text.strip():
        sys.stderr.write("openrouter returned an empty response\n")
        sys.exit(1)
    return text


# ONLY the nonce'd fence is accepted. A bare ```json or ```findings-json block is ignored,
# because that is exactly what an echoed example from the diff would look like.
BLOCK_RE = re.compile(r"```" + re.escape(FENCE) + r"\s*(\{.*?\})\s*```", re.S)


def parse_findings(text):
    """Return (obj, None) or (None, reason).

    NEVER returns zero counts for unparseable input — that equivalence is the whole bug this
    function exists to remove.
    """
    blocks = BLOCK_RE.findall(text)
    if not blocks:
        return None, "no fenced %s block found" % FENCE
    # Last block wins. The contract puts the block last, and prose above may quote the example
    # from the contract itself; taking the first would parse the illustration, not the answer.
    try:
        obj = json.loads(blocks[-1])
    except Exception as e:
        return None, "findings block is not valid JSON: %s" % e
    if not isinstance(obj, dict):
        return None, "findings block is not a JSON object"
    for key in ("p1", "p2", "p3"):
        value = obj.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None, "key %r missing or not a non-negative integer" % key
    items = obj.get("findings")
    if not isinstance(items, list):
        return None, "findings key is missing or not a list"
    tally = {"P1": 0, "P2": 0, "P3": 0}
    for item in items:
        if not isinstance(item, dict):
            return None, "a findings entry is not an object"
        sev = item.get("severity")
        if sev not in tally:
            return None, "a findings entry has severity %r (want P1, P2 or P3)" % (sev,)
        tally[sev] += 1
    # Counts vs array: the ARRAY is authoritative, the scalars are a checksum.
    #
    # Refusing outright on any disagreement was the first design, and it is wrong in the one
    # direction that matters. The array is an enumeration — every finding is explicitly there —
    # while the scalars are the model's own arithmetic, and models miscount. Observed live:
    # p3=6 claimed beside 7 listed P3s, twice including on the retry, while P1 and P2 (the only
    # fields the stop condition reads) agreed exactly. Refusing there throws away a good round
    # over a slip in a field that gates nothing, and a model that reliably fumbles the sum would
    # stall the loop indefinitely at full round cost.
    #
    # So take the MAXIMUM of the two per severity. That can never under-report, which is the
    # property this whole contract exists to guarantee: the dangerous shape is p1=0 beside a
    # populated array, and max() reports the finding rather than the zero. The disagreement is
    # still surfaced loudly — a reviewer that cannot count its own findings is worth knowing
    # about — it just is not grounds for discarding the findings.
    mismatch = (obj["p1"], obj["p2"], obj["p3"]) != (tally["P1"], tally["P2"], tally["P3"])
    if mismatch:
        sys.stderr.write(
            "WARNING: findings counts (p1=%d p2=%d p3=%d) disagree with the array "
            "(P1=%d P2=%d P3=%d). Taking the larger of each — a count is never allowed to "
            "under-report.\n"
            % (obj["p1"], obj["p2"], obj["p3"], tally["P1"], tally["P2"], tally["P3"]))
    # Use the ARRAY, not max(). The comment above says the array is authoritative, and max()
    # quietly disagreed with it — a contradiction that turns into a hang: if the model
    # consistently over-reports a scalar (p1=1 beside an empty array), max() pins the count at
    # 1 forever and the loop can never reach "no P1, no P2" no matter how clean the artefact is.
    # A stop condition that cannot be satisfied is worse than one that is merely wrong.
    #
    # The array alone is safe in the direction that matters: it is an enumeration, so it cannot
    # under-report the findings the model actually listed — the p1=0-beside-a-populated-array
    # case still yields 1. And it can be genuinely empty, so the loop can terminate.
    obj["p1"], obj["p2"], obj["p3"] = tally["P1"], tally["P2"], tally["P3"]
    obj["counts_disagreed"] = mismatch
    return obj, None


# How much of the previous reply to echo back on the retry.
#
# A HEAD slice (text[:12000]) was exactly the wrong end. The contract puts the findings block
# LAST, so a reply long enough to need trimming is a reply whose block sits in the discarded
# tail — the retry then shows the model everything except the thing it is being asked to fix,
# and the most common malformation (a block that is present but subtly wrong) becomes invisible
# to the one mechanism meant to recover it. The failure is silent: the retry still runs, still
# bills, and still fails, so it reads as "the model would not comply" rather than "we never
# showed it the block".
#
# Keep both ends and elide the MIDDLE, weighted to the tail: the tail carries the malformed
# block, the head carries the prose findings the model needs to re-enumerate. The elision is
# announced in-band so the model cannot mistake the gap for the end of its own reply.
ECHO_BUDGET = 12000


def echo_for_retry(text, budget=ECHO_BUDGET):
    """Return `text` trimmed to roughly `budget` chars, preserving BOTH ends."""
    if len(text) <= budget:
        return text
    tail = (budget * 2) // 3   # the block the contract asks for is at the end
    head = budget - tail
    return (text[:head]
            + "\n\n[... %d characters elided from the middle of your reply ...]\n\n"
              % (len(text) - head - tail)
            + text[-tail:])


def write_usage(prompt_tokens, completion_tokens, served_model,
                retry_model="", retry_prompt_tokens=0, retry_completion_tokens=0):
    # `model` is the model that served the FIRST call, and the token totals are the sum across
    # both calls. When a retry is routed to a different upstream those two facts disagree, so
    # the retry is broken out into its own fields rather than folded into `model` — a single
    # model id standing for two upstreams is a confidently WRONG attribution, and this file is
    # what the per-voice cost/hit-rate analysis reads. Additive fields: existing readers that
    # only know `model`/`prompt_tokens` keep working and are no longer silently misinformed.
    with open(os.environ["OR_RESP"], "w") as fh:
        json.dump({"prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens,
                   "model": served_model,
                   "retry_model": retry_model,
                   "retry_prompt_tokens": retry_prompt_tokens,
                   "retry_completion_tokens": retry_completion_tokens}, fh)


payload = ask(prompt)
text = content_of(payload)
usage = payload.get("usage") or {}
p_tok = usage.get("prompt_tokens", 0)
c_tok = usage.get("completion_tokens", 0)
served = payload.get("model", model)

retry_served = ""
r_ptok = 0
r_ctok = 0

findings = None
if findings_path:
    findings, why = parse_findings(text)
    if findings is None:
        # ONE re-prompt, for the block only — not a second full review. Cheap, and it covers
        # the ordinary case: a model that did the review and forgot the envelope.
        sys.stderr.write("findings block unusable (%s) — re-prompting once for the block\n" % why)
        # One % expression over the whole string. An earlier version spliced the fence in with
        # `+ FENCE +`, which breaks the implicit concatenation into two expressions so the %
        # bound only to the trailing fragment — a TypeError that crashed the RETRY path, i.e.
        # the recovery code was reachable only in the situation where it did not work.
        retry = ("Your previous reply did not carry a usable findings block (%s).\n\n"
                 "Reply with NOTHING but the fenced block below. The fence marker must be "
                 "copied EXACTLY, including the trailing hex:\n\n```%s\n"
                 "{\"p1\": <int>, \"p2\": <int>, \"p3\": <int>, \"findings\": "
                 "[{\"severity\": \"P1\", \"title\": \"<short>\", \"location\": \"<file:line>\"}]}"
                 "\n```\n\nYour previous reply was:\n\n%s"
                 % (why, FENCE, echo_for_retry(text)))
        payload2 = ask(retry)
        text2 = content_of(payload2)
        usage2 = payload2.get("usage") or {}
        r_ptok = usage2.get("prompt_tokens", 0)
        r_ctok = usage2.get("completion_tokens", 0)
        p_tok += r_ptok
        c_tok += r_ctok
        # OpenRouter routes per request, so the retry can land on a different upstream than the
        # first call. Recording only the first model attributes BOTH calls' tokens to it, which
        # silently corrupts exactly the per-voice cost/hit-rate data this logging exists for.
        retry_served = payload2.get("model", model)
        if retry_served != served:
            sys.stderr.write(
                "NOTE: the retry was served by %r while the first call was served by %r — "
                "usage for this round spans two upstreams and is logged separately per call.\n"
                % (retry_served, served))
        findings, why2 = parse_findings(text2)
        if findings is None:
            sys.stderr.write(
                "FINDINGS BLOCK UNUSABLE after one retry (%s). Refusing to report a severity "
                "count. This round did NOT establish that the artefact is clean — it must not "
                "be treated as satisfying the stop condition.\n" % why2)
            sys.stdout.write(text)
            write_usage(p_tok, c_tok, served, retry_served, r_ptok, r_ctok)
            sys.exit(4)

sys.stdout.write(text)
if not text.endswith("\n"):
    sys.stdout.write("\n")

if findings_path and findings is not None:
    with open(findings_path, "w") as fh:
        json.dump(findings, fh)
    sys.stderr.write("findings: P1=%d P2=%d P3=%d\n"
                     % (findings["p1"], findings["p2"], findings["p3"]))

write_usage(p_tok, c_tok, served, retry_served, r_ptok, r_ctok)
