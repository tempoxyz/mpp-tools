//go:build !tempogo_decode_key_authorization

package main

// decodeKeyAuthorization: tempo-go v0.6.1 has no public decoder from a
// transaction's raw key authorization RLP to keychain.KeyAuthorization.
// build.sh builds keyauth_decode.go instead once the pinned tempo-go has
// keychain.DecodeKeyAuthorization (tempo-go#135).
func decodeKeyAuthorization(raw []interface{}) (map[string]any, error) {
	return nil, errf("unsupported", "tempo-go has no API to decode a transaction's key authorization (Tx.KeyAuthorization is raw RLP, pkg/transaction/transaction.go:28)")
}
