package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/tempoxyz/mpp-go/pkg/mpp"
	"github.com/tempoxyz/mpp-go/pkg/server"
)

type commandResponse struct {
	Success   bool   `json:"success"`
	Result    any    `json:"result,omitempty"`
	Error     string `json:"error,omitempty"`
	ErrorType string `json:"error_type,omitempty"`
}

type adapterRequest struct {
	Schema int             `json:"schema"`
	Op     string          `json:"op"`
	Input  json.RawMessage `json:"input"`
}

type adapterResponse struct {
	OK    bool          `json:"ok"`
	Value any           `json:"value,omitempty"`
	Error *adapterError `json:"error,omitempty"`
}

type adapterError struct {
	Type    string `json:"type"`
	Message string `json:"message"`
}

type serverVerifyInput struct {
	Realm             string         `json:"realm"`
	SecretKey         string         `json:"secretKey"`
	Method            string         `json:"method"`
	Intent            string         `json:"intent"`
	Request           map[string]any `json:"request"`
	Expires           string         `json:"expires,omitempty"`
	VerificationError string         `json:"verificationError,omitempty"`
	Credential        credentialDTO  `json:"credential"`
}

type credentialDTO struct {
	Challenge mpp.Challenge   `json:"challenge"`
	Payload   map[string]any  `json:"payload"`
	Source    json.RawMessage `json:"source,omitempty"`
}

type conformanceIntent struct {
	name         string
	method       string
	errorMessage string
}

func (i conformanceIntent) Name() string {
	return i.name
}

func (i conformanceIntent) Verify(_ context.Context, _ *mpp.Credential, _ map[string]any) (*mpp.Receipt, error) {
	if i.errorMessage != "" {
		return nil, fmt.Errorf("%s", i.errorMessage)
	}
	return &mpp.Receipt{
		Status:    "success",
		Timestamp: time.Date(2026, 1, 29, 12, 0, 0, 0, time.UTC),
		Reference: "conformance-receipt",
		Method:    i.method,
	}, nil
}

func main() {
	if len(os.Args) < 2 {
		handleAdapterRequest()
		return
	}

	command := os.Args[1]
	input, err := io.ReadAll(os.Stdin)
	if err != nil {
		printJSON(commandResponse{Success: false, Error: fmt.Sprintf("Failed to read stdin: %v", err), ErrorType: "unknown_error"})
		return
	}

	trimmed := strings.TrimSpace(string(input))
	switch command {
	case "parse-www-authenticate":
		handleParseWWWAuthenticate(trimmed)
	case "parse-authorization":
		handleParseAuthorization(trimmed)
	case "parse-receipt":
		handleParseReceipt(trimmed)
	case "format-www-authenticate":
		handleFormatWWWAuthenticate(trimmed)
	case "format-authorization":
		handleFormatAuthorization(trimmed)
	case "format-receipt":
		handleFormatReceipt(trimmed)
	case "base64url-encode":
		printJSON(commandResponse{Success: true, Result: base64.RawURLEncoding.EncodeToString([]byte(trimmed))})
	case "base64url-decode":
		handleBase64URLDecode(trimmed)
	case "generate-challenge-id":
		handleGenerateChallengeID(trimmed)
	default:
		printJSON(commandResponse{Success: false, Error: fmt.Sprintf("Unknown command: %s", command), ErrorType: "unknown_error"})
	}
}

func handleParseWWWAuthenticate(input string) {
	challenge, err := mpp.ParseChallenge(input)
	if err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "parse_error"})
		return
	}
	printJSON(commandResponse{Success: true, Result: toMap(challenge)})
}

func handleParseAuthorization(input string) {
	credential, err := mpp.ParseCredential(input)
	if err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "parse_error"})
		return
	}
	printJSON(commandResponse{Success: true, Result: toMap(credential)})
}

func handleParseReceipt(input string) {
	receipt, err := parseConformanceReceipt(input)
	if err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "parse_error"})
		return
	}
	printJSON(commandResponse{Success: true, Result: receiptToMap(receipt)})
}

func handleFormatWWWAuthenticate(input string) {
	var challenge mpp.Challenge
	if err := json.Unmarshal([]byte(input), &challenge); err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "format_error"})
		return
	}

	printJSON(commandResponse{Success: true, Result: challenge.ToAuthenticate(challenge.Realm)})
}

