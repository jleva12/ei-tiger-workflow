package environment

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func TestFingerprintInputsAndLimits(t *testing.T) {
	root := t.TempDir()
	file := filepath.Join(root, "module.pyi")
	if err := os.WriteFile(file, []byte("first"), 0600); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	before, err := DigestTree(ctx, root, 10)
	if err != nil {
		t.Fatal(err)
	}
	again, err := DigestTree(ctx, root, 10)
	if err != nil || before != again {
		t.Fatal("unstable fingerprint", err)
	}
	if err := os.WriteFile(file, []byte("other"), 0600); err != nil {
		t.Fatal(err)
	}
	after, err := DigestTree(ctx, root, 10)
	if err != nil || before == after {
		t.Fatal("same-size edit hidden", err)
	}
	if _, err := DigestTree(ctx, root, 4); !errors.Is(err, bc.ErrLimitExceeded) {
		t.Fatal(err)
	}
	budget := TreeBudget{Bytes: 10, Files: 1, Depth: 1}
	if _, err := DigestTreeWithBudget(ctx, root, &budget); err != nil {
		t.Fatal(err)
	}
	if _, err := DigestTreeWithBudget(ctx, root, &budget); !errors.Is(err, bc.ErrLimitExceeded) {
		t.Fatal("aggregate file limit", err)
	}
	if err := os.Mkdir(filepath.Join(root, "namespace"), 0700); err != nil {
		t.Fatal(err)
	}
	withNamespace, err := DigestTree(ctx, root, 10)
	if err != nil || withNamespace == after {
		t.Fatal("namespace directory hidden", err)
	}
	ctx, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := DigestTree(ctx, root, 10); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if err := os.Symlink(file, filepath.Join(root, "linked.pyi")); err != nil {
		t.Fatal(err)
	}
	if _, err := DigestTree(context.Background(), root, 100); err == nil {
		t.Fatal("symlink accepted")
	}
}
