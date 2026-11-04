import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import city1551 as c


def row(uid, t, kind, content, street="вул. Андрія Малишка", house="21", org="ЖЕД", city="Київ"):
    return {
        "Id": hash((uid, org)) & 0xFFFFFF, "uid": uid, "receivedDateTime": t, "type": "Звернення",
        "kind": kind, "content": content, "accrualMethod": "Дзвінок в 1551", "CATUTTC": "UA80000000000093317",
        "addressPostCode": None, "addressAdminUnitL1": "Україна", "addressAdminUnitL2": city,
        "addressAdminUnitL3": None, "addressAdminUnitL4": None, "addressPostName": city,
        "addressThoroughfare": street, "addressLocatorDesignator": house, "addressLocatorBuilding": None,
        "status": "Зареєстровано", "organizationName": org, "organizationId": None, "result": "",
    }


BUVETS = (
    "﻿uid,organizationName,organizationId,wellDepth,status,generator,waterAnalysis,addressAdminUnitL1,"
    "addressAdminUnitL2,addressAdminUnitL3,addressAdminUnitL4,addressPostName,addressPostDistrict,"
    "addressThoroughfare,addressLocatorDesignator,addressLocatorBuilding,lat,lon\n"
    + "".join(
        f"{i},СВКП,1,100,працює,{'TRUE' if i % 2 else 'FALSE'},,Україна,місто Київ,null,null,Київ,Дніпровський,"
        f"просп. Миру,{i},null,50.4{i:02d},30.6\n" for i in range(1, 61)
    )
).encode()


class SlugTest(unittest.TestCase):
    def test_translit(self):
        self.assertEqual(c.slugify("вул. Андрія Малишка"), "vul-andriia-malyshka")
        self.assertEqual(c.slugify("вул. Євгена Сверстюка"), "vul-yevhena-sverstiuka")
        self.assertEqual(c.slugify("вул. Анатолія Солов'яненка"), "vul-anatoliia-solovianenka")
        self.assertEqual(c.slugify("просп. Їжакевича 5-А"), "prosp-yizhakevycha-5-a")

    def test_collisions_stable(self):
        s = c.assign_slugs(["вул. Миру", "вул Миру", "вул. Миру "])
        self.assertEqual(sorted(s.values()), ["vul-myru", "vul-myru-2", "vul-myru-3"])
        self.assertEqual(s, c.assign_slugs(list(reversed(list(s)))))