func handleFormatAuthorization(input string) {
	var credential mpp.Credential
	if err := json.Unmarshal([]byte(input), &credential); err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "format_error"})
		return
	}

	printJSON(commandResponse{Success: true, Result: credential.ToAuthorization()})
}

func handleFormatReceipt(input string) {
	var data map[string]any
	if err := json.Unmarshal([]byte(input), &data); err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "format_error"})
		return
	}

	timestamp, err := time.Parse(time.RFC3339, stringField(data, "timestamp"))
	if err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "format_error"})
		return
	}

	receipt := &mpp.Receipt{
		Status:     stringField(data, "status"),
		Timestamp:  timestamp.UTC(),
		Reference:  stringField(data, "reference"),
		Method:     stringField(data, "method"),
		ExternalID: stringField(data, "externalId"),
	}
	if extra, ok := data["extra"].(map[string]any); ok {
		receipt.Extra = extra
	}

	formatted, err := formatConformanceReceipt(receipt)
	if err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "format_error"})
		return
	}

	printJSON(commandResponse{Success: true, Result: formatted})
}

func handleBase64URLDecode(input string) {
	decoded, err := base64.RawURLEncoding.DecodeString(input)
	if err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "encoding_error"})
		return
	}
	printJSON(commandResponse{Success: true, Result: string(decoded)})
}

func handleGenerateChallengeID(input string) {
	var data map[string]any
	if err := json.Unmarshal([]byte(input), &data); err != nil {
		printJSON(commandResponse{Success: false, Error: err.Error(), ErrorType: "generation_error"})
		return
	}

	result := generateConformanceChallengeID(
		stringField(data, "secretKey"),
		stringField(data, "realm"),
		stringField(data, "method"),
		stringField(data, "intent"),
		mapField(data, "request"),
		stringField(data, "expires"),
		stringField(data, "digest"),
		stringField(data, "opaque"),
	)
	printJSON(commandResponse{Success: true, Result: result})
}

func handleAdapterRequest() {
	input, err := io.ReadAll(os.Stdin)
	if err != nil {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: "unknown_error", Message: err.Error()}})
		return
	}

	var request adapterRequest
	if err := json.Unmarshal(input, &request); err != nil {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: "unknown_error", Message: err.Error()}})
		return
	}

	if request.Op == "server.verify" {
		handleServerVerify(request.Input)
		return
	}

	command, ok := legacyCommandForOperation(request.Op)
	if !ok {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: "unsupported_operation", Message: fmt.Sprintf("Unknown operation: %s", request.Op)}})
		return
	}
	legacyInput, err := legacyInputForOperation(request.Op, request.Input)
	if err != nil {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: "unknown_error", Message: err.Error()}})
		return
	}
	result := runLegacyCommand(command, legacyInput, nil)
	printAdapterFromLegacy(request.Op, result)
}

func legacyCommandForOperation(op string) (string, bool) {
	commands := map[string]string{
		"challenge.parse":   "parse-www-authenticate",
		"challenge.format":  "format-www-authenticate",
		"credential.parse":  "parse-authorization",
		"credential.format": "format-authorization",
		"receipt.parse":     "parse-receipt",
		"receipt.format":    "format-receipt",
		"base64url.encode":  "base64url-encode",
		"base64url.decode":  "base64url-decode",
		"challenge.id":      "generate-challenge-id",
	}
	command, ok := commands[op]
	return command, ok
}

func legacyInputForOperation(op string, raw json.RawMessage) (string, error) {
	if strings.HasSuffix(op, ".parse") {
		var input struct {
			Header string `json:"header"`
		}
		if err := json.Unmarshal(raw, &input); err != nil {
			return "", err
		}
		return input.Header, nil
	}
	if strings.HasPrefix(op, "base64url.") {
		var input struct {
			Text string `json:"text"`
		}
		if err := json.Unmarshal(raw, &input); err != nil {
			return "", err
		}
		return input.Text, nil
	}
	return string(raw), nil
}

