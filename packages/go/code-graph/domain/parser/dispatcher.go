package parser

import (
	"context"
	"errors"
	"fmt"

	"ei-aitiger-codegraph/pkg/ir"
)

// ErrDispatch identifies routing, configuration and session lifecycle failures.
// These stop an ingestion run rather than becoming a per-file parsing failure.
var ErrDispatch = errors.New("parser dispatch failed")

// Dispatcher is immutable and shared by workflows and discovery. It selects
// adapters by registered filename extension and verifies source-set metadata.
// Native parser sessions belong to Worker instances, never to this shared value.
type Dispatcher struct{ registry *Registry }

func NewDispatcher(registry *Registry) (*Dispatcher, error) {
	if registry == nil || registry.Digest() == "" {
		return nil, fmt.Errorf("%w: parser registry required", ErrDispatch)
	}
	return &Dispatcher{registry: registry}, nil
}

func (d *Dispatcher) LanguageForPath(name string) (string, bool) {
	return d.registry.LanguageForPath(name)
}
func (d *Dispatcher) Digest() string {
	if d == nil {
		return ""
	}
	return d.registry.Digest()
}

// Descriptor returns owned registration metadata for compatibility checks.
func (d *Dispatcher) Descriptor(language string) (Descriptor, bool) {
	if d == nil {
		return Descriptor{}, false
	}
	entry, ok := d.registry.Lookup(language)
	return entry.Descriptor, ok
}
func (d *Dispatcher) WorkerReservation(limits Limits) (uint64, error) {
	return d.registry.WorkerReservation(limits)
}
func (d *Dispatcher) ValidateProfile(profile Profile, limits Limits) error {
	entry, ok := d.registry.Lookup(profile.Language)
	if !ok {
		return fmt.Errorf("%w: no parser for language %q", ErrUnsupportedConfig, profile.Language)
	}
	return entry.Validate(profile, limits)
}

// NewWorker creates a lazy session owner for one processing goroutine. Callers
// must Close it after use. Workers are not safe for concurrent calls.
func (d *Dispatcher) NewWorker() *Worker { return &Worker{dispatcher: d} }

type Worker struct {
	dispatcher *Dispatcher
	session    Session
	language   string
	closed     bool
}

func (w *Worker) Parse(ctx context.Context, in Input) (ir.SourceFile, error) {
	if w.closed {
		return ir.SourceFile{}, fmt.Errorf("%w: parser worker is closed", ErrDispatch)
	}
	if err := ctx.Err(); err != nil {
		return ir.SourceFile{}, err
	}
	language, ok := w.dispatcher.LanguageForPath(in.Source.Path)
	if !ok {
		return ir.SourceFile{}, fmt.Errorf("%w: %w: unregistered file extension", ErrDispatch, ErrUnsupportedConfig)
	}
	if language != in.Source.Language {
		return ir.SourceFile{}, fmt.Errorf("%w: %w: file extension and source language disagree", ErrDispatch, ErrUnsupportedConfig)
	}
	profile := Profile{Language: language, Version: in.Source.LanguageVersion, Options: in.Options}
	if err := w.dispatcher.ValidateProfile(profile, in.Limits); err != nil {
		return ir.SourceFile{}, fmt.Errorf("%w: %w", ErrDispatch, err)
	}
	if w.session == nil || w.language != language {
		// Close before opening the next language to honor the one-session memory budget.
		if w.session != nil {
			session := w.session
			w.session = nil
			if err := session.Close(context.Background()); err != nil {
				return ir.SourceFile{}, fmt.Errorf("%w: close parser: %w", ErrDispatch, err)
			}
		}
		entry, _ := w.dispatcher.registry.Lookup(language)
		session, err := entry.New()
		if err != nil {
			if session != nil {
				err = errors.Join(err, session.Close(context.Background()))
			}
			return ir.SourceFile{}, fmt.Errorf("%w: create parser %s: %w", ErrDispatch, language, err)
		}
		if session == nil {
			return ir.SourceFile{}, fmt.Errorf("%w: parser %s returned a nil session", ErrDispatch, language)
		}
		w.session, w.language = session, language
	}
	return w.session.Parse(ctx, in)
}

func (w *Worker) Close(ctx context.Context) error {
	if w.closed {
		return nil
	}
	w.closed = true
	if w.session == nil {
		return nil
	}
	session := w.session
	w.session = nil
	return session.Close(ctx)
}
