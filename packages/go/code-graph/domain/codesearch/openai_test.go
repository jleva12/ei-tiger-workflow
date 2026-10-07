package codesearch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestOpenAIWindowsAreLosslessAndResponseOrderIsChecked(t *testing.T) {
	var received strings.Builder
	duplicate := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/embeddings" || r.Header.Get("Authorization") != "Bearer test-only" {
			t.Error("incorrect request")
		}
		var q struct {
			Input      []string
			Model      string
			Dimensions int
			Encoding   string `json:"encoding_format"`
		}
		if err := json.NewDecoder(r.Body).Decode(&q); err != nil {
			t.Error(err)
		}
		if q.Model != "test-model" || q.Dimensions != 2 || q.Encoding != "float" {
			t.Error("incorrect model configuration")
		}
		var data []map[string]any
		for _, s := range q.Input {
			received.WriteString(s)
			if len(s) > 7500 {
				t.Error("oversized window")
			}
		}
		for i := len(q.Input) - 1; i >= 0; i-- {
			index := i
			if duplicate {
				index = 0
			}
			data = append(data, map[string]any{"index": index, "embedding": []float64{1, 0}})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": data})
	}))
	defer server.Close()
	p, err := NewOpenAI(OpenAIConfig{APIKey: "test-only", BaseURL: server.URL, Model: "test-model", Dimensions: 2})
	if err != nil {
		t.Fatal(err)
	}
	body := strings.Repeat("中文λ source()\n", 19000)
	v, err := EmbedText(context.Background(), p, body)
	if err != nil || len(v) != 2 || v[0] != 1 || received.String() != body {
		t.Fatalf("lossy embedding windows: %v", err)
	}
	duplicate = true
	if _, err = p.Embed(context.Background(), []string{"one", "two"}); err == nil {
		t.Fatal("duplicate response index accepted")
	}
}

func TestOpenAIPermanentHTTPFailuresNeverRetry(t *testing.T) {
	for _, tc := range []struct {
		status int
		body   string
	}{
		{400, `{"error":{"code":"invalid_request"}}`}, {401, `{}`}, {403, `{}`}, {404, `{}`}, {422, `{}`}, {501, `{}`},
		{429, `{"error":{"code":"insufficient_quota","message":"private source"}}`},
		{429, `{"error":{"type":"insufficient_quota"}}`},
	} {
		calls := 0
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls++
			w.WriteHeader(tc.status)
			_, _ = w.Write([]byte(tc.body))
		}))
		p, err := NewOpenAI(OpenAIConfig{APIKey: "test", BaseURL: srv.URL, Dimensions: 2})
		if err != nil {
			t.Fatal(err)
		}
		_, err = p.Embed(context.Background(), []string{"source"})
		srv.Close()
		var provider *ProviderError
		if calls != 1 || !errors.As(err, &provider) || provider.Retryable() || strings.Contains(err.Error(), "private") {
			t.Fatalf("status %d calls %d: %v", tc.status, calls, err)
		}
	}
}

func TestOpenAIRetriesTransientFailureThenSucceeds(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte(`{"data":[{"index":0,"embedding":[1,0]}]}`))
	}))
	defer srv.Close()
	p, err := NewOpenAI(OpenAIConfig{APIKey: "test", BaseURL: srv.URL, Dimensions: 2})
	if err != nil {
		t.Fatal(err)
	}
	_, err = p.Embed(context.Background(), []string{"source"})
	if err != nil || calls != 2 {
		t.Fatalf("recovery calls %d: %v", calls, err)
	}
}

func TestRetryAfterDelay(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	for _, tc := range []struct {
		header string
		want   time.Duration
	}{
		{"10", 10 * time.Second}, {now.Add(20 * time.Second).Format(http.TimeFormat), 20 * time.Second}, {"invalid", time.Second}, {"-1", time.Second},
	} {
		if got := retryDelay(tc.header, time.Second, now); got != tc.want {
			t.Fatalf("%q: %s want %s", tc.header, got, tc.want)
		}
	}
}

func TestOpenAIRejectsRedirectAndDoesNotLeakProviderBody(t *testing.T) {
	status := http.StatusUnauthorized
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", "http://localhost:1/secret")
		w.WriteHeader(status)
		_, _ = w.Write([]byte("private source and key"))
	}))
	defer server.Close()
	p, err := NewOpenAI(OpenAIConfig{APIKey: "test-only", BaseURL: server.URL, Dimensions: 2})
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []int{http.StatusUnauthorized, http.StatusTemporaryRedirect} {
		status = s
		_, err = p.Embed(context.Background(), []string{"source"})
		if err == nil || strings.Contains(err.Error(), "private") {
			t.Fatalf("unsafe error: %v", err)
		}
	}
}

func TestOpenAIRetriesABrokenConnection(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			conn, _, err := w.(http.Hijacker).Hijack()
			if err == nil {
				conn.Close()
			}
			return
		}
		_, _ = w.Write([]byte(`{"data":[{"index":0,"embedding":[1,0]}]}`))
	}))
	defer srv.Close()
	p, err := NewOpenAI(OpenAIConfig{APIKey: "test", BaseURL: srv.URL, Dimensions: 2})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = p.Embed(context.Background(), []string{"source"}); err != nil || calls != 2 {
		t.Fatalf("recovery calls %d: %v", calls, err)
	}
}

// A provider that asks for a longer wait than a job should sit through ends
// the request with its retryable error, so the job is retried later.
func TestOpenAIDoesNotSitThroughALongRetryAfter(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Retry-After", "3600")
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	defer srv.Close()
	p, err := NewOpenAI(OpenAIConfig{APIKey: "test", BaseURL: srv.URL, Dimensions: 2})
	if err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	_, err = p.Embed(context.Background(), []string{"source"})
	var provider *ProviderError
	if calls != 1 || !errors.As(err, &provider) || !provider.Retryable() || time.Since(start) > 10*time.Second {
		t.Fatalf("calls %d after %s: %v", calls, time.Since(start), err)
	}
}

func TestProviderDelayReadsEveryRateLimitHeader(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	for _, tc := range []struct {
		headers map[string]string
		want    time.Duration
	}{
		{map[string]string{"retry-after-ms": "1500", "Retry-After": "9"}, 1500 * time.Millisecond},
		{map[string]string{"Retry-After": "9"}, 9 * time.Second},
		{map[string]string{"x-ratelimit-reset-requests": "20ms", "x-ratelimit-reset-tokens": "6m0s"}, 6 * time.Minute},
		{map[string]string{"x-ratelimit-reset-tokens": "garbage"}, time.Second},
		{nil, time.Second},
	} {
		h := http.Header{}
		for k, v := range tc.headers {
			h.Set(k, v)
		}
		if got := providerDelay(h, time.Second, now); got != tc.want {
			t.Fatalf("%v: %s want %s", tc.headers, got, tc.want)
		}
	}
	for _, status := range []int{400, 413, 422} {
		if !Rejected(fmt.Errorf("wrapped: %w", &ProviderError{Status: status})) {
			t.Fatalf("%d is a refusal of the content", status)
		}
	}
	for _, e := range []error{&ProviderError{Status: 401}, &ProviderError{Status: 400, QuotaExhausted: true}, errors.New("400")} {
		if Rejected(e) {
			t.Fatalf("%v is not a refusal of the content", e)
		}
	}
}
