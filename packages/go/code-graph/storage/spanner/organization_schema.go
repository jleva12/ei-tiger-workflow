package spannerstore

import "strings"

// PendingOrganizationSchema returns only missing organization objects. It never
// alters or drops existing tables, indexes, graph data, or embedding dimensions.
func PendingOrganizationSchema(existing []string) []string {
	objects := map[string]bool{}
	for _, statement := range existing {
		if match := schemaObject.FindStringSubmatch(strings.TrimSpace(statement)); match != nil {
			objects[strings.ToLower(match[1])] = true
		}
	}
	content, err := schemaFS.ReadFile("schema/003_organization.sql")
	if err != nil {
		panic(err)
	}
	out := []string{}
	for _, statement := range strings.Split(string(content), ";") {
		statement = strings.TrimSpace(statement)
		if statement == "" {
			continue
		}
		match := schemaObject.FindStringSubmatch(statement)
		if match == nil {
			panic("invalid organization schema")
		}
		if !objects[strings.ToLower(match[1])] {
			out = append(out, statement)
		}
	}
	return out
}
