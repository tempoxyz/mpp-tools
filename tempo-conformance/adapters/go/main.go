// Command adapter is the tempo-go adapter for the Tempo SDK conformance suite.
// It serves the JSON-lines ABI in ../../SPEC.md on stdin/stdout.
package main

import (
	"bufio"
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"os"
	"strings"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/tempoxyz/tempo-go/pkg/keychain"
	"github.com/tempoxyz/tempo-go/pkg/signer"
	"github.com/tempoxyz/tempo-go/pkg/transaction"
)

// ---------- errors ----------

type adapterError struct {
	Type    string
	Message string
}

func (e *adapterError) Error() string { return e.Type + ": " + e.Message }

func errf(typ, format string, args ...any) *adapterError {
	return &adapterError{Type: typ, Message: fmt.Sprintf(format, args...)}
}

func invalid(format string, args ...any) *adapterError { return errf("invalid_input", format, args...) }

// ---------- main loop ----------

func main() {
	in := bufio.NewReaderSize(os.Stdin, 1<<20)
	out := bufio.NewWriter(os.Stdout)
	defer out.Flush()
	for {
		line, err := in.ReadBytes('\n')
		if len(bytes.TrimSpace(line)) > 0 {
			resp := handleLine(line)
			b, mErr := json.Marshal(resp)
			if mErr != nil {
				b, _ = json.Marshal(errResp(errf("unknown_error", "marshal response: %v", mErr)))
			}
			out.Write(b)
			out.WriteByte('\n')
			out.Flush()
		}
		if err != nil {
			if err != io.EOF {
				fmt.Fprintln(os.Stderr, "read error:", err)
			}
			return
		}
	}
}

func errResp(e *adapterError) map[string]any {
	return map[string]any{"ok": false, "error": map[string]any{"type": e.Type, "message": e.Message}}
}

func handleLine(line []byte) (resp map[string]any) {
	defer func() {
		if r := recover(); r != nil {
			resp = errResp(errf("unknown_error", "panic: %v", r))
		}
	}()
	var req struct {
		Op    string          `json:"op"`
		Input json.RawMessage `json:"input"`
	}
	if err := json.Unmarshal(line, &req); err != nil {
		return errResp(invalid("request is not valid JSON: %v", err))
	}
	var input map[string]any
	dec := json.NewDecoder(bytes.NewReader(req.Input))
	dec.UseNumber()
	if err := dec.Decode(&input); err != nil || input == nil {
		return errResp(invalid("input must be a JSON object"))
	}
	var (
		value any
		err   error
	)
	switch req.Op {
	case "tx.sign":
		value, err = txSign(input)
	case "tx.decode":
		value, err = txDecode(input)
	case "key_authorization.sign":
		value, err = keyAuthSign(input)
	default:
		err = errf("unsupported", "unknown op %q", req.Op)
	}
	if err != nil {
		var ae *adapterError
		if errors.As(err, &ae) {
			return errResp(ae)
		}
		return errResp(errf("unknown_error", "%v", err))
	}
	return map[string]any{"ok": true, "value": value}
}

// ---------- input helpers ----------

func field(m map[string]any, key string) (any, bool) {
	v, ok := m[key]
	if !ok || v == nil {
		return nil, false
	}
	return v, true
}

func parseInt(v any, name string) (*big.Int, error) {
	var s string
	switch t := v.(type) {
	case string:
		s = t
	case json.Number:
		s = t.String()
	default:
		return nil, invalid("%s must be a decimal string", name)
	}
	if s == "" || strings.TrimLeft(s, "0123456789") != "" {
		return nil, invalid("%s must be a non-negative decimal integer, got %q", name, s)
	}
	n, ok := new(big.Int).SetString(s, 10)
	if !ok {
		return nil, invalid("%s: bad integer %q", name, s)
	}
	if n.BitLen() > 256 {
		return nil, invalid("%s exceeds uint256", name)
	}
	return n, nil
}

