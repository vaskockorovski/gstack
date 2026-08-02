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
import urllib.parse
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
    # HOW TO READ A UNIFIED DIFF, stated explicitly.
    #
    # Measured, not guessed: across three consecutive live rounds on this adapter's own branch,
    # a tool-less reviewer produced 12 of 13, 6 of 9, and 6 of 12 false positives, and the bulk
    # of them were two mistakes about the FORM of the input rather than the content:
    #
    #   1. `-` lines read as live code. Two rounds re-reported defects the previous round had
    #      FIXED, because the removal is still visible in the diff. One quoted a comment that
    #      says "an earlier version fell back to `git diff HEAD`" as proof the fallback exists.
    #   2. absence read as non-existence. `$TMP_ROOT` was reported unset — it is assigned 130
    #      lines above the hunk, outside the diff. A tool-less backend cannot look, so it must
    #      be told that not-in-the-diff means unknown, not missing.
    #
    # This matters beyond noise. The loop's stop condition is "no P1, no P2" on the REPORTED
    # severities, so a reviewer that re-flags deleted code can never reach it — the loop stops
    # converging and starts costing money to re-litigate its own history. That is a stop
    # condition that cannot be satisfied, which is strictly worse than one that is merely wrong.
    prompt = (prompt + note + "\n\nYou have NO filesystem or tool access. Review only "
              "the diff between the delimiters.\n\n"
              "HOW TO READ THE DIFF — both of these produce false reports:\n"
              "- Lines starting with '-' have been DELETED. They are not in the code any more. "
              "Never report a defect in a '-' line, and never cite one as evidence that "
              "behaviour still exists. Only '+' and context lines are the current code.\n"
              "- A comment may describe what an EARLIER version did, usually to explain why it "
              "was changed. Read it as history, not as a description of current behaviour.\n"
              "- You are seeing a fragment. If a symbol, variable or function is used but not "
              "defined in this diff, it is defined elsewhere in a file you cannot see — that is "
              "expected and is NOT a finding. Report something as undefined only when the diff "
              "itself removes or renames its definition.\n"
              "Report only defects you can demonstrate from the '+' and context lines shown.\n"
              "\nDIFF_START\n" + diff + "\nDIFF_END\n")

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
#
# ALLOWLIST the scheme; do not blocklist `http`. Testing only for `http` left three doors open,
# and each one sends the key somewhere it should never go:
#   * case. URL schemes are case-insensitive, so `HTTP://evil` is plain http to urllib but is
#     not the string "http" — the guard was skipped and the key went out in clear text.
#   * other schemes. `ftp://` has a urllib handler; `file://` reads the local disk and the run
#     is then logged as a successful round. Neither is https, and neither was checked.
#   * parsing. Hand-splitting on ":" mangles IPv6 — `http://[::1]:8080` yielded a _host of "["
#     which failed the allowlist, so the one loopback form the comment promised to support was
#     refused. urlsplit().hostname strips the brackets and lowercases, which is the whole job.
# Fail closed: anything not explicitly permitted is refused before a single byte is sent.
_split = urllib.parse.urlsplit(base_url)
_scheme = (_split.scheme or "").lower()
if _scheme == "https":
    pass
