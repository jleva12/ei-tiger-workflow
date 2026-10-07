package codesearch

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type OpenAIConfig struct {
	APIKey     string
	BaseURL    string
	Model      string
	Dimensions int
	// MaxInputBytes bounds one embedded window (default 7500, at most 8000):
	// a token spans at least one byte, so 7500 bytes stay inside an 8,192
	// token model window whatever the text is. A model with a smaller window
	// needs a smaller value.
	MaxInputBytes int
	Client        *http.Client
	// Logger, when set, reports throttling and server errors that are retried.
	Logger *slog.Logger
}
type OpenAI struct{ config OpenAIConfig }

// ProviderError exposes a bounded status/category, never the response body
// (which can contain source or credentials). Both retry layers use this type.
type ProviderError struct {
	Status         int
	QuotaExhausted bool
}

func (e *ProviderError) Error() string {
	if e.QuotaExhausted {
		return "code search: embedding provider quota exhausted"
	}
	return fmt.Sprintf("code search: embedding provider HTTP %d", e.Status)
}

func (e *ProviderError) Retryable() bool {
	if e.QuotaExhausted {
		return false
	}
	switch e.Status {
	case 408, 409, 425, 429, 500, 502, 503, 504:
		return true
	default:
		return false
	}
}

// Rejected reports that the provider refused the request's content rather
// than the caller or the service: one input of the batch can be at fault.
func (e *ProviderError) Rejected() bool {
	return !e.QuotaExhausted && (e.Status == 400 || e.Status == 413 || e.Status == 422)
}

// Rejected reports whether err is a provider's refusal of a request's
// content (see ProviderError.Rejected).
func Rejected(err error) bool {
	var provider *ProviderError
	return errors.As(err, &provider) && provider.Rejected()
}

// transportError is a request that got no complete answer: the connection
// failed, timed out or broke while the response was read. It is retried,
// and a job that still ends on one is retried later.
type transportError struct{ err error }

func (e *transportError) Error() string {
	return "code search: embedding request failed: " + e.err.Error()
}
func (e *transportError) Unwrap() error   { return e.err }
func (e *transportError) Retryable() bool { return true }

// Retries: attempts back off exponentially from one second to a minute with
// jitter, or wait as long as the provider asks, up to maxProviderWait; a
// longer requested wait ends the request with its retryable error.
const (
	maxAttempts     = 8
	maxBackoff      = time.Minute
	maxProviderWait = 5 * time.Minute
)

func backoff(attempt int) time.Duration {
	d := min(time.Second<<min(attempt, 10), maxBackoff)
	return d - d/5 + time.Duration(rand.Int64N(int64(d/5)*2+1))
}

// providerDelay is how long the provider asked the client to wait, from the
// retry-after-ms, Retry-After or x-ratelimit-reset-* headers, never less
// than fallback.
func providerDelay(h http.Header, fallback time.Duration, now time.Time) time.Duration {
	if ms, err := strconv.ParseInt(h.Get("retry-after-ms"), 10, 64); err == nil && ms >= 0 {
		return max(fallback, time.Duration(ms)*time.Millisecond)
	}
	if v := h.Get("Retry-After"); v != "" {
		return retryDelay(v, fallback, now)
	}
	wait := fallback
	for _, name := range []string{"x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"} {
		if d, err := time.ParseDuration(h.Get(name)); err == nil && d > 0 {
			wait = max(wait, d)
		}
	}
	return wait
}

func providerError(status int, body []byte) *ProviderError {
	var result struct {
		Error struct {
			Code string `json:"code"`
			Type string `json:"type"`
		} `json:"error"`
	}
	_ = json.Unmarshal(body, &result)
	quota := func(code string) bool {
		return code == "insufficient_quota" || code == "billing_hard_limit_reached" || code == "billing_not_active"
	}
	return &ProviderError{Status: status, QuotaExhausted: quota(result.Error.Code) || quota(result.Error.Type)}
}

func retryDelay(header string, fallback time.Duration, now time.Time) time.Duration {
	if seconds, err := strconv.ParseInt(header, 10, 32); err == nil && seconds >= 0 {
		return max(fallback, time.Duration(seconds)*time.Second)
	}
	if when, err := http.ParseTime(header); err == nil {
		return max(fallback, when.Sub(now))
	}
	return fallback
}

