package authorization

import (
	"fmt"
	"io"
	"time"
)

// WriteMetrics appends Prometheus exposition without actor, tenant or group
// labels. Kind/action labels are always bounded by the compiled catalog.
func (s *Service) WriteMetrics(w io.Writer) {
	if s == nil {
		return
	}
	fmt.Fprintf(w, "\n# TYPE codegraph_authorization_decisions_total counter\ncodegraph_authorization_decisions_total %d\n", s.Decisions.Load())
	fmt.Fprintf(w, "codegraph_authorization_denials_total %d\ncodegraph_authorization_evaluation_errors_total %d\ncodegraph_authorization_reload_failures_total %d\n", s.Denials.Load(), s.EvaluationErrors.Load(), s.ReloadFailures.Load())
	fmt.Fprintf(w, "codegraph_authorization_membership_failures_total %d\ncodegraph_authorization_configuration_conflicts_total %d\n", s.MembershipFailures.Load(), s.ConfigurationConflicts.Load())
	fmt.Fprintf(w, "# TYPE codegraph_authorization_outbox_backlog gauge\ncodegraph_authorization_outbox_backlog %d\n", s.OutboxBacklog.Load())
	if snapshot := s.current.Load(); snapshot != nil {
		lag := uint64(0)
		if known := s.known.Load(); known > snapshot.Revision {
			lag = known - snapshot.Revision
		}
		fmt.Fprintf(w, "codegraph_authorization_applied_revision %d\ncodegraph_authorization_revision_lag %d\ncodegraph_authorization_snapshot_age_seconds %f\n", snapshot.Revision, lag, time.Since(snapshot.ConfirmedAt).Seconds())
	}
	fmt.Fprint(w, "# TYPE codegraph_authorization_evaluation_seconds histogram\n")
	s.decisionStats.Range(func(key, value any) bool {
		stat := value.(*decisionStat)
		fmt.Fprintf(w, "codegraph_authorization_evaluations_total{%s} %d\n", key, stat.count.Load())
		fmt.Fprintf(w, "codegraph_authorization_evaluation_seconds_sum{%s} %f\n", key, float64(stat.nanos.Load())/1e9)
		fmt.Fprintf(w, "codegraph_authorization_evaluation_seconds_count{%s} %d\n", key, stat.count.Load())
		for idx, bound := range latencyBounds {
			fmt.Fprintf(w, "codegraph_authorization_evaluation_seconds_bucket{%s,le=\"%g\"} %d\n", key, bound.Seconds(), stat.buckets[idx].Load())
		}
		fmt.Fprintf(w, "codegraph_authorization_evaluation_seconds_bucket{%s,le=\"+Inf\"} %d\n", key, stat.count.Load())
		return true
	})
}
