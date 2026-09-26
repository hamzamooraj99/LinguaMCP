from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

import httpx

from viewer.app import create_app
from viewer.storage import (
    MAX_DOCUMENT_BYTES,
    InvalidRequestError,
    validate_relative_path,
)


class DeploymentArtifactTests(unittest.TestCase):
    """Repository checks for the Phase 3 activation artifacts."""

    repository_root = Path(__file__).resolve().parents[1]

    def test_phase3_unit_is_read_only_and_loopback_bound(self) -> None:
        unit = (self.repository_root / "deploy" / "systemd" / "linguamcp-viewer.service").read_text(encoding="utf-8")
        for required in (
            "User=linguamcp-viewer",
            "Group=linguamcp-viewer",
            "WorkingDirectory=/opt/services/linguamcp",
            "--host 127.0.0.1 --port 8001",
            "--data-root /var/lib/linguamcp",
            "Restart=on-failure",
            "UMask=0027",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "ReadOnlyPaths=/var/lib/linguamcp",
            "StandardOutput=journal",
            "StandardError=journal",
        ):
            self.assertIn(required, unit)
        self.assertNotIn("ReadWritePaths=", unit)

    def test_phase3_guide_keeps_access_private_and_fastmcp_separate(self) -> None:
        guide = (self.repository_root / "deploy" / "PHASE3_DEBIAN.md").read_text(encoding="utf-8")
        for required in (
            "linguamcp.service",
            "127.0.0.1:8000",
            "127.0.0.1:8001",
            "setfacl",
            "tailscale serve --bg --https=8443 8001",
            "tailscale funnel status",
            "systemd-analyze verify",
            "expected failure: create",
            "expected failure: modify",
            "expected failure: rename",
            "expected failure: delete",
            "SKIP: no Markdown file exists",
            "set -eu",
        ):
            self.assertIn(required, guide)


