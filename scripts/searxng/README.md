# Local SearXNG for SHAMS web research

SHAMS research tools read SearXNG's JSON results. Run SearXNG on this PC and point SHAMS at it:

```json
"research": {"enabled": true,
             "search_url": "http://127.0.0.1:8888/search?q={query}&format=json&engines=google"}
```

The configured search host is the only local address the research tools may reach, and only for searches.

## Docker

```powershell
$dir = "$env:USERPROFILE\.shams\searxng"; New-Item -ItemType Directory -Force $dir | Out-Null
Copy-Item scripts\searxng\settings.yml $dir\settings.yml
(Get-Content $dir\settings.yml) -replace 'CHANGE_ME_AT_INSTALL', [guid]::NewGuid().ToString('N') | Set-Content $dir\settings.yml
docker run -d --name shams-searxng --restart unless-stopped -p 127.0.0.1:8888:8080 `
  -v "${dir}:/etc/searxng" searxng/searxng
```

In the container SearXNG listens on 8080; the `-p` mapping publishes it on 127.0.0.1:8888 only.
Check: `curl "http://127.0.0.1:8888/search?q=three.js&format=json&engines=google"` returns JSON with `results`.
Google may still rate-limit SearXNG; add `&engines=google,bing` in `search_url` for a fallback.
