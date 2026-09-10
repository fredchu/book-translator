# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **A shared `requests.Session` per provider — the per-thread design from the previous entry
  never actually reused a connection** (2026-09-10). The real driver re-creates its thread pool
  once per chapter (an outer per-chapter pool, an inner per-chunk pool inside it), so every
  chapter's threads were new and every "reused" per-thread Session died with them. Simulating
  the real shape (an outer/inner thread-pool pair, re-created per chapter) against a real local
  HTTP/1.1 server and counting TCP `accept()`s, not requests: 24 connections for 66 requests at
  N=4, 92 for 132 at N=8 — barely better than opening a new connection every time. Replacing the
  per-thread Session with one Session for the whole provider brings both down to exactly N.
  Re-examined the "not guaranteed thread-safe" warning that motivated the per-thread design in
  the first place: `requests.Session` isn't safe for concurrent *mutation* of session-level state
  (cookies, `session.headers`), but this provider never touches either — `_req_kwargs()` passes
  headers as a per-call argument to `.post()`/`.get()`, never onto `self._session.headers` — and
  the connection pool that actually hands out sockets is `urllib3`'s, which has its own internal
  lock. A shared Session is safe here specifically because of that call shape, not in general.

- **`cloud_llm.sh`'s translation watchdog no longer assumes one book** (2026-09-10). `--book`
  accepts more than one path (`nargs="+"`, or the flag repeated), but `CLOUD_LLM_MAX_HOURS`
  was a flat 6-hour budget sized for a single book; passing several meant the watchdog could
  send `TERM` (and the exit trap would terminate the rented machine) partway through a run that
  legitimately needed longer. The default now scales as `6h + 0.5h × (book_count - 1)`, capped
  at 12h — one book is still exactly 6 (unchanged for every existing invocation), two books is
  6.5, thirteen books hits the 12h cap exactly. **Past the cap the script refuses to guess and
  dies before renting anything**, asking for an explicit `CLOUD_LLM_MAX_HOURS` instead of
  silently scaling further — a large batch is exactly the case where a wrong guess burns the
  most money unattended. Every path immediately following `--book` or `--book=` counts, stopping
  at the next flag so an unrelated argument (e.g. the directory after `--out`) is never
  miscounted as a book. An explicit `CLOUD_LLM_MAX_HOURS` always wins over the formula, even
  when it's smaller than what the formula would pick, and may itself be fractional. The
  effective value and book count are logged before anything is rented, same reasoning as the
  earlier silent `max_tokens` mismatch: a difference nobody can see is the hardest one to debug.
  **This watchdog is a local `sleep` + `kill`, not a remote safeguard**: it depends on this
  machine's own shell staying alive: the rented instance itself doesn't self-terminate, and no
  size of this budget changes that — see the permanent comment beside the constants.

### Documented
- **fp8 is a different register, not higher quality** (2026-09-10, read by a human).
  Same chapter, same card model, same prompt and post-processing, quantization the only
  variable: the two versions are 84.3% character-identical, below the 88.2% that a plain
  rerun of one version produces — so the difference is real, not sampling noise. The reader's
  verdict was **not** "one is better": int4 reads plainer and more colloquial with shorter
  sentences; fp8 reads more polished and written, with a formal, rigorous feel. Each suits
  different books. `--profile fp8` is therefore documented as a **register choice**, not an
  upgrade — the previous wording ("this one deserves better") invited people to buy quality
  they were not getting. Cost, normalised to one price and boot time: fp8 is ~31% more per
  book at 16 in flight, ~111% more at 1.

  This is the second time an automated signal failed to predict what a reader caught: in
  2026-06-25 an automated register score promoted a 35B model to default on a 5x throughput
  win, and a full-chapter read-through reversed it on 2026-08-09.


### Added
- **`cloud_llm.sh` reuses bookcast's Vast machine memory instead of learning from scratch**
  (2026-09-10). bookcast already tracks which Vast machines boot reliably in a small sqlite3
  ledger (`bookcast.vast_machine_memory`, an independent CLI with a `--db` path argument), and
  `cloud_llm.sh`'s own stall detector (`BOOT_STALL_MIN`) already matched bookcast's logic line
  for line — it just never fed the result back anywhere. Both tools rent from the same Vast
  pool, so a machine bad for one is bad for the other; `CLOUD_LLM_MACHINE_MEMORY` now points at
  bookcast's own database file by default, no changes to bookcast itself required (its CLI
  already supported this).

  Offer selection is now three independent-budget tiers — preferred (recently-good machines) →
  general market excluding active-bad machines → an unfiltered emergency market if the general
  tier was ever narrowed by the blocklist — each with its own full try count
  (`CLOUD_LLM_WHITELIST_TRIES`, default 2; `CLOUD_LLM_MAX_OFFER_TRIES`, default 3, used by both
  the general and emergency tiers). **The three budgets must never share a pool**: an earlier
  design mistake in a sibling project let the preferred tier exhausting its offers eat into the
  general tier's budget too, so when the whole preferred list got rented out first, ordinary
  search never got a fair try at all. Tested by forcing every create attempt to fail and
  counting exactly how many were attempted across tiers.

  This is a runtime dependency on another repo, so every call into it is fail-soft by
  contract: a missing `bookcast` module, an import error, or a corrupt database all print a
  warning and continue with an empty preferred/blocked list — never `die()`. Machine identity
  (`machine_id`) isn't available until after `create`, only `offer_id` is, so a bad machine is
  captured via an instance-record lookup between create and terminate (same order bookcast
  uses). Only the script's own `die()`-triggered exit records a failure; SIGINT/SIGTERM/SIGHUP
  do not — the user interrupting a boot is not evidence the machine is bad. Success is recorded
  once the server is ready and the thinking preflight passes, not at the end of a multi-hour
  translation — a book failing for unrelated reasons (content, quota, Ctrl-C) shouldn't
  penalize a perfectly good machine. Both event kinds share the fixed three-value schema
  bookcast's table already enforces (`created` / `boot_failed` / `synth_ok`); `stage` is set to
  `book-translator` to distinguish these rows from bookcast's own in the same shared table.

  **Not done this round** (explicitly out of scope, see `spec-03-machine-memory-reuse.md`):
  bookcast's own cloud-machine orphan detector still only recognizes `bookcast-` labels and
  cannot see book-translator's machines — fixing that means changing bookcast, and the detector
  only pushes a notification rather than terminating anything, so it can't replace "rent, then
  verify termination on exit" as the actual safety net. Left as an open item, not fixed here.

