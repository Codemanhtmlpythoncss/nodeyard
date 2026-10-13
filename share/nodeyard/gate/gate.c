/* nodeyard model gate: your own network uses the model without its API key; everyone else needs the key.
 *
 * The C version of gate.py (same rules, same answers) so every node pays a few hundred KB for it instead of a Python
 * interpreter (~20 MB) and a Python image. It sits in front of the model server (llama.cpp's llama-server, which only
 * knows "key or no key") and decides by the address a request really comes from; it never trusts X-Forwarded-For or
 * any other header for that.
 *
 * Environment:
 *   PORT         port to listen on (default 31435), IPv4 and IPv6
 *   UPSTREAM     the model server, e.g. http://llama.ai-split.svc.cluster.local:8080
 *   API_KEY      the model's API key (optional; without one, only trusted addresses are served)
 *   TRUSTED      comma-separated networks that need no key (default: loopback, private ranges, Tailscale)
 *   PUBLIC_PORT  optional second port, on 127.0.0.1 only, where EVERY request needs the key (what Tailscale Funnel,
 *                `nodeyard public on`, points at: Funnel delivers internet traffic from 127.0.0.1)
 *
 * One request per connection (answers say Connection: close); answers stream through as the model writes them, and
 * a caller that leaves closes the model's connection, which stops its work. Static, no libraries (see build.sh).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#define DEFAULT_TRUSTED "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,::1/128,fc00::/7,fd7a:115c:a1e0::/48"
#define MAX_HEAD 65536
#define MAX_HEADERS 100
#define MAX_BODY (32L * 1024 * 1024)
#define WINDOW 300.0          /* failed attempts per address per window before it is told to wait */
#define LIMIT 12
#define PUBLIC_LIMIT 60       /* failed attempts on the public port, from anywhere, per window */
#define MAX_CONNS 128

static const char *UNAUTHORIZED = "{\"error\":{\"message\":\"Invalid API Key\",\"type\":\"authentication_error\",\"code\":401}}";
/* request headers never passed on (hop-by-hop, framing, and the caller's own credentials) */
static const char *HOP_REQ[] = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
                                "upgrade", "host", "content-length", "authorization", "x-api-key", "expect", NULL};
/* answer headers never passed back (the framing ones are: the bytes are copied as they come) */
static const char *HOP_RESP[] = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade", NULL};

typedef struct { int fam; unsigned char a[16]; int bits; } Net;
static Net g_nets[64];
static int g_nnets;
static char g_key[1024], g_up_host[256], g_up_port[8];
static int g_active;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

/* ------------------------------------------------------------------ networks */

static int parse_net(const char *s, Net *n) {
    char buf[96];
    snprintf(buf, sizeof buf, "%s", s);
    char *slash = strchr(buf, '/');
    int bits = -1;
    if (slash) { *slash = 0; char *e; bits = (int)strtol(slash + 1, &e, 10); if (*e || e == slash + 1) return -1; }
    memset(n, 0, sizeof *n);
    if (inet_pton(AF_INET, buf, n->a) == 1) { n->fam = 4; n->bits = bits < 0 ? 32 : bits; return n->bits > 32 ? -1 : 0; }
    if (inet_pton(AF_INET6, buf, n->a) == 1) { n->fam = 6; n->bits = bits < 0 ? 128 : bits; return n->bits > 128 ? -1 : 0; }
    return -1;
}
static int in_net(int fam, const unsigned char *a, const Net *n) {
    if (fam != n->fam) return 0;
    int full = n->bits / 8, rest = n->bits % 8;
    if (memcmp(a, n->a, (size_t)full)) return 0;
    if (rest) { unsigned char m = (unsigned char)(0xff << (8 - rest)); if ((a[full] & m) != (n->a[full] & m)) return 0; }
    return 1;
}
/* the caller's address: family 4 or 6, its bytes and text (IPv4-mapped IPv6 counts as IPv4) */
static void peer(const struct sockaddr_storage *ss, int *fam, unsigned char *a, char *txt, size_t cap) {
    memset(a, 0, 16);
    if (ss->ss_family == AF_INET) {
        const struct sockaddr_in *s4 = (const struct sockaddr_in *)ss;
        *fam = 4;
        memcpy(a, &s4->sin_addr, 4);
    } else {
        const struct sockaddr_in6 *s6 = (const struct sockaddr_in6 *)ss;
        static const unsigned char mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};
        if (!memcmp(&s6->sin6_addr, mapped, 12)) { *fam = 4; memcpy(a, (const unsigned char *)&s6->sin6_addr + 12, 4); }
        else { *fam = 6; memcpy(a, &s6->sin6_addr, 16); }
    }
    inet_ntop(*fam == 4 ? AF_INET : AF_INET6, a, txt, (socklen_t)cap);
}

