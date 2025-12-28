package tree_sitter_siasun_test

import (
	"testing"

	tree_sitter "github.com/tree-sitter/go-tree-sitter"
	tree_sitter_siasun "github.com/tree-sitter/tree-sitter-siasun/bindings/go"
)

func TestCanLoadGrammar(t *testing.T) {
	language := tree_sitter.NewLanguage(tree_sitter_siasun.Language())
	if language == nil {
		t.Errorf("Error loading Siasun grammar")
	}
}
