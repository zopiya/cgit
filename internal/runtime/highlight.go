package runtime

import (
	"bytes"
	"io"
	"strings"

	chromahtml "github.com/alecthomas/chroma/v2/formatters/html"
	"github.com/alecthomas/chroma/v2/lexers"
	"github.com/alecthomas/chroma/v2/styles"
	"github.com/yuin/goldmark/ast"
	"github.com/yuin/goldmark/renderer"
	"github.com/yuin/goldmark/util"
)

// fencedCodeRenderer emits only generated token markup. The complete Markdown
// document still passes through the HTML sanitizer after rendering.
type fencedCodeRenderer struct{}

func (*fencedCodeRenderer) RegisterFuncs(reg renderer.NodeRendererFuncRegisterer) {
	reg.Register(ast.KindFencedCodeBlock, renderFencedCode)
}
func renderFencedCode(w util.BufWriter, source []byte, node ast.Node, entering bool) (ast.WalkStatus, error) {
	if !entering {
		return ast.WalkContinue, nil
	}
	block := node.(*ast.FencedCodeBlock)
	var b bytes.Buffer
	for i := 0; i < block.Lines().Len(); i++ {
		line := block.Lines().At(i)
		b.Write(line.Value(source))
	}
	lexer := lexers.Get(strings.TrimSpace(string(block.Language(source))))
	if lexer == nil {
		lexer = lexers.Fallback
	}
	tokens, e := lexer.Tokenise(nil, b.String())
	if e != nil {
		return ast.WalkStop, e
	}
	formatter := chromahtml.New(chromahtml.WithClasses(true))
	if e = formatter.Format(w, styles.Get("github"), tokens); e != nil {
		return ast.WalkStop, e
	}
	return ast.WalkSkipChildren, nil
}
func highlightCSS(w io.Writer) error {
	if _, e := io.WriteString(w, "<style>"); e != nil {
		return e
	}
	formatter := chromahtml.New(chromahtml.WithClasses(true))
	if e := formatter.WriteCSS(w, styles.Get("github")); e != nil {
		return e
	}
	_, e := io.WriteString(w, "</style>")
	return e
}
