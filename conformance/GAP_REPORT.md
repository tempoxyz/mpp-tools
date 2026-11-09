# Requirement gap report

`make gap-report` lists the BCP 14 rules in [tempoxyz/mpp-specs](https://github.com/tempoxyz/mpp-specs) and which ones vectors and flows cover. It writes `gap-report.json` and `gap-report.md`, and exits non-zero without writing either when the specs path has no `specs/**/*.md` drafts.

```bash
make gap-report                         # clone mpp-specs main into a temp dir
make gap-report SPECS_PATH=../mpp-specs # use a local checkout
```

- A rule is a paragraph or list item in `specs/**/*.md` with an RFC 2119/8174 keyword in capitals, read as kramdown-rfc renders it: only rendered sections, and no code, comments, link URLs or tables. Its key is its ID, or `<draft>#<section>:<sha256 prefix>` until it has one. Table rows with keywords are counted, not extracted.
- Put a rule's ID in a `{: #req-<draft>-<nnn>}` IAL on the line right after it, or at the start of a list item (`- {: #req-core-001} Clients MUST ...`). At the end of a text line kramdown-rfc prints it as text, so it tags nothing and the report warns.
- A vector scenario or flow case claims rules with `"requirements": ["req-core-001"]`. List the IDs a new scenario checks there.
- An adapter manifest claims drafts with `"profile": ["core", "tempo-charge"]`. The report shows coverage per claimed draft, counting only claims the runners run against that adapter: vector scenarios that match its capabilities (every operation the scenario's tests call), `adapters` and `sdkVersions` (evaluated with `vector_runner.py`'s version logic, which skips a whole file for an adapter whose version it cannot resolve or whose constraint does not parse), and every flow case except `server_only` ones and the discovery, JSON-RPC and `no_payment` cases that send raw HTTP without the adapter, with `client_flow` cases only for adapters that advertise `http.payment_request`.
- The report flags requirement IDs no spec defines, duplicate IDs and IDs with another draft's prefix (rules carrying them never count as covered), claims no SDK can pass (their file has an invalid scenario, or they call a command with no adapter operation), and adapters whose pinned SDK version it cannot resolve.
