// Package ir defines the versioned, post-parse intermediate representation.
//
// It records syntax and source evidence. A name such as a.Service is preserved
// as written; parsing does not establish that a is a package or resolve Service
// to a declaration. Binding results and graph projections belong to later stages.
//
// One SourceFile is a serialization unit. Typed IDs link file-local tables;
// no repository-wide object graph, parser handles, or database objects are held.
// Treat a published SourceFile as immutable. Go structs and slices themselves
// do not enforce immutability, so producers must transfer ownership or copy.
package ir