func reqInt(m map[string]any, key string) (*big.Int, error) {
	v, ok := field(m, key)
	if !ok {
		return nil, invalid("missing %s", key)
	}
	return parseInt(v, key)
}

func optInt(m map[string]any, key string) (*big.Int, error) {
	v, ok := field(m, key)
	if !ok {
		return nil, nil
	}
	return parseInt(v, key)
}

func toU64(n *big.Int, name string) (uint64, error) {
	if n == nil {
		return 0, nil
	}
	if !n.IsUint64() {
		return 0, invalid("%s exceeds uint64 (tempo-go represents it as uint64)", name)
	}
	return n.Uint64(), nil
}

func parseHex(v any, name string) ([]byte, error) {
	s, ok := v.(string)
	if !ok {
		return nil, invalid("%s must be a 0x hex string", name)
	}
	if !strings.HasPrefix(s, "0x") && !strings.HasPrefix(s, "0X") {
		return nil, invalid("%s must start with 0x", name)
	}
	b, err := hex.DecodeString(s[2:])
	if err != nil {
		return nil, invalid("%s: bad hex: %v", name, err)
	}
	return b, nil
}

func parseAddress(v any, name string) (common.Address, error) {
	b, err := parseHex(v, name)
	if err != nil {
		return common.Address{}, err
	}
	if len(b) != common.AddressLength {
		return common.Address{}, invalid("%s must be 20 bytes, got %d", name, len(b))
	}
	return common.BytesToAddress(b), nil
}

func parseHash(v any, name string) (common.Hash, error) {
	b, err := parseHex(v, name)
	if err != nil {
		return common.Hash{}, err
	}
	if len(b) != common.HashLength {
		return common.Hash{}, invalid("%s must be 32 bytes, got %d", name, len(b))
	}
	return common.BytesToHash(b), nil
}

func asObject(v any, name string) (map[string]any, error) {
	m, ok := v.(map[string]any)
	if !ok {
		return nil, invalid("%s must be an object", name)
	}
	return m, nil
}

func asArray(v any, name string) ([]any, error) {
	a, ok := v.([]any)
	if !ok {
		return nil, invalid("%s must be an array", name)
	}
	return a, nil
}

func newSigner(v any, name string) (*signer.Signer, error) {
	b, err := parseHex(v, name)
	if err != nil {
		return nil, err
	}
	if len(b) != 32 {
		return nil, invalid("%s must be 32 bytes", name)
	}
	s, err := signer.NewSigner(hex.EncodeToString(b))
	if err != nil {
		return nil, invalid("%s: %v", name, err)
	}
	return s, nil
}

// ---------- output helpers ----------

func hexb(b []byte) string         { return "0x" + hex.EncodeToString(b) }
func addr(a common.Address) string { return hexb(a.Bytes()) }
func dec(n *big.Int) string {
	if n == nil {
		return "0"
	}
	return n.String()
}
func decU(n uint64) string { return new(big.Int).SetUint64(n).String() }

// ---------- key authorization ----------

var keyTypes = map[string]uint8{
	"secp256k1": keychain.SignatureTypeSecp256k1,
	"p256":      keychain.SignatureTypeP256,
	"webauthn":  keychain.SignatureTypeWebAuthn,
}

var keyTypeNames = map[uint8]string{0: "secp256k1", 1: "p256", 2: "webauthn"}

