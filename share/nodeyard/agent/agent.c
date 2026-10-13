/* nodeyard node agent: read-only facts about the machine it runs on.
 *
 * The C version of agent.py: the same answers on the same port, for well under a megabyte of memory per node
 * instead of a Python interpreter (~20 MB) and a Python image on every node's disk. Processes, per-core clock
 * speeds, temperatures, load, a memory breakdown, pressure and OOM counters, read from /proc and /sys (the pod
 * runs with the host's process view). It never changes anything and only answers requests that carry the token.
 *
 *     nodeyard-agent [--port 9093] [--proc /proc] [--sys /sys] [--passwd /etc/passwd]
 *     nodeyard-agent --once             print one sample as JSON (two, a second apart, for the CPU numbers)
 *     nodeyard-agent pageout PID [START]
 *         Move one process's memory out to swap (process_madvise MADV_PAGEOUT). Not used by the agent pod: the
 *         dashboard's Memory tab runs it as root in a short-lived helper pod after you've asked for it. START is the
 *         process's start time in clock ticks (/proc/PID/stat field 22), so a recycled PID is never touched.
 *
 * One thread: the HTTP server waits with poll() between samples, every 2 s. Static, no libraries (see build.sh).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/uio.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define SAMPLE_MS 2000
#define TOP_BY_MEMORY 60
#define TOP_BY_CPU 30
#define BIG_RSS (5LL << 20)        /* every process at least this big gets its details (the Memory tab lists them) */
#define MAX_DETAILED 250
#define HARDWARE_EVERY 600
#define MAX_CPUS 1024

static const char *g_proc = "/proc", *g_sys = "/sys", *g_passwd = "/etc/passwd";
static long g_page = 4096, g_clk = 100;

/* ------------------------------------------------------------------ text */

typedef struct { char *p; size_t n, cap; } Buf;

static void b_grow(Buf *b, size_t add) {
    if (b->n + add + 1 <= b->cap) return;
    size_t cap = b->cap ? b->cap : 4096;
    while (cap < b->n + add + 1) cap *= 2;
    char *p = realloc(b->p, cap);
    if (!p) { fputs("agent: out of memory\n", stderr); exit(1); }
    b->p = p;
    b->cap = cap;
}
static void b_put(Buf *b, const char *s, size_t n) { b_grow(b, n); memcpy(b->p + b->n, s, n); b->n += n; b->p[b->n] = 0; }
static void b_str(Buf *b, const char *s) { b_put(b, s, strlen(s)); }
static void b_fmt(Buf *b, const char *fmt, ...) {
    char tmp[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof tmp, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if ((size_t)n < sizeof tmp) { b_put(b, tmp, (size_t)n); return; }
    b_grow(b, (size_t)n);
    va_start(ap, fmt);
    vsnprintf(b->p + b->n, (size_t)n + 1, fmt, ap);
    va_end(ap);
    b->n += (size_t)n;
}

/* bytes in the valid UTF-8 sequence at s, or 0 */
static size_t utf8_len(const unsigned char *s, size_t n) {
    unsigned c = s[0];
    size_t len = (c >= 0xc2 && c <= 0xdf) ? 2 : (c >= 0xe0 && c <= 0xef) ? 3 : (c >= 0xf0 && c <= 0xf4) ? 4 : 0;
    if (!len || len > n) return 0;
    for (size_t i = 1; i < len; i++)
        if ((s[i] & 0xc0) != 0x80) return 0;
    if (c == 0xe0 && s[1] < 0xa0) return 0;
    if (c == 0xed && s[1] > 0x9f) return 0;
    if (c == 0xf0 && s[1] < 0x90) return 0;
    if (c == 0xf4 && s[1] > 0x8f) return 0;
    return len;
}

/* a JSON string; anything that isn't valid UTF-8 becomes U+FFFD (as Python's errors="replace" did) */
static void j_strn(Buf *b, const char *s, size_t n) {
    const unsigned char *u = (const unsigned char *)s;
    b_put(b, "\"", 1);
    for (size_t i = 0; i < n;) {
        unsigned c = u[i];
        if (c == '"' || c == '\\') { char e[2] = {'\\', (char)c}; b_put(b, e, 2); i++; }
        else if (c < 0x20 || c == 0x7f) { b_fmt(b, "\\u%04x", c); i++; }
        else if (c < 0x80) { b_put(b, s + i, 1); i++; }
        else {
            size_t len = utf8_len(u + i, n - i);
            if (len) { b_put(b, s + i, len); i += len; }
            else { b_str(b, "\xef\xbf\xbd"); i++; }
        }
    }
    b_put(b, "\"", 1);
}
static void j_str(Buf *b, const char *s) { j_strn(b, s, strlen(s)); }
static void j_key(Buf *b, const char *k) { b_put(b, "\"", 1); b_str(b, k); b_put(b, "\":", 2); }
static void j_ll_or_null(Buf *b, long long v, int has) { if (has) b_fmt(b, "%lld", v); else b_str(b, "null"); }

/* ------------------------------------------------------------------ files */

static ssize_t rd(const char *path, char *dst, size_t cap) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) { dst[0] = 0; return -1; }
    size_t n = 0;
    while (n + 1 < cap) {
        ssize_t r = read(fd, dst + n, cap - 1 - n);
        if (r < 0) { if (errno == EINTR) continue; break; }
        if (r == 0) break;
        n += (size_t)r;
    }
    close(fd);
    dst[n] = 0;
    return (ssize_t)n;
}
static ssize_t rdf(char *dst, size_t cap, const char *fmt, ...) {
    char path[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(path, sizeof path, fmt, ap);
    va_end(ap);
    return rd(path, dst, cap);
}
/* a whole file of any size (cpuinfo on big machines) */
static int rd_all(const char *path, Buf *b) {
    b->n = 0;
    b_grow(b, 1);
    b->p[0] = 0;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    for (;;) {
        b_grow(b, 16384);
        ssize_t r = read(fd, b->p + b->n, 16384);
        if (r < 0) { if (errno == EINTR) continue; break; }
        if (r == 0) break;
        b->n += (size_t)r;
    }
    close(fd);
    b->p[b->n] = 0;
    return 0;
}
static char *trim(char *s) {
    while (*s && isspace((unsigned char)*s)) s++;
    size_t n = strlen(s);
    while (n && isspace((unsigned char)s[n - 1])) s[--n] = 0;
    return s;
}
static int is_digits(const char *s) {
    if (!*s) return 0;
    for (; *s; s++)
        if (!isdigit((unsigned char)*s)) return 0;
    return 1;
}
static int is_int(const char *s) { return is_digits(*s == '-' ? s + 1 : s); }
static int exists(const char *path) { struct stat st; return stat(path, &st) == 0; }
static int is_dir(const char *path) { struct stat st; return stat(path, &st) == 0 && S_ISDIR(st.st_mode); }
/* the last path component of where a symlink points, or "" */
static void link_base(const char *path, char *out, size_t cap) {
    char tmp[4096];
    out[0] = 0;
    if (!realpath(path, tmp)) return;
    const char *b = strrchr(tmp, '/');
    snprintf(out, cap, "%s", b ? b + 1 : tmp);
}

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_REALTIME, &t); return (double)t.tv_sec + t.tv_nsec / 1e9; }
static long long mono_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000; }

/* sorted directory names (lexicographic, like Python's sorted(os.listdir())) */
typedef struct { char **v; int n; } Names;
static int cmp_str(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }
static Names list_dir(const char *path) {
    Names r = {0, 0};
    DIR *d = opendir(path);
    if (!d) return r;
    int cap = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        if (r.n == cap) {
            cap = cap ? cap * 2 : 32;
            char **v = realloc(r.v, sizeof(char *) * (size_t)cap);
            if (!v) break;
            r.v = v;
        }
        r.v[r.n++] = strdup(e->d_name);
    }
    closedir(d);
    if (r.n) qsort(r.v, (size_t)r.n, sizeof(char *), cmp_str);
    return r;
}
static void free_names(Names *r) { for (int i = 0; i < r->n; i++) free(r->v[i]); free(r->v); r->v = 0; r->n = 0; }
/* "cpu12" -> 12 when the name is a prefix followed only by digits, else -1 */
static int suffix_num(const char *name, const char *prefix) {
    size_t n = strlen(prefix);
    if (strncmp(name, prefix, n) || !is_digits(name + n)) return -1;
    return atoi(name + n);
}

/* ------------------------------------------------------------------ secrets on command lines */

static int has_secret_word(const char *s, size_t n) {
    static const char *words[] = {"api-key", "api_key", "apikey", "password", "passwd", "token", "secret", "credential", "auth"};
    char low[256];
    if (n >= sizeof low) n = sizeof low - 1;
    for (size_t i = 0; i < n; i++) low[i] = (char)tolower((unsigned char)s[i]);
    low[n] = 0;
    for (size_t w = 0; w < sizeof words / sizeof *words; w++)
        if (strstr(low, words[w])) return 1;
    return 0;
}
static int flag_char(char c) { return isalnum((unsigned char)c) || c == '_' || c == '-'; }
static int blob_char(char c) { return isalnum((unsigned char)c) || c == '+' || c == '/' || c == '_' || c == '=' || c == '-'; }

/* agent.py's redact(): values of --token/--password/... flags and long base64-ish blobs become ***, then 240 chars */
static void redact(const char *in, char *out, size_t cap) {
    Buf a = {0}, z = {0};
    size_t n = strlen(in);
    for (size_t i = 0; i < n;) {
        if (in[i] == '-') {
            size_t j = i;
            while (j < n && flag_char(in[j])) j++;             /* the flag name, dashes included */
            size_t k = j;
            while (k < n && (in[k] == '=' || isspace((unsigned char)in[k]))) k++;
            size_t v = k;
            while (v < n && !isspace((unsigned char)in[v])) v++;
            if (has_secret_word(in + i, j - i) && k > j && v > k) {
                b_put(&a, in + i, k - i);
                b_str(&a, "***");
                i = v;
                continue;
            }
            b_put(&a, in + i, j - i);
            i = j;
            continue;
        }
        b_put(&a, in + i, 1);
        i++;
    }
    for (size_t i = 0; i < a.n;) {
        if (blob_char(a.p[i])) {
            size_t j = i;
            while (j < a.n && blob_char(a.p[j])) j++;
            if (j - i >= 40) b_str(&z, "***"); else b_put(&z, a.p + i, j - i);
            i = j;
        } else { b_put(&z, a.p + i, 1); i++; }
    }
    /* 240 characters, never cutting one in half */
    size_t len = 0, chars = 0;
    while (z.p && len < z.n && chars < 240) {
        size_t l = ((unsigned char)z.p[len] < 0x80) ? 1 : utf8_len((unsigned char *)z.p + len, z.n - len);
        len += l ? l : 1;
        chars++;
    }
    if (len >= cap) len = cap - 1;
    if (z.p) memcpy(out, z.p, len);
    out[len] = 0;
    free(a.p);
    free(z.p);
}