func runLegacyCommand(command string, input string, env map[string]string) commandResponse {
	executable, err := os.Executable()
	if err != nil {
		return commandResponse{Success: false, Error: err.Error(), ErrorType: "unknown_error"}
	}
	cmd := exec.Command(executable, command)
	cmd.Stdin = strings.NewReader(input)
	cmd.Env = os.Environ()
	for key, value := range env {
		cmd.Env = append(cmd.Env, key+"="+value)
	}
	output, err := cmd.Output()
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			return commandResponse{Success: false, Error: string(exit.Stderr), ErrorType: "unknown_error"}
		}
		return commandResponse{Success: false, Error: err.Error(), ErrorType: "unknown_error"}
	}
	var generic map[string]any
	if err := json.Unmarshal(output, &generic); err != nil {
		return commandResponse{Success: false, Error: err.Error(), ErrorType: "unknown_error"}
	}
	if _, ok := generic["success"]; !ok {
		return commandResponse{Success: true, Result: generic}
	}
	encoded, _ := json.Marshal(generic)
	var result commandResponse
	if err := json.Unmarshal(encoded, &result); err != nil {
		return commandResponse{Success: false, Error: err.Error(), ErrorType: "unknown_error"}
	}
	return result
}

func handleServerVerify(raw json.RawMessage) {
	var input serverVerifyInput
	if err := json.Unmarshal(raw, &input); err != nil {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: "verification_error", Message: err.Error()}})
		return
	}

	credential, err := input.Credential.toCredential()
	if err != nil {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: "verification_error", Message: err.Error()}})
		return
	}

	result, err := server.VerifyOrChallenge(context.Background(), server.VerifyParams{
		Authorization: credential.ToAuthorization(),
		Intent: conformanceIntent{
			name:         input.Intent,
			method:       input.Method,
			errorMessage: input.VerificationError,
		},
		Request:   input.Request,
		Realm:     input.Realm,
		SecretKey: input.SecretKey,
		Method:    input.Method,
		Expires:   input.Expires,
	})
	if err != nil {
		printJSON(adapterResponse{OK: true, Value: map[string]any{
			"ok":        false,
			"errorType": serverErrorType(err),
		}})
		return
	}
	if result == nil || result.Receipt == nil {
		printJSON(adapterResponse{OK: true, Value: map[string]any{
			"ok":        false,
			"errorType": "unknown_error",
		}})
		return
	}
	printJSON(adapterResponse{OK: true, Value: map[string]any{
		"ok":      true,
		"receipt": receiptToMap(result.Receipt),
	}})
}

func (c credentialDTO) toCredential() (*mpp.Credential, error) {
	payload := c.Payload
	if payload == nil {
		payload = map[string]any{}
	}
	source := ""
	if len(c.Source) > 0 && string(c.Source) != "null" {
		if err := json.Unmarshal(c.Source, &source); err != nil {
			return nil, fmt.Errorf("unsupported credential source shape")
		}
	}
	return &mpp.Credential{
		Challenge: c.Challenge.ToEcho(),
		Payload:   payload,
		Source:    source,
	}, nil
}

func serverErrorType(err error) string {
	paymentErr, ok := err.(*mpp.PaymentError)
	if !ok {
		return "unknown_error"
	}
	switch paymentErr.Type {
	case mpp.ErrorTypeMalformedCredential:
		return "malformed_credential"
	case mpp.ErrorTypeInvalidChallenge:
		return "invalid_challenge"
	case mpp.ErrorTypePaymentExpired:
		return "payment_expired"
	case mpp.ErrorTypeVerificationFailed:
		return "verification_failed"
	default:
		return "unknown_error"
	}
}

func printAdapterFromLegacy(op string, result commandResponse) {
	if !result.Success {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: result.ErrorType, Message: result.Error}})
		return
	}
	printJSON(adapterResponse{OK: true, Value: adapterValueForOperation(op, result.Result)})
}

func adapterValueForOperation(op string, result any) any {
	if strings.HasSuffix(op, ".format") {
		return map[string]any{"header": result}
	}
	if strings.HasPrefix(op, "base64url.") {
		return map[string]any{"text": result}
	}
	if op == "challenge.id" {
		return map[string]any{"id": result}
	}
	return result
}

// toMap marshals a value with its custom MarshalJSON and returns a generic map.
func toMap(v any) map[string]any {
	b, err := json.Marshal(v)
	if err != nil {
		return map[string]any{}
	}
	var m map[string]any
	json.Unmarshal(b, &m)
	return m
}

func receiptToMap(receipt *mpp.Receipt) map[string]any {
	result := map[string]any{
		"status":    receipt.Status,
		"timestamp": formatReceiptTimestamp(receipt.Timestamp),
		"reference": receipt.Reference,
	}
	if receipt.Method != "" {
		result["method"] = receipt.Method
	}
	if receipt.ExternalID != "" {
		result["externalId"] = receipt.ExternalID
	}
	if len(receipt.Extra) > 0 {
		result["extra"] = receipt.Extra
	}
	return result
}

