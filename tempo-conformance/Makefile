# Tempo SDK conformance: TypeScript (viem/ox), Rust (tempo-primitives),
# Go (tempo-go) and Python (pytempo) against a local Tempo dev node.
# The runner uses the MPP conformance runner's environment (../conformance/uv.lock).

TEMPO_IMAGE ?= ghcr.io/tempoxyz/tempo:1.17.0
ADAPTERS ?=
REPORT ?=
ARGS ?=
PYTHON ?= uv run --locked --project ../conformance python

RUN = TEMPO_IMAGE=$(TEMPO_IMAGE) $(PYTHON) scripts/run.py $(if $(ADAPTERS),--adapters $(ADAPTERS)) $(if $(REPORT),--report $(REPORT)) $(ARGS)

.PHONY: all build test vectors validate test-scripts skips sdk-versions generate help

all: ## Build every adapter, run all vectors, submit node scenarios to a fresh dev node per adapter
	$(RUN) --build

build: ## Build adapters only
	$(PYTHON) scripts/run.py --build-only $(if $(ADAPTERS),--adapters $(ADAPTERS))

test: all

vectors: ## Run vectors without the node (fast)
	$(RUN) --no-node

validate: ## Validate vectors, operations.json and adapter manifests against schemas/
	$(PYTHON) scripts/run.py --validate-only

test-scripts: ## Unit tests for the runner's skip rules and SDK version readers
	$(PYTHON) -m unittest discover -s scripts -p 'test_*.py'

skips: ## List every skipped scenario per adapter with its reason
	$(RUN) --no-node --list-skips

sdk-versions: ## Print the pinned SDK version of each adapter, as sdkVersions sees it
	$(PYTHON) scripts/sdk_version.py

generate: ## Regenerate vectors/*.json from the TypeScript reference (keeps each scenario's skips)
	cd adapters/typescript && npm ci --silent && node generate.ts

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-13s %s\n", $$1, $$2}'
