package graph

import (
	"errors"
	"fmt"
	"math"
	"strings"
	"unicode/utf8"
)

var (
	ErrInvalid   = errors.New("graph: invalid value")
	ErrIntegrity = errors.New("graph: integrity failure")
	ErrNotFound  = errors.New("graph: not found")
)

const (
	MaxTextBytes     = 64 << 10
	MaxPropertyCount = 64
	MaxRecordBytes   = 1 << 20
)

func text(field, value string, required bool, max int) error {
	if required && value == "" {
		return fmt.Errorf("%w: %s required", ErrInvalid, field)
	}
	if len(value) > max || !utf8.ValidString(value) || strings.IndexByte(value, 0) >= 0 {
		return fmt.Errorf("%w: %s too long or not valid UTF-8", ErrInvalid, field)
	}
	return nil
}

func (a SourceAnchor) Validate() error {
	if !ValidID(a.Lineage) {
		return fmt.Errorf("%w: anchor lineage", ErrInvalid)
	}
	if len(a.ContentSHA256) != 64 {
		return fmt.Errorf("%w: anchor content hash", ErrInvalid)
	}
	if a.Span.End.ByteOffset < a.Span.Start.ByteOffset || a.Span.Start.Line == 0 || a.Span.End.Line == 0 {
		return fmt.Errorf("%w: anchor span", ErrInvalid)
	}
	return nil
}

func validateProperties(p map[string]PropertyValue) error {
	if len(p) > MaxPropertyCount {
		return fmt.Errorf("%w: too many properties", ErrInvalid)
	}
	for k, v := range p {
		if err := text("property key", k, true, 128); err != nil {
			return err
		}
		n := 0
		if v.String != nil {
			n++
			if err := text("property "+k, *v.String, false, MaxTextBytes); err != nil {
				return err
			}
		}
		if v.Int64 != nil {
			n++
		}
		if v.Float64 != nil {
			n++
			if math.IsNaN(*v.Float64) || math.IsInf(*v.Float64, 0) {
				return fmt.Errorf("%w: property %s not finite", ErrInvalid, k)
			}
		}
		if v.Bool != nil {
			n++
		}
		if v.Strings != nil {
			n++
			for _, s := range *v.Strings {
				if err := text("property "+k, s, false, MaxTextBytes); err != nil {
					return err
				}
			}
		}
		if n != 1 {
			return fmt.Errorf("%w: property %s must hold exactly one value", ErrInvalid, k)
		}
	}
	return nil
}

func (n Node) Validate() error {
	if !ValidID(n.ID) {
		return fmt.Errorf("%w: node id %q", ErrInvalid, n.ID)
	}
	if err := text("node kind", n.Kind, true, 64); err != nil {
		return err
	}
	if err := text("node name", n.Name, false, MaxTextBytes); err != nil {
		return err
	}
	if err := text("node qualified name", n.QualifiedName, false, MaxTextBytes); err != nil {
		return err
	}
	if n.Source != nil {
		if err := n.Source.Validate(); err != nil {
			return err
		}
	}
	return validateProperties(n.Properties)
}

func (e Edge) Validate() error {
	if !ValidID(e.ID) || !ValidID(e.SourceID) || !ValidID(e.TargetID) {
		return fmt.Errorf("%w: edge ids", ErrInvalid)
	}
	if err := text("edge kind", e.Kind, true, 64); err != nil {
		return err
	}
	if e.Source != nil {
		if err := e.Source.Validate(); err != nil {
			return err
		}
	}
	return validateProperties(e.Properties)
}

func (f Fact) Validate() error {
	switch {
	case f.Node != nil && f.Edge == nil:
		return f.Node.Validate()
	case f.Edge != nil && f.Node == nil:
		return f.Edge.Validate()
	default:
		return fmt.Errorf("%w: fact must hold exactly one of node or edge", ErrInvalid)
	}
}

func (v Version) Validate() error {
	if err := v.Fact.Validate(); err != nil {
		return err
	}
	if v.GenFrom == 0 || (v.GenTo != 0 && v.GenTo <= v.GenFrom) {
		return fmt.Errorf("%w: version generations", ErrInvalid)
	}
	if v.Lineage != "" && !ValidID(v.Lineage) {
		return fmt.Errorf("%w: version lineage", ErrInvalid)
	}
	if len(v.CommitFrom) != 40 || (v.GenTo != 0 && len(v.CommitTo) != 40) || (v.GenTo == 0 && (v.CommitTo != "" || v.Retired)) {
		return fmt.Errorf("%w: version commits", ErrInvalid)
	}
	return nil
}

func (c Change) Validate() error {
	switch c.Op {
	case OpAdd, OpReopen:
		if c.After == nil || c.Before != nil {
			return fmt.Errorf("%w: %s needs after only", ErrInvalid, c.Op)
		}
	case OpUpdate:
		if c.After == nil || c.Before == nil {
			return fmt.Errorf("%w: update needs before and after", ErrInvalid)
		}
	case OpRetire:
		if c.After != nil || c.Before == nil {
			return fmt.Errorf("%w: retire needs before only", ErrInvalid)
		}
	default:
		return fmt.Errorf("%w: operation %q", ErrInvalid, c.Op)
	}
	if c.After != nil {
		if err := c.After.Validate(); err != nil {
			return err
		}
		if c.After.Key() != c.Key {
			return fmt.Errorf("%w: change key differs from fact", ErrInvalid)
		}
	}
	if c.Before != nil {
		if err := c.Before.Validate(); err != nil {
			return err
		}
		if c.Before.Fact.Key() != c.Key || c.Before.GenTo != 0 {
			return fmt.Errorf("%w: change before-image", ErrInvalid)
		}
	}
	if !ValidID(c.Key.ID) || (c.Key.Kind != RecordNode && c.Key.Kind != RecordEdge) {
		return fmt.Errorf("%w: change key", ErrInvalid)
	}
	return nil
}