/* ------------------------------------------------------------------ small /proc parsers */

static const char *MEM_KEYS[] = {"MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SwapCached", "Active(file)", "Inactive(file)",
                                 "Active(anon)", "Inactive(anon)", "Dirty", "Shmem", "Slab", "SReclaimable", "SUnreclaim", "Mapped",
                                 "AnonPages", "KernelStack", "PageTables", "SwapTotal", "SwapFree"};
#define N_MEM (sizeof MEM_KEYS / sizeof *MEM_KEYS)
typedef struct { long long v[N_MEM]; int has[N_MEM]; } MemInfo;

static void read_meminfo(MemInfo *m) {
    char buf[8192];
    memset(m, 0, sizeof *m);
    char path[512];
    snprintf(path, sizeof path, "%s/meminfo", g_proc);
    if (rd(path, buf, sizeof buf) <= 0) return;
    for (char *line = strtok(buf, "\n"); line; line = strtok(NULL, "\n")) {
        char *colon = strchr(line, ':');
        if (!colon) continue;
        *colon = 0;
        for (size_t i = 0; i < N_MEM; i++)
            if (!strcmp(line, MEM_KEYS[i])) {
                char *end;
                long long v = strtoll(colon + 1, &end, 10);
                if (end != colon + 1) { m->v[i] = v * 1024; m->has[i] = 1; }
            }
    }
}
static long long mem_get(const MemInfo *m, const char *key) {
    for (size_t i = 0; i < N_MEM; i++)
        if (!strcmp(MEM_KEYS[i], key)) return m->has[i] ? m->v[i] : -1;
    return -1;
}

typedef struct { int valid; unsigned long long busy, total; } CpuTimes;
/* index 0 = "all", 1 + n = cpu n */
static void read_cpu_times(CpuTimes *out, int cap) {
    static char buf[1 << 17];
    memset(out, 0, sizeof(CpuTimes) * (size_t)cap);
    char path[512];
    snprintf(path, sizeof path, "%s/stat", g_proc);
    if (rd(path, buf, sizeof buf) <= 0) return;
    for (char *line = strtok(buf, "\n"); line; line = strtok(NULL, "\n")) {
        if (strncmp(line, "cpu", 3)) continue;
        int idx;
        char *p = line + 3;
        if (*p == ' ') idx = 0;
        else { idx = atoi(p) + 1; while (isdigit((unsigned char)*p)) p++; }
        if (idx >= cap) continue;
        unsigned long long vals[10];
        int nv = 0;
        char *end;
        while (nv < 10) {
            unsigned long long v = strtoull(p, &end, 10);
            if (end == p) break;
            vals[nv++] = v;
            p = end;
        }
        if (nv < 4) continue;
        unsigned long long idle = vals[3] + (nv > 4 ? vals[4] : 0), total = 0;
        int upto = nv >= 8 ? 8 : nv;
        for (int i = 0; i < upto; i++) total += vals[i];
        out[idx].valid = 1;
        out[idx].busy = total - idle;
        out[idx].total = total;
    }
}

/* "some avg10=0.00 ..." lines -> {"some":x,"full":y} */
static void j_pressure(Buf *b, const char *what) {
    char buf[1024];
    rdf(buf, sizeof buf, "%s/pressure/%s", g_proc, what);
    b_put(b, "{", 1);
    int first = 1;
    for (char *line = strtok(buf, "\n"); line; line = strtok(NULL, "\n")) {
        const char *kind = !strncmp(line, "some ", 5) ? "some" : !strncmp(line, "full ", 5) ? "full" : NULL;
        char *a = strstr(line, "avg10=");
        if (!kind || !a) continue;
        if (!first) b_put(b, ",", 1);
        first = 0;
        j_key(b, kind);
        b_fmt(b, "%.2f", strtod(a + 6, NULL));
    }
    b_put(b, "}", 1);
}

typedef struct { char comm[64]; char state; int ppid; unsigned long long ticks, start; long threads; } Stat;
static int parse_stat(const char *text, Stat *s) {
    const char *open = strchr(text, '('), *close = strrchr(text, ')');
    if (!open || !close || close < open) return -1;
    size_t n = (size_t)(close - open - 1);
    if (n >= sizeof s->comm) n = sizeof s->comm - 1;
    memcpy(s->comm, open + 1, n);
    s->comm[n] = 0;
    const char *p = close + 1;
    char *f[24];
    char tmp[1024];
    snprintf(tmp, sizeof tmp, "%s", p);
    int nf = 0;
    for (char *t = strtok(tmp, " \n"); t && nf < 24; t = strtok(NULL, " \n")) f[nf++] = t;
    if (nf < 20) return -1;
    s->state = f[0][0];
    s->ppid = atoi(f[1]);
    s->ticks = strtoull(f[11], NULL, 10) + strtoull(f[12], NULL, 10);
    s->threads = atol(f[17]);
    s->start = strtoull(f[19], NULL, 10);
    return 0;
}

typedef struct { long long uid, rss, anon, file, shmem, swap, threads; int has_uid, has_rss, has_anon, has_file, has_shmem, has_swap, has_threads; } Status;
static void parse_status(char *text, Status *s) {
    memset(s, 0, sizeof *s);
    for (char *line = strtok(text, "\n"); line; line = strtok(NULL, "\n")) {
        char *colon = strchr(line, ':');
        if (!colon) continue;
        *colon = 0;
        char *v = colon + 1, *end;
        long long n = strtoll(v, &end, 10);
        int ok = end != v;
        if (!strcmp(line, "Uid")) { s->uid = n; s->has_uid = ok; }
        else if (!strcmp(line, "VmRSS")) { s->rss = n * 1024; s->has_rss = ok; }
        else if (!strcmp(line, "RssAnon")) { s->anon = n * 1024; s->has_anon = ok; }
        else if (!strcmp(line, "RssFile")) { s->file = n * 1024; s->has_file = ok; }
        else if (!strcmp(line, "RssShmem")) { s->shmem = n * 1024; s->has_shmem = ok; }
        else if (!strcmp(line, "VmSwap")) { s->swap = n * 1024; s->has_swap = ok; }
        else if (!strcmp(line, "Threads")) { s->threads = n; s->has_threads = ok; }
    }
}

/* ------------------------------------------------------------------ where a process lives */

enum { G_KERNEL, G_POD, G_SERVICE, G_USER, G_SYSTEM };
static const char *G_KIND[] = {"kernel", "pod", "service", "user", "system"};
typedef struct { int kind; char uid[40]; char name[128]; char container[16]; } Group;

static int is_hex(char c) { return isdigit((unsigned char)c) || (c >= 'a' && c <= 'f'); }
/* pod<8>[_-]<4>[_-]<4>[_-]<4>[_-]<12> anywhere in the line */
static int find_pod_uid(const char *line, char *out) {
    static const int groups[] = {8, 4, 4, 4, 12};
    for (const char *p = strstr(line, "pod"); p; p = strstr(p + 1, "pod")) {
        const char *q = p + 3;
        char uid[40];
        int o = 0, ok = 1;
        for (int g = 0; g < 5 && ok; g++) {
            for (int i = 0; i < groups[g]; i++, q++)
                if (!is_hex(*q)) { ok = 0; break; } else uid[o++] = *q;
            if (ok && g < 4) {
                if (*q == '_' || *q == '-') { uid[o++] = '-'; q++; } else ok = 0;
            }
        }
        if (ok) { uid[o] = 0; strcpy(out, uid); return 1; }
    }
    return 0;
}
static void find_container(const char *line, char *out) {
    static const char *pre[] = {"cri-containerd-", "docker-", "crio-"};
    out[0] = 0;
    for (size_t i = 0; i < 3; i++)
        for (const char *p = strstr(line, pre[i]); p; p = strstr(p + 1, pre[i])) {
            const char *q = p + strlen(pre[i]);
            int n = 0;
            while (n < 64 && is_hex(q[n])) n++;
            if (n == 64) { memcpy(out, q, 12); out[12] = 0; return; }
        }
}
static void parse_group(char *cg, int pid, int ppid, Group *g) {
    memset(g, 0, sizeof *g);
    if (pid == 2 || ppid == 2) { g->kind = G_KERNEL; strcpy(g->name, "kernel threads"); return; }
    char copy[4096];
    snprintf(copy, sizeof copy, "%s", cg);
    for (char *line = strtok(copy, "\n"); line; line = strtok(NULL, "\n"))
        if (find_pod_uid(line, g->uid)) { g->kind = G_POD; find_container(line, g->container); return; }
    snprintf(copy, sizeof copy, "%s", cg);
    for (char *line = strtok(copy, "\n"); line; line = strtok(NULL, "\n")) {
        /* the first path segment that ends in .service */
        for (char *seg = strchr(line, '/'); seg; seg = strchr(seg + 1, '/')) {
            char *end = strchr(seg + 1, '/');
            size_t len = end ? (size_t)(end - seg - 1) : strlen(seg + 1);
            if (len > 8 && !strncmp(seg + 1 + len - 8, ".service", 8)) {
                if (len >= sizeof g->name) len = sizeof g->name - 1;
                memcpy(g->name, seg + 1, len);
                g->name[len] = 0;
                g->kind = G_SERVICE;
                return;
            }
        }
        char *u = strstr(line, "/user-");
        if (u) {
            char *e;
            long uid = strtol(u + 6, &e, 10);
            if (e != u + 6 && !strncmp(e, ".slice", 6)) {
                g->kind = G_USER;
                snprintf(g->name, sizeof g->name, "user session (uid %ld)", uid);
                return;
            }
        }
    }
    g->kind = G_SYSTEM;
    strcpy(g->name, "system");
}
static void j_group(Buf *b, const Group *g) {
    b_str(b, "{\"kind\":");
    j_str(b, G_KIND[g->kind]);
    if (g->kind == G_POD) {
        b_str(b, ",\"uid\":");
        j_str(b, g->uid);
        b_str(b, ",\"container\":");
        j_str(b, g->container);
    } else {
        b_str(b, ",\"name\":");
        j_str(b, g->name);
    }
    b_put(b, "}", 1);
}