func parseKeyAuthorization(m map[string]any) (*keychain.KeyAuthorization, error) {
	chainID, err := reqInt(m, "chainId")
	if err != nil {
		return nil, err
	}
	cid, err := toU64(chainID, "authorization.chainId")
	if err != nil {
		return nil, err
	}
	ktv, ok := field(m, "keyType")
	if !ok {
		return nil, invalid("missing keyType")
	}
	kts, _ := ktv.(string)
	kt, ok := keyTypes[kts]
	if !ok {
		return nil, invalid("unknown keyType %v", ktv)
	}
	kidv, ok := field(m, "keyId")
	if !ok {
		return nil, invalid("missing keyId")
	}
	keyID, err := parseAddress(kidv, "keyId")
	if err != nil {
		return nil, err
	}
	auth := keychain.NewKeyAuthorization(cid, kt, keyID)

	expiry, err := optInt(m, "expiry")
	if err != nil {
		return nil, err
	}
	if expiry != nil {
		e, err := toU64(expiry, "expiry")
		if err != nil {
			return nil, err
		}
		// tempo-go: Expiry == 0 means "never expires" (absent); an explicit 0 cannot be expressed.
		auth.WithExpiry(e)
	}

	if lv, ok := field(m, "limits"); ok {
		arr, err := asArray(lv, "limits")
		if err != nil {
			return nil, err
		}
		limits := make([]keychain.TokenLimit, 0, len(arr))
		for i, item := range arr {
			lm, err := asObject(item, fmt.Sprintf("limits[%d]", i))
			if err != nil {
				return nil, err
			}
			tv, ok := field(lm, "token")
			if !ok {
				return nil, invalid("limits[%d].token missing", i)
			}
			token, err := parseAddress(tv, "token")
			if err != nil {
				return nil, err
			}
			amount, err := reqInt(lm, "limit")
			if err != nil {
				return nil, err
			}
			tl := keychain.TokenLimit{Token: token, Amount: amount}
			period, err := optInt(lm, "period")
			if err != nil {
				return nil, err
			}
			if period != nil {
				p, err := toU64(period, "period")
				if err != nil {
					return nil, err
				}
				tl.Period = p
			}
			limits = append(limits, tl)
		}
		auth.WithLimits(limits)
	}
	if err := auth.Validate(); err != nil {
		return nil, invalid("tempo-go KeyAuthorization.Validate: %v", err)
	}
	return auth, nil
}

func keyAuthSign(input map[string]any) (any, error) {
	av, ok := field(input, "authorization")
	if !ok {
		return nil, invalid("missing authorization")
	}
	am, err := asObject(av, "authorization")
	if err != nil {
		return nil, err
	}
	auth, err := parseKeyAuthorization(am)
	if err != nil {
		return nil, err
	}
	pk, ok := field(input, "privateKey")
	if !ok {
		return nil, invalid("missing privateKey")
	}
	s, err := newSigner(pk, "privateKey")
	if err != nil {
		return nil, err
	}
	hash, err := auth.SignatureHash()
	if err != nil {
		return nil, invalid("SignatureHash: %v", err)
	}
	signed, err := auth.Sign(s)
	if err != nil {
		return nil, errf("unknown_error", "KeyAuthorization.Sign: %v", err)
	}
	serialized, err := rlp.EncodeToBytes(signed)
	if err != nil {
		return nil, errf("unknown_error", "rlp encode signed authorization: %v", err)
	}
	// Report the signature exactly as tempo-go embeds it: r || s || v with
	// v = 27 + yParity (key_authorization.go:210), matching the vectors.
	// SPEC.md asks for yParity 0/1 here; that conflict is reported to the lead.
	sig, ok := signed[1].([]byte)
	if !ok || len(sig) != 65 {
		return nil, errf("unknown_error", "unexpected signed authorization shape")
	}
	return map[string]any{
		"signingHash": hexb(hash.Bytes()),
		"signature":   hexb(sig),
		"serialized":  hexb(serialized),
	}, nil
}

// ---------- tx.sign ----------

