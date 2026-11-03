import contextlib
import http.server
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import generate as g  # noqa: E402


def channel(cid, title, genre="electronic", listeners=1, mp3=(128,)):
    playlists = []
    for rate in mp3:
        name = cid if rate == 128 else f"{cid}{rate}"
        playlists.append({"url": f"https://api.somafm.com/{name}.pls", "format": "mp3",
                          "quality": "highest" if rate == max(mp3) else "high"})
    playlists.append({"url": f"https://api.somafm.com/{cid}130.pls", "format": "aac", "quality": "highest"})
    playlists.append({"url": f"https://api.somafm.com/{cid}64.pls", "format": "aacp", "quality": "high"})
    return {"id": cid, "title": title, "genre": genre, "listeners": str(listeners), "playlists": playlists}


def cfg(**kw):
    c = dict(g.DEFAULTS)
    c.update(min_channels=0)
    c.update(kw)
    return c


def sample(n=5):
    chans = [channel(f"ch{i}", f"Channel {i}", listeners=i) for i in range(n)]
    return {"channels": chans}


class BitrateTests(unittest.TestCase):
    def test_bitrates_come_from_mp3_playlist_names(self):
        ch = channel("groovesalad2", "Groove Salad 2", mp3=(256, 128))
        self.assertEqual(g.mp3_bitrates(ch), {128, 256})

    def test_aac_playlists_are_ignored(self):
        ch = channel("dz2", "Drone Zone 2", mp3=(128,))
        self.assertEqual(g.mp3_bitrates(ch), {128})

    def test_ids_ending_in_digits(self):
        self.assertEqual(g.mp3_bitrates(channel("sf1033", "SF 10-33", mp3=(128,))), {128})
        self.assertEqual(g.mp3_bitrates(channel("seventies", "70s", mp3=(320, 128))), {128, 320})

    def test_pick_bitrate(self):
        self.assertEqual(g.pick_bitrate({128, 256}, 256), 256)
        self.assertEqual(g.pick_bitrate({128, 192}, 256), 192)  # closest lower
        self.assertEqual(g.pick_bitrate({256}, 128), 256)       # nothing lower: lowest offered
        self.assertIsNone(g.pick_bitrate(set(), 128))

    def test_pick_bitrate_max(self):
        self.assertEqual(g.pick_bitrate({128, 256, 320}, "max"), 320)
        self.assertEqual(g.pick_bitrate({128}, "max"), 128)
        self.assertIsNone(g.pick_bitrate(set(), "max"))


class EntryTests(unittest.TestCase):
    def test_stream_url_and_overrides(self):
        data = {"channels": [channel("a", "A", mp3=(256, 128)), channel("b", "B", mp3=(256, 128))]}
        c = cfg(bitrate_overrides={"a": 256})
        entries, skipped = g.build_entries(data["channels"], c)
        self.assertEqual(skipped, [])
        self.assertEqual(entries[0]["url"], "https://ice1.somafm.com/a-256-mp3")
        self.assertEqual(entries[1]["url"], "https://ice1.somafm.com/b-128-mp3")

    def test_max_as_default_and_as_override(self):
        chans = [channel("a", "A", mp3=(320, 128)), channel("b", "B", mp3=(256, 128)),
                 channel("c", "C", mp3=(128,)), channel("d", "D", mp3=(256, 128))]
        entries, _ = g.build_entries(chans, cfg(default_bitrate="max", bitrate_overrides={"d": 128}))
        self.assertEqual([e["bitrate"] for e in entries], [320, 256, 128, 128])
        entries, _ = g.build_entries(chans, cfg(default_bitrate=128, bitrate_overrides={"b": "max"}))
        self.assertEqual([e["bitrate"] for e in entries], [128, 256, 128, 128])

    def test_exclude_by_id_and_genre(self):
        chans = [channel("a", "A"), channel("b", "B", genre="holiday"),
                 channel("c", "C", genre="live/news"), channel("d", "D", genre="x|spoken")]
        c = cfg(exclude=["a"], exclude_genres=["Holiday", "news", "spoken"])
        entries, _ = g.build_entries(chans, c)
        self.assertEqual(entries, [])

    def test_channel_without_mp3_is_skipped(self):
        ch = {"id": "x", "title": "X", "playlists": [{"url": "https://api.somafm.com/x130.pls", "format": "aac"}]}
        entries, skipped = g.build_entries([ch], cfg())
        self.assertEqual((entries, skipped), ([], ["x"]))


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.entries, _ = g.build_entries([
            channel("b", "Beta", listeners=5), channel("a", "alpha", listeners=1),
            channel("c", "Gamma", listeners=9), channel("d", "Delta", listeners=5),
        ], cfg())

    def ids(self, mode, existing=()):
        return [e["id"] for e in g.order_entries(self.entries, mode, list(existing))]

    def test_listeners(self):
        self.assertEqual(self.ids("listeners"), ["c", "b", "d", "a"])  # ties by title

    def test_alpha_is_case_insensitive(self):
        self.assertEqual(self.ids("alpha"), ["a", "b", "d", "c"])

    def test_api_order(self):
        self.assertEqual(self.ids("api"), ["b", "a", "c", "d"])

    def test_keep_preserves_existing_and_appends_new_by_popularity(self):
        self.assertEqual(self.ids("keep", ["d", "gone", "a"]), ["d", "a", "c", "b"])

    def test_keep_without_existing_falls_back_to_listeners(self):
        self.assertEqual(self.ids("keep"), self.ids("listeners"))