elif _scheme == "http":
    if (_split.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write("refusing to send the API key over plain http to %r — use https, "
                         "or point at loopback for testing\n" % (_split.hostname or base_url))
        sys.exit(1)
else:
    sys.stderr.write("refusing to send the API key to a %r URL — GSTACK_OUTSIDE_VOICE_BASE_URL "
                     "must be https (or http on loopback for testing)\n" % (_scheme or base_url))
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
        if e.code in (401, 403):
            # `probe` said "ready" to get here — it only checks that the variable is NON-EMPTY,
            # which is a presence check wearing a capability check's clothes. So the operator
            # arrives holding a green readiness signal and a bare upstream error, and OpenRouter's
            # own 401 text is "Missing Authentication header", which reads as "the adapter forgot
            # to send it" rather than "your key was rejected". Naming the variable and the expected
            # shape is what turns a wrong-service key — the actual cause, observed live — from a
            # code hunt into a one-line fix. Never echo the value.
            sys.stderr.write(
                "  ^ the request DID carry an Authorization header; the key in "
                "$OPENROUTER_API_KEY was rejected. OpenRouter keys look like 'sk-or-v1-…'; a "
                "credential for a different service will fail exactly like this. Note that a "
                "non-login shell inherits its environment from the parent process, so an edited "
                "profile does not take effect until the shell is a login shell or the process "
                "restarts.\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write("openrouter request failed: %s\n" % e)
        sys.exit(1)


def content_of(payload, on_fail=None):
    """Extract the reply text, or exit 1 — recording the cost first.

    `on_fail` writes the usage row before exiting. Both failure paths here describe a request
    that REACHED the API and was billed: an `error` object and an empty `content` both arrive
    with a usage block attached. Exiting without recording it made a paid round leave no trace
    in the cost log at all, which is worse than the zero-vs-null problem it sits next to — a
    missing row does not merely misreport the round, it denies the round happened. Observed
    live: round 13 of this adapter's own loop returned an empty response after a billed call.
    """
    def bail():
        if on_fail is not None:
            on_fail()
        sys.exit(1)

    if payload.get("error"):
        sys.stderr.write("openrouter error: %s\n" % json.dumps(payload["error"])[:2000])
        bail()
    choices = payload.get("choices") or []
    text = (choices[0].get("message", {}).get("content") if choices else "") or ""
    if not text.strip():
        sys.stderr.write("openrouter returned an empty response — the call was billed but "
                         "produced nothing. NOT a clean round: nothing was reviewed.\n")
        bail()
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
    # A max() of the two was tried here and REJECTED — recorded because the argument for it is
    # seductive and will be re-proposed. It never under-reports, which sounds like exactly the
    # right property, but it also never comes down: a model that over-reports a scalar (p1=1
    # beside an empty array) pins the count at 1 forever and the loop can never reach "no P1,
    # no P2" however clean the artefact gets. See the note at the assignment below. The
    # disagreement is still surfaced loudly — a reviewer that cannot count its own findings is
    # worth knowing about — it just is not grounds for discarding the findings.
    mismatch = (obj["p1"], obj["p2"], obj["p3"]) != (tally["P1"], tally["P2"], tally["P3"])
    if mismatch:
        sys.stderr.write(
            "WARNING: findings counts (p1=%d p2=%d p3=%d) disagree with the array "
            "(P1=%d P2=%d P3=%d). Using the ARRAY, which is an enumeration and cannot "
            "under-report what the reviewer actually listed.\n"
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


def toks(usage, key):
    """Token count as an int, whatever the server sent.

    The base URL is overridable, so the responder is not always OpenRouter — some
    OpenAI-compatible proxies return usage fields as strings. `p_tok += "1234"` is a
    TypeError, uncaught, which kills the process AFTER the request was billed and BEFORE
    write_usage runs: the money is spent and the cost row is missing. Under-reporting spend
    is the one direction the analytics must never fail in, so coerce and move on. A value
    that is not a number at all counts as 0 rather than crashing — a wrong-but-present row
    still shows the round happened, which a missing row does not.

    Returns None when the field is ABSENT — which is "we do not know", not "it was free".
    The distinction is the whole point: this file feeds a cost comparison between a cheap loop
    and a frontier gate, and a 0 that means "unreported" makes the expensive tier look free and
    inverts the answer. The same fix was made one layer up for the codex backend; leaving this
    half at 0 is what "a fix is not done until every dependent site agrees" is about.
    """
    # `key in usage` is not enough: an API that sends {"prompt_tokens": null} is telling us
    # it does not know, and int(None or 0) would quietly turn that into 0 — reintroducing the
    # free-looking row this function was written to prevent, through the one door it left open.
    if not isinstance(usage, dict) or usage.get(key) is None:
        return None
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def add_toks(a, b):
    """Sum two possibly-unknown counts.

    None + None stays None — nothing was reported, so nothing is known. None + n yields n,
    which is a genuine partial and is at least non-zero, so it can never be read as free.
    """
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def write_usage(prompt_tokens, completion_tokens, served_model):
    with open(os.environ["OR_RESP"], "w") as fh:
        json.dump({"prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens,
                   "model": served_model}, fh)


payload = ask(prompt)
# Read the cost BEFORE validating the content: every path below that can exit describes a
# request that already reached the API and was already billed.
usage = payload.get("usage") or {}
p_tok = toks(usage, "prompt_tokens")
c_tok = toks(usage, "completion_tokens")
served = payload.get("model", model)
text = content_of(payload, lambda: write_usage(p_tok, c_tok, served))

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
                 # 12000 characters silently ATE FINDINGS. Round 7 of this adapter's own loop
                 # produced a 27KB prose review; the retry saw the first 12KB of it and
                 # enumerated only what survived the cut — the block came back p3=0 beside
                 # roughly fourteen P3s in the prose. The retry exists to recover the envelope
                 # of a review that already happened, so truncating its input turns a recovery
                 # into a quiet under-count, which is the exact failure the contract was built
                 # to prevent. Generous cap: this carries no diff, so even at 200k it is a
                 # fraction of the first call, and the ceiling is only here to stop a runaway
                 # response from blowing the context window.
                 % (why, FENCE, text[:200000]))
        payload2 = ask(retry)
        usage2 = payload2.get("usage") or {}
        p_tok = add_toks(p_tok, toks(usage2, "prompt_tokens"))
        c_tok = add_toks(c_tok, toks(usage2, "completion_tokens"))
        text2 = content_of(payload2, lambda: write_usage(p_tok, c_tok, served))
        findings, why2 = parse_findings(text2)
        if findings is not None:
            # PROVENANCE. On a retried round the prose on stdout is the FIRST reply and the
            # counts come from the SECOND, so they can legitimately disagree — the model
            # re-enumerates from a summary of its own earlier answer and may drop or merge
            # entries. Silently handing back counts from a different call than the review the
            # reader is looking at is the kind of mismatch that gets blamed on the parser.
            findings["from_retry"] = True
        if findings is None:
            sys.stderr.write(
                "FINDINGS BLOCK UNUSABLE after one retry (%s). Refusing to report a severity "
                "count. This round did NOT establish that the artefact is clean — it must not "
                "be treated as satisfying the stop condition.\n" % why2)
            sys.stdout.write(text)
            write_usage(p_tok, c_tok, served)
            sys.exit(4)

sys.stdout.write(text)
if not text.endswith("\n"):
    sys.stdout.write("\n")

if findings_path and findings is not None:
    with open(findings_path, "w") as fh:
        json.dump(findings, fh)
    sys.stderr.write("findings: P1=%d P2=%d P3=%d%s\n"
                     % (findings["p1"], findings["p2"], findings["p3"],
                        "  (counts came from the RE-PROMPT; the prose above is the first reply, "
                        "so the two can differ)" if findings.get("from_retry") else ""))

write_usage(p_tok, c_tok, served)
