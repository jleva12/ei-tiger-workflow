package serviceconfig

import (
	"errors"
	"fmt"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"

	"github.com/go-viper/mapstructure/v2"
	"github.com/joho/godotenv"
	"github.com/spf13/pflag"
	"github.com/spf13/viper"
)

const (
	// EnvPrefix is the prefix of every standard environment name.
	EnvPrefix = "CODEGRAPH"
	// EnvFileVar names the dotenv file to load; EnvFile is the default.
	EnvFileVar = "CODEGRAPH_ENV_FILE"
	EnvFile    = ".env"
	// ConfigFileVar names an explicit configuration file (YAML, TOML or
	// JSON); without it, ConfigName is searched in ConfigPaths.
	ConfigFileVar = "CODEGRAPH_CONFIG_FILE"
	ConfigName    = "codegraph"
	// MaxEnvBytes bounds the env file and any single JSON-valued setting.
	MaxEnvBytes = 64 << 10
	// CommonFileVar names the repository's shared settings file, which the
	// env file's ${NAME} references resolve from; set but empty, there is
	// none. Unset, it is CommonFile, relative to the working directory
	// (apps/<name>), when that exists.
	CommonFileVar = "FORGE_ENV_COMMON_FILE"
	CommonFile    = "../../.env.common"
)

// reference is a ${NAME} reference in a dotenv value.
var reference = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

// assignment is a dotenv line setting a variable: its name and the value's
// opening quote, if any.
var assignment = regexp.MustCompile(`^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(['"]?)(.*)$`)

// ConfigPaths are searched, in order, for ConfigName.{yaml,yml,toml,json}.
var ConfigPaths = []string{".", "/etc/codegraph"}

// Options tune Load.
type Options struct {
	// Aliases adds environment names for keys, for settings whose historical
	// name differs from the standard one. `env:"NAME"` struct tags do the
	// same per field.
	Aliases map[string][]string
	// Hooks decode string values into non-scalar fields (JSON objects, for
	// example). Durations are always decoded.
	Hooks []mapstructure.DecodeHookFunc
	// Flags binds command-line flags to keys: flag name to key. A flag that
	// was set on the command line outranks the environment.
	Flags    *pflag.FlagSet
	FlagKeys map[string]string
}

// Load populates cfg, a pointer to a struct, through Viper from four layers
// of increasing precedence: the struct's current values (the defaults), a
// configuration file, the environment, and command-line flags.
//
// Keys are derived from the struct: `mapstructure` tags or snake_case field
// names, nested by dots, so a field Spanner.Database is the key
// spanner.database, the environment name CODEGRAPH_SPANNER_DATABASE and the
// YAML path spanner: database:. An `env` tag or an alias declares
// additional environment names.
//
// A dotenv file is read first and its values are placed in the process
// environment without overriding variables already set, the way godotenv
// does, so the process environment always wins. The file is CODEGRAPH_ENV_FILE
// when set (and must then exist) or .env when present. A CODEGRAPH_ key in
// the file that no setting claims is an error, so a misspelled name never
// passes silently; a configuration file with an unknown key fails the same
// way. A non-string setting whose environment value is empty is an error.
//
// Values several apps share live in the repository's .env.common, which the
// dotenv file names with ${NAME} references: NAME resolves to an earlier line
// of the file, otherwise to .env.common (see CommonFileVar). Nothing in
// .env.common reaches the environment unless the file references it, and a
// reference defined nowhere is an error unless the process environment sets
// the variable holding it. Single-quoted values are literal.
func Load(cfg any, opts Options) error {
	keys, err := keysOf(cfg, EnvPrefix, opts.Aliases)
	if err != nil {
		return err
	}
	known := map[string]bool{EnvFileVar: true, ConfigFileVar: true}
	for _, k := range keys {
		for _, name := range k.Env {
			known[name] = true
		}
	}
	if err := loadDotenv(known); err != nil {
		return err
	}

	v := viper.New()
	v.AllowEmptyEnv(true)
	for _, k := range keys {
		v.SetDefault(k.Path, k.Default)
		if err := v.BindEnv(append([]string{k.Path}, k.Env...)...); err != nil {
			return err
		}
	}
	if err := readConfigFile(v); err != nil {
		return err
	}
	for name, path := range opts.FlagKeys {
		if opts.Flags == nil || opts.Flags.Lookup(name) == nil {
			return fmt.Errorf("config: flag %s is not defined", name)
		}
		if err := v.BindPFlag(path, opts.Flags.Lookup(name)); err != nil {
			return err
		}
	}
	for _, k := range keys {
		if k.Kind == reflect.String {
			continue
		}
		for _, name := range k.Env {
			if value, ok := os.LookupEnv(name); ok && value == "" {
				return fmt.Errorf("invalid %s; an explicit value is required", name)
			}
		}
	}
	hooks := append([]mapstructure.DecodeHookFunc{mapstructure.StringToTimeDurationHookFunc(), mapstructure.TextUnmarshallerHookFunc()}, opts.Hooks...)
	err = v.UnmarshalExact(cfg, viper.DecodeHook(mapstructure.ComposeDecodeHookFunc(hooks...)), func(dc *mapstructure.DecoderConfig) {
		dc.MatchName = matchName
		dc.ErrorUnused = true
	})
	if err != nil {
		return fmt.Errorf("config: %w", describeDecodeError(err))
	}
	return nil
}