/* ------------------------------------------------------------------ users */

typedef struct { long uid; char name[64]; } User;
static User *g_users;
static int g_nusers;
static void read_users(void) {
    Buf b = {0};
    if (rd_all(g_passwd, &b) < 0) { free(b.p); return; }
    for (char *line = strtok(b.p, "\n"); line; line = strtok(NULL, "\n")) {
        char *f[4] = {0};
        int nf = 0;
        char *s = line;
        while (nf < 4) { f[nf++] = s; s = strchr(s, ':'); if (!s) break; *s++ = 0; }
        if (nf < 3 || !is_digits(f[2])) continue;
        User *u = realloc(g_users, sizeof(User) * (size_t)(g_nusers + 1));
        if (!u) break;
        g_users = u;
        g_users[g_nusers].uid = atol(f[2]);
        snprintf(g_users[g_nusers].name, sizeof g_users[g_nusers].name, "%s", f[0]);
        g_nusers++;
    }
    free(b.p);
}
static const char *user_name(long uid) {
    for (int i = 0; i < g_nusers; i++)
        if (g_users[i].uid == uid) return g_users[i].name;
    return NULL;
}

/* ------------------------------------------------------------------ hardware (read every 10 minutes) */

static const char *FLAGS_OF_NOTE[] = {"sse4_2", "avx", "avx2", "fma", "f16c", "avx512f", "avx512_vnni", "avx_vnni", "amx_tile", "aes", "sha_ni",
                                      "asimd", "neon", "asimddp", "sve", "sve2", "i8mm", "bf16", "fp16", "asimdhp"};
static const struct { const char *id, *name; } ARM_IMPL[] = {{"0x41", "ARM"}, {"0x61", "Apple"}, {"0x51", "Qualcomm"}, {"0x48", "HiSilicon"}};
static const struct { const char *id, *name; } ARM_PARTS[] = {
    {"0xd03", "Cortex-A53"}, {"0xd04", "Cortex-A35"}, {"0xd05", "Cortex-A55"}, {"0xd07", "Cortex-A57"}, {"0xd08", "Cortex-A72"},
    {"0xd09", "Cortex-A73"}, {"0xd0a", "Cortex-A75"}, {"0xd0b", "Cortex-A76"}, {"0xd0d", "Cortex-A77"}, {"0xd41", "Cortex-A78"},
    {"0xd44", "Cortex-X1"}, {"0xd46", "Cortex-A510"}, {"0xd47", "Cortex-A710"}, {"0xd48", "Cortex-X2"}, {"0xd0c", "Neoverse-N1"},
    {"0xd40", "Neoverse-V1"}, {"0xd49", "Neoverse-N2"}, {"0xc07", "Cortex-A7"}, {"0xc0f", "Cortex-A15"}};

static char g_cpu_model[256];

/* "model name" (x86) or "Model" (Pi) or "Hardware", from cpuinfo */
static void cpu_model_of(const char *cpuinfo, char *out, size_t cap) {
    char model[256] = "", hardware[256] = "";
    const char *p = cpuinfo;
    while (*p) {
        const char *nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        char line[1024];
        if (len >= sizeof line) len = sizeof line - 1;
        memcpy(line, p, len);
        line[len] = 0;
        char *colon = strchr(line, ':');
        if (colon) {
            *colon = 0;
            char *k = trim(line), *v = trim(colon + 1);
            if (!strcmp(k, "model name") && !model[0]) snprintf(model, sizeof model, "%s", v);
            else if (!strcmp(k, "Model") && !model[0]) snprintf(model, sizeof model, "%s", v);
            else if (!strcmp(k, "Hardware") && !hardware[0]) snprintf(hardware, sizeof hardware, "%s", v);
        }
        p += len + (nl ? 1 : 0);
        if (!nl) break;
    }
    snprintf(out, cap, "%s", model[0] ? model : hardware);
}

#define SIOCETHTOOL_ 0x8946
/* the fastest speed the card supports (ETHTOOL_GSET, no privileges needed), or -1 */
static int iface_max_mbps(const char *name) {
    unsigned char data[44] = {0};
    uint32_t cmd = 1;
    memcpy(data, &cmd, 4);
    struct ifreq ifr;
    memset(&ifr, 0, sizeof ifr);
    snprintf(ifr.ifr_name, sizeof ifr.ifr_name, "%s", name);
    ifr.ifr_data = (char *)data;
    int s = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (s < 0) return -1;
    int r = ioctl(s, SIOCETHTOOL_, &ifr);
    close(s);
    if (r < 0) return -1;
    uint32_t sup;
    memcpy(&sup, data + 4, 4);
    if (!sup) return -1;
    static const struct { uint32_t bit; int mbps; } m[] = {{1u << 12, 10000}, {1u << 15, 2500}, {1u << 5, 1000}, {1u << 4, 1000},
                                                           {1u << 3, 100}, {1u << 2, 100}, {1u << 1, 10}, {1u << 0, 10}};
    for (size_t i = 0; i < sizeof m / sizeof *m; i++)
        if (sup & m[i].bit) return m[i].mbps;
    return -1;
}
static void iface_ipv4(const char *name, char *out, size_t cap) {
    out[0] = 0;
    struct ifreq ifr;
    memset(&ifr, 0, sizeof ifr);
    snprintf(ifr.ifr_name, sizeof ifr.ifr_name, "%s", name);
    int s = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (s < 0) return;
    if (ioctl(s, SIOCGIFADDR, &ifr) == 0) {
        struct sockaddr_in *sin = (struct sockaddr_in *)&ifr.ifr_addr;
        inet_ntop(AF_INET, &sin->sin_addr, out, (socklen_t)cap);
    }
    close(s);
}
static long long size_text(const char *t) {
    char *end;
    while (isspace((unsigned char)*t)) t++;
    long long n = strtoll(t, &end, 10);
    if (end == t) return -1;
    while (*end == ' ') end++;
    return n * (*end == 'K' ? 1024LL : *end == 'M' ? 1048576LL : *end == 'G' ? 1073741824LL : 1);
}
static int starts_any(const char *s, const char *const *pre, size_t n) {
    for (size_t i = 0; i < n; i++)
        if (!strncmp(s, pre[i], strlen(pre[i]))) return 1;
    return 0;
}

