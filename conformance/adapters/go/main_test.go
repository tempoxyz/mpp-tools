package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/tempoxyz/mpp-go/pkg/mpp"
)

func TestRunHTTPPaymentRequest(t *testing.T) {
	var challenge *mpp.Challenge
	var bodies []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		bodies = append(bodies, string(body))
		if r.Header.Get("Authorization") == "" {
			w.Header().Set("WWW-Authenticate", challenge.ToAuthenticate(challenge.Realm))
			w.WriteHeader(http.StatusPaymentRequired)
			return
		}

		credential, err := mpp.ParseCredential(r.Header.Get("Authorization"))
		if err != nil || credential.Payload["signature"] != "0xabc" || credential.Source != "did:key:payer" {
			http.Error(w, "invalid credential", http.StatusBadRequest)
			return
		}
		w.Header().Set("Payment-Receipt", "receipt")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	defer server.Close()

	serverURL, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	challenge = mpp.NewChallenge("secret", serverURL.Host, "tempo", "charge", map[string]any{"amount": "100"})
	body := `{"prompt":"hello"}`
	response := runHTTPPaymentRequest(marshalRequest(t, httpPaymentRequest{
		URL:     server.URL,
		Method:  http.MethodPost,
		Headers: map[string]string{"Content-Type": "application/json"},
		Body:    &body,
		Payment: httpPayment{
			Payload: map[string]any{"type": "transaction", "signature": "0xabc"},
			Source:  json.RawMessage(`"did:key:payer"`),
		},
		Mode: "payment",
	}))

	if !response.OK {
		t.Fatalf("runHTTPPaymentRequest() error = %#v", response.Error)
	}
	value := response.Value.(map[string]any)
	if value["status"] != http.StatusOK {
		t.Fatalf("status = %v, want %d", value["status"], http.StatusOK)
	}
	if len(bodies) != 2 || bodies[0] != body || bodies[1] != body {
		t.Fatalf("request bodies = %#v, want preserved %q", bodies, body)
	}
	header := value["headers"].(map[string]string)
	if header["Payment-Receipt"] != "receipt" {
		t.Fatalf("Payment-Receipt = %q, want receipt", header["Payment-Receipt"])
	}
}

func TestRunHTTPPaymentRequestPlainDoesNotAuthorize(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Error("plain request sent Authorization")
		}
		w.WriteHeader(http.StatusPaymentRequired)
	}))
	defer server.Close()

	response := runHTTPPaymentRequest(marshalRequest(t, newHTTPPaymentRequest(server.URL, "plain")))
	if !response.OK {
		t.Fatalf("runHTTPPaymentRequest() error = %#v", response.Error)
	}
	if status := response.Value.(map[string]any)["status"]; status != http.StatusPaymentRequired {
		t.Fatalf("status = %v, want %d", status, http.StatusPaymentRequired)
	}
}

func TestRunHTTPPaymentRequestRejectsCrossOriginRedirect(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Error("redirect target should not receive request")
	}))
	defer target.Close()

	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer origin.Close()

	response := runHTTPPaymentRequest(marshalRequest(t, newHTTPPaymentRequest(origin.URL, "payment")))
	if response.OK || response.Error == nil {
		t.Fatalf("runHTTPPaymentRequest() = %#v, want http_error", response)
	}
	if response.Error.Type != "http_error" || !strings.Contains(response.Error.Message, "Refusing to send payment credential across redirect") {
		t.Fatalf("error = %#v, want normalized redirect error", response.Error)
	}
}

func TestRunHTTPPaymentRequestRejectsUnsupportedMode(t *testing.T) {
	response := runHTTPPaymentRequest(marshalRequest(t, newHTTPPaymentRequest("https://example.com", "unsupported")))
	if response.OK || response.Error == nil || response.Error.Type != "unsupported_operation" {
		t.Fatalf("runHTTPPaymentRequest() = %#v, want unsupported_operation", response)
	}
}

func newHTTPPaymentRequest(rawURL, mode string) httpPaymentRequest {
	return httpPaymentRequest{
		URL:     rawURL,
		Method:  http.MethodGet,
		Headers: map[string]string{},
		Mode:    mode,
	}
}

func marshalRequest(t *testing.T, request httpPaymentRequest) json.RawMessage {
	t.Helper()
	encoded, err := json.Marshal(request)
	if err != nil {
		t.Fatal(fmt.Errorf("marshal request: %w", err))
	}
	return encoded
}