## [1.1.0] - 2026-09-10

### Added
- **Whole-book chapter concurrency, opt-in, with the safe request width measured on the
  rented machine instead of guessed** (2026-09-10). The last sequential dependency between
  chapters was the cross-chapter carryover — each chapter's first chunk carried the previous
  chapter's final 200 translated characters. New `--concurrent-chapters` drops it and lets
  chapters overlap; `cloud_llm.sh` passes it by default (local single-GPU gains nothing from
  concurrency, so the flag stays off there and nothing changes for the 27 books already
  translated). **This closes the "Not done this round" item below.**

  **The two gates are deliberately independent.** `--max-concurrent-requests` controls
  chunk fan-out *within* a chapter; `--concurrent-chapters` controls whether the *cross*-chapter
  dependency exists. Reusing one gate for both — the natural implementation — would have
  overturned the review-15 regression test added the day before; kept separate, that test is
  unchanged and still green.

  **Why dropping the carryover is safe, and what it was actually doing.** Same chapter, same
  model, same term table, same post-processing: clearing the cross-chapter carryover leaves
  88.3% character-level similarity to the original translation, while *rerunning with nothing
  changed* leaves 88.2%. Term consistency likewise: 7 of 328 glossary terms rendered
  differently after clearing, versus 8 after a plain rerun. Three independent re-measurements
  by a peer session (per-paragraph 3-gram cosine with a 20,000-sample bootstrap interval that
  straddles zero, bidirectional n-gram drift, punctuation-density distance) found no effect
  either. A natural experiment on a second book explains why: chapter 29 predominantly used
  one transliteration of a character's name (45 vs 8), but its final 200 characters happened
  to use the minority form — and chapter 30 followed the *minority* form throughout, the
  dominant one dropping to zero. **The carryover does not pin names; it propagates whatever
  form is in the last 200 characters, right or wrong.**

  **Concurrency requires a term table — this is the important operational finding.** Eight
  runs on one chapter of a memoir, counting how many transliterations of one name appear:
  with a term table, sequential / carryover-cleared / concurrent all produced exactly one form
  (61/61/60). Without one, four of five runs produced three forms, and the concurrent run was
  the worst, inventing a fifth. Concurrency makes an un-tabled book *worse*, because the
  within-chapter carry becomes source-based, so each chunk decides names independently. The
  driver now warns on stderr (warn only, never blocks) when concurrency is on and the book has
  no `spec_terms.json`.

- **`CLOUD_LLM_ENGINE=vllm|sglang`** with each engine composing its own server arguments.
  The previous claim that swapping engines only needed two environment variables did not hold:
  `VLLM_ARGS` hard-coded vLLM-specific flags (positional model, `--max-model-len`,
  `--gpu-memory-utilization`, `--max-num-seqs`) and the extra-args variable only appends.
  Tests assert negatively that vLLM-only flags never appear in the SGLang invocation.

- **Thinking-leak preflight on both engines** — the first completion request after boot
  asserts non-empty content, no `<think>` tag (newlines stripped first, so `<think\n>` is
  caught) and empty `reasoning_content`; any failure destroys the instance before translation
  starts. The risk is model-level, not engine-level: this is a fine-tune whose chat template
  may silently ignore `enable_thinking=false`, and vLLM is the default engine. SGLang
  additionally forces `--reasoning-parser qwen3`, and an attempt to override it aborts
  *before* renting.

### Changed
- **`max_tokens` 8192 → 2048 on the omlx path** (2026-09-10). The 8192 ceiling was
  unreachable: measured single-request throughput is 26.6–46.4 tok/s, so the 120 s request
  timeout caps output at 3,192–5,568 tokens. Every long generation therefore hit the timeout
  first and was retried at temperature 0.5 — GPU burned, and the sampling temperature silently
  changed. Real full-size 3,000-character chunks emit 509–534 tokens (660 cloud requests
  averaged 350), so the legitimate ceiling is ~650 and 2048 leaves 3x headroom while capping a
  runaway at ~77 s, inside the timeout. **Changing the provider default alone was a no-op** —
  both CLIs pass `--num-predict` explicitly (4096 and 8192); they now resolve after engine
  selection, omlx to 2048, Ollama keeping its previous values (different models, different
  output profiles), an explicit value always honoured and the effective value printed.

- **Cloud concurrency is measured, not configured** (2026-09-10). After the server is ready,
  a wave of 24 disjoint production-sized chunks from the book being translated is issued
  simultaneously (`threading.Barrier`); if the wave fails it retries at 16, then 12, then 8.
  A wave passes only when mean single-request throughput clears `max_tokens / (timeout × 0.8)`
  = 21.33 tok/s **and** the slowest request finishes inside `timeout / 2`. The second criterion
  is what makes the probe cover memory pressure: an undersized cache shows up as preemption
  and recompute, i.e. a straggler, not as truncation.

  Earlier attempts to tier by GPU are recorded here because they are all wrong in instructive
  ways. VRAM is not the constraint — VRAM minus weights is, and the same 32 GB card leaves
  13 GB under int4 but under 1 GB under fp8. Tiering by *remaining* space needs a threshold
  that cannot be derived on paper: `--gpu-memory-utilization` is 0.92 and CUDA context plus
  workspace take ~1.5 GB, so a 48 GB card really has ~24 GB, and the first threshold drafted
  this way excluded the very card that had been measured. Vast reports `gpu_ram` in MB and
  below nominal — RTX 6000 Ada 49140 (47.99 GiB), 5090 32607, 4090 24564, and **L40S 46068
  (44.99 GiB against a nominal 48)** — so nominal thresholds demote real cards. And extrapolating
  a high-N rate from an N=1 probe fails on the same card: relative single-request throughput at
  N=16 is 0.62 under int4 but 0.88 under fp8 (fp8 *rises* from N=1 to N=8), so the int4 ratio
  predicts 12.7 tok/s for fp8 where 18.0 was measured.

  `CLOUD_LLM_CONCURRENCY` still overrides, but the probe runs anyway and warns when the
  explicit value exceeds the measured-safe one. If all four levels fail the run continues at 8
  with a loud warning — the bar is "a runaway can finish inside the timeout", not "legitimate
  output cannot" — and a probe error falls back to 16 without aborting. The server's own cap is
  pinned to the highest candidate independently of the client value: leaving them coupled meant
  a 24-wide probe against a 16-wide server measured the queue, not the concurrency.