static void j_hardware(Buf *b) {
    char buf[4096], path[1024];
    Buf ci = {0};
    snprintf(path, sizeof path, "%s/cpuinfo", g_proc);
    rd_all(path, &ci);
    char vendor[128] = "", impl[32] = "", part[32] = "", phys[32] = "";
    double bogo = -1;
    char **cores = NULL;
    int ncores = 0;
    unsigned flags_seen = 0;
    for (char *line = strtok(ci.p ? ci.p : (char *)"", "\n"); line; line = strtok(NULL, "\n")) {
        char *colon = strchr(line, ':');
        if (!colon) continue;
        *colon = 0;
        char *k = trim(line), *v = trim(colon + 1);
        if (!strcmp(k, "flags") || !strcmp(k, "Features")) {
            for (char *f = strtok_r(v, " ", &v); f; f = strtok_r(NULL, " ", &v))
                for (size_t i = 0; i < sizeof FLAGS_OF_NOTE / sizeof *FLAGS_OF_NOTE; i++)
                    if (!strcmp(f, FLAGS_OF_NOTE[i])) flags_seen |= 1u << i;
        } else if (!strcmp(k, "vendor_id") && !vendor[0]) snprintf(vendor, sizeof vendor, "%s", v);
        else if (!strcmp(k, "CPU implementer") && !impl[0]) snprintf(impl, sizeof impl, "%s", v);
        else if (!strcmp(k, "CPU part") && !part[0]) snprintf(part, sizeof part, "%s", v);
        else if (!strcasecmp(k, "bogomips") && bogo < 0) { char *e; double d = strtod(v, &e); if (e != v) bogo = d; }
        else if (!strcmp(k, "physical id")) snprintf(phys, sizeof phys, "%s", v);
        else if (!strcmp(k, "core id")) {
            char key[96];
            snprintf(key, sizeof key, "%s/%s", phys, v);
            int seen = 0;
            for (int i = 0; i < ncores && !seen; i++) seen = !strcmp(cores[i], key);
            if (!seen) { char **c = realloc(cores, sizeof(char *) * (size_t)(ncores + 1)); if (c) { cores = c; cores[ncores++] = strdup(key); } }
        }
    }
    free(ci.p);
    for (char *c = impl; *c; c++) *c = (char)tolower((unsigned char)*c);
    for (char *c = part; *c; c++) *c = (char)tolower((unsigned char)*c);

    char base[512];
    snprintf(base, sizeof base, "%s/devices/system/cpu", g_sys);
    Names cpus = list_dir(base);
    int threads = 0;
    for (int i = 0; i < cpus.n; i++) if (suffix_num(cpus.v[i], "cpu") >= 0) threads++;
    if (!threads) threads = (int)sysconf(_SC_NPROCESSORS_CONF);
    int physical = ncores;
    if (!ncores) {                          /* Arm: count the sibling groups instead */
        char **sib = NULL;
        int ns = 0;
        for (int i = 0; i < threads; i++) {
            if (rdf(buf, sizeof buf, "%s/cpu%d/topology/thread_siblings_list", base, i) <= 0) continue;
            char *t = trim(buf);
            if (!*t) continue;
            int seen = 0;
            for (int j = 0; j < ns && !seen; j++) seen = !strcmp(sib[j], t);
            if (!seen) { char **s = realloc(sib, sizeof(char *) * (size_t)(ns + 1)); if (s) { sib = s; sib[ns++] = strdup(t); } }
        }
        physical = ns ? ns : threads;
        for (int j = 0; j < ns; j++) free(sib[j]);
        free(sib);
    }
    for (int i = 0; i < ncores; i++) free(cores[i]);
    free(cores);
    long maxmhz = -1, minmhz = -1;
    for (int i = 0; i < threads; i++) {
        if (rdf(buf, sizeof buf, "%s/cpu%d/cpufreq/cpuinfo_max_freq", base, i) > 0 && is_digits(trim(buf))) {
            long v = atol(trim(buf)) / 1000;
            if (v > maxmhz) maxmhz = v;
        }
        if (rdf(buf, sizeof buf, "%s/cpu%d/cpufreq/cpuinfo_min_freq", base, i) > 0 && is_digits(trim(buf))) {
            long v = atol(trim(buf)) / 1000;
            if (minmhz < 0 || v < minmhz) minmhz = v;
        }
    }
    free_names(&cpus);

    const char *core_name = "";
    if (!strcmp(impl, "0x41"))
        for (size_t i = 0; i < sizeof ARM_PARTS / sizeof *ARM_PARTS; i++)
            if (!strcmp(part, ARM_PARTS[i].id)) core_name = ARM_PARTS[i].name;
    const char *vend = vendor[0] ? vendor : impl;
    if (!vendor[0])
        for (size_t i = 0; i < sizeof ARM_IMPL / sizeof *ARM_IMPL; i++)
            if (!strcmp(impl, ARM_IMPL[i].id)) vend = ARM_IMPL[i].name;
    struct utsname un;
    uname(&un);

    b_str(b, "{\"cpu\":{\"model\":");
    j_str(b, g_cpu_model);
    b_str(b, ",\"core\":"); j_str(b, core_name);
    b_str(b, ",\"vendor\":"); j_str(b, vend);
    b_str(b, ",\"arch\":"); j_str(b, un.machine);
    b_fmt(b, ",\"cores\":%d,\"threads\":%d,\"max_mhz\":", physical, threads);
    j_ll_or_null(b, maxmhz, maxmhz >= 0);
    b_str(b, ",\"min_mhz\":"); j_ll_or_null(b, minmhz, minmhz >= 0);
    b_str(b, ",\"bogomips\":");
    if (bogo >= 0) b_fmt(b, "%.2f", bogo); else b_str(b, "null");
    b_str(b, ",\"caches\":[");
    char cdir[600];
    snprintf(cdir, sizeof cdir, "%s/cpu0/cache", base);
    Names idx = list_dir(cdir);
    int firstc = 1;
    for (int i = 0; i < idx.n; i++) {
        if (strncmp(idx.v[i], "index", 5)) continue;
        char lvl[32], type[64], size[64], shared[256];
        rdf(lvl, sizeof lvl, "%s/%s/level", cdir, idx.v[i]);
        rdf(type, sizeof type, "%s/%s/type", cdir, idx.v[i]);
        rdf(size, sizeof size, "%s/%s/size", cdir, idx.v[i]);
        rdf(shared, sizeof shared, "%s/%s/shared_cpu_list", cdir, idx.v[i]);
        long long sz = size_text(size);
        b_str(b, firstc ? "{" : ",{");
        firstc = 0;
        b_fmt(b, "\"level\":%d,\"type\":", atoi(trim(lvl)));
        j_str(b, trim(type));
        b_str(b, ",\"size\":"); j_ll_or_null(b, sz, sz >= 0);
        b_str(b, ",\"shared\":"); j_str(b, trim(shared));
        b_put(b, "}", 1);
    }
    free_names(&idx);
    b_str(b, "],\"flags\":[");
    int firstf = 1;
    for (size_t i = 0; i < sizeof FLAGS_OF_NOTE / sizeof *FLAGS_OF_NOTE; i++)
        if (flags_seen & (1u << i)) { if (!firstf) b_put(b, ",", 1); firstf = 0; j_str(b, FLAGS_OF_NOTE[i]); }
    b_str(b, "]}");

    MemInfo m;
    read_meminfo(&m);
    long long mt = mem_get(&m, "MemTotal"), st = mem_get(&m, "SwapTotal");
    b_str(b, ",\"memory\":{\"total\":"); j_ll_or_null(b, mt, mt >= 0);
    b_str(b, ",\"swap\":"); j_ll_or_null(b, st, st >= 0);
    b_put(b, "}", 1);

    /* board */
    char model[256] = "", dmi[6][256];
    static const char *dmif[] = {"sys_vendor", "product_name", "board_vendor", "board_name", "bios_version", "bios_date"};
    for (int i = 0; i < 6; i++) { rdf(dmi[i], sizeof dmi[i], "%s/class/dmi/id/%s", g_sys, dmif[i]); char *t = trim(dmi[i]); memmove(dmi[i], t, strlen(t) + 1); }
    char raw[256];
    ssize_t rn = rdf(raw, sizeof raw, "%s/device-tree/model", g_proc);
    if (rn <= 0) rn = rdf(raw, sizeof raw, "%s/firmware/devicetree/base/model", g_sys);
    if (rn > 0) {
        size_t o = 0;
        for (ssize_t i = 0; i < rn && o + 1 < sizeof model; i++) if (raw[i]) model[o++] = raw[i];
        model[o] = 0;
        char *t = trim(model);
        memmove(model, t, strlen(t) + 1);
    }
    if (!model[0]) {
        for (int i = 0; i < 2; i++) {
            if (!dmi[i][0] || strstr(dmi[i], "O.E.M") || strstr(dmi[i], "Default")) continue;
            if (model[0]) strncat(model, " ", sizeof model - strlen(model) - 1);
            strncat(model, dmi[i], sizeof model - strlen(model) - 1);
        }
    }
    char board[512] = "";
    snprintf(board, sizeof board, "%s%s%s", dmi[2], dmi[2][0] && dmi[3][0] ? " " : "", dmi[3]);
    b_str(b, ",\"board\":{\"model\":"); j_str(b, model);
    b_str(b, ",\"board\":"); j_str(b, board);
    b_str(b, ",\"bios\":"); j_str(b, dmi[4]);
    b_str(b, ",\"bios_date\":"); j_str(b, dmi[5]);
    b_put(b, "}", 1);

    /* disks */
    static const char *skip_disk[] = {"loop", "ram", "zram", "dm-", "sr", "md", "fd", "nbd"};
    char bdir[512];
    snprintf(bdir, sizeof bdir, "%s/block", g_sys);
    Names blk = list_dir(bdir);
    b_str(b, ",\"disks\":[");
    int firstd = 1;
    for (int i = 0; i < blk.n; i++) {
        const char *name = blk.v[i];
        if (starts_any(name, skip_disk, 8)) continue;
        char sectors[64];
        rdf(sectors, sizeof sectors, "%s/%s/size", bdir, name);
        long long size = is_digits(trim(sectors)) ? atoll(trim(sectors)) * 512 : 0;
        if (size <= 0) continue;
        char rot[16], rem[16], mdl[256], vendor2[128], sched[256], real[4096];
        rdf(rot, sizeof rot, "%s/%s/queue/rotational", bdir, name);
        rdf(rem, sizeof rem, "%s/%s/removable", bdir, name);
        if (rdf(mdl, sizeof mdl, "%s/%s/device/model", bdir, name) <= 0) rdf(mdl, sizeof mdl, "%s/%s/device/name", bdir, name);
        rdf(vendor2, sizeof vendor2, "%s/%s/device/vendor", bdir, name);
        rdf(sched, sizeof sched, "%s/%s/queue/scheduler", bdir, name);
        snprintf(path, sizeof path, "%s/%s", bdir, name);
        int usb = realpath(path, real) && strstr(real, "/usb");
        const char *kind = !strncmp(name, "nvme", 4) ? "NVMe SSD" : !strncmp(name, "mmcblk", 6) ? "SD card / eMMC" : !strcmp(trim(rot), "1") ? "HDD" : "SSD";
        char kindb[64];
        snprintf(kindb, sizeof kindb, "%s%s", kind, usb ? " (USB)" : "");
        char sc[64] = "";
        char *lb = strchr(sched, '['), *rb = lb ? strchr(lb, ']') : NULL;
        if (lb && rb && rb - lb - 1 < (long)sizeof sc) { memcpy(sc, lb + 1, (size_t)(rb - lb - 1)); sc[rb - lb - 1] = 0; }
        b_str(b, firstd ? "{" : ",{");
        firstd = 0;
        b_str(b, "\"name\":"); j_str(b, name);
        b_fmt(b, ",\"size\":%lld,\"kind\":", size); j_str(b, kindb);
        b_str(b, ",\"model\":"); j_str(b, trim(mdl));
        b_str(b, ",\"vendor\":"); j_str(b, trim(vendor2));
        b_fmt(b, ",\"removable\":%s,\"scheduler\":", !strcmp(trim(rem), "1") ? "true" : "false"); j_str(b, sc);
        b_put(b, "}", 1);
    }
    free_names(&blk);
    b_put(b, "]", 1);

    /* network links */
    static const char *skip_nic[] = {"lo", "veth", "cni", "flannel", "docker", "br-", "virbr", "kube", "cali", "vxlan", "tunl", "dummy"};
    char ndir[512];
    snprintf(ndir, sizeof ndir, "%s/class/net", g_sys);
    Names nics = list_dir(ndir);
    const char *node_ip = getenv("NODE_IP");
    b_str(b, ",\"nics\":[");
    int firstn = 1;
    for (int i = 0; i < nics.n; i++) {
        const char *name = nics.v[i];
        if (starts_any(name, skip_nic, 12)) continue;
        snprintf(path, sizeof path, "%s/%s/device", ndir, name);
        if (!exists(path) && strncmp(name, "tailscale", 9) && strncmp(name, "wg", 2)) continue;
        char sp[32], op[32], mtu[32], ip4[64], drv[256];
        rdf(sp, sizeof sp, "%s/%s/speed", ndir, name);
        rdf(op, sizeof op, "%s/%s/operstate", ndir, name);
        rdf(mtu, sizeof mtu, "%s/%s/mtu", ndir, name);
        iface_ipv4(name, ip4, sizeof ip4);
        snprintf(path, sizeof path, "%s/%s/device/driver", ndir, name);
        link_base(path, drv, sizeof drv);
        int maxs = iface_max_mbps(name);
        if (maxs < 0 && !strcmp(drv, "smsc95xx")) maxs = 100;      /* Raspberry Pi 1-3B: 100 Mbit/s hardware */
        snprintf(path, sizeof path, "%s/%s/wireless", ndir, name);
        int wireless = is_dir(path) || !strncmp(name, "wl", 2);
        char *spt = trim(sp), *opt = trim(op);
        long speed = is_digits(spt) ? atol(spt) : 0;
        b_str(b, firstn ? "{" : ",{");
        firstn = 0;
        b_str(b, "\"name\":"); j_str(b, name);
        b_str(b, ",\"ipv4\":"); j_str(b, ip4);
        b_fmt(b, ",\"cluster\":%s,\"max_mbps\":", ip4[0] && node_ip && !strcmp(ip4, node_ip) ? "true" : "false");
        j_ll_or_null(b, maxs, maxs >= 0);
        b_str(b, ",\"driver\":"); j_str(b, strcmp(drv, "driver") ? drv : "");
        b_fmt(b, ",\"wireless\":%s,\"speed_mbps\":", wireless ? "true" : "false");
        j_ll_or_null(b, speed, speed > 0);
        b_fmt(b, ",\"up\":%s,\"mtu\":%d}", !strcmp(opt, "up") || !strcmp(opt, "unknown") ? "true" : "false", atoi(trim(mtu)));
    }
    free_names(&nics);
    b_put(b, "]", 1);

    /* graphics devices (PCI class 0x03) */
    char nvver[64] = "";
    if (rdf(buf, sizeof buf, "%s/driver/nvidia/version", g_proc) > 0) {
        char *k = strstr(buf, "Kernel Module");
        if (k) {
            k += 13;
            while (*k == ' ' || *k == '\t') k++;
            size_t n = 0;
            while ((isdigit((unsigned char)k[n]) || k[n] == '.') && n + 1 < sizeof nvver) n++;
            memcpy(nvver, k, n);
            nvver[n] = 0;
        }
    }
    char pdir[512];
    snprintf(pdir, sizeof pdir, "%s/bus/pci/devices", g_sys);
    Names pci = list_dir(pdir);
    b_str(b, ",\"gpus\":[");
    int firstg = 1;
    for (int i = 0; i < pci.n; i++) {
        const char *bus = pci.v[i];
        char cls[32];
        rdf(cls, sizeof cls, "%s/%s/class", pdir, bus);
        if (strncmp(trim(cls), "0x03", 4)) continue;
        char drv[256] = "", ven[32], dev[32], boot[16], vram[64], gmodel[256] = "";
        snprintf(path, sizeof path, "%s/%s/driver", pdir, bus);
        if (exists(path)) link_base(path, drv, sizeof drv);
        rdf(ven, sizeof ven, "%s/%s/vendor", pdir, bus);
        rdf(dev, sizeof dev, "%s/%s/device", pdir, bus);
        rdf(boot, sizeof boot, "%s/%s/boot_vga", pdir, bus);
        if (rdf(buf, sizeof buf, "%s/driver/nvidia/gpus/%s/information", g_proc, bus) > 0) {
            for (char *line = strtok(buf, "\n"); line; line = strtok(NULL, "\n"))
                if (!strncmp(line, "Model:", 6)) { snprintf(gmodel, sizeof gmodel, "%s", trim(line + 6)); break; }
        }
        rdf(vram, sizeof vram, "%s/%s/mem_info_vram_total", pdir, bus);
        char *vr = trim(vram);
        b_str(b, firstg ? "{" : ",{");
        firstg = 0;
        b_str(b, "\"bus\":"); j_str(b, bus);
        b_str(b, ",\"vendor\":"); j_str(b, trim(ven));
        b_str(b, ",\"device\":"); j_str(b, trim(dev));
        b_str(b, ",\"driver\":"); j_str(b, drv);
        b_fmt(b, ",\"boot\":%s,\"model\":", !strcmp(trim(boot), "1") ? "true" : "false"); j_str(b, gmodel);
        b_str(b, ",\"driver_version\":"); j_str(b, gmodel[0] ? nvver : "");
        b_str(b, ",\"vram\":"); j_ll_or_null(b, is_digits(vr) ? atoll(vr) : 0, is_digits(vr));
        b_put(b, "}", 1);
    }
    free_names(&pci);
    b_put(b, "]", 1);
    char kern[256];
    rdf(kern, sizeof kern, "%s/sys/kernel/osrelease", g_proc);
    b_str(b, ",\"kernel\":"); j_str(b, trim(kern));
    b_put(b, "}", 1);
}

