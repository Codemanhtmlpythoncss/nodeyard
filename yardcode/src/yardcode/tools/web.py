"""Tools that reach the internet: WebSearch, WebFetch, Wikipedia, Arxiv and Weather. None of them needs an API key.

Fetching refuses addresses on your own network (and loopback) unless you allow it, and connects to the
address it checked, so a hostile page can't point a name at your router after the check.
"""
import base64
import gzip
import html as htmllib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser

from .. import net, util
from .base import Result, Tool, ToolError, need

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 yardcode/0.1"
MAX_BYTES = 3 * 1024 * 1024


def host_is_private(ip):
    a = ipaddress.ip_address(ip)
    return not a.is_global or a.is_multicast


class _Pinned:
    """http.client connections that connect to a given address, whatever the host name says."""

    @staticmethod
    def make(scheme, host, port, ip, timeout):
        if scheme == "https":
            ctx = net.ssl_context()

            class C(http.client.HTTPSConnection):
                def connect(self):
                    sock = socket.create_connection((ip, port), timeout)
                    self.sock = ctx.wrap_socket(sock, server_hostname=host)
            return C(host, port, timeout=timeout, context=ctx)

        class P(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.create_connection((ip, port), timeout)
        return P(host, port, timeout=timeout)


def resolve_public(host, port, allow_private):
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ToolError("Can't find the host %s (%s)." % (host, e))
    addrs = [i[4][0] for i in infos]
    if not addrs:
        raise ToolError("No address for %s." % host)
    if not allow_private:
        for a in addrs:
            if host_is_private(a):
                raise ToolError("%s is on a private or local network (%s). Fetching those is off; turn it on with web.allow_private if you mean it." % (host, a))
    return addrs[0]


def http_fetch(url, method="GET", data=None, headers=None, timeout=20, max_bytes=MAX_BYTES, allow_private=False, redirects=5):
    """(status, headers, body, final url). Follows redirects itself so every hop is checked."""
    hdrs = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.8", "Accept-Encoding": "gzip, deflate"}
    hdrs.update(headers or {})
    for _ in range(redirects + 1):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ToolError("Only http and https addresses can be fetched (got %r)." % url[:80])
        port = u.port or (443 if u.scheme == "https" else 80)
        ip = resolve_public(u.hostname, port, allow_private)
        conn = _Pinned.make(u.scheme, u.hostname, port, ip, timeout)
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        try:
            conn.request(method, path, body=data, headers=dict(hdrs, Host=u.netloc.split("@")[-1]))
            resp = conn.getresponse()
            body = resp.read(max_bytes + 1)
            rh = {k.lower(): v for k, v in resp.getheaders()}
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            raise ToolError("Couldn't fetch %s: %s" % (url[:100], e))
        finally:
            conn.close()
        if resp.status in (301, 302, 303, 307, 308) and rh.get("location"):
            url = urllib.parse.urljoin(url, rh["location"])
            if resp.status == 303:
                method, data = "GET", None
            continue
        enc = rh.get("content-encoding", "")
        try:
            if "gzip" in enc:
                body = gzip.decompress(body)
            elif "deflate" in enc:
                body = zlib.decompress(body)
        except (OSError, zlib.error):
            pass
        return resp.status, rh, body[:max_bytes], url
    raise ToolError("Too many redirects from %s." % url[:100])


def decode_body(body, headers):
    ctype = headers.get("content-type", "")
    m = re.search(r"charset=([\w-]+)", ctype, re.I) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    cs = (m.group(1).decode() if m and isinstance(m.group(1), bytes) else (m.group(1) if m else "utf-8"))
    try:
        return body.decode(cs, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


# ---- HTML to readable text -------------------------------------------------------------------------------

SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "select", "button", "input", "textarea", "head"}
BLOCK = {"p", "div", "section", "article", "main", "ul", "ol", "table", "tr", "blockquote", "pre", "figure", "figcaption", "details", "summary", "dl", "dt",
         "dd", "header", "footer", "nav", "aside", "form", "h1", "h2", "h3", "h4", "h5", "h6", "li", "hr", "br"}


class HtmlText(HTMLParser):
    def __init__(self, base=""):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out = []
        self.skip = 0
        self.pre = 0
        self.title = ""
        self.in_title = False
        self.href = None
        self.link_text = []
        self.list_stack = []
        self.main_start = None   # index in out where <main>/<article> began
        self.main_end = None

    def _nl(self, n=1):
        if not self.out:
            return
        tail = "".join(self.out[-3:])
        have = len(tail) - len(tail.rstrip("\n"))
        if have < n:
            self.out.append("\n" * (n - have))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("main", "article") and self.main_start is None:
            self.main_start = len(self.out)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._nl(2)
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._nl()
            self.out.append("  " * max(0, len(self.list_stack) - 1) + ("%d. " % self.list_stack[-1][0] if self.list_stack and self.list_stack[-1][1] else "- "))
            if self.list_stack and self.list_stack[-1][1]:
                self.list_stack[-1][0] += 1
        elif tag in ("ul", "ol"):
            self._nl()
            self.list_stack.append([1, tag == "ol"])
        elif tag == "pre":
            self._nl(2)
            self.out.append("```\n")
            self.pre += 1
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "a" and a.get("href") and not a["href"].startswith(("#", "javascript:", "mailto:")):
            self.href = urllib.parse.urljoin(self.base, a["href"])
            self.link_text = []
            self.out.append("[")
        elif tag == "img" and a.get("alt"):
            self.out.append("[image: %s]" % a["alt"].strip())
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in BLOCK:
            self._nl(2 if tag in ("p", "blockquote", "table", "header") else 1)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("main", "article") and self.main_start is not None and self.main_end is None:
            self.main_end = len(self.out)
        if tag in ("ul", "ol") and self.list_stack:
            self.list_stack.pop()
            self._nl()
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```")
            self._nl(2)
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "a" and self.href:
            self.out.append("](%s)" % self.href)
            self.href = None
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "tr", "li", "blockquote", "section", "table"):
            self._nl(2 if tag.startswith("h") or tag in ("p", "table", "blockquote") else 1)

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        if self.pre:
            self.out.append(data)
        else:
            text = re.sub(r"\s+", " ", data)
            if text.strip() or (self.out and not self.out[-1].endswith(("\n", " "))):
                self.out.append(text)

    def text(self, prefer_main=True):
        parts = self.out
        if prefer_main and self.main_start is not None:
            end = self.main_end if self.main_end is not None else len(parts)
            chosen = "".join(parts[self.main_start:end])
            if len(chosen) > 400:
                parts = [chosen]
        t = "".join(parts)
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        t = re.sub(r"\[\s*\]\([^)]*\)", "", t)
        return t.strip()


def html_to_text(html, base=""):
    p = HtmlText(base)
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    return p.title.strip(), p.text()


# ---- the tools ---------------------------------------------------------------------------------------------

def _allow_private(args, ctx):
    return bool(args.get("_private_ok") or ctx.settings.get("web.allow_private"))


def _timeout(ctx):
    return int(ctx.settings.get("web.timeout", 20) or 20)


class WebFetch(Tool):
    name = "WebFetch"
    kind = "net"
    description = "Fetch a web page or file as readable text; pass start to read on in long pages."
    parameters = {"type": "object", "properties": {
        "url": {"type": "string", "description": "http(s) address"},
        "start": {"type": "integer", "description": "Character offset to start from (default 0)"},
        "max_chars": {"type": "integer", "description": "How many characters to return (default 12000)"}}, "required": ["url"]}

    def specifier(self, args, ctx):
        host = urllib.parse.urlsplit(str(args.get("url", ""))).hostname or ""
        return "domain:" + host

    def summary(self, args, ctx):
        return "WebFetch(%s)" % str(args.get("url", ""))[:110]

    def run(self, args, ctx):
        url = need(args, "url").strip()
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        status, headers, body, final = http_fetch(url, timeout=_timeout(ctx), allow_private=_allow_private(args, ctx))
        if status >= 400:
            raise ToolError("%s answered %d." % (final[:100], status))
        ctype = headers.get("content-type", "").lower()
        title = ""
        if "pdf" in ctype or final.lower().endswith(".pdf"):
            try:
                out = subprocess.run(["pdftotext", "-layout", "-", "-"], input=body, capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
            except (OSError, subprocess.SubprocessError):
                raise ToolError("That is a PDF and pdftotext isn't installed (poppler-utils), so it can't be read.")
            text = out
        elif "html" in ctype or body[:200].lstrip().lower().startswith((b"<!doctype html", b"<html")):
            title, text = html_to_text(decode_body(body, headers), final)
        elif "json" in ctype:
            raw = decode_body(body, headers)
            try:
                text = json.dumps(json.loads(raw), indent=2)
            except ValueError:
                text = raw
        elif ctype.startswith("text/") or "xml" in ctype or "javascript" in ctype or not ctype:
            text = decode_body(body, headers)
            if "xml" in ctype and ("<rss" in text[:500] or "<feed" in text[:500]):
                text = feed_text(text)
        else:
            raise ToolError("That is %s (%s), not something that can be read as text." % (ctype.split(";")[0] or "a binary file", util.human_bytes(len(body))))
        start = max(0, int(args.get("start") or 0))
        n = max(500, min(int(args.get("max_chars") or 12000), 60000))
        piece = text[start:start + n]
        head = "URL: %s\n" % final + ("Title: %s\n" % title if title else "")
        tail = ""
        if start + n < len(text):
            tail = "\n\n[%d more characters; call again with start=%d]" % (len(text) - start - n, start + n)
        return Result(head + "\n" + piece + tail, summary="Fetched %s (%s)" % (urllib.parse.urlsplit(final).hostname, util.human_bytes(len(body))),
                      preview=[title or final])


def feed_text(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return xml_text
    out = []
    for item in list(root.iter("item")) + list(root.iter("{http://www.w3.org/2005/Atom}entry")):
        def f(tag):
            e = item.find(tag)
            if e is None:
                e = item.find("{http://www.w3.org/2005/Atom}" + tag)
            return (e.text or "").strip() if e is not None and e.text else (e.get("href", "") if e is not None else "")
        out.append("- %s\n  %s\n  %s" % (f("title"), f("link"), re.sub(r"<[^>]+>", "", f("description") or f("summary"))[:300]))
    return "\n".join(out) or xml_text


# ---- search --------------------------------------------------------------------------------------------------

def _strip(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def _ddg_url(href):
    href = htmllib.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.urlsplit(href)
    if "duckduckgo.com" in (q.hostname or "") and q.path.startswith("/l/"):
        t = urllib.parse.parse_qs(q.query).get("uddg")
        if t:
            return t[0]
    return href


def _bing_url(href):
    href = htmllib.unescape(href)
    q = urllib.parse.urlsplit(href)
    if (q.hostname or "").endswith("bing.com") and q.path.startswith("/ck/"):
        u = urllib.parse.parse_qs(q.query).get("u", [""])[0]
        if u.startswith("a1"):
            try:
                raw = u[2:] + "=" * (-len(u[2:]) % 4)
                return base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
            except ValueError:
                return href
    return href


def _attr(tag, name):
    m = re.search(r'\b%s\s*=\s*("([^"]*)"|\'([^\']*)\')' % name, tag)
    return (m.group(2) if m.group(2) is not None else m.group(3)) if m else ""


def parse_ddg_html(text, n=10):
    """Results from html.duckduckgo.com/html/ (the result links carry class result__a; snippets class result__snippet)."""
    out = []
    tags = list(re.finditer(r'<a\b[^>]*\bclass\s*=\s*["\'][^"\']*\bresult__a\b[^"\']*["\'][^>]*>(.*?)</a>', text, re.S))
    for i, m in enumerate(tags):
        href = _attr(m.group(0), "href")
        if not href:
            continue
        end = tags[i + 1].start() if i + 1 < len(tags) else len(text)
        snip = re.search(r'class\s*=\s*["\'][^"\']*result__snippet[^"\']*["\'][^>]*>(.*?)</(?:a|td|div|span)>', text[m.end():end], re.S)
        out.append({"title": _strip(m.group(1)), "url": _ddg_url(href), "snippet": _strip(snip.group(1)) if snip else ""})
        if len(out) >= n * 2:
            break
    return out


def parse_ddg_lite(text, n=10):
    """Results from lite.duckduckgo.com/lite/ (a table: link rows, then snippet rows)."""
    out = []
    links = list(re.finditer(r'<a\b[^>]*\bclass\s*=\s*["\']result-link["\'][^>]*>(.*?)</a>', text, re.S))
    snips = re.findall(r'class\s*=\s*["\']result-snippet["\'][^>]*>(.*?)</td>', text, re.S)
    for i, m in enumerate(links):
        href = _attr(m.group(0), "href")
        if href:
            out.append({"title": _strip(m.group(1)), "url": _ddg_url(href), "snippet": _strip(snips[i]) if i < len(snips) else ""})
    return out[:n * 2]


def parse_bing(text, n=10):
    out = []
    for blk in re.findall(r'<li class="b_algo".*?</li>', text, re.S):
        m = re.search(r'<h2[^>]*>\s*<a\b([^>]*)>(.*?)</a>', blk, re.S)
        if not m:
            continue
        href = _attr(m.group(1), "href")
        sn = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S) or re.search(r'class="b_caption"[^>]*>(.*?)</div>', blk, re.S)
        if href:
            out.append({"title": _strip(m.group(2)), "url": _bing_url(href), "snippet": _strip(sn.group(1)) if sn else ""})
    return out[:n * 2]


def search_ddg(query, n, ctx):
    status, h, body, _ = http_fetch("https://html.duckduckgo.com/html/", "POST", urllib.parse.urlencode({"q": query, "kl": "wt-wt"}).encode(),
                                    {"Content-Type": "application/x-www-form-urlencoded", "Referer": "https://html.duckduckgo.com/"}, timeout=_timeout(ctx), allow_private=True)
    text = decode_body(body, h)
    out = parse_ddg_html(text, n)
    if not out and ("anomaly" in text or "captcha" in text.lower() or status in (202, 403, 429)):
        raise ToolError("DuckDuckGo asked for a captcha")
    return out


def search_ddg_lite(query, n, ctx):
    status, h, body, _ = http_fetch("https://lite.duckduckgo.com/lite/", "POST", urllib.parse.urlencode({"q": query}).encode(),
                                    {"Content-Type": "application/x-www-form-urlencoded"}, timeout=_timeout(ctx), allow_private=True)
    return parse_ddg_lite(decode_body(body, h), n)


def search_bing(query, n, ctx):
    status, h, body, _ = http_fetch("https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "en"}), timeout=_timeout(ctx), allow_private=True)
    return parse_bing(decode_body(body, h), n)


def search_searxng(base, query, n, ctx):
    status, h, body, _ = http_fetch(base.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"}), timeout=_timeout(ctx), allow_private=True)
    if status != 200:
        raise ToolError("SearXNG answered %d" % status)
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")} for r in json.loads(body).get("results", [])][:n * 2]


def search_brave(key, query, n, ctx):
    status, h, body, _ = http_fetch("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": min(20, n * 2)}),
                                    headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=_timeout(ctx), allow_private=True)
    if status != 200:
        raise ToolError("Brave Search answered %d" % status)
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": _strip(r.get("description", ""))}
            for r in (json.loads(body).get("web") or {}).get("results", [])]


def search_tavily(key, query, n, ctx):
    status, h, body, _ = http_fetch("https://api.tavily.com/search", "POST", json.dumps({"api_key": key, "query": query, "max_results": min(20, n * 2)}).encode(),
                                    {"Content-Type": "application/json"}, timeout=_timeout(ctx), allow_private=True)
    if status != 200:
        raise ToolError("Tavily answered %d" % status)
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")[:400]} for r in json.loads(body).get("results", [])]


def search_wikipedia(query, n, ctx):
    status, h, body, _ = http_fetch("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": n}), timeout=_timeout(ctx), allow_private=True)
    return [{"title": r["title"], "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(r["title"].replace(" ", "_")), "snippet": _strip(r.get("snippet", ""))}
            for r in json.loads(body).get("query", {}).get("search", [])]


def web_search(query, n, ctx, engine=None):
    """(results, engine used). Tries the configured engine, then the keyless ones in turn."""
    s = ctx.settings
    engine = engine or s.get("search.engine", "auto")
    tries = []
    if s.get("search.searxng_url"):
        tries.append(("searxng", lambda: search_searxng(s.get("search.searxng_url"), query, n, ctx)))
    if s.get("search.brave_key"):
        tries.append(("brave", lambda: search_brave(s.get("search.brave_key"), query, n, ctx)))
    if s.get("search.tavily_key"):
        tries.append(("tavily", lambda: search_tavily(s.get("search.tavily_key"), query, n, ctx)))
    keyless = [("duckduckgo", lambda: search_ddg(query, n, ctx)), ("bing", lambda: search_bing(query, n, ctx)),
               ("duckduckgo-lite", lambda: search_ddg_lite(query, n, ctx)), ("wikipedia", lambda: search_wikipedia(query, n, ctx))]
    if engine != "auto":
        pick = [t for t in tries + keyless if t[0] == engine or t[0].startswith(engine)]
        tries = pick or tries
    else:
        tries += keyless
    errors = []
    for name, fn in tries:
        try:
            res = fn()
        except (ToolError, ValueError, OSError, http.client.HTTPException) as e:
            errors.append("%s: %s" % (name, e))
            continue
        if res:
            return res, name
        errors.append("%s: no results" % name)
    raise ToolError("Web search failed (%s). Check the internet connection, or set search.searxng_url / search.brave_key." % "; ".join(errors[-3:]))


class WebSearch(Tool):
    name = "WebSearch"
    kind = "net"
    description = "Search the web (titles, URLs, snippets). WebFetch the best results; cite the URLs you used."
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "What to search for"},
        "max_results": {"type": "integer", "description": "How many results (default 6, max 12)"},
        "allowed_domains": {"type": "array", "items": {"type": "string"}, "description": "Only results from these sites"},
        "blocked_domains": {"type": "array", "items": {"type": "string"}, "description": "Leave out results from these sites"}}, "required": ["query"]}

    def specifier(self, args, ctx):
        return args.get("query", "")

    def run(self, args, ctx):
        q = need(args, "query").strip()
        if not q:
            raise ToolError("The query is empty.")
        n = max(1, min(int(args.get("max_results") or 6), 12))
        allow = [d.lower().lstrip(".") for d in (args.get("allowed_domains") or []) if isinstance(d, str)]
        block = [d.lower().lstrip(".") for d in (args.get("blocked_domains") or []) if isinstance(d, str)]
        if allow:
            q += " " + " OR ".join("site:" + d for d in allow)
        res, engine = web_search(q, n, ctx)
        keep, seen = [], set()
        for r in res:
            host = (urllib.parse.urlsplit(r["url"]).hostname or "").lower()
            if not r["url"].startswith("http") or r["url"] in seen:
                continue
            if block and any(host == d or host.endswith("." + d) for d in block):
                continue
            if allow and not any(host == d or host.endswith("." + d) for d in allow):
                continue
            seen.add(r["url"])
            keep.append(r)
            if len(keep) >= n:
                break
        if not keep:
            return Result("No results for %r." % q, summary="No results")
        text = "\n\n".join("%d. %s\n   %s\n   %s" % (i, r["title"], r["url"], r["snippet"]) for i, r in enumerate(keep, 1))
        return Result("Search results for %r (via %s):\n\n%s" % (q, engine, text), summary="%d result%s (%s)" % (len(keep), "" if len(keep) == 1 else "s", engine),
                      preview=["%s  %s" % (r["title"][:60], urllib.parse.urlsplit(r["url"]).hostname) for r in keep[:5]])


class Wikipedia(Tool):
    name = "Wikipedia"
    kind = "net"
    description = "Wikipedia article introduction (full=true for the whole article)."
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "Topic or article title"},
        "lang": {"type": "string", "description": "Language code (default en)"},
        "full": {"type": "boolean", "description": "Return the whole article (long) instead of the introduction"}}, "required": ["query"]}

    def specifier(self, args, ctx):
        return args.get("query", "")

    def run(self, args, ctx):
        q = need(args, "query").strip()
        lang = re.sub(r"[^a-z-]", "", str(args.get("lang") or "en").lower())[:8] or "en"
        base = "https://%s.wikipedia.org/w/api.php?" % lang
        status, h, body, _ = http_fetch(base + urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": 5}), timeout=_timeout(ctx), allow_private=True)
        hits = json.loads(body).get("query", {}).get("search", [])
        if not hits:
            return Result("Wikipedia has nothing for %r." % q, summary="Nothing found")
        title = hits[0]["title"]
        params = {"action": "query", "prop": "extracts|info", "explaintext": 1, "titles": title, "format": "json", "inprop": "url", "redirects": 1}
        if not args.get("full"):
            params["exintro"] = 1
        status, h, body, _ = http_fetch(base + urllib.parse.urlencode(params), timeout=_timeout(ctx), allow_private=True)
        pages = json.loads(body).get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        text = (page.get("extract") or "").strip()
        other = ", ".join(x["title"] for x in hits[1:4])
        out = "%s\n%s\n\n%s" % (title, page.get("fullurl", ""), util.truncate_middle(text, 12000))
        if other:
            out += "\n\nOther matches: " + other
        return Result(out, summary="Wikipedia: %s" % title, preview=[text[:120]])


class Arxiv(Tool):
    name = "Arxiv"
    kind = "net"
    description = "Search arXiv for research papers."
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "Search terms, e.g. 'speculative decoding' or 'au:Hinton'"},
        "max_results": {"type": "integer", "description": "How many papers (default 5, max 10)"}}, "required": ["query"]}

    def specifier(self, args, ctx):
        return args.get("query", "")

    def run(self, args, ctx):
        q = need(args, "query").strip()
        n = max(1, min(int(args.get("max_results") or 5), 10))
        search = q if re.match(r"^(all|ti|au|abs|cat):", q) else "all:" + q
        status, h, body, _ = http_fetch("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"search_query": search, "start": 0, "max_results": n, "sortBy": "relevance"}), timeout=_timeout(ctx), allow_private=True)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise ToolError("arXiv sent something unreadable.")
        out = []
        for e in root.findall("a:entry", ns):
            g = lambda t: re.sub(r"\s+", " ", (e.findtext("a:" + t, "", ns) or "")).strip()
            authors = ", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)[:5])
            link = next((l.get("href") for l in e.findall("a:link", ns) if l.get("type") == "application/pdf"), g("id"))
            out.append("%s\n  %s · %s\n  %s\n  %s\n  %s" % (g("title"), authors, g("published")[:10], g("id"), link, util.truncate_middle(g("summary"), 700)))
        if not out:
            return Result("No papers found for %r." % q, summary="No papers")
        return Result("\n\n".join(out), summary="%d paper%s" % (len(out), "" if len(out) == 1 else "s"))


WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
       80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
       99: "severe thunderstorm with hail"}


class Weather(Tool):
    name = "Weather"
    kind = "net"
    description = "Current weather and 3-day forecast for a place."
    parameters = {"type": "object", "properties": {"location": {"type": "string", "description": "City or place, e.g. 'Leeds' or 'Paris, France'"}}, "required": ["location"]}

    def specifier(self, args, ctx):
        return args.get("location", "")

    def run(self, args, ctx):
        loc = need(args, "location").strip()
        status, h, body, _ = http_fetch("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": loc.split(",")[0], "count": 3, "format": "json"}),
                                        timeout=_timeout(ctx), allow_private=True)
        res = json.loads(body).get("results") or []
        if not res:
            raise ToolError("Couldn't find a place called %r." % loc)
        pick = res[0]
        if "," in loc:
            want = loc.split(",", 1)[1].strip().lower()
            pick = next((r for r in res if want in (r.get("country", "") + " " + r.get("admin1", "")).lower()), res[0])
        status, h, body, _ = http_fetch("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": pick["latitude"], "longitude": pick["longitude"], "timezone": "auto", "forecast_days": 3,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,precipitation,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"}), timeout=_timeout(ctx), allow_private=True)
        j = json.loads(body)
        c, d = j.get("current", {}), j.get("daily", {})
        lines = ["%s, %s%s" % (pick["name"], pick.get("admin1", ""), (", " + pick["country"]) if pick.get("country") else ""),
                 "Now: %s, %s°C (feels like %s°C), humidity %s%%, wind %s km/h, rain %s mm" % (
                     WMO.get(c.get("weather_code"), "?"), c.get("temperature_2m"), c.get("apparent_temperature"), c.get("relative_humidity_2m"),
                     c.get("wind_speed_10m"), c.get("precipitation"))]
        for i, day in enumerate(d.get("time", [])):
            lines.append("%s: %s, %s to %s°C, rain %s mm" % (day, WMO.get(d["weather_code"][i], "?"), d["temperature_2m_min"][i], d["temperature_2m_max"][i],
                                                              d["precipitation_sum"][i]))
        return Result("\n".join(lines), summary="Weather for %s" % pick["name"], preview=lines[1:2])


WEB_TOOLS = [WebSearch, WebFetch, Wikipedia, Arxiv, Weather]
_ = (base64, subprocess)
