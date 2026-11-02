package main

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/tempoxyz/mpp-go/pkg/mpp"
)

func TestHTTPServerSettlesOnceAndDeclines(t *testing.T) {
	handler, err := newHTTPServerHandler(httpServerInput{
		Realm:     "pairs.conformance",
		SecretKey: "pair-conformance-secret-key-32-bytes-minimum",
		Routes: []httpServerRoute{{
			Path: "/paid",
			Offers: []httpServerOffer{{
				Method:  "tempo",
				Intent:  "charge",
				Request: map[string]string{"amount": "1000", "currency": "USD", "recipient": "merchant"},
				Expires: "2099-01-01T00:00:00Z",
			}},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	get := func(path, authorization string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodGet, "http://127.0.0.1"+path, nil)
		if authorization != "" {
			request.Header.Set("Authorization", authorization)
		}
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, request)
		return recorder
	}
	pay := func(signature string) string {
		unpaid := get("/paid", "")
		if unpaid.Code != http.StatusPaymentRequired {
			t.Fatalf("unpaid status = %d, want 402", unpaid.Code)
		}
		challenge, err := mpp.ParseChallenge(unpaid.Header().Get("WWW-Authenticate"))
		if err != nil {
			t.Fatal(err)
		}
		authorization, err := mpp.FormatCredential(&mpp.Credential{
			Challenge: challenge.ToEcho(),
			Payload:   map[string]any{"type": "transaction", "signature": signature},
		})
		if err != nil {
			t.Fatal(err)
		}
		return authorization
	}

	if got := get("/missing", "").Code; got != http.StatusNotFound {
		t.Fatalf("unknown route status = %d, want 404", got)
	}
	authorization := pay("0xabc")
	paid := get("/paid", authorization)
	if paid.Code != http.StatusOK || paid.Header().Get("Payment-Receipt") == "" {
		t.Fatalf("paid status = %d, receipt %q", paid.Code, paid.Header().Get("Payment-Receipt"))
	}
	receipt, err := mpp.ParseReceipt(paid.Header().Get("Payment-Receipt"))
	if err != nil || receipt.Reference != "ref-0xabc" {
		t.Fatalf("receipt = %+v, err %v", receipt, err)
	}
	if got := get("/paid", authorization).Code; got != http.StatusForbidden {
		t.Fatalf("replayed status = %d, want 403", got)
	}
	if got := get("/paid", pay("0xdecline")).Code; got != http.StatusPaymentRequired {
		t.Fatalf("declined status = %d, want 402", got)
	}
}
