#!/usr/bin/env python3
"""Ретранслятор даних про аварії ДТЕК для AnenjiMon (масив Північно-Броварський).

Запускається через GitHub Actions кожні 10 хвилин. Відкриває безголовий браузер
(Playwright Chromium) на сторінці https://www.dtek-kem.com.ua/ua/shutdowns,
проходить WAF (Cloudflare / Incapsula) та у сесії сторінки опитує внутрішній
AJAX ендпоінт ДТЕК (/ua/ajax) за 8 вулицями масиву:

  * вул. Малишка Андрія
  * вул. Солов’яненка Анатолія (Бойченка Олександра)
  * вул. Миропільська
  * бул. Дарницький
  * вул. Шалетт міста
  * вул. Юності
  * вул. Космічна
  * вул. Князя Романа Мстиславича (Генерала Жмаченка)

Результати записуються у директорію out/ (для гілки `data`):
  dtek/latest.json                     — повні дані по всіх 8 вулицях та будинках
  dtek/active_emergencies.json         — список тільки активних аварійних відключень
  dtek/status.json                     — компактний статус для легких віджетів
  dtek/areas/pivnichno-brovarskyi.json — зріз аварій по масиву Північно-Броварський
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR.parent))

# Імпортуємо правила нормалізації та зіставлення вулиць із city1551
try:
    from relay.city1551 import load_areas, street_matches, write_json
except ImportError:
    from city1551 import load_areas, street_matches, write_json

DTEK_URL = "https://www.dtek-kem.com.ua/ua/shutdowns"


def get_kyiv_time(now: dt.datetime | None = None) -> tuple[str, str]:
    """Повертає ISO UTC час та читабельний рядок за київським часом."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    # Kyiv: UTC+2 winter / UTC+3 summer
    # Використовуємо zoneinfo, якщо доступно, інакше зміщення
    try:
        import zoneinfo
        tz_kyiv = zoneinfo.ZoneInfo("Europe/Kyiv")
        kyiv_dt = now.astimezone(tz_kyiv)
    except Exception:
        # Резервний розрахунок для Києва (EET/EEST)
        kyiv_dt = now + dt.timedelta(hours=2)
    
    iso_utc = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    kyiv_str = kyiv_dt.strftime("%d.%m.%Y, %H:%M:%S")
    return iso_utc, kyiv_str


