// Package syntax describes an explicitly configured extraction-only source
// environment. It does not discover or claim Maven/Gradle compilation settings.
package syntax

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path"
	"strconv"
	"strings"

	"ei-aitiger-codegraph/pkg/buildcontext"
)

const Version = "1.1.0"

type Provider struct {
	inventory buildcontext.Inventory
	producer  buildcontext.Producer
}

var _ buildcontext.Provider = (*Provider)(nil)

func New(release int) (*Provider, error) {
	switch release {
	case 8, 11, 17, 21:
	default:
		return nil, fmt.Errorf("%w: choose Java release 8, 11, 17 or 21", buildcontext.ErrInvalidInput)
	}
	return newProvider(javaInventory(release))
}

func javaInventory(release int) buildcontext.Inventory {
	in := buildcontext.Inventory{
		Inputs: []buildcontext.Input{{ID: "checkout-sources", Kind: buildcontext.InputSourceRoot, Location: &buildcontext.Location{Root: "checkout", Path: "."}},
			{ID: "unknown-jdk", Kind: buildcontext.InputJDK, UnavailableReason: "JDK identity and bytes were not discovered in syntax-only mode"}},
		JDKs:          []buildcontext.JDK{{ID: "syntax-profile", HomeInputID: "unknown-jdk", Vendor: "unknown", Version: "unknown", Major: release}},
		Modules:       []buildcontext.Module{{ID: "syntax", Name: "explicit syntax scan", Directory: "."}},
		SourceSets:    []buildcontext.SourceSet{{ID: "syntax", ModuleID: "syntax", Name: "all Java sources", Kind: buildcontext.SourceSetCustom, JDKID: "syntax-profile", TargetRelease: release, SourceRootIDs: []buildcontext.InputID{"checkout-sources"}}},
		MissingInputs: []buildcontext.MissingInput{{ID: "build-inventory", Requested: "actual modules, source sets, toolchain, dependencies and generated inputs", Reason: "syntax-only scan: module/source-set IDs group extraction inputs and do not describe compiler visibility"}},
	}
	return in
}

func newProvider(in buildcontext.Inventory) (*Provider, error) {
	owned, err := buildcontext.CanonicalInventory(in)
	if err != nil {
		return nil, err
	}
	return &Provider{inventory: owned, producer: buildcontext.Producer{Name: "explicit-syntax-profile", Version: Version}}, nil
}

// WithProducer names the provider that composed the profiles, for a
// language whose project discovery delegates to a syntax profile.
func (p *Provider) WithProducer(name, version string) *Provider {
	if p == nil {
		return nil
	}
	copied := *p
	copied.producer = buildcontext.Producer{Name: name, Version: version}
	return &copied
}

// Profile declares one language's syntax version/options and checkout-relative
// source roots. Use separate profiles for languages sharing the same roots.
// Supported versions/options are validated by the pipeline's parser registry.
type Profile struct {
	Language      string
	Version       string
	Roots         []string
	EnablePreview bool
	Settings      map[string]string
	// Excludes are relative source globs (`**/node_modules/**`) the
	// discovery walker skips inside the roots; Includes, when set, restrict
	// the walk to matching files (exclusions still take precedence).
	Excludes []string
	Includes []string
}

