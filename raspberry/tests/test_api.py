"""Contract 4 REST API tests (Flask test_client when installed + static route & index.html parity)."""

import ast
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

RASPBERRY_DIR = Path(__file__).resolve().parents[1]
if str(RASPBERRY_DIR) not in sys.path:
    sys.path.insert(0, str(RASPBERRY_DIR))

try:
    import flask  # noqa: F401

    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

from i2c_master.controller import FeederI2CError, FeederMaster
from i2c_master.transport import MockArduinoTransport
from service import database as db
from service.camera import CameraBusyError, CameraError, CameraManager
from service.client_sync import ThreadSafeFeederMaster
from service.worker import FeederWorkerThread


class TestApiAndWebContractStaticParity(unittest.TestCase):
    """Always-run checks (no Flask required): api.py AST routes, index.html parity, CLI & systemd."""

    def setUp(self) -> None:
        self.api_path = RASPBERRY_DIR / "service" / "api.py"
        self.html_path = RASPBERRY_DIR / "service" / "web" / "index.html"
        self.unit_path = RASPBERRY_DIR / "pet-feeder.service"

    def _extract_flask_routes(self) -> set:
        tree = ast.parse(self.api_path.read_text(encoding="utf-8"), filename=str(self.api_path))
        routes = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if (
                        isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "route"
                        and dec.args
                        and isinstance(dec.args[0], ast.Constant)
                    ):
                        path = dec.args[0].value
                        methods = ["GET"]
                        for kw in dec.keywords:
                            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                                methods = [elt.value for elt in kw.value.elts if isinstance(elt, ast.Constant)]
                        for m in methods:
                            routes.add((m, path))
        return routes

    def test_api_compiles_and_exposes_all_contract4_routes(self) -> None:
        source = self.api_path.read_text(encoding="utf-8")
        compile(source, str(self.api_path), "exec")

        routes = self._extract_flask_routes()
        expected = {
            ("GET", "/"),
            ("GET", "/api/status"),
            ("GET", "/api/config"),
            ("PUT", "/api/config"),
            ("GET", "/api/schedules"),
            ("POST", "/api/schedules"),
            ("PUT", "/api/schedules/<int:schedule_id>"),
            ("DELETE", "/api/schedules/<int:schedule_id>"),
            ("POST", "/api/dispense"),
            ("POST", "/api/unjam"),
            ("POST", "/api/stop"),
            ("GET", "/api/history"),
            ("GET", "/api/media"),
            ("POST", "/api/media/capture"),
            ("GET", "/media/<path:filename>"),
            ("DELETE", "/api/media/<int:media_id>"),
            ("POST", "/api/mock/ir"),
        }
        self.assertEqual(routes, expected)

    def test_index_html_matches_api_routes_methods_and_json_fields(self) -> None:
        html = self.html_path.read_text(encoding="utf-8")
        routes = self._extract_flask_routes()

        # Verify every api("...") / api(`...`) call in index.html maps to an exact api.py route & method
        call_re = re.compile(r'api\(\s*(["`])([^"`]+)\1(?:\s*,\s*\{([^}]*)\})?\s*\)')
        calls = call_re.findall(html)
        self.assertGreaterEqual(len(calls), 12)

        def normalize_url(url: str) -> str:
            base = url.split("?", 1)[0]
            base = re.sub(r"/api/schedules/\$\{[^}]+\}", "/api/schedules/<int:schedule_id>", base)
            base = re.sub(r"/api/media/\$\{[^}]+\}", "/api/media/<int:media_id>", base)
            base = re.sub(r"\$\{qs\}$", "", base)
            return base

        for _quote, raw_url, opts in calls:
            method_match = re.search(r'method:\s*"([A-Z]+)"', opts)
            method = method_match.group(1) if method_match else "GET"
            norm = normalize_url(raw_url)
            self.assertIn(
                (method, norm),
                routes,
                f"index.html calls {method} {raw_url!r} (normalized {norm!r}) not found in api.py routes",
            )

        # Config form fields in index.html must match database.DEFAULT_SETTINGS (all except last_interval_run_at)
        expected_form_keys = set(db.DEFAULT_SETTINGS.keys()) - {"last_interval_run_at"}
        form_names = set(re.findall(r'<(?:input|select)[^>]+name="([a-z0-9_]+)"', html))
        self.assertEqual(form_names, expected_form_keys)

        # Response fields referenced in index.html match Contract 4
        for token in (
            "data.mode",
            "a.connected",
            "a.motor_busy",
            "a.steps_remaining",
            "a.ir_live",
            "w.next_schedule",
            "c.backend",
            "c.capturing",
            "data.server_time",
            "res.arduino_synced",
            "res.steps_cancelled",
            "res.wiggle_steps",
            "res.item.filename",
            "item.media_type",
            "item.url",
            "item.trigger",
        ):
            self.assertIn(token, html, f"Expected UI token {token!r} missing from index.html")

    def test_systemd_service_flags_match_run_service(self) -> None:
        unit = self.unit_path.read_text(encoding="utf-8")
        run_src = (RASPBERRY_DIR / "run_service.py").read_text(encoding="utf-8")
        compile(run_src, "run_service.py", "exec")

        self.assertIn("After=network-online.target time-sync.target", unit)
        self.assertIn("SupplementaryGroups=i2c video", unit)
        for flag in ("--bus", "--addr", "--db", "--media-dir", "--host", "--port"):
            self.assertIn(flag, unit)
            self.assertIn(flag, run_src)