def scrape_dtek(target_streets: list[str]) -> dict:
    """Запускає Playwright і збирає дані про аварії з сайту ДТЕК."""
    from playwright.sync_api import sync_playwright

    print(f"Запуск Chromium через Playwright для {DTEK_URL}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
        )

        page = context.new_page()

        max_attempts = 3
        scrape_result = None

        for attempt in range(1, max_attempts + 1):
            try:
                print(f"Спроба {attempt}: завантаження сторінки...")
                resp = page.goto(DTEK_URL, wait_until="domcontentloaded", timeout=45000)
                status = resp.status if resp else None
                print(f"Статус відповіді: {status}")

                # Очікування проходження WAF (Cloudflare/Incapsula)
                page.wait_for_timeout(6000)

                scrape_result = page.evaluate(
                    """async (streetsToMonitor) => {
                        function norm(s) {
                            if (!s) return '';
                            return s.toLowerCase()
                                .replace(/['’`ʼ]/g, "'")
                                .replace(/вул\\.|вулиця|бульв\\.|бульвар|просп\\.|проспект|пров\\.|провулок/g, '')
                                .replace(/\\s+/g, ' ')
                                .trim();
                        }

                        // 1. Пошук адреси AJAX-обробника
                        const metaAjax = document.querySelector('meta[name="ajaxUrl"]')?.getAttribute('content');
                        const ajaxUrl = metaAjax || window.ajaxUrl || '/ua/ajax';

                        // 2. Оголошення або глобальний банер про екстрені відключення
                        const alertEls = Array.from(document.querySelectorAll('.alert, .discon-alert, .emergency-notice, [class*="alert"]'));
                        const bannerText = alertEls.map(el => el.innerText.trim()).filter(Boolean).join('\\n') || null;
                        const isGlobalEmergency = bannerText ? /аварі|екстрен/i.test(bannerText) : false;

                        // 3. Запит списку доступних вулиць
                        let allStreets = [];
                        let city = 'м. Київ';

                        try {
                            const f = new URLSearchParams();
                            f.append('method', 'getStreets');
                            const res = await fetch(ajaxUrl, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' },
                                body: f.toString()
                            });
                            if (res.ok) {
                                const json = await res.json();
                                if (json && json.streets) {
                                    if (Array.isArray(json.streets)) {
                                        allStreets = json.streets;
                                    } else if (typeof json.streets === 'object') {
                                        for (const c of Object.keys(json.streets)) {
                                            if (/київ/i.test(c)) city = c;
                                            const l = json.streets[c];
                                            if (Array.isArray(l)) allStreets.push(...l);
                                            else if (typeof l === 'object' && l !== null) {
                                                allStreets.push(...Object.keys(l), ...Object.values(l).filter(v => typeof v === 'string'));
                                            }
                                        }
                                    }
                                }
                            }
                        } catch (e) {
                            console.error('getStreets error:', e);
                        }

                        allStreets = Array.from(new Set(allStreets));

                        // 4. Опитування будинків для кожної вулиці
                        const streetResults = {};
                        const activeEmergencies = [];

                        for (const targetPattern of streetsToMonitor) {
                            // Знаходимо назву вулиці в базі ДТЕК
                            let matchedName = allStreets.find(s => {
                                const sNorm = norm(s);
                                const pNorm = norm(targetPattern);
                                const pWords = pNorm.split(' ').filter(w => w.length > 2);
                                return pWords.every(w => sNorm.includes(w));
                            });

                            if (!matchedName) {
                                matchedName = targetPattern;
                            }

                            const timestamp = Math.floor(Date.now() / 1000);
                            let homeData = null;

                            // Спроба 1: WordPress serializedArray
                            try {
                                const p1 = new URLSearchParams();
                                p1.append('method', 'getHomeNum');
                                p1.append('data[0][name]', 'city');
                                p1.append('data[0][value]', city);
                                p1.append('data[1][name]', 'street');
                                p1.append('data[1][value]', matchedName);
                                p1.append('data[2][name]', 'updateFact');
                                p1.append('data[2][value]', String(timestamp));

                                const r1 = await fetch(ajaxUrl, {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' },
                                    body: p1.toString()
                                });
                                if (r1.ok) homeData = await r1.json();
                            } catch (_) {}

                            // Спроба 2: прямі параметри
                            if (!homeData || !homeData.data) {
                                try {
                                    const p2 = new URLSearchParams();
                                    p2.append('method', 'getHomeNum');
                                    p2.append('city', city);
                                    p2.append('street', matchedName);
                                    p2.append('updateFact', String(timestamp));

                                    const r2 = await fetch(ajaxUrl, {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' },
                                        body: p2.toString()
                                    });
                                    if (r2.ok) {
                                        const j2 = await r2.json();
                                        if (j2 && j2.data) homeData = j2;
                                    }
                                } catch (_) {}
                            }

                            const streetEmergencies = [];
                            const housesInfo = {};
                            let housesCount = 0;

                            if (homeData && homeData.data) {
                                const raw = homeData.data;
                                housesCount = Object.keys(raw).length;

                                for (const [houseNo, info] of Object.entries(raw)) {
                                    const subType = info?.sub_type || '';
                                    const reasons = Array.isArray(info?.sub_type_reason)
                                        ? info.sub_type_reason
                                        : (info?.sub_type_reason ? [String(info.sub_type_reason)] : []);

                                    // Виявлення аварії / пошкодження
                                    const isEm = /аварі|екстрен|пошкодж|несправн|локальн/i.test(subType) ||
                                                 reasons.some(r => /аварі|екстрен|пошкодж|несправн|локальн/i.test(r));

                                    let outageType = 'ok';
                                    if (isEm) {
                                        outageType = 'emergency';
                                    } else if (/стабілізац/i.test(subType) || reasons.some(r => /gpv|стабілізац/i.test(r))) {
                                        outageType = 'stabilization';
                                    } else if (/планов/i.test(subType)) {
                                        outageType = 'planned';
                                    }

                                    housesInfo[houseNo] = {
                                        house: houseNo,
                                        outage_type: outageType,
                                        is_emergency: isEm,
                                        sub_type: subType,
                                        reasons: reasons,
                                        start_date: info?.start_date || null,
                                        end_date: info?.end_date || null
                                    };

                                    if (isEm) {
                                        const record = {
                                            pattern: targetPattern,
                                            matched_street: matchedName,
                                            house: houseNo,
                                            sub_type: subType,
                                            reasons: reasons,
                                            start_date: info?.start_date || null,
                                            end_date: info?.end_date || null
                                        };
                                        streetEmergencies.push(record);
                                        activeEmergencies.push(record);
                                    }
                                }
                            }

                            streetResults[targetPattern] = {
                                pattern: targetPattern,
                                matched_name: matchedName,
                                has_emergency: streetEmergencies.length > 0,
                                emergencies_count: streetEmergencies.length,
                                emergencies: streetEmergencies,
                                houses_total: housesCount,
                                houses: housesInfo,
                                preset: homeData?.preset || null,
                                fact: homeData?.fact || null
                            };
                        }

                        return {
                            banner: bannerText,
                            is_global_emergency: isGlobalEmergency,
                            streets_found_total: allStreets.length,
                            active_emergencies: activeEmergencies,
                            streets: streetResults
                        };
                    }""",
                    target_streets,
                )

                if scrape_result:
                    print("Дані ДТЕК успішно зібрано.")
                    break
            except Exception as err:
                print(f"Помилка спроби {attempt}: {err}")
                if attempt < max_attempts:
                    page.wait_for_timeout(8000)
                else:
                    browser.close()
                    raise

        browser.close()

    if not scrape_result:
        raise RuntimeError("Не вдалося зібрати дані з ДТЕК після кількох спроб.")

    return scrape_result


