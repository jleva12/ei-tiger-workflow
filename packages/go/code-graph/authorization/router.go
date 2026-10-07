package authorization

import (
	"net/http"
	"strings"
)

// Router has no unclassified Handle method. Every route is explicitly public,
// session-only, a capability, or a resource decision; Routes is inspectable.
type Router struct {
	mux      *http.ServeMux
	service  *Service
	provider Provider
	registry Registry
}

func NewRouter(service *Service, provider Provider) *Router {
	return &Router{mux: http.NewServeMux(), service: service, provider: provider}
}
func (r *Router) ServeHTTP(w http.ResponseWriter, request *http.Request) { r.mux.ServeHTTP(w, request) }
func (r *Router) Routes() []RoutePolicy                                  { return r.registry.Routes() }
func (r *Router) add(pattern, classification, kind, action string, handler http.Handler) {
	method, path, ok := strings.Cut(pattern, " ")
	if !ok {
		method, path = "*", pattern
	}
	r.registry.Add(RoutePolicy{Method: method, Path: path, Classification: classification, Kind: kind, Action: action})
	if classification != "public" {
		handler = r.service.Middleware(r.provider, handler)
	}
	r.mux.Handle(pattern, handler)
}
func (r *Router) Public(pattern string, handler http.Handler) {
	r.add(pattern, "public", "", "", handler)
}
func (r *Router) Session(pattern string, handler http.Handler) {
	r.add(pattern, "session", "", "", handler)
}
func (r *Router) Capability(pattern, kind, action string, handler http.Handler) {
	r.add(pattern, "capability", kind, action, r.service.RequirePermission(kind, action)(handler.ServeHTTP))
}
func (r *Router) Resource(pattern, kind, action string, resolver ResourceResolver, handler http.Handler) {
	r.add(pattern, "resource", kind, action, r.service.RequireResource(kind, action, resolver)(handler.ServeHTTP))
}
