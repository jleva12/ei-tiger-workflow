package project

import "ei-aitiger-codegraph/worker/internal/jsonc"

// stripJSONC removes line and block comments and trailing commas from
// tsconfig-style JSON with comments, leaving string contents untouched.
func stripJSONC(data []byte) []byte { return jsonc.Strip(data) }
