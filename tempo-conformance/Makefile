# Tempo SDK conformance: TypeScript (viem/ox), Rust (tempo-primitives),
# Go (tempo-go) and Python (pytempo) against a local Tempo dev node.

TEMPO_IMAGE ?= ghcr.io/tempoxyz/tempo:1.17.0
ADAPTERS ?=
REPORT ?=
PYTHON ?= python3

RUN = TEMPO_IMAGE=$(TEMPO_IMAGE) $(PYTHON) scripts/run.py $(if $(ADAPTERS),--adapters $(ADAPTERS)) $(if $(REPORT),--report $(REPORT))

.PHONY: all build test vectors generate help

all: ## Build every adapter, run all vectors, submit node cases to a fresh dev node
	$(RUN) --build

build: ## Build adapters only
	$(PYTHON) scripts/run.py --build --no-node --filter __no_case__

vectors: ## Run vectors without the node (fast)
	$(RUN) --no-node

generate: ## Regenerate vectors/*.json from the TypeScript reference
	cd adapters/typescript && npm ci --silent && node generate.ts

help:
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

