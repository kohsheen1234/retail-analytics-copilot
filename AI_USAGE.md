# AI usage

## Tools

**Claude Code (Opus 5)** was the only AI tool used, driven interactively from the
terminal in this repository. It wrote the bulk of the code in `agent/`, `tests/`,
`optimize.py`, `scripts/` and the prose in `README.md` / `DECISIONS.md`, under my
direction and review.

**What I did not use it for:** the assessment brief and its checksum were never pasted
into a tool while the earlier brief still classified them as invitation-email material,
and no credential, token or URL from the invitation was sent anywhere. The database was
verified locally.

**How I worked.** The pattern that mattered was refusing to accept a component until it
was measured. Almost every correction below came from a test or a run, not from reading
the diff — and the two most expensive defects (the router and the retrieval cutoff) were
invisible in review and obvious within one end-to-end run. `scripts/evaluate.py` exists
because of that: it turns "this design is better" into a number I can be wrong about.

---

## Rejections and corrections

### 1. Rejected: reusing the starter's `tables_used()` for citations

**What the tool produced.** Asked to build citations, the first suggestion was the
obvious one — call the helper the pack already ships:

```python
citations = tool.tables_used(sql)          # starter/sqlite_tool.py
```

**Why I rejected it.** `tables_used` regex-matches known table names anywhere in the
query string:

```python
return [t for t in known if re.search(r'(?<![\w"])"?' + re.escape(t.lower()) + r'"?(?![\w"])', lowered)]
```

Validation requires table citations to *exactly* cover the physical tables of the
executed SQL — extra **and** missing both fail — and a substring match cannot tell a
physical table from a CTE of the same name, or from a table name used as a column alias.
`WITH Products AS (...) SELECT ... FROM Products` cites `Products`, which is wrong;
`SELECT CategoryName AS Products FROM Categories` cites it too. The helper's own
docstring says "Verify, do not trust blindly", which I read as the pack testing whether
you do.

**What I did instead.** Wrote `agent/sql_analysis.physical_tables`: tokenise, strip
literals and comments, resolve `FROM`/`JOIN` targets at any depth including comma joins,
normalise quoting, discard aliases, subtract CTE names. Measured: 33/33 exact against
every provided `gold_tables` (`scripts/evaluate.py citations`), plus adversarial unit
tests for the CTE-shadowing and alias cases that the real data never exercises.

---

### 2. Corrected: the table extractor silently lost tables inside derived tables

**What the tool produced.** Its own extractor, on meeting a parenthesised group in a
`FROM` position, skipped it:

```python
if toks[i].text == "(":                     # subquery or parenthesised join
    return _skip_parens(toks, i)
```

**Why that is wrong.** A derived table still reads from real tables. For
`FROM (SELECT OrderID FROM Orders) x JOIN Products p ON 1=1` it returned `['Products']`
and lost `Orders` — a guaranteed citation failure on any question whose SQL uses a
derived table.

**How it survived review.** This is the case worth reporting. The extractor passed
**23/23** against every provided gold statement while the bug was live, because not one
of the 23 uses a derived table. I only caught it because I wrote a parametrised test for
SQL shapes the dataset does not contain. Passing on real labelled data is weaker evidence
than it looks when the data does not exercise the shape.

**Fix.** Descend rather than skip (`return i + 1`), letting the scanner walk the inner
`FROM`. Pinned by a test.

---

### 3. Rejected: an LM-first router with a rule fallback

**What the tool produced.** A `Router` module that asked phi3.5 for the label and fell
back to rules only when the output was unparseable — the conventional design, and the one
I originally asked for.

**Why I rejected it.** I ran it. On the first end-to-end run the LM router:

* cost **22 seconds per question** (21.9s and 22.9s on two questions), and
* got the **first question wrong** — it labelled *"According to the product policy, what
  is the return window in days for unopened Beverages?"* as `hybrid`, with
  `used_fallback: false`, so the rule prior that said `rag` never applied.

That sent a pure document lookup down the SQL path, burned all three NL-to-SQL attempts
on a database that has no return-window column, and escalated a question whose answer is
sitting in `product_policy::chunk1`. Meanwhile `rule_route()` costs ~0ms and agrees with
34 of 35 provided `route` labels. Latency is not cosmetic here: the hidden set must
finish in under five minutes, and routing alone would have consumed roughly half of it.

**What I did instead.** Inverted it — rules decide, and the LM classifier is opt-in
(`Router(use_lm=True)`), kept and tested for the O2 optional task. The trace records
`decided_by` either way. Measured in `scripts/evaluate.py routing`: 0.971 accuracy, and
the single disagreement is a provided example that contradicts itself (see §6).

---

### 4. Corrected: two regexes that were wrong in opposite directions

**Too narrow.** The KPI formula matcher was anchored at line start with a label group
that could not cross a colon:

```python
_FORMULA = re.compile(r"^[-*\s]*(?P<label>[^=:]{0,80}?)\b(?P<name>[A-Z][A-Za-z ()/]{1,40}?)\s*=\s*(?P<expr>.+)$")
```

Both AOV lines in `kpi_definitions.md` are prefixed `Current definition (effective
2016-01-01): `, so **neither AOV definition was ever extracted** — the module silently
found no AOV formula on the AOV question and the model was left to invent one. Fixed by
unanchoring the search and taking the label from whatever precedes the match.