/* ------------------------------------------------------------------ wrong keys */

typedef struct { char who[96]; double t[64]; int n; double last; } Fail;
static Fail g_fails[512];

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (double)t.tv_sec + t.tv_nsec / 1e9; }
static Fail *fail_slot(const char *who, int add) {
    Fail *free_one = NULL, *oldest = NULL;
    for (size_t i = 0; i < sizeof g_fails / sizeof *g_fails; i++) {
        Fail *f = &g_fails[i];
        if (f->who[0] && !strcmp(f->who, who)) return f;
        if (!f->who[0] && !free_one) free_one = f;
        if (f->who[0] && (!oldest || f->last < oldest->last)) oldest = f;
    }
    if (!add) return NULL;
    Fail *f = free_one ? free_one : oldest;
    memset(f, 0, sizeof *f);
    snprintf(f->who, sizeof f->who, "%s", who);
    return f;
}
static void prune(Fail *f, double now) {
    int k = 0;
    for (int i = 0; i < f->n; i++) if (now - f->t[i] < WINDOW) f->t[k++] = f->t[i];
    f->n = k;
}
static int locked_out(const char *who, int limit) {
    pthread_mutex_lock(&g_lock);
    Fail *f = fail_slot(who, 0);
    int out = 0;
    if (f) { prune(f, now_s()); out = f->n >= limit; }
    pthread_mutex_unlock(&g_lock);
    return out;
}
static void failed(const char *who) {
    pthread_mutex_lock(&g_lock);
    Fail *f = fail_slot(who, 1);
    double now = now_s();
    prune(f, now);
    if (f->n == 64) { memmove(f->t, f->t + 1, sizeof(double) * 63); f->n = 63; }
    f->t[f->n++] = now;
    f->last = now;
    pthread_mutex_unlock(&g_lock);
}
static int key_ok(const char *got) {
    size_t a = strlen(got), b = strlen(g_key);
    unsigned char diff = (unsigned char)(a != b);
    for (size_t i = 0; i < b; i++) diff |= (unsigned char)(g_key[i] ^ (i < a ? got[i] : 0));
    return !diff;
}

/* ------------------------------------------------------------------ I/O */

static int send_all(int fd, const char *p, size_t n) {
    while (n) {
        ssize_t w = send(fd, p, n, MSG_NOSIGNAL);
        if (w < 0) { if (errno == EINTR) continue; return -1; }
        p += w;
        n -= (size_t)w;
    }
    return 0;
}
static void reply(int fd, int code, const char *ctype, const char *body, const char *extra) {
    const char *txt = code == 200 ? "OK" : code == 400 ? "Bad Request" : code == 401 ? "Unauthorized" : code == 403 ? "Forbidden"
                    : code == 413 ? "Payload Too Large" : code == 429 ? "Too Many Requests" : code == 502 ? "Bad Gateway" : "Service Unavailable";
    char h[512];
    int n = snprintf(h, sizeof h, "HTTP/1.1 %d %s\r\nServer: nodeyard-gate\r\nContent-Type: %s\r\nContent-Length: %zu\r\nConnection: close\r\n%s\r\n",
                     code, txt, ctype, strlen(body), extra ? extra : "");
    if (send_all(fd, h, (size_t)n) == 0) send_all(fd, body, strlen(body));
}
static void say(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fflush(stderr);
}

typedef struct { char *name, *value; } Header;
typedef struct {
    char head[MAX_HEAD + 1];
    size_t head_len, have;      /* bytes of head, bytes read in all (the rest is the start of the body) */
    char method[16], target[8192];
    Header h[MAX_HEADERS];
    int nh;
} Req;