class CompactTest(unittest.TestCase):
    def test_dedupe_filter(self):
        rows = [
            row("1/1", "2026-11-04T08:50Z", "Холодне водопостачання", "Відсутність ХВП", org="ЖЕД"),
            row("1/1", "2026-11-04T08:50Z", "Холодне водопостачання", "Відсутність ХВП", org="Водоканал"),
            row("2/1", "2026-11-04T09:00Z", "Транспорт", "Незадовільний графік роботи"),
            row("3/1", "2026-11-04T09:10Z", "Обслуговування зовнішніх та внутрішньобудинкових систем",
                "Порив труб у будинку (окрім квартири)"),
            row("4/1", "2026-11-04T09:20Z", "Обслуговування зовнішніх та внутрішньобудинкових систем",
                "Ремонт сантехнічного обладнання"),
            row("5/1", "2026-11-04T09:30Z", "Холодне водопостачання", "Відсутність ХВП", city="Бровари"),
            row("6/1", "2026-11-04T09:40Z", "Ліфт", "Не працює пасажирський ліфт", house=None),
        ]
        recs = c.compact(rows)
        self.assertEqual([r["u"] for r in recs], ["1/1", "3/1", "6/1"])
        self.assertEqual([r["k"] for r in recs], ["cold", "pipes", "lift"])
        self.assertIsNone(recs[2]["h"])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "src"
        self.out = Path(self.tmp.name) / "out"
        self.src.mkdir()
        oct_rows = [
            row("o1", "2026-10-02T10:00Z", "Холодне водопостачання", "Відсутність ХВП"),
            row("o2", "2026-10-30T10:00Z", "Холодне водопостачання",
                "Відсутній тиск ХВП з 10-го поверху включно", house="25"),
        ]
        nov_rows = [
            row("n1", "2026-11-04T15:00Z", "Холодне водопостачання", "Відсутність ХВП", house="17"),
            row("n2", "2026-11-04T16:00Z", "Холодне водопостачання", "Відсутність ХВП", house="31"),
            row("n3", "2026-11-04T17:00Z", "Холодне водопостачання", "Відсутність ХВП", house="21-А"),
            row("n4", "2026-11-04T17:30Z", "Робота бюветів", "Не працює бювет", street="просп. Миру", house="3"),
            row("n5", "2026-11-04T17:40Z", "Опалення", "Відсутність опалення", street="вул. Ентузіастів", house="1"),
        ]
        (self.src / "2026-10.json").write_text(json.dumps(oct_rows, ensure_ascii=False), encoding="utf-8")
        (self.src / "2026-11.json").write_text(json.dumps(nov_rows, ensure_ascii=False), encoding="utf-8")
        self.now = dt.datetime(2026, 11, 4, 19, 0, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full(self):
        meta = c.run(self.out, self.now, self.src, BUVETS)
        self.assertEqual(meta["source_latest"], "2026-11-04T17:40Z")
        self.assertEqual(meta["lag_minutes"], 80)
        # окно 31 день: o1 (02.10) уже вне окна, o2 (30.10) внутри
        self.assertEqual(meta["window_records"], 6)
        idx = json.loads((self.out / "1551/index.json").read_text(encoding="utf-8"))
        names = {s["name"]: s for s in idx["streets"]}
        mal = names["вул. Андрія Малишка"]
        self.assertEqual(mal["file"], "streets/vul-andriia-malyshka.json")
        self.assertEqual(mal["n"], 4)
        st = json.loads((self.out / "1551" / mal["file"]).read_text(encoding="utf-8"))
        self.assertEqual([i["h"] for i in st["items"]], ["21-А", "31", "17", "25"])  # новые сверху
        city = json.loads((self.out / "1551/city.json").read_text(encoding="utf-8"))
        self.assertEqual(city["hot"]["cold"][0]["street"], "вул. Андрія Малишка")
        self.assertEqual(city["hot"]["cold"][0]["houses"], ["17", "21-А", "31"])
        self.assertEqual(city["daily"]["2026-11-04"]["cold"], 3)
        buv = json.loads((self.out / "buvets/buvets.json").read_text(encoding="utf-8"))
        self.assertEqual(len(buv["items"]), 60)
        b3 = [b for b in buv["items"] if b["house"] == "3"][0]
        self.assertEqual(b3["complaints_7d"], 1)
        self.assertTrue(b3["generator"])
        self.assertTrue((self.out / "1551/months/2026-10.json.gz").exists())

    def test_area(self):
        extra = json.loads((self.src / "2026-11.json").read_text(encoding="utf-8")) + [
            row("a1", "2026-11-04T10:00Z", "Холодне водопостачання", "Відсутність ХВП",
                street="вул. Солов’яненка Анатолія (Бойченка Олександра)", house="4"),
            row("a2", "2026-11-04T11:00Z", "Ліфт", "Не працює пасажирський ліфт",
                street="вул. Ново-Дарницька", house="1"),
        ]
        (self.src / "2026-11.json").write_text(json.dumps(extra, ensure_ascii=False), encoding="utf-8")
        areas = {"pb": {"title": "Масив", "streets": ["Андрія Малишка", "Солов'яненка", "Дарницький", "Юності"]}}
        meta = c.run(self.out, self.now, self.src, BUVETS, areas)
        self.assertEqual(meta["areas"]["pb"], {"file": "areas/pb.json", "recent": "areas/pb-recent.json",
                                               "streets": 2, "n": 5, "n_recent": 4,
                                               "unmatched": ["Дарницький", "Юності"]})
        r = json.loads((self.out / "1551/areas/pb-recent.json").read_text(encoding="utf-8"))
        self.assertEqual(r["hours"], 48)
        self.assertEqual(len(r["items"]), 4)  # 30.10 уже не входит
        a = json.loads((self.out / "1551/areas/pb.json").read_text(encoding="utf-8"))
        self.assertEqual([s["name"] for s in a["streets"]],
                         ["вул. Андрія Малишка", "вул. Солов’яненка Анатолія (Бойченка Олександра)"])
        self.assertEqual(a["items"][0]["s"], "вул. Андрія Малишка")
        self.assertEqual(a["items"][0]["h"], "21-А")
        self.assertIn("вул. Солов’яненка Анатолія (Бойченка Олександра)", {i["s"] for i in a["items"]})

    def test_street_matching(self):
        self.assertTrue(c.street_matches("бульв. Дарницький", "Дарницький"))
        self.assertFalse(c.street_matches("вул. Ново-Дарницька", "Дарницький"))
        self.assertTrue(c.street_matches("вул. Солов’яненка Анатолія (Бойченка Олександра)", "Солов'яненка"))
        self.assertTrue(c.street_matches("вул. Князя Романа Мстиславича (Жмаченка Генерала)", "Романа Мстиславича"))
        self.assertFalse(c.street_matches("вул. Соловцова Миколи", "Солов'яненка"))

    def test_areas_file_valid(self):
        areas = c.load_areas(Path(c.__file__).with_name("areas.json"))
        self.assertEqual(len(areas["pivnichno-brovarskyi"]["streets"]), 8)

    def test_idempotent_and_stale_cleanup(self):
        c.run(self.out, self.now, self.src, BUVETS)
        before = {p: p.read_bytes() for p in (self.out / "1551/streets").glob("*.json")}
        c.run(self.out, self.now, self.src, None)  # бюветы — из прошлого файла
        after = {p: p.read_bytes() for p in (self.out / "1551/streets").glob("*.json")}
        self.assertEqual(before, after)
        # через месяц ноябрьское выпало из окна → файлы этих улиц удаляются
        dec = [row("d1", "2026-12-09T10:00Z", "Газифікація", "Відсутність газу", street="вул. Нова", house="1")]
        (self.src / "2026-12.json").write_text(json.dumps(dec, ensure_ascii=False), encoding="utf-8")
        later = dt.datetime(2026, 12, 10, 12, 0, tzinfo=dt.timezone.utc)
        meta = c.run(self.out, later, self.src, None)
        left = sorted(p.name for p in (self.out / "1551/streets").glob("*.json"))
        self.assertEqual(left, ["vul-nova.json"])
        self.assertEqual(sorted(meta["months"]), ["2026-10", "2026-11", "2026-12"])

    def test_refuses_broken_source(self):
        c.run(self.out, self.now, self.src, BUVETS)
        (self.src / "2026-11.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            c.run(self.out, self.now, self.src, None)


if __name__ == "__main__":
    unittest.main()
