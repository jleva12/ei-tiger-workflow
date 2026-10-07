package serviceconfig

import (
	"encoding/json"
	"errors"
	"os"

	"google.golang.org/api/option"
)

// GoogleClientOptions supports a service-account secret on hosts without secret
// file mounts (Railway). With no secret, the SDK uses normal ADC. Never include
// credential bytes or parser errors in returned errors.
func GoogleClientOptions() ([]option.ClientOption, error) {
	raw := os.Getenv("FORGE_GOOGLE_CREDENTIALS_JSON")
	if raw == "" {
		return nil, nil
	}
	var account struct {
		Type     string `json:"type"`
		Email    string `json:"client_email"`
		Key      string `json:"private_key"`
		TokenURI string `json:"token_uri"`
	}
	if json.Unmarshal([]byte(raw), &account) != nil || account.Type != "service_account" || account.Email == "" || account.Key == "" || account.TokenURI != "https://oauth2.googleapis.com/token" {
		return nil, errors.New("FORGE_GOOGLE_CREDENTIALS_JSON must contain valid Google service-account credentials")
	}
	return []option.ClientOption{option.WithCredentialsJSON([]byte(raw))}, nil
}
