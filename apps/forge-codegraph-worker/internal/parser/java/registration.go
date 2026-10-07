package java

import (
	"fmt"
	"strconv"
	"strings"
	"unicode"

	"ei-aitiger-codegraph/pkg/parser"
)

// Registration exposes Java syntax capabilities without putting Java policy
// in the workflow, discovery or worker implementation.
func Registration() parser.Registration {
	return parser.Registration{
		Descriptor: parser.Descriptor{Language: "java", Extensions: []string{".java"}, Version: Version, GrammarVersion: GrammarVersion, FeatureSet: FeatureSet},
		New: func() (parser.Session, error) {
			engine, err := New()
			if err != nil {
				return nil, err
			}
			return engine, nil
		},
		Validate: validateProfile,
		WorkerMemory: func(limits parser.Limits) (uint64, error) {
			if err := validateLimits(limits); err != nil {
				return 0, err
			}
			// Conservative scheduling estimate, not a hard native allocation cap.
			if limits.MaxOutputBytes > (1<<63)/3 {
				return 0, fmt.Errorf("%w: output memory estimate overflow", parser.ErrUnsupportedConfig)
			}
			return 16*limits.MaxSourceBytes + 3*limits.MaxOutputBytes + (8 << 20), nil
		},
	}
}

func validateLimits(limits parser.Limits) error {
	if err := limits.Validate(); err != nil {
		return err
	}
	if limits.MaxSourceBytes > 16<<20 || limits.MaxSyntaxDepth > 16384 {
		return fmt.Errorf("%w: Java adapter supports source budgets up to 16 MiB and depth up to 256", parser.ErrUnsupportedConfig)
	}
	return nil
}

// validateProfile accepts any Java release from 8 on, with or without
// preview features: the grammar parses the language through Java 21, the
// builder reports syntax newer than the release as a release issue, and
// syntax the grammar does not know (newer or preview) as partial coverage.
// javac, not the parser, is the authority on what a release allows.
func validateProfile(profile parser.Profile, limits parser.Limits) error {
	if profile.Language != "java" {
		return fmt.Errorf("%w: Java profile required", parser.ErrUnsupportedConfig)
	}
	release, err := strconv.Atoi(profile.Version)
	if err != nil || release < 8 || release > 99 || strconv.Itoa(release) != profile.Version {
		return fmt.Errorf("%w: Java release must be 8 or later", parser.ErrUnsupportedConfig)
	}
	if err := validateBuildSettings(profile); err != nil {
		return err
	}
	return validateLimits(limits)
}

// Build discovery carries these Java-owned compilation settings with every
// source-set profile. They do not enable syntax or processors in this parser;
// retaining them in Options also retains them in Producer.ConfigDigest.
func validateBuildSettings(profile parser.Profile) error {
	for key, value := range profile.Options.Settings {
		valid := false
		switch key {
		case "java.compiler.mode":
			valid = value == "source-target" || value == "release"
		case "java.compiler.target":
			target, err := strconv.Atoi(value)
			source, _ := strconv.Atoi(profile.Version)
			valid = err == nil && target >= source && strconv.Itoa(target) == value && profile.Options.Settings["java.compiler.mode"] == "source-target"
		case "java.compiler.proc":
			valid = value == "only" || value == "none" || value == "full"
		case "java.maven.execution", "java.maven.phase":
			valid = value != "" && len(value) <= 4096 && strings.IndexFunc(value, unicode.IsControl) < 0
		}
		if !valid {
			return fmt.Errorf("%w: unknown or invalid Java compilation setting %q", parser.ErrUnsupportedConfig, key)
		}
	}
	return nil
}