/* ------------------------------------------------------------------ live per-core, temperatures, NVIDIA */

static void j_temps(Buf *b) {
    char dir[512], buf[256], name[256];
    char seen[64][128];
    int nseen = 0, first = 1;
    b_put(b, "[", 1);
    snprintf(dir, sizeof dir, "%s/class/thermal", g_sys);
    Names z = list_dir(dir);
    for (int i = 0; i < z.n; i++) {
        if (strncmp(z.v[i], "thermal_zone", 12)) continue;
        rdf(buf, sizeof buf, "%s/%s/temp", dir, z.v[i]);
        char *t = trim(buf);
        if (!is_int(t)) continue;
        rdf(name, sizeof name, "%s/%s/type", dir, z.v[i]);
        char *nm = trim(name);
        if (!*nm) nm = z.v[i];
        if (nseen < 64) snprintf(seen[nseen++], sizeof seen[0], "%s", nm);
        b_str(b, first ? "{\"name\":" : ",{\"name\":");
        first = 0;
        j_str(b, nm);
        b_fmt(b, ",\"c\":%.3f}", atol(t) / 1000.0);
    }
    free_names(&z);
    snprintf(dir, sizeof dir, "%s/class/hwmon", g_sys);
    Names h = list_dir(dir);
    for (int i = 0; i < h.n; i++) {
        rdf(name, sizeof name, "%s/%s/name", dir, h.v[i]);
        char *nm = trim(name);
        if (!*nm) nm = h.v[i];
        char sub[600];
        snprintf(sub, sizeof sub, "%s/%s", dir, h.v[i]);
        Names f = list_dir(sub);
        for (int j = 0; j < f.n; j++) {
            const char *fn = f.v[j];
            if (strncmp(fn, "temp", 4)) continue;
            const char *p = fn + 4;
            if (!isdigit((unsigned char)*p)) continue;
            while (isdigit((unsigned char)*p)) p++;
            if (strcmp(p, "_input")) continue;
            rdf(buf, sizeof buf, "%s/%s", sub, fn);
            char *t = trim(buf);
            if (!is_int(t)) continue;
            int dup = 0;
            for (int k = 0; k < nseen && !dup; k++) dup = !strcmp(seen[k], nm);
            if (dup) continue;
            if (nseen < 64) snprintf(seen[nseen++], sizeof seen[0], "%s", nm);
            b_str(b, first ? "{\"name\":" : ",{\"name\":");
            first = 0;
            j_str(b, nm);
            b_fmt(b, ",\"c\":%.3f}", atol(t) / 1000.0);
        }
        free_names(&f);
    }
    free_names(&h);
    b_put(b, "]", 1);
}

static void j_power(Buf *b) {
    char dir[512], name[64], v[16];
    snprintf(dir, sizeof dir, "%s/class/hwmon", g_sys);
    Names h = list_dir(dir);
    for (int i = 0; i < h.n; i++) {
        rdf(name, sizeof name, "%s/%s/name", dir, h.v[i]);
        if (strcmp(trim(name), "rpi_volt")) continue;
        rdf(v, sizeof v, "%s/%s/in0_lcrit_alarm", dir, h.v[i]);
        b_fmt(b, "{\"undervoltage\":%s}", !strcmp(trim(v), "1") ? "true" : "false");
        free_names(&h);
        return;
    }
    free_names(&h);
    b_str(b, "{}");
}

/* nvidia-smi: only there in the GPU flavour of the agent pod (NVIDIA's runtime adds it) */
static char g_nvsmi[512];
static void find_nvidia_smi(void) {
    const char *path = getenv("PATH");
    char copy[2048];
    snprintf(copy, sizeof copy, "%s", path ? path : "/usr/bin:/bin");
    for (char *d = strtok(copy, ":"); d; d = strtok(NULL, ":")) {
        char p[600];
        snprintf(p, sizeof p, "%s/nvidia-smi", d);
        if (access(p, X_OK) == 0) { snprintf(g_nvsmi, sizeof g_nvsmi, "%s", p); return; }
    }
}
static void j_num_or_null(Buf *b, const char *s) {
    char *end;
    double d = strtod(s, &end);
    while (*end == ' ') end++;
    if (end == s || *end) b_str(b, "null"); else b_fmt(b, "%g", d);
}
static void j_gpu_live(Buf *b) {
    b_put(b, "[", 1);
    if (!g_nvsmi[0]) { b_put(b, "]", 1); return; }
    int fds[2];
    if (pipe2(fds, O_CLOEXEC) < 0) { b_put(b, "]", 1); return; }
    pid_t pid = fork();
    if (pid == 0) {
        dup2(fds[1], 1);
        int dn = open("/dev/null", O_WRONLY);
        if (dn >= 0) dup2(dn, 2);
        execl(g_nvsmi, "nvidia-smi", "--query-gpu=index,pci.bus_id,name,utilization.gpu,utilization.memory,memory.used,memory.total,"
              "temperature.gpu,power.draw,power.limit,clocks.sm,clocks.max.sm,fan.speed", "--format=csv,noheader,nounits", (char *)NULL);
        _exit(127);
    }
    close(fds[1]);
    Buf out = {0};
    long long deadline = mono_ms() + 5000;
    for (;;) {
        struct pollfd p = {fds[0], POLLIN, 0};
        long long left = deadline - mono_ms();
        if (left <= 0 || poll(&p, 1, (int)left) <= 0) break;
        b_grow(&out, 4096);
        ssize_t r = read(fds[0], out.p + out.n, 4096);
        if (r <= 0) break;
        out.n += (size_t)r;
        out.p[out.n] = 0;
    }
    close(fds[0]);
    if (pid > 0) {
        if (waitpid(pid, NULL, WNOHANG) == 0) { kill(pid, SIGKILL); waitpid(pid, NULL, 0); }
    }
    int first = 1;
    for (char *line = strtok(out.p ? out.p : (char *)"", "\n"); line; line = strtok(NULL, "\n")) {
        char *f[16];
        int nf = 0;
        char *s = line;
        while (nf < 16) { f[nf++] = s; s = strchr(s, ','); if (!s) break; *s++ = 0; }
        if (nf < 13) continue;
        for (int i = 0; i < nf; i++) f[i] = trim(f[i]);
        char bus[64];
        snprintf(bus, sizeof bus, "%s", !strncmp(f[1], "00000000:", 9) ? f[1] + 4 : f[1]);
        for (char *c = bus; *c; c++) *c = (char)tolower((unsigned char)*c);
        b_str(b, first ? "{" : ",{");
        first = 0;
        b_fmt(b, "\"index\":%d,\"bus\":", atoi(f[0]));
        j_str(b, bus);
        b_str(b, ",\"name\":"); j_str(b, f[2]);
        b_str(b, ",\"use\":"); j_num_or_null(b, f[3]);
        b_str(b, ",\"mem_use\":"); j_num_or_null(b, f[4]);
        b_fmt(b, ",\"mem_used\":%lld,\"mem_total\":%lld", (long long)(strtod(f[5], NULL) * 1048576), (long long)(strtod(f[6], NULL) * 1048576));
        b_str(b, ",\"temp_c\":"); j_num_or_null(b, f[7]);
        b_str(b, ",\"power_w\":"); j_num_or_null(b, f[8]);
        b_str(b, ",\"power_limit_w\":"); j_num_or_null(b, f[9]);
        b_str(b, ",\"clock_mhz\":"); j_num_or_null(b, f[10]);
        b_str(b, ",\"clock_max_mhz\":"); j_num_or_null(b, f[11]);
        b_str(b, ",\"fan\":"); j_num_or_null(b, f[12]);
        b_put(b, "}", 1);
    }
    free(out.p);
    b_put(b, "]", 1);
}

