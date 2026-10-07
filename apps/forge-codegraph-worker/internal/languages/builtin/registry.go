// Package builtin is the single composition point for bundled language adapters.
package builtin

import (
	"ei-aitiger-codegraph/worker/internal/languages"
	"ei-aitiger-codegraph/worker/internal/languages/java"
	"ei-aitiger-codegraph/worker/internal/languages/python"
	"ei-aitiger-codegraph/worker/internal/languages/typescript"
)

// Registry bundles Java (javac), Python (Pyright), and TypeScript/JavaScript
// (syntax-tier binding).
func Registry() (*languages.Registry, error) {
	return languages.NewRegistry(java.Adapter(), typescript.Adapter(), python.Adapter())
}