static const char *hget(const Req *r, const char *name) {
    for (int i = 0; i < r->nh; i++) if (!strcasecmp(r->h[i].name, name)) return r->h[i].value;
    return NULL;
}
static int in_list(const char *name, const char **list) {
    for (int i = 0; list[i]; i++) if (!strcasecmp(name, list[i])) return 1;
    return 0;
}
static char *trim(char *s) {
    while (*s == ' ' || *s == '\t') s++;
    size_t n = strlen(s);
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t')) s[--n] = 0;
    return s;
}

/* reads and parses the request line and headers; 0, or an HTTP status to answer with */
static int read_head(int fd, Req *r) {
    char *end = NULL;
    r->have = 0;
    while (!end) {
        if (r->have >= MAX_HEAD) return 400;
        ssize_t n = recv(fd, r->head + r->have, MAX_HEAD - r->have, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        r->have += (size_t)n;
        r->head[r->have] = 0;
        end = memmem(r->head, r->have, "\r\n\r\n", 4);
    }
    r->head_len = (size_t)(end - r->head) + 4;
    *end = 0;
    char *line = r->head, *nl = strstr(line, "\r\n");
    if (nl) *nl = 0;
    /* METHOD SP /target SP HTTP/1.x */
    char *sp1 = strchr(line, ' '), *sp2 = sp1 ? strchr(sp1 + 1, ' ') : NULL;
    if (!sp1 || !sp2 || sp1 - line >= (long)sizeof r->method || sp2 - sp1 - 1 >= (long)sizeof r->target || strncmp(sp2 + 1, "HTTP/1.", 7)) return 400;
    memcpy(r->method, line, (size_t)(sp1 - line));
    r->method[sp1 - line] = 0;
    for (char *c = r->method; *c; c++) if (!isupper((unsigned char)*c)) return 400;
    memcpy(r->target, sp1 + 1, (size_t)(sp2 - sp1 - 1));
    r->target[sp2 - sp1 - 1] = 0;
    if (r->target[0] != '/') return 400;
    for (char *c = r->target; *c; c++) if ((unsigned char)*c <= ' ' || *c == 0x7f) return 400;
    r->nh = 0;
    for (char *h = nl ? nl + 2 : NULL; h && *h;) {
        char *next = strstr(h, "\r\n");
        if (next) *next = 0;
        char *colon = strchr(h, ':');
        if (!colon || colon == h || r->nh >= MAX_HEADERS) return 400;
        *colon = 0;
        for (char *c = h; *c; c++) if (!(isalnum((unsigned char)*c) || strchr("!#$%&'*+-.^_`|~", *c))) return 400;
        char *v = trim(colon + 1);
        for (char *c = v; *c; c++) if (*c == '\r' || *c == '\n') return 400;
        r->h[r->nh].name = h;
        r->h[r->nh].value = v;
        r->nh++;
        h = next ? next + 2 : NULL;
    }
    return 0;
}

/* growable buffer for the request body */
typedef struct { char *p; size_t n, cap; } Buf;
static int b_put(Buf *b, const char *s, size_t n) {
    if (b->n + n > (size_t)MAX_BODY) return -1;
    if (b->n + n > b->cap) {
        size_t cap = b->cap ? b->cap : 8192;
        while (cap < b->n + n) cap *= 2;
        char *p = realloc(b->p, cap);
        if (!p) return -1;
        b->p = p;
        b->cap = cap;
    }
    memcpy(b->p + b->n, s, n);
    b->n += n;
    return 0;
}

/* the rest of the request, already read bytes first: one byte at a time is never needed */
typedef struct { int fd; const char *pre; size_t pre_n, pre_off; } Src;
static ssize_t src_read(Src *s, char *dst, size_t n) {
    if (s->pre_off < s->pre_n) {
        size_t k = s->pre_n - s->pre_off < n ? s->pre_n - s->pre_off : n;
        memcpy(dst, s->pre + s->pre_off, k);
        s->pre_off += k;
        return (ssize_t)k;
    }
    for (;;) {
        ssize_t r = recv(s->fd, dst, n, 0);
        if (r < 0 && errno == EINTR) continue;
        return r;
    }
}
static int src_line(Src *s, char *line, size_t cap) {
    size_t n = 0;
    for (;;) {
        char c;
        if (src_read(s, &c, 1) != 1) return -1;
        if (c == '\n') break;
        if (n + 1 >= cap) return -1;
        line[n++] = c;
    }
    if (n && line[n - 1] == '\r') n--;
    line[n] = 0;
    return 0;
}
static int src_exact(Src *s, Buf *b, size_t n) {
    char tmp[16384];
    while (n) {
        ssize_t r = src_read(s, tmp, n < sizeof tmp ? n : sizeof tmp);
        if (r <= 0 || b_put(b, tmp, (size_t)r)) return -1;
        n -= (size_t)r;
    }
    return 0;
}
/* 0, or an HTTP status (400 bad framing, 413 too large) */
static int read_body(int fd, Req *r, Buf *body) {
    Src s = {fd, r->head + r->head_len, r->have - r->head_len, 0};
    const char *te = hget(r, "Transfer-Encoding"), *cl = hget(r, "Content-Length");
    if (te && strcasestr(te, "chunked")) {
        char line[256];
        for (;;) {
            if (src_line(&s, line, sizeof line)) return 400;
            char *e;
            long size = strtol(line, &e, 16);
            if (e == line || size < 0) return 400;
            if (size == 0) {
                do { if (src_line(&s, line, sizeof line)) return 400; } while (line[0]);   /* (trailers, then the blank line) */
                return 0;
            }
            if (body->n + (size_t)size > (size_t)MAX_BODY) return 413;
            if (src_exact(&s, body, (size_t)size)) return 400;
            if (src_line(&s, line, sizeof line) || line[0]) return 400;
        }
    }
    if (!cl) return 0;
    char *e;
    long long n = strtoll(cl, &e, 10);
    if (e == cl || *e || n < 0) return 400;
    if (n > MAX_BODY) return 413;
    return src_exact(&s, body, (size_t)n) ? 400 : 0;
}

/* a TCP connection to the model server, connecting within 10 s */
static int upstream(void) {
    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(g_up_host, g_up_port, &hints, &res) || !res) return -1;
    int fd = -1;
    for (struct addrinfo *ai = res; ai && fd < 0; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
        if (fd < 0) continue;
        if (connect(fd, ai->ai_addr, ai->ai_addrlen) && errno != EINPROGRESS) { close(fd); fd = -1; continue; }
        struct pollfd p = {fd, POLLOUT, 0};
        int err = 0;
        socklen_t el = sizeof err;
        if (poll(&p, 1, 10000) != 1 || getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &el) || err) { close(fd); fd = -1; continue; }
        fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) & ~O_NONBLOCK);
    }
    freeaddrinfo(res);
    if (fd >= 0) {
        struct timeval tv = {900, 0};       /* answering can take minutes (a long prompt on slow machines) */
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        int one = 1;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    }
    return fd;
}

