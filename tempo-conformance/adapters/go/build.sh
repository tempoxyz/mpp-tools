#!/bin/sh
# Builds the tempo-go adapter. keychain.DecodeKeyAuthorization (tempo-go#135)
# is newer than v0.6.1: use it when the pinned tempo-go has it.
set -eu
cd "$(dirname "$0")"
# go doc only sees modules already in the module cache.
go mod download github.com/tempoxyz/tempo-go
if go doc github.com/tempoxyz/tempo-go/pkg/keychain DecodeKeyAuthorization >/dev/null 2>&1; then
	go build -tags tempogo_decode_key_authorization -o adapter .
else
	go build -o adapter .
fi
