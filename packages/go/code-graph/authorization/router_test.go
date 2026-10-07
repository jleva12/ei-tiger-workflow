package authorization

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRouterClassifiesRoutesAndUnconfiguredIdentityDenies(t *testing.T) {
	service, _, _ := fixture(t, "tenant")
	router := NewRouter(service, nil)
	calls := 0
	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { calls++; w.WriteHeader(204) })
	router.Public("GET /healthz", handler)
	router.Session("GET /session", handler)
	router.Capability("POST /records", "organization_collection", "create", handler)
	routes := router.Routes()
	if len(routes) != 3 || routes[0].Classification != "public" || routes[1].Classification != "session" || routes[2].Classification != "capability" {
		t.Fatalf("missing inventory: %+v", routes)
	}
	for _, test := range []struct {
		method, path, cookie string
		status               int
	}{
		{"GET", "/healthz", "", 204},
		{"GET", "/session", "", 401},
		{"POST", "/records", "", 401},
		{"GET", "/session", "company_session=unverified", 503},
		{"POST", "/records", "company_session=unverified", 503},
	} {
		request := httptest.NewRequest(test.method, test.path, nil)
		request.Header.Set("Cookie", test.cookie)
		response := httptest.NewRecorder()
		router.ServeHTTP(response, request)
		if response.Code != test.status {
			t.Fatalf("%s %s: %d", test.method, test.path, response.Code)
		}
		if test.status >= 400 && response.Header().Get("X-Request-ID") == "" {
			t.Fatal("missing request ID")
		}
	}
	if calls != 1 {
		t.Fatal("protected handler ran without verified identity")
	}
	routes[0].Classification = "session"
	if router.Routes()[0].Classification != "public" {
		t.Fatal("inventory exposed mutable registrations")
	}
}
