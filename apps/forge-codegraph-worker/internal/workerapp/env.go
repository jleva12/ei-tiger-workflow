package workerapp

import (
	"encoding/json"
	"errors"
	"reflect"

	"github.com/go-viper/mapstructure/v2"

	"ei-aitiger-codegraph/serviceconfig"
	"ei-aitiger-codegraph/worker/internal/languages"
)

// aliases keep the historical environment names of settings whose standard
// name (CODEGRAPH_ plus the key path) differs. Fields with an `env` tag
// declare their own.
var aliases = map[string][]string{
	"parser.max_source_bytes":  {"CODEGRAPH_MAX_SOURCE_BYTES"},
	"discovery.max_files":      {"CODEGRAPH_MAX_FILES"},
	"discovery.max_hash_bytes": {"CODEGRAPH_MAX_HASH_BYTES"},
}

// LoadConfig reads the shared and worker settings through serviceconfig.Load:
// defaults, then codegraph.yaml when present, then the environment (with the
// dotenv file selected by CODEGRAPH_ENV_FILE, or .env), then validates.
func LoadConfig() (Config, error) {
	c := DefaultConfig()
	if err := serviceconfig.Load(&c, serviceconfig.Options{Aliases: aliases, Hooks: []mapstructure.DecodeHookFunc{rootsHook, languageSettingsHook}}); err != nil {
		return Config{}, err
	}
	if err := c.Validate(); err != nil {
		return Config{}, err
	}
	return c, nil
}

// rootsHook decodes CODEGRAPH_ROOTS, a JSON object of root names to absolute
// directories.
func rootsHook(from, to reflect.Type, value any) (any, error) {
	if from.Kind() != reflect.String || to != reflect.TypeOf(map[string]string{}) {
		return value, nil
	}
	var roots map[string]string
	if err := json.Unmarshal([]byte(value.(string)), &roots); err != nil || roots == nil {
		return nil, errors.New("roots must be a JSON object of names to absolute directories")
	}
	return roots, nil
}

// languageSettingsHook decodes CODEGRAPH_LANGUAGES, a JSON object of language
// ids to adapter configuration objects.
func languageSettingsHook(from, to reflect.Type, value any) (any, error) {
	if from.Kind() != reflect.String || to != reflect.TypeOf(languages.Settings{}) {
		return value, nil
	}
	var settings languages.Settings
	raw := value.(string)
	if len(raw) > serviceconfig.MaxEnvBytes {
		return nil, errors.New("language configuration exceeds 64 KiB")
	}
	if err := json.Unmarshal([]byte(raw), &settings); err != nil || settings == nil {
		return nil, errors.New("languages must be a JSON object of configuration objects")
	}
	for _, entry := range settings {
		var object map[string]json.RawMessage
		if err := json.Unmarshal(entry, &object); err != nil || object == nil {
			return nil, errors.New("each language configuration must be an object")
		}
	}
	return settings, nil
}