// loadDotenv places the dotenv file's variables in the environment without
// overriding what is already set, after rejecting unknown CODEGRAPH_ keys and
// resolving the file's references to the shared .env.common.
func loadDotenv(known map[string]bool) error {
	file, explicit := os.LookupEnv(EnvFileVar)
	if !explicit {
		file = EnvFile
	}
	if file == "" {
		return nil
	}
	env, err := readEnvFile(file, explicit)
	if err != nil || env == nil {
		return err
	}
	common, err := readCommonFile()
	if err != nil {
		return err
	}
	own, err := godotenv.Unmarshal(string(env))
	if err != nil {
		return errors.New("cannot read env file; check dotenv syntax and file permissions")
	}
	if err := checkReferences(file, string(env), common.names, common.missing, func(name string) bool {
		_, set := os.LookupEnv(name)
		return set
	}); err != nil {
		return err
	}
	// godotenv expands a reference from the lines above it, so the shared
	// file goes first and the env file's own lines override it.
	values, err := godotenv.Unmarshal(common.text + "\n" + string(env))
	if err != nil {
		return errors.New("cannot read env file; check dotenv syntax and file permissions")
	}
	names := make([]string, 0, len(own))
	for name := range own {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if strings.HasPrefix(name, EnvPrefix+"_") && !known[name] {
			return fmt.Errorf("unknown setting %s in env file", name)
		}
		if _, set := os.LookupEnv(name); !set {
			if err := os.Setenv(name, values[name]); err != nil {
				return err
			}
		}
	}
	return nil
}

// readEnvFile returns the contents of a dotenv file, or nil when an implicit
// one does not exist.
func readEnvFile(file string, explicit bool) ([]byte, error) {
	info, err := os.Stat(file)
	switch {
	case errors.Is(err, os.ErrNotExist) && !explicit:
		return nil, nil
	case err != nil:
		return nil, fmt.Errorf("inspect env file: %w", err)
	case !info.Mode().IsRegular() || info.Size() > MaxEnvBytes:
		return nil, errors.New("env file must be a regular file no larger than 64 KiB")
	}
	data, err := os.ReadFile(file)
	if err != nil {
		return nil, errors.New("cannot read env file; check dotenv syntax and file permissions")
	}
	return data, nil
}

// commonValues is the shared file as references see it.
type commonValues struct {
	text    string
	names   map[string]bool
	missing string // how an error says a name is defined nowhere
}