// NewProfiles creates a syntax-only provider without assuming a Java toolchain.
// One profile per language is allowed; custom providers/manifests can describe
// multiple source sets of the same language with different settings.
func NewProfiles(profiles ...Profile) (*Provider, error) {
	if len(profiles) == 0 {
		return nil, fmt.Errorf("%w: explicit language profiles required", buildcontext.ErrInvalidInput)
	}
	in := buildcontext.Inventory{
		Modules:       []buildcontext.Module{{ID: "syntax", Name: "explicit syntax scan", Directory: "."}},
		MissingInputs: []buildcontext.MissingInput{{ID: "build-inventory", Requested: "actual modules, source sets, toolchain, dependencies and generated inputs", Reason: "syntax-only scan: module/source-set IDs group extraction inputs and do not describe compiler visibility"}},
	}
	for _, profile := range profiles {
		if profile.Language == "" || profile.Version == "" || len(profile.Roots) == 0 {
			return nil, fmt.Errorf("%w: explicit language, version and roots required", buildcontext.ErrInvalidInput)
		}
		id := "syntax-" + profile.Language
		set := buildcontext.SourceSet{ID: buildcontext.SourceSetID(id), ModuleID: "syntax", Name: profile.Language + " sources", Kind: buildcontext.SourceSetCustom,
			Language: profile.Language, LanguageVersion: profile.Version, LanguageOptions: profile.Settings, EnablePreview: profile.EnablePreview, ExcludePatterns: append([]string(nil), profile.Excludes...), IncludePatterns: append([]string(nil), profile.Includes...)}
		for i, root := range profile.Roots {
			inputID := buildcontext.InputID(id + "-root-" + strconv.Itoa(i))
			in.Inputs = append(in.Inputs, buildcontext.Input{ID: inputID, Kind: buildcontext.InputSourceRoot, Location: &buildcontext.Location{Root: "checkout", Path: root}})
			set.SourceRootIDs = append(set.SourceRootIDs, inputID)
		}
		if profile.Language == "java" {
			release, err := strconv.Atoi(profile.Version)
			if err != nil {
				return nil, fmt.Errorf("%w: Java version must be an explicit release", buildcontext.ErrInvalidInput)
			}
			set.TargetRelease, set.JDKID = release, buildcontext.JDKID(id)
			inputID := buildcontext.InputID(id + "-jdk")
			in.Inputs = append(in.Inputs, buildcontext.Input{ID: inputID, Kind: buildcontext.InputJDK, UnavailableReason: "JDK identity and bytes were not discovered in syntax-only mode"})
			in.JDKs = append(in.JDKs, buildcontext.JDK{ID: set.JDKID, HomeInputID: inputID, Vendor: "unknown", Version: "unknown", Major: release})
		}
		in.SourceSets = append(in.SourceSets, set)
	}
	return newProvider(in)
}

// checkRoot inspects each component without traversing a symlink.
func checkRoot(root *os.Root, name string) (buildcontext.CheckStatus, error) {
	current := "."
	for _, part := range strings.Split(name, "/") {
		if part == ".git" {
			return buildcontext.Unsupported, nil
		}
		current = path.Join(current, part)
		info, err := root.Lstat(current)
		if errors.Is(err, os.ErrNotExist) {
			return buildcontext.Missing, nil
		}
		if err != nil {
			return "", err
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return buildcontext.Unsupported, nil
		}
	}
	return buildcontext.Available, nil
}

func (p *Provider) Build(ctx context.Context, r buildcontext.Request) (buildcontext.BuildContext, error) {
	if err := ctx.Err(); err != nil {
		return buildcontext.BuildContext{}, err
	}
	if err := r.Validate(); err != nil {
		return buildcontext.BuildContext{}, err
	}
	if p == nil || len(p.inventory.SourceSets) == 0 {
		return buildcontext.BuildContext{}, buildcontext.ErrInvalidInput
	}
	root, err := os.OpenRoot(r.Checkout.Path)
	if err != nil {
		return buildcontext.BuildContext{}, err
	}
	defer root.Close()
	in := p.inventory
	var checks []buildcontext.InputCheck
	for _, input := range in.Inputs {
		if err := ctx.Err(); err != nil {
			return buildcontext.BuildContext{}, err
		}
		status := buildcontext.Missing
		if input.Location != nil {
			status, err = checkRoot(root, input.Location.Path)
			if err != nil {
				return buildcontext.BuildContext{}, err
			}
		}
		checks = append(checks, buildcontext.InputCheck{InputID: input.ID, Status: status})
	}
	digest, err := in.Digest()
	if err != nil {
		return buildcontext.BuildContext{}, err
	}
	c, err := buildcontext.Seal(buildcontext.BuildContext{RepositoryID: r.Checkout.RepositoryID, SnapshotID: r.Checkout.SnapshotID,
		Producer: buildcontext.Producer{Name: p.producer.Name, Version: p.producer.Version, InputSHA256: digest}, Inventory: in,
		Checks: checks})
	if err != nil {
		return buildcontext.BuildContext{}, err
	}
	data, err := json.Marshal(c)
	if err != nil {
		return buildcontext.BuildContext{}, err
	}
	if c.RecordCount() > r.Limits.MaxRecords || uint64(len(c.Diagnostics)) > r.Limits.MaxDiagnostics || uint64(len(data)) > r.Limits.MaxOutputBytes {
		return buildcontext.BuildContext{}, buildcontext.ErrLimitExceeded
	}
	if err := ctx.Err(); err != nil {
		return buildcontext.BuildContext{}, err
	}
	return c, nil
}
