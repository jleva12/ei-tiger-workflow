package serviceconfig

import (
	"strings"
	"testing"
)

func TestGoogleCredentialSecret(t *testing.T) {
	t.Setenv("FORGE_GOOGLE_CREDENTIALS_JSON", "")
	opts, err := GoogleClientOptions()
	if err != nil || len(opts) != 0 {
		t.Fatal("ADC should remain the default")
	}
	for _, raw := range []string{`{private-secret`, `{"type":"authorized_user","refresh_token":"private-secret"}`, `{"type":"service_account","client_email":"user","private_key":"private-secret","token_uri":"https://attacker.invalid"}`} {
		t.Setenv("FORGE_GOOGLE_CREDENTIALS_JSON", raw)
		if _, err = GoogleClientOptions(); err == nil || strings.Contains(err.Error(), "private-secret") {
			t.Fatal("invalid credential not safely rejected")
		}
	}
	t.Setenv("FORGE_GOOGLE_CREDENTIALS_JSON", `{"type":"service_account","client_email":"worker@example.iam.gserviceaccount.com","private_key":"key","token_uri":"https://oauth2.googleapis.com/token"}`)
	opts, err = GoogleClientOptions()
	if err != nil || len(opts) != 1 {
		t.Fatal("service account option missing")
	}
}