def build_output(out_dir: Path, target_streets: list[str], raw_result: dict, now: dt.datetime | None = None) -> None:
    """Формує структуровані JSON файли для гілки `data`."""
    iso_utc, kyiv_str = get_kyiv_time(now)

    dtek_dir = out_dir / "dtek"
    areas_dir = dtek_dir / "areas"
    areas_dir.mkdir(parents=True, exist_ok=True)

    active_emergencies = raw_result.get("active_emergencies", [])
    streets_data = raw_result.get("streets", {})

    # 1. Повний стан (dtek/latest.json)
    full_state = {
        "metadata": {
            "source": DTEK_URL,
            "generated_utc": iso_utc,
            "generated_kyiv": kyiv_str,
            "streets_count": len(target_streets),
        },
        "system": {
            "banner": raw_result.get("banner"),
            "is_global_emergency": raw_result.get("is_global_emergency", False),
        },
        "summary": {
            "has_active_emergencies": len(active_emergencies) > 0,
            "emergencies_count": len(active_emergencies),
            "streets_with_emergencies": [
                s for s, d in streets_data.items() if d.get("has_emergency")
            ],
        },
        "active_emergencies": active_emergencies,
        "streets": streets_data,
    }
    write_json(dtek_dir / "latest.json", full_state, pretty=True)

    # 2. Масив лише активних аварій (dtek/active_emergencies.json)
    write_json(dtek_dir / "active_emergencies.json", active_emergencies, pretty=True)

    # 3. Компактний статус (dtek/status.json)
    status_summary = {
        "status": "EMERGENCY" if active_emergencies else "OK",
        "last_updated_kyiv": kyiv_str,
        "last_updated_utc": iso_utc,
        "emergencies_count": len(active_emergencies),
        "global_alert": raw_result.get("is_global_emergency", False),
        "streets": [
            {
                "street": s,
                "has_emergency": d.get("has_emergency", False),
                "emergencies_count": d.get("emergencies_count", 0),
                "houses_total": d.get("houses_total", 0),
            }
            for s, d in streets_data.items()
        ],
    }
    write_json(dtek_dir / "status.json", status_summary, pretty=True)

    # 4. Файл конкретного масиву (dtek/areas/pivnichno-brovarskyi.json)
    area_file = {
        "area": "pivnichno-brovarskyi",
        "title": "Північно-Броварський масив",
        "generated_kyiv": kyiv_str,
        "has_emergencies": len(active_emergencies) > 0,
        "emergencies": active_emergencies,
        "streets": [
            {
                "pattern": s,
                "matched_name": d.get("matched_name"),
                "has_emergency": d.get("has_emergency", False),
                "emergencies_count": d.get("emergencies_count", 0),
                "houses_total": d.get("houses_total", 0),
            }
            for s, d in streets_data.items()
        ],
    }
    write_json(areas_dir / "pivnichno-brovarskyi.json", area_file, pretty=True)

    print(f"\n--- ПІДСУМОК ДТЕК ({kyiv_str}) ---")
    print(f"Знайдено аварій: {len(active_emergencies)}")
    for i, em in enumerate(active_emergencies, 1):
        reasons_str = "; ".join(em.get("reasons", []))
        print(f"  {i}. {em.get('matched_street')} б. {em.get('house')}: {em.get('sub_type')} ({reasons_str})")
    if not active_emergencies:
        print("  Аварійних відключень за 8 вулицями не зафіксовано.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Збір аварійних відключень ДТЕК.")
    parser.add_argument("--out", type=Path, default=Path("out"), help="Директорія виводу (data branch)")
    parser.add_argument("--areas-file", type=Path, default=Path("relay/areas.json"), help="Шлях до areas.json")
    args = parser.parse_args(argv)

    # Завантажуємо список 8 вулиць з relay/areas.json
    areas = load_areas(args.areas_file)
    pivn = areas.get("pivnichno-brovarskyi", {})
    target_streets = pivn.get("streets", [
        "вул. Малишка Андрія",
        "вул. Солов’яненка Анатолія",
        "вул. Миропільська",
        "бул. Дарницький",
        "вул. Шалетт міста",
        "вул. Юності",
        "вул. Космічна",
        "вул. Князя Романа Мстиславича",
    ])

    print(f"Моніторинг {len(target_streets)} вулиць масиву:")
    for s in target_streets:
        print(f"  - {s}")

    raw_result = scrape_dtek(target_streets)
    build_output(args.out, target_streets, raw_result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
