package buildcontext_test

import (
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func TestLanguageProfileValidation(t *testing.T) {
	legacy := fixture()
	if language, version := legacy.SourceSets[0].SyntaxLanguage(); language != "java" || version != "17" {
		t.Fatal(language, version)
	}
	for _, change := range []func(*bc.SourceSet){
		func(s *bc.SourceSet) { s.LanguageVersion = "21" },
		func(s *bc.SourceSet) { s.Language = "python"; s.LanguageVersion = "3.12" },
		func(s *bc.SourceSet) { s.JDKID = "" },
	} {
		in := fixture()
		change(&in.SourceSets[0])
		if err := in.Validate(); err == nil {
			t.Fatal("invalid Java profile accepted")
		}
	}
	in := bc.Inventory{Inputs: []bc.Input{{ID: "src", Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: "."}}},
		Modules:    []bc.Module{{ID: "app", Name: "app", Directory: "."}},
		SourceSets: []bc.SourceSet{{ID: "main", ModuleID: "app", Name: "main", Kind: bc.SourceSetMain, Language: "python", LanguageVersion: "3.12", SourceRootIDs: []bc.InputID{"src"}}}}
	if err := in.Validate(); err != nil {
		t.Fatal("non-Java profile requires fake JDK", err)
	}
	in.SourceSets[0].LanguageVersion = ""
	if err := in.Validate(); err == nil {
		t.Fatal("implicit non-Java version accepted")
	}
}