/* ------------------------------------------------------------------ one request */

typedef struct { int fd, public; struct sockaddr_storage ss; } Conn;

static void handle(int fd, int public, const struct sockaddr_storage *ss) {
    int fam;
    unsigned char a[16];
    char ip[64];
    peer(ss, &fam, a, ip, sizeof ip);
    struct timeval tv = {30, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    struct timeval tv2 = {120, 0};
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv2, sizeof tv2);
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);

    Req *r = malloc(sizeof *r);
    if (!r) return;
    int bad = read_head(fd, r);
    if (bad) {
        if (bad > 0) reply(fd, 400, "application/json", "{\"error\":{\"message\":\"Bad request.\",\"code\":400}}", NULL);
        free(r);
        return;
    }
    char path[8192];
    snprintf(path, sizeof path, "%s", r->target);
    char *q = strchr(path, '?');
    if (q) *q = 0;
    if (!strcmp(path, "/gate-health")) { reply(fd, 200, "text/plain", "ok", NULL); free(r); return; }

    /* who may use it */
    const char *verdict = "ok", *why = "";
    if (strcmp(path, "/health")) {
        char who[96];
        int trusted = 0;
        snprintf(who, sizeof who, "%s", ip);
        if (public) {
            /* from the internet (through Tailscale Funnel): the key is always needed. The forwarded address only
               decides whose failures are counted, never trust. */
            const char *xff = hget(r, "X-Forwarded-For");
            char first[64] = "";
            if (xff) { snprintf(first, sizeof first, "%s", xff); char *c = strchr(first, ','); if (c) *c = 0; }
            snprintf(who, sizeof who, "public:%s", trim(first)[0] ? trim(first) : ip);
            if (locked_out("public:*", PUBLIC_LIMIT)) { verdict = "wait"; why = "too many wrong keys from the internet"; }
        } else {
            for (int i = 0; i < g_nnets && !trusted; i++) trusted = in_net(fam, a, &g_nets[i]);
        }
        if (!strcmp(verdict, "ok") && !trusted) {
            if (!g_key[0]) { verdict = "forbidden"; why = "no API key is set, so only your own network can use this"; }
            else if (locked_out(who, LIMIT)) { verdict = "wait"; why = "too many wrong keys"; }
            else {
                const char *auth = hget(r, "Authorization"), *xk = hget(r, "X-Api-Key");
                char got[1024] = "";
                if (auth && !strncasecmp(auth, "bearer ", 7)) snprintf(got, sizeof got, "%s", auth + 7);
                else if (xk) snprintf(got, sizeof got, "%s", xk);
                if (!key_ok(trim(got))) {
                    failed(who);
                    if (public) failed("public:*");
                    verdict = "unauthorized";
                    why = "wrong or missing key";
                }
            }
        }
    }
    if (strcmp(verdict, "ok")) {
        say("gate: %s %s from %s: %s\n", verdict, r->method, ip, why);
        if (!strcmp(verdict, "wait"))
            reply(fd, 429, "application/json", "{\"error\":{\"message\":\"Too many wrong keys. Try again in a few minutes.\",\"type\":\"rate_limit\",\"code\":429}}", "Retry-After: 300\r\n");
        else if (!strcmp(verdict, "forbidden"))
            reply(fd, 403, "application/json", "{\"error\":{\"message\":\"This address needs an API key and none is set up.\",\"type\":\"forbidden\",\"code\":403}}", NULL);
        else
            reply(fd, 401, "application/json", UNAUTHORIZED, "WWW-Authenticate: Bearer\r\n");
        free(r);
        return;
    }

    /* the body, then the model server */
    const char *expect = hget(r, "Expect");
    if (expect && !strcasecmp(expect, "100-continue")) send_all(fd, "HTTP/1.1 100 Continue\r\n\r\n", 25);
    tv.tv_sec = 120;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    Buf body = {0};
    int bb = read_body(fd, r, &body);
    if (bb == 413) { reply(fd, 413, "application/json", "{\"error\":{\"message\":\"Request too large.\",\"code\":413}}", NULL); goto done; }
    if (bb) { reply(fd, 400, "application/json", "{\"error\":{\"message\":\"Bad request body.\",\"code\":400}}", NULL); goto done; }
    int up = upstream();
    if (up < 0) { reply(fd, 502, "application/json", "{\"error\":{\"message\":\"The model server isn't answering. It may be loading.\",\"code\":502}}", NULL); goto done; }
    Buf out = {0};
    char line[9000];
    int n = snprintf(line, sizeof line, "%s %s HTTP/1.1\r\nHost: %s:%s\r\n", r->method, r->target, g_up_host, g_up_port);
    int ok = b_put(&out, line, (size_t)n) == 0;
    for (int i = 0; ok && i < r->nh; i++) {
        if (in_list(r->h[i].name, HOP_REQ)) continue;
        ok = !b_put(&out, r->h[i].name, strlen(r->h[i].name)) && !b_put(&out, ": ", 2) && !b_put(&out, r->h[i].value, strlen(r->h[i].value)) && !b_put(&out, "\r\n", 2);
    }
    if (ok && g_key[0]) ok = !b_put(&out, "Authorization: Bearer ", 22) && !b_put(&out, g_key, strlen(g_key)) && !b_put(&out, "\r\n", 2);
    int wants_body = body.n || !strcmp(r->method, "POST") || !strcmp(r->method, "PUT") || !strcmp(r->method, "PATCH");
    if (ok && wants_body) { n = snprintf(line, sizeof line, "Content-Length: %zu\r\n", body.n); ok = !b_put(&out, line, (size_t)n); }
    if (ok) ok = !b_put(&out, "Connection: close\r\n\r\n", 21);
    if (!ok || send_all(up, out.p, out.n) || (body.n && send_all(up, body.p, body.n))) {
        reply(fd, 502, "application/json", "{\"error\":{\"message\":\"The model server isn't answering. It may be loading.\",\"code\":502}}", NULL);
        free(out.p);
        close(up);
        goto done;
    }
    free(out.p);

    /* its answer: the status and headers (minus hop-by-hop ones), then every byte as it comes */
    {
        char *rh = malloc(MAX_HEAD + 1);
        size_t have = 0;
        if (!rh) { close(up); goto done; }
        char *end = NULL;
        while (!end && have < MAX_HEAD) {
            ssize_t k = recv(up, rh + have, MAX_HEAD - have, 0);
            if (k < 0 && errno == EINTR) continue;
            if (k <= 0) break;
            have += (size_t)k;
            rh[have] = 0;
            end = memmem(rh, have, "\r\n\r\n", 4);
        }
        int code = 0;
        if (!end || sscanf(rh, "HTTP/1.%*d %d", &code) != 1 || code < 100 || code > 599) {
            reply(fd, 502, "application/json", "{\"error\":{\"message\":\"The model server isn't answering. It may be loading.\",\"code\":502}}", NULL);
            free(rh);
            close(up);
            goto done;
        }
        size_t head_len = (size_t)(end - rh) + 4;
        *end = 0;
        Buf o = {0};
        char *nl = strstr(rh, "\r\n");
        char *status_end = nl ? nl : rh + strlen(rh);
        b_put(&o, rh, (size_t)(status_end - rh));
        b_put(&o, "\r\n", 2);
        for (char *h = nl ? nl + 2 : NULL; h && *h;) {
            char *next = strstr(h, "\r\n");
            if (next) *next = 0;
            char *colon = strchr(h, ':');
            if (colon) {
                char name[128];
                size_t ln = (size_t)(colon - h) < sizeof name - 1 ? (size_t)(colon - h) : sizeof name - 1;
                memcpy(name, h, ln);
                name[ln] = 0;
                if (!in_list(name, HOP_RESP)) { b_put(&o, h, strlen(h)); b_put(&o, "\r\n", 2); }
            }
            h = next ? next + 2 : NULL;
        }
        b_put(&o, "Connection: close\r\n\r\n", 21);
        int alive = send_all(fd, o.p, o.n) == 0;
        free(o.p);
        int no_body = !strcmp(r->method, "HEAD") || code == 204 || code == 304 || code < 200;
        if (alive && !no_body && have > head_len) alive = send_all(fd, rh + head_len, have - head_len) == 0;
        char buf[16384];
        while (alive && !no_body) {
            ssize_t k = recv(up, buf, sizeof buf, 0);
            if (k < 0 && errno == EINTR) continue;
            if (k <= 0) break;
            alive = send_all(fd, buf, (size_t)k) == 0;      /* the caller left: closing the model's connection stops its work */
        }
        free(rh);
        close(up);
    }