/* ------------------------------------------------------------------ the sample */

typedef struct {
    int pid, ppid, group, pick;
    char comm[64], state;
    unsigned long long ticks, start;
    long threads;
    long long rss;
    double cpu;
    char container[16];
} Proc;
typedef struct { Group g; long long rss; double cpu; int procs; } Agg;
typedef struct { int pid; unsigned long long start, ticks; } Prev;

static Prev *g_prev;
static int g_nprev;
static double g_prev_t;
static CpuTimes g_prev_cpu[MAX_CPUS + 1];
static int g_have_prev;
static Buf g_hw;
static double g_hw_at;
static char g_node[256];

static int cmp_prev(const void *a, const void *b) { return ((const Prev *)a)->pid - ((const Prev *)b)->pid; }
static Proc *g_sort_procs;
static int cmp_rss_desc(const void *a, const void *b) {
    long long x = g_sort_procs[*(const int *)a].rss, y = g_sort_procs[*(const int *)b].rss;
    return x < y ? 1 : x > y ? -1 : 0;
}
static int cmp_cpu_desc(const void *a, const void *b) {
    double x = g_sort_procs[*(const int *)a].cpu, y = g_sort_procs[*(const int *)b].cpu;
    return x < y ? 1 : x > y ? -1 : 0;
}
static Agg *g_sort_aggs;
static int cmp_agg_desc(const void *a, const void *b) {
    long long x = g_sort_aggs[*(const int *)a].rss, y = g_sort_aggs[*(const int *)b].rss;
    return x < y ? 1 : x > y ? -1 : 0;
}
typedef struct { long long rss; size_t off, len; } Detail;
static int cmp_detail(const void *a, const void *b) {
    long long x = ((const Detail *)a)->rss, y = ((const Detail *)b)->rss;
    return x < y ? 1 : x > y ? -1 : 0;
}