@unittest.skipUnless(HAS_FLASK, "flask is not installed in this Python environment")
class TestFlaskApiContract4(unittest.TestCase):
    """Full Contract 4 HTTP test suite using Flask test_client()."""

    def setUp(self) -> None:
        from service.api import create_app

        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.db_path = str(self.root / "feeder.db")
        self.media_dir = self.root / "media"
        db.init_db(self.db_path)

        self.clock_t = 100.0
        self.transport = MockArduinoTransport(clock=lambda: self.clock_t)
        self.master = FeederMaster(transport=self.transport, delay=0.0)
        self.client = ThreadSafeFeederMaster(self.master)
        self.camera = CameraManager(self.media_dir, mock=True)
        self.worker = FeederWorkerThread(self.client, self.db_path, self.camera, poll_interval=0.5)
        self.app = create_app(self.client, self.db_path, self.camera, worker=self.worker)
        self.http = self.app.test_client()

    def tearDown(self) -> None:
        self.worker.stop(wait_captures=True)
        self._tmpdir.cleanup()

    def test_index_and_status(self) -> None:
        r_idx = self.http.get("/")
        self.assertEqual(r_idx.status_code, 200)
        self.assertIn(b"Dispenser", r_idx.data)

        r_st = self.http.get("/api/status")
        self.assertEqual(r_st.status_code, 200)
        body = r_st.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["mode"], "mock")
        self.assertTrue(body["arduino"]["connected"])
        self.assertEqual(body["camera"]["backend"], "mock")
        self.assertIn("server_time", body)
        self.assertIn("worker", body)

        # Disconnected Arduino still returns 200 with connected=False
        with mock.patch.object(self.client, "get_status", side_effect=FeederI2CError("offline")):
            r_off = self.http.get("/api/status")
            self.assertEqual(r_off.status_code, 200)
            self.assertFalse(r_off.get_json()["arduino"]["connected"])

    def test_config_get_put_and_validation(self) -> None:
        r_get = self.http.get("/api/config")
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.get_json()["config"]["steps_per_portion"], 4096)

        # Motor setting update syncs Arduino
        r_put = self.http.put("/api/config", json={"steps_per_portion": 2048, "interval_enabled": True})
        self.assertEqual(r_put.status_code, 200)
        data = r_put.get_json()
        self.assertTrue(data["arduino_synced"])
        self.assertEqual(data["config"]["steps_per_portion"], 2048)
        self.assertTrue(data["config"]["last_interval_run_at"])

        # Non-motor update does not force sync
        r_put2 = self.http.put("/api/config", json={"ir_cooldown_s": 60})
        self.assertEqual(r_put2.status_code, 200)
        self.assertFalse(r_put2.get_json()["arduino_synced"])

        # Invalid body / out of range -> 400
        self.assertEqual(self.http.put("/api/config", json={}).status_code, 400)
        self.assertEqual(self.http.put("/api/config", json={"steps_per_portion": 10}).status_code, 400)

    def test_schedules_crud_and_errors(self) -> None:
        r_create = self.http.post("/api/schedules", json={"time_hhmm": "08:30", "portions": 2, "enabled": True})
        self.assertEqual(r_create.status_code, 201)
        sid = r_create.get_json()["schedule"]["id"]

        self.assertEqual(len(self.http.get("/api/schedules").get_json()["schedules"]), 1)

        r_upd = self.http.put(f"/api/schedules/{sid}", json={"portions": 3, "enabled": False})
        self.assertEqual(r_upd.status_code, 200)
        self.assertEqual(r_upd.get_json()["schedule"]["portions"], 3)
        self.assertFalse(r_upd.get_json()["schedule"]["enabled"])

        # 400 / 404 cases
        self.assertEqual(self.http.post("/api/schedules", json={"time_hhmm": "99:99", "portions": 1}).status_code, 400)
        self.assertEqual(self.http.post("/api/schedules", json={"time_hhmm": "09:00"}).status_code, 400)
        self.assertEqual(self.http.put("/api/schedules/99999", json={"portions": 1}).status_code, 404)
        self.assertEqual(self.http.put(f"/api/schedules/{sid}", json={"time_hhmm": None}).status_code, 400)

        r_del = self.http.delete(f"/api/schedules/{sid}")
        self.assertEqual(r_del.status_code, 200)
        self.assertEqual(r_del.get_json()["deleted_id"], sid)
        self.assertEqual(self.http.delete(f"/api/schedules/{sid}").status_code, 404)

    def test_dispense_unjam_stop_busy_and_i2c_errors(self) -> None:
        r_disp = self.http.post("/api/dispense", json={"portions": 1})
        self.assertEqual(r_disp.status_code, 200)
        self.assertEqual(r_disp.get_json()["steps"], 4096)

        # Motor is now busy -> 409 on second dispense & unjam
        self.assertEqual(self.http.post("/api/dispense", json={"steps": 512}).status_code, 409)
        self.assertEqual(self.http.post("/api/unjam", json={}).status_code, 409)

        # Stop cancels motion -> 200
        r_stop = self.http.post("/api/stop", json={})
        self.assertEqual(r_stop.status_code, 200)
        self.assertGreater(r_stop.get_json()["steps_cancelled"], 0)

        # Unjam now succeeds -> 200
        r_unjam = self.http.post("/api/unjam", json={"wiggle_steps": 256})
        self.assertEqual(r_unjam.status_code, 200)
        self.assertEqual(r_unjam.get_json()["wiggle_steps"], 256)
        self.http.post("/api/stop", json={})

        # Invalid arguments -> 400
        self.assertEqual(self.http.post("/api/dispense", json={"portions": 0}).status_code, 400)
        self.assertEqual(self.http.post("/api/dispense", json={"steps": 0}).status_code, 400)
        self.assertEqual(self.http.post("/api/unjam", json={"wiggle_steps": 10}).status_code, 400)

        # I2C failure -> 503
        with mock.patch.object(self.client, "dispense_steps", side_effect=FeederI2CError("bus down")):
            self.assertEqual(self.http.post("/api/dispense", json={"portions": 1}).status_code, 503)
        with mock.patch.object(self.client, "unjam", side_effect=FeederI2CError("bus down")):
            self.assertEqual(self.http.post("/api/unjam", json={}).status_code, 503)
        with mock.patch.object(self.client, "stop_motor", side_effect=FeederI2CError("bus down")):
            self.assertEqual(self.http.post("/api/stop", json={}).status_code, 503)

        # History reflects events
        r_hist = self.http.get("/api/history?limit=10&offset=0")
        self.assertEqual(r_hist.status_code, 200)
        self.assertGreaterEqual(r_hist.get_json()["total"], 3)

    def test_media_capture_serve_delete_and_errors(self) -> None:
        r_photo = self.http.post("/api/media/capture", json={"mode": "photo"})
        self.assertEqual(r_photo.status_code, 201)
        item = r_photo.get_json()["item"]
        self.assertEqual(item["media_type"], "photo")
        self.assertTrue(item["url"].startswith("/media/"))

        r_vid = self.http.post("/api/media/capture", json={"mode": "video", "duration_s": 3})
        self.assertEqual(r_vid.status_code, 201)
        self.assertEqual(r_vid.get_json()["item"]["duration_s"], 3.0)

        # Filter & list
        r_list = self.http.get("/api/media?type=photo")
        self.assertEqual(r_list.status_code, 200)
        self.assertEqual(r_list.get_json()["total"], 1)

        # Serve file & reject traversal
        r_file = self.http.get(item["url"])
        self.assertEqual(r_file.status_code, 200)
        self.assertTrue(r_file.data.startswith(b"\xff\xd8"))
        self.assertEqual(self.http.get("/media/nonexistent.jpg").status_code, 404)
        self.assertEqual(self.http.get("/media/../feeder.db").status_code, 404)

        # Errors: 400 invalid mode/duration, 409 busy, 503 camera error
        self.assertEqual(self.http.post("/api/media/capture", json={"mode": "gif"}).status_code, 400)
        self.assertEqual(
            self.http.post("/api/media/capture", json={"mode": "video", "duration_s": 99}).status_code, 400
        )
        with mock.patch.object(self.camera, "capture_photo", side_effect=CameraBusyError("busy")):
            self.assertEqual(self.http.post("/api/media/capture", json={"mode": "photo"}).status_code, 409)
        with mock.patch.object(self.camera, "capture_photo", side_effect=CameraError("hw fail")):
            self.assertEqual(self.http.post("/api/media/capture", json={"mode": "photo"}).status_code, 503)

        # Delete removes DB entry and file on disk
        r_del = self.http.delete(f"/api/media/{item['id']}")
        self.assertEqual(r_del.status_code, 200)
        self.assertFalse((self.media_dir / item["filename"]).exists())
        self.assertEqual(self.http.delete(f"/api/media/{item['id']}").status_code, 404)

    def test_mock_ir_endpoint_and_non_mock_404(self) -> None:
        r_ok = self.http.post("/api/mock/ir", json={})
        self.assertEqual(r_ok.status_code, 200)
        self.assertTrue(r_ok.get_json()["simulated"])

        with mock.patch.object(type(self.client), "is_mock", new_callable=mock.PropertyMock, return_value=False):
            r_404 = self.http.post("/api/mock/ir", json={})
            self.assertEqual(r_404.status_code, 404)


if __name__ == "__main__":
    unittest.main()
