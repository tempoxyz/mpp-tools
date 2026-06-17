# Conformance Testing Makefile
# Cross-SDK protocol compatibility testing for MPP implementations

.PHONY: all update-locks update-java install install-runner install-java use-local-sdk test test-all test-sdk test-typescript test-rust test-python test-go test-ruby test-java test-json flow flow-sdk flow-typescript flow-rust flow-python flow-go flow-ruby flow-java server-verify server-verify-sdk server-verify-go update-flow-golden clean help

RUBY ?= ruby
BUNDLE ?= bundle
ADAPTER ?=
SDK_PATH ?=

# Default target
all: install
	@status=0; \
	$(MAKE) test || status=$$?; \
	$(MAKE) flow || status=$$?; \
	$(MAKE) server-verify || status=$$?; \
	exit $$status

#------------------------------------------------------------------------------
# SDK Updates
#------------------------------------------------------------------------------

update-locks:
	@echo "→ Updating package-manager locks..."
	npm install
	cd adapters/rust && cargo update -p mpp
	cd adapters/python && uv lock --upgrade-package pympp
	cd adapters/go && GOTOOLCHAIN=auto go get -u github.com/tempoxyz/mpp-go && GOTOOLCHAIN=auto go mod tidy
	cd adapters/ruby && $(BUNDLE) lock --update mpp-rb sorbet-runtime
	cd adapters/java && ./gradlew --no-daemon dependencies --write-locks

# SDK pins live in package manager manifests and lockfiles:
# - TypeScript: package.json/package-lock.json
# - Rust: adapters/rust/Cargo.toml/Cargo.lock
# - Python: adapters/python/pyproject.toml/uv.lock
# - Go: adapters/go/go.mod/go.sum
# - Ruby: adapters/ruby/Gemfile/Gemfile.lock
# - Java: adapters/java/build.gradle/gradle.lockfile

#------------------------------------------------------------------------------
# Installation
#------------------------------------------------------------------------------

install: install-ts install-rust install-python install-go install-ruby install-java install-scripts
	@echo "✓ All adapters installed"

install-runner: install-ts install-scripts
	@echo "✓ Conformance runner installed"

use-local-sdk:
	@test -n "$(ADAPTER)" || (echo "ADAPTER is required, e.g. ADAPTER=rust" >&2; exit 2)
	@test -n "$(SDK_PATH)" || (echo "SDK_PATH is required, e.g. SDK_PATH=../mpp-rs" >&2; exit 2)
	python3 scripts/use_local_sdk.py --adapter "$(ADAPTER)" --sdk-path "$(SDK_PATH)"

install-scripts:
	@echo "→ Installing script dependencies..."
	python3 -m pip install -q -r requirements.txt

install-ts:
	@echo "→ Installing TypeScript dependencies..."
	npm ci

install-rust:
	@echo "→ Building Rust adapter..."
	cd adapters/rust && cargo build --locked --release

install-python:
	@echo "→ Installing Python adapter dependencies..."
	cd adapters/python && uv sync --locked --python 3.12

install-go:
	@echo "→ Preparing Go adapter..."
	cd adapters/go && GOTOOLCHAIN=auto go mod download && GOTOOLCHAIN=auto go build -o adapter .

install-ruby:
	@echo "→ Installing Ruby adapter dependencies..."
	cd adapters/ruby && python3 run_adapter.py --build

install-java:
	@echo "→ Building Java adapter..."
	cd adapters/java && ./gradlew --no-daemon installDist

#------------------------------------------------------------------------------
# Testing
#------------------------------------------------------------------------------

test: test-all

test-sdk:
	@test -n "$(ADAPTER)" || (echo "ADAPTER is required, e.g. ADAPTER=rust" >&2; exit 2)
	@echo "→ Running $(ADAPTER) vector conformance tests..."
	python3 scripts/vector_runner.py --adapter "$(ADAPTER)"

flow:
	@echo "→ Running flow conformance tests..."
	python3 scripts/flow_runner.py

flow-sdk:
	@test -n "$(ADAPTER)" || (echo "ADAPTER is required, e.g. ADAPTER=rust" >&2; exit 2)
	@echo "→ Running $(ADAPTER) flow conformance tests..."
	python3 scripts/flow_runner.py --adapter "$(ADAPTER)"

server-verify:
	@echo "→ Running server verification conformance tests..."
	python3 scripts/server_verify_runner.py

server-verify-sdk:
	@test -n "$(ADAPTER)" || (echo "ADAPTER is required, e.g. ADAPTER=go" >&2; exit 2)
	@echo "→ Running $(ADAPTER) server verification conformance tests..."
	python3 scripts/server_verify_runner.py --adapter "$(ADAPTER)"

server-verify-go:
	@echo "→ Running Go server verification conformance tests..."
	python3 scripts/server_verify_runner.py --adapter go

flow-typescript:
	@echo "→ Running TypeScript flow conformance tests..."
	python3 scripts/flow_runner.py --adapter typescript

flow-rust:
	@echo "→ Running Rust flow conformance tests..."
	python3 scripts/flow_runner.py --adapter rust

flow-python:
	@echo "→ Running Python flow conformance tests..."
	python3 scripts/flow_runner.py --adapter python

flow-go:
	@echo "→ Running Go flow conformance tests..."
	python3 scripts/flow_runner.py --adapter go

flow-ruby:
	@echo "→ Running Ruby flow conformance tests..."
	python3 scripts/flow_runner.py --adapter ruby

