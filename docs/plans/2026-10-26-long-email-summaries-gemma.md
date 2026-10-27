# Long-email summaries on Gemma 4 E4B without losing details

Date: 2026-10-26. Measured against `claude/rc1-integration` at 6d782f6e.

## The problem

Wisp loses details from a long email before Gemma ever reads it. The body is
cut at the head, in three places:

| Where | Limit | What happens to the rest |
| --- | --- | --- |
| `gmail_source.extract_body` (`BODY_CAP`) | 20,000 chars | dropped at capture |
| Model-led Ask (`model_only.ASK_TOOL_RESULT_CHARS`) | 6,000 chars | cut, with a "this is NOT all of it" note |
| General loop (`loop._MAX_TOOL_RESULT_CHARS`) | 12,000 chars | cut, same note |

So on the Gemma Ask path, everything after the first ~6,000 characters of a
`view_emails` result is gone. That includes late corrections ("Update: swimming
is cancelled"), deadlines and phone numbers. The note tells the model to call
again with narrower arguments, but narrower arguments can't reach the second
half of a single email.

## What was measured

The benchmark used four synthetic long emails, not real mail:

- a 16k-char project update
- a 13k-char school newsletter
- a 12k-char trip thread, including a quoted reply
- a 21k-char term sheet

Each email had 8–10 planted details, and 24 of the 37 sat past the 6,000-char
cut. Gemma `gemma-4-E4B-it-oQ4e-mtp` ran on an isolated oMLX engine on port
8795 at Wisp's production sampling (temperature 0.8, top_p 0.95, top_k 64).
There were 3 runs per strategy per email. Recall means a planted detail
appears in the summary as an exact string.

| Strategy | Recall | Mean time | Output tokens |
| --- | --- | --- | --- |
| A. Today: cut at 6k, thinking on (as in Ask) | **41%** | 16.3 s | 877 |
| A'. Cut at 6k, thinking off | 41% | 5.6 s | 263 |
| B. Full cleaned body, plain "summarize" prompt | 95% | 8.5 s | 390 |
| **C. Full cleaned body, structured prompt, thinking off** | **95%** | **5.6 s** | **294** |
| D. Map-reduce (6k chunks, extract, then merge) | 94% | 13.0 s | 714 |
| E. C plus a deterministic hard-detail backfill | 94% | 5.4 s | 288 |

Findings:

1. **The cut is the whole problem.** Every miss in A was a detail past 6,000
   chars. Once Gemma sees the full body, recall goes from 41% to 95%.
2. **Thinking adds nothing here.** It tripled the time (16.3 s vs 5.6 s) and
   recall was identical. A summary call made outside the agent loop has no
   `role: tool` message, so thinking off is safe (see the Gemma
   empty-narration note).
3. **Map-reduce isn't worth it at this size.** Gemma's native context is 131k,
   and a 20k-char body is about 3–5k tokens. That fits in one call well inside
   the 16k window when no tool schemas come with it. Map-reduce was 2.3× slower
   and no more accurate. Chunking is only needed past roughly 40k chars, which
   `BODY_CAP` already rules out.
4. **The structured prompt is the cheap win.** It got the same recall as the
   plain prompt, was 34% faster, and produced fewer tokens. Its output also
   copies numbers and codes exactly (`$2.4 million`, `7720#`, `INC-4471`) and
   names the asks with their deadlines.
5. **About 5% of details still go missing.** The misses are soft facts near the
   end of an email: the liability cap, "two QA engineers", "Venmo Sam". These
   are omissions, not mistakes. No invented facts were seen in the outputs
   checked. The regex backfill (money, %, dates, times, phone numbers, codes)
   never fired, because Gemma kept every hard token on its own. It's a cheap
   safety net, but it isn't proven to help.

## Recommendation

When one email body is longer than the tool-result cap, Wisp should **digest
it instead of cutting it at the head**:

- **Clean the body first.** Drop the quoted thread below `On … wrote:` (the
  regex already exists as `email_tools._QUOTED_MAIL_HISTORY`) and drop legal
  or unsubscribe footer lines. Leave a note saying how much quoted history was
  left out.
- **Make one dedicated Gemma call.** Send the full cleaned body with the
  structured prompt below. Use thinking off through `config.thinking_kwargs`,
  `max_tokens` around 900, no tool schemas, and the same 12 s / background
  rules as the other summary calls. Expect about 5–6 s and about 300 output
  tokens.
- **Return the digest as the `view_emails` result** for that message, along
  with the message identity, and say plainly that it's a digest of the full
  body. Short emails stay verbatim, as today.
- **Treat the digest as untrusted display text**, like any mail. It never
  drives a tool.
- Optionally run the deterministic hard-detail backfill as a guard.

The structured prompt (Wisp's bulleted "IMPORTANT rules" style):

```
Summarize this email for the person who received it.
IMPORTANT rules:
- Start with 1-2 sentences on what the email is about and who it is from.
- Then a 'Key details' list. Include EVERY: date, time, deadline, amount of money, number, code or reference, phone number, place, and person named with what they are doing.
- List anything the reader is asked to do, with its deadline.
- Call out any change, cancellation or correction explicitly.
- Copy numbers, codes and names exactly as written. Do not round or paraphrase them.
- One short line per detail. No commentary.
```

## Caveats

- The emails are synthetic. Their filler is repetitive (about 5 chars per
  token, where real mail is about 4), and recall is an exact-string match.
  Real HTML newsletters and forwarded chains are untested.
- A 20k-char body is about 5k tokens. Inside the agent loop that would still
  be too big alongside about 6k tokens of schemas and history. That's why the
  digest runs as its own call rather than raising the 6k cap.
- Nothing was built or wired into Wisp. The harness is in the session
  scratchpad (`bench/emails.py`, `bench/bench.py`) and is not in the repo.