### Fixed
- **`finish_reason` was never checked anywhere in the repository.** A response truncated at
  `max_tokens` was passed to marker validation and took its chances; it now raises immediately
  so the driver's existing temperature-0.5 retry handles it, and the value is recorded in the
  request log.

- **`load_fixed_terms` silently treated well-formed JSON with a non-object root
  (`[]`, `null`, `42`) as an empty term table.** Given the measured cost of translating without
  one, that silent downgrade is now a hard error. The stderr warning probe stays warn-only so a
  damaged file cannot block a run before the real load reports it.

- **A bash 3.2 portability break in the escape hatch.** `cloud_llm.sh` runs under
  `set -euo pipefail`, and macOS ships bash 3.2, where expanding an empty array aborts. The
  array is empty exactly when `CLOUD_LLM_CONCURRENT_CHAPTERS=0` — the path taken when something
  has gone wrong and you want the old behaviour back.

### Measured

Real hardware, RTX 6000 Ada 48 GB on Vast.ai, int4, the 503-request Mind-Gut workload:

| requests in flight | throughput | single-request | book translation | cost/book |
|---|---|---|---|---|
| 4 (previous default) | 144.0 tok/s | 12.6 s | 20.3 min | ~0.30 USD |
| 8 | 248.4 | 13.8 s | 11.7 min | ~0.21 |
| 16 | 420.3 | 17.1 s | 7.2 min | **~0.16** |
| 24 | 517.3 | 23.2 s | — | — |
| 32 | 582.4 | — | — | — |

All quality signals were zero at every width. Boot took 5–11 minutes, not the 19 previously
assumed, which makes boot the dominant cost at high concurrency — at 16 the machine spends
longer starting than translating. fp8 on the same card runs 1.6–2.2x slower; normalised to one
price and boot time it costs 31% more per book at N=16 and 111% more at N=1, so **the price of
the higher-precision profile falls as concurrency rises**. Its translation quality was not
compared this round.

An earlier draft of these numbers reported N=24 as a hardware ceiling. It was not: a single
runaway generation hit the token cap and dragged the batch metric down. Same request count at
both widths, total output differed by exactly +7,818 tokens while every other width averaged
349–353 tokens per request against N=24's 415. The measurement scripts now replay an identical
request count at every width and send the production `max_tokens`.

---

