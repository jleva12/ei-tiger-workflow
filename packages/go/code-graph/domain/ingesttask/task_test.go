package ingesttask

import (
	"encoding/json"
	"strings"
	"testing"
)

func validPayload() Payload {
	return Payload{RepositoryURL: "https://github.com/example/app.git", CommitSHA: strings.Repeat("a", 40), RunID: "ingest-123"}
}

func TestPayloadRoundTrip(t *testing.T) {
	p := validPayload()
	data, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	got, err := Decode(data)
	if err != nil || got != p {
		t.Fatal(got, err)
	}
}

func TestInvalidPayloads(t *testing.T) {
	for _, change := range []func(*Payload){
		func(p *Payload) { p.CommitSHA = "main" }, func(p *Payload) { p.CommitSHA = "" }, func(p *Payload) { p.RunID = "" }, func(p *Payload) { p.RunID = "../escape" },
		func(p *Payload) { p.RepositoryURL = "https://secret@github.com/example/app" }, func(p *Payload) { p.RepositoryURL = "https://example.com/a/b" },
		func(p *Payload) { p.RepositoryURL = "https://github.com/a/b/tree/main" }, func(p *Payload) { p.RepositoryURL = "https://github.com/a/b?token=secret" },
	} {
		p := validPayload()
		change(&p)
		if err := p.Validate(); err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatal("invalid/unsafe payload", err)
		}
	}
	data, _ := json.Marshal(validPayload())
	for _, bad := range [][]byte{
		nil, []byte("null"), []byte("[]"), []byte("{}"), []byte(strings.Repeat(" ", MaxPayloadBytes+1)), []byte{0xff},
		append(append([]byte{}, data...), []byte(" {}")...),
		[]byte(strings.TrimSuffix(string(data), "}") + `,"run_id":"duplicate"}`),
		[]byte(strings.TrimSuffix(string(data), "}") + `,"token":"secret"}`),
		[]byte(strings.Replace(string(data), `"run_id":"ingest-123"`, `"Run_ID":"ingest-123"`, 1)),
	} {
		if _, err := Decode(bad); err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatal("invalid/unsafe JSON accepted", err)
		}
	}
}
