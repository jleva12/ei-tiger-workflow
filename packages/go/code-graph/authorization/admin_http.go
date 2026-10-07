package authorization

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
)

// AdminHandler is mounted under /v1/admin/authorization (the existing API
// prefix). Its own guards supplement the outer authentication middleware.
func AdminHandler(service *Service, store *Store, provider Provider) http.Handler {
	mux := http.NewServeMux()
	read := func(handler http.HandlerFunc) http.HandlerFunc {
		return service.RequirePermission("authorization_config", "read")(handler)
	}
	state := read(func(w http.ResponseWriter, r *http.Request) {
		i, err := CurrentIdentity(r.Context())
		if err != nil {
			WriteError(w, r, err)
			return
		}
		out, err := store.Configuration(r.Context(), i.TenantID)
		if err != nil {
			WriteError(w, r, err)
			return
		}
		JSON(w, out)
	})
	mux.HandleFunc("GET /v1/admin/authorization", state)
	mux.HandleFunc("GET /v1/admin/authorization/roles", state)
	mux.HandleFunc("GET /v1/admin/authorization/permission-groups", state)
	mux.HandleFunc("GET /v1/admin/authorization/group-mappings", state)
	mux.HandleFunc("GET /v1/admin/authorization/catalog", read(func(w http.ResponseWriter, r *http.Request) { JSON(w, Catalog()) }))
	mux.HandleFunc("GET /v1/admin/authorization/revision", read(func(w http.ResponseWriter, r *http.Request) {
		revision, err := store.Revision(r.Context())
		if err != nil {
			WriteError(w, r, err)
			return
		}
		service.ObserveRevision(revision)
		JSON(w, map[string]uint64{"committedRevision": revision, "appliedRevision": service.AppliedRevision()})
	}))
	mux.HandleFunc("GET /v1/admin/authorization/audit", read(func(w http.ResponseWriter, r *http.Request) {
		i, err := CurrentIdentity(r.Context())
		if err != nil {
			WriteError(w, r, err)
			return
		}
		var before uint64
		if raw := r.URL.Query().Get("before"); raw != "" {
			before, err = strconv.ParseUint(raw, 10, 64)
			if err != nil {
				WriteError(w, r, ErrInvalid)
				return
			}
		}
		out, err := store.Audit(r.Context(), i.TenantID, before)
		if err != nil {
			WriteError(w, r, err)
			return
		}
		JSON(w, out)
	}))
	for _, route := range []struct{ pattern, operation string }{
		{"POST /v1/admin/authorization/roles", "role.create"}, {"PATCH /v1/admin/authorization/roles/{key}", "role.update"}, {"DELETE /v1/admin/authorization/roles/{key}", "role.delete"},
		{"POST /v1/admin/authorization/permission-groups", "group.create"}, {"PATCH /v1/admin/authorization/permission-groups/{key}", "group.update"}, {"DELETE /v1/admin/authorization/permission-groups/{key}", "group.delete"},
		{"PUT /v1/admin/authorization/roles/{key}/permission-groups", "role.groups"}, {"PUT /v1/admin/authorization/permission-groups/{key}/grants", "group.grants"},
		{"POST /v1/admin/authorization/group-mappings", "mapping.add"}, {"DELETE /v1/admin/authorization/group-mappings", "mapping.delete"},
	} {
		mux.HandleFunc(route.pattern, service.RequirePermission("authorization_config", "write")(func(w http.ResponseWriter, r *http.Request) {
			r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
			decoder := json.NewDecoder(r.Body)
			decoder.DisallowUnknownFields()
			var cmd Command
			if decoder.Decode(&cmd) != nil || decoder.Decode(&struct{}{}) != io.EOF {
				WriteError(w, r, ErrInvalid)
				return
			}
			cmd.Operation = route.operation
			if key := r.PathValue("key"); key != "" {
				if cmd.Key != "" && cmd.Key != key {
					WriteError(w, r, ErrInvalid)
					return
				}
				cmd.Key = key
			}
			revision, err := store.Apply(r.Context(), provider, cmd)
			if err != nil {
				if errors.Is(err, ErrConflict) {
					service.ConfigurationConflicts.Add(1)
				}
				WriteError(w, r, err)
				return
			}
			service.ObserveRevision(revision)
			// Once committed, report success even if the local rebuild is pending.
			_ = service.Refresh(r.Context())
			JSON(w, Commit{revision, service.AppliedRevision() >= revision})
		}))
	}
	return http.NewCrossOriginProtection().Handler(mux)
}