flow-java:
	@echo "→ Running Java flow conformance tests..."
	python3 scripts/flow_runner.py --adapter java

update-flow-golden:
	@echo "→ Updating flow golden results..."
	python3 scripts/flow_runner.py --update-golden

test-all:
	@echo "→ Running conformance tests for all adapters..."
	python3 scripts/vector_runner.py

test-typescript:
	@echo "→ Testing TypeScript (golden) adapter..."
	python3 scripts/vector_runner.py --adapter typescript

test-rust:
	@echo "→ Testing Rust adapter..."
	python3 scripts/vector_runner.py --adapter rust

test-python:
	@echo "→ Testing Python adapter..."
	python3 scripts/vector_runner.py --adapter python

test-go:
	@echo "→ Testing Go adapter..."
	python3 scripts/vector_runner.py --adapter go

test-ruby:
	@echo "→ Testing Ruby adapter..."
	python3 scripts/vector_runner.py --adapter ruby

test-java:
	@echo "→ Testing Java adapter..."
	python3 scripts/vector_runner.py --adapter java

test-json:
	@echo "→ Running conformance tests (JSON output)..."
	python3 scripts/vector_runner.py --output json

#------------------------------------------------------------------------------
# Individual adapter CLI tests (for debugging)
#------------------------------------------------------------------------------

test-parse-www-auth:
	@echo "TypeScript:" && echo 'Payment id="test", realm="api", method="tempo", intent="charge", request="e30"' | npx tsx golden/adapter.ts parse-www-authenticate
	@echo "Rust:" && echo 'Payment id="test", realm="api", method="tempo", intent="charge", request="e30"' | ./adapters/rust/target/release/adapter parse-www-authenticate
	@echo "Python:" && echo 'Payment id="test", realm="api", method="tempo", intent="charge", request="e30"' | uv run --project adapters/python --python 3.12 adapters/python/adapter.py parse-www-authenticate
	@echo "Ruby:" && cd adapters/ruby && echo 'Payment id="test", realm="api", method="tempo", intent="charge", request="e30"' | python3 run_adapter.py parse-www-authenticate
	@echo "Java:" && cd adapters/java && printf '%s' '{"schema":1,"op":"challenge.parse","input":{"header":"Payment id=\"test\", realm=\"api\", method=\"tempo\", intent=\"charge\", request=\"e30\""}}' | ./build/install/mpp-java-adapter/bin/mpp-java-adapter

#------------------------------------------------------------------------------
# Maintenance
#------------------------------------------------------------------------------

clean:
	rm -rf node_modules dist
	rm -f adapters/go/adapter
	rm -rf adapters/go/.gocache
	rm -rf adapters/rust/target
	rm -rf adapters/python/.venv adapters/python/__pycache__ adapters/python/*.egg-info
	rm -rf adapters/ruby/.bundle adapters/ruby/vendor
	rm -rf adapters/java/build adapters/java/.gradle

update-rust:
	cd adapters/rust && cargo update -p mpp

update-python:
	cd adapters/python && uv lock --upgrade-package pympp

update-java:
	cd adapters/java && ./gradlew --no-daemon dependencies --write-locks

#------------------------------------------------------------------------------
# Help
#------------------------------------------------------------------------------

help:
	@echo "MPP Conformance Testing"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  all              Install pinned SDK releases and run vectors + flows"
	@echo "  update-locks     Update pinned SDK package locks"
	@echo "  update-java      Update the Java adapter Gradle lockfile"
	@echo "  install          Install all adapter dependencies"
	@echo "  install-runner   Install shared runner dependencies only"
	@echo "  use-local-sdk    Point one adapter at SDK_PATH, e.g. ADAPTER=rust SDK_PATH=../mpp-rs"
	@echo "  test             Run conformance tests for all adapters"
	@echo "  test-sdk         Run vector tests for ADAPTER"
	@echo "  flow             Run flow conformance tests"
	@echo "  flow-sdk         Run flow tests for ADAPTER"
	@echo "  server-verify    Run server verification conformance tests"
	@echo "  server-verify-sdk  Run server verification tests for ADAPTER"
	@echo "  update-flow-golden  Regenerate flow golden results"
	@echo "  test-typescript  Test TypeScript adapter only"
	@echo "  test-rust        Test Rust adapter only"
	@echo "  test-python      Test Python adapter only"
	@echo "  test-go          Test Go adapter only"
	@echo "  test-ruby        Test Ruby adapter only"
	@echo "  test-java        Test Java adapter only"
	@echo "  flow-typescript  Run TypeScript flow conformance tests only"
	@echo "  flow-rust        Run Rust flow conformance tests only"
	@echo "  flow-python      Run Python flow conformance tests only"
	@echo "  flow-go          Run Go flow conformance tests only"
	@echo "  flow-ruby        Run Ruby flow conformance tests only"
	@echo "  flow-java        Run Java flow conformance tests only"
	@echo "  clean            Remove build artifacts and generated files"
	@echo "  help             Show this help message"
	@echo ""
	@echo "SDK Packages:"
	@echo "  TypeScript: mppx (npm)"
	@echo "  Rust:       mpp (Cargo)"
	@echo "  Python:     pympp (uv/PyPI)"
	@echo "  Go:         github.com/tempoxyz/mpp-go (Go module)"
	@echo "  Ruby:       mpp-rb (Bundler/RubyGems)"
	@echo "  Java:       com.github.stripe:mpp-java (Gradle/JitPack)"