static void sample(Buf *out) {
    double now = now_s();
    static char buf[65536];
    char path[512];
    static CpuTimes cpu[MAX_CPUS + 1];
    read_cpu_times(cpu, MAX_CPUS + 1);

    /* every process: stat + statm */
    Proc *procs = NULL;
    int np = 0, capp = 0;
    DIR *d = opendir(g_proc);
    struct dirent *e;
    while (d && (e = readdir(d))) {
        if (!is_digits(e->d_name)) continue;
        int pid = atoi(e->d_name);
        Stat st;
        if (rdf(buf, sizeof buf, "%s/%d/stat", g_proc, pid) <= 0 || parse_stat(buf, &st)) continue;
        if (np == capp) {
            capp = capp ? capp * 2 : 512;
            Proc *p = realloc(procs, sizeof(Proc) * (size_t)capp);
            if (!p) break;
            procs = p;
        }
        Proc *p = &procs[np++];
        memset(p, 0, sizeof *p);
        p->pid = pid;
        p->ppid = st.ppid;
        p->state = st.state;
        p->ticks = st.ticks;
        p->start = st.start;
        p->threads = st.threads;
        snprintf(p->comm, sizeof p->comm, "%s", st.comm);
        char statm[256];
        if (rdf(statm, sizeof statm, "%s/%d/statm", g_proc, pid) > 0) {
            unsigned long long size, res;
            if (sscanf(statm, "%llu %llu", &size, &res) == 2) p->rss = (long long)res * g_page;
        }
    }
    if (d) closedir(d);

    /* CPU since the last sample */
    double dt = g_have_prev ? now - g_prev_t : 0;
    double cores_use[MAX_CPUS + 1];
    int cores_have[MAX_CPUS + 1];
    memset(cores_have, 0, sizeof cores_have);
    if (g_have_prev && dt > 0)
        for (int i = 0; i <= MAX_CPUS; i++)
            if (cpu[i].valid && g_prev_cpu[i].valid && cpu[i].total > g_prev_cpu[i].total) {
                double u = 100.0 * (double)(cpu[i].busy - g_prev_cpu[i].busy) / (double)(cpu[i].total - g_prev_cpu[i].total);
                cores_use[i] = u < 0 ? 0 : u > 100 ? 100 : u;
                cores_have[i] = 1;
            }
    for (int i = 0; i < np; i++) {
        Proc *p = &procs[i];
        p->cpu = 0;
        if (g_have_prev && dt > 0) {
            Prev key = {p->pid, 0, 0};
            Prev *hit = bsearch(&key, g_prev, (size_t)g_nprev, sizeof(Prev), cmp_prev);
            if (hit && hit->start == p->start && p->ticks >= hit->ticks) p->cpu = (double)(p->ticks - hit->ticks) / (double)g_clk / dt * 100.0;
        }
    }
    free(g_prev);
    g_prev = malloc(sizeof(Prev) * (size_t)(np ? np : 1));
    g_nprev = 0;
    for (int i = 0; g_prev && i < np; i++) g_prev[g_nprev++] = (Prev){procs[i].pid, procs[i].start, procs[i].ticks};
    if (g_prev) qsort(g_prev, (size_t)g_nprev, sizeof(Prev), cmp_prev);
    memcpy(g_prev_cpu, cpu, sizeof cpu);
    g_prev_t = now;
    g_have_prev = 1;

    /* details only for the processes that matter: the biggest, the busiest, and everything over 5 MB */
    int *order = malloc(sizeof(int) * (size_t)(np ? np : 1));
    for (int i = 0; i < np; i++) order[i] = i;
    g_sort_procs = procs;
    qsort(order, (size_t)np, sizeof(int), cmp_rss_desc);
    for (int i = 0; i < np; i++)
        if (i < TOP_BY_MEMORY || (procs[order[i]].rss >= BIG_RSS && i < MAX_DETAILED)) procs[order[i]].pick = 1;
    qsort(order, (size_t)np, sizeof(int), cmp_cpu_desc);
    for (int i = 0; i < np && i < TOP_BY_CPU; i++) procs[order[i]].pick = 1;

    /* groups for all of them */
    Agg *aggs = NULL;
    int na = 0, capa = 0;
    Buf procs_json = {0};
    Detail *det = malloc(sizeof(Detail) * (size_t)(np ? np : 1));
    int nd = 0;
    char uptime_s[64];
    rdf(uptime_s, sizeof uptime_s, "%s/uptime", g_proc);
    double uptime = strtod(uptime_s, NULL);
    double boot = uptime > 0 ? now - uptime : 0;
    long long counts_running = 0, counts_sleeping = 0, counts_zombie = 0, counts_threads = 0;
    for (int i = 0; i < np; i++) {
        Proc *p = &procs[i];
        counts_threads += p->threads;
        if (p->state == 'R') counts_running++;
        else if (p->state == 'S' || p->state == 'D' || p->state == 'I') counts_sleeping++;
        else if (p->state == 'Z') counts_zombie++;
        char cg[4096];
        rdf(cg, sizeof cg, "%s/%d/cgroup", g_proc, p->pid);
        Group g;
        parse_group(cg, p->pid, p->ppid, &g);
        snprintf(p->container, sizeof p->container, "%s", g.container);
        const char *key = g.kind == G_POD ? g.uid : g.name[0] ? g.name : G_KIND[g.kind];
        int ai = -1;
        for (int k = 0; k < na; k++) {
            const Group *h = &aggs[k].g;
            const char *hk = h->kind == G_POD ? h->uid : h->name[0] ? h->name : G_KIND[h->kind];
            if (!strcmp(hk, key)) { ai = k; break; }
        }
        if (ai < 0) {
            if (na == capa) { capa = capa ? capa * 2 : 64; Agg *a = realloc(aggs, sizeof(Agg) * (size_t)capa); if (!a) continue; aggs = a; }
            ai = na++;
            memset(&aggs[ai], 0, sizeof(Agg));
            aggs[ai].g = g;
        }
        aggs[ai].rss += p->rss;
        aggs[ai].cpu += p->cpu;
        aggs[ai].procs++;
        if (!p->pick) continue;

        Status s;
        if (rdf(buf, sizeof buf, "%s/%d/status", g_proc, p->pid) < 0) buf[0] = 0;
        parse_status(buf, &s);
        char cmd[4096];
        ssize_t cn = rdf(cmd, sizeof cmd, "%s/%d/cmdline", g_proc, p->pid);
        for (ssize_t k = 0; k < cn; k++) if (!cmd[k]) cmd[k] = ' ';
        char *ct = trim(cmd);
        char red[1024];
        redact(ct, red, sizeof red);
        size_t off = procs_json.n;
        b_fmt(&procs_json, "{\"pid\":%d,\"ppid\":%d,\"name\":", p->pid, p->ppid);
        j_str(&procs_json, p->comm);
        char stc[2] = {p->state, 0};
        b_str(&procs_json, ",\"state\":"); j_str(&procs_json, stc);
        b_fmt(&procs_json, ",\"threads\":%lld,\"user\":", s.has_threads ? s.threads : (long long)p->threads);
        const char *un = s.has_uid ? user_name((long)s.uid) : NULL;
        char uidb[32] = "";
        if (s.has_uid && !un) snprintf(uidb, sizeof uidb, "%lld", s.uid);
        j_str(&procs_json, un ? un : uidb);
        long long rss = s.has_rss ? s.rss : p->rss;
        b_fmt(&procs_json, ",\"rss\":%lld,\"anon\":", rss);
        j_ll_or_null(&procs_json, s.anon, s.has_anon);
        b_str(&procs_json, ",\"file\":"); j_ll_or_null(&procs_json, s.file, s.has_file);
        b_str(&procs_json, ",\"shmem\":"); j_ll_or_null(&procs_json, s.shmem, s.has_shmem);
        b_str(&procs_json, ",\"swap\":"); j_ll_or_null(&procs_json, s.swap, s.has_swap);
        b_fmt(&procs_json, ",\"cpu\":%.1f,\"cmd\":", p->cpu);
        j_str(&procs_json, red);
        b_str(&procs_json, ",\"group\":");
        j_group(&procs_json, &g);
        b_fmt(&procs_json, ",\"started\":%.2f,\"start_ticks\":%llu}", boot ? boot + (double)p->start / (double)g_clk : 0.0, p->start);
        det[nd++] = (Detail){rss, off, procs_json.n - off};
    }
    qsort(det, (size_t)nd, sizeof(Detail), cmp_detail);

    /* now write it all */
    out->n = 0;
    b_str(out, "{\"gpu_live\":");
    j_gpu_live(out);
    if (!g_hw.n || now - g_hw_at > HARDWARE_EVERY) {
        g_hw.n = 0;
        j_hardware(&g_hw);
        g_hw_at = now;
    }
    b_str(out, ",\"hardware\":");
    b_put(out, g_hw.p, g_hw.n);
    b_str(out, ",\"node\":");
    j_str(out, g_node);
    b_fmt(out, ",\"time\":%.3f,\"uptime\":%.2f,\"load\":[", now, uptime);
    char la[256];
    rdf(la, sizeof la, "%s/loadavg", g_proc);
    double l1, l5, l15;
    if (sscanf(la, "%lf %lf %lf", &l1, &l5, &l15) == 3) b_fmt(out, "%.2f,%.2f,%.2f", l1, l5, l15);
    b_str(out, "],\"cpu\":{\"model\":");
    j_str(out, g_cpu_model);
    b_str(out, ",\"cores\":[");
    char cdir[512];
    snprintf(cdir, sizeof cdir, "%s/devices/system/cpu", g_sys);
    Names cn = list_dir(cdir);
    int ids[MAX_CPUS], nid = 0;
    for (int i = 0; i < cn.n && nid < MAX_CPUS; i++) { int id = suffix_num(cn.v[i], "cpu"); if (id >= 0) ids[nid++] = id; }
    free_names(&cn);
    for (int i = 1; i < nid; i++) for (int j = i; j > 0 && ids[j - 1] > ids[j]; j--) { int t = ids[j]; ids[j] = ids[j - 1]; ids[j - 1] = t; }
    for (int i = 0; i < nid; i++) {
        int id = ids[i];
        char v[64], gov[64], onl[16];
        long cur = -1, mn = -1, mx = -1;
        if (rdf(v, sizeof v, "%s/cpu%d/cpufreq/scaling_cur_freq", cdir, id) > 0 && is_digits(trim(v))) cur = atol(trim(v)) / 1000;
        if (cur <= 0 && rdf(v, sizeof v, "%s/cpu%d/cpufreq/cpuinfo_cur_freq", cdir, id) > 0 && is_digits(trim(v))) cur = atol(trim(v)) / 1000;
        if (rdf(v, sizeof v, "%s/cpu%d/cpufreq/cpuinfo_min_freq", cdir, id) > 0 && is_digits(trim(v))) mn = atol(trim(v)) / 1000;
        if (rdf(v, sizeof v, "%s/cpu%d/cpufreq/cpuinfo_max_freq", cdir, id) > 0 && is_digits(trim(v))) mx = atol(trim(v)) / 1000;
        rdf(gov, sizeof gov, "%s/cpu%d/cpufreq/scaling_governor", cdir, id);
        if (rdf(onl, sizeof onl, "%s/cpu%d/online", cdir, id) < 0) strcpy(onl, "1");
        b_fmt(out, "%s{\"id\":%d,\"mhz\":", i ? "," : "", id);
        j_ll_or_null(out, cur, cur > 0);
        b_str(out, ",\"min\":"); j_ll_or_null(out, mn, mn > 0);
        b_str(out, ",\"max\":"); j_ll_or_null(out, mx, mx > 0);
        b_str(out, ",\"governor\":"); j_str(out, trim(gov));
        b_fmt(out, ",\"online\":%s,\"use\":", strcmp(trim(onl), "0") ? "true" : "false");
        if (id + 1 <= MAX_CPUS && cores_have[id + 1]) b_fmt(out, "%.1f", cores_use[id + 1]); else b_str(out, "null");
        b_put(out, "}", 1);
    }
    b_str(out, "],\"use\":");
    if (cores_have[0]) b_fmt(out, "%.1f", cores_use[0]); else b_str(out, "null");
    b_str(out, "},\"memory\":{");
    MemInfo m;
    read_meminfo(&m);
    int firstm = 1;
    for (size_t i = 0; i < N_MEM; i++) {
        if (!m.has[i]) continue;
        if (!firstm) b_put(out, ",", 1);
        firstm = 0;
        j_key(out, MEM_KEYS[i]);
        b_fmt(out, "%lld", m.v[i]);
    }
    b_str(out, "},\"pressure\":{\"cpu\":");
    j_pressure(out, "cpu");
    b_str(out, ",\"memory\":");
    j_pressure(out, "memory");
    b_str(out, ",\"io\":");
    j_pressure(out, "io");
    b_str(out, "},\"vmstat\":{");
    snprintf(path, sizeof path, "%s/vmstat", g_proc);
    Buf vm = {0};
    rd_all(path, &vm);
    int firstv = 1;
    for (char *line = strtok(vm.p ? vm.p : (char *)"", "\n"); line; line = strtok(NULL, "\n")) {
        char *sp = strchr(line, ' ');
        if (!sp) continue;
        *sp = 0;
        if (strcmp(line, "oom_kill") && strcmp(line, "pgmajfault") && strcmp(line, "pswpin") && strcmp(line, "pswpout")) continue;
        if (!is_digits(trim(sp + 1))) continue;
        if (!firstv) b_put(out, ",", 1);
        firstv = 0;
        j_key(out, line);
        b_str(out, trim(sp + 1));
    }
    free(vm.p);
    b_str(out, "},\"disks\":[");
    snprintf(path, sizeof path, "%s/diskstats", g_proc);
    Buf ds = {0};
    rd_all(path, &ds);
    int firstds = 1;
    for (char *line = strtok(ds.p ? ds.p : (char *)"", "\n"); line; line = strtok(NULL, "\n")) {
        char name[64];
        unsigned long long f[11];
        unsigned maj, min;
        if (sscanf(line, " %u %u %63s %llu %llu %llu %llu %llu %llu %llu %llu %llu %llu %llu", &maj, &min, name,
                   &f[0], &f[1], &f[2], &f[3], &f[4], &f[5], &f[6], &f[7], &f[8], &f[9], &f[10]) < 14) continue;
        int ok = 0;
        const char *q;
        if ((!strncmp(name, "sd", 2) || !strncmp(name, "vd", 2)) && name[2]) { ok = 1; for (q = name + 2; *q; q++) ok &= *q >= 'a' && *q <= 'z'; }
        else if (!strncmp(name, "xvd", 3) && name[3]) { ok = 1; for (q = name + 3; *q; q++) ok &= *q >= 'a' && *q <= 'z'; }
        else if (!strncmp(name, "mmcblk", 6)) ok = is_digits(name + 6);
        else if (!strncmp(name, "nvme", 4)) {
            q = name + 4;
            if (isdigit((unsigned char)*q)) { while (isdigit((unsigned char)*q)) q++; if (*q == 'n') ok = is_digits(q + 1); }
        }
        if (!ok) continue;
        b_str(out, firstds ? "{\"name\":" : ",{\"name\":");
        firstds = 0;
        j_str(out, name);
        b_fmt(out, ",\"read\":%llu,\"write\":%llu}", f[2] * 512ULL, f[6] * 512ULL);
    }
    free(ds.p);
    b_str(out, "],\"temps\":");
    j_temps(out);
    b_str(out, ",\"power\":");
    j_power(out);
    b_fmt(out, ",\"counts\":{\"total\":%d,\"running\":%lld,\"sleeping\":%lld,\"zombie\":%lld,\"threads\":%lld}",
          np, counts_running, counts_sleeping, counts_zombie, counts_threads);
    char fnr[128];
    rdf(fnr, sizeof fnr, "%s/sys/fs/file-nr", g_proc);
    b_str(out, ",\"files\":[");
    if (isdigit((unsigned char)fnr[0])) b_fmt(out, "%lld", atoll(fnr));
    b_str(out, "],\"processes\":[");
    for (int i = 0; i < nd; i++) {
        if (i) b_put(out, ",", 1);
        b_put(out, procs_json.p + det[i].off, det[i].len);
    }
    b_str(out, "],\"groups\":[");
    int *ao = malloc(sizeof(int) * (size_t)(na ? na : 1));
    for (int i = 0; i < na; i++) ao[i] = i;
    g_sort_aggs = aggs;
    qsort(ao, (size_t)na, sizeof(int), cmp_agg_desc);
    for (int i = 0; i < na && i < 40; i++) {
        const Agg *a = &aggs[ao[i]];
        b_str(out, i ? ",{\"kind\":" : "{\"kind\":");
        j_str(out, G_KIND[a->g.kind]);
        b_str(out, ",\"uid\":"); j_str(out, a->g.kind == G_POD ? a->g.uid : "");
        b_str(out, ",\"name\":"); j_str(out, a->g.kind == G_POD ? "" : a->g.name);
        b_fmt(out, ",\"rss\":%lld,\"cpu\":%.1f,\"procs\":%d}", a->rss, a->cpu, a->procs);
    }
    b_str(out, "]}");
    free(ao);
    free(aggs);
    free(det);
    free(procs_json.p);
    free(order);
    free(procs);
}

