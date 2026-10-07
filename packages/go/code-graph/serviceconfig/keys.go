package serviceconfig

import (
	"fmt"
	"reflect"
	"strings"
	"unicode"
)

// key is one configuration setting: the dotted Viper key derived from the
// struct that holds it, the field's default value, and the environment
// names that set it.
type key struct {
	Path    string // e.g. "spanner.database"
	Default any    // the field's value before loading
	Kind    reflect.Kind
	Env     []string // the standard name first, then aliases from `env` tags
}

// keysOf walks a configuration struct and returns its leaf settings. Field
// names come from `mapstructure` tags, or from the field name in snake_case.
// Embedded structs tagged `mapstructure:",squash"` contribute their fields
// at the parent level; other nested structs contribute under their key.
// Maps, slices, durations and scalars are leaves.
func keysOf(cfg any, prefix string, aliases map[string][]string) ([]key, error) {
	v := reflect.ValueOf(cfg)
	if v.Kind() != reflect.Pointer || v.Elem().Kind() != reflect.Struct {
		return nil, fmt.Errorf("config: Load needs a pointer to a struct, got %T", cfg)
	}
	var out []key
	walk(v.Elem(), "", prefix, aliases, &out)
	seen := map[string]bool{}
	for _, k := range out {
		if seen[k.Path] {
			return nil, fmt.Errorf("config: setting %s is declared twice", k.Path)
		}
		seen[k.Path] = true
	}
	return out, nil
}

func walk(v reflect.Value, parent, prefix string, aliases map[string][]string, out *[]key) {
	t := v.Type()
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if !f.IsExported() {
			continue
		}
		name, squash := tagName(f)
		if name == "-" {
			continue
		}
		fv := v.Field(i)
		if fv.Kind() == reflect.Struct && !isLeafStruct(fv.Type()) {
			child := parent
			if !squash {
				child = join(parent, name)
			}
			walk(fv, child, prefix, aliases, out)
			continue
		}
		path := join(parent, name)
		names := []string{envName(prefix, path)}
		if alias := f.Tag.Get("env"); alias != "" {
			for _, a := range strings.Split(alias, ",") {
				if a = strings.TrimSpace(a); a != "" {
					names = append(names, a)
				}
			}
		}
		names = append(names, aliases[path]...)
		*out = append(*out, key{Path: path, Default: fv.Interface(), Kind: fv.Kind(), Env: names})
	}
}

// tagName reads the mapstructure tag: "name", ",squash" or "-".
func tagName(f reflect.StructField) (name string, squash bool) {
	tag := f.Tag.Get("mapstructure")
	parts := strings.Split(tag, ",")
	name = parts[0]
	for _, opt := range parts[1:] {
		if opt == "squash" {
			squash = true
		}
	}
	if name == "" {
		if f.Anonymous && squash {
			return "", true
		}
		name = snakeCase(f.Name)
	}
	return name, squash
}

// isLeafStruct reports struct types decoded as one value, not walked.
func isLeafStruct(t reflect.Type) bool {
	return t.PkgPath() == "time" || t.Implements(reflect.TypeFor[interface{ UnmarshalText([]byte) error }]())
}

func join(parent, name string) string {
	if parent == "" {
		return name
	}
	return parent + "." + name
}

// envName is the standard environment name of a key: the prefix, then the
// path upper-cased with dots as underscores. "spanner.database" becomes
// CODEGRAPH_SPANNER_DATABASE.
func envName(prefix, path string) string {
	return prefix + "_" + strings.ToUpper(strings.ReplaceAll(path, ".", "_"))
}

// snakeCase converts a Go identifier: MaxIRRecords is max_ir_records,
// BaseURL is base_url, HealthAddr is health_addr.
func snakeCase(s string) string {
	runes := []rune(s)
	var b strings.Builder
	for i, r := range runes {
		if i > 0 && unicode.IsUpper(r) {
			prevLower := unicode.IsLower(runes[i-1]) || unicode.IsDigit(runes[i-1])
			nextLower := i+1 < len(runes) && unicode.IsLower(runes[i+1])
			if prevLower || (nextLower && unicode.IsUpper(runes[i-1])) {
				b.WriteByte('_')
			}
		}
		b.WriteRune(unicode.ToLower(r))
	}
	return b.String()
}