class DiffTests(unittest.TestCase):
    def old(self, cid, title, rate=128, host="https://ice1.somafm.com"):
        return {"id": cid, "title": title, "bitrate": rate, "url": f"{host}/{cid}-{rate}-mp3"}

    def new(self, cid, title, rate=128, host="https://ice1.somafm.com"):
        return {"id": cid, "title": title, "bitrate": rate, "url": f"{host}/{cid}-{rate}-mp3",
                "listeners": 0}

    def test_all_categories(self):
        old = [self.old("a", "Alpha"), self.old("b", "Beta"), self.old("c", "Gamma"),
               self.old("d", "Delta"), self.old("e", "Cafe")]
        new = [self.new("a", "Alpha"), self.new("c", "Gamma 2"), self.new("d", "Delta", 256),
               self.new("e", "Caf\u00e9", host="http://ice2.somafm.com"), self.new("f", "Fresh")]
        ch = g.diff_entries(old, new)
        self.assertEqual([e["id"] for e in ch.added], ["f"])
        self.assertEqual([e["id"] for e in ch.removed], ["b"])
        self.assertEqual([n["id"] for _, n in ch.renamed], ["c"])   # "Cafe" vs "Caf\u00e9": not a rename
        self.assertEqual([n["id"] for _, n in ch.bitrate], ["d"])
        self.assertEqual([n["id"] for _, n in ch.address], ["e"])
        self.assertTrue(ch)

    def test_no_changes(self):
        self.assertFalse(g.diff_entries([self.old("a", "Alpha")], [self.new("a", "Alpha")]))

    def test_read_existing_roundtrip(self):
        entries, _ = g.build_entries([channel("a", "A", mp3=(256, 128))], cfg(default_bitrate="max"))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.m3u"
            path.write_bytes(g.render_all(entries, "x")["x.m3u"])
            back = g.read_existing(path)
        self.assertEqual(back, [{"id": "a", "title": "A", "bitrate": 256,
                                 "url": "https://ice1.somafm.com/a-256-mp3"}])
        self.assertFalse(g.diff_entries(back, entries))

    def test_read_existing_missing_file(self):
        self.assertEqual(g.read_existing(Path("/nonexistent/none.m3u")), [])


class RenderTests(unittest.TestCase):
    def setUp(self):
        data = {"channels": [channel("a", "Caf\u00e9 \u2019Zone\u2019", listeners=3),
                             channel("b", "Plain", listeners=2)]}
        self.res = g.generate(data, cfg(), [])

    def test_ascii_for_m3u_and_pls_unicode_kept_elsewhere(self):
        f = self.res.files
        self.assertIn("Cafe 'Zone'", f["SomaFM.m3u"].decode("ascii"))
        self.assertIn("Cafe 'Zone'", f["SomaFM.pls"].decode("ascii"))
        self.assertIn("Caf\u00e9 \u2019Zone\u2019", f["SomaFM.m3u8"].decode("utf-8"))
        self.assertIn("Caf\u00e9 \u2019Zone\u2019", f["SomaFM.aimppl4"].decode("utf-16"))

    def test_encodings_and_line_endings(self):
        f = self.res.files
        self.assertFalse(f["SomaFM.m3u8"].startswith(b"\xef\xbb\xbf"))
        self.assertTrue(f["SomaFM.aimppl4"].startswith(b"\xff\xfe"))
        for name in ("SomaFM.m3u", "SomaFM.pls"):
            self.assertTrue(f[name].endswith(b"\r\n"))
            self.assertNotIn(b"\n", f[name].replace(b"\r\n", b""))

    def test_all_formats_describe_the_same_list(self):
        f = self.res.files
        urls = [e["url"] for e in self.res.entries]
        m3u = [l for l in f["SomaFM.m3u"].decode().splitlines() if l.startswith("http")]
        pls = [l.split("=", 1)[1] for l in f["SomaFM.pls"].decode().splitlines() if l.startswith("File")]
        aimp_text = f["SomaFM.aimppl4"].decode("utf-16")
        aimp = [l.split("|")[0] for l in aimp_text.splitlines() if l.startswith("http")]
        self.assertEqual(m3u, urls)
        self.assertEqual(pls, urls)
        self.assertEqual(aimp, urls)
        self.assertIn("NumberOfEntries=2", f["SomaFM.pls"].decode())
        self.assertIn("ContentFiles=2", aimp_text)

    def test_pipe_in_title_does_not_break_aimp(self):
        self.assertEqual(g.clean_title("A | B\n C"), "A - B C")