func parseTx(m map[string]any) (*transaction.Tx, *keychain.KeyAuthorization, *signer.Signer, error) {
	tx := transaction.New()
	var err error
	if tx.ChainID, err = reqInt(m, "chainId"); err != nil {
		return nil, nil, nil, err
	}
	nonce, err := reqInt(m, "nonce")
	if err != nil {
		return nil, nil, nil, err
	}
	if tx.Nonce, err = toU64(nonce, "nonce"); err != nil {
		return nil, nil, nil, err
	}
	if tx.NonceKey, err = reqInt(m, "nonceKey"); err != nil {
		return nil, nil, nil, err
	}
	gas, err := reqInt(m, "gas")
	if err != nil {
		return nil, nil, nil, err
	}
	if tx.Gas, err = toU64(gas, "gas"); err != nil {
		return nil, nil, nil, err
	}
	if tx.MaxFeePerGas, err = reqInt(m, "maxFeePerGas"); err != nil {
		return nil, nil, nil, err
	}
	if tx.MaxPriorityFeePerGas, err = reqInt(m, "maxPriorityFeePerGas"); err != nil {
		return nil, nil, nil, err
	}
	cv, ok := field(m, "calls")
	if !ok {
		return nil, nil, nil, invalid("missing calls")
	}
	calls, err := asArray(cv, "calls")
	if err != nil {
		return nil, nil, nil, err
	}
	tx.Calls = make([]transaction.Call, 0, len(calls))
	for i, c := range calls {
		cm, err := asObject(c, fmt.Sprintf("calls[%d]", i))
		if err != nil {
			return nil, nil, nil, err
		}
		call := transaction.Call{}
		if tv, ok := field(cm, "to"); ok {
			to, err := parseAddress(tv, fmt.Sprintf("calls[%d].to", i))
			if err != nil {
				return nil, nil, nil, err
			}
			call.To = &to
		}
		if call.Value, err = reqInt(cm, "value"); err != nil {
			return nil, nil, nil, err
		}
		dv, ok := field(cm, "data")
		if !ok {
			call.Data = []byte{}
		} else if call.Data, err = parseHex(dv, fmt.Sprintf("calls[%d].data", i)); err != nil {
			return nil, nil, nil, err
		}
		tx.Calls = append(tx.Calls, call)
	}
	tx.AccessList = transaction.AccessList{}
	if alv, ok := field(m, "accessList"); ok {
		al, err := asArray(alv, "accessList")
		if err != nil {
			return nil, nil, nil, err
		}
		for i, item := range al {
			im, err := asObject(item, fmt.Sprintf("accessList[%d]", i))
			if err != nil {
				return nil, nil, nil, err
			}
			av, _ := field(im, "address")
			a, err := parseAddress(av, "accessList.address")
			if err != nil {
				return nil, nil, nil, err
			}
			tuple := transaction.AccessTuple{Address: a, StorageKeys: []common.Hash{}}
			if skv, ok := field(im, "storageKeys"); ok {
				sks, err := asArray(skv, "storageKeys")
				if err != nil {
					return nil, nil, nil, err
				}
				for _, sk := range sks {
					h, err := parseHash(sk, "storageKey")
					if err != nil {
						return nil, nil, nil, err
					}
					tuple.StorageKeys = append(tuple.StorageKeys, h)
				}
			}
			tx.AccessList = append(tx.AccessList, tuple)
		}
	}
	vb, err := optInt(m, "validBefore")
	if err != nil {
		return nil, nil, nil, err
	}
	if tx.ValidBefore, err = toU64(vb, "validBefore"); err != nil {
		return nil, nil, nil, err
	}
	va, err := optInt(m, "validAfter")
	if err != nil {
		return nil, nil, nil, err
	}
	if tx.ValidAfter, err = toU64(va, "validAfter"); err != nil {
		return nil, nil, nil, err
	}
	if fv, ok := field(m, "feeToken"); ok {
		if tx.FeeToken, err = parseAddress(fv, "feeToken"); err != nil {
			return nil, nil, nil, err
		}
	}
	var auth *keychain.KeyAuthorization
	var authSigner *signer.Signer
	if kav, ok := field(m, "keyAuthorization"); ok {
		kam, err := asObject(kav, "keyAuthorization")
		if err != nil {
			return nil, nil, nil, err
		}
		av, ok := field(kam, "authorization")
		if !ok {
			return nil, nil, nil, invalid("keyAuthorization.authorization missing")
		}
		am, err := asObject(av, "keyAuthorization.authorization")
		if err != nil {
			return nil, nil, nil, err
		}
		if auth, err = parseKeyAuthorization(am); err != nil {
			return nil, nil, nil, err
		}
		pk, ok := field(kam, "privateKey")
		if !ok {
			return nil, nil, nil, invalid("keyAuthorization.privateKey missing")
		}
		if authSigner, err = newSigner(pk, "keyAuthorization.privateKey"); err != nil {
			return nil, nil, nil, err
		}
	}
	return tx, auth, authSigner, nil
}