// parseConformanceReceipt is stricter than mpp.ParsePaymentReceipt: it requires
// method and timestamp fields that the library treats as optional.
func parseConformanceReceipt(header string) (*mpp.Receipt, error) {
	decoded, err := mpp.B64Decode(strings.TrimSpace(header))
	if err != nil {
		return nil, fmt.Errorf("mpp: invalid receipt encoding: %w", err)
	}

	status := stringField(decoded, "status")
	if status == "" {
		return nil, fmt.Errorf("mpp: receipt missing status")
	}
	if status != "success" {
		return nil, fmt.Errorf("mpp: invalid receipt status: %q", status)
	}

	method := stringField(decoded, "method")
	if method == "" {
		return nil, fmt.Errorf("mpp: receipt missing method")
	}

	timestampRaw := stringField(decoded, "timestamp")
	if timestampRaw == "" {
		return nil, fmt.Errorf("mpp: receipt missing timestamp")
	}
	timestamp, err := time.Parse(time.RFC3339Nano, timestampRaw)
	if err != nil {
		return nil, fmt.Errorf("mpp: invalid receipt timestamp: %w", err)
	}

	reference := stringField(decoded, "reference")
	if reference == "" {
		return nil, fmt.Errorf("mpp: receipt missing reference")
	}

	receipt := &mpp.Receipt{
		Status:     status,
		Method:     method,
		Timestamp:  timestamp.UTC(),
		Reference:  reference,
		ExternalID: stringField(decoded, "externalId"),
	}
	if extra, ok := decoded["extra"].(map[string]any); ok {
		receipt.Extra = extra
	}

	return receipt, nil
}

// generateConformanceChallengeID computes an HMAC-SHA256 challenge ID with
// raw-string opaque support. The conformance spec allows opaque to be a plain
// string placed directly in the pipe-delimited HMAC input, which differs from
// the library's map[string]string encoding. Once the spec settles on a single
// encoding this can be replaced with mpp.GenerateChallengeID.
func generateConformanceChallengeID(secretKey, realm, method, intent string, request map[string]any, expires, digest, opaque string) string {
	requestB64, _ := encodeJSONBase64URL(request)

	input := strings.Join([]string{
		realm,
		method,
		intent,
		requestB64,
		expires,
		digest,
		opaque,
	}, "|")

	mac := hmac.New(sha256.New, []byte(secretKey))
	mac.Write([]byte(input))

	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func formatConformanceReceipt(receipt *mpp.Receipt) (string, error) {
	payload := map[string]any{
		"status":    receipt.Status,
		"timestamp": formatReceiptTimestamp(receipt.Timestamp),
		"reference": receipt.Reference,
		"method":    receipt.Method,
	}
	if receipt.ExternalID != "" {
		payload["externalId"] = receipt.ExternalID
	}
	if len(receipt.Extra) > 0 {
		payload["extra"] = receipt.Extra
	}

	encoded, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}

	return base64.RawURLEncoding.EncodeToString(encoded), nil
}

func formatReceiptTimestamp(timestamp time.Time) string {
	return timestamp.UTC().Format(time.RFC3339Nano)
}

func encodeJSONBase64URL(data map[string]any) (string, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(data); err != nil {
		return "", err
	}
	encoded := strings.TrimSuffix(buffer.String(), "\n")
	return base64.RawURLEncoding.EncodeToString([]byte(encoded)), nil
}

func stringField(data map[string]any, key string) string {
	value, ok := data[key]
	if !ok || value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return text
	}
	return fmt.Sprint(value)
}

func mapField(data map[string]any, key string) map[string]any {
	value, ok := data[key]
	if !ok || value == nil {
		return map[string]any{}
	}
	if mapped, ok := value.(map[string]any); ok {
		return mapped
	}
	return map[string]any{}
}

func printJSON(value any) {
	encoded, err := json.Marshal(value)
	if err != nil {
		fmt.Fprintf(os.Stdout, `{"success":false,"error":%q,"error_type":"unknown_error"}`+"\n", err.Error())
		return
	}
	fmt.Fprintln(os.Stdout, string(encoded))
}
