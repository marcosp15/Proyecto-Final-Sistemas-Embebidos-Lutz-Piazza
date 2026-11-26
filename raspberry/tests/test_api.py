"""Contract 4 REST API tests (Flask test_client when installed + static route & index.html parity)."""

import ast
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import types
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
            ("POST", "/api/config"),
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

        # Redesigned UI exposes user-facing fields (turns_per_portion, schedule_mode, camera, interval)
        # and intentionally hides step_interval_us and ir_cooldown_s (API-only).
        expected_form_keys = {
            "turns_per_portion",
            "schedule_mode",
            "interval_minutes",
            "interval_portions",
            "capture_mode",
            "video_duration_s",
            "max_media_files",
        }
        form_names = set(re.findall(r'<(?:input|select)[^>]+name="([a-z0-9_]+)"', html))
        self.assertEqual(form_names, expected_form_keys)
        self.assertNotIn("step_interval_us", html)
        self.assertNotIn("ir_cooldown_s", html)
        self.assertNotIn("Actualizar", html)

        # Layout & UX invariants
        self.assertIn("body:not(.debug-mode) .debug-only", html)
        self.assertIn('id="debug-toggle"', html)
        self.assertIn("debugToggle.checked = false", html)
        self.assertLess(html.index('id="btn-dispense"'), html.index('role="tablist"'))
        for tab_label in (
            "Configuración de porción",
            "Configuración de cámara",
            "Galería",
            "Historial",
        ):
            self.assertIn(tab_label, html)
        for mode_val in ('value="schedules"', 'value="interval"', 'value="manual"'):
            self.assertIn(mode_val, html)
        self.assertIn('min="0.5" max="10" step="0.5"', html)
        self.assertNotIn("/ 2048", html)
        self.assertIn("?download=1", html)
        self.assertIn("input.disabled = isPhoto", html)
        self.assertIn("field-disabled", html)
        self.assertIn('<th class="debug-only">Pasos</th>', html)
        self.assertIn("!v.paused", html)

        # Response fields referenced in index.html match Contract 4 + redesigned API
        for token in (
            "data.mode",
            "data.current_action",
            "a.connected",
            "a.motor_busy",
            "a.steps_remaining",
            "a.ir_live",
            "w.next_schedule",
            "ns.mode",
            "ns.interval_minutes",
            "c.backend",
            "c.capturing",
            "data.server_time",
            "config.turns_per_portion",
            "config.schedule_mode",
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


class TestApiRuntimeWithFlaskShim(unittest.TestCase):
    """Executes api.create_app() handlers directly via a stdlib Flask shim (works even without flask)."""

    @staticmethod
    def _load_api_with_shim():
        class _FakeResponse:
            def __init__(self, data=None, directory=None, filename=None, as_attachment=False):
                self.json_data = data
                self.directory = directory
                self.filename = filename
                self.as_attachment = as_attachment
                self.headers = {}

        class _FakeRequest:
            def __init__(self):
                self.method = "GET"
                self.args = {}
                self._json = None

            def get_json(self, silent=False):
                return self._json

        fake_req = _FakeRequest()

        class _FakeFlask:
            def __init__(self, _name):
                self.routes = {}

            def route(self, path, methods=None):
                m_list = methods or ["GET"]

                def deco(fn):
                    for m in m_list:
                        self.routes[(m, path)] = fn
                    return fn

                return deco

        fake_flask = types.ModuleType("flask")
        fake_flask.Flask = _FakeFlask
        fake_flask.request = fake_req
        fake_flask.jsonify = lambda payload: _FakeResponse(data=payload)
        fake_flask.send_from_directory = (
            lambda directory, filename, as_attachment=False: _FakeResponse(
                directory=directory, filename=filename, as_attachment=as_attachment
            )
        )

        api_file = RASPBERRY_DIR / "service" / "api.py"
        spec = importlib.util.spec_from_file_location("_test_api_shim_mod", str(api_file))
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"flask": fake_flask}):
            spec.loader.exec_module(mod)
        return mod, fake_req

    def setUp(self) -> None:
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
        self.api_mod, self.req = self._load_api_with_shim()
        self.app = self.api_mod.create_app(self.client, self.db_path, self.camera, worker=self.worker)

    def tearDown(self) -> None:
        self.worker.stop(wait_captures=True)
        self._tmpdir.cleanup()

    def _call(self, method: str, route: str, json_body=None, args=None, **kwargs):
        self.req.method = method
        self.req._json = json_body
        self.req.args = dict(args or {})
        res = self.app.routes[(method, route)](**kwargs)
        if isinstance(res, tuple):
            resp, code = res
            return code, resp
        return 200, res

    def test_config_post_put_turns_per_portion_and_schedule_mode(self) -> None:
        code, resp = self._call("GET", "/api/config")
        self.assertEqual(code, 200)
        cfg = resp.json_data["config"]
        self.assertEqual(cfg["step_interval_us"], 1000)
        self.assertEqual(cfg["turns_per_portion"], 1.0)
        self.assertEqual(cfg["schedule_mode"], "schedules")

        # PUT turns_per_portion=2.5 -> 10240 steps + Arduino sync
        code, resp = self._call("PUT", "/api/config", json_body={"turns_per_portion": 2.5})
        self.assertEqual(code, 200)
        self.assertTrue(resp.json_data["arduino_synced"])
        self.assertEqual(resp.json_data["config"]["steps_per_portion"], 10240)
        self.assertEqual(resp.json_data["config"]["turns_per_portion"], 2.5)
        self.assertEqual(self.transport.manual_steps, 10240)

        # POST /api/config updates step_interval_us + ir_cooldown_s and syncs Arduino
        code, resp = self._call(
            "POST", "/api/config", json_body={"step_interval_us": 1500, "ir_cooldown_s": 45}
        )
        self.assertEqual(code, 200)
        self.assertTrue(resp.json_data["arduino_synced"])
        self.assertEqual(resp.json_data["config"]["step_interval_us"], 1500)
        self.assertEqual(resp.json_data["config"]["ir_cooldown_s"], 45)
        self.assertEqual(self.transport.step_interval_us, 1500)

        # schedule_mode -> interval syncs interval_enabled and anchors last_interval_run_at
        code, resp = self._call("PUT", "/api/config", json_body={"schedule_mode": "interval"})
        self.assertEqual(code, 200)
        self.assertEqual(resp.json_data["config"]["schedule_mode"], "interval")
        self.assertIs(resp.json_data["config"]["interval_enabled"], True)
        self.assertTrue(resp.json_data["config"]["last_interval_run_at"])

        # Invalid turns_per_portion & conflict rejected with 400
        for bad in (
            {"turns_per_portion": 0.25},
            {"turns_per_portion": 0},
            {"turns_per_portion": 10.5},
            {"turns_per_portion": True},
            {"turns_per_portion": 2.0, "steps_per_portion": 4096},
        ):
            with self.subTest(bad=bad):
                code, _ = self._call("PUT", "/api/config", json_body=bad)
                self.assertEqual(code, 400)

    def test_status_current_action_transitions(self) -> None:
        code, resp = self._call("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(resp.json_data["current_action"], "En espera")

        code, _ = self._call("POST", "/api/dispense", json_body={"portions": 2})
        self.assertEqual(code, 200)
        code, resp = self._call("GET", "/api/status")
        self.assertIn("Dispensando", resp.json_data["current_action"])
        self.assertIn("2 porc.", resp.json_data["current_action"])

        code, _ = self._call("POST", "/api/stop", json_body={})
        self.assertEqual(code, 200)
        code, resp = self._call("GET", "/api/status")
        self.assertEqual(resp.json_data["current_action"], "En espera")

        code, _ = self._call("POST", "/api/unjam", json_body={"wiggle_steps": 256})
        self.assertEqual(code, 200)
        code, resp = self._call("GET", "/api/status")
        self.assertIn("Destrabando", resp.json_data["current_action"])

    def test_media_download_flag_and_traversal_guard(self) -> None:
        code, resp = self._call("GET", "/media/<path:filename>", filename="sample.jpg", args={"download": "1"})
        self.assertEqual(code, 200)
        self.assertTrue(resp.as_attachment)

        code, resp = self._call("GET", "/media/<path:filename>", filename="sample.jpg", args={})
        self.assertEqual(code, 200)
        self.assertFalse(resp.as_attachment)

        code, _ = self._call(
            "GET", "/media/<path:filename>", filename="../feeder.db", args={"download": "1"}
        )
        self.assertEqual(code, 404)


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
        self.assertIn(b"Comedero", r_idx.data)

        r_st = self.http.get("/api/status")
        self.assertEqual(r_st.status_code, 200)
        body = r_st.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["mode"], "mock")
        self.assertEqual(body["current_action"], "En espera")
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
        cfg0 = r_get.get_json()["config"]
        self.assertEqual(cfg0["steps_per_portion"], 4096)
        self.assertEqual(cfg0["step_interval_us"], 1000)
        self.assertEqual(cfg0["turns_per_portion"], 1.0)
        self.assertEqual(cfg0["schedule_mode"], "schedules")

        # Motor setting update via PUT + turns_per_portion syncs Arduino
        r_put = self.http.put(
            "/api/config", json={"turns_per_portion": 1.5, "schedule_mode": "interval"}
        )
        self.assertEqual(r_put.status_code, 200)
        data = r_put.get_json()
        self.assertTrue(data["arduino_synced"])
        self.assertEqual(data["config"]["steps_per_portion"], 6144)
        self.assertEqual(data["config"]["turns_per_portion"], 1.5)
        self.assertEqual(data["config"]["schedule_mode"], "interval")
        self.assertIs(data["config"]["interval_enabled"], True)
        self.assertTrue(data["config"]["last_interval_run_at"])

        # POST /api/config updates step_interval_us & ir_cooldown_s and syncs Arduino
        r_post = self.http.post(
            "/api/config", json={"step_interval_us": 1500, "ir_cooldown_s": 45}
        )
        self.assertEqual(r_post.status_code, 200)
        pdata = r_post.get_json()
        self.assertTrue(pdata["arduino_synced"])
        self.assertEqual(pdata["config"]["step_interval_us"], 1500)
        self.assertEqual(pdata["config"]["ir_cooldown_s"], 45)

        # Non-motor update does not force sync
        r_put2 = self.http.put("/api/config", json={"ir_cooldown_s": 60})
        self.assertEqual(r_put2.status_code, 200)
        self.assertFalse(r_put2.get_json()["arduino_synced"])

        # Invalid body / out of range / turns conflict -> 400
        self.assertEqual(self.http.put("/api/config", json={}).status_code, 400)
        self.assertEqual(self.http.put("/api/config", json={"steps_per_portion": 10}).status_code, 400)
        self.assertEqual(self.http.put("/api/config", json={"turns_per_portion": 0.25}).status_code, 400)
        self.assertEqual(
            self.http.put(
                "/api/config", json={"turns_per_portion": 2.0, "steps_per_portion": 4096}
            ).status_code,
            400,
        )

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

        # Motor is now busy -> current_action reflects dispense, 409 on second dispense & unjam
        st_disp = self.http.get("/api/status").get_json()
        self.assertIn("Dispensando", st_disp["current_action"])
        self.assertIn("1 porc.", st_disp["current_action"])
        self.assertEqual(self.http.post("/api/dispense", json={"steps": 512}).status_code, 409)
        self.assertEqual(self.http.post("/api/unjam", json={}).status_code, 409)

        # Stop cancels motion -> 200
        r_stop = self.http.post("/api/stop", json={})
        self.assertEqual(r_stop.status_code, 200)
        self.assertGreater(r_stop.get_json()["steps_cancelled"], 0)
        self.assertEqual(self.http.get("/api/status").get_json()["current_action"], "En espera")

        # Unjam now succeeds -> 200 & current_action reflects unjam
        r_unjam = self.http.post("/api/unjam", json={"wiggle_steps": 256})
        self.assertEqual(r_unjam.status_code, 200)
        self.assertEqual(r_unjam.get_json()["wiggle_steps"], 256)
        self.assertIn("Destrabando", self.http.get("/api/status").get_json()["current_action"])
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

        # Serve file (inline + attachment download) & reject traversal
        r_file = self.http.get(item["url"])
        self.assertEqual(r_file.status_code, 200)
        self.assertTrue(r_file.data.startswith(b"\xff\xd8"))
        self.assertNotIn("attachment", r_file.headers.get("Content-Disposition", "").lower())

        r_dl = self.http.get(f"{item['url']}?download=1")
        self.assertEqual(r_dl.status_code, 200)
        self.assertIn("attachment", r_dl.headers.get("Content-Disposition", "").lower())

        self.assertEqual(self.http.get("/media/nonexistent.jpg").status_code, 404)
        self.assertEqual(self.http.get("/media/../feeder.db").status_code, 404)
        self.assertEqual(self.http.get("/media/../feeder.db?download=1").status_code, 404)

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