func txSign(input map[string]any) (any, error) {
	txv, ok := field(input, "tx")
	if !ok {
		return nil, invalid("missing tx")
	}
	txm, err := asObject(txv, "tx")
	if err != nil {
		return nil, err
	}
	tx, auth, authSigner, err := parseTx(txm)
	if err != nil {
		return nil, err
	}
	sv, ok := field(input, "signer")
	if !ok {
		return nil, invalid("missing signer")
	}
	sm, err := asObject(sv, "signer")
	if err != nil {
		return nil, err
	}

	var feePayer *signer.Signer
	if fpv, ok := field(input, "feePayerPrivateKey"); ok {
		if feePayer, err = newSigner(fpv, "feePayerPrivateKey"); err != nil {
			return nil, err
		}
		// Sponsored: sender signs the fee-payer form (0x00 marker, fee token skipped).
		tx.AwaitingFeePayer = true
	}

	if auth != nil {
		if err := auth.SignAndAttach(tx, authSigner); err != nil {
			return nil, invalid("KeyAuthorization.SignAndAttach: %v", err)
		}
	}

	if err := tx.Validate(); err != nil {
		return nil, invalid("tempo-go Tx.Validate: %v", err)
	}
	senderHash, err := transaction.GetSignPayload(tx)
	if err != nil {
		return nil, invalid("GetSignPayload: %v", err)
	}

	st, _ := sm["type"].(string)
	switch st {
	case "secp256k1":
		pk, ok := field(sm, "privateKey")
		if !ok {
			return nil, invalid("signer.privateKey missing")
		}
		s, err := newSigner(pk, "signer.privateKey")
		if err != nil {
			return nil, err
		}
		if err := transaction.SignTransaction(tx, s); err != nil {
			return nil, invalid("SignTransaction: %v", err)
		}
	case "keychain":
		pk, ok := field(sm, "accessKeyPrivateKey")
		if !ok {
			return nil, invalid("signer.accessKeyPrivateKey missing")
		}
		s, err := newSigner(pk, "signer.accessKeyPrivateKey")
		if err != nil {
			return nil, err
		}
		rv, ok := field(sm, "rootAccount")
		if !ok {
			return nil, invalid("signer.rootAccount missing")
		}
		root, err := parseAddress(rv, "signer.rootAccount")
		if err != nil {
			return nil, err
		}
		if err := keychain.SignWithAccessKey(tx, s, root); err != nil {
			return nil, invalid("SignWithAccessKey: %v", err)
		}
	default:
		return nil, invalid("unknown signer type %q", st)
	}

	var feePayerHash any
	if feePayer != nil {
		h, err := transaction.GetFeePayerSignPayload(tx, tx.From)
		if err != nil {
			return nil, errf("unknown_error", "GetFeePayerSignPayload: %v", err)
		}
		feePayerHash = hexb(h.Bytes())
		if err := transaction.AddFeePayerSignature(tx, feePayer); err != nil {
			return nil, errf("unknown_error", "AddFeePayerSignature: %v", err)
		}
	}

	raw, err := transaction.Serialize(tx, nil)
	if err != nil {
		return nil, errf("unknown_error", "Serialize: %v", err)
	}
	hash, err := tx.Hash()
	if err != nil {
		return nil, errf("unknown_error", "Hash: %v", err)
	}
	return map[string]any{
		"senderSigningHash":   hexb(senderHash.Bytes()),
		"feePayerSigningHash": feePayerHash,
		"raw":                 strings.ToLower(raw),
		"hash":                hexb(hash.Bytes()),
		"sender":              addr(tx.From),
	}, nil
}

