// Package buildcontext defines immutable, versioned build inventories.
// It describes inputs and visibility; it does not resolve symbols or run builds.
package buildcontext

const SchemaVersion = "1.1.0"

type (
	ID          string
	InputID     string
	JDKID       string
	ModuleID    string
	SourceSetID string
	ArtifactID  string
	GapID       string
)

// BuildContext is sealed with a content-derived ID. Treat returned values as
// immutable. ID covers identity, inventory, observations and producer version;
// it never includes machine-specific absolute paths or timestamps.
type BuildContext struct {
	SchemaVersion string       `json:"schema_version"`
	ID            ID           `json:"id"`
	RepositoryID  string       `json:"repository_id"`
	SnapshotID    string       `json:"snapshot_id"`
	Producer      Producer     `json:"producer"`
	Inventory     Inventory    `json:"inventory"`
	Checks        []InputCheck `json:"checks"`
	Status        Status       `json:"status"`
	Diagnostics   []Diagnostic `json:"diagnostics,omitempty"`
}

type Producer struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	// InputSHA256 fingerprints normalized provider configuration, not its
	// whitespace or JSON object-key order. Dependency arrays retain their order.
	InputSHA256 string `json:"input_sha256"`
}

type Inventory struct {
	Inputs        []Input        `json:"inputs"`
	JDKs          []JDK          `json:"jdks"`
	Modules       []Module       `json:"modules"`
	SourceSets    []SourceSet    `json:"source_sets"`
	Artifacts     []Artifact     `json:"artifacts,omitempty"`
	MissingInputs []MissingInput `json:"missing_inputs,omitempty"`
}

// Location uses a logical root. "checkout" identifies the requested checkout;
// all other root names require explicit service-owned mount configuration.
// Path is normalized, relative and slash-separated; "." denotes the root.
type Location struct {
	Root string `json:"root"`
	Path string `json:"path"`
}

type InputKind string

const (
	InputSourceRoot    InputKind = "source_root"
	InputGeneratedRoot InputKind = "generated_source_root"
	InputJAR           InputKind = "jar"
	InputClasses       InputKind = "class_directory"
	InputJDK           InputKind = "jdk"
)

// Input identifies expected bytes, even when unavailable. Exactly one of
// Location and UnavailableReason is set. JARs use file SHA-256; generated,
// class and JDK directories use the documented ei-tree-sha256-v1 digest.
// Checkout source roots are pinned by SnapshotID and checked for existence;
// hashing each source file remains the parser/discovery boundary's job.
type Input struct {
	ID                InputID   `json:"id"`
	Kind              InputKind `json:"kind"`
	Location          *Location `json:"location,omitempty"`
	SHA256            string    `json:"sha256,omitempty"`
	UnavailableReason string    `json:"unavailable_reason,omitempty"`
}

type JDK struct {
	ID          JDKID   `json:"id"`
	HomeInputID InputID `json:"home_input_id"`
	Vendor      string  `json:"vendor"`
	Version     string  `json:"version"`
	Major       int     `json:"major"`
}

// Module identifies a build module. JavaModuleName is the declared JPMS name
// when known; an empty name is not an inferred automatic-module name.
type Module struct {
	ID             ModuleID     `json:"id"`
	Name           string       `json:"name"`
	Directory      string       `json:"directory"`
	JavaModuleName string       `json:"java_module_name,omitempty"`
	Coordinates    *Coordinates `json:"coordinates,omitempty"`
}

type SourceSetKind string

const (
	SourceSetMain   SourceSetKind = "main"
	SourceSetTest   SourceSetKind = "test"
	SourceSetCustom SourceSetKind = "custom"
)

