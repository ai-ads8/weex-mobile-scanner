"""End-to-end tests with mocked WEEX and Telegram (no network)."""
import importlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_engine import make_market  # noqa: E402

TMP = tempfile.mkdtemp()
os.environ.update(SCANNER_DISABLE_WORKER="1", BOT_DISABLE_THREADS="1",
                  SCANNER_DB_PATH=os.path.join(TMP, "scan.db"), DB_PATH=os.path.join(TMP, "bot.db"),
                  TELEGRAM_BOT_TOKEN="123:FAKE", ALERT_CHAT_IDS="111,222",
                  SCAN_SYMBOLS="XRPUSDT,BTCUSDT,RNDRUSDT")


def fake_weex(now):
    markets = {"XRPUSDT": make_market(0.9992, 1.6, now), "BTCUSDT": make_market(0.9999, 1.0, now)}

    def get_json(path, params=None, timeout=10):
        if path == "/ticker/24hr":
            return [{"symbol": s, "lastPrice": str(q["lastPrice"]), "markPrice": str(q["markPrice"]),
                     "closeTime": int(now * 1000)} for s, (_, _, q) in markets.items()]
        if path == "/ticker/bookTicker":
            return [{"symbol": s, "bidPrice": str(q["bidPrice"]), "askPrice": str(q["askPrice"]),
                     "time": int(now * 1000)} for s, (_, _, q) in markets.items()]
        if path == "/klines":
            data = markets[params["symbol"]][0][params["interval"]]
            return [[c["t"], c["o"], c["h"], c["l"], c["c"], c["v"]] for c in data]
        raise AssertionError(path)
    return get_json


class Scanner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.now = (int(time.time()) // 60) * 60 + 4
        cls.app = importlib.import_module("app")

    def setUp(self):
        # never touch the real Telegram API; tests that inspect messages patch again
        self.block = mock.patch.object(self.app.notifier, "send", return_value=True)
        self.block.start()
        with self.app.db() as c:
            c.execute("DELETE FROM alerts_sent")

    def tearDown(self):
        self.block.stop()

    def test_scan_alerts_and_endpoints(self):
        app = self.app
        sent = []
        with mock.patch.object(app, "get_json", side_effect=fake_weex(self.now)), \
                mock.patch("time.time", return_value=self.now), \
                mock.patch.object(app.notifier, "send", side_effect=lambda t, c, m: sent.append((c, m)) or True):
            app.last_quotes = 0
            app.scan_once()
            app.scan_once()   # second scan must not re-alert (dedup)
            c = app.app.test_client()
            st = c.get("/api/status").get_json()
            health = c.get("/api/health").get_json()
        self.assertEqual(st["scannerVersion"], "v6.0")
        self.assertEqual(st["symbols"]["RNDRUSDT"]["status"], "UNAVAILABLE")
        self.assertIn("RNDRUSDT", health["unavailableSymbols"])
        xrp = [s for s in st["setups"] if s["symbol"] == "XRPUSDT" and s["mode"] == "SCALP"]
        self.assertTrue(xrp, st.get("rejects"))
        self.assertIn("live", xrp[0])
        alerts = [m for _, m in sent if "XRPUSDT" in m]
        self.assertEqual(xrp[0]["quality"], "A")
        self.assertEqual(len(alerts), 2)               # one per chat, and only once over two scans
        self.assertIn("SCALP", alerts[0])
        self.assertIn("Invalidatie", alerts[0])
        self.assertGreaterEqual(st["metrics"]["tracked"], 1)
        r = c.get("/api/positions")
        self.assertEqual(r.status_code, 403)           # tracking disabled without APP_ACCESS_TOKEN

    def test_kline_cache_reduces_calls(self):
        app = self.app
        app.kline_cache.clear()
        calls = []
        fj = fake_weex(self.now)

        def counting(path, params=None, timeout=10):
            calls.append(path)
            return fj(path, params, timeout)
        with mock.patch.object(app, "get_json", side_effect=counting), mock.patch("time.time", return_value=self.now):
            app.last_quotes = 0
            app.scan_once()
            first = calls.count("/klines")
            app.scan_once()
            second = calls.count("/klines") - first
        self.assertEqual(first, 2 * 7)
        self.assertEqual(second, 0)   # nothing new has closed at the same timestamp

    def test_trade_analyzer_endpoint(self):
        app = self.app
        with mock.patch.object(app, "get_json", side_effect=fake_weex(self.now)), mock.patch("time.time", return_value=self.now):
            app.last_quotes = 0
            app.scan_once()
            c = app.app.test_client()
            mark = c.get("/api/status").get_json()["symbols"]["XRPUSDT"]["quote"]["markPrice"]
            r = c.get(f"/api/trade-analyzer?symbol=XRPUSDT&side=SHORT&entry={mark}&margin=12&leverage=156")
            bad = c.get("/api/trade-analyzer?symbol=XRPUSDT&side=SHORT&entry=-1&margin=12&leverage=156")
            page = c.get("/trade")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["status"], "REDUCE EXPOSURE")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(page.status_code, 200)