// ---------- tx.decode ----------

func txDecode(input map[string]any) (any, error) {
	rv, ok := field(input, "raw")
	if !ok {
		return nil, invalid("missing raw")
	}
	rawStr, ok := rv.(string)
	if !ok {
		return nil, invalid("raw must be a hex string")
	}
	if _, err := parseHex(rawStr, "raw"); err != nil {
		return nil, errf("decode_error", "%v", err)
	}
	tx, err := transaction.Deserialize(rawStr)
	if err != nil {
		return nil, errf("decode_error", "Deserialize: %v", err)
	}
	if tx.KeyAuthorization != nil {
		// transaction.Tx.KeyAuthorization is the raw RLP list ([]interface{}); tempo-go has no
		// public decoder from it to keychain.KeyAuthorization.
		return nil, errf("unsupported", "tempo-go has no API to decode a transaction's key authorization (Tx.KeyAuthorization is raw RLP, transaction.go:28)")
	}
	if tx.Signature == nil {
		return nil, errf("decode_error", "transaction is unsigned")
	}

	var sender common.Address
	sigType := tx.Signature.Type
	switch sigType {
	case "secp256k1", "":
		sigType = "secp256k1"
		sender, err = transaction.VerifySignature(tx)
		if err != nil {
			return nil, errf("decode_error", "VerifySignature: %v", err)
		}
	case "keychain":
		_, root, err := keychain.VerifyAccessKeySignature(tx)
		if err != nil {
			return nil, errf("decode_error", "VerifyAccessKeySignature: %v", err)
		}
		sender = root
	default:
		return nil, errf("unsupported", "tempo-go cannot recover the sender of a %s signature off-chain (transaction.VerifySignature, signing.go:108)", sigType)
	}

	var feePayer any
	if tx.FeePayerSignature != nil {
		fp, err := transaction.VerifyFeePayerSignature(tx, sender)
		if err != nil {
			return nil, errf("decode_error", "VerifyFeePayerSignature: %v", err)
		}
		feePayer = addr(fp)
	}

	hash, err := tx.Hash()
	if err != nil {
		return nil, errf("decode_error", "Hash: %v", err)
	}

	calls := make([]any, 0, len(tx.Calls))
	for _, c := range tx.Calls {
		var to any
		if c.To != nil {
			to = addr(*c.To)
		}
		data := c.Data
		if data == nil {
			data = []byte{}
		}
		calls = append(calls, map[string]any{"to": to, "value": dec(c.Value), "data": hexb(data)})
	}
	al := make([]any, 0, len(tx.AccessList))
	for _, t := range tx.AccessList {
		keys := make([]string, 0, len(t.StorageKeys))
		for _, k := range t.StorageKeys {
			keys = append(keys, hexb(k.Bytes()))
		}
		al = append(al, map[string]any{"address": addr(t.Address), "storageKeys": keys})
	}
	var validBefore, validAfter, feeToken any
	if tx.ValidBefore != 0 {
		validBefore = decU(tx.ValidBefore)
	}
	if tx.ValidAfter != 0 {
		validAfter = decU(tx.ValidAfter)
	}
	if tx.FeeToken != (common.Address{}) {
		feeToken = addr(tx.FeeToken)
	}
	return map[string]any{
		"tx": map[string]any{
			"chainId":              dec(tx.ChainID),
			"nonce":                decU(tx.Nonce),
			"nonceKey":             dec(tx.NonceKey),
			"gas":                  decU(tx.Gas),
			"maxFeePerGas":         dec(tx.MaxFeePerGas),
			"maxPriorityFeePerGas": dec(tx.MaxPriorityFeePerGas),
			"calls":                calls,
			"accessList":           al,
			"validBefore":          validBefore,
			"validAfter":           validAfter,
			"feeToken":             feeToken,
			"keyAuthorization":     nil,
		},
		"signatureType": sigType,
		"sender":        addr(sender),
		"feePayer":      feePayer,
		"hash":          hexb(hash.Bytes()),
	}, nil
}
