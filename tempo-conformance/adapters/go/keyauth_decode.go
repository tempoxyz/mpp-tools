//go:build tempogo_decode_key_authorization

package main

import "github.com/tempoxyz/tempo-go/pkg/keychain"

// decodeKeyAuthorization reports a transaction's key authorization through
// keychain.DecodeKeyAuthorization (tempo-go#135, after v0.6.1). build.sh sets
// the tempogo_decode_key_authorization tag when the pinned tempo-go has it.
func decodeKeyAuthorization(raw []interface{}) (map[string]any, error) {
	auth, _, err := keychain.DecodeKeyAuthorization(raw)
	if err != nil {
		return nil, errf("decode_error", "DecodeKeyAuthorization: %v", err)
	}
	keyType, ok := keyTypeNames[auth.KeyType]
	if !ok {
		return nil, errf("decode_error", "unknown key type %d", auth.KeyType)
	}
	var expiry, limits, allowedCalls, witness, account any
	if auth.Expiry != 0 {
		expiry = decU(auth.Expiry)
	}
	if auth.Limits != nil {
		out := make([]any, 0, len(*auth.Limits))
		for _, l := range *auth.Limits {
			out = append(out, map[string]any{"token": addr(l.Token), "limit": dec(l.Amount), "period": decU(l.Period)})
		}
		limits = out
	}
	if auth.AllowedCalls != nil {
		out := make([]any, 0, len(*auth.AllowedCalls))
		for _, s := range *auth.AllowedCalls {
			rules := make([]any, 0, len(s.SelectorRules))
			for _, r := range s.SelectorRules {
				recipients := make([]string, 0, len(r.Recipients))
				for _, rec := range r.Recipients {
					recipients = append(recipients, addr(rec))
				}
				rules = append(rules, map[string]any{"selector": hexb(r.Selector[:]), "recipients": recipients})
			}
			out = append(out, map[string]any{"target": addr(s.Target), "selectorRules": rules})
		}
		allowedCalls = out
	}
	if auth.Witness != nil {
		witness = hexb(auth.Witness.Bytes())
	}
	if auth.Account != nil {
		account = addr(*auth.Account)
	}
	return map[string]any{
		"chainId":      decU(auth.ChainID),
		"keyType":      keyType,
		"keyId":        addr(auth.KeyID),
		"expiry":       expiry,
		"limits":       limits,
		"allowedCalls": allowedCalls,
		"witness":      witness,
		"isAdmin":      auth.IsAdmin,
		"account":      account,
	}, nil
}