/* ------------------------------------------------------------------ HTTP */

static Buf g_snap;
static char g_token[256];

static void send_all(int fd, const char *p, size_t n) {
    while (n) {
        ssize_t w = send(fd, p, n, MSG_NOSIGNAL);
        if (w < 0) { if (errno == EINTR) continue; return; }
        p += w;
        n -= (size_t)w;
    }
}
static void respond(int fd, int code, const char *ctype, const char *body, size_t n, int head_only) {
    const char *txt = code == 200 ? "OK" : code == 401 ? "Unauthorized" : code == 404 ? "Not Found" : code == 405 ? "Method Not Allowed"
                    : code == 503 ? "Service Unavailable" : "Bad Request";
    char h[512];
    int hn = snprintf(h, sizeof h, "HTTP/1.1 %d %s\r\nServer: nodeyard-agent\r\nContent-Type: %s\r\nContent-Length: %zu\r\n"
                      "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n", code, txt, ctype, n);
    send_all(fd, h, (size_t)hn);
    if (!head_only) send_all(fd, body, n);
}
static int token_ok(const char *got) {
    size_t a = strlen(got), b = strlen(g_token);
    unsigned char diff = (unsigned char)(a != b);
    for (size_t i = 0; i < b; i++) diff |= (unsigned char)(g_token[i] ^ (i < a ? got[i] : 0));
    return !diff && b > 0;
}
static void serve_conn(int fd) {
    struct timeval tv = {3, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    char req[8192];
    size_t n = 0;
    req[0] = 0;
    while (n < sizeof req - 1 && !strstr(req, "\r\n\r\n")) {
        ssize_t r = recv(fd, req + n, sizeof req - 1 - n, 0);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) break;
        n += (size_t)r;
        req[n] = 0;
    }
    char *end = strstr(req, "\r\n\r\n");
    if (!end) return;
    *end = 0;
    char *line_end = strstr(req, "\r\n");
    if (line_end) *line_end = 0;
    char method[16] = "", target[1024] = "";
    if (sscanf(req, "%15s %1023s", method, target) != 2) { respond(fd, 400, "application/json", "{\"error\":\"bad request\"}", 23, 0); return; }
    char token[256] = "";
    for (char *h = line_end ? line_end + 2 : NULL; h && *h;) {
        char *next = strstr(h, "\r\n");
        if (next) *next = 0;
        char *colon = strchr(h, ':');
        if (colon) {
            *colon = 0;
            if (!strcasecmp(h, "X-Agent-Token")) snprintf(token, sizeof token, "%s", trim(colon + 1));
        }
        h = next ? next + 2 : NULL;
    }
    int head = !strcmp(method, "HEAD");
    if (strcmp(method, "GET") && !head) { respond(fd, 405, "application/json", "{\"error\":\"read-only\"}", 21, 0); return; }
    if (!strcmp(target, "/healthz")) { respond(fd, 200, "text/plain", "ok", 2, head); return; }
    if (strcmp(target, "/metrics.json")) { respond(fd, 404, "application/json", "{\"error\":\"not found\"}", 21, head); return; }
    if (!token_ok(token)) { respond(fd, 401, "application/json", "{\"error\":\"token\"}", 17, head); return; }
    if (!g_snap.n) { respond(fd, 503, "application/json", "{\"error\":\"warming up\"}", 22, head); return; }
    respond(fd, 200, "application/json", g_snap.p, g_snap.n, head);
}

/* ------------------------------------------------------------------ pageout */

#ifndef SYS_pidfd_open
#define SYS_pidfd_open 434
#endif
#ifndef SYS_process_madvise
#define SYS_process_madvise 440
#endif
#define MADV_PAGEOUT_ 21

static void status_mem(int pid, long long *rss, long long *swap) {
    char buf[8192];
    Status s;
    if (rdf(buf, sizeof buf, "%s/%d/status", g_proc, pid) < 0) buf[0] = 0;
    parse_status(buf, &s);
    *rss = s.has_rss ? s.rss : 0;
    *swap = s.has_swap ? s.swap : 0;
}

static int pageout(int pid, unsigned long long want_start) {
    char buf[4096];
    Stat st;
    if (pid <= 2 || rdf(buf, sizeof buf, "%s/%d/stat", g_proc, pid) <= 0 || parse_stat(buf, &st)) { puts("error=that process isn't running"); return 2; }
    if (want_start && st.start != want_start) { puts("error=that process has ended (another one has its number now)"); return 2; }
    MemInfo m;
    read_meminfo(&m);
    long long rss0, swap0, rss1, swap1;
    status_mem(pid, &rss0, &swap0);
    int pfd = (int)syscall(SYS_pidfd_open, pid, 0);
    if (pfd < 0) { printf("error=pidfd_open: %s\nunsupported=%d\n", strerror(errno), errno == ENOSYS); return errno == ENOSYS ? 3 : 2; }
    char maps[64];
    snprintf(maps, sizeof maps, "%s/%d/maps", g_proc, pid);
    FILE *f = fopen(maps, "re");
    if (!f) { printf("error=can't read its memory map: %s\n", strerror(errno)); close(pfd); return 2; }
    char line[4096];
    long long advised = 0;
    int regions = 0, failed = 0, unsupported = 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long lo, hi;
        char perms[8];
        if (sscanf(line, "%lx-%lx %7s", &lo, &hi, perms) != 3 || hi <= lo) continue;
        if (strstr(line, "[vsyscall]") || strstr(line, "[vvar]") || strstr(line, "[vdso]")) continue;
        struct iovec iov = {(void *)lo, hi - lo};
        long r = syscall(SYS_process_madvise, pfd, &iov, 1, MADV_PAGEOUT_, 0);
        regions++;
        if (r >= 0) advised += r;
        else if (errno == ENOSYS) { unsupported = 1; break; }
        else if (errno == EPERM) { printf("error=not allowed to move its memory (%s)\n", strerror(errno)); fclose(f); close(pfd); return 2; }
        else failed++;
    }
    fclose(f);
    close(pfd);
    if (unsupported) { puts("error=this kernel can't move one process's memory (needs Linux 5.10 or later)\nunsupported=1"); return 3; }
    usleep(300000);
    status_mem(pid, &rss1, &swap1);
    long long swap_total = mem_get(&m, "SwapTotal"), swap_free = mem_get(&m, "SwapFree");
    printf("ok=1\nname=%s\nrss_before=%lld\nswap_before=%lld\nrss_after=%lld\nswap_after=%lld\nswap_total=%lld\nswap_free=%lld\nregions=%d\nskipped=%d\nadvised=%lld\n",
           st.comm, rss0, swap0, rss1, swap1, swap_total < 0 ? 0 : swap_total, swap_free < 0 ? 0 : swap_free, regions, failed, advised);
    return 0;
}

/* ------------------------------------------------------------------ main */

static void on_term(int sig) { (void)sig; _exit(0); }

int main(int argc, char **argv) {
    int port = 9093, once = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "pageout")) {
            if (i + 1 >= argc || !is_digits(argv[i + 1])) { fputs("usage: nodeyard-agent pageout PID [START]\n", stderr); return 64; }
            unsigned long long start = i + 2 < argc && is_digits(argv[i + 2]) ? strtoull(argv[i + 2], NULL, 10) : 0;
            g_page = sysconf(_SC_PAGESIZE);
            return pageout(atoi(argv[i + 1]), start);
        }
        if (!strcmp(argv[i], "--once")) once = 1;
        else if (i + 1 < argc && !strcmp(argv[i], "--port")) port = atoi(argv[++i]);
        else if (i + 1 < argc && !strcmp(argv[i], "--proc")) g_proc = argv[++i];
        else if (i + 1 < argc && !strcmp(argv[i], "--sys")) g_sys = argv[++i];
        else if (i + 1 < argc && !strcmp(argv[i], "--passwd")) g_passwd = argv[++i];
        else { fprintf(stderr, "agent: unknown option %s\n", argv[i]); return 64; }
    }
    g_page = sysconf(_SC_PAGESIZE);
    g_clk = sysconf(_SC_CLK_TCK);
    if (g_page <= 0) g_page = 4096;
    if (g_clk <= 0) g_clk = 100;
    const char *nn = getenv("NODE_NAME");
    if (nn && *nn) snprintf(g_node, sizeof g_node, "%s", nn); else gethostname(g_node, sizeof g_node - 1);
    read_users();
    find_nvidia_smi();
    char path[512];
    Buf ci = {0};
    snprintf(path, sizeof path, "%s/cpuinfo", g_proc);
    rd_all(path, &ci);
    cpu_model_of(ci.p ? ci.p : "", g_cpu_model, sizeof g_cpu_model);
    free(ci.p);
    signal(SIGPIPE, SIG_IGN);
    signal(SIGCHLD, SIG_DFL);

    if (once) {
        sample(&g_snap);
        sleep(1);
        sample(&g_snap);
        fwrite(g_snap.p, 1, g_snap.n, stdout);
        putchar('\n');
        return 0;
    }
    const char *tok = getenv("AGENT_TOKEN");
    if (!tok || strlen(tok) < 16) { fputs("agent: AGENT_TOKEN is missing or too short\n", stderr); return 1; }
    snprintf(g_token, sizeof g_token, "%s", tok);
    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);

    int ls = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (ls < 0 || bind(ls, (struct sockaddr *)&addr, sizeof addr) < 0 || listen(ls, 64) < 0) {
        fprintf(stderr, "agent: can't listen on :%d: %s\n", port, strerror(errno));
        return 1;
    }
    Buf work = {0};
    sample(&work);
    Buf t = g_snap; g_snap = work; work = t;
    printf("nodeyard agent for %s listening on :%d\n", g_node, port);
    fflush(stdout);
    long long next = mono_ms() + SAMPLE_MS;
    for (;;) {
        long long wait = next - mono_ms();
        struct pollfd p = {ls, POLLIN, 0};
        int r = poll(&p, 1, wait > 0 ? (int)wait : 0);
        if (r > 0 && (p.revents & POLLIN)) {
            int c = accept4(ls, NULL, NULL, SOCK_CLOEXEC);
            if (c >= 0) { serve_conn(c); close(c); }
        }
        if (mono_ms() >= next) {
            sample(&work);
            t = g_snap; g_snap = work; work = t;
            next = mono_ms() + SAMPLE_MS;
        }
    }
}
