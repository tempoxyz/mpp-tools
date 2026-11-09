# Requirement gap report

`make gap-report` lists the BCP 14 rules in [tempoxyz/mpp-specs](https://github.com/tempoxyz/mpp-specs) and which ones vectors and flows cover. It writes `gap-report.json` and `gap-report.md`, and exits non-zero without writing either when the specs path has no `specs/**/*.md` drafts.

```bash
make gap-report                         # clone mpp-specs main into a temp dir
make gap-report SPECS_PATH=../mpp-specs # use a local checkout
```

- A rule is a paragraph or list item in `specs/**/*.md` with an RFC 2119/8174 keyword in capitals. Its key is the `{: #req-<draft>-<nnn>}` IAL after it, or `<draft>#<section>:<sha256 prefix>` until it has one. Table rows with keywords are counted, not extracted.
- A vector scenario or flow case claims rules with `"requirements": ["req-core-001"]`. List the IDs a new scenario checks there.
- An adapter manifest claims drafts with `"profile": ["core", "tempo-charge"]`. The report shows coverage per claimed draft, counting only claims the runners run against that adapter: vector scenarios that match its capabilities, `adapters` and `sdkVersions` (evaluated with `vector_runner.py`'s version logic), and every flow case except `server_only` ones and the discovery, JSON-RPC and `no_payment` cases that send raw HTTP without the adapter, with `client_flow` cases only for adapters that advertise `http.payment_request`.
- The report flags requirement IDs no spec defines, duplicate IDs, IDs with another draft's prefix, and adapters whose pinned SDK version it cannot resolve.
