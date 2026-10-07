package buildcontext

import "testing"

func TestCompilationSourcePatterns(t *testing.T) {
	for _, test := range []struct {
		pattern, name string
		want          bool
	}{{"**/*.java", "A.java", true}, {"**/*.java", "a/b/A.java", true}, {"*.java", "a/A.java", false}, {"**/A.java", "b/A.java", true}, {"**/A.java", "A.java", true}, {"a/**/b/*.java", "a/b/C.java", true}, {"a/**/b/*.java", "a/z/y/b/C.java", true}, {"a/**/b/*.java", "b/C.java", false}} {
		if got := MatchSourcePattern(test.pattern, test.name); got != test.want {
			t.Fatalf("%s %s=%v", test.pattern, test.name, got)
		}
	}
	for _, bad := range []string{"../A.java", "/A.java", "a//b", "a\\b", "[broken", ""} {
		if ValidSourcePattern(bad) {
			t.Fatal("invalid pattern accepted", bad)
		}
	}
	set := SourceSet{IncludePatterns: []string{"**/*.java"}, ExcludePatterns: []string{"**/Generated*.java"}}
	if !set.SelectsSource("A.java") || set.SelectsSource("a/GeneratedThing.java") || set.SelectsSource("x.txt") {
		t.Fatal("include/exclude precedence")
	}
}
