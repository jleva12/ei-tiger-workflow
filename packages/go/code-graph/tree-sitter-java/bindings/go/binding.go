package tree_sitter_java

// Generated parser SHA-256: 1041ce0d172b654fcf46f2942b085ef171e9f6e39743d90f0162dda7181eff14

// #cgo CFLAGS: -std=c11 -fPIC
// #include "../../src/parser.c"
import "C"

import "unsafe"

// Get the tree-sitter Language for this grammar.
func Language() unsafe.Pointer {
	return unsafe.Pointer(C.tree_sitter_java())
}
