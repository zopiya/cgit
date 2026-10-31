package runtime

import (
	"bufio"
	"bytes"
	"fmt"
	"html"
	"io"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"

	chromahtml "github.com/alecthomas/chroma/v2/formatters/html"
	"github.com/alecthomas/chroma/v2/lexers"
	"github.com/alecthomas/chroma/v2/styles"
	"github.com/microcosm-cc/bluemonday"
	"github.com/yuin/goldmark"
	"github.com/yuin/goldmark/extension"
	"github.com/yuin/goldmark/renderer"
	goldhtml "github.com/yuin/goldmark/renderer/html"
	"github.com/yuin/goldmark/util"
	xhtml "golang.org/x/net/html"
	"golang.org/x/net/html/atom"
)

func AboutFilter(args []string) error {
	data, e := io.ReadAll(io.LimitReader(os.Stdin, (2<<20)+1))
	if e != nil {
		return e
	}
	if len(data) > 2<<20 {
		return fmt.Errorf("Markdown input exceeds 2 MiB filter limit")
	}
	md := goldmark.New(goldmark.WithExtensions(extension.GFM, extension.DefinitionList, extension.Footnote), goldmark.WithRendererOptions(goldhtml.WithUnsafe(), renderer.WithNodeRenderers(util.Prioritized(&fencedCodeRenderer{}, 100))))
	var rendered bytes.Buffer
	if e = md.Convert(data, &rendered); e != nil {
		return e
	}
	filename := "README.md"
	if len(args) > 0 {
		filename = args[0]
	}
	repoURL := os.Getenv("CGIT_REPO_URL")
	commit := ""
	if repoURL != "" {
		c, e := LoadConfig()
		if e != nil {
			return e
		}
		repo, _, e := repository(c, repoURL, true)
		if e != nil {
			return e
		}
		revision := ""
		if a, b, ok := strings.Cut(filename, ":"); ok {
			revision, filename = a, b
		}
		if revision == "" {
			revision = envDefault("CGIT_REPO_DEFBRANCH", "HEAD")
		}
		b, e := git(repo, "rev-parse", "--verify", "--end-of-options", revision+"^{commit}")
		if e == nil {
			commit = strings.TrimSpace(string(b))
		}
	}
	nodes, e := xhtml.ParseFragment(bytes.NewReader(rendered.Bytes()), &xhtml.Node{Type: xhtml.ElementNode, Data: "div", DataAtom: atom.Div})
	if e != nil {
		return e
	}
	var visit func(*xhtml.Node)
	visit = func(n *xhtml.Node) {
		if commit != "" && (n.Data == "img" || n.Data == "a") {
			key, route := "href", "tree"
			if n.Data == "img" {
				key, route = "src", "plain"
			}
			for i, a := range n.Attr {
				if a.Key != key {
					continue
				}
				u, e := url.Parse(a.Val)
				if e != nil || u.Scheme != "" || u.Host != "" || strings.HasPrefix(a.Val, "/") || strings.HasPrefix(a.Val, "#") {
					continue
				}
				p := path.Clean(path.Join(path.Dir(filename), u.Path))
				if p == ".." || strings.HasPrefix(p, "../") {
					continue
				}
				target := url.URL{Path: "/" + repoURL + "/" + route + "/" + p, RawQuery: url.Values{"id": {commit}}.Encode(), Fragment: u.Fragment}
				n.Attr[i].Val = target.String()
			}
		}
		for ch := n.FirstChild; ch != nil; ch = ch.NextSibling {
			visit(ch)
		}
	}
	var output bytes.Buffer
	for _, n := range nodes {
		visit(n)
		if e = xhtml.Render(&output, n); e != nil {
			return e
		}
	}
	policy := bluemonday.UGCPolicy()
	policy.AllowAttrs("class", "id").Globally()
	policy.AllowElements("details", "summary", "kbd", "sup", "sub")
	if e = highlightCSS(os.Stdout); e != nil {
		return e
	}
	fmt.Fprint(os.Stdout, `<div class="markdown-body">`+policy.Sanitize(output.String())+`</div>`)
	return nil
}
func SourceFilter(args []string) error {
	data, e := io.ReadAll(io.LimitReader(os.Stdin, (2<<20)+1))
	if e != nil {
		return e
	}
	if len(data) > 2<<20 {
		return fmt.Errorf("source input exceeds 2 MiB filter limit")
	}
	filename := "file"
	if len(args) > 0 {
		filename = args[0]
	}
	oid, size, ok := parsePointer(data)
	if !ok {
		lexer := lexers.Match(filename)
		if lexer == nil {
			lexer = lexers.Fallback
		}
		tokens, err := lexer.Tokenise(nil, string(data))
		if err != nil {
			return err
		}
		formatter := chromahtml.New(chromahtml.WithClasses(true), chromahtml.PreventSurroundingPre(true))
		fmt.Fprint(os.Stdout, "<style>")
		if err = formatter.WriteCSS(os.Stdout, styles.Get("github")); err != nil {
			return err
		}
		fmt.Fprint(os.Stdout, "</style><span class=\"highlight\"><span class=\"chroma\">")
		err = formatter.Format(os.Stdout, styles.Get("github"), tokens)
		fmt.Fprint(os.Stdout, "</span></span>")
		return err
	}
	c, e := LoadConfig()
	if e != nil {
		return e
	}
	name := os.Getenv("CGIT_REPO_URL")
	_, canonical, e := repository(c, name, true)
	available := false
	if e == nil {
		name = canonical
		p, e := ObjectPath(c, name, oid)
		if e == nil {
			st, e := os.Stat(p)
			available = e == nil && st.Mode().IsRegular() && st.Size() == size
		}
	}
	href := "/" + escapePath(name) + "/info/lfs/objects/" + oid + "?" + url.Values{"filename": {filename}}.Encode()
	preview := href + "&inline=1"
	fmt.Fprintf(os.Stdout, "</code></pre><div class=\"lfs-file\"><strong>Git LFS · %s</strong><p>%s bytes · SHA-256 <code>%s</code></p>", html.EscapeString(filename), comma(size), oid)
	if available {
		fmt.Fprintf(os.Stdout, "<p><a href=\"%s\">Download original / 下载原文件</a></p>", html.EscapeString(href))
		suffix := strings.ToLower(filepath.Ext(filename))
		if contains([]string{".png", ".jpg", ".jpeg", ".gif", ".webp"}, suffix) && size <= 10<<20 {
			fmt.Fprintf(os.Stdout, "<img loading=\"lazy\" src=\"%s\" alt=\"%s\">", html.EscapeString(preview), html.EscapeString(filename))
		} else if suffix == ".pdf" {
			fmt.Fprintf(os.Stdout, "<p><a href=\"%s\" target=\"_blank\" rel=\"noopener\">Open PDF / 打开 PDF</a></p>", html.EscapeString(preview))
		}
	} else {
		fmt.Fprint(os.Stdout, `<p role="status">LFS object missing or size mismatch / 对象缺失或大小不符，请先上传或迁入。</p>`)
	}
	fmt.Fprint(os.Stdout, `<p class="lfs-note">ZIP / tar.gz snapshots contain the Git pointer, not this original file.<br>仓库归档包含 LFS 指针；需要完整文件请使用 git clone。</p></div><pre><code>`)
	return nil
}
func comma(n int64) string {
	s := strconv.FormatInt(n, 10)
	for i := len(s) - 3; i > 0; i -= 3 {
		s = s[:i] + "," + s[i:]
	}
	return s
}
func AccessLog() error {
	p := envDefault("CGIT_ACCESS_LOG", "/var/cache/cgit/access.log")
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 4096), 1<<20)
	for scanner.Scan() {
		line := append(append([]byte{}, scanner.Bytes()...), '\n')
		st, e := os.Stat(p)
		if e != nil && !os.IsNotExist(e) {
			return e
		}
		if e == nil && st.Size()+int64(len(line)) > 5<<20 {
			for i := 3; i >= 0; i-- {
				old := p
				if i > 0 {
					old += "." + strconv.Itoa(i)
				}
				if e = os.Rename(old, p+"."+strconv.Itoa(i+1)); e != nil && !os.IsNotExist(e) {
					return e
				}
			}
		}
		f, e := os.OpenFile(p, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
		if e != nil {
			return e
		}
		_, e = f.Write(line)
		ce := f.Close()
		if e != nil {
			return e
		}
		if ce != nil {
			return ce
		}
	}
	return scanner.Err()
}
