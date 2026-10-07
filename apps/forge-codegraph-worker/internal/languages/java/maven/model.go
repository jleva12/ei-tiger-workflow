// Package maven owns Java-specific effective Maven build observation. It never
// treats raw POM declarations as a complete resolved compilation environment.
package maven

import (
	"context"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

const Version = "maven-observed-v3"

type properties struct {
	Entries []property `xml:",any"`
}
type property struct {
	XMLName xml.Name
	Value   string `xml:",chardata"`
}

func (p properties) get(name string) string {
	for _, entry := range p.Entries {
		if entry.XMLName.Local == name {
			return strings.TrimSpace(entry.Value)
		}
	}
	return ""
}

type coordinates struct {
	Group      string `xml:"groupId"`
	Name       string `xml:"artifactId"`
	Version    string `xml:"version"`
	Classifier string `xml:"classifier"`
	Type       string `xml:"type"`
	Scope      string `xml:"scope"`
}
type project struct {
	Group        string        `xml:"groupId"`
	Name         string        `xml:"artifactId"`
	Version      string        `xml:"version"`
	Packaging    string        `xml:"packaging"`
	Parent       coordinates   `xml:"parent"`
	Properties   properties    `xml:"properties"`
	Dependencies []coordinates `xml:"dependencies>dependency"`
	Build        build         `xml:"build"`
}
type build struct {
	Directory           string   `xml:"directory"`
	SourceDirectory     string   `xml:"sourceDirectory"`
	TestSourceDirectory string   `xml:"testSourceDirectory"`
	OutputDirectory     string   `xml:"outputDirectory"`
	TestOutputDirectory string   `xml:"testOutputDirectory"`
	Plugins             []plugin `xml:"plugins>plugin"`
}
type plugin struct {
	Group         string        `xml:"groupId"`
	Name          string        `xml:"artifactId"`
	Version       string        `xml:"version"`
	Configuration configuration `xml:"configuration"`
	Executions    []execution   `xml:"executions>execution"`
}
type execution struct {
	ID            string        `xml:"id"`
	Phase         string        `xml:"phase"`
	Goals         []string      `xml:"goals>goal"`
	Configuration configuration `xml:"configuration"`
}
type configuration struct {
	CombineSelf                   string   `xml:"combine.self,attr"`
	Release                       string   `xml:"release"`
	Source                        string   `xml:"source"`
	Target                        string   `xml:"target"`
	TestRelease                   string   `xml:"testRelease"`
	TestSource                    string   `xml:"testSource"`
	TestTarget                    string   `xml:"testTarget"`
	Proc                          string   `xml:"proc"`
	Includes                      []string `xml:"includes>include"`
	Excludes                      []string `xml:"excludes>exclude"`
	TestIncludes                  []string `xml:"testIncludes>testInclude"`
	TestExcludes                  []string `xml:"testExcludes>testExclude"`
	Sources                       []string `xml:"sources>source"`
	OutputDirectory               string   `xml:"outputDirectory"`
	GeneratedSourcesDirectory     string   `xml:"generatedSourcesDirectory"`
	GeneratedTestSourcesDirectory string   `xml:"generatedTestSourcesDirectory"`
	CompilerArgs                  []string `xml:"compilerArgs>compilerArg"`
}

// Model XML contains many independent effective projects, each separately
// bounded by MaxInputBytes. The enclosing artifact also has an explicit cap;
// a streaming decoder never retains Maven's large documentation/plugin catalogs.
func readProjects(ctx context.Context, name string, limits bc.Limits, maxModelBytes int64) ([]project, error) {
	f, err := os.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > maxModelBytes {
		return nil, bc.ErrLimitExceeded
	}
	reader := &modelReader{Reader: io.LimitReader(f, maxModelBytes+1), recordRemaining: -1}
	decoder := xml.NewDecoder(reader)
	var out []project
	for {
		if err = ctx.Err(); err != nil {
			return nil, err
		}
		token, err := decoder.Token()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%w: effective Maven XML", bc.ErrInvalidInput)
		}
		start, ok := token.(xml.StartElement)
		if !ok || start.Name.Local != "project" {
			continue
		}
		offset := decoder.InputOffset()
		reader.recordRemaining = int64(limits.MaxInputBytes) - (reader.read - offset)
		if reader.recordRemaining < 0 {
			reader.recordRemaining = 0
		}
		var p project
		if err = decoder.DecodeElement(&p, &start); err != nil {
			if reader.exceeded {
				return nil, bc.ErrLimitExceeded
			}
			return nil, fmt.Errorf("%w: effective Maven project", bc.ErrInvalidInput)
		}
		reader.recordRemaining = -1
		if decoder.InputOffset()-offset > int64(limits.MaxInputBytes) || uint64(len(out)) >= limits.MaxRecords {
			return nil, bc.ErrLimitExceeded
		}
		if p.Group == "" {
			p.Group = p.Parent.Group
		}
		if p.Version == "" {
			p.Version = p.Parent.Version
		}
		if p.Group == "" || p.Name == "" || p.Version == "" || strings.Contains(p.Group+p.Name+p.Version, "${") {
			return nil, fmt.Errorf("%w: unresolved effective reactor identity", bc.ErrInvalidInput)
		}
		if p.Group == aggregatorGroup && p.Name == aggregatorArtifact {
			continue // rootPOM's, not the checkout's
		}
		out = append(out, p)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("%w: empty effective Maven model", bc.ErrInvalidInput)
	}
	return out, nil
}

// xml.Decoder may buffer a small amount beyond the current token. Account for
// that buffer when starting each project, and enforce the bound while decoding
// so one hostile project cannot allocate the entire document before rejection.
type modelReader struct {
	io.Reader
	read            int64
	recordRemaining int64
	exceeded        bool
}

func (r *modelReader) Read(b []byte) (int, error) {
	if r.recordRemaining == 0 {
		r.exceeded = true
		return 0, bc.ErrLimitExceeded
	}
	if r.recordRemaining > 0 && int64(len(b)) > r.recordRemaining {
		b = b[:r.recordRemaining]
	}
	n, err := r.Reader.Read(b)
	r.read += int64(n)
	if r.recordRemaining > 0 {
		r.recordRemaining -= int64(n)
	}
	return n, err
}