class ViewerAPITests(unittest.TestCase):
    """Phase 2 checks for the live read-only viewer application."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary_directory.name)
        self._create_fixture()
        self.application = create_app(data_root=self.data_root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write(self, relative_path: str, content: str) -> Path:
        path = self.data_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _write_bytes(self, relative_path: str, content: bytes) -> Path:
        path = self.data_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _create_fixture(self) -> None:
        for language in ("german", "japanese", "spanish"):
            (self.data_root / language / "sessions").mkdir(parents=True)
            (self.data_root / language / "archives").mkdir()
            (self.data_root / language / "delivery").mkdir()

        current_files = (
            "00-profile.md",
            "01-lesson-plan.md",
            "02-progress.md",
            "03-vocabulary.md",
            "04-mistakes.md",
            "05-scenarios.md",
            "active-session.md",
            "latest-summary.md",
            "latest-homework.md",
        )
        for filename in current_files:
            self._write(f"german/{filename}", f"# {filename}\n")

        self._write(
            "german/00-profile.md",
            "# German Learner Profile\n\nGr\u00fc\u00dfe from the learner.\n",
        )
        self._write(
            "german/sessions/2026-08-24T15-30-00-000000Z.md",
            "# Older session\n",
        )
        self._write(
            "german/sessions/2026-08-25T09-45-00-000000Z.md",
            "# Newer session\n",
        )
        self._write(
            "german/archives/03-vocabulary/2026-08-01T12-00-00-000000Z.md",
            "# Older vocabulary archive\n",
        )
        self._write(
            "german/archives/03-vocabulary/2026-08-24T12-00-00-000000Z.md",
            "# Newer vocabulary archive\n",
        )
        self._write(
            "german/archives/02-progress/2026-08-20T12-00-00-000000Z.md",
            "# Progress archive\n",
        )
        self._write("german/delivery/latest-email.md", "# Email draft\n")
        self._write("german/delivery/latest-whatsapp.md", "# WhatsApp draft\n")
        self._write("german/notes/empty.md", "")
        self._write("german/notes/reading-list.md", "# Reading list\n")
        self._write("german/unknown-file.md", "# Unknown file\n")
        self._write("german/notes/ignore.txt", "not Markdown")
        self._write("german/.hidden/hidden.md", "# Hidden\n")
        self._write("german/notes/.hidden/hidden.md", "# Hidden\n")

        self._write("spanish/00-profile.md", "# Perfil\n\nEspa\u00f1ol.\n")
        self._write("audit-log.jsonl", '{"private": true}\n')
        self._write("root-note.md", "Must not be exposed.\n")
        self._write(".hidden-root/hidden.md", "Must not be exposed.\n")
        (self.data_root / "not_a_language").mkdir()
        (self.data_root / "Uppercase").mkdir()

    async def _request_async(
        self, method: str, path: str, **kwargs: object
    ) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://viewer.test"
        ) as client:
            return await client.request(method, path, **kwargs)

    def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        return asyncio.run(self._request_async(method, path, **kwargs))

    def _json(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
        response = self.request(method, path, **kwargs)
        self.assertTrue(response.headers.get("content-type", "").startswith("application/json"))
        return response.json()

    def test_language_discovery_is_deterministic_and_excludes_root_files(self) -> None:
        first = self._json("GET", "/api/languages")
        second = self._json("GET", "/api/languages")

        self.assertEqual(first, second)
        self.assertEqual(
            first["languages"],
            [
                {"id": "german", "label": "German"},
                {"id": "japanese", "label": "Japanese"},
                {"id": "spanish", "label": "Spanish"},
            ],
        )

    def test_document_groups_cover_all_five_groups_and_order_files(self) -> None:
        payload = self._json("GET", "/api/languages/german/documents")
        groups = payload["groups"]

        self.assertEqual(
            [group["id"] for group in groups],
            ["current", "sessions", "archives", "delivery", "other"],
        )
        current = groups[0]["documents"]
        self.assertEqual(
            [document["path"] for document in current],
            [
                "00-profile.md",
                "01-lesson-plan.md",
                "02-progress.md",
                "03-vocabulary.md",
                "04-mistakes.md",
                "05-scenarios.md",
                "active-session.md",
                "latest-summary.md",
                "latest-homework.md",
            ],
        )

        sessions = groups[1]["documents"]
        self.assertEqual(
            [document["path"] for document in sessions],
            [
                "sessions/2026-08-25T09-45-00-000000Z.md",
                "sessions/2026-08-24T15-30-00-000000Z.md",
            ],
        )
        self.assertTrue(sessions[0]["label"].startswith("Session"))

        archives = groups[2]["documents"]
        self.assertEqual(
            [document["path"] for document in archives],
            [
                "archives/02-progress/2026-08-20T12-00-00-000000Z.md",
                "archives/03-vocabulary/2026-08-24T12-00-00-000000Z.md",
                "archives/03-vocabulary/2026-08-01T12-00-00-000000Z.md",
            ],
        )
        self.assertEqual(archives[1]["label"].split(" ")[0], "Vocabulary")

        delivery = groups[3]["documents"]
        self.assertEqual(
            [document["label"] for document in delivery],
            ["Email draft", "WhatsApp draft"],
        )

        other = groups[4]["documents"]
        self.assertEqual(
            [document["path"] for document in other],
            [
                "notes/empty.md",
                "notes/reading-list.md",
                "unknown-file.md",
            ],
        )

    def test_current_lesson_contract_appears_as_current_memory(self) -> None:
        self._write(
            "german/current-lesson-contract.md",
            "# Lesson Contract\n\nA temporary lesson scope.\n",
        )
        groups = self._json("GET", "/api/languages/german/documents")["groups"]
        current = groups[0]["documents"]
        contract = next(item for item in current if item["path"] == "current-lesson-contract.md")
        self.assertEqual(contract["label"], "Lesson contract")

        response = self.request(
            "GET", "/api/languages/german/documents/current-lesson-contract.md"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("temporary lesson scope", response.json()["html"])

    def test_pending_save_returns_recovery_required_without_paths(self) -> None:
        pending = self.data_root / "german" / ".transactions" / "pending"
        pending.mkdir(parents=True)

        response = self.request("GET", "/api/languages/german/documents")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "recovery_required")
        self.assertNotIn(str(self.data_root), response.text)

    def test_empty_directories_and_empty_markdown_are_supported(self) -> None:
        payload = self._json("GET", "/api/languages/japanese/documents")
        self.assertTrue(all(not group["documents"] for group in payload["groups"]))

        response = self.request("GET", "/api/languages/german/documents/notes/empty.md")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["html"], "")

    def test_utf8_content_is_read_and_rendered(self) -> None:
        response = self.request("GET", "/api/languages/german/documents/00-profile.md")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Gr\u00fc\u00dfe", response.json()["html"])

    def test_invalid_languages_and_plain_or_encoded_traversal_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequestError):
            validate_relative_path("../outside.md")
        with self.assertRaises(InvalidRequestError):
            validate_relative_path("sessions/../outside.md")
        with self.assertRaises(InvalidRequestError):
            validate_relative_path("C:\\outside.md")

        for language in ("German", "not_a_language"):
            response = self.request("GET", f"/api/languages/{language}/documents")
            self.assertEqual(response.status_code, 400, language)

        encoded_language = self.request("GET", "/api/languages/%2e%2e/documents")
        self.assertEqual(encoded_language.status_code, 400)

        encoded_path = self.request(
            "GET",
            "/api/languages/german/documents/%2e%2e%2f00-profile.md",
        )
        self.assertEqual(encoded_path.status_code, 400)

    def test_missing_resources_return_404_without_paths(self) -> None:
        missing_language = self.request("GET", "/api/languages/french/documents")
        missing_document = self.request(
            "GET", "/api/languages/german/documents/missing.md"
        )
        for response, message in (
            (missing_language, "Language not found."),
            (missing_document, "Document not found."),
        ):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["message"], message)
            self.assertNotIn(str(self.data_root), response.text)

    def test_non_markdown_files_and_symlinks_are_not_exposed(self) -> None:
        payload = self._json("GET", "/api/languages/german/documents")
        paths = [
            document["path"]
            for group in payload["groups"]
            for document in group["documents"]
        ]
        self.assertNotIn("notes/ignore.txt", paths)
        self.assertNotIn(".hidden/hidden.md", paths)
        self.assertNotIn("notes/.hidden/hidden.md", paths)

        outside_file = self.data_root / "outside.md"
        outside_directory = self.data_root / "outside-directory"
        outside_file.write_text("# Outside\n", encoding="utf-8")
        outside_directory.mkdir()
        (outside_directory / "outside.md").write_text("# Outside\n", encoding="utf-8")
        file_link = self.data_root / "german" / "linked.md"
        directory_link = self.data_root / "german" / "linked-directory"
        try:
            file_link.symlink_to(outside_file)
            directory_link.symlink_to(outside_directory, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symbolic links are unavailable in this environment: {error}")

        payload = self._json("GET", "/api/languages/german/documents")
        paths = [
            document["path"]
            for group in payload["groups"]
            for document in group["documents"]
        ]
        self.assertNotIn("linked.md", paths)
        self.assertFalse(any(path.startswith("linked-directory/") for path in paths))

    def test_file_size_and_utf8_errors_use_the_planned_statuses(self) -> None:
        self._write_bytes("german/large.md", b"x" * (MAX_DOCUMENT_BYTES + 1))
        self._write_bytes("german/unreadable.md", b"\xff\xfe\xfa")

        too_large = self.request("GET", "/api/languages/german/documents/large.md")
        unreadable = self.request(
            "GET", "/api/languages/german/documents/unreadable.md"
        )
        self.assertEqual(too_large.status_code, 413)
        self.assertEqual(too_large.json()["error"]["code"], "too_large")
        self.assertEqual(unreadable.status_code, 500)
        self.assertEqual(unreadable.json()["error"]["code"], "unreadable")

    def test_unsafe_markdown_is_not_emitted_as_active_markup(self) -> None:
        self._write(
            "german/unsafe.md",
            """
