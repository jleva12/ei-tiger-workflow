// Package parser defines the public, engine-independent syntax extraction
// contract. Adapters consume original UTF-8 source bytes and emit pkg/ir facts.
// Parsing does not resolve symbols, discover dependencies, or write a graph.
package parser
