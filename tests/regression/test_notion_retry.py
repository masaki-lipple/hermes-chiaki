import os, sys, types, urllib.error
os.environ["NOTION_INTEGRATION_TOKEN"] = "tok"
sys.path.insert(0, "/Users/malus_bot/Claude/Hermes")
from lib import notion

sleeps = []
notion.time = types.SimpleNamespace(sleep=lambda s: sleeps.append(s))
calls = {"n": 0}
class FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return b'{"ok": true}'

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)
    ok += 1

# 1. DNS失敗→2回目で成功
def seq_fail_then_ok(req, timeout=30):
    calls["n"] += 1
    if calls["n"] == 1:
        raise urllib.error.URLError("dns down")
    return FakeResp()
notion.urllib.request.urlopen = seq_fail_then_ok
sleeps.clear(); calls["n"] = 0
check("transient recovers", notion._api("POST", "databases/x/query", {}) == {"ok": True})
check("slept 8s", sleeps == [8])

# 2. 404は即諦め（リトライしない）
def always_404(req, timeout=30):
    calls["n"] += 1
    raise urllib.error.HTTPError("u", 404, "nf", {}, None)
notion.urllib.request.urlopen = always_404
sleeps.clear(); calls["n"] = 0
check("404 no retry", notion._api("GET", "pages/x") is None and calls["n"] == 1 and sleeps == [])

# 3. 恒久的DNS障害→2回リトライして諦め
def always_dns(req, timeout=30):
    calls["n"] += 1
    raise urllib.error.URLError("dns down")
notion.urllib.request.urlopen = always_dns
sleeps.clear(); calls["n"] = 0
check("permanent fail gives up after retries", notion._api("POST", "databases/x/query", {}) is None
      and calls["n"] == 3 and sleeps == [8, 20])

# 4. 429はリトライ対象
def seq_429_then_ok(req, timeout=30):
    calls["n"] += 1
    if calls["n"] == 1:
        raise urllib.error.HTTPError("u", 429, "rate", {}, None)
    return FakeResp()
notion.urllib.request.urlopen = seq_429_then_ok
sleeps.clear(); calls["n"] = 0
check("429 retried", notion._api("POST", "databases/x/query", {}) == {"ok": True})

print(f"\n{ok} checks passed")