**Too broad.** The routing prior treated bare entity nouns as database intent, and
`products?` matches the word "product" inside *"the product policy"* — so every policy
question looked like a database question. Fixed by requiring aggregation intent alongside
entity nouns, and by deliberately excluding "maximum"/"minimum" from the aggregation
vocabulary, since *"the maximum return window in days"* is a document lookup and the
provided `dev_policy_perishables_max_days` is labelled `rag`.

Both were found by tests, not by reading the patterns.

---

### 5. Corrected: the review gate escalated answerable questions

**What the tool produced.** A gate that blocks whenever the plan contains a formula
needing a column the schema lacks — reasonable in isolation.

**What went wrong.** Retrieval is lexical. On *"How many orders were shipped to France in
2019?"* BM25 pulled in `kpi_definitions::chunk2` (Gross Margin) on shared vocabulary; the
planner dutifully found `CostOfGoods` missing from the database and the gate escalated a
trivially answerable `COUNT(*)`. Under the published escalation scoring that converts a
1.0 into a 0.25, and it would have fired on a large share of the hidden set.

**Fix.** `planner.kpi_relevant` — a formula may only constrain or block an answer if the
question actually invokes it, matched on the formula name and on its heading's variants so
"gross margin", "margin" and "GM" all resolve to the same chunk. Caught by the
end-to-end test, not by inspection.

---

### 6. Corrected: an AI-suggested leakage check that failed on the pack's own data

**What the tool produced.** A leakage gate that raises when any train or dev question is
≥85% similar to an eval question. It immediately failed — on a **provided** example:

```
train_condiments_revenue_summer_2017 is 91% similar to eval hybrid_revenue_beverages_summer_2017
```

The two differ by one word (`Condiments` vs `Beverages`) and the phrase "dates".

**Why I did not do the obvious thing.** The tool's next suggestion was to delete or
rewrite the provided example so the check passes. I rejected that: the gold answers differ
(358005.08 vs 611562.68), so it is a sibling question, not the same question, and editing
provided data to silence a check of my own making is the wrong direction.

**What I did instead.** Scoped the gate to what I am responsible for — it raises for
examples **I** added and reports provided near-duplicates as warnings. The warning is
worth keeping: a bootstrapped demo drawn from that example hands the model most of an eval
question, which is a real caveat on my dev-to-hidden prediction. Notably the four
most-eval-similar examples in the dataset are all provided ones.

---

### 7. Corrected: my own test expectation, not the code

Worth including because it went the other way. I asserted that an answer resting on a
COGS approximation must have confidence ≤ 0.60. It came out at 0.90 and I initially took
that as a bug. It was not: when the *question* supplies the proxy ("Approximate
CostOfGoods as 70% of the line item UnitPrice"), the gold uses the same proxy, so the
answer is contract-correct and low confidence would be miscalibrated. The cap belongs only
where the proxy is invented — which escalates instead. I changed the code to penalise
(−0.10) rather than cap for a supplied approximation, and corrected the test.

---

### 8. Minor mechanical corrections

* An assignment expression inside a comprehension iterable — `{q.line for q in (found := scan(...))}` —
  which is a `SyntaxError`. Caught on first import.
* `db_schema` had to be renamed from `schema`: `schema` shadows an attribute on
  `dspy.Signature` and emitted a `UserWarning` that would have become a real bug.
* A dead branch, `if k.status != "legacy" or True:`, which the tool wrote and which is
  unconditionally true.
* A definition-expansion heuristic that treated the parenthesised "(2017)" in
  `# Northwind Marketing Calendar (2017)` as a defined term, so every question mentioning
  2017 dragged in a title-only chunk. Fixed by requiring a definition name to contain a
  non-numeric word.

---

## What I did not write and do not fully understand

Stated plainly, because the live session asks.

* **`sqlite_tool.py`, `models.py`, `chunker.py`** are the pack's, committed
  byte-identical. I have read all three closely and can explain them, including two
  behaviours their authors may not intend: `_depth0_keywords` treats a depth-0 `REPLACE(`
  function call as a write verb and rejects legal read-only SQL, and `check_format`
  accepts an `int` where `float` is required. I chose not to touch either.
* **DSPy internals.** I read `dspy/teleprompt/bootstrap.py` closely enough to be certain
  about how the metric's return value is consumed (`success = metric_val`, truthiness) and
  how demos are built from `dspy.settings.trace`. I have *not* read the adapter layer in
  the same depth: I know empirically that `ChatAdapter` renders every input field into
  each demo — which is why `MAX_DEMOS = 2` — but I could not derive the exact token
  layout from the source without going back to it.
* **litellm's parameter passthrough.** I verified by proxying a real request that
  `num_ctx` reaches Ollama's `options` even though it is absent from
  `get_supported_openai_params`. I know *that* it works and where in the code it happens;
  I do not know whether it is a guarantee or an implementation detail, which is why there
  is a test pinning the kwargs.
* **`rank_bm25`'s scoring internals.** I use `BM25Okapi` with default `k1`/`b` and did not
  tune them. With 13 chunks I judged tuning to be overfitting to the visible corpus, but
  that is a judgement I did not test.