done:
    free(body.p);
    free(r);
}

static void *conn_thread(void *arg) {
    Conn *c = arg;
    handle(c->fd, c->public, &c->ss);
    shutdown(c->fd, SHUT_WR);
    close(c->fd);
    free(c);
    pthread_mutex_lock(&g_lock);
    g_active--;
    pthread_mutex_unlock(&g_lock);
    return NULL;
}

/* ------------------------------------------------------------------ main */

static int listen_on(const char *addr, int port) {
    int one = 1, zero = 0;
    if (!addr) {      /* IPv4 and IPv6 on one socket when the machine has IPv6; plain IPv4 otherwise */
        int fd = socket(AF_INET6, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (fd >= 0) {
            struct sockaddr_in6 s6;
            memset(&s6, 0, sizeof s6);
            s6.sin6_family = AF_INET6;
            s6.sin6_port = htons((uint16_t)port);
            setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
            setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &zero, sizeof zero);
            if (!bind(fd, (struct sockaddr *)&s6, sizeof s6) && !listen(fd, 128)) return fd;
            close(fd);
        }
        addr = "0.0.0.0";
    }
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_in s4;
    memset(&s4, 0, sizeof s4);
    s4.sin_family = AF_INET;
    s4.sin_port = htons((uint16_t)port);
    inet_pton(AF_INET, addr, &s4.sin_addr);
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    if (fd < 0 || bind(fd, (struct sockaddr *)&s4, sizeof s4) || listen(fd, 128)) {
        fprintf(stderr, "gate: can't listen on %s:%d: %s\n", addr, port, strerror(errno));
        exit(1);
    }
    return fd;
}