### Fixed
- **A blank line inside one paragraph's own translation no longer desyncs the paragraph
  count** (2026-09-09). `parse_marker_output()` only stripped each marker body's leading/
  trailing whitespace — an internal blank line the model inserted inside what was meant to be
  ONE paragraph survived unchanged. Every downstream consumer (`assemble.py`, the seam-repair
  pass, `run_benchmark.py`) rejoins bodies with `"\n\n"` and later re-splits on `"\n\n"` to
  recover paragraph boundaries, so that internal blank line becomes indistinguishable from a
  real one: N source paragraphs come out looking like N+1. Not a hypothetical — a real book
  (Mind-Gut) was checked chapter-by-chapter and never hit it (1,894/1,894 paragraphs matched
  across all 20 chapters, 0 mismatches), but a fake-provider probe forcing the input
  reproduced it on the *sequential* path too (13 output paragraphs vs 12 source), so this
  predates concurrency — it was just never exercised by any real model's output. `parse_marker_
  output()` now collapses any internal blank-line run to a single newline and reports which
  marker indices it touched (`ParseResult.blank_line_markers`, new field); `validate_translation`
  surfaces it as a warning. The no-marker single-paragraph fallback prompt never goes through
  the marker parser at all, so it gets the identical collapse call directly
  (`marker_alignment.collapse_internal_blank_lines`, now public, called from both places).
  This is detection-and-normalization, not a retry trigger: the model producing a stray blank
  line is usually harmless prose, so the fix removes the structural risk without spending
  another translation attempt on it.

### Added
- **Optional within-chapter concurrent translation for a cloud vLLM/SGLang endpoint**
  (2026-09-09). The real lock was never the `supports_concurrency` flag — it was
  `chunk_carry = aligned[-200:]` (`translate_book_ollama.py`): every chunk waited on the
  *translation* of the one before it, serializing a chapter's ~16-40 chunks (median/max
  measured on one book; not independently re-measured this round — see findings) into a
  single queue no matter how many concurrent requests the server could take. New
  `--max-concurrent-requests N` (omlx engine only, default 1 = old sequential behavior,
  unchanged) switches each chunk's context from the *previous chunk's translation* to the
  *previous chunk's own source text* (`chunker.source_tail`), computed for every chunk up
  front from the chunk plan alone — no chunk's prompt depends on any other chunk's output,
  so all of a chapter's chunks can be dispatched to the provider at once
  (`_translate_chapter_chunked_concurrent`, `_translate_chunks_concurrently`). **This is a
  trade paid to unlock concurrency, not a verified quality improvement.** The research
  memo backing this design (`research-concurrency-2026-09-08.md`) claimed source-side
  context "matches or beats" target-side and that target-side causes error propagation;
  fact-checking its own five cited papers (2026-09-09 annotation at the top of that file)
  found the opposite where the papers say anything, and none of the five address
  chunk/chapter boundaries at all — they're all single-document, sentence-level. The
  quality cost of this switch is real but its size is **unmeasured**; seam-repair below is
  an attempt to bound it, also unmeasured. **Cross-chapter carryover is deliberately left
  target-text-based** on both paths (review-15 correction: chapters are translated one at a
  time regardless of provider concurrency, so decoupling that carry would have bought nothing
  while losing the real translated tail at all 19 chapter boundaries for free — an earlier
  draft of this change did decouple it and was reverted). Losing the real translated tail at
  each *intra-chapter* chunk boundary is repaired afterwards, not avoided: a post-hoc
  **seam-repair** pass (`_repair_chunk_seams`, on by default, `--no-seam-repair` to disable)
  re-translates each boundary's first paragraph once more using the real preceding
  translation, itself dispatched concurrently since every boundary's input is already on disk
  once the first pass finishes. Found and fixed along the way: `_translate_chunk` used to
  select each retry's temperature by mutating the shared `provider.temperature` — safe when
  chunks run one at a time, a race once they run on separate threads. Both
  `OmlxProvider.translate()` and `OllamaProvider.translate()` now take an optional
  `temperature=` override instead; `_resolve_max_workers()` also defaults an
  attribute-less `supports_concurrency=True` provider (e.g. `AnthropicProvider`) to 1
  worker, not an unbounded job count. **Not done this round**: actual cross-chapter
  concurrent dispatch (chapters remain sequential), and no live-server throughput
  measurement (the task rules out loading the 27B model). **Both were done on 2026-09-10 — see the 1.1.0 entry above; the ~5.9x figure below is a
  theoretical ceiling that the real measurement replaced.** The theoretical speedup is
  **not** "chunks per chapter" — it's bounded by the server's own concurrency cap
  (`~/.omlx/settings.json`'s `scheduler.max_concurrent_requests`, 8 on this machine as of
  2026-08-15 — **that is the configured value; nobody has issued concurrent requests to
  confirm the server actually processes 8 at once, and whether mlx-lm's BatchGenerator
  scales linearly at 27B is unknown**): batching Mind-Gut's 260 chunks in groups of ≤8 *per chapter* takes 44
  batches total (some chapters' chunk counts don't divide evenly by 8), for a **~5.9x**
  (260/44) theoretical ceiling — not a measurement. A cloud vLLM/SGLang endpoint's own cap
  must be checked separately; it is not this file's value. See `findings-worker-5.md` in
  the collab directory for the full verification.

### Changed
- **Simplified→Traditional conversion is now sentence-scoped, not whole-chapter** (2026-09-09).
  `s2tw` alone was still unsafe on already-Traditional prose: it emits Simplified on some input
  (肥皂劇 → 肥皂**剧**), guesses phrases wrong (只能 → **隻**能), and rewrites characters the Ministry
  of Education lists as correct (疱疹 → **皰**疹). Measured across three books (5,190 paragraphs,
  369K Han characters): real Simplified residue was 227 characters (0.06%), present in 1.5% of
  paragraphs — while opencc *changed* 8.1% of paragraphs, wrongly rewriting about **50 correct
  characters per book** (里→裡 in transliterated names, 干預→幹預, 手表示抗議→手**錶**示).
  `to_traditional` now splits on sentence boundaries and converts only sentences that contain an
  unambiguously Simplified character. The trigger set is read from opencc's own `STCharacters.txt`
  under the rule "the character is a key, is **not** in its own candidate list, and `s2tw` changes
  it" — `s2t(x) != x` cannot be used, since it fires on 77% of pure-Traditional paragraphs.
  `着` is added (it lives in `TWVariants`, not `STCharacters`); the rest of `TWVariants` is not,
  because it also normalises Traditional variants. A six-character exclusion list (疱雇霉晒苧洼)
  comes from the Ministry of Education dictionary — each has its own meaning, and 洼 is a surname.
  **Result: 0 wrong rewrites on a 195K-character finished book, down from 24; about 3% of real
  Simplified residue is knowingly missed** (某种, 面粉, 老板 — characters that are legal Traditional
  on their own), a trade the user accepted explicitly. `translate_book_ollama` now loads the
  trigger set before spending any GPU time and refuses to start without it, rather than silently
  translating a whole book unconverted.

### Added
- **`scripts/build_terms.py` produces a ranked terminology review queue** (2026-09-09).
  It combines mechanical extraction with LLM proposals, verifies every key against source-body
  text using the same normalized whole-token matcher as runtime injection, lists hallucinated
  proposals and person-name collisions, applies wordfreq's high/middle/low policy, and—when
  aligned with-table/without-table translations exist—ranks by observed translation changes
  rather than raw frequency. Chinese values remain proposals until the user approves them.
- **Audits report three levels, not two** (2026-09-09). `translation_quality_audit` failed a whole
  run over three correct chapter titles ("Chapter 7: Understanding Intuitive Decision Making" →
  「第七章：理解直覺決策」, ratio 0.20), because Chinese titles are far more compact than English
  ones (0.15–0.22 versus 0.30 for body prose) and the audit only recognised `<h1>`–`<h6>`, not the
  `<p class="h3">` many publishers use. Across the whole local corpus (27 bilingual EPUBs, 20,916
  src/tgt pairs) that rule holds **1,398 genuine failures** — misaligned pairs, truncated output,
  leaked model tokens — against **7 false alarms**. The gate was never noise; the problem was that
  a human seeing red had to decide which kind it was, and then shipped anyway. `AuditResult` now
  carries `warnings` alongside `failures`: alignment breaks, truncation, model-token leaks and
  Simplified residue stay red; title-shaped and short-sentence length ratios go amber and no longer
  block. Verified by conservation — 1,398 red + 7 amber = the original 1,405 — so nothing was
  released. Amber is surfaced through `state.json`, the result dict and the run log, because a
  warning nobody can see in an unattended run is not a warning.

### Fixed
- **Assembly no longer fakes bilingual coverage** (2026-09-09). `bilingual_rewriter` stamped a
  「譯文：」 prefix onto any translation with no Han characters, purely so that
  `bilingual_coverage_audit` — which checks for an adjacent Han sibling — would pass. Readers got
  「譯文：Aagaard, Kjersti, Jun Ma, Kathleen M. Antony…」; the finished Mind-Gut Connection carried
  85 of them, and across the corpus the prefix covered **1,257 paragraphs**, of which 506 were
  echoed English or misaligned notes that cleared both gates because of it. The prefix is gone.
  Exemptions now require a *reason*: `assemble` writes `{src_text, reason}` and only when a
  classifier can name one (`bibliography`, `index`, `notes`, `publication_metadata`, `identifier`),
  and the audit accepts only reasoned entries. The old "no zh sibling means exempt" rule was the
  same mistake in a different place — it justified an exemption by absence rather than by cause.
  Page-role matching is whole-token: substring matching exempted `preferences` (contains
  "reference"), a book's opening `index.xhtml`, and `notes_on_contributors`, and one book's
  filename (`b05`) was hardcoded into a classifier every book shares — all four in the direction
  of weakening the gate. Short untranslated English newly in scope (`—Reid Hoffman`,
  `Begin Reading`) reports amber rather than red, since promoting it would flip already-shipped
  books from green to red. Nodes that already carry their own Chinese — the bilingual ToC renders
  「Title Page ｜ 書名頁」 inside one node — are excluded from the coverage universe; without that,
  Superagency's contents page produced 6 failures and The Meaning of Your Life's 3.
  **Four books re-assembled and re-audited: zero red.**
  Backward incompatible: `source_only.json` bare-string entries are ignored (with one warning), so
  auditing an EPUB built by an older version reports failures until it is re-assembled.

### Fixed
- **Chapter titles split across three blocks were not recognised, wrecking the whole ToC**
  (2026-09-09). `styled_paragraph_title` required ALL CAPS — deliberately, since on the local
  corpus the short mixed-case leading blocks are epigraphs that must not become titles. But
  The Mind-Gut Connection splits every heading into `<p>Chapter</p>` + `<p>1</p>` +
  `<p>The Mind-Body Connection Is Real</p>` in Title Case, so all 20 entries fell back to
  "first 80 characters of body text" and shipped as *"Chapter 1 The Mind-Body Connection Is
  Real W hen I started medical school in 197"* — the stray `W hen` being the source's drop-cap
  span. Two new paths run only after the all-caps path returns nothing, so existing behaviour
  is untouched: one keyed on a leading chapter marker (`Chapter`/`Part`/`Section`/`Book`), one
  for a lone Title Case heading sitting on prose (Preface / Bibliography / Index / "Praise
  for …", which `infer_role` classifies as plain `body` and `_ROLE_TO_HEADING` therefore
  misses). **Measured end to end on the book: 17 broken headings out of 26 → 0.**
  Two guards learned the hard way while writing this: the marker and number blocks are short
  but the title is not (a flat cap truncated two real chapters to a bare `"Chapter: 5"`), and
  length alone cannot separate a long title from a short opening sentence — a title does not
  end in a full stop, prose does. Regression tests assert both directions.

### Added
- **Selective terminology injection** (2026-09-08). `spec_terms.json` was broadcast whole into every
  chunk's system prompt, so a real term table could not be used: 328 terms is 6954 chars per chunk
  across 260 chunks, which both costs tokens and dilutes attention (the model reads past the
  instructions it needs). `dispatch.select_terms_for_text` now injects only the terms the chunk
  actually contains — measured average 113 chars (median 102, max 365), a **98.4% reduction**, with
  6.7 terms per chunk and only 2 of 260 chunks matching nothing. Matching is whole-token (`gut` does
  not match `gutter`) and case-sensitive only for capitalised terms, so `Weeks` (an army physician)
  does not match the 20 occurrences of "weeks" while `gut` still matches a sentence-initial "Gut".

### Fixed
- **Offline post-processing corrupted correct Traditional Chinese** (2026-09-08). `to_traditional`
  used opencc `s2twp`; the trailing `p` applies a mainland→Taiwan *vocabulary* table that carries
  computing terms. The local model already emits Traditional Chinese, so the table fired on correct
  prose. Measured on ch.7 of *The Mind-Gut Connection* (51,774 chars): 10 edits, **5 of them wrong** —
  血液循環 → 血液迴圈 (x3), 隨時調用 → 隨時呼叫, 易感窗口 → 易感視窗, 排泄 → 排洩, 受到干擾 →
  受到幹擾. A reader caught 幹擾 on the first read of the chapter. Now uses `s2tw`, plus a
  `_PROTECTED_TERMS` shield for words whose Simplified form maps to several Traditional forms
  (干 → 干/乾/幹). Same chapter now takes **1 edit instead of 10**, and real Simplified still converts
  correctly (血液循环 → 血液循環, where `s2twp` gave 血液迴圈). Regression tests assert both
  directions: reverting the config fails 6 tests, emptying the shield fails 3.

### Changed
- `cloud_llm.sh` exports bandwidth-aware cost estimates to the Vast offer ranking (srt-skill 1.12.4):
  int4 29 GB / fp8 41 GB download (image + model), 1.5 GPU hours. Vast bills bandwidth, and for this
  flow the download often costs more than the GPU time; hosts with free bandwidth now rank first.

### Fixed
- `translation_quality_audit.py` / `bilingual_coverage_audit.py`: pass `from_encoding="utf-8"` when
  parsing spine XHTML bytes. beautifulsoup4 4.15 (with lxml 6.1, what CI installs since 2026-09) guesses a
  different encoding for XHTML without a charset meta, turning the Chinese target text into mojibake so the
  banned-pattern check silently passed (CI red on every platform; local 4.14 was fine). EPUB XHTML is UTF-8 by spec.

### Changed
- `extract_epub.py`: text contents (TOC) pages are now translated instead of kept source-only —
  a TOC is a list of chapter titles, cheap to translate and otherwise the one all-English page in
  the bilingual book. Image-only / empty contents pages fall back to `source_only` (same guard as
  empty body chapters). `bilingual_rewriter.py` only injects the built-in contents-link labels when
  the page has no translation of its own, so a translated TOC no longer shows two Chinese renderings.
  Tests: `test_text_contents_page_is_translated`, `test_image_only_contents_page_falls_back_to_source_only`.

### Added
- `scripts/cloud_llm.sh`: run the omlx engine against a vLLM server on a rented GPU (Vast.ai or RunPod).
  No code upload, no SSH — the instance runs the official `vllm/vllm-openai:v0.28.0` image in args mode
  with port 8000 exposed and `--api-key` protection; the script waits for `/v1/models` to list the model,
  runs `translate_book_ollama.py --engine omlx --omlx-host <endpoint>`, then destroys the instance
  (trap on every exit path, list re-checked). `--profile int4` (default,
  `XReyRobert/Qwopus3.6-27B-v2-GPTQ-Pro-v1` on RTX 5090) / `--profile fp8`
  (`Jackrong/Qwopus3.6-27B-v2-FP8` on L40S). `--keep`/`--stop` reuse one server across books.
  Boot budget, pull-stall detection and a translation watchdog (`CLOUD_LLM_MAX_HOURS`) bound the spend.
  Instance lifecycle comes from srt-skill's `vast_instance_lib.sh` / `runpod_pod_lib.sh`.
- `OmlxProvider(api_key=...)` and `--omlx-api-key` (env `OMLX_API_KEY`) on both CLIs: Bearer header for
  key-protected OpenAI-compatible endpoints; without a key the request shape is unchanged.
- Tests: `test/test_cloud_llm.py` (fake `vastai`, fake RunPod transport, local fake vLLM) covers
  no-key stop, readiness gate (200 + model id; 401 is not ready), boot-budget teardown, keep/stop,
  RunPod v2 payload (`args`, `ports`, `startSsh:false`), fp8 profile, unparsed-create adoption by label.
- Live run (2026-09-04, Vast.ai, Taiwan RTX 5090 at 0.356 USD/h): image pull + 18.7 GB model download +
  engine init took 19 min to `/v1/models`; the structural fixture (5 chapters) translated in 0.3 min;
  instance destroyed and confirmed absent. Two earlier attempts were aborted by bugs now fixed and
  tested: the shared Vast CLI wrapper appended `--raw` after `--args` (it became a vLLM argument and the
  container crash-looped while `actual_status` stayed `running`), and args-mode create responses are a
  Python-dict string, not JSON, so an unparsed-but-successful create leaked three instances before the
  adopt-by-label guard existed. RunPod path is unit-tested but not yet run live.

## [1.0.1] — 2026-08-10

### Fixed
- **Footnote pages and run-on front matter get bilingual ToC labels** (2026-08-10).
  Per-chapter footnote pages (`Superagency_FN001.xhtml`) open with the footnote text
  itself, so there is no title to extract and the ToC showed a sentence fragment in
  both columns. The label is now derived from the filename
  (`extract_epub._footnote_page_heading` -> "Footnote 3", paired with 註腳 3 in
  `nav_builder`). **Deliberately applied after `infer_role` and never fed into it**:
  classifying these pages as `notes` would flip `output_strategy` to `source_only`
  and silently stop translating twelve pages of real content — a test locks that
  down. Praise / "Also by" pages, whose first block runs the title straight into a
  blurb, are matched by prefix instead of exact string. Superagency now assembles
  with 0 English-only nav warnings, down from 31; no per-book repair script needed.
- **Chapter titles styled as `<p>` instead of `<h1>`-`<h6>` now reach the ToC**
  (2026-08-10). Publishers mark titles with CSS classes — `<p class="CN">CHAPTER 4</p>`
  + `<p class="CT">THE TRIUMPH…</p>` — which neither `extract_epub._first_heading`'s
  tag scan nor `build_nav_overrides`' heading check recognised. The English side of
  each nav label fell back to the first 80 characters of body text ("CHAPTER 1
  HUMANITY HAS ENTERED THE CHAT As 2022 drew to a close…") and the Chinese side was
  never generated, so every affected book needed a one-off repair script. This was
  the fifth book to hit it. New `extract_epub.styled_paragraph_title` and
  `offline_postprocess._leading_title_block_count` share one definition of a title
  block so both sides of a bilingual label agree; a chapter-number line and its
  title are joined into one label. Precedence is heading tag > canonical structural
  label > styled title: an earlier ordering let the styled title through first and
  turned "Contents" into "CONTENTS" and "Notes" into "NOTES: INTRODUCTION", breaking
  the zh lookup for pages that had previously worked.
  **ALL CAPS is the discriminator, and it is load-bearing.** Measured over the local
  corpus: 52 chapters use the styled-`<p>` shape and are all-caps, the 43 body-prose
  openings are long and mixed-case, and the 12 short mixed-case leading blocks are
  epigraphs and dedications ("My heart is not a home for cowards.") that must never
  become chapter titles. Structural pages that fall through (Copyright, Praise for …)
  are already covered by `STRUCTURAL_LABELS_ZH_TW`.
- **`collapse_acronym_glosses` learned the wrong Chinese rendering** (2026-08-10).
  Chinese has no word delimiters, so the greedy pre-bracket match on
  「隨著高度能動的人工智慧（AI）」 yielded 隨著高度能動的人工智慧, which matched nothing
  downstream: the full Superagency run collapsed 1 of 241 人工智慧 mentions. The unit
  tests missed it because their fixtures put the term at the start of a sentence,
  which real prose almost never does. Fixed by walking left from the bracket and
  stopping at a function word; the stop-character set is deliberately narrow, since
  用 (通用), 能 (智能), 有 (所有) and friends occur inside real terms — an over-wide
  first attempt trimmed 人工通用智慧 down to 智慧. After the fix: 人工智慧 241 -> 1,
  大型語言模型 60 -> 1.

## [1.0.0] — 2026-08-09

### Changed
- **Offline default model: `Qwen3.6-35B-Heretic-4bit` → `Qwopus3.6-27B-v2-MLX-4bit`**
  (2026-08-09). The 2026-06-25 promotion to the 35B rested on a ~5x throughput win
  and an automated register score from a single-chapter spot check. A full-chapter
  read-through of *Superagency* reversed it: the 35B's prose was judged clearly
  flatter with more 中國用語, and it needed 2 retries plus a single-paragraph
  fallback on the chapter the 27B translated with 0 retries and 0 dropped
  paragraphs. The 35B stays available as a ~4x-faster draft option via
  `--omlx-model`. A 470K-char book now takes ~3.2 h instead of ~0.8 h.

### Added
- **`OFFLINE_STYLE_RULES` in the offline prompt** (2026-08-09). The offline path
  previously shipped a bare format contract; the style rules had been stripped in
  the hy-mt2:7b era and never restored after the model got bigger. Without them the
  27B emits 47 mainland-Chinese usages per chapter (the 35B: 0) and both models
  produce 「A，這些A……」 anaphora and subject-predicate splits. The rules are
  structural only — relative-clause rewriting, no 「，這些X」 anaphora, 台灣 usage,
  inline 中譯（English）glosses, meaning preservation.
  **A sentence-LENGTH rule was tried and reverted**: 「單句超過 40 字就斷句」 scored
  best on the >55-char metric (29.2% → 10.0%) while collapsing sentence-length
  stdev 27.4 → 18.4 and crushing inline glosses 58 → 11. The reader rejected that
  output on sight. Optimising a length proxy optimises for monotony.
- **`offline_postprocess.collapse_acronym_glosses`** (2026-08-09). After the first
  「人工智慧（AI）」, later 人工智慧 become the bare acronym. Same cross-chunk cause as
  gloss dedupe — every chunk treats itself as the term's first mention, measured at
  人工智慧 x10 per chapter in two independent runs. The Chinese rendering is learned
  from the surviving gloss rather than hardcoded, so a book glossing 「人工智能（AI）」
  collapses that instead.
- **Per-book term table `<book_dir>/spec_terms.json`** (2026-08-09).
  `dispatch.load_fixed_terms()` reads `{"terms": {source: 中譯}}` and appends it to the
  offline system prompt as lookup data. Book-specific editorial data, deliberately not
  in this repo. Measured on Superagency ch.4: prompt 756 -> 1065 chars with 10 terms,
  still 0 retries / 0 dropped paragraphs, so the table does not reproduce the
  rule-count degradation that killed the length-rule version.
- **`offline_postprocess.dedupe_inline_glosses`** (2026-08-09). Keeps only the
  first 「中譯（English）」 per term book-wide. The gloss rule fires per chunk and the
  model has no cross-chunk memory, so terms get re-glossed repeatedly (104 glosses
  / 78 unique terms in one chapter). Deterministic dedupe, not a prompt fix.

### Fixed
- **Offline bilingual ToC: chapter titles in a `<header>` now get translated**
  (2026-06-25, systematic-debugging). Root cause: chapter titles live in
  `<header>` (chapter-number `<h1>` + `role="doc-subtitle"`), which
  `strip_non_content` drops, so `build_nav_overrides` saw the first body `<p>`
  (not a heading), skipped, and the EPUB ToC + in-body chapter titles rendered
  English-only. Only front/back matter (bare `<h1>`) got labels. New
  `offline_postprocess.translate_header_titles(book_dir, manifest, provider)`
  extracts the header title and batch-translates it through the model (marker-
  tagged, positional fallback); the driver calls it after `build_nav_overrides`.
- **translation_quality audit no longer false-fails on inline-bilingual ToC
  links.** A source_only paragraph rendered "English ｜ 中文" by
  `_bilingualize_contents_links` carries its translation inline; the audit now
  exempts any `src` paragraph that already contains Han (`_contains_han`) instead
  of demanding a separate `tgt` sibling or a `source_only.json` exception.
- Both verified end-to-end on *The Meaning of Your Life* (Arthur C. Brooks): 17
  chapters, 4/4 audit gates green, EPUB nav + in-body titles bilingual. New tests
  in `test/test_offline_header_titles.py` (6); full suite 324 passed.

### Changed
- **Offline default model: `Qwopus3.6-27B-v2-MLX-4bit` → `Qwen3.6-35B-Heretic-4bit`**
  (2026-06-25). The new default is a Qwen3.6-35B-A3B 3B-active MoE that generates
  ~5x faster (~43 vs ~8 t/s on M1 Max) at parity Opus-tier register and lower
  Simplified leak (0.3% vs 0.9%) — a 23-chapter book drops from ~2h13m to an
  estimated ~25-30min. `enable_thinking:False` suppresses thinking cleanly via the
  model's fixed chat template, so `OmlxProvider` is unchanged. Qwopus3.6-27B-v2
  remains the documented fallback (smaller RAM footprint). Defaults updated in
  `translate_chapter_cli.py` + `translate_book_ollama.py`; default-contract tests
  updated. Evidence: `company/book-translator/2026-06-25-heretic-35b-a3b-local-translation-speedup.md`.

### Added
- `<book_dir>/translations_extra.json` schema for per-book overrides
  (`by_exact_text` and `nav_overrides`). The assembler reads this file when
  present; book-specific dedications, copyright body text, or custom nav labels
  for a specific edition belong here, never in source.
- `assets/register_hints.json` with three generic registers
  (`literary_fiction`, `non_fiction_narrative`, `academic_technical`). The
  glossary prompt loads these at import time and injects them into the LLM
  prompt as abstract style traits — no author names cited.
- Explicit `parent_id` field on `SpineEntry`. The extractor threads each
  body/epilogue/acknowledgments/about_author/notes entry that follows a
  `part_divider` under that divider's id, so PART → chapter nesting is
  data-driven, not heuristic.
- `scripts/marker_alignment.py` — `[[PARA_N]]` source-side wrap +
  output-side parser for structural verification of subagent translations.
- `scripts/translation_log.py` — per-chapter `<book_dir>/translation_log/<chapter_id>.json`
  capture of full prompt + raw response + parsed translation + validation warnings.
- `scripts/replay_chapter.py` — offline CLI to re-validate + re-extract a logged
  chapter's translation without re-dispatching the subagent.
- `state.py` adds `aup_refused` status + `mark_aup_refused` helper. Terminal
  state for chapters refused by Anthropic AUP; output preserves source with
  an HTML annotation block instead of erroring out the whole book.
- `dispatch.strip_known_leak_prefixes` — removes preface phrases ("Here is
  the translation:", markdown fences, etc.) before marker parsing.
- `dispatch.detect_aup_refusal` — recognizes common AUP refusal phrases in
  the response so callers can route to `mark_aup_refused` instead of `mark_failed`.
- `dispatch.extract_aligned_translation` — strip-and-parse helper that
  returns plain chapter text from a marker-aligned response.
- `scripts/providers/` package — `TranslationProvider` ABC with
  `OllamaProvider` (HTTP client to localhost:11434) and `AnthropicProvider`
  (marker class). Lets the pipeline target a local Ollama server (e.g.
  translategemma:4b/12b/27b) as a CC-quota fallback or offline draft.
- `scripts/translate_chapter_cli.py` — single-chapter CLI used for
  benchmarking and ad-hoc spot-checks; gains `--validate-markers` flag that
  runs Phase 1 marker alignment against the provider output and writes
  `<request_id>.aligned.txt` on success.
- `scripts/run_benchmark.py` — cross-model benchmark CLI that runs N
  models across M chapters, extracts Bocky/Fred bilingual baselines for
  side-by-side review, and emits a `comparison.md` template.

### Changed
- Renamed `assemble.ZH_BY_EXACT_TEXT` → `STRUCTURAL_LABELS_ZH_TW` and
  `CONTENTS_LINK_LABELS` → `CONTENTS_LINK_LABELS_ZH_TW`. Both are documented
  as generic structural i18n dicts — well-known English structural labels
  (Contents, Acknowledgments, Notes, Cover, Title Page, etc.) mapped to
  standard 台灣繁體中文. Book-specific content lives in
  `translations_extra.json`, not these dicts.
- `_build_nav_xhtml()` now renders generically from `manifest.spine[]` in
  every case: top-level `<ol>` with nested `<ol>` under each `part_divider`,
  bilingual "English ｜ 繁中" labels derived from
  `translations_extra.nav_overrides` → `STRUCTURAL_LABELS_ZH_TW` →
  `CONTENTS_LINK_LABELS_ZH_TW` → existing fallback.
- `dispatch.SUBAGENT_PROMPT_TEMPLATE` — source paragraphs now arrive wrapped
  with `[[PARA_1]]..[[PARA_N]]` markers. The subagent must echo every marker
  followed by its translation and start its output with `[[PARA_1]]` (no
  preface). Verification rule asks the subagent to count markers before
  returning.
- `dispatch.validate_translation` — primary path is marker alignment
  (missing/extra markers reported); legacy paragraph-ratio path retained for
  responses without markers. Also surfaces `AUP_REFUSED:` warning and preface
  leak.
- `assemble.insert_bilingual` accepts `chapter_status` + `aup_reason` kwargs.
  `aup_refused` chapters get a visible `<div class="aup-refused-note">`
  injected at top of `<body>`; source preserved verbatim, no translation
  siblings.
- `translation_quality_audit` + `bilingual_coverage_audit` short-circuit on
  chapters containing the aup-refused note.
- `structural_audit` accepts `aup_refused` alongside `done` / `source_ready` /
  `dropped` as a valid terminal state for ship-ready chapters.
- SKILL.md: model discipline now documents Sonnet 4.6 default for ch.02+
  fan-out subagents, Opus 4.7 for ch.01 style sample + escalation on
  validation reject. Cross-modal eval slot still uses Opus.

### Removed
- `scripts/regenerate_bilingual.py`. The "re-assemble a bilingual EPUB from
  existing translated chapter files" workflow is now covered by
  `scripts/assemble.py` (idempotent given the same `book_dir`). The deleted
  file also held ~150 lines of copyrighted paragraph translations and three
  hardcoded user-specific paths — neither of which belong in source.
- `_is_co_intelligence()` and `CO_INTELLIGENCE_NAV_XHTML` from
  `scripts/assemble.py`. Replaced by generic manifest-driven nav rendering.
- Author-name register exemplars from `scripts/glossary.py` prompt template
  (specific writers were referenced as register exemplars; replaced with
  abstract style descriptions).

## [0.1.0-pre] — 2026-05-14

Internal milestone (pre-OSS). Initial extraction from the in-tree skill
implementation into a standalone repository.

### Added
- `scripts/extract_epub.py`: walk the source OPF spine and emit a full
  manifest v2 (every spine item represented, `output_strategy` ∈
  `{translate, source_only, nav_generated, drop_explicit}`).
- `scripts/dispatch.py`: build per-subagent prompts with glossary +
  style anchor + carryover for cross-chunk coherence.
- `scripts/assemble.py`: emit the bilingual EPUB from extracted source
  spine + per-item translation files, preserving original CSS / fonts /
  images / XHTML paths / internal href targets verbatim.
- Five deterministic audit gates (`structural_audit.py`,
  `bilingual_coverage_audit.py`, `href_resolve_audit.py`,
  `translation_quality_audit.py`, plus pytest). Each invariant lives in
  its own script so "audit pass but quality collapse" failure modes are
  separately caught.
- Glossary prompt template + parser + canonical form writer
  (`scripts/glossary.py`).
- Bilingual README (English + 繁中) and `SKILL.md` documenting the
  full workflow.

### Notes
- This release is the basis for the public 1.0.0 cut, after the
  "Unreleased" refactor above lands.
