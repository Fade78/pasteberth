"""Tests de concurrence : uploads simultanés, rétention cohérente,
lecteurs pendant les écritures, sessions multiples."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pasteberth.auth import SessionStore

from tests.helpers import (
    build_multipart,
    json_of,
    login,
    make_png,
    request,
    write_config,
    LiveServer,
)

PASSWORD = "concurrence-test-000"


def _upload(port, cookie, zone):
    body, ctype = build_multipart(data=make_png(3, 3))
    status, _, resp = request(
        port, "POST", f"/api/zones/{zone}/images",
        body=body, headers={"Content-Type": ctype}, cookie=cookie,
    )
    return status, resp


def _named_upload(port, cookie, zone, data, *, replace=False):
    fields = {"preserve_name": "1"}
    if replace:
        fields["replace"] = "1"
    body, ctype = build_multipart(
        filename="shared.txt",
        data=data,
        content_type="text/plain",
        extra_fields=fields,
    )
    status, _, resp = request(
        port, "POST", f"/api/zones/{zone}/images",
        body=body, headers={"Content-Type": ctype}, cookie=cookie,
    )
    return status, resp


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.dirs = {z: tmp / z for z in ("a", "b")}
        cfg_path = write_config(
            tmp,
            zones=[
                {"id": "a", "directory": str(self.dirs["a"]), "retain": 4},
                {"id": "b", "directory": str(self.dirs["b"]), "retain": 2},
            ],
            auth_enabled=True,
            password=PASSWORD,
        )
        self.server = LiveServer(cfg_path)
        self.addCleanup(self.server.stop)
        self.addCleanup(self._tmp.cleanup)
        self.cookie = login(self.server.port, PASSWORD)


class TestConcurrenceMemeZone(Base):
    def test_12_uploads_simultanes_retain_4(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: _upload(
                self.server.port, self.cookie, "a"), range(12)))
        statuses = [s for s, _ in results]
        self.assertTrue(all(s == 201 for s in statuses),
                        f"échecs d'upload : {statuses}")
        items = [json_of(r) for _, r in results]
        filenames = [i["filename"] for i in items]
        self.assertEqual(len(set(filenames)), 12, "collision de noms détectée")

        status, _, resp = request(
            self.server.port, "GET", "/api/zones/a/images", cookie=self.cookie)
        listed = json_of(resp)["images"]
        # État final conforme à retain=4
        self.assertEqual(len(listed), 4)
        # La plus récente image n'a jamais pu être supprimée :
        newest_uploaded = max(items, key=lambda i: i["created_at"])
        self.assertEqual(listed[0]["filename"], newest_uploaded["filename"])
        # Les survivants existent bien sur disque, tous uniques.
        on_disk = list(self.dirs["a"].glob("*.png"))
        self.assertEqual(len({p.name for p in on_disk}), 4)
        self.assertEqual({i["filename"] for i in listed}, {p.name for p in on_disk})


class TestZonesParalleles(Base):
    def test_deux_zones_en_parallele(self):
        jobs = [("a", range(10)), ("b", range(10))]
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = []
            for zone, rng in jobs:
                for _ in rng:
                    futures.append(pool.submit(_upload, self.server.port, self.cookie, zone))
            outcomes = [f.result() for f in futures]
        self.assertTrue(all(s == 201 for s, _ in outcomes))
        counts = {}
        for zone, retain in (("a", 4), ("b", 2)):
            status, _, resp = request(
                self.server.port, "GET", f"/api/zones/{zone}/images", cookie=self.cookie)
            counts[zone] = len(json_of(resp)["images"])
            names = [i["filename"] for i in json_of(resp)["images"]]
            self.assertEqual(len(set(names)), len(names))
        self.assertEqual(counts, {"a": 4, "b": 2})

    def test_lecteurs_pendant_ecritures(self):
        def reader(_):
            status, _, resp = request(
                self.server.port, "GET", "/api/zones/a/images", cookie=self.cookie)
            return status, len(json_of(resp)["images"])

        with ThreadPoolExecutor(max_workers=12) as pool:
            writers = [pool.submit(_upload, self.server.port, self.cookie, "a")
                       for _ in range(10)]
            readers = [pool.submit(reader, i) for i in range(30)]
            write_results = [w.result() for w in writers]
            read_results = [r.result() for r in readers]
        self.assertTrue(all(s == 201 for s, _ in write_results))
        for status, count in read_results:
            self.assertEqual(status, 200)
            self.assertLessEqual(count, 5)  # retain + éventuel transitoire


class TestConcurrenceNomPreserve(Base):
    def test_deux_uploads_nommes_sans_confirmation_ne_remplacent_pas(self):
        status, response = _named_upload(
            self.server.port, self.cookie, "a", b"version initiale"
        )
        self.assertEqual(status, 201)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(
                lambda data: _named_upload(
                    self.server.port, self.cookie, "a", data
                ),
                (b"tentative 1", b"tentative 2"),
            ))

        self.assertEqual([status for status, _ in outcomes], [428, 428])
        self.assertEqual(json_of(response)["filename"], "shared.txt")
        status, _, body = request(
            self.server.port,
            "GET",
            "/previews/a/shared.txt",
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"version initiale")

    def test_remplacement_explicite_et_upload_sans_confirmation_sont_serialises(self):
        status, _ = _named_upload(
            self.server.port, self.cookie, "a", b"version initiale"
        )
        self.assertEqual(status, 201)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(
                lambda replace: _named_upload(
                    self.server.port,
                    self.cookie,
                    "a",
                    b"version remplacee" if replace else b"tentative",
                    replace=replace,
                ),
                (True, False),
            ))

        self.assertEqual(sorted(status for status, _ in outcomes), [201, 428])
        status, _, body = request(
            self.server.port,
            "GET",
            "/previews/a/shared.txt",
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"version remplacee")


class TestSessionsMultiples(Base):
    """(#31)(#32) deux navigateurs / deux machines simultanés."""

    def test_deux_sessions_independantes(self):
        c1 = login(self.server.port, PASSWORD)
        c2 = login(self.server.port, PASSWORD)
        self.assertNotEqual(c1, c2)
        s1, _, r1 = request(self.server.port, "GET", "/api/zones/a/images", cookie=c1)
        upload_status, _ = _upload(self.server.port, c2, "a")
        self.assertEqual((s1, upload_status), (200, 201))
        # logout de la session 1 : la session 2 reste valide.
        request(self.server.port, "POST", "/logout", cookie=c1)
        status_after_logout, _, _ = request(
            self.server.port, "GET", "/api/zones/a/images", cookie=c1)
        self.assertEqual(status_after_logout, 401)
        status_other, _, _ = request(
            self.server.port, "GET", "/api/zones/b/images", cookie=c2)
        self.assertEqual(status_other, 200)

    def test_store_sessions_sous_threads(self):
        store = SessionStore(ttl_seconds=60)
        tokens = set()

        def create(_):
            return store.create()

        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = set(pool.map(create, range(64)))
        self.assertEqual(len(tokens), 64)
        self.assertTrue(all(store.validate(t) for t in tokens))


if __name__ == "__main__":
    unittest.main()