// SourceSet is a compilation environment. No dependency or main/test visibility
// is inferred. Both paths are fully expanded ordered inputs. A source_set entry
// exposes that set's output; it does not inherit that set's dependency arrays.
type SourceSet struct {
	// Empty Language is the legacy Java profile: version comes from TargetRelease.
	// Other languages require an explicit LanguageVersion and no Java settings.
	Language         string            `json:"language,omitempty"`
	LanguageVersion  string            `json:"language_version,omitempty"`
	LanguageOptions  map[string]string `json:"language_options,omitempty"`
	ID               SourceSetID       `json:"id"`
	ModuleID         ModuleID          `json:"module_id"`
	Name             string            `json:"name"`
	Kind             SourceSetKind     `json:"kind"`
	JDKID            JDKID             `json:"jdk_id,omitempty"`
	TargetRelease    int               `json:"target_release,omitempty"`
	EnablePreview    bool              `json:"enable_preview,omitempty"`
	IncludePatterns  []string          `json:"include_patterns,omitempty"`
	ExcludePatterns  []string          `json:"exclude_patterns,omitempty"`
	SourceRootIDs    []InputID         `json:"source_root_ids,omitempty"`
	GeneratedRootIDs []InputID         `json:"generated_root_ids,omitempty"`
	OutputInputID    InputID           `json:"output_input_id,omitempty"`
	Classpath        []PathEntry       `json:"classpath,omitempty"`
	ModulePath       []PathEntry       `json:"module_path,omitempty"`
}

type EntryKind string

const (
	EntryArtifact  EntryKind = "artifact"
	EntrySourceSet EntryKind = "source_set"
	EntryMissing   EntryKind = "missing"
)

// RefID is an ArtifactID, SourceSetID or GapID selected by Kind. Missing entries
// retain their exact position instead of silently compacting a dependency path.
type PathEntry struct {
	Kind  EntryKind `json:"kind"`
	RefID string    `json:"ref_id"`
}

type Coordinates struct {
	Group      string `json:"group"`
	Name       string `json:"name"`
	Version    string `json:"version"`
	Classifier string `json:"classifier,omitempty"`
	Extension  string `json:"extension"`
}

type Artifact struct {
	ID             ArtifactID     `json:"id"`
	Coordinates    Coordinates    `json:"coordinates"`
	BinaryInputID  InputID        `json:"binary_input_id"`
	SourcesInputID InputID        `json:"sources_input_id,omitempty"`
	Origin         *SourceMapping `json:"origin,omitempty"`
}

// SourceMapping pins the source repository behind a binary/catalog artifact.
// It neither downloads that repository nor joins all same-named declarations.
type SourceMapping struct {
	RepositoryID string `json:"repository_id"`
	SnapshotID   string `json:"snapshot_id"`
	ModuleID     string `json:"module_id,omitempty"`
	SourceSetID  string `json:"source_set_id,omitempty"`
}

// MissingInput records incomplete build knowledge (for example an unresolved
// dependency version). A known artifact with absent bytes stays in Artifacts
// and Inputs instead. Requested retains the unresolved coordinate/expression.
type MissingInput struct {
	ID          GapID       `json:"id"`
	Requested   string      `json:"requested"`
	Reason      string      `json:"reason"`
	ModuleID    ModuleID    `json:"module_id,omitempty"`
	SourceSetID SourceSetID `json:"source_set_id,omitempty"`
}

// GapCompiledOutput is the Requested value of a gap that says a source set's
// compiled output is missing, most often because the build could not compile
// it. The set is still analysed from source; lookups other sets make into it
// stay unresolved. Ingestion reports the reason as a warning, not a failure.
const GapCompiledOutput = "compiled_output"

// GapBuildFailed is the Requested value of a gap that says a language's
// build could not be read at all, so its sources were analysed without one
// (no dependencies, conventional layout). Ingestion reports the reason as a
// warning, not a failure.
const GapBuildFailed = "build"

// GapConfiguration is the Requested value of a gap that says a project's
// configuration could not be followed as written (a file that does not
// parse, a language level outside what the analysis supports) and names
// what was used instead. Ingestion reports the reason as a warning.
const GapConfiguration = "configuration"

type Status string

const (
	Complete   Status = "complete"
	Incomplete Status = "incomplete"
)

type CheckStatus string

const (
	Available      CheckStatus = "available"
	Missing        CheckStatus = "missing"
	DigestMismatch CheckStatus = "digest_mismatch"
	Unsupported    CheckStatus = "unsupported"
)

type InputCheck struct {
	InputID        InputID     `json:"input_id"`
	Status         CheckStatus `json:"status"`
	ObservedSHA256 string      `json:"observed_sha256,omitempty"`
}

type Diagnostic struct {
	Code    string  `json:"code"`
	InputID InputID `json:"input_id,omitempty"`
	GapID   GapID   `json:"gap_id,omitempty"`
	Message string  `json:"message"`
}
