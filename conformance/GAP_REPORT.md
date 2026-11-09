# Requirement gap report

`make gap-report` lists the BCP 14 rules in [tempoxyz/mpp-specs](https://github.com/tempoxyz/mpp-specs) and which ones vectors and flows cover. It writes `gap-report.json` and `gap-report.md`, and exits non-zero without writing either when the specs path has no `specs/**/*.md` drafts.

```bash
make gap-report                         # clone mpp-specs main into a temp dir
make gap-report SPECS_PATH=../mpp-specs # use a local checkout
```

- It renders each draft with [kramdown-rfc](https://github.com/cabo/kramdown-rfc) and reads the XML, so it needs kramdown-rfc at the version mpp-specs' `Gemfile.lock` pins (`gem install kramdown-rfc -v 1.7.39`). With several versions installed it picks the pinned one; without it, it fails before writing anything. To use another command, run `python scripts/gap_report.py --kramdown-rfc "docker run --rm -i ietf-spec-tools kramdown-rfc"` (mpp-specs' image), for example.
- A rule is a paragraph or list item in `specs/**/*.md` with an RFC 2119/8174 keyword in capitals, as kramdown-rfc renders it: only rendered sections, and no headings, code, comments, link URLs or tables. Its key is its ID, or `<draft>#<section>:<sha256 prefix>` of its rendered text until it has one. Table rows with keywords are counted, not extracted.
- Put a rule's ID in a `{: #req-<draft>-<nnn>}` IAL on the line right after it, or at the start of a list item (`- {: #req-core-001} Clients MUST ...`). The report warns about an ID that tags no rule: one on a heading, list, table, code block or span, one at the end of a text line (kramdown-rfc prints it as text), and one another IAL replaces.
- A vector scenario or flow case claims rules with `"requirements": ["req-core-001"]`. List the IDs a new scenario checks there.
- An adapter manifest claims drafts with `"profile": ["core", "tempo-charge"]`. The report shows coverage per claimed draft, counting only claims the runners run against that adapter: vector scenarios that match its capabilities (every operation the scenario's tests call), `adapters` and `sdkVersions` (evaluated with `vector_runner.py`'s version logic, which skips a whole file for an adapter whose version it cannot resolve or whose constraint does not parse), and every flow case except `server_only` ones and the discovery, JSON-RPC and `no_payment` cases that send raw HTTP without the adapter, with `client_flow` cases only for adapters that advertise `http.payment_request`.
- The report flags requirement IDs no spec defines, duplicate IDs and IDs with another draft's prefix (rules carrying them never count as covered), claims no SDK can pass (their file has an invalid scenario, or they call a command with no adapter operation), and adapters whose pinned SDK version it cannot resolve.