class Bot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = importlib.import_module("telegram_bot")

    def setUp(self):
        self.sent = []
        self.p = mock.patch.object(self.bot, "send", side_effect=lambda chat, text: self.sent.append((chat, text)) or True)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def setup_dict(self, sid, quality, age, score=80, mode="SCALP"):
        now = time.time()
        return {"id": sid, "symbol": "XRPUSDT", "side": "SHORT", "mode": mode, "entry": 1.0, "stop": 1.01,
                "tp1": 0.98, "tp2": 0.97, "score": score, "quality": quality, "candleCloseAt": now - age,
                "createdAt": now - age, "maxSignalAgeSec": 90 if mode == "SCALP" else 420,
                "quote": {"lastPrice": 1.0}, "status": "PAPER_OPEN"}

    def test_seeded_subscribers_survive_restart(self):
        self.assertEqual(sorted(self.bot.subscribers()), [111, 222])

    def test_notify_filters_quality_and_freshness_and_dedups(self):
        payload = {"setups": [self.setup_dict("a", "A", 10), self.setup_dict("b", "B", 10, score=97),
                              self.setup_dict("c", "A", 400)]}
        self.bot.notify(payload)
        self.bot.notify(payload)
        ids = [t for _, t in self.sent]
        self.assertEqual(len(ids), 2)                 # only fresh A, once per chat
        self.assertTrue(all("Kwaliteit A" in t for t in ids))

    def test_webhook_security_and_duplicate_updates(self):
        c = self.bot.app.test_client()
        self.assertEqual(c.post(self.bot.PATH, json={}).status_code, 403)
        h = {"X-Telegram-Bot-Api-Secret-Token": self.bot.SECRET}
        upd = {"update_id": 42, "message": {"chat": {"id": 5}, "text": "/test"}}
        self.assertEqual(c.post(self.bot.PATH, json=upd, headers=h).status_code, 200)
        self.assertEqual(c.post(self.bot.PATH, json=upd, headers=h).get_json().get("duplicate"), True)
        self.assertEqual(len(self.sent), 1)

    def test_scan_sorted_by_quality(self):
        with self.bot.LOCK:
            self.bot.STATE.update(lastScan=time.time(), error=None, symbols={"XRPUSDT": {"status": "OK"}},
                                  setups=[self.setup_dict("x1", "B", 5, 99), self.setup_dict("x2", "A", 20, 70),
                                          self.setup_dict("x3", "A", 300, 99)])
        self.bot.process_message({"chat": {"id": 7}, "text": "/scan"})
        text = self.sent[-1][1]
        self.assertLess(text.index("Kwaliteit A"), text.index("Kwaliteit B"))
        self.assertIn("1 verlopen", text)

    def test_status_when_scanner_offline(self):
        with self.bot.LOCK:
            self.bot.STATE.update(lastScan=time.time() - 999, error="ConnectionError: x")
        self.bot.process_message({"chat": {"id": 7}, "text": "/scan"})
        self.assertIn("Geen actuele scannerdata", self.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
