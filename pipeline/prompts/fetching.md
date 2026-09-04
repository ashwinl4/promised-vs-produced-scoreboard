## When a source will not load

Roughly a third of cited pages refuse the first attempt — 7 of 19 rows in the last
collection run. This is expected, and it has a fixed procedure. Work down the ladder,
stop at the first step that returns the article text, and record in `flag` which step
you used.

**Do not improvise a fetch.** The last run reinvented this six times with six different
user-agent strings and timeouts, which is most of why iterations doing identical work
ranged from 59 to 343 seconds. The steps below are the ones that actually worked.

**1. `WebFetch` the URL.** The normal case, and the only one most rows need.

**2. HTTP 403, "Forbidden", Cloudflare, Akamai.** The server refused the default client,
not you. Retry with a browser user agent over HTTP/1.1:

```
curl -sSL --http1.1 --max-time 60 \
  -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36" \
  -o scratch/page.html -w "HTTP %{http_code}  %{size_download} bytes\n" "<URL>"
```

`--http1.1` is not decoration: some CDNs reject the HTTP/2 negotiation rather than the
client. Then read `scratch/page.html`.

**3. HTTP 404, page gone.** Usual for governor's-office and state-agency releases, which
are rotated off within a year or two — several of the last run's failures were exactly
that. Ask the Wayback Machine for the nearest snapshot:

```
curl -sS "https://archive.org/wayback/available?url=<URL>"
```

or go straight to a year: `https://web.archive.org/web/2022/<URL>`.

**4. Timeout.** Retry once at step 2. If it times out again, treat it as step 3.

**5. The page loads but carries no article** — a video player, a JavaScript-only shell.
The text is not there to be read. Do **not** infer it from the headline, the URL, or the
lead's summary. Find another source that states the same fact, cite that one, and say so
in `flag`.

**6. Nothing works.** Leave the field empty and say why in `flag`. An empty cell with a
stated reason is data. A guess is not — and in-model recollection is never a substitute
for a source. That rule does not relax because a page is down.

### Citing a page you read through a mirror

Cite the **original URL** in the source column. That is where the claim was published and
what a reader should check. Put the snapshot URL in `flag`, so the reading stays
reproducible — e.g. `read via https://web.archive.org/web/2022…/<URL>; the original 404s`.

Write anything you download into `scratch/` (gitignored), not `/tmp`.