<script>alert('x')</script>
<div onclick="alert('x')">event</div>
[bad](javascript:alert('x'))
![remote image](https://example.com/image.png)
""",
        )
        response = self.request("GET", "/api/languages/german/documents/unsafe.md")
        self.assertEqual(response.status_code, 200)
        html = response.json()["html"].lower()
        self.assertNotIn("<script", html)
        self.assertNotIn("<div onclick=", html)
        self.assertNotIn("href=\"javascript:", html)
        self.assertNotIn("<img", html)
        self.assertIn("markdown-image-alt", html)

    def test_read_methods_and_status_codes_are_enforced(self) -> None:
        invalid_extension = self.request(
            "GET", "/api/languages/german/documents/notes/ignore.txt"
        )
        self.assertEqual(invalid_extension.status_code, 400)

        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = self.request(method, "/api/languages")
            self.assertEqual(response.status_code, 405, method)
            self.assertEqual(response.headers.get("allow"), "GET, HEAD")

    def test_security_and_cache_headers_are_present_on_live_responses(self) -> None:
        for path in (
            "/healthz",
            "/api/languages",
            "/api/languages/german/documents",
            "/api/languages/german/documents/00-profile.md",
        ):
            response = self.request("GET", path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("cache-control"), "no-store")
            self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(response.headers.get("x-frame-options"), "DENY")
            self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")
            self.assertIn("default-src 'self'", response.headers.get("content-security-policy", ""))

    def test_reads_leave_file_hashes_and_modification_times_unchanged(self) -> None:
        profile = self.data_root / "german" / "00-profile.md"
        before_hash = hashlib.sha256(profile.read_bytes()).hexdigest()
        before_mtime = profile.stat().st_mtime_ns

        self.request("GET", "/api/languages/german/documents")
        self.request("GET", "/api/languages/german/documents/00-profile.md")

        self.assertEqual(hashlib.sha256(profile.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(profile.stat().st_mtime_ns, before_mtime)

    def test_live_static_frontend_uses_http_source_and_hides_mock_route(self) -> None:
        index = self.request("GET", "/")
        app_script = self.request("GET", "/app.js")
        data_script = self.request("GET", "/http-data.js")
        mock_script = self.request("GET", "/mock-data.js")

        self.assertEqual(index.status_code, 200)
        self.assertIn('src="app.js"', index.text)
        self.assertIn('data-theme-toggle', index.text)
        self.assertIn('hamburger-icon', index.text)
        self.assertIn('data-action="toggle-files"', index.text)
        self.assertNotIn('data-theme-option', index.text)
        self.assertEqual(app_script.status_code, 200)
        self.assertIn("http-data.js", app_script.text)
        self.assertEqual(data_script.status_code, 200)
        self.assertIn("api/languages", data_script.text)
        self.assertEqual(mock_script.status_code, 404)

    def test_frontend_is_an_installable_network_only_web_app(self) -> None:
        index = self.request("GET", "/")
        manifest_response = self.request("GET", "/manifest.webmanifest")
        service_worker = self.request("GET", "/service-worker.js")
        app_script = self.request("GET", "/app.js")

        self.assertIn('rel="manifest" href="manifest.webmanifest"', index.text)
        self.assertIn('rel="apple-touch-icon"', index.text)
        self.assertIn('name="theme-color"', index.text)

        self.assertEqual(manifest_response.status_code, 200)
        self.assertEqual(manifest_response.headers.get("cache-control"), "no-cache")
        manifest = json.loads(manifest_response.text)
        self.assertEqual(manifest["name"], "LinguaMCP Learner Memory")
        self.assertEqual(manifest["short_name"], "LinguaMCP")
        self.assertEqual(manifest["start_url"], "./")
        self.assertEqual(manifest["scope"], "./")
        self.assertEqual(manifest["display"], "standalone")
        self.assertIn(
            ("192x192", "any"),
            {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]},
        )
        self.assertIn(
            ("512x512", "any"),
            {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]},
        )
        self.assertIn(
            ("512x512", "maskable"),
            {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]},
        )

        for icon in manifest["icons"]:
            response = self.request("GET", f"/{icon['src']}")
            self.assertEqual(response.status_code, 200, icon["src"])
            self.assertEqual(response.headers.get("content-type"), "image/png")
            width, height = struct.unpack(">II", response.content[16:24])
            expected_size = int(icon["sizes"].split("x", 1)[0])
            self.assertEqual((width, height), (expected_size, expected_size))

        apple_icon = self.request("GET", "/assets/icon-180.png")
        self.assertEqual(apple_icon.status_code, 200)
        self.assertEqual(struct.unpack(">II", apple_icon.content[16:24]), (180, 180))

        self.assertEqual(service_worker.status_code, 200)
        self.assertEqual(service_worker.headers.get("cache-control"), "no-cache")
        self.assertIn("fetch(event.request)", service_worker.text)
        self.assertNotIn("caches.open", service_worker.text)
        self.assertIn('register("./service-worker.js")', app_script.text)


if __name__ == "__main__":
    unittest.main()