// readCommonFile reads the shared file CommonFileVar selects.
func readCommonFile() (commonValues, error) {
	file, explicit := os.LookupEnv(CommonFileVar)
	switch {
	case explicit && file == "":
		return commonValues{missing: "no earlier line defines (" + CommonFileVar + " is empty)"}, nil
	case !explicit:
		file = CommonFile
	}
	missing := "neither .env.common nor an earlier line defines (make env creates .env.common)"
	if explicit {
		missing = "neither " + file + " nor an earlier line defines"
	}
	info, err := os.Stat(file)
	switch {
	case errors.Is(err, os.ErrNotExist) && !explicit:
		return commonValues{missing: missing}, nil
	case errors.Is(err, os.ErrNotExist):
		return commonValues{}, fmt.Errorf("%s names %s, which does not exist", CommonFileVar, file)
	case err != nil:
		return commonValues{}, fmt.Errorf("inspect %s: %w", file, err)
	case !info.Mode().IsRegular() || info.Size() > MaxEnvBytes:
		return commonValues{}, fmt.Errorf("%s must be a regular file no larger than 64 KiB", file)
	}
	data, err := os.ReadFile(file)
	if err != nil {
		return commonValues{}, fmt.Errorf("cannot read %s; check its permissions", file)
	}
	values, err := godotenv.Unmarshal(string(data))
	if err != nil {
		return commonValues{}, fmt.Errorf("cannot read %s; check dotenv syntax", file)
	}
	if err := checkReferences(file, string(data), nil, "no earlier line defines", func(string) bool { return false }); err != nil {
		return commonValues{}, err
	}
	names := make(map[string]bool, len(values))
	for name := range values {
		names[name] = true
	}
	return commonValues{text: string(data), names: names, missing: missing}, nil
}

// checkReferences fails on a ${NAME} reference in an unquoted or
// double-quoted value of text that neither an earlier line nor shared
// defines, unless set reports that the process environment sets the
// variable holding it. godotenv would silently expand it to nothing. The
// error names the variable, the file and NAME, never a value.
func checkReferences(file, text string, shared map[string]bool, missing string, set func(string) bool) error {
	defined := map[string]bool{}
	for _, line := range strings.Split(text, "\n") {
		match := assignment.FindStringSubmatch(line)
		if match == nil || strings.HasPrefix(strings.TrimSpace(line), "#") {
			continue
		}
		name, quote, value := match[1], match[2], match[3]
		if quote != "'" && !set(name) {
			for _, ref := range reference.FindAllStringSubmatch(value, -1) {
				if !defined[ref[1]] && !shared[ref[1]] {
					return fmt.Errorf("%s in %s references ${%s}, which %s", name, file, ref[1], missing)
				}
			}
		}
		defined[name] = true
	}
	return nil
}

func readConfigFile(v *viper.Viper) error {
	if file, ok := os.LookupEnv(ConfigFileVar); ok && file != "" {
		v.SetConfigFile(file)
		if err := v.ReadInConfig(); err != nil {
			return fmt.Errorf("config: read %s: %w", file, err)
		}
		return nil
	}
	v.SetConfigName(ConfigName)
	for _, p := range ConfigPaths {
		v.AddConfigPath(p)
	}
	err := v.ReadInConfig()
	var notFound viper.ConfigFileNotFoundError
	if errors.As(err, &notFound) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("config: read %s: %w", v.ConfigFileUsed(), err)
	}
	return nil
}

// matchName lets snake_case keys reach CamelCase fields that carry no tag:
// max_source_bytes matches MaxSourceBytes.
func matchName(mapKey, fieldName string) bool {
	return normalize(mapKey) == normalize(fieldName)
}

func normalize(s string) string {
	return strings.ToLower(strings.NewReplacer("_", "", "-", "").Replace(s))
}

// describeDecodeError turns mapstructure's aggregate into one line per problem.
func describeDecodeError(err error) error {
	var joined interface{ Unwrap() []error }
	if errors.As(err, &joined) {
		var lines []string
		for _, e := range joined.Unwrap() {
			lines = append(lines, e.Error())
		}
		return errors.New(strings.Join(lines, "; "))
	}
	return err
}