func NewOpenAI(c OpenAIConfig) (*OpenAI, error) {
	if c.APIKey == "" {
		return nil, errors.New("code search: OPENAI_API_KEY is required")
	}
	if c.BaseURL == "" {
		c.BaseURL = "https://api.openai.com/v1"
	}
	u, err := url.Parse(c.BaseURL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Scheme != "https" && !(u.Scheme == "http" && (u.Hostname() == "127.0.0.1" || u.Hostname() == "localhost" || u.Hostname() == "::1"))) {
		return nil, errors.New("code search: invalid embedding endpoint")
	}
	if c.Model == "" {
		c.Model = "text-embedding-3-large"
	}
	if c.Dimensions == 0 {
		c.Dimensions = DefaultDimensions
	}
	if c.MaxInputBytes == 0 {
		c.MaxInputBytes = 7500
	}
	if c.Client == nil {
		c.Client = &http.Client{Timeout: 90 * time.Second}
	}
	// Never forward source documents or credentials to a redirect destination.
	client := *c.Client
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	c.Client = &client
	p := &OpenAI{config: c}
	return p, validateProvider(p)
}
func (p *OpenAI) Model() string      { return p.config.Model }
func (p *OpenAI) Dimensions() int    { return p.config.Dimensions }
func (p *OpenAI) MaxInputBytes() int { return p.config.MaxInputBytes }

func (p *OpenAI) Embed(ctx context.Context, input []string) ([][]float64, error) {
	if len(input) == 0 || len(input) > 16 {
		return nil, errors.New("code search: invalid embedding batch")
	}
	for _, text := range input {
		if len(text) == 0 || len(text) > p.MaxInputBytes() {
			return nil, errors.New("code search: embedding input exceeds window")
		}
	}
	body, err := json.Marshal(struct {
		Model      string   `json:"model"`
		Input      []string `json:"input"`
		Dimensions int      `json:"dimensions"`
		Encoding   string   `json:"encoding_format"`
	}{p.Model(), input, p.Dimensions(), "float"})
	if err != nil {
		return nil, err
	}
	var last error
	var wait time.Duration
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			if wait > maxProviderWait {
				break
			}
			if p.config.Logger != nil {
				p.config.Logger.Warn("embedding provider retry", "attempt", attempt+1, "backoff", wait, "inputs", len(input), "error", last)
			}
			timer := time.NewTimer(wait)
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, ctx.Err()
			case <-timer.C:
			}
		}
		out, retryAfter, err := p.embedOnce(ctx, body, len(input))
		if err == nil {
			return out, nil
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		var retryable interface{ Retryable() bool }
		if !errors.As(err, &retryable) || !retryable.Retryable() {
			return nil, err
		}
		last, wait = err, max(backoff(attempt), retryAfter)
	}
	return nil, fmt.Errorf("code search: embedding retries exhausted: %w", last)
}

// embedOnce sends one request. A failure that is worth retrying says so
// through Retryable, with the wait the provider asked for.
func (p *OpenAI) embedOnce(ctx context.Context, body []byte, inputs int) ([][]float64, time.Duration, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(p.config.BaseURL, "/")+"/embeddings", bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Authorization", "Bearer "+p.config.APIKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := p.config.Client.Do(req)
	if err != nil {
		return nil, 0, &transportError{err}
	}
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, (8<<20)+1))
	resp.Body.Close()
	if readErr != nil {
		return nil, 0, &transportError{readErr}
	}
	if len(raw) > 8<<20 {
		return nil, 0, errors.New("code search: embedding response too large")
	}
	if resp.StatusCode != http.StatusOK {
		failure := providerError(resp.StatusCode, raw)
		return nil, providerDelay(resp.Header, 0, time.Now()), failure
	}
	var result struct {
		Data []struct {
			Index     int       `json:"index"`
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
	}
	if err = json.Unmarshal(raw, &result); err != nil {
		// A body cut short on the way is a transport failure.
		return nil, 0, &transportError{fmt.Errorf("malformed embedding response: %w", err)}
	}
	if len(result.Data) != inputs {
		return nil, 0, errors.New("code search: embedding response count mismatch")
	}
	out := make([][]float64, inputs)
	for _, row := range result.Data {
		if row.Index < 0 || row.Index >= len(out) || out[row.Index] != nil {
			return nil, 0, errors.New("code search: invalid embedding response index")
		}
		if err = ValidateVector(row.Embedding, p.Dimensions()); err != nil {
			return nil, 0, err
		}
		out[row.Index] = row.Embedding
	}
	return out, 0, nil
}
