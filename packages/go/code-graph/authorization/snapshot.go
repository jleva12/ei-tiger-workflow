package authorization

import "time"

// BuildSnapshot validates and builds an immutable configuration snapshot. SQL
// storage additionally validates all persisted tenants in one read transaction.
// This constructor also supports deterministic tests and future read adapters.
func BuildSnapshot(c Configuration) (*Snapshot, error) {
	if c.Revision == 0 {
		return nil, ErrInvalid
	}
	if err := validateConfiguration(c, false); err != nil {
		return nil, err
	}
	if err := validateMemberships(c.TeamMembers); err != nil {
		return nil, err
	}
	e, err := enforcer(nil)
	if err != nil {
		return nil, err
	}
	for _, rule := range c.Rules {
		if rule.Type == "p" {
			_, err = e.AddPolicy(rule.Values)
		} else {
			_, err = e.AddGroupingPolicy(rule.Values)
		}
		if err != nil {
			return nil, err
		}
	}
	return &Snapshot{enforcer: e, members: membershipIndex(c.TeamMembers), Revision: c.Revision, ModelVersion: ModelVersion, ConfirmedAt: time.Now()}, nil
}