static void on_term(int sig) { (void)sig; _exit(0); }

int main(void) {
    const char *e;
    int port = (e = getenv("PORT")) && *e ? atoi(e) : 31435;
    int public_port = (e = getenv("PUBLIC_PORT")) && *e ? atoi(e) : 0;
    const char *up = (e = getenv("UPSTREAM")) && *e ? e : "http://llama.ai-split.svc.cluster.local:8080";
    snprintf(g_key, sizeof g_key, "%s", (e = getenv("API_KEY")) ? e : "");
    char *k = g_key;
    while (*k == ' ' || *k == '\n' || *k == '\r' || *k == '\t') k++;
    memmove(g_key, k, strlen(k) + 1);
    for (size_t n = strlen(g_key); n && strchr(" \n\r\t", g_key[n - 1]); n--) g_key[n - 1] = 0;

    if (strncmp(up, "http://", 7)) { fprintf(stderr, "gate: UPSTREAM must be http://HOST:PORT\n"); return 1; }
    char hp[300];
    snprintf(hp, sizeof hp, "%s", up + 7);
    char *slash = strchr(hp, '/');
    if (slash) *slash = 0;
    char *colon = strrchr(hp, ':');
    if (colon && !strchr(colon, ']')) { *colon = 0; snprintf(g_up_port, sizeof g_up_port, "%s", colon + 1); } else snprintf(g_up_port, sizeof g_up_port, "80");
    char *host = hp;
    if (*host == '[') { host++; char *rb = strchr(host, ']'); if (rb) *rb = 0; }
    snprintf(g_up_host, sizeof g_up_host, "%s", host);

    char nets[2048];
    snprintf(nets, sizeof nets, "%s", (e = getenv("TRUSTED")) && *e ? e : DEFAULT_TRUSTED);
    char shown[2048] = "";
    for (char *t = strtok(nets, ","); t; t = strtok(NULL, ",")) {
        char *s = trim(t);
        if (!*s) continue;
        if (g_nnets >= 64 || parse_net(s, &g_nets[g_nnets])) { fprintf(stderr, "gate: bad TRUSTED list: %s\n", s); return 1; }
        g_nnets++;
        strncat(shown, shown[0] ? ", " : "", sizeof shown - strlen(shown) - 1);
        strncat(shown, s, sizeof shown - strlen(shown) - 1);
    }
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);

    struct pollfd lfd[2];
    int nl = 0;
    lfd[nl++] = (struct pollfd){listen_on(NULL, port), POLLIN, 0};
    printf("nodeyard model gate on :%d -> %s (no key needed from: %s)\n", port, up, shown);
    if (public_port) {
        lfd[nl++] = (struct pollfd){listen_on("127.0.0.1", public_port), POLLIN, 0};
        printf("public port 127.0.0.1:%d: the key is always needed (for Tailscale Funnel)\n", public_port);
    }
    fflush(stdout);
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    pthread_attr_setstacksize(&attr, 256 * 1024);
    for (;;) {
        if (poll(lfd, (nfds_t)nl, -1) <= 0) continue;
        for (int i = 0; i < nl; i++) {
            if (!(lfd[i].revents & POLLIN)) continue;
            Conn *c = calloc(1, sizeof *c);
            if (!c) continue;
            socklen_t sl = sizeof c->ss;
            c->fd = accept4(lfd[i].fd, (struct sockaddr *)&c->ss, &sl, SOCK_CLOEXEC);
            c->public = i == 1;
            if (c->fd < 0) { free(c); continue; }
            pthread_mutex_lock(&g_lock);
            int busy = g_active >= MAX_CONNS;
            if (!busy) g_active++;
            pthread_mutex_unlock(&g_lock);
            pthread_t t;
            if (busy || pthread_create(&t, &attr, conn_thread, c)) {
                if (!busy) { pthread_mutex_lock(&g_lock); g_active--; pthread_mutex_unlock(&g_lock); }
                reply(c->fd, 503, "application/json", "{\"error\":{\"message\":\"Too many requests at once.\",\"code\":503}}", NULL);
                close(c->fd);
                free(c);
            }
        }
    }
}
