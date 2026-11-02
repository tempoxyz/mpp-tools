package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/tempoxyz/mpp-go/pkg/mpp"
	"github.com/tempoxyz/mpp-go/pkg/server"
)

type httpServerOffer struct {
	Method  string            `json:"method"`
	Intent  string            `json:"intent"`
	Request map[string]string `json:"request"`
	Expires string            `json:"expires,omitempty"`
	Meta    map[string]string `json:"meta,omitempty"`
}

type httpServerRoute struct {
	Path        string            `json:"path"`
	Offers      []httpServerOffer `json:"offers"`
	EchoHeaders []string          `json:"echoHeaders,omitempty"`
}

type httpServerInput struct {
	Port         int               `json:"port"`
	Realm        string            `json:"realm"`
	SecretKey    string            `json:"secretKey"`
	RequiresAuth bool              `json:"requiresAuth"`
	Routes       []httpServerRoute `json:"routes"`
}

// settledChallenges records the challenge ids the stand-in methods settled, so
// a replayed credential is rejected as mpp-go's own charge methods reject one.
type settledChallenges struct {
	mu  sync.Mutex
	ids map[string]bool
}

func (s *settledChallenges) claim(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ids[id] {
		return false
	}
	s.ids[id] = true
	return true
}

// conformanceServerMethod is the stand-in charge method every http.server offer
// uses: its payload is { type: "transaction", signature }, it declines the
// signature 0xdecline, settles each challenge once and returns a fixed receipt.
type conformanceServerMethod struct {
	name    string
	settled *settledChallenges
}

func (m conformanceServerMethod) Name() string { return m.name }

func (m conformanceServerMethod) Intents() map[string]server.Intent {
	return map[string]server.Intent{"charge": conformanceChargeIntent(m)}
}

type conformanceChargeIntent conformanceServerMethod

func (i conformanceChargeIntent) Name() string { return "charge" }

func (i conformanceChargeIntent) Verify(_ context.Context, credential *mpp.Credential, _ map[string]any) (*mpp.Receipt, error) {
	payloadType, _ := credential.Payload["type"].(string)
	signature, ok := credential.Payload["signature"].(string)
	if payloadType != "transaction" || !ok {
		return nil, mpp.ErrInvalidPayload("payload must be { type: \"transaction\", signature: string }")
	}
	if signature == "0xdecline" {
		return nil, mpp.ErrVerificationFailed("payment declined")
	}
	if !i.settled.claim(credential.Challenge.ID) {
		return nil, mpp.ErrCredentialReplayed("credential already used")
	}
	return &mpp.Receipt{
		Status:    "success",
		Timestamp: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
		Reference: "ref-" + signature,
		Method:    i.name,
	}, nil
}

// newHTTPServerHandler builds the payment-protected routes of an http.server
// scenario with mpp-go's server engine: ChargeMiddleware for one offer,
// ComposeMiddleware for several.
func newHTTPServerHandler(input httpServerInput) (http.Handler, error) {
	settled := &settledChallenges{ids: map[string]bool{}}
	payments := map[string]*server.Mpp{}
	for _, route := range input.Routes {
		for _, offer := range route.Offers {
			if _, ok := payments[offer.Method]; ok {
				continue
			}
			payment, err := server.New(
				conformanceServerMethod{name: offer.Method, settled: settled},
				input.Realm,
				input.SecretKey,
				server.WithRequiresAuth(input.RequiresAuth),
			)
			if err != nil {
				return nil, err
			}
			payments[offer.Method] = payment
		}
	}

	mux := http.NewServeMux()
	routes := map[string]http.Handler{}
	for _, route := range input.Routes {
		route := route
		if len(route.Offers) == 0 {
			return nil, fmt.Errorf("route %s has no offers", route.Path)
		}
		offers := make([]server.Offer, 0, len(route.Offers))
		for _, offer := range route.Offers {
			if offer.Intent != "" && offer.Intent != "charge" {
				return nil, fmt.Errorf("unsupported intent %q", offer.Intent)
			}
			offers = append(offers, server.Offer{
				Mpp: payments[offer.Method],
				Params: server.ChargeParams{
					Amount:    offer.Request["amount"],
					Currency:  offer.Request["currency"],
					Recipient: offer.Request["recipient"],
					Expires:   offer.Expires,
					Meta:      offer.Meta,
				},
			})
		}
		paid := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body := map[string]any{"ok": true, "path": route.Path}
			for _, name := range route.EchoHeaders {
				if values := r.Header.Values(name); len(values) > 0 {
					body[name] = values[0]
				} else {
					body[name] = nil
				}
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(body)
		})
		var protect func(http.Handler) http.Handler
		if len(offers) == 1 {
			protect = server.ChargeMiddleware(offers[0].Mpp, offers[0].Params)
		} else {
			protect = server.ComposeMiddleware(offers...)
		}
		routes[route.Path] = protect(paid)
	}
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		handler, ok := routes[r.URL.Path]
		if !ok {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		handler.ServeHTTP(w, r)
	})
	return mux, nil
}

// handleHTTPServer starts the http.server operation: it writes the adapter
// response with the server URL, then serves until the process is terminated.
func handleHTTPServer(raw json.RawMessage) {
	var input httpServerInput
	if err := json.Unmarshal(raw, &input); err != nil {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: "http_error", Message: err.Error()}})
		return
	}
	handler, err := newHTTPServerHandler(input)
	if err != nil {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: "http_error", Message: err.Error()}})
		return
	}
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", input.Port))
	if err != nil {
		printJSON(adapterResponse{OK: false, Error: &adapterError{Type: "http_error", Message: err.Error()}})
		return
	}
	printJSON(adapterResponse{OK: true, Value: map[string]any{"url": "http://" + listener.Addr().String()}})
	_ = http.Serve(listener, handler)
}