class SanityTests(unittest.TestCase):
    def test_too_few_channels(self):
        with self.assertRaises(g.GenerationError):
            g.generate(sample(3), cfg(min_channels=10), [])

    def test_unexpected_format(self):
        with self.assertRaises(g.GenerationError):
            g.generate({"stations": []}, cfg(), [])

    def test_many_channels_without_mp3(self):
        chans = [{"id": f"x{i}", "title": "X", "playlists": []} for i in range(30)]
        with self.assertRaises(g.GenerationError):
            g.generate({"channels": chans}, cfg(), [])

    def test_big_drop_against_existing_list(self):
        with self.assertRaises(g.GenerationError):
            g.generate(sample(5), cfg(), [{"id": f"old{i}", "title": "Old", "bitrate": 128, "url": ""}
                                          for i in range(20)])

    def test_bad_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"scheme": "ftp"}')
            with self.assertRaises(g.GenerationError):
                g.load_config(p)
            p.write_text('{"typo_option": 1}')
            with self.assertRaises(g.GenerationError):
                g.load_config(p)
            for bad in ('"best"', "0", "true", "[128]"):
                p.write_text('{"default_bitrate": %s}' % bad)
                with self.assertRaises(g.GenerationError, msg=bad):
                    g.load_config(p)
            p.write_text('{"bitrate_overrides": {"a": "fast"}}')
            with self.assertRaises(g.GenerationError):
                g.load_config(p)
            p.write_text('{"default_bitrate": "max", "bitrate_overrides": {"a": "max", "b": 192}}')
            self.assertEqual(g.load_config(p)[0]["default_bitrate"], "max")


class MainTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = g.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_write_then_idempotent_and_keep_order(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "channels.json"
            src.write_text(json.dumps(sample(25)))
            conf = d / "config.json"
            conf.write_text("{}")
            out = d / "out"
            code, text, _ = self.run_main("--config", str(conf), "--input", str(src), "--output-dir", str(out))
            self.assertEqual(code, 0)
            self.assertIn("+25 added", text)
            first = {p.name: p.read_bytes() for p in out.iterdir()}
            self.assertEqual(sorted(first), ["SomaFM.aimppl4", "SomaFM.m3u", "SomaFM.m3u8", "SomaFM.pls"])

            code, text, _ = self.run_main("--config", str(conf), "--input", str(src), "--output-dir", str(out))
            self.assertIn("+0 added, -0 removed", text)
            self.assertEqual(text.count("unchanged"), 4)
            self.assertEqual(first, {p.name: p.read_bytes() for p in out.iterdir()})

            # listener counts change: "keep" must not reshuffle the playlist
            data = sample(25)
            for ch in data["channels"]:
                ch["listeners"] = str(100 - int(ch["listeners"]))
            data["channels"].append(channel("fresh", "Fresh", listeners=1000))
            src.write_text(json.dumps(data))
            code, text, _ = self.run_main("--config", str(conf), "--input", str(src), "--output-dir", str(out))
            self.assertIn("+1 added", text)
            ids = g.read_existing_ids(out / "SomaFM.m3u")
            self.assertEqual(ids[:25], [f"ch{n}" for n in sorted(range(25), key=lambda n: -n)])
            self.assertEqual(ids[-1], "fresh")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "channels.json"
            src.write_text(json.dumps(sample(25)))
            conf = d / "config.json"
            conf.write_text("{}")
            code, text, _ = self.run_main("--config", str(conf), "--input", str(src),
                                          "--output-dir", str(d / "out"), "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("dry run", text)
            self.assertFalse((d / "out").exists())

    def test_failure_leaves_existing_files_alone(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "channels.json"
            src.write_text(json.dumps(sample(25)))
            conf = d / "config.json"
            conf.write_text("{}")
            out = d / "out"
            self.run_main("--config", str(conf), "--input", str(src), "--output-dir", str(out))
            before = {p.name: p.read_bytes() for p in out.iterdir()}
            src.write_text(json.dumps({"channels": []}))
            code, _, err = self.run_main("--config", str(conf), "--input", str(src), "--output-dir", str(out))
            self.assertEqual(code, 1)
            self.assertIn("error:", err)
            self.assertEqual(before, {p.name: p.read_bytes() for p in out.iterdir()})

    def test_release_notes(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, conf, out, notes = d / "c.json", d / "config.json", d / "out", d / "notes.md"
            conf.write_text("{}")
            args = ["--config", str(conf), "--input", str(src), "--output-dir", str(out),
                    "--notes-file", str(notes)]

            def data(*chans):
                src.write_text(json.dumps({"channels": list(chans)}))

            fixed = [channel(f"k{i}", f"Keep {i}", listeners=i) for i in range(24)]
            data(*fixed, channel("a", "Alpha", mp3=(256, 128)), channel("b", "Beta"))
            self.run_main(*args)
            first = notes.read_text()
            self.assertIn("26 channels", first)
            self.assertIn("**Added**", first)
            self.assertIn("- Alpha (`a`), 128 kbps", first)
            self.assertIn("Updated files: SomaFM.m3u", first)

            notes.unlink()
            self.run_main(*args)                       # nothing changed
            self.assertFalse(notes.exists())

            conf.write_text('{"default_bitrate": "max"}')
            data(*fixed, channel("a", "Alpha Prime", mp3=(256, 128)), channel("c", "Gamma"))
            self.run_main(*args)
            text = notes.read_text()
            self.assertIn("**Added**\n- Gamma (`c`), 128 kbps", text)
            self.assertIn("**Removed**\n- Beta (`b`)", text)
            self.assertIn("**Renamed**\n- Alpha \u2192 Alpha Prime (`a`)", text)
            self.assertIn("**Bitrate changed**\n- Alpha Prime (`a`): 128 \u2192 256 kbps", text)

    def test_notes_when_only_formatting_changed(self):
        entries, _ = g.build_entries([channel("a", "A")], cfg())
        res = g.Result(entries=entries, changes=g.diff_entries([{"id": "a", "title": "A", "bitrate": 128,
                       "url": entries[0]["url"]}], entries))
        text = g.render_release_notes(res, ["SomaFM.pls"], "2026-11-09")
        self.assertIn("No channels were added, removed or renamed", text)
        self.assertIn("2026-11-09", text)

    def test_github_step_summary(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "channels.json"
            src.write_text(json.dumps(sample(25)))
            conf = d / "config.json"
            conf.write_text("{}")
            summary = d / "summary.md"
            os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
            try:
                self.run_main("--config", str(conf), "--input", str(src), "--output-dir", str(d / "out"))
            finally:
                del os.environ["GITHUB_STEP_SUMMARY"]
            self.assertIn("25 channels", summary.read_text())


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            self.wfile.write(b"\x00" * 2048)
        elif self.path == "/empty":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        # tiny server that answers like old Shoutcast: "ICY 200 OK"
        cls.icy = socket.socket()
        cls.icy.bind(("127.0.0.1", 0))
        cls.icy.listen(5)
        cls.icy_port = cls.icy.getsockname()[1]

        def serve_icy():
            while True:
                try:
                    conn, _ = cls.icy.accept()
                except OSError:
                    return
                conn.recv(4096)
                conn.sendall(b"ICY 200 OK\r\nicy-name: test\r\n\r\n" + b"\x00" * 1024)
                conn.close()

        threading.Thread(target=serve_icy, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.icy.close()

    def test_ok(self):
        self.assertTrue(g.probe(f"http://127.0.0.1:{self.port}/ok")[0])

    def test_not_found(self):
        self.assertEqual(g.probe(f"http://127.0.0.1:{self.port}/nope"), (False, "HTTP 404"))

    def test_empty_body(self):
        self.assertFalse(g.probe(f"http://127.0.0.1:{self.port}/empty")[0])

    def test_icy_status_line_counts_as_ok(self):
        self.assertTrue(g.probe(f"http://127.0.0.1:{self.icy_port}/stream")[0])

    def test_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(g.probe(f"http://127.0.0.1:{port}/x", timeout=2)[0])

    def test_check_streams_reports_only_failures(self):
        entries = [{"id": "ok", "title": "OK", "url": f"http://127.0.0.1:{self.port}/ok"},
                   {"id": "bad", "title": "Bad", "url": f"http://127.0.0.1:{self.port}/nope"}]
        failed = g.check_streams(entries)
        self.assertEqual([e["id"] for e, _ in failed], ["bad"])


if __name__ == "__main__":
    unittest.main()
