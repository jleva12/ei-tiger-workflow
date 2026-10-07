package deployment

import "fmt"

type Phase string

const (
	Accepted   Phase = "ACCEPTED"
	Running    Phase = "RUNNING"
	Succeeded  Phase = "SUCCEEDED"
	Failed     Phase = "FAILED"
	Superseded Phase = "SUPERSEDED"
)

func (p Phase) Validate() error {
	switch p {
	case Accepted, Running, Succeeded, Failed, Superseded:
		return nil
	default:
		return fmt.Errorf("%w: unknown phase %q", ErrInvalidRequest, string(p))
	}
}

func (p Phase) Terminal() bool { return p == Succeeded || p == Superseded }

// Terminal also rejects legacy successful runs whose required index never
// completed. They must be resumed, not acknowledged as successful jobs.
func (r Run) Terminal() bool {
	return r.Phase == Superseded || (r.Phase == Succeeded && (r.Index == nil || r.Index.Status == IndexComplete))
}

// ValidateTransition checks the phase graph only. A failed run may run again
// (a retry); a superseded run never runs. Succeeded is final: a later commit
// is a new run, never a re-run.
func ValidateTransition(from, to Phase) error {
	if err := from.Validate(); err != nil {
		return err
	}
	if err := to.Validate(); err != nil {
		return err
	}
	if from == to {
		return nil
	}
	allowed := false
	switch from {
	case Accepted:
		allowed = to == Running || to == Failed || to == Superseded
	case Running:
		allowed = to == Succeeded || to == Failed || to == Superseded
	case Failed:
		allowed = to == Running || to == Superseded
	}
	if !allowed {
		return fmt.Errorf("%w: %s -> %s", ErrInvalidTransition, from, to)
	}
	return nil
}
