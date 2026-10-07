// Package ingesttask defines the wire contract for asynchronous repository ingestion.
package ingesttask

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"regexp"
	"strings"
	"unicode/utf8"
)

const Type = "codegraph:ingest:v1"
const Queue = "ingestion"
const MaxPayloadBytes = 8 << 10

// Payload contains only job identity; credentials, filesystem paths and resource
// limits are worker-owned. CommitSHA must pin the same bytes across retries.
type Payload struct {
	RepositoryURL string `json:"repository_url"`
	CommitSHA     string `json:"commit_sha"`
	RunID         string `json:"run_id"`
}

var runIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
var commitPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)
var ownerPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`)
var repoPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)

func (p Payload) Validate() error {
	if !runIDPattern.MatchString(p.RunID) {
		return errors.New("ingestion task: run_id must be 1-128 letters, digits, dots, underscores, colons or hyphens")
	}
	if !commitPattern.MatchString(p.CommitSHA) {
		return errors.New("ingestion task: commit_sha must be a full lowercase 40-character Git commit SHA")
	}
	u, err := url.Parse(p.RepositoryURL)
	if err != nil || len(p.RepositoryURL) > 2048 || u.Scheme != "https" || u.Host != "github.com" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.RawPath != "" || u.ForceQuery {
		return errors.New("ingestion task: repository_url must be an HTTPS GitHub repository URL without credentials, query or fragment")
	}
	parts := strings.Split(strings.TrimSuffix(strings.TrimPrefix(u.Path, "/"), "/"), "/")
	if len(parts) != 2 || !ownerPattern.MatchString(parts[0]) {
		return errors.New("ingestion task: repository_url must identify an owner and repository")
	}
	repo := strings.TrimSuffix(parts[1], ".git")
	if !repoPattern.MatchString(repo) || repo == "." || repo == ".." {
		return errors.New("ingestion task: invalid repository name")
	}
	return nil
}

// Decode rejects unknown/duplicate fields and trailing data before any Git or
// storage work. Diagnostics deliberately do not echo arbitrary task payloads.
func Decode(data []byte) (Payload, error) {
	var p Payload
	if len(data) > MaxPayloadBytes || !utf8.Valid(data) {
		return p, errors.New("ingestion task: payload must be UTF-8 JSON no larger than 8 KiB")
	}
	d := json.NewDecoder(bytes.NewReader(data))
	token, err := d.Token()
	if err != nil || token != json.Delim('{') {
		return p, errors.New("ingestion task: JSON object required")
	}
	seen := map[string]bool{}
	for d.More() {
		key, err := d.Token()
		if err != nil {
			return Payload{}, errors.New("ingestion task: invalid JSON")
		}
		name, ok := key.(string)
		if !ok || seen[name] {
			return Payload{}, errors.New("ingestion task: duplicate or invalid field")
		}
		seen[name] = true
		var value string
		if err := d.Decode(&value); err != nil {
			return Payload{}, errors.New("ingestion task: field values must be strings")
		}
		switch name {
		case "repository_url":
			p.RepositoryURL = value
		case "commit_sha":
			p.CommitSHA = value
		case "run_id":
			p.RunID = value
		default:
			return Payload{}, errors.New("ingestion task: unknown field")
		}
	}
	if _, err := d.Token(); err != nil {
		return Payload{}, errors.New("ingestion task: invalid JSON")
	}
	if _, err := d.Token(); err != io.EOF {
		return Payload{}, errors.New("ingestion task: trailing JSON")
	}
	if err := p.Validate(); err != nil {
		return Payload{}, fmt.Errorf("invalid payload: %w", err)
	}
	return p, nil
}
