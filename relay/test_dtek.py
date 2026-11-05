import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))

import dtek


class DtekTest(unittest.TestCase):
    def test_build_output(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            target_streets = ["вул. Малишка Андрія", "вул. Юності"]

            mock_result = {
                "banner": None,
                "is_global_emergency": False,
                "active_emergencies": [
                    {
                        "pattern": "вул. Малишка Андрія",
                        "matched_street": "вул. Андрія Малишка",
                        "house": "3",
                        "sub_type": "Аварійне відключення",
                        "reasons": ["Пошкодження кабельної лінії"],
                        "start_date": "2026-11-05 15:00",
                        "end_date": "2026-11-05 19:00",
                    }
                ],
                "streets": {
                    "вул. Малишка Андрія": {
                        "pattern": "вул. Малишка Андрія",
                        "matched_name": "вул. Андрія Малишка",
                        "has_emergency": True,
                        "emergencies_count": 1,
                        "emergencies": [],
                        "houses_total": 40,
                        "houses": {},
                        "preset": None,
                        "fact": None,
                    },
                    "вул. Юності": {
                        "pattern": "вул. Юності",
                        "matched_name": "вул. Юності",
                        "has_emergency": False,
                        "emergencies_count": 0,
                        "emergencies": [],
                        "houses_total": 20,
                        "houses": {},
                        "preset": None,
                        "fact": None,
                    },
                },
            }

            now = dt.datetime(2026, 11, 5, 15, 0, 0, tzinfo=dt.timezone.utc)
            dtek.build_output(out, target_streets, mock_result, now=now)

            # Перевіряємо згенеровані файли
            latest_file = out / "dtek" / "latest.json"
            self.assertTrue(latest_file.exists())
            latest_data = json.loads(latest_file.read_text(encoding="utf-8"))
            self.assertEqual(latest_data["summary"]["emergencies_count"], 1)
            self.assertTrue(latest_data["summary"]["has_active_emergencies"])

            em_file = out / "dtek" / "active_emergencies.json"
            self.assertTrue(em_file.exists())
            em_data = json.loads(em_file.read_text(encoding="utf-8"))
            self.assertEqual(len(em_data), 1)
            self.assertEqual(em_data[0]["house"], "3")

            status_file = out / "dtek" / "status.json"
            self.assertTrue(status_file.exists())
            status_data = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertEqual(status_data["status"], "EMERGENCY")

            area_file = out / "dtek" / "areas" / "pivnichno-brovarskyi.json"
            self.assertTrue(area_file.exists())
            area_data = json.loads(area_file.read_text(encoding="utf-8"))
            self.assertEqual(area_data["area"], "pivnichno-brovarskyi")
            self.assertTrue(area_data["has_emergencies"])

            # Перевіряємо згенеровані Markdown-таблиці
            readme_file = out / "dtek" / "README.md"
            self.assertTrue(readme_file.exists())
            readme_text = readme_file.read_text(encoding="utf-8")
            self.assertIn("Де зараз аварії та коли повернуть світло", readme_text)
            self.assertIn("Немає світла", readme_text)
            self.assertIn("3", readme_text)

            status_md = out / "dtek" / "status.md"
            self.assertTrue(status_md.exists())

            # Перевіряємо згенеровану HTML-сторінку
            html_file = out / "dtek" / "index.html"
            self.assertTrue(html_file.exists())
            html_text = html_file.read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE html>", html_text)
            self.assertIn("Де зараз аварії та коли повернуть світло", html_text)
            self.assertIn("Немає світла", html_text)


if __name__ == "__main__":
    unittest.main()
