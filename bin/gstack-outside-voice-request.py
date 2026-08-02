#!/usr/bin/env python3
"""HTTP leg of gstack-outside-voice: one chat-completion call, with a validated findings block.

Lives in its own file rather than inline `python3 -c '...'` on purpose. Inlined, every single
quote in this code terminates the shell's quoting — a message like "'findings' is missing"
silently turned the program into shell words and produced exit 127. A separate file makes the
quoting hazard structurally impossible instead of something to remember.

Reads its inputs from the environment (set by the calling shell function):
  OR_MODEL, OR_PROMPT_FILE, OR_DIFF_FILE, OR_TRUNCATED, OR_RESP, OR_TIMEOUT,
  OR_FINDINGS (optional), OPENROUTER_API_KEY, GSTACK_OUTSIDE_VOICE_BASE_URL (optional)

Exit codes:
  0  ok
  1  transport / API / empty-response failure
  4  the findings block could not be parsed after one retry — DISTINCT from 1 so a caller can
     tell "the reviewer could not be parsed" from "the reviewer could not be reached", and so
     that neither can ever be mistaken for "the reviewer found nothing".
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

model = os.environ["OR_MODEL"]
prompt = open(os.environ["OR_PROMPT_FILE"], encoding="utf-8", errors="replace").read()
diff_path = os.environ.get("OR_DIFF_FILE") or ""
truncated = os.environ.get("OR_TRUNCATED") == "yes"
findings_path = os.environ.get("OR_FINDINGS") or ""

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
FINDINGS_CONTRACT = """

---
MANDATORY OUTPUT CONTRACT — your response is parsed by a program.

After your prose review, emit EXACTLY ONE fenced block, last in your response:

```findings-json
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
    prompt = prompt + FINDINGS_CONTRACT

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


BLOCK_RE = re.compile(r"```(?:findings-json|json)?\s*(\{.*?\})\s*```", re.S)


def parse_findings(text):
    """Return (obj, None) or (None, reason).

    NEVER returns zero counts for unparseable input — that equivalence is the whole bug this
    function exists to remove.
    """
    blocks = BLOCK_RE.findall(text)
    if not blocks:
        return None, "no fenced findings-json block found"
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
    obj["p1"] = max(obj["p1"], tally["P1"])
    obj["p2"] = max(obj["p2"], tally["P2"])
    obj["p3"] = max(obj["p3"], tally["P3"])
    obj["counts_disagreed"] = mismatch
    return obj, None


def write_usage(prompt_tokens, completion_tokens, served_model):
    with open(os.environ["OR_RESP"], "w") as fh:
        json.dump({"prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens,
                   "model": served_model}, fh)


payload = ask(prompt)
text = content_of(payload)
usage = payload.get("usage") or {}
p_tok = usage.get("prompt_tokens", 0)
c_tok = usage.get("completion_tokens", 0)
served = payload.get("model", model)

findings = None
if findings_path:
    findings, why = parse_findings(text)
    if findings is None:
        # ONE re-prompt, for the block only — not a second full review. Cheap, and it covers
        # the ordinary case: a model that did the review and forgot the envelope.
        sys.stderr.write("findings block unusable (%s) — re-prompting once for the block\n" % why)
        retry = ("Your previous reply did not carry a usable findings block (%s).\n\n"
                 "Reply with NOTHING but the fenced block, restating the findings you already "
                 "made:\n\n```findings-json\n"
                 "{\"p1\": <int>, \"p2\": <int>, \"p3\": <int>, \"findings\": "
                 "[{\"severity\": \"P1\", \"title\": \"<short>\", \"location\": \"<file:line>\"}]}"
                 "\n```\n\nYour previous reply was:\n\n%s" % (why, text[:12000]))
        payload2 = ask(retry)
        text2 = content_of(payload2)
        usage2 = payload2.get("usage") or {}
        p_tok += usage2.get("prompt_tokens", 0)
        c_tok += usage2.get("completion_tokens", 0)
        findings, why2 = parse_findings(text2)
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
    sys.stderr.write("findings: P1=%d P2=%d P3=%d\n"
                     % (findings["p1"], findings["p2"], findings["p3"]))

write_usage(p_tok, c_tok, served)
