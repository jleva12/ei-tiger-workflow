package buildcontext

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"reflect"
	"sort"
)

func digest(v any) string {
	data, _ := json.Marshal(v)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// CanonicalInventory returns owned data, with unordered catalogs sorted by ID.
// It never sorts source roots, classpaths or module paths. Arrays there express
// effective compiler order, including duplicates and unresolved positions.
func CanonicalInventory(in Inventory) (Inventory, error) {
	if err := in.Validate(); err != nil {
		return Inventory{}, err
	}
	data, err := json.Marshal(in)
	if err != nil {
		return Inventory{}, err
	}
	var out Inventory
	if err := json.Unmarshal(data, &out); err != nil {
		return Inventory{}, err
	}
	sort.Slice(out.Inputs, func(i, j int) bool { return out.Inputs[i].ID < out.Inputs[j].ID })
	sort.Slice(out.JDKs, func(i, j int) bool { return out.JDKs[i].ID < out.JDKs[j].ID })
	sort.Slice(out.Modules, func(i, j int) bool { return out.Modules[i].ID < out.Modules[j].ID })
	sort.Slice(out.SourceSets, func(i, j int) bool { return out.SourceSets[i].ID < out.SourceSets[j].ID })
	sort.Slice(out.Artifacts, func(i, j int) bool { return out.Artifacts[i].ID < out.Artifacts[j].ID })
	sort.Slice(out.MissingInputs, func(i, j int) bool { return out.MissingInputs[i].ID < out.MissingInputs[j].ID })
	return out, nil
}

func (in Inventory) Digest() (string, error) {
	out, err := CanonicalInventory(in)
	if err != nil {
		return "", err
	}
	return digest(struct {
		Version   string
		Inventory Inventory
	}{SchemaVersion, out}), nil
}

func derived(in Inventory, checks []InputCheck) (Status, []Diagnostic, error) {
	byID := map[InputID]Input{}
	for _, x := range in.Inputs {
		byID[x.ID] = x
	}
	seen := map[InputID]bool{}
	status := Complete
	var diagnostics []Diagnostic
	for _, c := range checks {
		x, ok := byID[c.InputID]
		if !ok || seen[c.InputID] {
			return "", nil, fmt.Errorf("%w: duplicate/unknown input check %s", ErrInvalidInput, c.InputID)
		}
		seen[c.InputID] = true
		if c.ObservedSHA256 != "" && !validDigest(c.ObservedSHA256) {
			return "", nil, fmt.Errorf("%w: invalid observed digest", ErrInvalidInput)
		}
		if x.Location == nil && c.Status != Missing {
			return "", nil, fmt.Errorf("%w: unavailable input must be missing", ErrInvalidInput)
		}
		d := Diagnostic{InputID: c.InputID}
		switch c.Status {
		case Available:
			if c.ObservedSHA256 != x.SHA256 {
				return "", nil, fmt.Errorf("%w: available input digest must match expected digest", ErrInvalidInput)
			}
			continue
		case Missing:
			d.Code, d.Message = "missing_input", "Expected input is unavailable"
			if x.UnavailableReason != "" {
				d.Message = x.UnavailableReason
			}
		case DigestMismatch:
			if !validDigest(c.ObservedSHA256) || x.SHA256 == "" || c.ObservedSHA256 == x.SHA256 {
				return "", nil, fmt.Errorf("%w: mismatched input needs different expected/observed digests", ErrInvalidInput)
			}
			d.Code, d.Message = "digest_mismatch", "Input bytes do not match the declared SHA-256"
		case Unsupported:
			d.Code, d.Message = "unsupported_input", "Input has an unsupported filesystem type or symbolic link"
		default:
			return "", nil, fmt.Errorf("%w: unknown input check status", ErrInvalidInput)
		}
		if (c.Status == Missing || c.Status == Unsupported) && c.ObservedSHA256 != "" {
			return "", nil, fmt.Errorf("%w: unavailable input cannot have an observed digest", ErrInvalidInput)
		}
		status = Incomplete
		diagnostics = append(diagnostics, d)
	}
	if len(seen) != len(in.Inputs) {
		return "", nil, fmt.Errorf("%w: exactly one check per input required", ErrInvalidInput)
	}
	for _, g := range in.MissingInputs {
		status = Incomplete
		diagnostics = append(diagnostics, Diagnostic{Code: "declared_gap", GapID: g.ID, Message: g.Reason})
	}
	return status, diagnostics, nil
}

// Seal creates owned canonical data and fills schema, status, diagnostics and
// ID. These derived fields on the input value are ignored. The inventory and
// each input observation must already be valid; no missing inputs are guessed.
func Seal(c BuildContext) (BuildContext, error) {
	if !validID(c.RepositoryID) || !validID(c.SnapshotID) || !validText(c.Producer.Name) || !validText(c.Producer.Version) || !validDigest(c.Producer.InputSHA256) {
		return BuildContext{}, fmt.Errorf("%w: context identity/producer", ErrInvalidInput)
	}
	in, err := CanonicalInventory(c.Inventory)
	if err != nil {
		return BuildContext{}, err
	}
	c.Inventory = in
	c.Checks = append([]InputCheck(nil), c.Checks...)
	sort.Slice(c.Checks, func(i, j int) bool { return c.Checks[i].InputID < c.Checks[j].InputID })
	c.Status, c.Diagnostics, err = derived(in, c.Checks)
	if err != nil {
		return BuildContext{}, err
	}
	c.SchemaVersion = SchemaVersion
	c.ID = ""
	c.ID = ID("build:" + digest(c))
	return c, nil
}

func (c BuildContext) Validate() error {
	if c.SchemaVersion != SchemaVersion {
		return fmt.Errorf("%w: %q", ErrUnsupportedVersion, c.SchemaVersion)
	}
	want, err := Seal(c)
	if err != nil {
		return err
	}
	if c.ID != want.ID {
		return fmt.Errorf("%w: context ID does not match content", ErrInvalidInput)
	}
	if c.Status != want.Status || !reflect.DeepEqual(c.Diagnostics, want.Diagnostics) {
		return fmt.Errorf("%w: context status/diagnostics disagree with input checks", ErrInvalidInput)
	}
	return nil
}

func (c BuildContext) RecordCount() uint64 {
	return c.Inventory.RecordCount() + uint64(len(c.Checks)+len(c.Diagnostics))
}
