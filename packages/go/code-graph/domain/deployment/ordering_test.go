package deployment

import "testing"

func TestRequestSupersededBy(t *testing.T) {
	at := func(seq uint64, trigger TriggerKind) Request {
		return Request{DeploymentSequence: seq, TriggerKind: trigger}
	}
	cases := []struct {
		name  string
		run   Request
		live  Request
		stale bool
	}{
		{"older sequence", at(1, TriggerDeployment), at(2, TriggerDeployment), true},
		{"older refresh", at(1, TriggerAnalysisRefresh), at(2, TriggerDeployment), true},
		{"same sequence deployment", at(2, TriggerDeployment), at(2, TriggerDeployment), true},
		{"same sequence over a refresh", at(2, TriggerDeployment), at(2, TriggerAnalysisRefresh), true},
		{"refresh of the live sequence", at(2, TriggerAnalysisRefresh), at(2, TriggerDeployment), false},
		{"refresh over a refresh", at(2, TriggerAnalysisRefresh), at(2, TriggerAnalysisRefresh), false},
		{"newer sequence", at(3, TriggerDeployment), at(2, TriggerAnalysisRefresh), false},
	}
	for _, c := range cases {
		if got := c.run.SupersededBy(c.live); got != c.stale {
			t.Errorf("%s: SupersededBy = %v, want %v", c.name, got, c.stale)
		}
	}
}
